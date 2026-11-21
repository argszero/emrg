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
files, and it is called from the two processes that can be first to touch the root: the
daemon's own entry point (``emrg/server/__main__.py``, which every launch path ends in —
the GUI and the TUI both spawn ``python -m emrg.server``), and ``emrg``'s CLI start, whose
commands read the config before any daemon is up. Both calls are the same function, and a
second one finds nothing left to move. ``stop`` and ``update`` are deliberately left out:
they are the paths an installation is repaired or replaced through, and a move underneath
them would be a second change while the first is in flight. In every caller the call sits
**before** anything that would create a directory inside ``global/`` — the name it would
take is the name a top-level entry of that name is about to move into.
"""

from __future__ import annotations

import logging
import os
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

#: The agent register's name. Spelled here because the migration has to find the file
#: after moving it, and the module that owns the register's *contents* is not the module
#: that owns this root's shape.
AGENTS_FILENAME = "agents.yml"


def register_after_migration(base: Path) -> Path:
    """Where the agent register is once the migration has run.

    ``global/agents.yml`` when it is there, the top level otherwise — the shape
    :func:`emrg.config.register_for_reading` uses, and for the same reason: an entry that
    did not move (or a root that was never migrated) must still be read at the one place
    it exists rather than reported as absent.
    """
    inside = base / GLOBAL_DIRNAME / AGENTS_FILENAME
    return inside if inside.exists() else base / AGENTS_FILENAME


def moved_path_mapper(done: list[tuple[Path, Path]]):
    """A mapper that carries a recorded directory along with the tree it was recorded in.

    The register stores the verbatim directory a session runs in, so moving ``evolution/``
    into ``global/`` leaves every row for a tree inside it naming a directory that no
    longer exists. The mapper answers the new path for a row that was inside a moved tree
    and the string it was given for every other row, which is what keeps the rewrite
    bounded to what actually moved.

    The root itself is never covered: a row for the home-level ``.emrg`` directory names
    the root, which stays where it is.
    """

    def mapper(current: str) -> str:
        if not current:
            return current
        for source, target in done:
            source_s, target_s = str(source), str(target)
            if current == source_s or current.startswith(source_s + os.sep):
                return target_s + current[len(source_s):]
        return current

    return mapper


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


#: Appended to the name of an entry that is set aside because ``global/`` already holds
#: something by that name.
SET_ASIDE_SUFFIX = ".pre-global"


def _free_name(target_root: Path, name: str) -> Path:
    """A path under ``target_root`` that nothing occupies yet, for a set-aside entry."""
    candidate = target_root / f"{name}{SET_ASIDE_SUFFIX}"
    counter = 2
    while candidate.exists():
        candidate = target_root / f"{name}{SET_ASIDE_SUFFIX}-{counter}"
        counter += 1
    return candidate


def pending_moves(root: Path | None = None) -> list[tuple[Path, Path]]:
    """The ``(source, target)`` pairs a migration would perform — moving nothing.

    A pair is *pending* while the entry is still at the top level. An entry whose name
    ``global/`` already holds is **set aside inside ``global/``** under
    :data:`SET_ASIDE_SUFFIX` rather than skipped or overwritten: skipped leaves it at the
    top level, which is the state this split exists to end, and overwriting replaces one
    tree with another, which is the one defect here that cannot be undone.

    The measured case is an upgrade. The new installer writes ``global/install`` while the
    previous installation's tree is still at ``install``, so by the time the daemon first
    starts, both exist; the old tree is the one that moves aside.
    """
    base = home_root() if root is None else Path(root)
    target_root = base / GLOBAL_DIRNAME
    pending: list[tuple[Path, Path]] = []
    for entry in _top_level(base):
        if entry.name == GLOBAL_DIRNAME or entry.name in AGENT_STATE_NAMES:
            continue
        if not entry.exists():
            continue
        target = target_root / entry.name
        if target.exists():
            target = _free_name(target_root, entry.name)
        pending.append((entry, target))
    return pending


def migrate_into_global(root: Path | None = None) -> list[str]:
    """Move every top-level entry EMRG owns into ``global/``, and report their names.

    Repeatable: a second call finds the targets present and does nothing. **All or
    nothing**: every move is committed only if every move succeeds, and a failure part
    way puts back what this call moved — a half-migrated root would leave some readers
    on the old path and some on the new, which is worse than an unmigrated one.

    Nothing is deleted and nothing is overwritten: a name already taken in ``global/``
    has its newcomer set aside beside it (:data:`SET_ASIDE_SUFFIX`) rather than replacing
    a file with a directory.

    The agent register's rows travel with the trees they name
    (:func:`_carry_register_rows`): a row stores the verbatim directory a session runs
    in, so a tree moved without its row leaves an agent pointing at a directory that no
    longer exists — well-formed, silently wrong, and found only by whoever stats the path.

    Called from the daemon's entry point and from ``emrg``'s CLI start — the processes that
    can be first to touch this root — and never from :func:`global_dir` or any other path
    answer, so that asking where something lives never moves a host's files.
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
    _carry_register_rows(base, done)
    return [source.name for source, _target in done]


def _carry_register_rows(base: Path, done: list[tuple[Path, Path]]) -> None:
    """Rewrite the register's rows for the directories this pass moved.

    A register stores the verbatim directory a session runs in. Moving ``evolution/``
    into ``global/`` therefore leaves that agent's row naming a directory that no longer
    exists — and nothing in the root says so: the row is well-formed, the tree is intact,
    and only a reader that stats the path finds the disagreement. Measured on the
    development host, 2 of its 42 rows were under a tree this migration moves.

    The rewrite goes through :meth:`emrg.server.agents.AgentRegistry.rewrite_paths`, which
    already exists for this case and had no caller — a second YAML writer here would be a
    second answer to "what does a row look like", and the register is the one place that
    knows.

    Imported inside the function on purpose: this module is imported by
    :mod:`emrg.config`, which the register's module reads, so a module-scope import would
    close the cycle.

    A register that cannot be read, or a rewrite that cannot be written, leaves the file
    exactly as it was and does not fail the migration: the files *did* move, and reporting
    that a pass failed because a reference could not be updated would send the next reader
    looking for a move that already happened.
    """
    try:
        from emrg.server.agents import AgentRegistry
    except Exception:  # pragma: no cover - an import that always succeeds in-tree
        logger.warning("could not load the agent register; its paths were not updated",
                       exc_info=True)
        return
    try:
        renamed = AgentRegistry(register_after_migration(base)).rewrite_paths(
            moved_path_mapper(done)
        )
    except OSError:
        logger.warning("could not update the agent register after the move",
                       exc_info=True)
        return
    if renamed:
        logger.info("agent register followed the move: %s", ", ".join(sorted(renamed)))

