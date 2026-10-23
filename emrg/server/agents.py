"""The agent register: the directories this installation has opened sessions in.

``agents.yml`` is a YAML list, one row per directory::

    - path: /Users/argszero/work/emrg   # verbatim str(session.cwd)
      name: emrg                        # the alias a task points at; globally unique
      last_active: 2026-10-23T14:20:00.123456

This module is the only code that **writes** that file, and it is pure logic — it
imports neither the daemon nor any server state — so its rules are testable on their
own. The shape mirrors ``emrg/server/rants.py``. Readers elsewhere (the scheduler's
name-to-path resolution, the sandbox's protected-files list) read the same shape and
are unaffected by the rules below, which apply where the file is written.

**One rule: a directory is registered as itself.** ``path`` is the string the session
holds, stored and compared without ``realpath``, ``expanduser`` or trailing-slash
normalisation. A symlinked directory, a relative spelling and the canonical one are
three different rows, because they are three different strings: the register answers
"which directory did this session run in", and it is not the register's business to
guess which other spelling of it someone meant. What reads the column — the sessions
index, a task's resolved path — compares it against a value that came from a session,
so collapsing spellings here is what makes the two disagree.

**Names are unique across the register.** A name starts as the directory's basename,
and a basename already taken gets ``-2``, then ``-3``. ``scheduler._resolve_agent_path``
looks a directory up *by name*, so two rows sharing one would silently point a task at
whichever was written last. The suffix is allocated when the row is created, against
the names present at that moment; it is not part of the row's identity, and removing a
row frees its name.

**A name is reserved only while its directory is there.** A basename held by a row whose
``path`` is gone does not push the next arrival onto ``-2``: that name is taken over,
and the dead row goes with it. Both halves matter. The scheduler repairs a stale entry
*by name*, so a task may still be resolving through the dead row's name — taking the
name over is what puts it back on a directory. Suffixing beside it instead is what left
**two rows on one path**: the repair then moved the dead row onto the directory the new
row had already been given ``-2`` for. Two rows for one directory is never a legal
state, and ``upsert`` cannot write one however it is called.

Two directories are not registered, and each for a stated reason rather than a guess:

* **the home directory** — a session opened there would put its own session directory
  under ``~/.emrg/sessions/``, beside the runtime configuration. That mixes "where this
  installation keeps its files" into "a directory someone works in", and the register
  is the second thing only;
* **any directory under an existing Agent's ``.emrg/``** — the sub-session slot, the
  boss session, every directory the server derives. Those are that Agent's own
  bookkeeping, reached through its path; they are not directories a host opened, and a
  row for one would claim the bookkeeping is a workspace. The test is the path alone,
  so it needs no name, no flag and no knowledge of who created the session.

Kept from this module's predecessor, because neither is a normalisation:

* ``ensure_local_exclude`` — registration is the moment a directory is taken on as an
  Agent, and so the moment it learns to ignore the ``.emrg/`` EMRG will write into it.
  The task-level dirty-tree guard reads ``git status``, and this keeps this
  installation's own bookkeeping out of that verdict. It is called when a **row is
  created**, not on every refresh: the ignore line is written once, and re-running a
  git subprocess per message would be work with no effect.
* **the non-regular-file refusal** — ``agents.yml`` is opened directly, and an open on
  a FIFO blocks until a writer appears while a socket waits for a connection. This runs
  on the daemon's event loop, so the block would be the whole daemon. A file that
  cannot be read therefore leaves the register **unread and unrewritten**, rather than
  being answered with an empty one — which, on this method's read-then-rewrite path,
  would delete every row the host had, to report a problem it never mentioned.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import yaml

from emrg.server.atomic import atomic_write_yaml
from emrg.server.git_utils import ensure_local_exclude
from emrg.tools.base import non_regular_kind

logger = logging.getLogger(__name__)

# Fields a row carries. Anything else that a hand-edited or older file holds is
# dropped on write: `auto_evolve` and `interval` described a registry that also
# scheduled, which the task scheduler took over (`tasks.yml` owns scheduling now).
_ROW_FIELDS = ("path", "name", "last_active")


def _unique_name(base_name: str, taken: set[str]) -> str:
    """``base_name``, or the first free ``base_name-N`` — the register's naming rule."""
    if base_name not in taken:
        return base_name
    n = 2
    while f"{base_name}-{n}" in taken:
        n += 1
    return f"{base_name}-{n}"


