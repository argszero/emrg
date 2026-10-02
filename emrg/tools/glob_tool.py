"""Glob tool — find files matching a pattern like **/*.py or src/**/*.ts."""

from __future__ import annotations

import logging
from pathlib import Path

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

logger = logging.getLogger(__name__)

MAX_RESULTS = 500  # Cap to prevent excessive result volume

#: Path parts that are never searched. The other half of the rule — a name starting
#: with a dot hides that name — is decided in `GlobTool._skip_reason`, which is the
#: one home for both halves and the only reader of this set.
SKIP_DIRS = frozenset({"__pycache__", "node_modules", ".git", ".venv"})

#: How many of the offending names the skip note lists before it stops counting them.
SKIP_NAMES_SHOWN = 4


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
