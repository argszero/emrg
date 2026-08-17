"""SubmitRantTool — write a user rant/feedback into rants.jsonl.

Rant 2026-08-17T11:51:59: rants are not a special mode — they are part of
normal conversation. The agent auto-detects rant intent, clarifies/polishes
with the user, and only calls this tool after the user has explicitly agreed.
The daemon ``rant`` command (TUI /rant, GUI rant panel) and this tool share
:func:`emrg.server.rants.append_rant`, so behavior stays identical.
"""

from __future__ import annotations

from emrg.server.rants import append_rant
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor


class SubmitRantTool(ToolExecutor):
    """Submit a user-confirmed rant/feedback into rants.jsonl for evolution.

    IMPORTANT: only call after the user has explicitly agreed to submit
    (show the polished text first, ask for confirmation, then call). Never
    call on an unconfirmed complaint.
    """

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="submit_rant",
            purpose="Write a user-confirmed rant/feedback into rants.jsonl for evolution",
            description=(
                "Write a user rant/feedback/improvement suggestion into "
                "rants.jsonl so the evolution system can act on it. "
                "**Must obtain explicit user consent before calling**: first "
                "clarify the target and polish the text, show the user the "
                "result, and only then call this tool."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": (
                            "REQUIRED — target project name (e.g. 'emrg', "
                            "'argszero/aitokenpool'). If you cannot determine "
                            "which project the rant targets, ask the user "
                            "before calling."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "The rant body — polished, complete description of "
                            "the feedback/suggestion/bug report."
                        ),
                    },
                },
                "required": ["project", "message"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        message = str(arguments.get("message", "")).strip()
        if not message:
            return ToolResult(
                name="submit_rant",
                content="Error: submit_rant requires a message",
                error=True,
            )
        project = str(arguments.get("project", "") or "").strip()
        if not project:
            return ToolResult(
                name="submit_rant",
                content=(
                    "Error: project is required — ask the user which project "
                    "this rant targets before submitting"
                ),
                error=True,
            )
        try:
            from emrg.config import config_dir
            count = append_rant(config_dir() / "rants.jsonl", message, project)
        except Exception as e:  # noqa: BLE001 — tool errors must never crash the loop
            return ToolResult(
                name="submit_rant",
                content=f"Error: failed to record rant: {e}",
                error=True,
            )
        target = f" ({project})" if project else ""
        return ToolResult(
            name="submit_rant",
            content=f"Rant recorded{target}. Total rants: {count}.",
        )
