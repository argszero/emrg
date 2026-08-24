"""SubmitRantTool — unified rant management tool for rants.jsonl.

Rant 2026-08-17T11:51:59: rants are not a special mode — they are part of
normal conversation. The agent auto-detects rant intent, clarifies/polishes
with the user, and only calls this tool after the user has explicitly agreed.
The daemon ``rant`` command (TUI /rant, GUI rant panel) and this tool share
:func:`emrg.server.rants.append_rant`, so behavior stays identical.

Rant 2026-08-18T16:42:52: extended into a unified rant-management tool with
four actions (submit / list / update / cleanup) so the evolution loop curates
rants.jsonl exclusively through this tool — never with hand-written
bash/python rewrites (the 2026-08-18 incident: format drift to array rows,
field loss, history pruning).
"""

from __future__ import annotations

from pathlib import Path

from emrg.server.rants import (
    append_rant,
    cleanup_rants,
    list_rants,
    update_rant,
)
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

_ACTIONS = ("submit", "list", "update", "cleanup")


class SubmitRantTool(ToolExecutor):
    """Submit a user-confirmed rant / list / update / cleanup rants.jsonl.

    IMPORTANT (submit): only call after the user has explicitly agreed to
    submit (show the polished text first, ask for confirmation, then call).
    Never call on an unconfirmed complaint.

    Curation (list / update / cleanup) is the ONLY supported way to touch
    rants.jsonl — see rant 2026-08-18T16:42:52.
    """

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="submit_rant",
            description=(
                "Unified rant-management tool for rants.jsonl. "
                "**action=submit**: write a user rant/feedback/improvement "
                "suggestion into rants.jsonl so the evolution system can act "
                "on it. Must obtain explicit user consent before calling — "
                "clarify the target and polish the text, show the user the "
                "result, and only then call. "
                "**action=list**: list rants (optional status/project filters; "
                "returns timestamp/project/status/progress/completed + message "
                "summary). "
                "**action=update**: update a rant by its timestamp (status "
                "follows the pending→in_progress→completed state machine, no "
                "skipping; completed timestamp auto-written). "
                "**action=cleanup**: keep all pending/in_progress rants plus "
                "the 10 most recent completed, prune older completed. "
                "All read/write of rants.jsonl MUST go through this tool — "
                "never rewrite the file with hand-written bash/python."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["submit", "list", "update", "cleanup"],
                        "description": (
                            "Which action to perform. Default: submit."
                        ),
                    },
                    "project": {
                        "type": "string",
                        "description": (
                            "REQUIRED for submit — target project name = the "
                            "short `name` registered in ~/.emrg/projects.yml "
                            "(e.g. 'emrg', 'aitokenpool') — NOT the GitHub "
                            "owner/repo form (rant 2026-08-24T10:54:04). If "
                            "you cannot determine which project the rant "
                            "targets, ask the user before calling. Optional "
                            "filter for list."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "REQUIRED for submit — the rant body (polished, "
                            "complete description of the feedback/suggestion/"
                            "bug report)."
                        ),
                    },
                    "timestamp": {
                        "type": "string",
                        "description": (
                            "REQUIRED for update — the rant's timestamp "
                            "(unique identifier, as shown by list)."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                        "description": (
                            "For update: new status (pending→in_progress→"
                            "completed, no skipping). For list: optional "
                            "status filter."
                        ),
                    },
                    "progress": {
                        "type": "string",
                        "description": (
                            "For update: progress description string "
                            "(e.g. 'PR #123 submitted, awaiting review')."
                        ),
                    },
                    "completed": {
                        "type": "string",
                        "description": (
                            "For update: optional explicit completed ISO "
                            "timestamp (normally auto-written when status "
                            "becomes completed)."
                        ),
                    },
                    "intent": {
                        "type": "string",
                        "description": "The purpose of this call: why you are invoking it and what you want to achieve. "
                        "One human-readable sentence, e.g. 'record the host's feedback about the tool log'.",
                    },
                },
                "required": ["action", "intent"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        action = str(arguments.get("action") or "submit").strip().lower()
        if action not in _ACTIONS:
            return ToolResult(
                name="submit_rant",
                content=(
                    f"Error: unknown action {action!r} — must be one of "
                    f"{', '.join(_ACTIONS)}"
                ),
                error=True,
            )
        if action == "submit":
            return self._execute_submit(arguments)
        if action == "list":
            return self._execute_list(arguments)
        if action == "update":
            return self._execute_update(arguments)
        return self._execute_cleanup(arguments)

    def _rants_log(self) -> Path:
        from emrg.config import config_dir
        return config_dir() / "rants.jsonl"

    def _execute_submit(self, arguments: dict) -> ToolResult:
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
            count = append_rant(self._rants_log(), message, project)
        except Exception as e:  # noqa: BLE001 — tool errors must never crash the loop
            return ToolResult(
                name="submit_rant",
                content=f"Error: failed to record rant: {e}",
                error=True,
            )
        target = f" ({project})" if project else ""
        # Rant 2026-08-24T10:54:04: project must be the short name registered
        # in ~/.emrg/projects.yml — not the GitHub owner/repo form. When the
        # given name is not registered, append a non-blocking warning with the
        # registered candidates so a copy-pasted wrong name is caught at
        # submit time instead of requiring a manual re-submit + void.
        registered = self._registered_project_names()
        if registered and project not in registered:
            candidates = " | ".join(sorted(registered))
            warning = (
                f"\n⚠ project {project!r} is not a registered name in "
                f"~/.emrg/projects.yml (registered: {candidates}) — use the "
                f"short `name` (e.g. 'emrg' or 'aitokenpool')."
            )
        else:
            warning = ""
        return ToolResult(
            name="submit_rant",
            content=f"Rant recorded{target}. Total rants: {count}.{warning}",
        )

    def _registered_project_names(self) -> list[str]:
        """Short names registered in ~/.emrg/projects.yml (config dir).

        The daemon registers projects there on first use; the list is the
        authoritative source for the rant ``project`` field. Any read error
        degrades to "no candidates" (validation is advisory, never blocking).
        """
        try:
            import yaml

            p = self._rants_log().parent / "projects.yml"
            if not p.exists():
                return []
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [
                    str(e.get("name", ""))
                    for e in data
                    if isinstance(e, dict) and e.get("name")
                ]
        except Exception:  # noqa: BLE001 — advisory only
            return []
        return []

    def _execute_list(self, arguments: dict) -> ToolResult:
        status = arguments.get("status")
        project = arguments.get("project")
        if status is not None:
            status = str(status).strip() or None
        if project is not None:
            project = str(project).strip() or None
        try:
            rants = list_rants(self._rants_log(), status=status, project=project)
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                name="submit_rant",
                content=f"Error: failed to list rants: {e}",
                error=True,
            )
        if not rants:
            return ToolResult(
                name="submit_rant",
                content=(
                    "No rants match."
                    + (f" (status={status})" if status else "")
                    + (f" (project={project})" if project else "")
                ),
            )
        lines = []
        for r in rants:
            summary = (r.get("message") or "").replace("\n", " ").strip()[:100]
            lines.append(
                f"{r.get('timestamp')} | {r.get('project')} | "
                f"status={r.get('status')} | progress={r.get('progress')} | "
                f"completed={r.get('completed')} | {summary}"
            )
        return ToolResult(
            name="submit_rant",
            content=f"{len(rants)} rant(s):\n" + "\n".join(lines),
        )

    def _execute_update(self, arguments: dict) -> ToolResult:
        timestamp = str(arguments.get("timestamp", "") or "").strip()
        if not timestamp:
            return ToolResult(
                name="submit_rant",
                content=(
                    "Error: update requires timestamp (the rant's unique "
                    "identifier — run action=list to find it)"
                ),
                error=True,
            )
        status = arguments.get("status")
        if status is not None:
            status = str(status).strip() or None
        progress = arguments.get("progress")
        completed = arguments.get("completed")
        try:
            ok, msg = update_rant(
                self._rants_log(),
                timestamp,
                status=status,
                progress=progress,
                completed=completed,
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                name="submit_rant",
                content=f"Error: failed to update rant: {e}",
                error=True,
            )
        if not ok:
            return ToolResult(
                name="submit_rant",
                content=f"Error: {msg}",
                error=True,
            )
        return ToolResult(name="submit_rant", content=msg)

    def _execute_cleanup(self, arguments: dict) -> ToolResult:
        try:
            count = cleanup_rants(self._rants_log())
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                name="submit_rant",
                content=f"Error: failed to clean up rants: {e}",
                error=True,
            )
        return ToolResult(
            name="submit_rant",
            content=f"Rant cleanup done — {count} entries kept "
            "(all pending/in_progress + 10 most recent completed).",
        )
