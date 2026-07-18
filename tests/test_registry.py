"""Tests for the tool registry."""

from emrg.tools.base import ToolExecutor
from emrg.tools.registry import ToolRegistry
from emrg.server.tool_types import ToolDefinition, ToolResult


class _FakeTool(ToolExecutor):
    """A minimal tool for testing the registry."""

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="fake",
            description="A fake tool for testing.",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, arguments: dict) -> ToolResult:
        return ToolResult(name="fake", content="ok")


class _AnotherTool(ToolExecutor):
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="another",
            description="Another fake tool.",
            parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        )

    async def execute(self, arguments: dict) -> ToolResult:
        return ToolResult(name="another", content="ok")


def test_register_and_get():
    registry = ToolRegistry()
    tool = _FakeTool()
    registry.register(tool)
    assert registry.get("fake") is tool
    assert registry.get("nonexistent") is None


def test_register_multiple():
    registry = ToolRegistry()
    t1 = _FakeTool()
    t2 = _AnotherTool()
    registry.register(t1)
    registry.register(t2)
    assert registry.get("fake") is t1
    assert registry.get("another") is t2


def test_override_registration():
    registry = ToolRegistry()
    t1 = _FakeTool()
    t2 = _FakeTool()
    registry.register(t1)
    registry.register(t2)  # same name, should replace
    assert registry.get("fake") is t2


def test_names():
    registry = ToolRegistry()
    assert registry.names == []
    registry.register(_FakeTool())
    assert registry.names == ["fake"]
    registry.register(_AnotherTool())
    assert registry.names == ["another", "fake"]


def test_to_openai_tools_empty():
    registry = ToolRegistry()
    assert registry.to_openai_tools() == []


def test_to_openai_tools():
    registry = ToolRegistry()
    registry.register(_FakeTool())
    tools = registry.to_openai_tools()
    assert len(tools) == 1
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "fake"
    assert tools[0]["function"]["description"] == "A fake tool for testing."
    assert tools[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_to_openai_tools_multiple():
    registry = ToolRegistry()
    registry.register(_FakeTool())
    registry.register(_AnotherTool())
    tools = registry.to_openai_tools()
    assert len(tools) == 2
    names = {t["function"]["name"] for t in tools}
    assert names == {"fake", "another"}
