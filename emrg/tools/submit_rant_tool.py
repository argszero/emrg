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

Issue #1514 (2026-09-21): `list` withheld the body of every `completed` rant and printed
one line in its place. The rows still list every rant, so the queue is whole and `cleanup`
(which decides by recency and status, i.e. by the row) is unchanged; `status="completed"`
returns the history whole, so nothing this action printed before became unreachable.

Rant 2026-10-15T12:35:03 item (b) (host decision 12:34:24, 「就定 a + b（c 去掉）」): the
same reasoning, one step further — **an unfiltered `list` is a scan, so it is scan rows
only**, and the full text follows a *narrowing* the caller actually chose: `status=`, or
`full=True`. #1514 fixed the history half of the cost and left the work half in place,
because it reasoned that a caller reading the queue must have the text; that is true of a
caller that **named the slice**, and false of the curation call, which decides which rants
to act on from the row. Measured on the live host queue the same day (50 rants, 2 pending,
48 completed): an unfiltered `list` returned **26,208 characters (~7,280 tokens)** of 50
rows — 48 of them withheld — and `list(status="pending")`, the slice the curation flow
actually reads, returned **6,431**. A cost re-paid every round the queue is in history. The
read path stays whole: every withheld row names the call that returns its text, and
`full=True` returns the queue exactly as #1514 left it.
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
from emrg.tools.base import ToolExecutor, non_regular_kind

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
_WITHHELD_NOTE = "    (text withheld — {why}; {remedy} returns it)"

#: The closing line of a scan-only read. A caller that asked for the queue and got rows has
#: to be able to tell "this is the scan view, the text is one call away" from "the queue
#: holds nothing but these rows" — the silent half of the 2026-09-17 defect, in its new
#: shape. Printed only when at least one body was actually withheld.
_SCAN_ONLY_NOTE = (
    "(scan view — {n} row(s), text withheld; `full=True`, or `status=` to name the slice "
    "you are acting on, returns the text)"
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
                "**action=list**: list rants (optional status/project/full "
                "filters). Every rant gets a row carrying timestamp/project/"
                "status/a progress excerpt/completed and a message excerpt — "
                "the scan view, and the default for an unfiltered read, "
                "because the queue is scanned to decide *which* rant to act "
                "on. The full message and progress follow as indented blocks "
                "under the row only when the caller named the slice it is "
                "acting on — `status=`, or `full=True`; otherwise the row "
                "carries a line naming the call that returns its text, so "
                "nothing this action ever printed is unreachable. It is the "
                "read path the task templates point at, so it has to carry "
                "the text they point it at for — on the call that asks. "
                "**action=update**: update a rant by its timestamp (status "
                "follows the pending→in_progress→completed state machine, no "
                "skipping; completed timestamp auto-written, and any explicit "
                "one must be at or after the rant's own submission instant and "
                "accompany status='completed'). "
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
                            "status filter — and naming it is what asks for "
                            "that slice's full text, so `status=\"pending\"` "
                            "is how a cycle reads the work in flight, and "
                            "`status=\"completed\"` how it reads history."
                        ),
                    },
                    "full": {
                        "type": "boolean",
                        "description": (
                            "For list: print every selected rant's full "
                            "message and progress, not just its scan row. "
                            "Use it when you have decided to act on a rant "
                            "and need its whole text; an unfiltered read "
                            "without it is the scan view, which costs rows "
                            "only (rant 2026-10-15T12:35:03 item (b))."
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
                            "For update: optional explicit completed ISO instant, "
                            "at or after the rant's own submission timestamp (a "
                            "stamp that does not follow it, or that cannot be "
                            "ordered against it, is refused and nothing is "
                            "written), and written only together with "
                            "status='completed'. Normally auto-written when "
                            "status becomes completed."
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

        The subject's kind is asked before it is opened: `exists()` is true for a FIFO, and
        `read_text` on one blocks at the open and raises nothing, so the
        ``except Exception`` below cannot bound the consequence — this is a tool, so the
        block is the daemon's event loop. Advisory reads degrade rather than refuse, which is
        this function's existing contract with a missing file.
        """
        try:
            import yaml

            p = self._rants_log().parent / "projects.yml"
            try:
                kind = non_regular_kind(p)
            except OSError:
                return []  # absent (or unstattable) — "no candidates", as before
            if kind:
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
        full = arguments.get("full")
        if isinstance(full, str):
            full = full.strip().lower() in ("1", "true", "yes", "y", "on")
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
        # The message is the rant, and the row is the scan view of it. `[…][:100]` used to
        # be all this action showed, and that was measured to be 100 of 3504 characters on
        # the one rant in flight (2026-09-17) — 97% of the feedback dropped by the very path
        # the task templates route every read through (`paper_prompt.md` says to check the
        # queue with `submit_rant(action="list")` and that there is no reason to open the
        # file at all, "not even to read it"). A read path that cannot deliver the text is
        # not a read path — so the text is always *reachable*, and what changed since is
        # **which call pays for it**.
        #
        # Issue #1514 (2026-09-21) fixed the history half: a `completed` rant's body is
        # provenance, so it was withheld while its row stayed. The work half stayed in place
        # — every pending / in_progress rant printed its whole message on every call,
        # whatever the caller asked for. Measured on the live host queue 2026-10-15 (50
        # rants, 2 pending, 48 completed): an unfiltered `list` returned **26,208 characters
        # (~7,280 tokens)**, and `list(status="pending")` — the slice the curation flow
        # decides on — returned **6,431**. The difference is bodies nobody asked for, re-paid
        # in the transcript every round after.
        #
        # So the scope of the full text is a **narrowing the caller named**: `status=` (the
        # host's own words, rant 2026-10-15T12:35:03 item (b): 「正文只在按 status 过滤或显式
        # 要求时给」 — the text comes when the caller filters by status or asks explicitly),
        # or `full=True`. `project=` deliberately does **not** unlock it: it is the dedup
        # filter `promote_prompt.md` uses, and on the queue measured 2026-10-15 it selects
        # 48 of 50 rows — it narrows the *subject*, not the cost. Every withheld row names
        # the call that returns its body, and the read closes with a line saying it is a scan
        # view at all: an absent block that reads as an empty message is the silent half of
        # the 2026-09-17 defect, and an absent *whole* block that reads as "the queue holds
        # nothing else" is its new shape.
        #
        # `progress` is bounded in the row too, and for the same reason it is *not* dropped
        # there: it is a one-line field of the scan view (the prompt curates with it), but
        # printed whole it **was** the row — 1651 of a 1743-character header on the rant in
        # flight. So the row keeps an excerpt, and the whole value follows in a `progress:`
        # block wherever the message follows.
        status_selected = status is not None
        text_selected = status_selected or bool(full)
        lines = []
        withheld = 0
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
            if not text_selected:
                withheld += 1
                history = (r.get("status") or "") == _HISTORY_STATUS
                lines.append(_WITHHELD_NOTE.format(
                    why="history, not work" if history else "this is the scan view",
                    remedy=(
                        'status="completed"' if history
                        else f'status="{r.get("status")}"'
                    ),
                ))
                continue
            lines.append(
                _indented(message) if message
                else "    (this rant has no message)"
            )
            if progress:
                lines.append("    progress:")
                lines.append(_indented(progress, "      "))
        content = f"{len(rants)} rant(s):\n" + "\n".join(lines)
        if withheld:
            content += "\n" + _SCAN_ONLY_NOTE.format(n=withheld)
        return ToolResult(name="submit_rant", content=content)

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
