"""Shared types for the EMRG tool system.

ToolDefinition and ToolResult are the universal types used by tool
executors, the daemon tool loop, and the LLM client. ToolCall is
handled via OpenAI's API dict format rather than as a dataclass.

The one part of that dict format the two ends *agree* on rather than copy is
`function.arguments`, and it is stated here, once, because each end used to guess
separately and both guesses were wrong in a way that ended the whole turn
(measured 2026-10-02, `tool_arguments_text` / `tool_arguments_object`):

    the client requests → llm.py accumulates the streamed deltas into one string
                        → daemon.py parses that string into the call's arguments

An arguments value that is not the object it is declared to be is not a detail
either end may shrug at: `llm.py` raised out of the stream generator on it, and
`daemon.py` reached `args.get(...)` with a non-dict and raised out of the turn.
Both are now this module's business.
"""

from __future__ import annotations

import json
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


def tool_arguments_text(value) -> str:
    """One piece of a tool call's `arguments` in its wire form, or `""`.

    The OpenAI streaming contract is *text that accumulates across deltas*
    (`llm.py`'s accumulator is the reader of that contract), so a string is one
    more fragment and passes through untouched — mid-stream it is very often an
    incomplete object (`'{"command": '`), which is exactly why it cannot be
    parsed here.

    A value that arrives **already an object** is off-contract but unambiguous: an
    OpenAI-compatible endpoint may send the whole payload at once. It *is* that
    object's text, so it is rendered as such — dropping it instead would run the
    tool with no arguments at all, which is a wrong action rather than a lost one.
    Anything else has no text to contribute.

    Measured 2026-10-02: the accumulator did `arguments += value` for every truthy
    value, so a dict — or an int, or a list — raised `TypeError: can only
    concatenate str (not "dict") to str` out of the stream generator. The turn
    died before any tool ran; no frame named the call, and the client's only
    evidence was a raw Python exception.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return ""


def tool_arguments_object(raw) -> dict:
    """The object a tool call's `arguments` names; `{}` when it names none.

    Every `ToolExecutor.execute` is declared `(self, arguments: dict)`, and the
    daemon used the parsed value without checking it was one. Measured 2026-10-02
    over a real turn: of the eleven values a tool call's `arguments` can carry,
    **seven ended the turn** with a raw Python exception printed to the client —
    five valid-JSON non-objects (`'null'`, `'[]'`, `'5'`, `'"x"'`, `'true'`, which
    the one-name `except json.JSONDecodeError` let through to `args.get("intent")`)
    and the two non-string shapes (a dict, a list) that `json.loads` refuses with
    `TypeError`, which that `except` also did not catch.

    `{}` is the meaning of "no readable arguments" that unparseable JSON already
    had: every tool answers for a missing field by name, the model sees the tool's
    own refusal and can correct itself, and the client still receives the call's
    `tool_start`/`tool_end` frames. A value that is already an object is returned
    as itself.

    The TUI answers the same question for **display** in
    `emrg.client.app._parse_arguments`, and reaches a different outcome on purpose:
    it keeps unreadable text as `{"_raw": …}` so the detail pane can show what the
    provider actually sent. That is a presentation rule, not this one — what the
    two share is only the parse, which is why this function is not imported there
    (feeding `_raw` to a tool would be a fabricated argument).
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:  # json.JSONDecodeError is one
        return {}
    return parsed if isinstance(parsed, dict) else {}
