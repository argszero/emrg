"""The rolling 24-hour token ledger — one file, one window, no session arithmetic.

Rant 2026-11-19T09:31:00: the status band's token figure was *this turn's* total, so
switching sessions, closing the window or restarting the GUI reset it to zero, and the
host could not answer "how much did I spend on this today". The window is that answer:
the daemon sums the last 24 hours, whether or not any session is open, and a client
reads the number rather than keeping a total of its own.

Three segments, never a sum (the rant's item 2): ``input`` is ``prompt_tokens`` with
the cache hits inside it, because that is how the provider reports it; ``cache`` is
``cache_hit_tokens``, a subset of input; ``output`` is ``completion_tokens``. A field
that no in-window response reported is **None**, never 0 — a client shows ``—`` for
"not measured", while a zero would claim that nothing was spent.

The ledger lives in a file so a daemon restart does not start the day over (item 5):
the window is read from disk, not from a counter in memory. Only records inside the
window are summed, and the file is rewritten once stale records have piled up, so its
size is bounded by the window rather than by uptime.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

from emrg.tools.base import non_regular_kind

logger = logging.getLogger(__name__)

#: The window's width. The rant names it, and both the sum and the prune read it, so a
#: change moves them together instead of leaving one behind.
WINDOW_SECONDS = 24 * 60 * 60

#: Segment name → the provider field it sums. The order is the order they are shown in.
_SEGMENTS = (
    ("input", "prompt_tokens"),
    ("cache", "cache_hit_tokens"),
    ("output", "completion_tokens"),
)

#: Rewrite the file once it passes this size. A record is well under 100 bytes, so this
#: holds tens of thousands of rounds — far more than 24 hours of any real session — and
#: the rewrite is what keeps a long-lived daemon's file from growing without bound.
_MAX_BYTES = 2 * 1024 * 1024


def _a_count(value: object) -> int | None:
    """``value`` as a token count, or None when it is not one.

    A provider reports these as numbers; anything else — a missing key, a string, a
    ``bool`` (which ``isinstance(..., int)`` would otherwise admit) — is "not measured"
    rather than zero. A negative count is refused for the same reason: a sum must not be
    talked down by a field that means something else.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


def _refuse_non_regular(path: Path) -> str | None:
    """The kind of ``path`` when it is not a regular file, else None.

    Asked **before** every open in this module, because opening is what decides whether the
    caller returns at all: a FIFO blocks at the open until a peer appears and raises nothing,
    so no ``except OSError`` bounds it — and these opens run on the daemon's path, where that
    block is the whole daemon and the only recovery is a restart, which belongs to the host
    alone.

    Absence is not a refusal. :func:`non_regular_kind` **stats**, so the ledger's ordinary
    first record — a file that is not there yet — arrives as an ``OSError``, and that is the
    case this answers None for.
    """
    try:
        return non_regular_kind(path)
    except OSError:
        return None


