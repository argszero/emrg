"""SubagentTool — delegate one job to a child session and wait for its answer.

Issue #2451. Delegation existed only as a page of instructions in a user-level
skill: the model hand-assembled the ``auth``/``task``/``delta``/``done`` frames,
chose its own session id, asked the daemon whether that session was busy, and
decided for itself whether to delete and reuse a slot. A page of instructions is
not a capability — one wrong step costs a whole round, and the tool loop already
has everything needed to run a second session for one turn. This exposes it.

**Blocking on purpose.** The parent's round stands still while the child runs and
resumes when it finishes, times out or is cancelled. One child per round, no
parallelism: a fan-out would report back a pile of half-answers, which the tool
layer cannot merge and the parent has no budget to reconcile.

**What the parent gets back is the child's final answer**, plus its id, its cwd,
its round count and its elapsed time. None of the child's intermediate narration
travels: measured while a hand-rolled delegate was in use, the concatenated
running commentary was shrapnel, not a report.

The pieces a caller must not choose are injected by the daemon, not declared in
this schema: ``cwd`` and the slot are computed (`_parent_session_id` /
`_parent_cwd` ride the argument dict from ``_inject_tool_arguments``), because a
model that picks its own slot picks a wrong one. So this tool refuses a call that
arrives without them, which is what happens outside a daemon turn.

The slot path is the child's **project cwd**, not its own session directory — the
child's directory is built from its cwd like any other session's
(``<slot>/.emrg/sessions/<child id>``), which is how the child's memory lands
inside the parent's session directory without a second project root.

The rest of the mechanism — the nine slots, the least-recently-closed choice, the
depth limit, the sandbox inherited from the parent, and the close stamp taken
after the child's memory write — belongs to the daemon that owns the sessions and
is documented where it lives (``EmrgServer._run_subagent_tool``).
"""

from __future__ import annotations

from typing import Awaitable, Callable

from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

#: The runner the daemon supplies: ``await runner(parent_session_id=…,
#: parent_cwd=…, prompt=…, timeout_s=…, intent=…)`` answering either
#: ``{"text": …}`` or ``{"error": …}``.
Runner = Callable[..., Awaitable[dict]]

#: A default that is generous enough for a real job and short enough that a
#: wedged child cannot hold the parent's round for ever.
DEFAULT_TIMEOUT_S = 600


class SubagentTool(ToolExecutor):
    """Run one child session to completion and return its final answer."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="subagent",
            description=(
                "Delegate one self-contained job to a separate child session, "
                "and wait for it. Use it when the job is big enough that doing "
                "it here would flood this conversation with material you do not "
                "need afterwards — a wide search, a long reading, a bulk edit — "
                "and when the job can be stated completely in one prompt. "
                "The child gets its own session, its own context and its own "
                "tools; the parent's round stands still until the child "
                "finishes, times out or is cancelled, and only the child's "
                "FINAL answer comes back (its intermediate narration does not). "
                "The result carries the child's session id, its cwd, how many "
                "rounds it took and how long it ran. One child per call: this "
                "does not run children in parallel. The child inherits this "
                "session's sandbox tier, and a child may delegate again, up to a "
                "fixed depth. Write the prompt as a complete brief — the child "
                "does not see this conversation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The complete brief for the child: what to do, where "
                            "to work, and what to report back. The child cannot "
                            "see this conversation, so state the goal and the "
                            "constraints in full."
                        ),
                    },
                    "timeout_s": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Whole seconds the child may run — at least 1, and a "
                            "value outside that is refused. The bound covers the "
                            "whole call, so it is also how long this tool waits for "
                            "a free slot. When it is reached the child's turn is "
                            "cancelled and an error is returned. "
                            f"Default {DEFAULT_TIMEOUT_S}."
                        ),
                    },
                    "intent": {
                        "type": "string",
                        "description": (
                            "One sentence on why this delegation is made and what "
                            "it should reach; it is logged with the call."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return ToolResult(
                name="subagent",
                content=(
                    "subagent: `prompt` is required and must not be empty — it is "
                    "the child's whole brief, since the child cannot see this "
                    "conversation."
                ),
                error=True,
            )

        timeout_s = arguments.get("timeout_s")
        if timeout_s is None:
            timeout_s = DEFAULT_TIMEOUT_S
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int):
            return ToolResult(
                name="subagent",
                content=(
                    f"subagent: `timeout_s` must be a whole number of seconds, so "
                    f"got {timeout_s!r}. It is the bound on the child's run, and a "
                    "value from outside that domain is a caller error, not a "
                    "default."
                ),
                error=True,
            )
        if timeout_s < 1:
            return ToolResult(
                name="subagent",
                content=(
                    f"subagent: `timeout_s` must be at least 1, so got {timeout_s}. "
                    "A non-positive bound would cancel the child before its first "
                    "round."
                ),
                error=True,
            )

        parent_session_id = arguments.get("_parent_session_id") or ""
        parent_cwd = arguments.get("_parent_cwd") or ""
        if not parent_session_id or not parent_cwd:
            return ToolResult(
                name="subagent",
                content=(
                    "subagent: this call carries no parent session, so there is "
                    "nothing to delegate from. The daemon injects the parent's "
                    "session and cwd into every call it dispatches; a call without "
                    "them did not come from a daemon turn."
                ),
                error=True,
            )

        outcome = await self._runner(
            parent_session_id=str(parent_session_id),
            parent_cwd=str(parent_cwd),
            prompt=prompt,
            timeout_s=int(timeout_s),
            intent=str(arguments.get("intent") or ""),
        )
        if outcome.get("error"):
            return ToolResult(name="subagent", content=outcome["error"], error=True)
        return ToolResult(name="subagent", content=outcome.get("text", ""))
