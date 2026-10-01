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

        # Read the file's own bytes, with no newline translation (issue #1803). The
        # match runs against the *view* the read tool shows the caller — every break
        # reads as "\n", so an ``old_string`` copied from a read matches a CRLF file —
        # and the splice puts the bytes back, so the file's own endings survive and
        # only the region ``old_string`` named changes. ``emrg/tools/newlines.py``
        # carries the measurement, the rule, and what a newly written line joins.
        #
        # What this replaced, measured 2026-10-02 on `bc114ab9`: the version that
        # landed with #1804 decided the endings with a per-file boolean, so (a) a file
        # with *mixed* endings was still rewritten LF-only — issue #1803's acceptance
        # item 1, a mixed file's untouched lines keeping their terminators byte for
        # byte — and (b) ``new_string`` was never read the way the file is read, so a
        # replacement carrying "\r\n" came out as "\r\r\n", a lone CR this tool never
        # produced before that commit.
        try:
            raw = path.open("r", encoding="utf-8", newline="").read()
        except UnicodeDecodeError:
            return ToolResult(
                name="edit", content=f"Error: cannot read {path} as text", error=True
            )

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
            with path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(new_content)
        except OSError as e:
            return ToolResult(
                name="edit", content=f"Error writing file: {e}", error=True
            )

        desc = f"{count} replacements" if replace_all else "1 replacement"
        return ToolResult(name="edit", content=f"Made {desc} in {path}")
