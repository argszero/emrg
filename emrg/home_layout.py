"""The home-level ``.emrg`` root: one directory for global data, and an agent's own state.

The root had become two directories wearing one name. It held EMRG's own data — the
config, the agent register, the task table, the rant log, the usage ledger, the install
tree, the upgrade work and backup trees, the application logs, the global skill root —
and it also held the state of a session started with the home directory as its working
directory, because that state lives in ``<cwd>/.emrg`` and ``<cwd>`` was the home
directory. Measured on the development host before this split: **47 top-level entries**,
of which about a dozen were EMRG's; the rest were logs, several hundred megabytes of
upgrade backups, three agents' working trees, eleven hand-made ``*.bak-*`` copies and a
few loose files. Nothing about the layer said which entry belonged to what.

So the root now has one shape with two parts:

* ``.emrg/global/`` holds everything EMRG owns, at the relative paths it already had.
* ``.emrg/sessions/``, ``.emrg/memory/`` and ``.emrg/emrg-client.log`` are the products
  of the ``<cwd>/.emrg`` rule when the working directory is the home directory. They
  exist only if an agent was actually started there, and EMRG does not write them from
  anywhere else.

**The split is a directory, not a judgement.** ``AGENT_STATE_NAMES`` names what the rule
produces; everything else at the top level is EMRG's and moves. An entry nobody
recognises is not a special case — it moves too, which is what stops the root collecting
loose files again: there is no list to fall off.

Both functions here are pure or opt-in. :func:`home_root` and :func:`global_dir` answer
paths and touch nothing; :func:`migrate_into_global` is the one call that moves a host's
files, and it is called from the daemon's startup and nowhere else.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

#: The one directory under the home-level root that holds EMRG's own data.
GLOBAL_DIRNAME = "global"

#: Top-level entries that belong to a session started in the home directory, under the
#: existing ``<cwd>/.emrg`` rule. They stay where that rule puts them.
#:
#: ``logs/`` is deliberately **not** here even though a home-directory agent can create
#: one: the application log directory (``emrgd.log`` and its siblings) lives under the
#: global root, so the one that exists before a migration is EMRG's and moves. A session
#: started in the home directory after the migration creates its own if it needs one.
AGENT_STATE_NAMES = frozenset({"sessions", "memory", "emrg-client.log"})


def home_root() -> Path:
    """The home-level ``.emrg`` directory — ``global/`` plus an agent's own state."""
    return Path.home() / ".emrg"


def global_dir() -> Path:
    """The global data root: every path EMRG owns hangs off this one."""
    return home_root() / GLOBAL_DIRNAME


def _top_level(root: Path) -> list[Path]:
    """Every entry at ``root``'s top level, sorted; empty when it does not exist."""
    try:
        return sorted(root.iterdir())
    except OSError:
        return []


def pending_moves(root: Path | None = None) -> list[tuple[Path, Path]]:
    """The ``(source, target)`` pairs a migration would perform — moving nothing.

    A pair is *pending* only when the target is still absent. An entry that is already
    at the target, and an entry whose name is already taken in ``global/``, are both left
    alone: the first needs no move and the second would overwrite something, and a
    migration that overwrites is the one defect here that cannot be undone.
    """
    base = home_root() if root is None else Path(root)
    target_root = base / GLOBAL_DIRNAME
    pending: list[tuple[Path, Path]] = []
    for entry in _top_level(base):
        if entry.name == GLOBAL_DIRNAME or entry.name in AGENT_STATE_NAMES:
            continue
        target = target_root / entry.name
        if target.exists() or not entry.exists():
            continue
        pending.append((entry, target))
    return pending


def migrate_into_global(root: Path | None = None) -> list[str]:
    """Move every top-level entry EMRG owns into ``global/``, and report their names.

    Repeatable: a second call finds the targets present and does nothing. **All or
    nothing**: every move is committed only if every move succeeds, and a failure part
    way puts back what this call moved — a half-migrated root would leave some readers
    on the old path and some on the new, which is worse than an unmigrated one.

    Nothing is deleted, nothing is overwritten, and a name already taken in ``global/``
    keeps the entry where it is rather than replacing a file with a directory.

    Called from the daemon's startup, which is the one process that owns this root —
    never from :func:`global_dir` or any other path answer, so that asking where
    something lives never moves a host's files.
    """
    base = home_root() if root is None else Path(root)
    pending = pending_moves(base)
    if not pending:
        return []
    (base / GLOBAL_DIRNAME).mkdir(parents=True, exist_ok=True)
    done: list[tuple[Path, Path]] = []
    try:
        for source, target in pending:
            shutil.move(str(source), str(target))
            done.append((source, target))
    except OSError as exc:
        for source, target in reversed(done):
            try:
                if target.exists() and not source.exists():
                    shutil.move(str(target), str(source))
            except OSError:
                logger.warning("could not put %s back into %s", target, source,
                               exc_info=True)
        logger.warning("migration into %s/ stopped and was reverted: %s",
                       GLOBAL_DIRNAME, exc)
        return []
    return [source.name for source, _target in done]
