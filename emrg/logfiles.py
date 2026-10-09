"""The application-level log files, and the migration of the ones left behind.

Why this module exists
----------------------
Rant 2026-10-09T14:20:18: the host's `~/.emrg` root had collected nine log files
while `~/.emrg/logs/` already existed and was already a log home (`stop_all-*.log`,
`task-runs/`, `summary.json`), so the rule was half-applied. The five
*application-level* logs now live in :func:`emrg.config.logs_dir` under these
names — the names did not change, only the directory.

This is the **only** place in the source that still names a log file in the config
root, and only because the migration below has to read the leftovers from there.
That is deliberate: `tests/test_app_logs_live_under_logs_dir.py` fails on any
*other* source line that joins the config root with a log name, so a log file
added to a writer without being added to `APP_LOG_FILES` is caught rather than
grandfathered by a `*.log` pattern.

The client-side logs (`<cwd>/.emrg/emrg-client.log` and
`emrg-client-crash.log`) are **not** in this list: they are cwd-relative, not
application-level, and whether they move too is a trade-off the host has reserved
— see the rant's boundary section.
"""

from __future__ import annotations

import logging
from pathlib import Path

from emrg.config import config_dir, logs_dir

logger = logging.getLogger(__name__)

#: The application-level log files, by their base name — the whole of what moves.
#:
#: A new entry is a decision, not a convenience: this tuple is the static guard's
#: exemption list, so adding one here is what makes a writer's root-dir spelling
#: legal.
APP_LOG_FILES = (
    "emrgd.log",
    "emrgd-crash.log",
    "emrgd-exit.log",
    "emrgd-start.err",
    "emrg-gui.log",
)

#: How many rotation backups a log may have left in the root. Only `emrgd.log` is
#: rotated, by a `RotatingFileHandler(backupCount=3)` in
#: `emrg/server/__main__.py`, so the suffix bound is that handler's — the other
#: four names simply have no `.1`…`.3` siblings and the loop finds nothing.
ROTATION_BACKUPS = 3


def legacy_log_names() -> tuple[str, ...]:
    """Every file name the migration looks for in the config root.

    The base names plus their rotation siblings, so a rotated `emrgd.log.2` left
    in the root moves with its base rather than staying behind as the only log
    file the host would still see there.
    """
    names: list[str] = []
    for base in APP_LOG_FILES:
        names.append(base)
        names.extend(f"{base}.{n}" for n in range(1, ROTATION_BACKUPS + 1))
    return tuple(names)


def migrate_legacy_logs(log_dir: Path | None = None) -> list[str]:
    """Rename the log files left in the config root into ``log_dir``.

    **Startup only, and before the log handler is opened.** Renaming a file a
    ``RotatingFileHandler`` is actively rotating is broken: the handler freezes
    its path as ``baseFilename`` at construction and ``_open()`` returns to that
    string after ``doRollover()``, so the running process would recreate a file at
    the *old* path on its next rotation and leave the moved copy orphaned. The
    change therefore takes effect on the next daemon/GUI start, and a running
    process takes no part in the migration — which is why this is called from
    ``_configure_logging()`` *before* the handler is built and never from an
    early "tidy up" hook.

    Idempotent, and never overwriting: a destination that already exists wins (it
    is the newer log), and the leftover is left where it is with a warning rather
    than deleted — a migration must not be able to destroy the host's data.

    :param log_dir: where the logs belong; defaults to :func:`emrg.config.logs_dir`.
    :returns: the names actually moved, for the log line and for the tests.
    """
    target = logs_dir() if log_dir is None else Path(log_dir)
    root = config_dir()
    moved: list[str] = []
    for name in legacy_log_names():
        source = root / name
        if not source.is_file():
            continue
        destination = target / name
        if destination.exists():
            logger.warning(
                "legacy log %s left in place: %s already exists", source, destination
            )
            continue
        try:
            source.rename(destination)
        except OSError as exc:  # best-effort: a log must never fail a start
            logger.warning("legacy log %s could not be moved: %s", source, exc)
            continue
        moved.append(name)
    if moved:
        logger.info(
            "moved %d legacy log file(s) into %s: %s",
            len(moved),
            target,
            ", ".join(moved),
        )
    return moved
