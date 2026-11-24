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
from emrg.server.life import item_lines, parse_instant, tier_of, total_of
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
    items = item_lines(row.get("life_items"))
    if items:
        line += ". What it was made of: " + "; ".join(items)
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
                "Write an agent's life judgement into the agent register. A judgement "
                "is a list of `items` — each pairing a signed weight with what it was "
                "for — and the stored value is derived from that list: the caller never "
                "states a total, so there is no second claim about the same fact to "
                "disagree with the items. Negative items are what the judged agent "
                "should stop doing, and a judgement that finds none says what it read "
                "while looking (`searched`) instead of reporting silence as a clean "
                "bill. `reason` is the one line the judged agent reads in its next "
                "round. A first judgement records no delta: a jump from nothing to "
                "something is not a change of nought."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "The agent's registered name, e.g. 'emrg'.",
                    },
                    "items": {
                        "type": "array",
                        "description": (
                            "What this judgement is made of: one entry per thing that "
                            "counted, in the order that tells the story. The value is "
                            "the sum of the entries, and the judged agent reads them "
                            "one by one — so each `what` names the thing that happened "
                            "rather than restating its weight."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "points": {
                                    "type": "integer",
                                    "description": (
                                        "This entry's weight: a non-zero integer, "
                                        "negative for a deduction."
                                    ),
                                },
                                "what": {
                                    "type": "string",
                                    "description": (
                                        "What the weight is for — the work delivered, "
                                        "or the thing the agent should stop doing."
                                    ),
                                },
                            },
                            "required": ["points", "what"],
                        },
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "What the judgement rests on, in one line — a sentence the "
                            "judged agent reads in its next round."
                        ),
                    },
                    "searched": {
                        "type": "string",
                        "description": (
                            "What you read while looking for something to deduct, and "
                            "why you found none. Required when every item is positive: "
                            "a round of nothing but credit is a claim about the search, "
                            "not an absence of evidence."
                        ),
                    },
                },
                "required": ["agent", "items", "reason"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        name = arguments.get("agent")
        if not isinstance(name, str) or not name.strip():
            return ToolResult(
                content="set_agent_life needs `agent`: the registered name to write.",
                error=True,
            )
        items = arguments.get("items")
        # One validator for the list, in the module that owns the arithmetic: the write
        # below sums the same list, and two ideas of "a well-formed item" would end up
        # as a value nobody can explain.
        if total_of(items) is None:
            return ToolResult(
                content=(
                    "set_agent_life needs `items`: a non-empty list of "
                    "{`points`, `what`} entries, each with a non-zero integer weight "
                    "and the thing it is for. An entry that moved nothing is refused "
                    "rather than ignored — the judged agent would read it as work that "
                    f"counted. It was {items!r}."
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
        searched = arguments.get("searched")
        if searched is not None and not isinstance(searched, str):
            return ToolResult(
                content=(
                    f"set_agent_life needs `searched` as text; it was {searched!r} "
                    f"({type(searched).__name__})."
                ),
                error=True,
            )
        # A judgement with no deduction is a claim about the search, so the search is
        # what it has to report. Read from the items rather than from their sum: +4 and
        # -2 net +2, which is a round that did find something to deduct.
        if all(item["points"] > 0 for item in items) and not (
            isinstance(searched, str) and searched.strip()
        ):
            return ToolResult(
                content=(
                    "set_agent_life needs `searched` when every item is positive: say "
                    "what you read while looking for something to deduct, and why you "
                    "found none. A round of nothing but credit is a claim about the "
                    "search, not an absence of evidence."
                ),
                error=True,
            )
        path = Path(self._register_path) if self._register_path else agents_file()
        row = AgentRegistry(path).set_life(
            name.strip(), items, reason.strip(),
            searched=searched.strip() if isinstance(searched, str) else None,
        )
        if row is None:
            return ToolResult(
                content=(
                    f"No life value was written: no agent is registered under "
                    f"{name.strip()!r}, or the register could not be read."
                ),
                error=True,
            )
        return ToolResult(content="Written. " + _describe(row))
