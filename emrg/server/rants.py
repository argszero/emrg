"""Shared rant logic — single source of truth for rants.jsonl.

Extracted from the daemon's ``rant`` handler (rant 2026-08-17T11:51:59: rant
submission moves from a command-only path to "Agent auto-detects in normal
conversation, confirms with the user, then calls the submit_rant tool").
Both the daemon ``rant`` command and the ``submit_rant`` tool call
:func:`append_rant`, so behavior stays identical.

Rant 2026-08-18T16:42:52: this module is also the only code allowed to
rewrite rants.jsonl — the ``submit_rant`` tool exposes ``list`` / ``update`` /
``cleanup`` actions on top of it so the evolution loop never hand-writes the
file with inline bash/python (the 2026-08-18 incident: format drift to array
rows, field loss, history pruning).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

# Canonical field order: timestamp → project → status → progress → completed → message
# (project right after timestamp per user feedback; message last)
_RANT_FIELDS = ("timestamp", "project", "status", "progress", "completed", "message")

# State machine: pending → in_progress → completed (never skip a level)
_ALLOWED_STATUS_TRANSITIONS = {
    "pending": {"pending", "in_progress"},
    "in_progress": {"in_progress", "completed"},
    "completed": {"completed"},
}


def _normalize_rant(raw) -> dict | None:
    """Normalize one parsed line to the canonical 6-field dict.

    Accepts dict rows (canonical). Converts legacy array rows
    ``[timestamp, project, status, progress, completed, message]`` back to
    dicts (the 2026-08-18 format-drift incident). Unknown field shapes /
    corrupt rows return None and are skipped.
    """
    if isinstance(raw, dict):
        return {k: raw.get(k) for k in _RANT_FIELDS}
    if isinstance(raw, list) and len(raw) == len(_RANT_FIELDS) and isinstance(raw[0], str):
        return dict(zip(_RANT_FIELDS, raw))
    return None


class RantRead(NamedTuple):
    """One read of the queue, and what it could not read.

    A result type rather than two functions, because the entries and the lines that
    failed to become entries come out of *one* pass: a caller that asked separately
    could be wrong about the same file twice.

    The tolerance above is deliberate (a corrupt line must not crash the queue), and it
    is also why this type exists. `_normalize_rant` returns None for a line that is not
    a dict and not a legacy array, and `json.JSONDecodeError` is caught per line — so a
    read can come back with *fewer* rants than the file has, silently. Measured
    2026-10-01: a file with one good rant and two unread lines answers `list_rants()`
    with **1** entry and the tool prints `No rants match.`-shaped output with no mention
    of the other two. Every writer rewrites the file whole from what was read
    (`_write_rants`), so the next `append` / `update` / `cleanup` **deletes** those lines
    — measured on the same fixture: two lines on disk before the append, gone after it.
    That deletion is a state the host has to be able to see, which is what `unreadable`
    is for; whether the write should also refuse is a separate question and a separate
    change.

    :param rants: the entries that were read, in file order.
    :param unreadable: the 1-based file line numbers that could not become an entry
        (unparseable JSON, or JSON that is neither a dict nor a legacy array row).
    """

    rants: list[dict]
    unreadable: tuple[int, ...]


class RantStoreMissing(RuntimeError):
    """The queue file is not there — which is **not** the same as a queue with no rants.

    A file that exists and holds no rows answers "no rants" honestly: the store was read
    and it was empty. A file that is *missing* answers nothing, and the two used to be
    collapsed into that one answer here. Measured 2026-10-02 on this host: `~/.emrg/rants.jsonl`
    did not exist, and `submit_rant(action="list")` — the reading the evolution prompt
    mandates for the queue — replied `No rants match.`, while the sibling reader
    `scripts/check-issue-links.py::load_rant_rows` raised for that identical state and its
    caller reported *unmeasurable* (exit 2). One file, two readers, two opposite answers,
    each written down as the rule: the guard's docstring said an empty answer here "would
    print 'no rants' on a host whose ledger simply is not there, a confident wrong verdict
    about a queue that may be fine", and this module's said the opposite.

    The cost is asymmetric, which is why the guard's rule is the one kept: a missing store
    answered as an empty queue tells a cycle "nothing pending" — the honest-looking wrong
    "nothing to evolve" that `review-queue.py` exists to prevent — and the host never learns
    the queue is gone (on this host it had held 39 rows the day before). Answered as
    unmeasurable, a genuinely fresh host is told the store is not there and loses nothing.

    The write path is deliberately *not* strict: `append_rant` must be able to create the
    file, so its pre-read goes through `_read_for_write`, which states that difference
    where it is made rather than by an `if` at the call site.
    """

    def __init__(self, rants_log: Path, cause: OSError | None = None):
        self.rants_log = rants_log
        self.cause = cause
        reason = f": {cause}" if cause else " (it does not exist)"
        super().__init__(
            f"the rant queue could not be read ({rants_log}){reason} - this is not "
            "\"no rants\": a store that is not there has not answered anything"
        )


def read_rants(rants_log: Path) -> RantRead:
    """Read the queue, reporting the lines that did not become entries.

    The one reader for this file. The daemon's rant panel used to carry its own inline
    `json.loads` loop beside this one, with a different rule — `r.get("status")` on a row
    that parsed to a *list* raised `AttributeError` inside the message handler (measured
    2026-10-01: a legacy array row, the exact shape `_normalize_rant` exists to convert,
    made the panel raise on both the filtered and the unfiltered path). One file, two
    readers, and the second one crashed where the first tolerated — so the rule lives
    here and the panel reads through it.

    A store that is missing or unreadable **raises** rather than reading as empty: the
    question every caller asks is what the queue *holds*, and a store that is not there
    leaves it unanswered. An empty answer would be indistinguishable from a queue whose
    rants are all handled, which is a verdict no reading supports.

    :param rants_log: the queue file.
    :raises RantStoreMissing: the file is not there, or cannot be opened.
    :returns: the entries and the unreadable line numbers.
    """
    rants: list[dict] = []
    unreadable: list[int] = []
    try:
        with open(rants_log, encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    unreadable.append(number)
                    continue
                entry = _normalize_rant(raw)
                if entry is None:
                    unreadable.append(number)
                    continue
                rants.append(entry)
    except OSError as exc:
        # Missing and unreadable are one state for this reader: neither one answered.
        raise RantStoreMissing(rants_log, exc) from exc
    return RantRead(rants, tuple(unreadable))


def _read_for_write(rants_log: Path) -> RantRead:
    """The write path's pre-read: a store that is not there yet holds no rows to keep.

    The only caller is `append_rant`, whose whole job is to create the file when the host
    submits the first rant — so "missing" here means "nothing to carry forward", not
    "nothing was measured". A store that exists but cannot be *opened* still raises: that
    one is never a licence to overwrite whatever is in it.
    """
    try:
        return read_rants(rants_log)
    except RantStoreMissing as exc:
        if isinstance(exc.cause, FileNotFoundError):
            return RantRead([], ())
        raise


def lines_a_write_would_drop(rants_log: Path) -> tuple[int, ...]:
    """The lines the next write will destroy, in 1-based file order.

    Every writer here rewrites the file whole from what the read saw (`_write_rants`
    emits `rants` and nothing else), so a line that did not become an entry is gone the
    moment anything is appended, updated or cleaned up. This is that fact, read *before*
    the write, for the callers that report it.

    It is one home rather than one per caller because the two callers have already
    disagreed once. Measured 2026-10-02 on this host, on a ledger holding one good row and
    one `{not json …}` line:

        submit_rant (tool)  -> warns, naming the line it is about to drop
        msg_type="rant"     -> {"ok": True, "count": 2}; the line is gone, and the frame
                               says nothing about it

    The tool was not wrong and the daemon was not lying — it simply never asked. The
    host's own client (GUI/TUI rant panel) writes through the second path, so the silence
    fell exactly on the caller that cannot see the file.

    A store that is not there answers `()` — there is nothing to lose, and this is the
    state `append_rant` creates the file from. A store that exists but cannot be opened
    raises, because "cannot read what is in there" and "nothing is in there" are the two
    facts this module keeps apart everywhere else.
    """
    return _read_for_write(rants_log).unreadable


def _write_rants(rants_log: Path, rants: list[dict]) -> None:
    """Sort by timestamp ascending and rewrite the file (dict 6-field order,
    ensure_ascii=False — the ONLY writer for rants.jsonl)."""
    rants.sort(key=lambda r: r.get("timestamp", ""))
    rants_log.parent.mkdir(parents=True, exist_ok=True)
    with open(rants_log, "w", encoding="utf-8") as f:
        for r in rants:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_rant(rants_log: Path, message: str, project: str = "") -> int:
    """Append a rant entry to ``rants_log``, sorted by timestamp.

    Args:
        rants_log: Path to rants.jsonl (e.g. ``~/.emrg/rants.jsonl``).
        message: The rant body (already user-confirmed / polished).
        project: Optional target project name (empty = EMRG itself).

    Returns:
        The new total rant count.
    """
    # Timestamp is daemon-authoritative local time (rant 2026-08-07T13:34Z):
    # clients previously supplied timestamps — GUI sent new Date().toISOString()
    # (UTC, 8h behind on UTC+8 hosts), TUI sent naive local time. A tz-aware
    # local ISO timestamp (+08:00) is self-describing, sorts correctly, and is
    # consistent regardless of which client submitted the rant.
    entry: dict = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "project": project,
        "status": "pending",
        "progress": None,
        "completed": None,
    }
    # message last, so status fields stay visible when scanning the file
    entry["message"] = message

    rants = _read_for_write(rants_log).rants
    rants.append(entry)
    _write_rants(rants_log, rants)

    return len(rants)


def list_rants(
    rants_log: Path,
    status: str | None = None,
    project: str | None = None,
) -> list[dict]:
    """Return rant entries, optionally filtered by status and/or project."""
    return [
        r for r in read_rants(rants_log).rants
        if (status is None or r.get("status") == status)
        and (project is None or r.get("project") == project)
    ]


def update_rant(
    rants_log: Path,
    timestamp: str,
    status: str | None = None,
    progress: str | None = None,
    completed: str | None = None,
) -> tuple[bool, str]:
    """Update a rant identified by its timestamp.

    Validates the status state machine (pending → in_progress → completed,
    never skip a level). When the status transitions to ``completed`` the
    ``completed`` timestamp is auto-written (ISO local time); leaving
    ``completed`` clears the field.

    Returns:
        ``(ok, message)`` — ok=False with a reason on invalid transition /
        unknown timestamp.
    """
    rants = read_rants(rants_log).rants
    for r in rants:
        if r.get("timestamp") != timestamp:
            continue
        current = r.get("status") or "pending"
        if status is not None:
            if status not in ("pending", "in_progress", "completed"):
                return False, f"invalid status: {status!r} (pending/in_progress/completed)"
            if status != current:
                allowed = _ALLOWED_STATUS_TRANSITIONS.get(current, set())
                if status not in allowed:
                    return False, (
                        f"invalid transition: {current} -> {status} "
                        f"(must be pending→in_progress→completed, no skipping)"
                    )
                r["status"] = status
                if status == "completed":
                    r["completed"] = datetime.now().astimezone().isoformat()
                else:
                    r["completed"] = None
        if progress is not None:
            r["progress"] = progress
        if completed is not None:
            r["completed"] = completed
        _write_rants(rants_log, rants)
        return True, f"updated rant {timestamp}: status={r.get('status')!r}"
    return False, f"rant not found: {timestamp}"


def cleanup_rants(rants_log: Path, keep: int = 10) -> int:
    """Prune old completed rants, keeping all pending/in_progress plus the
    ``keep`` most recent completed (by completed timestamp, fallback
    timestamp). Returns the total number of entries kept."""
    rants = read_rants(rants_log).rants
    active = [r for r in rants if r.get("status") != "completed"]
    completed = [
        r for r in rants if r.get("status") == "completed"
    ]
    completed.sort(key=lambda r: r.get("completed") or r.get("timestamp") or "")
    kept_completed = completed[-keep:] if keep > 0 else []
    _write_rants(rants_log, active + kept_completed)
    return len(active) + len(kept_completed)
