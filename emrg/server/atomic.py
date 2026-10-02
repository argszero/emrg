"""Atomic file write utilities — shared by daemon and scheduler.

Provides a single atomic write path for YAML data files, replacing
the duplicated mkstemp + fdopen + safe_dump + replace pattern.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def atomic_write_yaml(
    data: list[dict],
    target: Path,
    *,
    prefix: str = ".atomic_",
    suffix: str = ".tmp",
    dumper: type | None = None,
) -> bool:
    """Atomically write a list of dicts as YAML to target.

    Writes to a temp file in the same directory, then os.replace()
    to atomically swap. On error, the temp file is cleaned up.

    Returns **whether the file was replaced**, and never raises for a write that
    could not happen — a caller that reports success to a human has to read this,
    because "warned in the log" and "wrote the file" are not the same thing. One
    call site already learned that: `task_create` returned "created" for a task
    that was never persisted, so the GUI showed a task that a restart forgot.

    ``dumper`` substitutes the dumper class. It exists for tasks.yml, whose writer
    needs multi-line strings as literal `|` blocks (a `SafeDumper` representer) so
    one edit does not reflow the host's own formatting; the default keeps the exact
    behaviour every other caller already had.

    ``mkdir`` and ``mkstemp`` are inside the guard on purpose. They used to sit
    above it, so the first thing that can fail — a directory that cannot be
    created, a temp file that cannot be made (permission, or ENOSPC on a full
    disk, which is the shape a host actually meets) — escaped as an OSError while
    every later failure was swallowed. Measured 2026-10-02: with the config
    directory read-only, this raised `PermissionError` out of `task_create`.
    """
    tmp_path: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=prefix,
            suffix=suffix,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if dumper is None:
                yaml.safe_dump(
                    data, f,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
            else:
                yaml.dump(
                    data, f,
                    Dumper=dumper,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
        os.replace(tmp_path, target)
        return True
    except OSError:
        logger.warning(
            "atomic_write_yaml: write failed for %s", target, exc_info=True
        )
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def atomic_write_bytes(
    data: str,
    target: Path,
    *,
    mode: int = 0o600,
    prefix: str = ".atomic_",
    suffix: str = ".tmp",
) -> bool:
    """Atomically write a text blob to target with explicit permissions.

    Writes to a temp file in the same directory, then chmod + os.replace()
    to atomically swap. On error, the temp file is cleaned up.

    Used for the daemon auth token file (``emrgd.token``) where mode 0o600
    is required (token must not leak to other local users).

    Returns whether the file was replaced and never raises for a write that could
    not happen — same contract, and the same reason, as :func:`atomic_write_yaml`
    (whose docstring carries the measurement).
    """
    tmp_path: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=prefix,
            suffix=suffix,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
        return True
    except OSError:
        logger.warning(
            "atomic_write_bytes: write failed for %s", target, exc_info=True
        )
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False
