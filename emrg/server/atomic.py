"""File I/O for the YAML state files — shared by daemon and scheduler.

The **write** half is the single atomic path for YAML data files, replacing the
duplicated mkstemp + fdopen + safe_dump + replace pattern.

The **read** half is :data:`YAML_READ_ERRORS`, the one home for "this reader could
not read the file" — because the readers had eight homes and drifted apart (see
that constant).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import yaml

from emrg.read_errors import FILE_READ_ERRORS

logger = logging.getLogger(__name__)


#: Every way **reading a YAML state file** can fail, in one home.
#:
#: Three shapes, and they are one class because a reader cannot tell them apart by
#: asking "could I read the file?":
#:
#: * **`OSError`** — no such file, a directory where the file should be, a permission
#:   bit. (`FileNotFoundError` and `IsADirectoryError` are members.)
#: * **`yaml.YAMLError`** — bytes that are not YAML.
#: * **`UnicodeDecodeError`** — bytes that are not valid UTF-8. It is a
#:   **`ValueError`, not an `OSError`**, which is why a tuple spelled
#:   `(yaml.YAMLError, OSError)` catches two of the three and lets this one escape.
#:
#: Measured 2026-10-03 (`cyc20261003-213507`) on the eight readers that spelled that
#: two-shape tuple — `projects.yml` and `tasks.yml`, in `daemon.py` and `scheduler.py`.
#: For each, a corrupt file and a non-UTF-8 file got **different answers**, and the
#: escape was not a message but a verdict: `read_table` raised a raw
#: `UnicodeDecodeError` instead of the `TableUnreadable` it declares and three call
#: sites catch; `_resolve_project_path` and `_load_project_config` raised instead of
#: answering `None` / `{}`; the scheduler self-heal crashed on a path whose whole job
#: is repair; the session-index backfill ran (degraded) for one shape and not at all
#: for the other; `_touch_project` recorded nothing; and both project commands died
#: mid-handler, which the client cannot tell apart from a dropped connection.
#:
#: The three readers that already had the class right are the control —
#: `submit_rant_tool._registered_project_names` and two `except Exception` sites in
#: `daemon.py` — so the correct spelling was known in this codebase and the tuple was
#: a spelling that drifted, not a decision.
YAML_READ_ERRORS = (*FILE_READ_ERRORS, yaml.YAMLError)


def atomic_write_yaml(
    data: list[dict],
    target: Path,
    *,
    prefix: str = ".atomic_",
    suffix: str = ".tmp",
    dumper: type | None = None,
) -> None:
    """Atomically write a list of dicts as YAML to target.

    Writes to a temp file in the same directory, then os.replace()
    to atomically swap. On error, the temp file is cleaned up.

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


def atomic_write_bytes(
    data: str,
    target: Path,
    *,
    mode: int = 0o600,
    prefix: str = ".atomic_",
    suffix: str = ".tmp",
) -> None:
    """Atomically write a text blob to target with explicit permissions.

    Writes to a temp file in the same directory, then chmod + os.replace()
    to atomically swap. On error, the temp file is cleaned up.

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
