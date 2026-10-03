"""Glob tool — find files matching a pattern like **/*.py or src/**/*.ts."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

logger = logging.getLogger(__name__)

MAX_RESULTS = 500  # Cap to prevent excessive result volume

#: Path parts that are never searched. The other half of the rule — a name starting
#: with a dot hides that name — is decided in `GlobTool._skip_reason`, which is the
#: one home for both halves and the only reader of this set.
SKIP_DIRS = frozenset({"__pycache__", "node_modules", ".git", ".venv"})

#: How many of the offending names a note lists before it stops counting them.
SKIP_NAMES_SHOWN = 4

#: The characters that make a pattern part a wildcard rather than a directory name.
#: One home: `_literal_base` reads it to find where a pattern stops naming directories,
#: and `_unlistable_dirs` reads it to decide whether any directory is listed at all.
_WILDCARD = "*?["


class GlobTool(ToolExecutor):
    """Find files matching a glob pattern relative to cwd.

    Uses Path.glob() with recursive support via ** wildcards.
    Returns matching file paths sorted by name.
    """

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="glob",
            description=(
                "Find files matching a glob pattern. "
                "Supports standard glob patterns: *, ?, [seq], ** for recursive. "
                "Use this to discover files in a project by name pattern — e.g., "
                "'**/*.py' for all Python files, 'src/**/*.ts' for TypeScript, "
                "'**/*test*' for test files. "
                "Results are capped at 500 matches, sorted by path. "
                "Names starting with '.' (other than '.emrg') and the build-noise "
                "directories __pycache__, node_modules, .git and .venv are not "
                "searched, so a hidden subtree's files do not appear; the answer names "
                "how many matching entries that dropped and which names hid them. "
                "A directory this process cannot list is named too, because the entries "
                "inside it were never matched. "
                "Use the bash tool to search a hidden subtree."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Glob pattern relative to the project root. "
                            "Examples: '**/*.py', 'src/**/*.rs', '**/*test*.py', "
                            "'*.md', 'emrg/tools/*.py'"
                        ),
                    },
                    "workdir": {
                        "type": "string",
                        "description": (
                            "Working directory for the pattern (default: project root)."
                        ),
                    },
                    "intent": {
                        "type": "string",
                        "description": "The purpose of this call: why you are invoking it and what you want to achieve. "
                        "One human-readable sentence, e.g. 'find all test files for the scheduler'.",
                    },
                },
                "required": ["pattern", "intent"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        pattern = arguments.get("pattern", "")
        workdir = arguments.get("workdir") or "."

        if not pattern:
            return ToolResult(name="glob", content="Error: no pattern provided", error=True)

        cwd = Path(workdir).expanduser().resolve()
        if not cwd.is_dir():
            return ToolResult(
                name="glob",
                content=f"Error: workdir not found or not a directory: {workdir}",
                error=True,
            )

        logger.debug("glob: pattern=%r in %s", pattern, cwd)

        try:
            found = sorted(cwd.glob(pattern))
        except (OSError, ValueError) as e:
            return ToolResult(
                name="glob", content=f"Error: invalid pattern: {e}", error=True
            )

        # One pass, and the filter's own reason is kept rather than thrown away, so the
        # answer can name what it dropped (`_skip_note`).
        matches: list[Path] = []
        culprits: list[str] = []
        for path in found:
            reason = self._skip_reason(path, cwd)
            if reason is None:
                matches.append(path)
            elif reason not in culprits:
                culprits.append(reason)

        skipped = len(found) - len(matches)
        note = self._skip_note(skipped, culprits) if skipped else ""
        note += self._blind_note(self._unlistable_dirs(cwd, pattern))

        if not matches:
            return ToolResult(
                name="glob",
                content=f"No files matched pattern '{pattern}' in {cwd}{note}",
            )

        # Format results
        lines: list[str] = []
        for p in matches[:MAX_RESULTS]:
            rel = str(p.relative_to(cwd))
            suffix = "/" if p.is_dir() else ""
            lines.append(f"  {rel}{suffix}")

        result = f"Found {len(matches)} matches for '{pattern}' in {cwd}:\n"
        result += "\n".join(lines)

        if len(matches) > MAX_RESULTS:
            result += (
                f"\n\n... [{len(matches) - MAX_RESULTS} more matches not shown]"
            )
        result += note

        return ToolResult(name="glob", content=result)

    @staticmethod
    def _skip_note(skipped: int, culprits: list[str]) -> str:
        """What this search dropped, as its own line on both answers.

        A skip that is not reported is a claim about the tree that was never measured,
        and it is the shape a reader cannot catch: measured on master `84d1cca0`,
        2026-10-02, `glob **/*.yml` **in this repository** answered
        ``No files matched pattern '**/*.yml' in /Users/xiaokeai/.emrg/evolution/emrg``
        while both CI workflows — ``.github/workflows/build-release.yml`` and
        ``.github/workflows/test.yml`` — match it and sit under a hidden directory. An
        agent that acts on that answer concludes the project has no CI, and neither the
        sentence nor the schema says a filter ran at all. Measured on the same tree:
        ``**/*.md`` reports 79 of 81 (``.pytest_cache`` and ``.github``).

        The count *and* the names are carried: the count says a tree is not empty, and
        the name says where to look next — but the culprits are the path parts that hid
        the entries (``.github``, not the two files under it, which is what a reader
        needs when a recursive pattern reaches into a hidden subtree), so they are
        deduplicated and capped.

        :param skipped: how many matching entries were dropped.
        :param culprits: the deduplicated hiding names, in the order found.
        :returns: the note, leading with the blank line that separates it, or ``""``.
        """
        shown = culprits[:SKIP_NAMES_SHOWN]
        rest = len(culprits) - len(shown)
        names = ", ".join(shown) + (f" and {rest} more" if rest > 0 else "")
        verb = "was" if skipped == 1 else "were"
        return (
            f"\n\n... [{skipped} entr{'y' if skipped == 1 else 'ies'} that match the "
            f"pattern {verb} skipped as hidden or ignored: {names}]"
        )

    @staticmethod
    def _blind_note(dirs: list[str]) -> str:
        """What this search could not read at all, as its own line on both answers.

        The second half of the same rule `_skip_note` carries, and the worse half: an
        entry dropped by a filter is an entry the search *saw and refused*, while a
        directory it cannot open is one whose entries were **never known to exist**.
        Measured 2026-10-04 (`cyc20261004-022954`) on the branch's own tree, over a
        `locked/` holding the only other ``.py`` file: ``glob **/*.py`` answered
        ``Found 1 matches`` with `error=False`, and over the locked directory itself —
        the `workdir` — the same tool answered ``No files matched pattern '*.py'``.
        Both are confident claims about a tree it never finished reading, and the
        second one is the shape a reader cannot catch at all.

        :param dirs: relative spellings of the directories that could not be listed.
        :returns: the note, leading with the blank line that separates it, or ``""``.
        """
        if not dirs:
            return ""
        shown = dirs[:SKIP_NAMES_SHOWN]
        rest = len(dirs) - len(shown)
        names = ", ".join(shown) + (f" and {rest} more" if rest > 0 else "")
        one = len(dirs) == 1
        return (
            f"\n\n... [{len(dirs)} director{'y' if one else 'ies'} could not be listed: "
            f"{names} — entries inside {'it' if one else 'them'} were never matched]"
        )

    @staticmethod
    def _skip_reason(path: Path, root: Path) -> str | None:
        """The path part that hides ``path`` from this search, or ``None``.

        One home for both halves of the rule — a name starting with a dot hides that
        name, and `SKIP_DIRS` names the build noise — and it answers *where* as well as
        *whether*, because an answer has to be able to say what it dropped
        (`_skip_note` carries the measurement that needs this).

        ``.emrg`` is the one exemption: it is this agent's own state directory, and the
        memory files under it are exactly what an evolution cycle searches for.

        :param path: one entry the pattern matched, not yet filtered.
        :param root: the directory the pattern was run in.
        :returns: the first offending path part relative to `root`, or ``None`` when
            nothing hides it.
        """
        for part in path.relative_to(root).parts:
            if part.startswith(".") and part != ".emrg":
                return part
            if part in SKIP_DIRS:
                return part
        return None

    @staticmethod
    def _literal_base(pattern: str) -> str:
        """The leading run of `pattern`'s parts that names directories, not wildcards.

        Returned as a relative spelling (``"."`` when the pattern can match in the
        search root itself), because the caller asks one directory a question about
        listability and needs the spelling a reader can go to.

        :param pattern: the glob pattern as the caller wrote it.
        :returns: e.g. ``"."`` for ``**/*.py``, ``"src"`` for ``src/**/*.ts``.
        """
        parts: list[str] = []
        for part in Path(pattern).parts:
            if any(ch in part for ch in _WILDCARD):
                break
            parts.append(part)
        return os.path.join(*parts) if parts else "."

    @staticmethod
    def _unlistable_dirs(root: Path, pattern: str) -> list[str]:
        """Directories this search would have read and could not, relative to `root`.

        `Path.glob` **swallows** the `PermissionError` a directory it cannot open
        raises: the entries inside simply never appear, nothing is raised, and the tool
        answers ``Found N matches`` or ``No files matched pattern ...`` over a tree it
        never finished reading. Measured 2026-10-04 (`cyc20261004-022954`) with the real
        tool — the tree holds two ``.py`` files, the second under ``locked/``:

            locked/ listable   -> Found 1 matches ...   (both files reported)
            locked/ chmod 000  -> Found 1 matches ...   (error=False, and silent)
            workdir = locked/  -> No files matched pattern '*.py' in <locked>

        Which directories are in scope is *the pattern's* answer, not this function's: a
        fully literal pattern (`emrg/tools`) lists nothing at all — `Path.glob` stats the
        one path it names — so no contents can be missed; a pattern with `**` in it walks
        everything under its literal base; and a pattern without `**` reads exactly its
        literal base one level deep. A base the search never means to enter (a dotted
        name, a `SKIP_DIRS` entry) is out of scope rather than unread, which is the same
        predicate `_skip_reason` already answers, so it is asked rather than re-spelled.

        :param root: the directory the pattern is run in.
        :param pattern: the glob pattern as the caller wrote it.
        :returns: sorted relative spellings, empty when nothing was missed.
        """
        if not any(ch in pattern for ch in _WILDCARD):
            return []
        base = GlobTool._literal_base(pattern)
        target = root if base == "." else root / base
        if GlobTool._skip_reason(target, root) is not None:
            return []
        if "**" in pattern:
            return GlobTool._walk_unlistable(target, root)
        return GlobTool._named_base_unlistable(target, base)

    @staticmethod
    def _walk_unlistable(target: Path, root: Path) -> list[str]:
        """Every directory under `target` this process may not open.

        `os.walk` takes an `onerror` hook where `Path.glob` has none, so this walks a
        second time. It opens no file, so it costs one directory listing per directory
        on top of the one the search already did — not a second read of the tree's
        contents. The alternative, matching the pattern from this walk, would be a second
        implementation of `glob`'s pattern semantics, which is exactly the kind of thing
        a later Python release changes quietly.

        Only `PermissionError` counts. The walk is entered at `target`, which the
        *pattern* named rather than discovered: a base that is simply not there
        (``nope/**/*.py``) raises `FileNotFoundError` for the same directory a
        discovered one would raise a permission error from, and a pattern that matches
        nothing is an answer rather than a hole.

        :param target: the literal base of the pattern, already in scope.
        :param root: the directory the pattern is run in, for relative spellings.
        :returns: sorted relative spellings, empty when every directory was readable.
        """
        unreadable: list[str] = []

        def on_error(exc: OSError) -> None:
            if not isinstance(exc, PermissionError):
                return
            name = getattr(exc, "filename", None)
            if not name:
                return
            path = Path(name)
            try:
                unreadable.append(str(path.relative_to(root)))
            except ValueError:  # a path the walk reached from outside `root`
                unreadable.append(str(path))

        for dirpath, dirnames, _files in os.walk(target, onerror=on_error):
            # Prune before descending: a directory the search skips by design is not a
            # hole in its coverage, and pruning keeps this from reporting one.
            dirnames[:] = [
                name
                for name in dirnames
                if GlobTool._skip_reason(Path(dirpath) / name, root) is None
            ]
        return sorted(set(unreadable))

    @staticmethod
    def _named_base_unlistable(target: Path, base: str) -> list[str]:
        """The one directory a non-recursive pattern reads, if it refuses to be read.

        `src/*.py` lists `src` and nothing under it, so that directory is the whole of
        what can hide a match. It is a directory the *caller* named, so failing to open
        it is not the same evidence a discovered directory gives: a base that is not
        there is a pattern that matches nothing, and a base that is a file is the same,
        neither of which is a hole. Only a base that exists and refuses is reported.

        :param target: the literal base of the pattern, already in scope.
        :param base: that base as the relative spelling a reader can go to.
        :returns: ``[base]`` when it could not be listed, else ``[]``.
        """
        try:
            with os.scandir(target):
                pass
        except PermissionError:
            return [base]
        except OSError:
            return []
        return []
