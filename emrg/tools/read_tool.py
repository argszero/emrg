"""Read tool — read a file from the filesystem with line numbers.

Inspired by Claude Code's FileReadTool. Default limits prevent oversized
tool results from consuming excessive tokens in the LLM context.
"""

from __future__ import annotations

import logging
from pathlib import Path

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

logger = logging.getLogger(__name__)

MAX_LINES = 2000  # Default max lines per read (matches Claude Code)
MAX_READ_SIZE = 256 * 1024  # 256KB — file size cap (matches Claude Code)

# Default max lines when no explicit limit is specified by the LLM.
# This prevents oversized context consumption for unknown file sizes.
# When the LLM explicitly requests a limit, up to MAX_LINES is honored.
DEFAULT_MAX_LINES = 1000


class ReadTool(ToolExecutor):
    """Read file contents with optional start_line/line_limit and line numbers."""

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read",
            description=(
                "Read a file from the filesystem. Returns content with "
                "line numbers prefixing each line (format: '  LINE_NUMBER\\tCONTENT'). "
                "Supports start_line and line_limit for reading large files in chunks. "
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
                    "start_line": {
                        "type": "integer",
                        "description": (
                            "Line number to start reading from (default: 1). "
                            "Alias: offset."
                        ),
                    },
                    "line_limit": {
                        "type": "integer",
                        "description": (
                            f"The number of lines to read. "
                            f"Only provide if the file is too large to read at once "
                            f"(default: {DEFAULT_MAX_LINES}, max: {MAX_LINES} for explicit calls). "
                            f"Alias: limit."
                        ),
                    },
                    "start_line_byte_offset": {
                        "type": "integer",
                        "description": (
                            "Byte offset within the first line to begin reading "
                            "(default: 0). Use to resume within a truncated line."
                        ),
                    },
                },
                "required": ["file_path"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        file_path = arguments.get("file_path", "")

        # ── Resolve start_line: support both start_line (new) and offset (legacy alias) ──
        raw_start = (arguments.get("start_line")
                     or arguments.get("offset", 0) or 0)
        try:
            start_line = max(1, int(raw_start))
        except (TypeError, ValueError):
            start_line = 1

        # ── Resolve line_limit: support both line_limit (new) and limit (legacy alias) ──
        raw_limit = arguments.get("line_limit") or arguments.get("limit")
        line_limit: int | None = None
        if raw_limit is not None:
            try:
                line_limit = int(raw_limit)
            except (TypeError, ValueError):
                line_limit = None

        # ── Resolve start_line_byte_offset ──
        raw_byte_off = arguments.get("start_line_byte_offset", 0) or 0
        try:
            start_line_byte_offset = max(0, int(raw_byte_off))
        except (TypeError, ValueError):
            start_line_byte_offset = 0

        if not file_path:
            return ToolResult(name="read", content="Error: no file_path provided", error=True)

        path = Path(file_path).expanduser().resolve()
        logger.debug("read: %s (start_line=%d, byte_offset=%d)", path, start_line, start_line_byte_offset)

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
        user_specified_range = (line_limit is not None
                                or start_line > 1
                                or start_line_byte_offset > 0)

        # File too large and user hasn't specified a range → error with guidance
        if file_size > MAX_READ_SIZE and not user_specified_range:
            return ToolResult(
                name="read",
                content=(
                    f"File is too large ({file_size:,} bytes). "
                    f"Use start_line and line_limit parameters to read specific "
                    f"portions of the file, or use the bash tool with "
                    f"head/tail/sed to search for specific content.\n\n"
                    f"Example: read with start_line=1, line_limit={MAX_LINES} "
                    f"to read the first {MAX_LINES} lines."
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
        #   Default (no limit specified): capped at DEFAULT_MAX_LINES to
        #     prevent excessive token consumption from unknown file sizes.
        #   Explicit limit: honored up to MAX_LINES (LLM knows what it asked for).
        if line_limit is not None:
            effective_limit = min(line_limit, MAX_LINES)
        else:
            effective_limit = DEFAULT_MAX_LINES

        start = start_line - 1
        end = min(start + effective_limit, total_lines)
        selected = all_lines[start:end]

        # Apply start_line_byte_offset to the first selected line
        if start_line_byte_offset > 0 and selected:
            first_line = selected[0]
            if start_line_byte_offset < len(first_line):
                selected[0] = first_line[start_line_byte_offset:]

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
            # Exact continuation hint so LLM can copy-paste directly
            result_lines.append(
                f"\ntruncated at start_line={end + 1}, "
                f"start_line_byte_offset=0 — "
                f"total {total_lines} lines"
            )

        return ToolResult(name="read", content="\n".join(result_lines))
