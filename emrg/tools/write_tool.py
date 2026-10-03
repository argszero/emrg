"""Write tool — create or overwrite a file."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from emrg.sandbox.fence import file_refusal
from emrg.sandbox.policy import resolve_policy
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor, resolve_tool_path
from emrg.tools.file_policy import resolve_file_target

logger = logging.getLogger(__name__)

MAX_WRITE_SIZE = 10 * 1024 * 1024  # 10 MB safety limit


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
        path, refusal = resolve_tool_path(target)
        if refusal:
            return ToolResult(name="write", content=refusal, error=True)

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
        )
        reason = file_refusal(target, policy)
        if reason:
            return ToolResult(name="write", content=reason, error=True)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return ToolResult(name="write", content=f"Error creating directory: {e}", error=True)

        try:
            existed = path.exists()
        except OSError:
            return ToolResult(
                name="write",
                content=f"Error: cannot write {path}: permission denied",
                error=True,
            )
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            return ToolResult(name="write", content=f"Error writing file: {e}", error=True)

        action = "Updated" if existed else "Created"
        logger.debug("write: %s %s (%d chars)", action, path, len(content))
        return ToolResult(
            name="write",
            content=f"{action} {path} ({len(content)} characters)",
        )
