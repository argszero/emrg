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
    rants = _read_rants(rants_log)
    active = [r for r in rants if r.get("status") != "completed"]
    completed = [
        r for r in rants if r.get("status") == "completed"
    ]
    completed.sort(key=lambda r: r.get("completed") or r.get("timestamp") or "")
    kept_completed = completed[-keep:] if keep > 0 else []
    _write_rants(rants_log, active + kept_completed)
    return len(active) + len(kept_completed)
