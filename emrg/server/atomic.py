"""Atomic file write utilities — shared by daemon and scheduler.

Provides a single atomic write path for YAML data files, replacing
the duplicated mkstemp + fdopen + safe_dump + replace pattern.

**Both writers answer whether the bytes landed.** They used to return ``None`` in
both states, so a caller could not tell a write that happened from one that did not —
and a caller that reported the outcome anyway reported the wrong one. Measured
2026-10-12 (`cyc20261012-115759`): with a *directory* at `.git/info/exclude`, the
``os.replace`` below raises ``IsADirectoryError``, this module's own handler logs it and
swallows it, and ``git_utils.ensure_local_exclude`` answered ``"added"`` — a success
report for a write that never landed, in a function whose docstring promises
``"error: <reason>"`` for a file it could not write. Its two production callers
(scheduler's runtime-dir registration and ``daemon._touch_project``) read ``"added"`` as
"this tree now ignores EMRG's runtime directory" and log exactly that.

``bool`` rather than an exception, and rather than ``None``/``str``: every caller today
treats a failed write as non-fatal (a token file, a cache, an exclude entry), the temp
file is already cleaned up here, and the reason is already logged with ``exc_info`` —
so the one thing a caller cannot get for itself is *whether* it landed. ``True`` means
the replacement is in place.
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

    Returns ``True`` when the replacement is in place, ``False`` when it is not (the
    reason is logged) — see the module docstring for why the answer is a ``bool``.

    ``dumper`` substitutes the dumper class. It exists for tasks.yml, whose writer
    needs multi-line strings as literal `|` blocks (a `SafeDumper` representer) so
    one edit does not reflow the host's own formatting; the default keeps the exact
    behaviour every other caller already had.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=prefix,
        suffix=suffix,
    )
    try:
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
    except OSError:
        logger.warning(
            "atomic_write_yaml: write failed for %s", target, exc_info=True
        )
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False
    return True


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

    Returns ``True`` when the replacement is in place, ``False`` when it is not (the
    reason is logged) — see the module docstring. The distinction is not cosmetic for
    the callers that report the outcome: measured 2026-10-12 (`cyc20261012-115759`),
    ``ensure_local_exclude`` answered ``"added"`` for a subject the replace could not
    overwrite, because both states arrived here as ``None``.

    Used for the daemon auth token file (``emrgd.token``) where mode 0o600
    is required (token must not leak to other local users).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=prefix,
        suffix=suffix,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    except OSError:
        logger.warning(
            "atomic_write_bytes: write failed for %s", target, exc_info=True
        )
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False
    return True
