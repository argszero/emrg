"""Grep tool — search file contents with regex patterns.

Inspired by Claude Code's Grep tool: searches across files for a pattern,
returns matching lines with filename:line_number prefixes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

logger = logging.getLogger(__name__)

MAX_RESULTS = 200  # Cap matches to prevent excessive result volume
MAX_FILE_SIZE = 512 * 1024  # 512KB — skip files larger than this


class GrepTool(ToolExecutor):
    """Search file contents using regex patterns with optional context lines.

    Returns matches as filename:line_number: content. Skips binary files,
    hidden dirs, and files over 512KB.
    """

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="grep",
            description=(
                "Search file contents for a regex pattern. "
                "Returns matching lines prefixed with filename:line_number. "
                "Supports -i (case-insensitive), context lines before/after matches, "
                "file glob filtering, and output truncation caps. "
                "Use this instead of 'bash grep' for cross-platform pattern search "
                "with automatic binary/hidden file skipping."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Regex pattern to search for. Supports Python regex syntax. "
                            "Examples: 'def test_', 'import os', 'TODO|FIXME', 'class \\w+Tool'"
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "File or directory to search. If a directory, searches "
                            "recursively. Default: current project root."
                        ),
                    },
                    "glob": {
                        "type": "string",
                        "description": (
                            "Only search files matching this glob pattern. "
                            "Examples: '*.py', '*.{py,rs}', 'src/**/*.ts'. "
                            "Default: all text files."
                        ),
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Case-insensitive search (default: false).",
                    },
                    "context_before": {
                        "type": "integer",
                        "description": "Number of context lines to show before each match.",
                    },
                    "context_after": {
                        "type": "integer",
                        "description": "Number of context lines to show after each match.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"Maximum matches to return (default: {MAX_RESULTS}).",
                    },
                    "intent": {
                        "type": "string",
                        "description": "The purpose of this call: why you are invoking it and what you want to achieve. "
                        "One human-readable sentence, e.g. 'find all callers of connect_to_server'.",
                    },
                },
                "required": ["pattern", "intent"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        pattern = arguments.get("pattern", "")
        search_path = arguments.get("path") or "."
        file_glob = arguments.get("glob")
        ignore_case = arguments.get("ignore_case", False)
        context_before = arguments.get("context_before") or 0
        context_after = arguments.get("context_after") or 0
        max_results = arguments.get("max_results") or MAX_RESULTS

        if not pattern:
            return ToolResult(
                name="grep", content="Error: no pattern provided", error=True
            )

        # Compile regex
        try:
            flags = re.IGNORECASE if ignore_case else 0
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(
                name="grep", content=f"Error: invalid regex pattern: {e}", error=True
            )

        root = Path(search_path).expanduser().resolve()
        if not root.exists():
            return ToolResult(
                name="grep", content=f"Error: path not found: {root}", error=True
            )

        logger.debug(
            "grep: pattern=%r path=%s glob=%s ignore_case=%s",
            pattern, root, file_glob, ignore_case,
        )

        # Collect files
        if root.is_file():
            files = [root]
        else:
            files = self._collect_files(root, file_glob)

        # Search. Blocks are collected **whole** — one per matching line, header first —
        # so the returned output can never end inside a context window, and the count is
        # taken where the matches are found rather than re-derived from the rendered text.
        # Both of those are measured defects, not tidiness (issue #1805): the count used to
        # be `sum(1 for r in results if r.endswith(":"))`, which the *context* lines that
        # happen to end in a colon also satisfy, so asking for context invented matches:
        # one matching line reported `Found 1 matches` with no context and `Found 4 matches`
        # with `context_before=1, context_after=2`. Measured 2026-10-02 on a file holding
        # `first:` / `second:` / `TARGET line` / `third:` / `fourth:`.
        blocks: list[list[str]] = []
        matches_found = 0
        files_searched = 0
        #: Whether the scan stopped because it had already seen one match more than
        #: `max_results` returns. When set, `matches_found` is a **lower bound**: the
        #: remaining files were never read, so no honest total exists to print.
        scan_capped = False

        for filepath in files:
            if scan_capped:
                break
            files_searched += 1

            # Skip large files
            try:
                if filepath.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            # Read and search
            try:
                lines = filepath.read_text(encoding="utf-8").split("\n")
            except (UnicodeDecodeError, OSError):
                continue

            rel = str(filepath.relative_to(root.parent if root.is_file() else root))

            for i, line in enumerate(lines):
                if not regex.search(line):
                    continue
                matches_found += 1
                if len(blocks) >= max_results:
                    # `max_results` is documented as "Maximum matches to return", so the cap
                    # is on matches. Reaching it with a match still in hand is what proves
                    # there is at least one more — the one measured fact about the rest of
                    # the tree this scan is allowed to have.
                    scan_capped = True
                    break

                ctx_start = max(0, i - context_before)
                ctx_end = min(len(lines), i + 1 + context_after)
                block = [f"{rel}:{i + 1}:"]
                for ctx_i in range(ctx_start, ctx_end):
                    marker = ">" if ctx_i == i else " "
                    block.append(f" {marker}{lines[ctx_i]}")
                blocks.append(block)

        if not blocks:
            return ToolResult(
                name="grep",
                content=(
                    f"No matches for '{pattern}' in {root} "
                    f"(searched {files_searched} files)"
                    + (f" matching '{file_glob}'" if file_glob else "")
                ),
            )

        # Build output. Two measured numbers, and the reader is told which kind of claim
        # each one is: an exact count, or a lower bound with the reason it is one.
        if scan_capped:
            summary = (
                f"Found at least {matches_found} matches for '{pattern}' in {root} "
                f"(searched {files_searched} files); returning the first {len(blocks)} - "
                "the search stopped once it had more than `max_results` matches, so the "
                "rest of the tree was not read and the total is not known\n\n"
            )
        else:
            summary = (
                f"Found {matches_found} matches for '{pattern}' "
                f"in {root} (searched {files_searched} files):\n\n"
            )

        return ToolResult(
            name="grep",
            content=summary + "\n".join(line for block in blocks for line in block),
        )

    @staticmethod
    def _collect_files(root: Path, file_glob: str | None) -> list[Path]:
        """Collect files recursively, skipping hidden/ignored dirs."""
        skip_dirs = {"__pycache__", "node_modules", ".git", ".venv"}
        glob_pattern = file_glob or "*"

        # Filter first (cheap), then sort (expensive on large repos)
        files: list[Path] = []
        for path in root.rglob(glob_pattern):
            parts = path.relative_to(root).parts
            if any(p.startswith(".") and p not in (".emrg",) for p in parts):
                continue
            if any(p in skip_dirs for p in parts):
                continue
            if path.is_file():
                files.append(path)

        files.sort()
        return files
