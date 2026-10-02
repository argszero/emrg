"""Tests for server.tool_types dataclasses."""

from emrg.server.tool_types import (
    ToolDefinition,
    ToolResult,
    tool_arguments_object,
    tool_arguments_text,
)


class TestToolDefinition:
    """Tests for the ToolDefinition dataclass."""

    def test_defaults(self):
        """All fields have sensible defaults."""
        td = ToolDefinition()
        assert td.name == ""
        assert td.description == ""
        assert td.parameters == {}

    def test_full_construction(self):
        """All fields can be set at construction."""
        td = ToolDefinition(
            name="read",
            description="Read a file",
            parameters={"type": "object", "properties": {}},
        )
        assert td.name == "read"
        assert td.description == "Read a file"
        assert td.parameters == {"type": "object", "properties": {}}

    def test_field_assignment(self):
        """Fields are mutable after construction."""
        td = ToolDefinition()
        td.name = "write"
        td.description = "Write to file"
        assert td.name == "write"

    def test_equality(self):
        """Dataclass equality works as expected."""
        a = ToolDefinition(name="x", description="desc")
        b = ToolDefinition(name="x", description="desc")
        c = ToolDefinition(name="y", description="desc")
        assert a == b
        assert a != c

    def test_repr(self):
        """repr is human-readable."""
        td = ToolDefinition(name="test", description="a test tool", parameters={"x": 1})
        r = repr(td)
        assert "test" in r
        assert "a test tool" in r

    def test_no_purpose_field(self):
        """Rant 2026-08-19T10:35:24: the static `purpose` field was removed —
        intent (per-call, agent-written) replaces it."""
        td = ToolDefinition(name="x", description="desc")
        assert not hasattr(td, "purpose")
        assert td.name == "x"


class TestToolResult:
    """Tests for the ToolResult dataclass."""

    def test_defaults(self):
        """Default ToolResult has empty fields and error=False."""
        tr = ToolResult()
        assert tr.tool_call_id == ""
        assert tr.name == ""
        assert tr.content == ""
        assert tr.error is False

    def test_full_construction(self):
        """All fields can be set at construction."""
        tr = ToolResult(
            tool_call_id="call_123",
            name="read",
            content="file contents here",
            error=False,
        )
        assert tr.tool_call_id == "call_123"
        assert tr.name == "read"
        assert tr.content == "file contents here"
        assert tr.error is False

    def test_error_flag(self):
        """error flag defaults to False but can be set True."""
        tr_ok = ToolResult(content="success")
        tr_err = ToolResult(content="fail", error=True)
        assert tr_ok.error is False
        assert tr_err.error is True

    def test_different_ids_not_equal(self):
        """Two results with different tool_call_ids are not equal."""
        a = ToolResult(tool_call_id="a", name="read", content="x")
        b = ToolResult(tool_call_id="b", name="read", content="x")
        assert a != b


# ── A tool call's `arguments` payload (measured 2026-10-02) ─────────────────
#
# Both ends of the tool-call path read `function.arguments`: `llm.py` accumulates
# the streamed deltas into one string, `daemon.py` parses that string into the
# object `ToolExecutor.execute(arguments: dict)` is declared to take. Each end
# used to guess alone, and each guess ended the whole turn on a value the other
# considered normal — so the rule is one home (this module) and both are pinned
# against it here.

#: Every shape a `arguments` value has been seen or measured in.
_ARGUMENT_SHAPES = [
    ("a proper object", '{"command": "hi"}', {"command": "hi"}),
    ("an empty object", "{}", {}),
    ("empty text (the field was absent)", "", {}),
    ("the text 'null'", "null", {}),
    ("the text '[]'", "[]", {}),
    ("the text '5'", "5", {}),
    ("the text '\"x\"'", '"x"', {}),
    ("the text 'true'", "true", {}),
    ("unparseable text", "{not json", {}),
    ("a dict (off-contract, already parsed)", {"command": "hi"}, {"command": "hi"}),
    ("a list", ["hi"], {}),
    ("a number", 5, {}),
    ("None", None, {}),
]


class TestToolArgumentsObject:
    """`tool_arguments_object` — the daemon's reader of that payload."""

    def test_every_shape_yields_a_dict(self):
        """The declared type is a dict for **every** input, never an exception.

        The defect this closes: `json.loads(...)` under a one-name
        `except json.JSONDecodeError` let five valid-JSON non-objects through to
        `args.get("intent")`, and refused the two non-string shapes with a
        `TypeError` that `except` did not catch — seven of these thirteen ended
        the turn with a raw Python exception delivered to the client.
        """
        for label, raw, expected in _ARGUMENT_SHAPES:
            got = tool_arguments_object(raw)
            assert got == expected, f"{label}: {raw!r} → {got!r}, expected {expected!r}"
            assert isinstance(got, dict), f"{label}: {got!r} is not a dict"

    def test_the_contract_value_survives_intact(self):
        """The direction that matters most: a real call is not silently emptied."""
        nested = {"path": "a b", "n": 0, "flag": False, "inner": {"x": [1, 2]}}
        import json as _json

        assert tool_arguments_object(_json.dumps(nested)) == nested
        assert tool_arguments_object(nested) == nested

    def test_unreadable_text_is_not_recovered_from_the_other_side(self):
        """A non-string that is not an object contributes nothing — no guessing."""
        assert tool_arguments_object([{"a": 1}]) == {}
        assert tool_arguments_object(0) == {}
        assert tool_arguments_object(False) == {}


class TestToolArgumentsText:
    """`tool_arguments_text` — the LLM stream's writer of that payload."""

    def test_text_passes_through_untouched(self):
        """A fragment is not parsed: mid-stream it is usually an incomplete object."""
        for fragment in ('{"command": ', "", '{"a": 1}', "{not json"):
            assert tool_arguments_text(fragment) == fragment

    def test_an_object_that_arrived_parsed_becomes_its_text(self):
        """Off-contract, but unambiguous: dropping it would run the tool with no
        arguments at all — a wrong action, not a lost one."""
        import json as _json

        assert tool_arguments_text({"command": "hi"}) == _json.dumps({"command": "hi"})
        assert tool_arguments_text({"命令": "hi"}) == '{"命令": "hi"}'

    def test_everything_else_contributes_no_text(self):
        """The accumulator's right-hand side is always a `str` — the defect was
        `arguments += value` raising `TypeError: can only concatenate str`."""
        for value in (5, ["hi"], None, True, object()):
            out = tool_arguments_text(value)
            assert out == "" and isinstance(out, str), (value, out)
