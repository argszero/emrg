"""Shared types for the EMRG tool system.

ToolDefinition and ToolResult are the universal types used by tool
executors, the daemon tool loop, and the LLM client. ToolCall is
handled via OpenAI's API dict format rather than as a dataclass — and the one
part of that dict format every reader has to agree on, the *shape of a tool-call
entry*, is stated here (:func:`tool_call_shape_problem`) so that no reader has
to guess it again.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolDefinition:
    """OpenAI-compatible tool definition for the API.

    name: tool name exposed to the model
    description: what the tool does (used by the model for routing)
    parameters: JSON Schema dict for the tool's arguments
    """

    name: str = ""
    description: str = ""
    parameters: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    """Result of executing a tool, sent back to the LLM as a tool message."""

    tool_call_id: str = ""
    name: str = ""
    content: str = ""
    error: bool = False


def shape_of(value: object) -> str:
    """The received value's shape, spelled the way the payload wrote it.

    A JSON null is `null` rather than Python's `NoneType`: the word is read by
    whoever sent the value, and that is the word their payload used. Lives here
    rather than beside its first caller because there is more than one caller
    (the argument rule and the tool-call-entry rule both name the shape they
    received), and a second copy of this renderer is a second spelling.
    """
    return "null" if value is None else type(value).__name__


def tool_call_shape_problem(entry: object) -> str | None:
    """Why this tool-call entry cannot be read, or ``None`` when it can.

    The class this exists for
    ------------------------
    An entry of the provider's ``tool_calls`` array is read by four sites — the
    stream accumulator in ``llm.chat_stream``, the round's own accumulator, the
    round's finalization (which reaches back into ``function`` and ``name``), and
    the memory reflection loop, which reads a non-streaming reply — and every one
    of them read it with ``.get``/indexing as if it were the object the OpenAI
    format declares. Measured on this host 2026-10-03 over a **real** turn
    (``cyc20261003-043254``: an httpx stub in place of the transport, the rest of
    the path untouched):

    ==========================  ==========================================================
    entry sent                  what the host saw
    ==========================  ==========================================================
    ``None``                    ``LLM error: 'NoneType' object has no attribute 'get'.
    ``"x"``                     Check config at ~/.emrg/config.toml``
    ``[1]``                     (same sentence with the shape substituted)
    ``{"function": None}``      (same)
    ``{"function": "f"}``       (same)
    ``{"function": {"name": ``  ``TypeError: unhashable type: 'list'`` — unhandled, out of
    ``["read"]}}``              the round, with no frame at all
    ==========================  ==========================================================

    Five of the six are one sentence: the raw Python exception the reader raised,
    plus the generic remedy. The sentence names neither the field nor the shape,
    and the remedy — "Check config at ~/.emrg/config.toml" — is the wrong place to
    look for a provider that sent a null where a call should be. The sixth ends
    the round with nothing at all.

    The rule
    --------
    An entry is readable when it is an object; its ``function``, when the entry
    carries the key, is an object; and ``function.name``, when that object
    carries the key, is a string. Anything else is refused **by name** — the same
    "must be X; got Y" spelling the tool-argument rule uses.

    Two deliberate boundaries, each with a measured reason:

    * **``function.arguments`` is not this rule's business.** It is a JSON *string*
      on the wire and is read by its own rule; refusing it here as well would be a
      second, disagreeing opinion about a field this rule does not read.
    * **``id`` is not checked.** Every reader takes it with a default and echoes
      it; a wrong shape there ends no round and runs no wrong tool, and this rule
      only refuses what actually breaks the read.

    One shape is refused that does *not* crash, and deliberately: a
    ``function.name`` that is null. It used to run, as the loop's own
    ``Unknown tool: None`` — an answer that names a tool the model never asked
    for and hides the fact that the name was never a name. A round that cannot
    be routed is refused here, where the reason is still available to say.

    :param entry: one element of the provider's ``tool_calls`` array, whatever
        shape it arrived in.
    :returns: a refusal sentence naming the part and the shape it received, or
        ``None`` when the entry is readable.
    """
    if not isinstance(entry, dict):
        return f"the tool call is not an object; got {shape_of(entry)}"
    if "function" not in entry:
        return None
    function = entry["function"]
    if not isinstance(function, dict):
        return f"the tool call's `function` is not an object; got {shape_of(function)}"
    if "name" not in function:
        return None
    name = function["name"]
    if not isinstance(name, str):
        return f"the tool call's `function.name` is not a string; got {shape_of(name)}"
    return None
