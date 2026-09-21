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

Issue #1514 (2026-09-21): `list` prints the full text for every rant that is not
`completed`, plus for every rant a `status=` filter selected, and prints one line where a
completed body was withheld. The rows still list every rant, so the queue is whole and
`cleanup` (which decides by recency and status, i.e. by the row) is unchanged;
`status="completed"` returns the history whole, so nothing this action printed before
becomes unreachable. The read used to cost the whole queue on every call — ~52 K
characters on the queue measured in the issue — because no task template passes a filter
on the axis the cost is on.
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


def _indented(message: str, indent: str = "    ") -> str:
    """`message` with every line indented, so it reads as belonging to its header line.

    The line breaks are kept rather than flattened: a rant body is written as prose with
    its own structure (the host's own words in quotes, then the demand), and a summary of
    it is what this tool used to return — 100 characters of 3504 on the rant measured
    2026-09-17. An **empty line stays empty** (no indent, so no trailing whitespace): it
    cannot run into the next rant's header, which starts at column 0 with a timestamp, and
    a reader who treats a blank line as the end of a paragraph inside a block is reading it
    the way the rant was written.
    """
    return "\n".join(indent + line if line else indent.rstrip() for line in message.splitlines())


#: How much of a one-line field the **header** may carry, in characters. The header is the
#: scan view — the row a caller looks down to find a timestamp — so it is bounded, and the
#: full text follows as a block below it. Measured 2026-09-17: with `progress` printed in
#: full the header of the one rant in flight was **1743** characters, i.e. the scan view was
#: the longest line in the output and the thing it was supposed to be a view of.
_HEADER_EXCERPT = 100


def _excerpt(text: str, limit: int = _HEADER_EXCERPT) -> str:
    """`text`, cut to `limit` characters and **marked** with `…` when anything was removed.

    The marker is the half that carries the information: a caller that cannot see the marker
    cannot tell an excerpt from the whole field, which is the silent half of the defect this
    action was fixed for. Both arms are asserted where this is used.
    """
    return text[:limit] + ("…" if len(text) > limit else "")


#: The status whose text is history rather than work, and therefore the one whose body the
#: default read withholds (issue #1514). A completed rant's body is provenance: `cleanup`
#: keeps it by recency and status — i.e. by its row — `promote` dedups against what is
#: *queued* (pending / in_progress), and no task template reads a completed body to decide
#: anything. Measured 2026-09-21 on the host that filed the issue: 11 of 13 rants were
#: completed, five of them carrying 3.5–5 K-character messages, and the read cost ~52 K
#: characters (~13 K tokens) per call — re-paid two or three times a cycle by the curation
#: flow, on a queue whose actionable part was a small fraction of it.
_HISTORY_STATUS = "completed"

#: What a withheld body says instead. It is the same half of the contract that `_excerpt`'s
#: `…` is: a reader who cannot tell "this rant has no message" from "this rant's message was
#: withheld" cannot make the one call that returns it — which is how the earlier truncation
#: stayed invisible for as long as it did. It names the remedy, not just the state.
_WITHHELD_NOTE = (
    '    (completed — text withheld as history; `action="list", status="completed"` '
    "returns it)"
)


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
                "every rant gets a row carrying timestamp/project/status/a "
                "progress excerpt/completed and a message excerpt — the scan "
                "view — and the full message and progress follow as indented "
                "blocks under it, **except on a row whose status is completed**: "
                "that text is history, so it is withheld and replaced by a line "
                "saying so. Pass `status=\"completed\"` to read it back whole, or "
                "any `status=` to see the selected slice in full. It is the read "
                "path the task templates point at, so it has to carry the text "
                "they point it at for). "
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
        # The message is the rant: `[…][:100]` used to be all of it that this action
        # showed, and that was measured to be 100 of 3504 characters on the one rant in
        # flight when it was looked at (2026-09-17) — 97% of the feedback dropped by the
        # very path the task templates now route every read through (`paper_prompt.md`
        # says to check the queue with `submit_rant(action="list")` and that there is no
        # reason to open the file at all, "not even to read it"). A read path that cannot
        # deliver the text is not a read path; the header line stays for scanning, and the
        # message follows it whole — a cap here would be the same defect with a larger
        # number in it, since nothing else can hand the caller the rest.
        #
        # Measured cost on the same queue (11 rants, cleanup caps it at 10 completed plus
        # the pending/in-progress ones; messages 3504 / 4031 / 3594 … chars): the whole
        # queue went 15300 → 42725 characters. The filters were named as the way to narrow
        # it, and the second half of that never happened: no task template passes a filter
        # except promote's `project=`, and `project=` is **orthogonal to the axis the cost
        # is on**. Measured 2026-09-21 (issue #1514): `list(project="emrg")` on the
        # evolution host returns 9 rants, 9/9 completed — 100% history with the work at
        # nil; over one session's whole history the calls averaged 17.4 K characters and
        # were 19.4% of every tool-result character it ever saw.
        #
        # So the scope is the fix, and a scope is not a cap. **The full text is printed for
        # every rant that is not `completed`, and for every rant a `status=` filter
        # selected** — a caller naming the slice is the narrowing mechanism this comment
        # always nominated, while "the rows narrow it" was never true of any caller. Every
        # row still lists every rant, completed ones included (the row is the queue, and
        # `cleanup` decides by recency and status, i.e. by the row), and
        # `status="completed"` returns the withheld history whole — so nothing this action
        # ever printed becomes unreachable, which is the property the refusal above was
        # protecting. A withheld body says so and names that call, because an absent block
        # that looks like an empty message is the silent half of the 2026-09-17 defect.
        #
        # `progress` is bounded in the header too, and for the same reason it is *not*
        # dropped there: it is a one-line field of the row (the prompt curates with it), but
        # printed whole it **was** the row — 1651 of that 1743-character header on the rant
        # in flight. So the row keeps an excerpt, and the whole value follows in a `progress:`
        # block after the message, on the same rule the message follows — and it is withheld
        # with the message on a completed row, since a completed rant's progress is the
        # longer of the two fields (measured: 5008 characters on one of the queue's
        # completed rants, against a 739-character message).
        status_selected = status is not None
        lines = []
        for r in rants:
            message = (r.get("message") or "").strip()
            progress = (r.get("progress") or "").strip()
            summary = _excerpt(" ".join(message.split()))
            progress_row = _excerpt(" ".join(progress.split()))
            lines.append(
                f"{r.get('timestamp')} | {r.get('project')} | "
                f"status={r.get('status')} | progress={progress_row} | "
                f"completed={r.get('completed')} | {summary}"
            )
            if not status_selected and (r.get("status") or "") == _HISTORY_STATUS:
                lines.append(_WITHHELD_NOTE)
                continue
            lines.append(
                _indented(message) if message
                else "    (this rant has no message)"
            )
            if progress:
                lines.append("    progress:")
                lines.append(_indented(progress, "      "))
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
