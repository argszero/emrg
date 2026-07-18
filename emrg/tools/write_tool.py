"""Write tool — create or overwrite a file."""

from __future__ import annotations

import logging
from pathlib import Path

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

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
                },
                "required": ["file_path", "content"],
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

        path = Path(file_path).expanduser().resolve()

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return ToolResult(name="write", content=f"Error creating directory: {e}", error=True)

        existed = path.exists()
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
