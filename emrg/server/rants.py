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


def _parse_instant(text: object) -> datetime | None:
    """`text` as an instant, or `None` when it is not one (never a silent zero)."""
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _stamp_refusal(submitted: object, completed: str) -> str | None:
    """Why `completed` cannot stand as this row's completion stamp, or `None` when it can.

    The retention rule ranks a completed record by ``completed or timestamp``, and
    `scripts/check-issue-links.py::_absent_origin_reading` classifies an origin the ledger
    does not hold from that same key — its arithmetic turns on one premise, that an entry
    cannot complete before it was submitted. Nothing enforced it: this module accepted any
    explicit stamp, so `update_rant(..., completed="2025-01-01T00:00:00+00:00")` on a row
    submitted today wrote a record whose completion precedes its submission.

    Measured 2026-10-09 (`cyc20261009-003853` on `ba12ffdb995f`, the tree `check-issue-links`
    PR #1946 lands) against the real owner at its shipped `keep=10`: 12 records plus one such
    violator, and the owner **really pruned the violator** — its key sorts below every survivor
    — while the classifier, asked about that same instant, printed *"the ledger holds 0
    completed rant(s) at or after this instant … the handle names no record this ledger ever
    held"*. A record the store removed was reported as one it never held, which is the loudest
    claim that reading can make.

    So the invariant is enforced where the field is written, which is the only place it can be
    made a fact rather than an assumption: a stamp that cannot be shown to be at or after the
    row's own submission is **refused**, never clamped (`count_argument` / `boolean_argument`
    state the same rule for a tool's parameters — refuse rather than substitute a value the
    caller did not send).

    :param submitted: the row's own `timestamp` field, whatever the file holds.
    :param completed: the stamp about to be written.
    :returns: the refusal to hand back, or `None` when the stamp may be written.
    """
    if _parse_instant(submitted) is None:
        return (
            f"the row's own timestamp is {submitted!r}, which is not an instant this can be "
            "ordered against - a completion stamp cannot be shown to follow it. Repair the "
            "row's timestamp first"
        )
    stamp = _parse_instant(completed)
    if stamp is None:
        return f"completed must be an ISO timestamp (got {completed!r})"
    submitted_at = _parse_instant(submitted)
    if (stamp.tzinfo is None) != (submitted_at.tzinfo is None):
        return (
            f"completed {completed!r} and the row's timestamp {submitted!r} cannot be ordered "
            "(one of them carries no offset), so the stamp cannot be shown to follow the "
            "submission"
        )
    if stamp < submitted_at:
        return (
            f"completed {completed!r} precedes the row's own timestamp {submitted!r}: a record "
            "cannot complete before it was submitted, and one ranked below the instant it cites "
            "is read by the retention rule as older than it is"
        )
    return None


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

    Both writers of ``completed`` go through `_stamp_refusal`, so a stamp earlier than the
    row's own ``timestamp`` — or one that cannot be ordered against it — is refused rather
    than written. Why that matters is stated there.

    Returns:
        ``(ok, message)`` — ok=False with a reason on invalid transition /
        unknown timestamp / a completion stamp that cannot stand.
    """
    rants = _read_rants(rants_log)
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
                if status == "completed":
                    # The auto-written stamp is this module's own claim about the row, so it
                    # is held to the same invariant as a caller's: a row whose submission
                    # instant cannot be read is refused here rather than given a stamp nothing
                    # can check.
                    stamp = datetime.now().astimezone().isoformat()
                    refusal = _stamp_refusal(r.get("timestamp"), stamp)
                    if refusal is not None:
                        return False, refusal
                    r["status"] = status
                    r["completed"] = stamp
                else:
                    r["status"] = status
                    r["completed"] = None
        if progress is not None:
            r["progress"] = progress
        if completed is not None:
            refusal = _stamp_refusal(r.get("timestamp"), completed)
            if refusal is not None:
                return False, refusal
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
