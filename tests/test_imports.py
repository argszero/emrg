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


def test_style_to_sgr_256color():
    """256-color paths must be reachable (not shadowed by truecolor fallback)."""
    from rich.style import Style
    from rich.color import Color
    from emrg.client.python_tui.output import style_to_sgr

    # Default empty style → reset
    assert style_to_sgr(Style()) == "\x1b[0m"

    # Named ANSI color (STANDARD type, number=1) → 38;5;1
    sgr = style_to_sgr(Style.parse("red"))
    assert "\x1b[38;5;1m" in sgr or "\x1b[1;" in sgr  # bold red can combine

    # Explicit 256-color → 38;5;N (must not be truecolor 38;2)
    c = Color.parse("color(100)")
    sgr = style_to_sgr(Style(color=c))
    assert "38;5;100" in sgr, f"expected 256-color, got {sgr!r}"
    assert "38;2;" not in sgr, f"should not use truecolor, got {sgr!r}"
