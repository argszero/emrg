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

        # Search
        results: list[str] = []
        #: Where each match block's header line landed in ``results``. The block count
        #: is this list's length, and nothing downstream re-derives it from the
        #: rendered text — see the summary below for what that cost.
        block_starts: list[int] = []
        files_searched = 0
        stop = False
        #: Set when the loop stopped at the result budget rather than at the end of the
        #: tree. The count is then a **floor**, and the summary has to say so: a number
        #: produced by a budget reads exactly like a number produced by counting, and
        #: this is the reading an agent answers "how many places does this happen?" from.
        search_cut = False

        for filepath in files:
            if stop:
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
                if stop:
                    break
                if regex.search(line):
                    ctx_start = max(0, i - context_before)
                    ctx_end = min(len(lines), i + 1 + context_after)

                    block_starts.append(len(results))
                    results.append(f"{rel}:{i + 1}:")
                    for ctx_i in range(ctx_start, ctx_end):
                        marker = ">" if ctx_i == i else " "
                        results.append(f" {marker}{lines[ctx_i]}")

                    if len(results) > max_results * (2 + context_before + context_after):
                        stop = True
                        search_cut = True
                        break

        if not results:
            return ToolResult(
                name="grep",
                content=(
                    f"No matches for '{pattern}' in {root} "
                    f"(searched {files_searched} files)"
                    + (f" matching '{file_glob}'" if file_glob else "")
                ),
            )

        # Build output. The count is the number of blocks appended above, not a
        # re-reading of the rendered lines: a block header is ``f"{rel}:{i}:"`` and a
        # context line is emitted with its own text intact (``" " + marker + text``),
        # so the predicate "ends with a colon" cannot tell them apart — a YAML block
        # key, a `public:` label or a Markdown `Term:` inside the window was counted as
        # a match. Measured on master `6b417c4`, 2026-10-02: one matching line with
        # `first:` / `second:` / `third:` around it printed **Found 4 matches** at
        # `context_before=1, context_after=2` and **Found 1 matches** for the same
        # search without context — asking for context created matches that do not
        # exist, and the inflation grows with the amount of context requested.
        matches_found = len(block_starts)
        if search_cut:
            # The second half of the same claim (measured on master `6b417c4`, 2026-10-02):
            # the loop above stops once the rendered lines pass
            # ``max_results * (2 + context_before + context_after)`` — i.e. at about
            # ``max_results`` matches — and the summary printed that number as if it were
            # the number in the tree. A file holding 4000 matching lines came back as
            # "Found 11 matches ... (searched 1 files)" with **no** indication that the
            # search had stopped, so the floor read as a total. It is the same claim this
            # action's count makes, one cause upstream, and the same reader: an agent
            # answering "how many places does this happen?" from a budget.
            summary = (
                f"Found {matches_found} matches for '{pattern}' in {root}, where the "
                f"search stopped at its result budget (max_results={max_results}) after "
                f"{files_searched} file(s) - so this count is a floor and the tree may "
                f"hold more. Narrow the pattern or the path, or raise max_results, to "
                f"count them all:\n\n"
            )
        else:
            summary = (
                f"Found {matches_found} matches for '{pattern}' "
                f"in {root} (searched {files_searched} files):\n\n"
            )

        # Truncate if too many lines — at a **block boundary**, and the note names the
        # two numbers this actually measured. A cut at ``max_results * 3`` lines lands
        # inside a block (a block is 1 header + the context window) and leaves context
        # lines whose header is gone, i.e. output that reads as belonging to no match;
        # and because a block is longer than one line, those same ``max_results * 3``
        # lines are *fewer* than ``max_results`` blocks, which the note used to claim.
        line_budget = max_results * 3
        if len(results) > line_budget:
            shown = 0
            cut = 0
            for index, start in enumerate(block_starts):
                end = (
                    block_starts[index + 1]
                    if index + 1 < len(block_starts)
                    else len(results)
                )
                # The first block is always kept: a budget smaller than one block would
                # otherwise print nothing but the notice.
                if end > line_budget and shown:
                    break
                shown += 1
                cut = end
            results = results[:cut]
            results.append(
                f"\n... [output truncated: {shown} of {matches_found} match blocks shown]"
            )

        return ToolResult(name="grep", content=summary + "\n".join(results))

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
