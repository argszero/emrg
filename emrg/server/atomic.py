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
) -> None:
    """Atomically write a list of dicts as YAML to target.

    Writes to a temp file in the same directory, then os.replace()
    to atomically swap. On error, the temp file is cleaned up.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=prefix,
        suffix=suffix,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                data, f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
        os.replace(tmp_path, target)
    except OSError:
        logger.warning(
            "atomic_write_yaml: write failed for %s", target, exc_info=True
        )
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
