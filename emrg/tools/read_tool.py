"""Read tool — read a file from the filesystem with line numbers.

Inspired by Claude Code's FileReadTool: default limits prevent oversized
tool results from overflowing the NDJSON transport layer.
"""

from __future__ import annotations

import logging
from pathlib import Path

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

logger = logging.getLogger(__name__)

MAX_LINES = 2000  # Default max lines per read (matches Claude Code)
MAX_READ_SIZE = 256 * 1024  # 256KB — file size cap (matches Claude Code)

# NDJSON safe max lines: a line of JSON must not exceed 64KB (asyncio readline limit).
# With JSON escaping and line-number prefix overhead, ~500 lines is the worst-case
# safe limit. Two-tier: explicit limit allowed up to 50KB worth of content,
# but default cap prevents overflow for any file.
NDJSON_SAFE_MAX_LINES = 1000


class ReadTool(ToolExecutor):
    """Read file contents with optional offset/limit and line numbers."""

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read",
            description=(
                "Read a file from the filesystem. Returns content with "
                "line numbers prefixing each line (format: '  LINE_NUMBER\\tCONTENT'). "
                "Supports offset and limit for reading large files in chunks. "
                "Can read text files. For images, PDFs, and notebooks, "
                "use the bash tool with appropriate commands instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to read.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Line number to start reading from. Only provide if the "
                            "file is too large to read at once (default: 1)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"The number of lines to read. "
                            f"Only provide if the file is too large to read at once "
                            f"(default: {MAX_LINES}, max: {MAX_LINES} for explicit calls)."
                        ),
                    },
                },
                "required": ["file_path"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        file_path = arguments.get("file_path", "")
        try:
            raw_offset = arguments.get("offset", 0) or 0
            offset = max(1, int(raw_offset))
        except (TypeError, ValueError):
            offset = 1
        limit = arguments.get("limit")  # None → use default MAX_LINES
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = None  # fall back to default

        if not file_path:
            return ToolResult(name="read", content="Error: no file_path provided", error=True)

        path = Path(file_path).expanduser().resolve()
        logger.debug("read: %s (offset=%d, limit=%s)", path, offset, limit)

        if not path.exists():
            return ToolResult(
                name="read",
                content=f"Error: file not found: {path}",
                error=True,
            )

        if path.is_dir():
            # Directory listing
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
            lines: list[str] = [f"Directory listing for {path}/:", ""]
            for e in entries:
                suffix = "/" if e.is_dir() else ""
                lines.append(f"  {e.name}{suffix}")
            return ToolResult(name="read", content="\n".join(lines))

        file_size = path.stat().st_size
        user_specified_range = arguments.get("limit") is not None or arguments.get("offset", 0) > 0

        # File too large and user hasn't specified a range → error with guidance
        if file_size > MAX_READ_SIZE and not user_specified_range:
            return ToolResult(
                name="read",
                content=(
                    f"File is too large ({file_size:,} bytes). "
                    f"Use offset and limit parameters to read specific portions "
                    f"of the file, or use the bash tool with head/tail/sed to "
                    f"search for specific content.\n\n"
                    f"Example: read with offset=1, limit={MAX_LINES} to read "
                    f"the first {MAX_LINES} lines."
                ),
                error=True,
            )

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                name="read",
                content=f"Error: cannot read {path} as text (binary file?)",
                error=True,
            )

        all_lines = text.split("\n")
        total_lines = len(all_lines)

        # Compute effective limit — two tiers:
        #   Default (no limit specified): capped at NDJSON_SAFE_MAX_LINES to
        #     guarantee the JSON line stays under 64KB for any file content.
        #   Explicit limit: honored up to MAX_LINES (LLM knows what it asked for).
        explicit_limit = arguments.get("limit")
        if explicit_limit is not None:
            effective_limit = min(explicit_limit, MAX_LINES)
        else:
            effective_limit = NDJSON_SAFE_MAX_LINES

        start = offset - 1
        end = min(start + effective_limit, total_lines)
        selected = all_lines[start:end]

        # Format with line numbers
        result_lines: list[str] = []
        for i, line in enumerate(selected):
            result_lines.append(f"{start + i + 1:6d}\t{line}")

        if not result_lines:
            return ToolResult(
                name="read",
                content=f"(empty range: lines {start + 1}-{end} of {total_lines})",
            )

        truncated = end < total_lines
        if truncated:
            result_lines.append(
                f"\n... [truncated {total_lines - end} lines] "
                f"total {total_lines} lines | "
                f"use offset={end + 1} to read more"
            )

        return ToolResult(name="read", content="\n".join(result_lines))
