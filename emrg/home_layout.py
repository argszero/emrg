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
import sys
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


def running_tree_anchors() -> list[Path]:
    """The paths that say where the code executing this process lives.

    Two of them, because either alone misses a case: ``sys.executable`` is the interpreter
    (a packaged install ships its own, so the tree it sits in is live), and ``__file__`` is
    this module (a checkout runs from a source tree with no packaged interpreter beside
    it). Both are resolved, because the migration's own subject is a directory that a
    symlink may already point at.
    """
    anchors: list[Path] = []
    for raw in (getattr(sys, "executable", ""), __file__):
        if not raw:
            continue
        try:
            anchors.append(Path(raw).resolve())
        except OSError:  # pragma: no cover - resolve answers a path or raises, never None
            continue
    return anchors


def holds_a_running_tree(entry: Path, anchors: list[Path] | None = None) -> bool:
    """Whether ``entry`` contains the interpreter or the package running this process.

    **The migration must never move one.** Renaming a directory under a live process
    succeeds on macOS, and every path that process holds — ``sys.path``'s source entry,
    ``emrg.__path__``, the packaged interpreter — then names a directory that is no longer
    there: the next *new* submodule import fails, `logging.handlers` fails, and the run
    dies inside the very call that moved it. Measured on a host that upgraded across the
    split: two `ModuleNotFoundError`s, a dead ``PATH`` anchor and three dead register rows,
    with the repair done by hand.
    """
    if anchors is None:
        anchors = running_tree_anchors()
    try:
        resolved = Path(entry).resolve()
    except OSError:
        return False
    for anchor in anchors:
        if anchor == resolved or str(anchor).startswith(str(resolved) + os.sep):
            return True
    return False

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
        if holds_a_running_tree(entry):
            # The install tree carries the executing source and the packaged interpreter.
            # Renaming it under a live process leaves every path that process holds
            # naming a directory that is gone; see :func:`holds_a_running_tree`. It
            # relocates by being replaced, never by being moved from underneath a run.
            logger.info(
                "not moving %s: the code running this process lives in it", entry
            )
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
        # The repair is not conditional on a move: the pass that should have carried the
        # rows fails on exactly the hosts that need it, so a root already half migrated
        # is repaired by the next start that finds nothing to move (issue #3143).
        reconcile_register_paths(base)
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
    reconcile_register_paths(base)
    if root is None:
        # Only the host's own shell files, and only when this is the host's own root: a
        # caller naming a root is a test, and its rc files are not ours to edit.
        rewrite_path_anchors()
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


#: The installer's block, and the two spellings of the bin directory it can hold.
#:
#: The markers are the installer's own (`packaging/make-installer.sh:165`) and
#: `bin/emrg-uninstall`'s `clean_environment()` cleans by them, so a third spelling here
#: would be a second convention for one block.
PATH_ANCHOR_START = "# >>> EMRG PATH >>>"
PATH_ANCHOR_END = "# <<< EMRG PATH <<<"
PRE_SPLIT_BIN = "/.emrg/install/bin"
POST_SPLIT_BIN = "/.emrg/global/install/bin"

#: The shell rc files the installer writes its anchor into, in its order.
RC_FILENAMES = (".zshrc", ".bash_profile", ".bashrc", ".profile")


def repaired_path(current: str, root: Path) -> str:
    """``current``'s post-split twin, when the row names a directory the split moved.

    The predicate is the disagreement itself, read from the disk rather than from a move
    log: a row survives the pass that should have rewritten it — that pass imports the
    register inside the function, so the migration that breaks the import graph is the one
    that skips the rewrite — and a repair tied to a move list can never revisit it. So a
    row is repairable when its own path is **gone** and the same path under ``root/global``
    is **there**.

    Bounded by ``root`` and not by a spelling of it: a row outside this root is another
    installation's (or another agent's) and this repair has no business touching it, and a
    row already under ``global/`` whose directory is missing is not this migration's
    doing. The root itself is left alone too — it does not move, so its row must not.
    """
    if not current:
        return current
    try:
        if Path(current).exists():
            return current
    except OSError:
        return current
    try:
        rest = Path(current).relative_to(root)
    except ValueError:
        return current
    if not rest.parts or rest.parts[0] == GLOBAL_DIRNAME:
        return current
    twin = root / GLOBAL_DIRNAME / rest
    try:
        return str(twin) if twin.exists() else current
    except OSError:
        return current


def split_path_mapper(root: Path):
    """A mapper for :meth:`AgentRegistry.rewrite_paths` that repairs a stale row.

    Kept separate from :func:`moved_path_mapper` because the two answer different
    questions: that one follows a move this process just performed, and this one asks the
    disk whether a row disagrees with it.
    """

    def mapper(current: str) -> str:
        return repaired_path(current, root)

    return mapper


