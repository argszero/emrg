"""Edit tool — exact string replacement in an existing file."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from emrg.sandbox.fence import file_refusal
from emrg.sandbox.policy import resolve_policy
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools import newlines
from emrg.tools.base import ToolExecutor
from emrg.tools.file_policy import resolve_file_target

logger = logging.getLogger(__name__)


class EditTool(ToolExecutor):
    """Exact string replacement — find old_string, replace with new_string.

    The old_string must be unique in the file (found exactly once).
    This constraint prevents accidental multi-replacements and makes
    the LLM be precise about what it changes.
    """

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="edit",
            description=(
                "Replace old_string with new_string in an existing file. "
                "old_string must appear exactly once in the file — use the "
                "read tool first to see the exact content. "
                "The match is exact (whitespace, indentation, and newlines "
                "must all match precisely). "
                "Only the text old_string named is rewritten: the file's own "
                "line endings (LF or CRLF) are kept, so a file's terminator is "
                "never changed by an edit that did not ask for it. "
                "For multiple replacements, set replace_all to true. "
                "Prefer edit over write for modifying existing files — "
                "it is safer and displays a diff in the UI."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to edit.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to find and replace. Must match precisely including whitespace and indentation.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Text to replace old_string with.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": (
                            "If true, replace all occurrences of old_string. "
                            "If false (default), old_string must be unique."
                        ),
                    },
                    "intent": {
                        "type": "string",
                        "description": "The purpose of this call: why you are invoking it and what you want to achieve. "
                        "One human-readable sentence, e.g. 'fix the off-by-one in the retry loop'.",
                    },
                },
                "required": ["file_path", "old_string", "new_string", "intent"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        file_path = arguments.get("file_path", "")
        old = arguments.get("old_string", "")
        new = arguments.get("new_string", "")
        replace_all = arguments.get("replace_all", False)

        if not file_path:
            return ToolResult(name="edit", content="Error: no file_path provided", error=True)
        if not old:
            return ToolResult(name="edit", content="Error: old_string is empty", error=True)

        # Where the bytes actually go, decided in one place (issue #1558) — a
        # relative `file_path` is joined onto the injected workspace (the session
        # cwd), not resolved against the daemon's own cwd, which is the tree the
        # predicates do not judge.
        target = resolve_file_target(file_path, arguments.get("workspace"))
        path = Path(target).resolve()

        # One policy for both tools — the same call the write tool makes, so the
        # two cannot drift (host ruling 2026-09-28T21:50, issue #1553;
        # `emrg/sandbox/fence.py` carries the reading and its consequences).
        # The workspace boundary is injected by the daemon (session cwd); silence
        # resolves to `danger-full-access` and stays unconfined, as before.
        policy = resolve_policy(
            mode=arguments.get("sandbox"),
            workspace_root=str(arguments.get("workspace") or os.getcwd()),
            session_id=arguments.get("session_id"),
        )
        reason = file_refusal(target, policy)
        if reason:
            return ToolResult(name="edit", content=reason, error=True)

        if not path.exists():
            return ToolResult(
                name="edit", content=f"Error: file not found: {path}", error=True
            )
        if path.is_dir():
            return ToolResult(
                name="edit", content=f"Error: {path} is a directory", error=True
            )

        logger.debug("edit: %s (replace_all=%s)", path, replace_all)

        try:
            # Bytes, not `read_text`: the tool writes this file back, and a text-mode
            # read would hand the writer a normalised string with the file's own line
            # endings already gone (issue #1803 — an edit to one word of a CRLF file
            # rewrote every line of it; `emrg/tools/newlines.py` carries the measurement
            # and the rule).
            raw = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                name="edit", content=f"Error: cannot read {path} as text", error=True
            )
        except OSError as e:
            return ToolResult(name="edit", content=f"Error reading file: {e}", error=True)

        new_content, count = newlines.replace(raw, old, new, replace_all=replace_all)
        if count == 0:
            return ToolResult(
                name="edit",
                content=(
                    f"Error: old_string not found in {path}. "
                    f"Use the read tool to verify the exact file content."
                ),
                error=True,
            )

        if not replace_all and count > 1:
            return ToolResult(
                name="edit",
                content=(
                    f"Error: old_string found {count} times in {path}. "
                    f"Either make it more specific (include surrounding context) "
                    f"or set replace_all=true."
                ),
                error=True,
            )

        try:
            path.write_bytes(new_content.encode("utf-8"))
        except OSError as e:
            return ToolResult(
                name="edit", content=f"Error writing file: {e}", error=True
            )

        desc = f"{count} replacements" if replace_all else "1 replacement"
        return ToolResult(name="edit", content=f"Made {desc} in {path}")
