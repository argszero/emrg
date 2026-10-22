"""BossTool — the task's owner, in a session of its own, deciding the stance.

Issue #2467. Every scheduled task is one agent that decides the direction and does
the work in the same round, and those two jobs pull against each other: the round
that must finish something now is the worst judge of whether it should be doing it
at all. Two ways of separating them were tried and both failed. Baking review
semantics into the generic ``subagent`` schema was refused — a general tool with a
special purpose is no longer general. Writing "call a subagent first and brief it
thus" into the templates ran at 16% → 56% → 31% of rounds, so the wording is not
the lever, and a parent-written brief is structurally wrong anyway: a brief is a
summary, and the position the owner exists to supply is what a summary drops.

So the owner is a tool of its own, with three properties that make it one:

**It reads the mission itself.** The child's cwd is
``<parent session dir>/sub-agent/sub-boss``, so the parent's own session directory
is two levels up and its ``history.jsonl`` is right there — the last ``user``
record is the entire task statement, ``extra_prompt`` included. The parent supplies
no text: ``focus`` is an optional *question*, never the brief. Measured on the
evolution task, that statement runs to tens of thousands of characters, and every
one of them is the position this role needs.

**It is not cleared between rounds.** The nine ``subagent`` slots are
least-recently-closed and emptied on use, which is right for a job that ends when
it answers. This one is a role held across rounds: emptied, it would re-read the
whole mission every round — paying for it again, and forgetting what it saw last
round. So it gets one fixed directory, ``sub-boss``, created on first use and
reused; it disappears with the parent's ``sub-agent/`` like anything else there.

**It is offered to scheduled tasks and not to host conversations.** That is not
decided here and must not be: the tool is registered once, and
``scheduled_task_tool_names()`` in the daemon is the only list that names it. A
role inside the tool asking "who called me?" would be a second mechanism for a
fact the producer already knows.

The bound (``timeout_s``, default as ``subagent``'s) covers the whole call, and a
timed-out call leaves nothing occupied, because there is no slot to occupy. The
depth limit is the delegation depth every child session carries: an owner that
delegates forever is bounded by the same rule as anything else it starts.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

#: The runner the daemon supplies: ``await runner(parent_session_id=…,
#: parent_cwd=…, focus=…, timeout_s=…, intent=…, parent_tools=…)`` answering
#: either ``{"text": …}`` or ``{"error": …}``. ``parent_tools`` is the parent
#: turn's effective tool set (#2465), which the child inherits.
Runner = Callable[..., Awaitable[dict]]

#: Generous enough for a real read of the parent's history, short enough that a
#: wedged owner session cannot hold the parent's round for ever.
DEFAULT_TIMEOUT_S = 600


class BossTool(ToolExecutor):
    """Ask this task's owner session what the coming rounds' stance is."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="boss",
            description=(
                "Ask the owner of this task what the coming rounds should do. The "
                "owner is a separate session that holds the direction while this "
                "session does the work: it reads this session's own history for "
                "the mission and for what has happened, so you do not describe "
                "either — and what you get back is a stance to treat as binding "
                "for this round, not a list of problems to consider. Anything the "
                "answer does not mention is still yours to decide. It is offered "
                "to scheduled tasks only, keeps its memory across rounds, and its "
                "round stands still until it answers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "description": (
                            "Optional. The one thing you want the owner's "
                            "judgement on — a question, not a summary of the work: "
                            "it already reads the history. Omit it and the owner "
                            "states its own stance."
                        ),
                    },
                    "timeout_s": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Whole seconds the owner may run — at least 1, and a "
                            "value outside that is refused. When it is reached the "
                            "owner's turn is cancelled and an error is returned. "
                            f"Default {DEFAULT_TIMEOUT_S}."
                        ),
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "One sentence on why the owner is being asked and what "
                            "the round needs from it; it is logged with the call."
                        ),
                    },
                },
                "required": ["intent"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        intent = arguments.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            return ToolResult(
                name="boss",
                content=(
                    "boss: `intent` is required and must not be empty — it is what "
                    "the log keeps of this call, and every tool call carries one."
                ),
                error=True,
            )

        focus = arguments.get("focus")
        if focus is not None and not isinstance(focus, str):
            return ToolResult(
                name="boss",
                content=(
                    f"boss: `focus` must be a string, so got {focus!r}. It is an "
                    "optional question, so leaving it out is how a caller states "
                    "'no particular question'."
                ),
                error=True,
            )

        timeout_s = arguments.get("timeout_s")
        if timeout_s is None:
            timeout_s = DEFAULT_TIMEOUT_S
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int):
            return ToolResult(
                name="boss",
                content=(
                    f"boss: `timeout_s` must be a whole number of seconds, so got "
                    f"{timeout_s!r}. It is the bound on the owner's run, and a "
                    "value from outside that domain is a caller error, not a "
                    "default."
                ),
                error=True,
            )
        if timeout_s < 1:
            return ToolResult(
                name="boss",
                content=(
                    f"boss: `timeout_s` must be at least 1, so got {timeout_s}. A "
                    "non-positive bound would cancel the owner before its first "
                    "round."
                ),
                error=True,
            )

        parent_session_id = arguments.get("_parent_session_id") or ""
        parent_cwd = arguments.get("_parent_cwd") or ""
        if not parent_session_id or not parent_cwd:
            return ToolResult(
                name="boss",
                content=(
                    "boss: this call carries no parent session, so there is no "
                    "task for an owner to own. The daemon injects the parent's "
                    "session and cwd into every call it dispatches; a call without "
                    "them did not come from a daemon turn."
                ),
                error=True,
            )

        # The parent turn's tool set, injected by the daemon (#2465). A value that
        # is not a list is "no statement", the same meaning the message field
        # carries, so silence cannot be read two ways.
        # The tier this turn is running under, injected by the daemon. A value that
        # is not a non-empty string is "no statement": silence means the turn itself
        # declared no tier, which the daemon reads as unconfined.
        raw_parent_sandbox = arguments.get("_parent_sandbox")
        parent_sandbox = (
            raw_parent_sandbox
            if isinstance(raw_parent_sandbox, str) and raw_parent_sandbox else None
        )

        raw_parent_tools = arguments.get("_parent_tools")
        parent_tools = (
            [str(name) for name in raw_parent_tools]
            if isinstance(raw_parent_tools, list) else None
        )

        outcome = await self._runner(
            parent_session_id=str(parent_session_id),
            parent_cwd=str(parent_cwd),
            focus=(focus or ""),
            timeout_s=int(timeout_s),
            intent=intent,
            parent_tools=parent_tools,
            parent_sandbox=parent_sandbox,
        )
        if outcome.get("error"):
            return ToolResult(name="boss", content=outcome["error"], error=True)
        return ToolResult(name="boss", content=outcome.get("text", ""))
