"""Edit tool — exact string replacement in an existing file."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from emrg.sandbox.fence import file_refusal
from emrg.sandbox.policy import resolve_policy
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import (
    ToolExecutor,
    boolean_argument,
    special_file_kind,
    special_file_refusal,
)
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
                            "If false (default), old_string must be unique. A value "
                            "that is not true or false (the strings 'true'/'false' "
                            "included) is refused rather than read as its opposite."
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
        # Refused rather than read by truthiness: `"false"` is a truthy string, and
        # this flag decides how many places the edit writes to (`boolean_argument`).
        replace_all, refusal = boolean_argument(arguments, "replace_all", default=False)
        if refusal is not None:
            return ToolResult(name="edit", content=refusal, error=True)

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
            extra_roots=arguments.get("writable_roots"),
        )
        reason = file_refusal(target, policy)
        if reason:
            return ToolResult(name="edit", content=reason, error=True)

        if not path.exists():
            return ToolResult(
                name="edit", content=f"Error: file not found: {path}", error=True
            )
        # A directory is one kind of subject `edit` cannot act on; a FIFO, a socket and a
        # device node are the others, and all of them *block* rather than fail — `open`
        # for read waits for a writer, `open` for write for a reader. The check used to
        # name the directory alone, so the rest reached `open` and never returned
        # (measured 2026-10-10, `cyc20261010-215146`: `edit` on a `mkfifo` named pipe did
        # not return within 10 s, in its own process, while a regular file's edit did).
        special = special_file_kind(path.stat().st_mode)
        if special is not None:
            return ToolResult(
                name="edit", content=special_file_refusal(path, special), error=True
            )

        logger.debug("edit: %s (replace_all=%s)", path, replace_all)

        # Read the file's own bytes, with no newline translation, because the write
        # below has to put the file's line endings back (issue #1803). The string the
        # match runs against is normalised the way the *read* tool shows a file — that
        # is the only view the caller has, so its ``old_string`` carries "\n" even when
        # the file on disk is CRLF — and reading with the default instead destroyed
        # exactly that: `read_text()` turned every `\r\n` into `\n` and `write_text()`
        # wrote the `\n` back out, so an edit naming one line changed every line of the
        # file. The class this costs: `.gitattributes` pins `*.cmd`/`*.bat`/`*.ps1` to
        # CRLF on every platform and LF-only `.cmd` files are the v0.2.25–v0.2.27
        # installer failure `tests/test_cmd_crlf.py` guards (rant 2026-08-12T12:30:41).
        # A file whose lines are *uniformly* CRLF gets them back; one that is LF takes
        # the same bytes it did before; a mixed file is written LF-only, which is what
        # this tool already did to it.
        try:
            raw = path.open("r", encoding="utf-8", newline="").read()
        except UnicodeDecodeError:
            return ToolResult(
                name="edit", content=f"Error: cannot read {path} as text", error=True
            )
        crlf_file = raw.count("\r\n") > 0 and raw.count("\r\n") == raw.count("\n")
        content = raw.replace("\r\n", "\n").replace("\r", "\n")

        count = content.count(old)
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

        new_content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        if crlf_file:
            new_content = new_content.replace("\n", "\r\n")
        try:
            with path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(new_content)
        except OSError as e:
            return ToolResult(
                name="edit", content=f"Error writing file: {e}", error=True
            )

        desc = f"{count} replacements" if replace_all else "1 replacement"
        return ToolResult(name="edit", content=f"Made {desc} in {path}")
