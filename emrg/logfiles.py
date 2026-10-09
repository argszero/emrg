"""The application-level log files, and the migration of the ones left behind.

Why this module exists
----------------------
Rant 2026-10-09T14:20:18: the host's `~/.emrg` root had collected nine log files
while `~/.emrg/logs/` already existed and was already a log home (`stop_all-*.log`,
`task-runs/`, `summary.json`), so the rule was half-applied. The five
*application-level* logs now live in :func:`emrg.config.logs_dir` under these
names — the names did not change, only the directory.

This is the only place in the source that *writes* to a log file in the config
root, and only because the migration below has to read the leftovers from there.
That is deliberate: `tests/test_app_logs_live_under_logs_dir.py` fails on any
*other* source line that joins the config root with a log name, so a log file
added to a writer without being added to `APP_LOG_FILES` is caught rather than
grandfathered by a `*.log` pattern.

That guard's cover is worth stating, because a guard whose verdict outruns its
walk is how the same reader gets dropped twice: it walks `emrg/`, `scripts/`,
`packaging/` and `bin/`, over `.py`, `.js`, `.sh`, `.cmd`, `.bat` and `.ps1`, and
it allows exactly two exemptions — this module's migration, for a name in
`APP_LOG_FILES`, and `packaging/smoke-test.sh`'s capture of the daemon's console
output (`emrgd-debug.log`), which it writes *before* the daemon exists to create
`logs/`. Both are enumerated; neither is a wildcard.

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


#: Suffixes tried, in order, when a leftover's own name is already taken in the log
#: directory. The taken name is a **live** log and must never be written over, but
#: the leftover must not stay in the config root either (rant 2026-10-09T14:20:18
#: requirement 3: 「先到者胜，丢弃或改名，不得覆盖新日志」 — this is the 改名).
#: Bounded rather than unbounded: a start may not spin, and running out is reported.
_ASIDE_SUFFIXES = (".legacy", ".legacy.2", ".legacy.3", ".legacy.4", ".legacy.5")


def _free_aside_path(destination: Path) -> Path | None:
    """The first free ``<destination><suffix>`` name, or ``None`` if all are taken.

    Returns a name rather than moving anything, so the caller's single ``rename``
    stays the only mutation and a failed one leaves nothing half-done.
    """
    for suffix in _ASIDE_SUFFIXES:
        candidate = destination.with_name(destination.name + suffix)
        if not candidate.exists():
            return candidate
    return None


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
    is the newer log). The leftover is then **renamed aside** beside it
    (:data:`_ASIDE_SUFFIXES`) rather than left where it is — because leaving it is
    not a corner case, it is the ordinary one. Both clients open
    ``logs/emrgd-start.err`` (`daemon_manager._truncate_start_stderr`, the GUI's
    `_openStartStderr`) and the GUI opens ``logs/emrg-gui.log`` at its own startup,
    all **before** the daemon runs this migration, so those two destinations exist
    on the very first run after an upgrade and the leftovers would otherwise stay
    in the config root for good — which is the complaint this module exists to
    answer (measured 2026-10-09: `moved: ['emrgd.log']` with two names left).

    Nothing is ever deleted: the old content survives under the aside name, which
    is what the rant authorises (「丢弃或改名，不得覆盖新日志」) and what keeps a
    migration from being able to destroy the host's data.

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
            aside = _free_aside_path(destination)
            if aside is None:
                logger.warning(
                    "legacy log %s left in place: %s has no free name beside it",
                    source,
                    destination,
                )
                continue
            destination = aside
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
