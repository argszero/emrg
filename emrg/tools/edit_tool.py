"""Edit tool — exact string replacement in an existing file."""

from __future__ import annotations

import logging
from pathlib import Path

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

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
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        file_path = arguments.get("file_path", "")
        old = arguments.get("old_string", "")
        new = arguments.get("new_string", "")
        replace_all = arguments.get("replace_all", False)

        if not file_path:
            return ToolResult(name="edit", content="Error: no file_path provided")
        if not old:
            return ToolResult(name="edit", content="Error: old_string is empty")

        path = Path(file_path).expanduser().resolve()

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
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                name="edit", content=f"Error: cannot read {path} as text", error=True
            )

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
        path.write_text(new_content, encoding="utf-8")

        desc = f"{count} replacements" if replace_all else "1 replacement"
        return ToolResult(name="edit", content=f"Made {desc} in {path}")
