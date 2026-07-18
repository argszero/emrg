"""Glob tool — find files matching a pattern like **/*.py or src/**/*.ts."""

from __future__ import annotations

import logging
from pathlib import Path

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

logger = logging.getLogger(__name__)

MAX_RESULTS = 500  # Cap to prevent NDJSON overflow


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
                "Results are capped at 500 matches, sorted by path."
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
                },
                "required": ["pattern"],
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
            matches = sorted(
                p for p in cwd.glob(pattern)
                if not self._is_hidden_or_ignored(p, cwd)
            )
        except (OSError, ValueError) as e:
            return ToolResult(
                name="glob", content=f"Error: invalid pattern: {e}", error=True
            )

        if not matches:
            return ToolResult(
                name="glob",
                content=f"No files matched pattern '{pattern}' in {cwd}",
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
