"""Write tool — create or overwrite a file."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from emrg.sandbox.fence import file_refusal
from emrg.sandbox.policy import grants_from_arguments, resolve_policy
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools import read_receipts
from emrg.tools.base import ToolExecutor, special_file_kind, special_file_refusal
from emrg.tools.file_policy import resolve_file_target

logger = logging.getLogger(__name__)

MAX_WRITE_SIZE = 10 * 1024 * 1024  # 10 MB safety limit


def _lines_held(path: Path) -> int | None:
    """How many lines `path` holds, by the read tool's own rule, or None.

    The rule is the read tool's rather than a fresh one: a file ending in a
    newline splits into one element more than it has lines, and counting that
    empty tail made every terminated file read as one line longer than it is
    (`emrg/tools/read_tool.py`, the paragraph above `total_lines`). The refusal
    below quotes a number back to the caller, so it has to be the number the
    caller was shown.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    # An open is not a read: on a FIFO it blocks until a peer appears, and this
    # runs on the daemon's event loop. `execute` has already refused a non-regular
    # subject at this path, and the kind is asked again here rather than leaning on
    # that — the path can be replaced between the two stats, and a refusal is worth
    # less than the daemon it would block.
    if special_file_kind(info.st_mode) is not None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # A binary or unreadable subject has no line count to state; the refusal
        # falls back to its size rather than inventing one.
        return None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return len(lines)


def _blind_overwrite_refusal(
    path: Path,
    size: int,
    coverage: tuple[int, int] | None,
) -> str:
    """The refusal a whole-file replacement earns when the file was not read.

    It names **both** numbers — what the file holds and what has been shown —
    because the caller's next action depends on the gap between them, and a
    refusal that said only "read it first" would leave the size of the loss
    unstated.
    """
    held = _lines_held(path)
    holds = (
        f"{held:,} lines ({size:,} bytes)"
        if held is not None
        else f"{size:,} bytes"
    )
    if coverage is None:
        seen = "none of them has been shown to this session"
    else:
        shown, total = coverage
        seen = (
            f"only {shown:,} of those {total:,} lines have been shown to this "
            f"session"
        )
    return (
        f"Error: {path} already holds {holds} and this call would replace all of "
        f"it, but {seen}.\n\n"
        f"A write that replaces content the caller has not read is refused rather "
        f"than answered: what it removes is invisible to whoever asked for it, and "
        f"the loss is silent — the file is simply shorter, and this tool reports "
        f"success.\n\n"
        f"Do one of these:\n"
        f"  - read the file first. `read` returns at most 1000 lines per call and "
        f"says so with a `truncated` line; a longer file takes repeated calls from "
        f"where the last one stopped, and the refusal stops once the whole file has "
        f"been shown;\n"
        f"  - use `edit` instead, which changes only the text you quote and is the "
        f"right tool for a large file.\n\n"
        f"Creating a new file needs nothing, and so does replacing a file this "
        f"session has just written."
    )