def _kind_refusal(path: Path, kind: str) -> str:
    """The one sentence both of this module's readers say about a subject they refuse."""
    return (
        f"{path} is {kind}, not a regular file. The register is opened directly, and "
        f"opening this one blocks until a peer appears — a FIFO waits for a writer, a "
        f"socket for a connection, a device never ends — so the call would never return, "
        f"and neither would the daemon, which reads it on its event loop."
    )


class AgentRegistry:
    """The register as a file: read it, register a directory in it, drop a name from it.

    Holds the path and nothing else. Every method reads the file, so two instances
    pointed at one path cannot disagree about what it says — which is what lets the
    daemon hand out a fresh one wherever it needs it.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ── reading ──────────────────────────────────────────────────────────

    def _read(self) -> list[dict] | None:
        """Every row in file order, or ``None`` when the file could not be read.

        ``None`` and ``[]`` are different answers and the difference is the point: no
        file yet is a register with no rows, while a file that *is* there and cannot be
        read — a FIFO, a directory, a document this module cannot parse — is one whose
        contents are unknown.

        Both writers answer ``None`` by doing nothing, and that is the whole reason it
        exists. The predecessor rebuilt the register from an unparseable file, which on
        a read-then-rewrite path means **deleting every row the host had** in order to
        report a problem it never mentioned; and the removal path answered the same
        file with "there is no such entry", which is a claim about rows this call never
        saw (#2062).
        """
        try:
            kind = non_regular_kind(self.path)
        except FileNotFoundError:
            return []
        if kind is not None:
            logger.error("%s — leaving the agent register unread", _kind_refusal(self.path, kind))
            return None
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            logger.error(
                "cannot read %s — leaving the agent register unread; repair or remove "
                "the file and the next registration rebuilds it", self.path,
                exc_info=True,
            )
            return None
        if not isinstance(data, list):
            # An empty file parses as `None`, and so does a file holding a mapping: both
            # mean "no rows" to every reader here, and neither is a reason to refuse.
            return []
        rows: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict) or not entry.get("path"):
                continue
            row = {k: entry.get(k) for k in _ROW_FIELDS}
            row["path"] = str(row["path"])
            row["name"] = str(row["name"]) if row["name"] else os.path.basename(
                row["path"].rstrip(os.sep)
            ) or row["path"]
            rows.append(row)
        return rows

    def entries(self) -> list[dict]:
        """Every row, for a caller that only reads — path-sorted, as the file is written.

        A register that could not be read answers no rows here: the caller is asking
        what to *display*, and there is nothing to display. The refusal is logged, and
        the writer paths (`for_dir`, `remove`) still tell it from an empty file.
        """
        rows = self._read()
        return [] if rows is None else rows

    def paths(self) -> list[str]:
        """Just the directories, for a caller that indexes them."""
        return [row["path"] for row in self.entries() if row.get("path")]

    # ── writing ──────────────────────────────────────────────────────────

    def _write(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_yaml(
            sorted(rows, key=lambda row: row.get("path", "")),
            self.path,
            prefix=".agents_",
        )

    def for_dir(self, cwd: str | os.PathLike[str]) -> dict:
        """The row for a directory, created in place when the register lacks one.

        Returns the row — ``{"path", "name", "last_active"}`` — or ``{}`` when the
        directory is one of the two this module declines to register (the module
        docstring says which, and why).
        """
        base = str(cwd)
        if not base:
            return {}
        rows = self._read()
        if rows is None:
            return {}

        # The home directory is the one *path* exception kept.
        if base == os.path.expanduser("~"):
            return {}

        now = datetime.now().isoformat()

        # An existing row is refreshed in place, before any other test: a directory
        # already in the register has been taken on, whatever it looks like now.
        for row in rows:
            if row.get("path") == base:
                row["last_active"] = now
                self._write(rows)
                return row

        # A directory under an existing Agent's `.emrg/` is that Agent's bookkeeping
        # (the sub-session slot, the boss session), not a directory a host opened.
        for row in rows:
            inner = os.path.join(row.get("path") or "", ".emrg")
            if base == inner or base.startswith(inner + os.sep):
                return {}

        status = ensure_local_exclude(base)
        if status == "added":
            logger.info(
                "register: %s now ignores .emrg/ locally (its own .git/info/exclude)",
                base,
            )

        # A name is reserved only while the directory holding it is there. A row pointing
        # at a path that is gone is the 2026-08-12 shape, and the scheduler repairs it
        # *by name* — so a task may still be resolving through that name, and taking the
        # name over is what puts it back on a directory. Appending `-2` beside it instead
        # is what left two rows on one path: the repair then moved the dead row onto the
        # directory the new row already named.
        living = {
            row.get("name")
            for row in rows
            if row.get("name") and os.path.isdir(row.get("path") or "")
        }
        name = _unique_name(os.path.basename(base.rstrip(os.sep)) or base, living)
        # The name is this row's now: whatever still holds it points at a directory that
        # is gone (a living holder would have been in `living`), so that row is the one
        # the name is taken from.
        rows = [row for row in rows if row.get("name") != name]
        row = {"path": base, "name": name, "last_active": now}
        rows.append(row)
        self._write(rows)
        logger.info("agent registered: %s as %s", base, name)
        return row

    def upsert(self, name: str, path: str) -> dict:
        """Create the row named ``name``, or move the row already carrying that name.

        For a caller that owns a row by name rather than by directory — the scheduler's
        self-heal, which keeps an ``emrg`` row pointing at the evolution workspace. It
        writes through this module so the file keeps one writer and one temp prefix.

        The name decides which row **moves**; the path decides what may survive beside
        it. A row already naming ``path`` is the same directory said twice, so it goes:
        one directory, one row. That state was reachable — the heal moved a stale
        ``emrg`` row onto a directory a second row had been given ``-2`` for — and the
        reachable half is closed at its origin, by ``for_dir`` taking a dead row's name
        over rather than suffixing beside it. What is left here is the guarantee, for a
        register a hand-edit or an older file left in that shape.
        """
        rows = self._read()
        if rows is None:
            return {}
        now = datetime.now().isoformat()
        target = str(path)
        row = next((r for r in rows if r.get("name") == name), None)
        if row is None:
            row = {"path": target, "name": name, "last_active": now}
            rows.append(row)
        else:
            row["path"] = target
            row["last_active"] = now
        # A dropped row may be the one a task was resolving through, so its name is kept
        # in the log: after this the task finds nothing, and that is what a reader of the
        # log needs to be able to see.
        for other in [r for r in rows if r is not row and r.get("path") == target]:
            logger.warning(
                "register: dropping row %r, which names %s as well — upsert(%r) leaves "
                "one row per directory", other.get("name"), target, name,
            )
            rows.remove(other)
        self._write(rows)
        return row

    def remove(self, name: str) -> bool | None:
        """Drop the row named ``name``: ``True`` removed, ``False`` no such row.

        ``None`` is the third answer, and not a synonym for "no such row": the register
        could not be read, so nothing was decided and nothing was written. A caller that
        reports ``False`` for it tells the host "there is no such entry" about a file it
        never read.
        """
        rows = self._read()
        if rows is None:
            return None
        remaining = [row for row in rows if row.get("name") != name]
        if len(remaining) == len(rows):
            return False
        self._write(remaining)
        logger.info("remove: dropped %s from %s", name, self.path)
        return True
