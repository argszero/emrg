"""How many times in a row one cause has aborted a turn, and when it last did.

Rant 2026-09-28T15:57:31, requirement L3 (「同一原因连续 abort 要能一眼看出」).
The measurement that made it a requirement: **53 aborted cycles over two days**,
every one of them caused by the same content-filter trigger, and nothing anywhere
said "this is the 53rd". Each abort was reported on its own — a WARNING per rung of
the retry ladder, an ERROR when the ladder was spent — so a run was legible only to
a reader counting log lines by hand, which is the reading the host then had to do.

What a run is, and why it is keyed the way it is
------------------------------------------------
Consecutive aborts *of the same cause, in the same session*. Same cause, because
two causes failing alternately is not a run of either. Same session, because those
53 aborts belong to the task that produced them — a single global counter would
report one task's collapse as every task's, which is the wrong answer to "is this
one thing broken or many".

A round the cause did not block ends the run. The number answers "how many in a
row", so any success anywhere in between makes it a different run; the count is a
fact about consecutiveness, not a lifetime total (the log keeps the total).

The state is a file, not a process variable
-------------------------------------------
``~/.emrg/logs/abort-runs.json``, beside the task-run records the host already
reads. A restart must not make the host start counting again, and a run the host
can open is worth more than one only the daemon can see.

What it may never do is fail a turn
-----------------------------------
Every read and write here is best-effort. An unreadable file reads as "no run
open"; an unwritable one logs and returns. A turn that has already lost its answer
to a content filter must not also lose the explanation of why.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from emrg.config import config_dir
from emrg.server.atomic import atomic_write_bytes

# The "is this path a regular file" rule is the tool layer's, reached at a path. Imported
# rather than restated, for the reason `emrg/sessions_index.py` gives at the same import:
# the boundary is one fact, and a second spelling of it is a second boundary that can drift.
from emrg.tools.base import non_regular_kind

logger = logging.getLogger(__name__)


#: A run that has not aborted for this long is history rather than a run, so it is
#: dropped when the file is next written. Without it a session that aborted once
#: and was then never used again would keep its entry for good.
RUN_TTL_DAYS = 7

#: Under the daemon's log directory (``config_dir() / "logs"``), beside
#: ``task-runs/`` — the directory the host already opens when asking what the
#: daemon has been doing.
STATE_FILENAME = "abort-runs.json"


def default_path() -> Path:
    """Where run state lives. Read at call time so a test can redirect one."""
    return config_dir() / "logs" / STATE_FILENAME


def _key(cause: str, session_id: str) -> str:
    """One run's identity: the cause and the session it aborted."""
    return f"{cause}:{session_id}"


def _parse(stamp: object) -> datetime | None:
    """An ISO stamp back to a comparable instant, or ``None`` if it is not one."""
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Written with an offset (``datetime.now().astimezone()``), so a naive
        # one can only be a hand-edit: read it as local, which is what a hand
        # edit means.
        parsed = parsed.astimezone()
    return parsed


