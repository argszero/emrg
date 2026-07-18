"""Tests for server.tool_types dataclasses."""

from emrg.server.tool_types import ToolDefinition, ToolResult


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
