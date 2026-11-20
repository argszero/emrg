"""The two life tools — one every agent carries, one only the judge does.

An agent's life value lives on its row in the agent register, and these are the
only two ways a turn touches it. They are deliberately asymmetric:

* ``get_agent_life`` reads any agent's value and is offered to everybody. Reading
  is not a privilege here — the register is a file every session can already open,
  so a tool that pretended otherwise would be a lock on an open door.
* ``set_agent_life`` writes and is offered to **one** agent. That restriction is
  not made inside this class, and cannot be: a tool cannot see who called it. It
  is made where a turn's tool list is decided (``emrg/server/daemon.py``), so the
  judge's turns carry the name and every other turn does not — which refuses a
  forged call as a name that turn does not hold, at the same point that would
  refuse a tool this build never registered.

That split is why the write's own validation is about the *value*, not about the
caller: the caller question is answered one layer up, once, for the schema and the
dispatch together.
"""

from __future__ import annotations

from pathlib import Path

from emrg.config import agents_file
from emrg.server.agents import AgentRegistry
from emrg.server.life import parse_instant, tier_of
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor


def _describe(row: dict) -> str:
    """One agent's life as a line of prose, for either tool's answer."""
    value = row.get("life")
    if not isinstance(value, int) or isinstance(value, bool):
        return f"{row.get('name')}: unjudged — no life value has been written yet."
    line = f"{row.get('name')}: {value} ({tier_of(value)})"
    when = parse_instant(row.get("life_at"))
    delta = row.get("life_delta")
    reason = row.get("life_reason")
    if when is not None:
        tail = f"last change {when.isoformat(timespec='seconds')}"
        if isinstance(delta, int) and not isinstance(delta, bool):
            tail += f", {delta:+d}"
        if isinstance(reason, str) and reason.strip():
            tail += f" — {reason.strip()}"
        line += f"; {tail}"
    return line + "."


class GetAgentLifeTool(ToolExecutor):
    """Read one agent's life value, its band and the last change behind it."""

    def __init__(self, register_path: Path | str | None = None) -> None:
        self._register_path = register_path

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_agent_life",
            description=(
                "Read an agent's life value from the agent register. Answers the "
                "value, which of the four bands it falls in (dying, surviving, "
                "healthy, thriving) and the last change's instant, size and reason. "
                "A row nobody has judged answers `unjudged` rather than a number, "
                "because zero and 'not looked at yet' are different states. Reading "
                "is open to every agent; writing is not."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "The agent's registered name, e.g. 'emrg'.",
                    },
                },
                "required": ["agent"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        name = arguments.get("agent")
        if not isinstance(name, str) or not name.strip():
            return ToolResult(
                content="get_agent_life needs `agent`: the registered name to read.",
                error=True,
            )
        name = name.strip()
        path = Path(self._register_path) if self._register_path else agents_file()
        row = next(
            (r for r in AgentRegistry(path).entries() if r.get("name") == name), None,
        )
        if row is None:
            return ToolResult(
                content=f"No agent is registered under the name {name!r}.",
                error=True,
            )
        return ToolResult(content=_describe(row))


class SetAgentLifeTool(ToolExecutor):
    """Write one agent's life value. Offered to the judge alone."""

    def __init__(self, register_path: Path | str | None = None) -> None:
        self._register_path = register_path

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="set_agent_life",
            description=(
                "Write an agent's life value into the agent register. The judge "
                "decides the number; this tool records it and derives the size of "
                "the change from the value already there, so the caller never states "
                "a delta. `reason` is what the judged agent will read in its next "
                "round, so it says why the number moved rather than restating it. "
                "A first judgement records no delta: a jump from nothing to something "
                "is not a change of nought."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "The agent's registered name, e.g. 'emrg'.",
                    },
                    "value": {
                        "type": "integer",
                        "description": (
                            "The new life value: an integer, higher is better, with "
                            "no upper bound."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why the value is what it is — a sentence the judged "
                            "agent reads in its next round."
                        ),
                    },
                },
                "required": ["agent", "value", "reason"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        name = arguments.get("agent")
        if not isinstance(name, str) or not name.strip():
            return ToolResult(
                content="set_agent_life needs `agent`: the registered name to write.",
                error=True,
            )
        value = arguments.get("value")
        # `isinstance(True, int)` is true, and a boolean is not a score: refuse it
        # rather than store 1 for a model that answered `true`.
        if not isinstance(value, int) or isinstance(value, bool):
            return ToolResult(
                content=(
                    f"set_agent_life needs `value` as an integer; it was "
                    f"{value!r} ({type(value).__name__})."
                ),
                error=True,
            )
        reason = arguments.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return ToolResult(
                content=(
                    "set_agent_life needs `reason`: the judged agent reads it in its "
                    "next round, so an empty one hides why the value moved."
                ),
                error=True,
            )
        path = Path(self._register_path) if self._register_path else agents_file()
        row = AgentRegistry(path).set_life(name.strip(), value, reason.strip())
        if row is None:
            return ToolResult(
                content=(
                    f"No life value was written: no agent is registered under "
                    f"{name.strip()!r}, or the register could not be read."
                ),
                error=True,
            )
        return ToolResult(content="Written. " + _describe(row))