def reconcile_register_paths(base: Path | None = None) -> list[str]:
    """Repair register rows the split has left naming a directory that is gone.

    **Runs on every start, not only after a move.** The pass that should have carried the
    rows fails on exactly the hosts that need it — it imports the register, and the
    migration it is part of is what broke that import — so a repair tied to the move list
    is a repair that never happens on the machine it is for. Measured on such a host: 44
    rows, 3 dead, all three with an intact tree one directory over.

    Goes through :meth:`emrg.server.agents.AgentRegistry.rewrite_paths`, the register's
    own writer: a second YAML writer here would be a second answer to "what does a row
    look like". Imported inside the function for the reason
    :func:`_carry_register_rows` records — this module is imported by
    :mod:`emrg.config`, which the register's module reads.
    """
    root = home_root() if base is None else Path(base)
    try:
        from emrg.server.agents import AgentRegistry
    except Exception:  # pragma: no cover - an import that always succeeds in-tree
        logger.warning("could not load the agent register; its paths were not repaired",
                       exc_info=True)
        return []
    try:
        renamed = AgentRegistry(register_after_migration(root)).rewrite_paths(
            split_path_mapper(root)
        )
    except OSError:
        logger.warning("could not repair the agent register", exc_info=True)
        return []
    if renamed:
        logger.info("agent register repaired: %s", ", ".join(sorted(renamed)))
    return renamed


def rewrite_path_anchors(home: Path | None = None) -> list[str]:
    """Point the installer's ``PATH`` anchor at the post-split bin directory.

    An upgrade is not an installer: the block in ``.zshrc`` and its three siblings was
    written against ``$HOME/.emrg/install/bin``, and the migration moves that directory.
    Nothing else rewrites it, so the next shell answers ``emrg: command not found`` while
    a working tree sits one directory away — and a fresh install never sees it, which is
    why the packaging gates did not.

    **Only lines inside the block are touched.** A host's own ``PATH`` line and everything
    outside the markers are returned byte-identical; the block is the installer's, and it
    is the only part of an rc file that is ours to edit. Idempotent: a block already
    naming the post-split directory answers no file names.
    """
    # The "is this a regular file" rule is the tool layer's, reached at a path rather than
    # at a mode - the same import `emrg/server/upgrade.py` makes for the same reason.
    # **Asked before the open, because this runs on the startup path**: `read_text` on a
    # FIFO waits for a writer and raises nothing, so no `except OSError` could bound it and
    # the daemon would hang on its way up with no way to say why.
    from emrg.tools.base import non_regular_kind

    base = Path.home() if home is None else Path(home)
    changed: list[str] = []
    for name in RC_FILENAMES:
        path = base / name
        try:
            kind = non_regular_kind(path)
        except OSError:
            continue  # absent, or unreadable: nothing of ours to rewrite
        if kind is not None:
            logger.warning(
                "not rewriting the PATH anchor in %s: it is a %s, not a regular file",
                path, kind,
            )
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines(keepends=True)
        inside = False
        rewritten = False
        out: list[str] = []
        for line in lines:
            if line.strip() == PATH_ANCHOR_START:
                inside = True
                out.append(line)
                continue
            if line.strip() == PATH_ANCHOR_END:
                inside = False
                out.append(line)
                continue
            if inside and PRE_SPLIT_BIN in line:
                out.append(line.replace(PRE_SPLIT_BIN, POST_SPLIT_BIN))
                rewritten = True
                continue
            out.append(line)
        if not rewritten:
            continue
        try:
            path.write_text("".join(out), encoding="utf-8")
        except OSError:
            logger.warning("could not rewrite the PATH anchor in %s", path, exc_info=True)
            continue
        changed.append(name)
    if changed:
        logger.info("PATH anchor followed the move: %s", ", ".join(changed))
    return changed


def half_migration_report(base: Path | None = None) -> str:
    """One line naming the recovery when this root is part migrated, or ``""``.

    The traceback a half-migrated start produces is accurate and operationally useless:
    it names a module, when what the host needs to know is that a directory moved. This
    answers the two conditions that produce it — the source tree this module was imported
    from no longer exists, or a register row still names a directory the split moved and
    the repair could not reach — as one sentence.
    """
    root = home_root() if base is None else Path(base)
    stale: list[str] = []
    try:
        source = Path(__file__).resolve().parent.parent
        if not source.exists():
            stale.append(f"the source tree this process runs from ({source}) is gone")
    except OSError:
        pass
    try:
        from emrg.server.agents import AgentRegistry
    except Exception:  # pragma: no cover
        AgentRegistry = None  # type: ignore[assignment]
    if AgentRegistry is not None:
        try:
            rows = AgentRegistry(register_after_migration(root))._read() or []
        except OSError:
            rows = []
        for row in rows:
            current = row.get("path") or ""
            if not current:
                continue
            try:
                # Only rows inside this root: a row for a directory outside it is not a
                # subject of this split, and naming one would tell a host its root is part
                # migrated when the dead path has nothing to do with the move.
                Path(current).relative_to(root)
            except ValueError:
                continue
            except OSError:
                continue
            try:
                if Path(current).exists():
                    continue
            except OSError:
                continue
            if repaired_path(current, root) != current:
                continue  # the next start repairs this one; not the host's problem
            stale.append(f"register row {row.get('name') or current} names {current}, which is gone")
    if not stale:
        return ""
    return (
        "emrg: this .emrg root is only part migrated - "
        + "; ".join(stale[:3])
        + (" (+more)" if len(stale) > 3 else "")
        + ". Start emrg again to finish the repair, or move the directory back by hand."
    )