class AbortRuns:
    """Consecutive aborts by one cause in one session, and when each last fired."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_path()
        self._state: dict[str, dict] | None = None

    # -- reading ----------------------------------------------------------

    def snapshot(self) -> dict[str, dict]:
        """Every run currently open, keyed ``"<cause>:<session_id>"``."""
        return {key: dict(entry) for key, entry in self._load().items()}

    def run(self, cause: str, session_id: str) -> dict | None:
        """The open run for this cause and session, or ``None``."""
        entry = self._load().get(_key(cause, session_id))
        return dict(entry) if entry else None

    # -- writing ----------------------------------------------------------

    def note(self, cause: str, session_id: str, *, now: datetime | None = None) -> dict:
        """Record one abort and return the run it belongs to.

        The record carries ``count`` (how many in a row), ``first_at`` (when the
        run began) and ``last_at`` — the three facts the reading is made of, and
        the three a log line is worth printing.

        ``now`` is **the clock this operation is about**, not merely the stamp's
        source: the write it triggers prunes with the same instant (see
        :func:`_save`), so a caller who injects one gets a fully deterministic
        result. Honouring it in the stamp and ignoring it in the prune is how a
        back-dated abort came to be silently dropped by the wall clock — a suite
        that goes red on a calendar date rather than on a change.
        """
        stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
        state = self._load()
        entry = state.get(_key(cause, session_id))
        if entry is None:
            entry = {
                "cause": cause,
                "session_id": session_id,
                "count": 1,
                "first_at": stamp,
                "last_at": stamp,
            }
            state[_key(cause, session_id)] = entry
        else:
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last_at"] = stamp
        self._save(now=now)
        return dict(entry)

    def clear(self, cause: str, session_id: str) -> None:
        """End the run: a round the cause did not block."""
        if self._load().pop(_key(cause, session_id), None) is not None:
            self._save()

    # -- storage ----------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        if self._state is None:
            self._state = self._read()
        return self._state

    def _read(self) -> dict[str, dict]:
        """The stored state; unreadable, missing and non-regular all read as "nothing open".

        The subject's **kind** is asked before it is opened, because opening it is what decides
        whether this call returns at all: `read_text` on a FIFO waits at the *open* for a writer,
        raises nothing, and so the handlers below are unreachable — no exception ever reaches
        them and the call simply never ends. Measured 2026-10-11 (`cyc20261011-130932`) on master
        `3f18f6a6`, one child process per arm under an 8 s cap: a `mkfifo` at the state path left
        `AbortRuns.snapshot()` and `AbortRuns.note()` **not returned in 8 s**, where the same
        fixture with a regular file returned in 0.00 s.

        Why it matters more here than elsewhere in this family: `note()` is called from the
        content-filter path — `emrg/server/daemon.py:4352` / `:4410`, the moment a turn has just
        lost its answer and the retry ladder is spent — as the argument of the `logger.error` that
        reports the run, with the `_broadcast` of the error frame *after* it. The block therefore
        hangs the daemon exactly where the client is waiting for the explanation of a failed turn.

        Degraded rather than raised, which is this module's own contract rather than a preference
        — see the module docstring: *"Every read and write here is best-effort. An unreadable file
        reads as 'no run open'"*. A refusal would make the abort path raise while it is trying to
        *report* an abort. It is safe for the reason `emrg/sessions_index.py::_load` gives at the
        same answer: the state is re-derived on the next `note()`, and `_save` is
        `atomic_write_bytes` (`mkstemp` + `os.replace`), which renames over the destination
        instead of opening it — measured, a FIFO destination returns at once — so there is no
        block for a reader-side guard to move onto the writer.

        The kind is named in the log so the omission is diagnosable rather than silent.
        """
        try:
            kind = non_regular_kind(self._path)
        except OSError:
            # Absent (or unstattable) — the empty state a missing file has always got. The
            # `FileNotFoundError` branch this replaces is now this one: `non_regular_kind`
            # *stats*, so absence arrives as an OSError rather than as a `None` kind.
            return {}
        if kind:
            logger.warning(
                "abort-run state at %s is %s, not a regular file — counting from scratch",
                self._path, kind,
            )
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning(
                "abort-run state at %s is unreadable (%s) — counting from scratch",
                self._path, exc,
            )
            return {}
        if not isinstance(raw, dict):
            logger.warning(
                "abort-run state at %s is not an object — counting from scratch",
                self._path,
            )
            return {}
        return {
            key: entry
            for key, entry in raw.items()
            if isinstance(key, str) and isinstance(entry, dict)
        }

    def _save(self, *, now: datetime | None = None) -> None:
        """Write the state, pruning against `now` (the wall clock when omitted).

        `now` is threaded in from :meth:`note` so the clock the caller injected
        is the one the prune measures against, not two clocks disagreeing about
        the same record.
        """
        state = self._load()
        _prune(state, now=now)
        try:
            atomic_write_bytes(
                json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                self._path,
            )
        except OSError as exc:
            # Losing the count costs a later reader one question; raising here
            # would cost the turn its explanation.
            logger.warning("could not write abort-run state to %s (%s)", self._path, exc)


def _prune(state: dict[str, dict], *, now: datetime | None = None) -> None:
    """Drop the runs whose last abort is older than :data:`RUN_TTL_DAYS`."""
    moment = now or datetime.now().astimezone()
    if moment.tzinfo is None:
        # The same reading `_parse` gives a naive stamp: as local time. A caller
        # may inject a naive clock (the tests do), and comparing one against the
        # aware stamp `_parse` returns would raise instead of pruning.
        moment = moment.astimezone()
    cutoff = moment - timedelta(days=RUN_TTL_DAYS)
    for key, entry in list(state.items()):
        last = _parse(entry.get("last_at"))
        if last is None or last < cutoff:
            del state[key]
