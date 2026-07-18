"""Smoke test that module imports work without circular import errors."""


def test_tool_types_import():
    """emrg.server.tool_types should import without circular dependency."""
    from emrg.server.tool_types import ToolDefinition, ToolResult
    assert ToolDefinition is not None
    assert ToolResult is not None


def test_tools_base_import():
    """emrg.tools.base should import without the server/__init__ circular chain."""
    from emrg.tools.base import ToolExecutor
    assert ToolExecutor is not None


def test_config_import():
    """emrg.config should import cleanly (no tomli fallback needed)."""
    from emrg.config import LlmConfig
    cfg = LlmConfig()
    assert cfg.model == "gpt-4o-mini"


def test_grep_tool_import():
    """emrg.tools.grep_tool (PR #17) should import cleanly."""
    from emrg.tools.grep_tool import GrepTool
    assert GrepTool is not None


def test_glob_tool_import():
    """emrg.tools.glob_tool (PR #15) should import cleanly."""
    from emrg.tools.glob_tool import GlobTool
    assert GlobTool is not None
