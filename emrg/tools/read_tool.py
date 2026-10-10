"""Read tool — read a file from the filesystem with line numbers.

Inspired by Claude Code's FileReadTool. Default limits prevent oversized
tool results from consuming excessive tokens in the LLM context.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor, count_argument

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
                            "1 or more; 0 or less is refused rather than read as the "
                            "first line. "
                            "Alias: offset."
                        ),
                    },
                    "line_limit": {
                        "type": "integer",
                        "description": (
                            f"The number of lines to read. At least 1; 0 or less is "
                            f"refused rather than read as the default. "
                            f"Only provide if the file is too large to read at once "
                            f"(default: {DEFAULT_MAX_LINES}, max: {MAX_LINES} for explicit calls). "
                            f"Alias: limit."
                        ),
                    },
                    "start_line_byte_offset": {
                        "type": "integer",
                        "description": (
                            "Offset within the first line to begin reading "
                            "(default: 0; 0 or more — a negative value is refused rather "
                            "than read as an offset of 0, which would silently return the "
                            "line whole) — "
                            "for reading one very long line in pieces. Applied to the "
                            "decoded text, so it counts characters, not bytes (the two "
                            "differ only for non-ASCII lines; the note it prints says "
                            "chars). This tool never cuts a line, so no offset of its own "
                            "making is ever needed: the offset is the caller's. An offset "
                            "inside the line reports the line's length and what was shown; "
                            "one at or past the line's end is reported and shows that line "
                            "empty, never the whole line."
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

        # ── Resolve the three numeric parameters, refusing a value outside its
        #    domain. `count_argument` carries the measurement: each of these used to
        #    be read as a different number rather than as a caller error — a negative
        #    `line_limit` sliced from the *end* of the file and named a continuation
        #    at a negative line number, `line_limit=0` was falsy and read as absent,
        #    and a negative byte offset was clamped to 0 (the whole line) with no note.
        start_line, refusal = count_argument(
            arguments, "start_line", "offset",
            minimum=1, default=1,
            hint="Line numbers are 1-based; omit it to start at the first line",
        )
        if refusal is not None:
            return ToolResult(name="read", content=refusal, error=True)
        line_limit, refusal = count_argument(
            arguments, "line_limit", "limit",
            minimum=1, default=None,
            hint=f"Omit it to use the default of {DEFAULT_MAX_LINES} lines",
        )
        if refusal is not None:
            return ToolResult(name="read", content=refusal, error=True)
        start_line_byte_offset, refusal = count_argument(
            arguments, "start_line_byte_offset",
            minimum=0, default=0,
            hint="It counts characters in from the start of the line; omit it to read the line whole",
        )
        if refusal is not None:
            return ToolResult(name="read", content=refusal, error=True)

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
        # A file that ends with a newline splits into one element more than it has
        # lines: "a\nb\nc\n" -> ['a', 'b', 'c', ''] — the trailing '' is the position
        # *after* the last terminator, not a line of the file. Counting it made every
        # terminated file (most of them) read as one line longer than it is, render a
        # numbered line holding nothing, and announce a truncation whose continuation
        # is empty: a 1000-line file read with no arguments reported
        # "truncated at start_line=1001 ... total 1001 lines", where line 1001 was ''.
        # `MemoryIndex.from_text` (emrg/memory.py) already drops this element with the
        # same two lines and the same reasoning; this is that rule, at the readers that
        # missed it. An unterminated non-empty file has no such element and is
        # unaffected; a zero-byte file has no lines at all, and this is the only
        # spelling of the condition that says so.
        if all_lines and all_lines[-1] == "":
            all_lines.pop()
        total_lines = len(all_lines)

        if total_lines == 0:
            return ToolResult(name="read", content=f"(empty file: {path})")

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

        # ── Apply start_line_byte_offset to the first selected line ──
        #
        # The offset is honoured **literally**, and the result says what it did. Measured
        # 2026-10-08 (`cyc20261008-153534`): this left a line whose end the offset was
        # past **whole**, which is byte-identical to asking for offset 0 — so a caller who
        # resumed a long line from the wrong byte was handed the entire line and could not
        # tell its offset had been dropped, while `test_read_start_line_byte_offset_at_eol`
        # (whose own docstring says "yields empty first line") asserted
        # `"     3\t" in lines[0] or lines[0].strip().startswith("3")`, which the whole-line
        # outcome satisfies too. A reading that cannot tell "the offset was applied" from
        # "the offset was ignored" is not a reading, and this is the family the repo fixed
        # for its guards (issue #1872: an empty subject is not a clean one).
        offset_note: str | None = None
        if start_line_byte_offset > 0 and selected:
            first_line = selected[0]
            selected[0] = first_line[start_line_byte_offset:]
            if start_line_byte_offset < len(first_line):
                offset_note = (
                    f"\nnote: line {start + 1} is {len(first_line)} chars; shown from "
                    f"character {start_line_byte_offset}, so {len(selected[0])} of them"
                )
            else:
                # `[n:]` past the end is `""`, which is the honest answer to the question
                # that was asked; the note is what keeps it from reading like an empty line.
                offset_note = (
                    f"\nnote: start_line_byte_offset={start_line_byte_offset} is at or past "
                    f"the end of line {start + 1} ({len(first_line)} chars), so that line is "
                    f"shown empty — none of it was read"
                )

        # Format with line numbers
        result_lines: list[str] = []
        for i, line in enumerate(selected):
            result_lines.append(f"{start + i + 1:6d}\t{line}")

        # Reached exactly when `start_line` is past the last line: `start >= end` forces
        # `end == total_lines`, so the `lines {start + 1}-{end}` this used to print was
        # **always descending** — a range that cannot be a range, offered where the caller
        # asked why nothing came back. Measured 2026-10-10 (`cyc20261010-204628`) on a
        # 10-line file: `start_line=50` answered `(empty range: lines 50-10 of 10)` and
        # `start_line=11` answered `(empty range: lines 11-10 of 10)`. The test that pinned
        # it (`test_read_start_line_beyond_eof`) asserted only that the substring
        # `empty range` appeared, so the descending range satisfied it. This branch names
        # the condition and the two numbers it holds — the request, and the subject's size.
        if not result_lines:
            plural = "" if total_lines == 1 else "s"
            return ToolResult(
                name="read",
                content=(
                    f"(no lines: start_line={start_line} is past the end of the file, "
                    f"which has {total_lines} line{plural})"
                ),
            )

        if offset_note is not None:
            result_lines.append(offset_note)

        truncated = end < total_lines
        if truncated:
            # Exact continuation hint so LLM can copy-paste directly
            result_lines.append(
                f"\ntruncated at start_line={end + 1}, "
                f"start_line_byte_offset=0 — "
                f"total {total_lines} lines"
            )

        return ToolResult(name="read", content="\n".join(result_lines))
