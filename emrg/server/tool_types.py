"""Shared types for the EMRG tool system.

ToolDefinition and ToolResult are the universal types used by tool
executors, the daemon tool loop, and the LLM client. ToolCall is
handled via OpenAI's API dict format rather than as a dataclass — and the two
parts of that dict format every reader has to agree on are stated here, so that
no reader has to guess either of them again:

* the *shape of a tool-call entry* (:func:`tool_call_shape_problem`);
* the *shape of its `function.arguments`* (:func:`tool_arguments_text` /
  :func:`tool_arguments_object`), which is the one part the two ends **agree on
  rather than copy** — each end used to guess separately, and both guesses were
  wrong in a way that ended the whole turn (measured 2026-10-02):

      the client requests → llm.py accumulates the streamed deltas into one string
                          → daemon.py parses that string into the call's arguments

  An arguments value that is not the object it is declared to be is not a detail
  either end may shrug at: `llm.py` raised out of the stream generator on it, and
  `daemon.py` reached `args.get(...)` with a non-dict and raised out of the turn.
  Both are this module's business.
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
