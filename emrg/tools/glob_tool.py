"""Glob tool — find files matching a pattern like **/*.py or src/**/*.ts."""

from __future__ import annotations

import logging
from pathlib import Path

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

logger = logging.getLogger(__name__)

MAX_RESULTS = 500  # Cap to prevent excessive result volume


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
                "Skips hidden entries and the noise directories .git, node_modules, "
                ".venv and __pycache__, with one exception: .emrg is read, because the "
                "agent's own state lives there. Says in the result how many paths it "
                "skipped — the count is what was left after that skip, not what the "
                "tree holds."
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
                            "Working directory for the pattern (default: project root). "
                            "The hidden/noise skipping is relative to it, so point it at "
                            "the directory you mean to search — that is how to read "
                            "inside a skipped one. `grep` names this same parameter "
                            "`path`; this tool reads that spelling too, so a call "
                            "naming either one searches the tree it named."
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
        # `workdir` is this tool's declared spelling; `path` is the name the sibling
        # `grep` uses for the same parameter, and a caller who has just used one tool
        # reaches for the other's name. Neither used to read the other's, so the call
        # fell back to the cwd and answered about a tree the caller never named —
        # measured 2026-10-11 (`cyc20261011-015723`) on master `63ee3a54`, where
        # `glob path=<tmpdir>` reported `No files matched pattern '*.py' in <this
        # checkout>`. The alias convention is `count_argument`'s — `read` reads
        # `line_limit` and `limit`.
        workdir = arguments.get("workdir") or arguments.get("path") or "."

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
            matched = sorted(cwd.glob(pattern))
        except (OSError, ValueError) as e:
            return ToolResult(
                name="glob", content=f"Error: invalid pattern: {e}", error=True
            )

        matches = [p for p in matched if not self._is_hidden_or_ignored(p, cwd)]
        #: Paths that *matched the pattern* and were then dropped by the skip policy.
        #: Both messages below name this number, because without it "0" reads as "the
        #: tree holds none" and "Found N" reads as "the tree holds N" - the two readings
        #: an agent answers a question about the tree with. Measured 2026-10-06
        #: (`cyc20261006-192020`) in this checkout's `emrg/gui`: `node_modules/**/package.json`
        #: matched **352** paths, every one skipped, and the tool answered `No files
        #: matched`; `**` matched **18014** and kept 293, reported as `Found 293 matches`
        #: with nothing saying 17721 paths had been dropped on the way.
        skipped = len(matched) - len(matches)

        if not matches:
            content = f"No files matched pattern '{pattern}' in {cwd}"
            if skipped:
                # Not "no files matched": the pattern matched, and the answer is about
                # the skip rather than about the tree. The remedy is the measured one -
                # `_is_hidden_or_ignored` compares each path *relative to the root it was
                # given*, so pointing `workdir` at the directory that holds them reads
                # them (measured: `workdir=<repo>/.git`, pattern `config` returns it).
                content += (
                    f" - {skipped} path(s) matched it but were skipped as hidden or "
                    "ignored, so this is not a reading over the whole tree. That "
                    "skipping is relative to the workdir: point workdir at the "
                    "directory that holds them to read them"
                )
            return ToolResult(name="glob", content=content)

        # Format results
        lines: list[str] = []
        for p in matches[:MAX_RESULTS]:
            # `.as_posix()`: `str(Path.relative_to(...))` renders with the platform's
            # separator, so a nested match would be listed `src\main.py` on Windows while
            # every other path this tool is pointed at is written with `/` -- and a test
            # asserting the POSIX spelling would have to skip the Windows leg
            # (measured on the sibling `grep` tool: `test_grep_simple` is skipped there).
            rel = p.relative_to(cwd).as_posix()
            suffix = "/" if p.is_dir() else ""
            lines.append(f"  {rel}{suffix}")

        result = f"Found {len(matches)} matches for '{pattern}' in {cwd}"
        if skipped:
            # The count's subject, in the shape `grep` uses for its own ("searched N
            # files"): this number is what was left after the skip, and a reader who
            # is not told so has no way to tell it from the number the tree holds.
            result += (
                f" ({skipped} path(s) also matched but were skipped as hidden or ignored)"
            )
        result += ":\n"
        result += "\n".join(lines)

        if len(matches) > MAX_RESULTS:
            result += (
                f"\n\n... [{len(matches) - MAX_RESULTS} more matches not shown]"
            )

        return ToolResult(name="glob", content=result)

    @staticmethod
    def _is_hidden_or_ignored(path: Path, root: Path) -> bool:
        """Skip hidden files/dirs and common ignore paths."""
        # Skip hidden files/dirs (starting with .)
        parts = path.relative_to(root).parts
        for part in parts:
            if part.startswith(".") and part not in (".emrg",):
                return True
        # Skip common noise
        if any(p in parts for p in ("__pycache__", "node_modules", ".git", ".venv")):
            return True
        return False