class WriteTool(ToolExecutor):
    """Write content to a file, creating parent directories as needed."""

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="write",
            description=(
                "Write content to a file. Creates the file if it doesn't exist, "
                "or overwrites it if it does. Parent directories are created "
                "automatically. Use this for creating new files or fully "
                "replacing existing file contents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path where the file should be written.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete content to write to the file.",
                    },
                    "intent": {
                        "type": "string",
                        "description": "The purpose of this call: why you are invoking it and what you want to achieve. "
                        "One human-readable sentence, e.g. 'write memory file recording the decision'.",
                    },
                },
                "required": ["file_path", "content", "intent"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        file_path = arguments.get("file_path", "")
        content = arguments.get("content", "")

        if not file_path:
            return ToolResult(name="write", content="Error: no file_path provided", error=True)

        if len(content) > MAX_WRITE_SIZE:
            return ToolResult(
                name="write",
                content=f"Error: content too large ({len(content)} chars > {MAX_WRITE_SIZE})",
                error=True,
            )

        # Where the bytes actually go, decided in one place (issue #1558). A
        # relative `file_path` is joined onto the injected workspace — the
        # session cwd — instead of being resolved against the daemon's own cwd,
        # which is the tree the predicates do not judge.
        target = resolve_file_target(file_path, arguments.get("workspace"))
        path = Path(target).resolve()

        # One policy for both tools (host ruling 2026-09-28T21:50, issue #1553):
        # the boundary is the derivation the kernel-enforced dialects read too
        # (`emrg/sandbox/fence.py`), so `read-only` writes nowhere — exactly as a
        # `read-only` bash command can — and `workspace-write` writes under the
        # session cwd and the platform temp areas and nowhere else. The workspace
        # boundary is injected by the daemon (session cwd); a call carrying no
        # tier resolves to `danger-full-access` and is unconfined, as before
        # (`policy.DEFAULT_MODE`), which is what keeps a host session unguarded.
        # The joined `target` is what the fence is handed, so the file it judges
        # is the file written below (before #1558 the spelling was passed on and
        # resolved elsewhere).
        policy = resolve_policy(
            mode=arguments.get("sandbox"),
            workspace_root=str(arguments.get("workspace") or os.getcwd()),
            session_id=arguments.get("session_id"),
            extra_grants=grants_from_arguments(arguments),
        )
        reason = file_refusal(target, policy)
        if reason:
            return ToolResult(name="write", content=reason, error=True)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return ToolResult(name="write", content=f"Error creating directory: {e}", error=True)

        existed = path.exists()
        before: os.stat_result | None = None
        if existed:
            before = path.stat()
            # An existing subject has a kind, and only a regular file can be written:
            # `write_text` on a FIFO blocks at the open until a reader appears (measured
            # 2026-10-10, `cyc20261010-215146`: no return within 10 s in its own
            # process, while the same call against a regular file returned). A path that
            # does not exist yet has no kind to judge, and creating it is the call's job.
            special = special_file_kind(before.st_mode)
            if special is not None:
                return ToolResult(
                    name="write", content=special_file_refusal(path, special), error=True
                )

            # ── A whole-file replacement of content nobody has read ──
            #
            # `write` replaces every byte of its subject, so the one fact it needs
            # is whether the caller has been shown them. Nothing asked that
            # question, and the cost was measured: a 5,000-line file read through
            # the read tool's default cap, written back with what that read
            # returned, came out at 1,002 lines — a call that reported `Updated`
            # (issue #2652). Every number below comes from the file on disk now,
            # not from anything the caller supplied.
            #
            # The guard is keyed on the version that is actually there: a receipt
            # describing an older version does not match, so a file that changed
            # after the read is read again rather than overwritten on the strength
            # of a reading about different content.
            #
            # A file that holds nothing has nothing for this call to destroy, so
            # there is no reading to demand: `write` is how an empty file, or one
            # whose content is being cleared, is authored. Refusing it would be a
            # refusal with no loss behind it — and it printed one, measured
            # 2026-10-30: `already holds 0 lines (0 bytes) … none of them has been
            # shown to this session`, which asks the caller to read a file with
            # nothing in it.
            coverage: tuple[int, int] | None = (0, 0) if before.st_size == 0 else None
            if coverage is None:
                coverage = read_receipts.coverage(
                    path,
                    size=before.st_size,
                    mtime_ns=before.st_mtime_ns,
                    session=arguments.get("_session_id"),
                )
            if coverage is None or coverage[0] < coverage[1]:
                return ToolResult(
                    name="write",
                    content=_blind_overwrite_refusal(path, before.st_size, coverage),
                    error=True,
                )
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            return ToolResult(name="write", content=f"Error writing file: {e}", error=True)

        # The caller has now authored every byte of the file, so a second write in
        # the same turn needs no further reading — and the receipt must describe the
        # version this call produced, not the one it replaced.
        try:
            after = path.stat()
        except OSError:
            after = None
        if after is not None:
            written_lines = len(content.split("\n"))
            if written_lines and content.endswith("\n"):
                written_lines -= 1
            read_receipts.record(
                path,
                size=after.st_size,
                mtime_ns=after.st_mtime_ns,
                start_line=1,
                end_line=written_lines,
                total_lines=written_lines,
                session=arguments.get("_session_id"),
            )

        action = "Updated" if existed else "Created"
        logger.debug("write: %s %s (%d chars)", action, path, len(content))
        return ToolResult(
            name="write",
            content=f"{action} {path} ({len(content)} characters)",
        )