class UsageLedger:
    """Append one record per LLM response, answer the last 24 hours.

    ``clock`` is the ledger's only source of "now", so a test can place records on both
    sides of the window's edge without sleeping: the window a caller asks for and the
    instant a record is stamped with are then the same clock.
    """

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock

    # ── writing ───────────────────────────────────────────────

    def record(self, usage: dict | None, session_id: str | None = None) -> None:
        """Append the provider's usage for one response, if it carries any count.

        Item 6 of the rant: a response with no usage is not an entry. An endpoint that
        reports only one of the three fields still records that one — a partial
        measurement is a measurement, and dropping the record would lose it.

        ``session_id`` is the session the round belonged to, written beside the counts so
        the window can answer one session's share as well as the machine's. It is
        optional because the ledger outlives any caller that names one: a row without it
        is a row from before this field existed, and it counts towards the machine and
        towards no session (issue #3072).
        """
        if not isinstance(usage, dict):
            return
        entry: dict[str, object] = {"ts": round(float(self._clock()), 3)}
        if isinstance(session_id, str) and session_id:
            entry["sid"] = session_id
        measured = False
        for _, key in _SEGMENTS:
            count = _a_count(usage.get(key))
            if count is None:
                continue
            entry[key] = count
            measured = True
        if not measured:
            return
        kind = _refuse_non_regular(self._path)
        if kind:
            # A FIFO here blocks the append until a reader appears, and this append runs in
            # the round that just finished, on the daemon's event loop: the line is dropped
            # rather than the daemon. Nothing is claimed from it — the window simply does
            # not count it, and the log says why the number is short.
            logger.warning(
                "usage ledger %s is a %s, not a regular file — this round is not recorded",
                self._path,
                kind,
            )
            return
        self._prune_if_stale()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _prune_if_stale(self) -> None:
        """Drop records that have left the window once the file has grown past its cap.

        Rewriting on every append would cost the file's length per round; doing it only
        past the cap keeps the append cheap while the file stays bounded. A file that
        cannot be read is left alone — the next append tries again, and a window read
        still filters by timestamp, so stale rows never reach a sum.
        """
        try:
            if self._path.stat().st_size <= _MAX_BYTES:
                return
        except OSError:
            return
        if _refuse_non_regular(self._path):
            return
        edge = float(self._clock()) - WINDOW_SECONDS
        kept = [entry for entry in self._read() if float(entry["ts"]) >= edge]
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                for entry in kept:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return

    # ── reading ───────────────────────────────────────────────

    def _read(self) -> list[dict]:
        """Every readable record, oldest first. A torn or foreign line is skipped.

        The file is appended to by this process alone, but a write can still be torn by
        a kill between the open and the flush; one unparsable line must not cost the
        window every other record.
        """
        if _refuse_non_regular(self._path):
            return []
        try:
            text = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(entry, dict) and isinstance(entry.get("ts"), (int, float)):
                records.append(entry)
        return records

    def window(self, now: float | None = None, session_id: str | None = None) -> dict:
        """The three sums over ``now``-24h … ``now``, each None when nothing reported it.

        The near edge is inclusive: a record stamped exactly 24 hours ago is still in the
        window, and one a minute older is not (the rant's boundary). ``window_start`` and
        ``window_end`` ride along so a client can say what it shows instead of assuming
        the daemon's clock agrees with its own.

        Two readings come back, and neither is derived from the other: the top-level three
        are the **machine's** last 24 hours — every session, every background round — and
        ``session`` is the same three segments for ``session_id`` alone. Item 5 of issue
        #3072: a call that names no session gets ``None`` for all three of its segments and
        still gets the machine's, because answering the machine figure under a session's
        name would claim that one conversation spent the whole day.
        """
        end = float(self._clock()) if now is None else float(now)
        start = end - WINDOW_SECONDS
        return self._sums(self._window_entries(start, end), start, end, session_id)

    def reading(self, now: float | None = None, session_id: str | None = None) -> dict:
        """``window()`` plus the same window grouped by session, from **one** read.

        Rant 2026-11-20T17:30:50 asks a client to rank one segment, so it needs every
        session's three segments and its call count — and it needs those to reconcile with
        the machine totals the status band already shows. Both come from the same records
        here, in one pass, for two reasons: two passes over a 2 MB file every poll is a
        cost this answer cannot justify, and two passes could straddle an append, so a
        client's breakdown would not add up to the total printed beside it.

        ``groups`` is the **only** account of the window's contents: the machine's three
        top-level figures are the sum of these rows, so item 7's reconciliation is an
        identity rather than a second measurement that could disagree.
        """
        end = float(self._clock()) if now is None else float(now)
        start = end - WINDOW_SECONDS
        entries = self._window_entries(start, end)
        out = self._sums(entries, start, end, session_id)
        out["groups"] = self._groups(entries)
        return out

    def _window_entries(self, start: float, end: float) -> list[dict]:
        """The records inside ``start``…``end``, both edges read the same way.

        One read, shared by every answer about this window: a window narrowed in one
        method and widened in the other is the way two readings of one file start
        disagreeing, and the near edge is the one the rant names explicitly.
        """
        return [
            entry for entry in self._read()
            if isinstance(entry.get("ts"), (int, float))
            and start <= float(entry["ts"]) <= end
        ]

    def _sums(
        self, entries: list[dict], start: float, end: float,
        session_id: str | None,
    ) -> dict:
        """The three sums over ``entries`` — the machine's, and one session's beside it."""
        named = session_id if isinstance(session_id, str) and session_id else None
        sums: dict[str, int] = {}
        mine: dict[str, int] = {}
        for entry in entries:
            # A row written before rows carried a session belongs to the machine and to
            # nobody else (item 4): `entry.get("sid")` is None for it, and `named` never is.
            is_mine = named is not None and entry.get("sid") == named
            for name, key in _SEGMENTS:
                count = _a_count(entry.get(key))
                if count is None:
                    continue
                sums[name] = sums.get(name, 0) + count
                if is_mine:
                    mine[name] = mine.get(name, 0) + count
        return {
            "window_start": start,
            "window_end": end,
            "input": sums.get("input"),
            "cache": sums.get("cache"),
            "output": sums.get("output"),
            "session": {name: mine.get(name) for name, _ in _SEGMENTS},
        }

    def _groups(self, entries: list[dict]) -> list[dict]:
        """``entries`` grouped by the session each record names, the unnamed volume last.

        A record written before rows carried a session lands in the row whose ``session``
        is ``None`` — item 5: its volume is real and so it is reported, and it belongs to
        no conversation, so no client may open it as one. Every segment a group never
        measured is ``None``, the same rule the window's own sums keep.

        ``calls`` counts records, not tokens: it is the denominator of a session's output
        per call, so it has to be a measurement of its own rather than something derived
        from a segment that may be missing.
        """
        by_session: dict[str, dict] = {}
        unnamed: dict = {"session": None, "calls": 0}
        for entry in entries:
            counts = {}
            for name, key in _SEGMENTS:
                count = _a_count(entry.get(key))
                if count is not None:
                    counts[name] = count
            if not counts:
                continue
            sid = entry.get("sid")
            group = unnamed
            if isinstance(sid, str) and sid:
                group = by_session.setdefault(sid, {"session": sid, "calls": 0})
            group["calls"] += 1
            for name, count in counts.items():
                group[name] = group.get(name, 0) + count
        return [
            {
                "session": group["session"],
                "calls": group["calls"],
                "input": group.get("input"),
                "cache": group.get("cache"),
                "output": group.get("output"),
            }
            for group in [*by_session.values(), unnamed]
        ]
