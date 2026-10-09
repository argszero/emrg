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


def _read_rants(rants_log: Path) -> list[dict]:
    """Read all rant entries, tolerantly converting legacy array rows to dicts."""
    rants: list[dict] = []
    if not rants_log.exists():
        return rants
    with open(rants_log, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry = _normalize_rant(raw)
            if entry is not None:
                rants.append(entry)
    return rants


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

    rants = _read_rants(rants_log)
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
        r for r in _read_rants(rants_log)
        if (status is None or r.get("status") == status)
        and (project is None or r.get("project") == project)
    ]


def _instant(text) -> datetime | None:
    """`text` as an instant, or `None` when it is not one (never a silent zero)."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _completion_ordering_error(timestamp: str, completed: str) -> str | None:
    """Why `completed` may not be written on the row submitted at `timestamp`, or `None`.

    The retention rule ranks a completed rant by ``completed or timestamp`` (see
    `cleanup_rants`), and `scripts/check-issue-links.py` reads that same key to decide whether
    the *absence* of a record a rant citation names can be explained by pruning at all. Its
    loudest sentence — "the retention rule cannot explain the absence ... this ledger never
    held it" — counts the completed records **at or after** the cited instant and therefore
    rests on one premise: **a completion stamp is never earlier than the row's own submission
    instant**. Prose cannot hold that premise: a record whose stamp precedes its submission
    ranks *below* an instant it was submitted after, so the store can prune it while the
    reading counts it as never held — a definite sentence about a record that really existed.

    So the premise is enforced where the field is written rather than assumed by its reader,
    and enforced **fail-closed**: a stamp that cannot be *shown* to follow the submission
    instant is refused too, not accepted as probably fine. The three ways that happens — the
    stamp is not an instant, the row's own timestamp is not one, and the two disagree about
    carrying a UTC offset (a naive instant and an aware one have no order at all) — are each
    named in the message, because "refused" without the reason is a second problem.
    """
    submitted = _instant(timestamp)
    stamp = _instant(completed)
    if stamp is None:
        return (
            f"invalid completed: {completed!r} is not an ISO instant "
            "(the stamp is the key the retention rule ranks a completed rant by)"
        )
    if submitted is None:
        return (
            f"invalid completed: the rant's submission instant {timestamp!r} is not an ISO "
            "instant, so a completion stamp cannot be shown to follow it"
        )
    if (stamp.tzinfo is None) != (submitted.tzinfo is None):
        return (
            f"invalid completed: {completed!r} and the submission instant {timestamp!r} "
            "cannot be ordered (one carries a UTC offset and the other does not) - spell "
            "the stamp with the same offset as the timestamp it belongs to"
        )
    if stamp < submitted:
        return (
            f"invalid completed: {completed!r} precedes the rant's submission instant "
            f"{timestamp!r} - a rant cannot be completed before it was submitted, and the "
            "retention rule would rank the record below an instant it was submitted after"
        )
    return None


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

    The ``completed`` timestamp is the ``completed`` status's shadow: it is written
    **only** when the resulting status is ``completed`` — auto-written by the
    transition when none is given, or replaced by an explicit one — and cleared when
    the status moves off ``completed``. An explicit stamp whose resulting status is
    anything else is refused, so no row carries a stamp the state machine calls unfinished.

    The completion stamp this call **writes** — explicit, or auto-written by the
    transition — is checked against the row's own submission instant and the
    update is refused when it does not follow it (`_completion_ordering_error`
    carries why). Nothing is written when the update is refused.

    Returns:
        ``(ok, message)`` — ok=False with a reason on invalid transition /
        unknown timestamp / a completion stamp out of order or off the
        ``completed`` status.
    """
    rants = _read_rants(rants_log)
    for r in rants:
        if r.get("timestamp") != timestamp:
            continue
        current = r.get("status") or "pending"
        target = current
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
                target = status
        # A completion stamp is the completed status's shadow, and it is written only with it.
        # Without this the two branches disagreed: the transition cleared the field for every
        # target but `completed`, while the explicit-argument branch wrote it unconditionally
        # further down — so `status='in_progress', completed=<stamp>` (and a bare `completed`
        # on a `pending` row) stored a completion stamp on a row the state machine calls
        # unfinished, and `action="list"` printed both facts in one row.
        if completed is not None and target != "completed":
            return False, (
                f"invalid completed: {completed!r} is a completion stamp, but the resulting "
                f"status would be {target!r} - a completion stamp is written only together "
                f"with status='completed'"
            )
        # The stamp the transition writes, or the explicit one when given: validated as the
        # value it is about to store, so the auto-written path is covered too (a row whose
        # own timestamp is in the future cannot be completed either).
        stamp = completed
        if stamp is None and target == "completed" and current != "completed":
            stamp = datetime.now().astimezone().isoformat()
        if stamp is not None:
            problem = _completion_ordering_error(timestamp, stamp)
            if problem:
                return False, problem
        if target != current:
            r["status"] = target
            r["completed"] = stamp if target == "completed" else None
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
    rants = _read_rants(rants_log)
    active = [r for r in rants if r.get("status") != "completed"]
    completed = [
        r for r in rants if r.get("status") == "completed"
    ]
    completed.sort(key=lambda r: r.get("completed") or r.get("timestamp") or "")
    kept_completed = completed[-keep:] if keep > 0 else []
    _write_rants(rants_log, active + kept_completed)
    return len(active) + len(kept_completed)
