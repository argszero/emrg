"""Shared rant-write logic — single source of truth for rants.jsonl.

Extracted from the daemon's ``rant`` handler (rant 2026-08-17T11:51:59: rant
submission moves from a command-only path to "Agent auto-detects in normal
conversation, confirms with the user, then calls the submit_rant tool").
Both the daemon ``rant`` command and the ``submit_rant`` tool call
:func:`append_rant`, so behavior stays identical.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def append_rant(rants_log: Path, message: str, project: str = "") -> int:
    """Append a rant entry to ``rants_log``, sorted by timestamp.

    Args:
        rants_log: Path to rants.jsonl (e.g. ``~/.emrg/rants.jsonl``).
        message: The rant body (already user-confirmed / polished).
        project: Optional target project name (empty = EMRG itself).

    Returns:
        The new total rant count.
    """
    # Field order: timestamp → project → status → progress → completed → message
    # (project right after timestamp per user feedback; message last)
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

    rants_log.parent.mkdir(parents=True, exist_ok=True)

    # Read existing rants, append new, sort by timestamp, rewrite sorted
    rants: list[dict] = []
    if rants_log.exists():
        with open(rants_log, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rants.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    rants.append(entry)
    rants.sort(key=lambda r: r.get("timestamp", ""))

    with open(rants_log, "w", encoding="utf-8") as f:
        for r in rants:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return len(rants)
