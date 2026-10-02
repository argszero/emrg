"""Read tool — read a file from the filesystem with line numbers.

Inspired by Claude Code's FileReadTool. Default limits prevent oversized
tool results from consuming excessive tokens in the LLM context.
"""

from __future__ import annotations

import json
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

# Image extensions supported in vision mode (rant 2026-08-24T14:36:01).
# When the model is vision-capable, reading these returns a structured image
# reference that the daemon converts to an OpenAI vision content block.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
# Keep image payloads sane — the base64 body is ~4/3 of the raw file size and
# counts as a single token-per-~1KB in vision APIs; oversized images are
# rejected with a hint to resize instead of blowing the context budget.
MAX_IMAGE_SIZE = 256 * 1024
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class ReadTool(ToolExecutor):
    """Read file contents with optional start_line/line_limit and line numbers."""

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read",
            description=(
                "Read a file from the filesystem. Returns content with "
                "line numbers prefixing each line (format: '  LINE_NUMBER\\tCONTENT'). "
                "Supports start_line and line_limit for reading large files in chunks. "
                "Can read text files. For images (.png/.jpg/.jpeg/.gif/.webp), "
                "returns a vision-format image block so the model can see the picture "
                "(requires a vision-capable model; otherwise a text placeholder is "
                "returned). For PDFs and notebooks, use the bash tool with appropriate "
                "commands instead."
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
                    "intent": {
                        "type": "string",
                        "description": "The purpose of this call: why you are invoking it and what you want to achieve. "
                        "One human-readable sentence, e.g. 'check how billing is implemented in billing.rs'.",
                    },
                },
                "required": ["file_path", "intent"],
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
            # A directory is a read result too, so it takes the same default bound
            # the file path below applies — this branch had none, and the module's
            # own docstring promises one ("Default limits prevent oversized tool
            # results from consuming excessive tokens"). Measured on master
            # `bc114ab9`, 2026-10-02: a directory of 5,000 entries came back as
            # 5,002 lines / 85,098 characters with no notice and `error=False`,
            # while a 5,000-**line file** was cut to 1,002 lines and said
            # `truncated at start_line=1001 ... total 5000 lines`. The trees a
            # caller lands in are the ordinary ones — `node_modules`, `dist`,
            # `.git/objects` — where the unbounded listing is thousands of lines.
            #
            # No `start_line` exists for a listing, so the notice names the calls
            # that can select instead: `glob` already caps its own answer and says
            # so, and the shell pages.
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
            shown = entries[:DEFAULT_MAX_LINES]
            lines: list[str] = [f"Directory listing for {path}/:", ""]
            for e in shown:
                suffix = "/" if e.is_dir() else ""
                lines.append(f"  {e.name}{suffix}")
            if len(entries) > len(shown):
                lines.append(
                    f"\n... [{len(entries) - len(shown)} more entries not shown — a "
                    f"directory listing is capped at {DEFAULT_MAX_LINES} entries like "
                    f"a file read; use glob with a pattern to select, or the bash "
                    f"tool to page through {path}]"
                )
            return ToolResult(name="read", content="\n".join(lines))

        file_size = path.stat().st_size
        user_specified_range = (line_limit is not None
                                or start_line > 1
                                or start_line_byte_offset > 0)

        # ── Image files → structured vision reference (rant 2026-08-24T14:36:01) ──
        # The daemon converts this JSON ref into an OpenAI vision content block
        # (base64 data URL) for vision-capable models, or a text placeholder for
        # non-vision models. Kept as a plain string here so the tool result stays
        # serializable and the daemon does the vision translation.
        ext = path.suffix.lower()
        if ext in IMAGE_EXTS:
            if file_size > MAX_IMAGE_SIZE:
                return ToolResult(
                    name="read",
                    content=(
                        f"Image is too large ({file_size:,} bytes, "
                        f"limit {MAX_IMAGE_SIZE:,}). Use the bash tool to "
                        f"resize/compress it first (e.g. sips -Z 1024 <file> "
                        f"on macOS or convert -resize on ImageMagick)."
                    ),
                    error=True,
                )
            return ToolResult(
                name="read",
                content=json.dumps({
                    "type": "image",
                    "path": str(path),
                    "mime": IMAGE_MIME[ext],
                }, ensure_ascii=False),
            )

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
