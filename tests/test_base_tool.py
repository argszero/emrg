"""Tests for the tool base class."""

import pytest

from emrg.tools.base import ToolExecutor


def test_tool_executor_is_abstract():
    """Verify ToolExecutor cannot be instantiated directly."""
    with pytest.raises(TypeError):
        ToolExecutor()  # type: ignore[abstract]


def test_concrete_subclass_works():
    """A concrete subclass implementing both abstract methods should work."""

    class _Concrete(ToolExecutor):
        from emrg.server.tool_types import ToolDefinition, ToolResult

        def definition(self):
            return self.ToolDefinition(
                name="test", description="test", parameters={}
            )

        async def execute(self, arguments):
            return self.ToolResult(name="test", content="ok")

    tool = _Concrete()
    d = tool.definition()
    assert d.name == "test"
