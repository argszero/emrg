"""Tests for the bash tool."""

import asyncio

from emrg.tools.bash_tool import BashTool


def _run(coro):
    return asyncio.run(coro)


def test_bash_definition():
    tool = BashTool()
    d = tool.definition()
    assert d.name == "bash"
    assert "command" in d.parameters.get("required", [])
    assert "command" in d.parameters.get("properties", {})


def test_bash_no_command():
    tool = BashTool()
    result = _run(tool.execute({}))
    assert result.error
    assert "no command" in result.content.lower()


def test_bash_empty_command():
    tool = BashTool()
    result = _run(tool.execute({"command": ""}))
    assert result.error
    assert "no command" in result.content.lower()


def test_bash_simple_echo():
    """Integration test: runs a real 'echo' command."""
    tool = BashTool()
    result = _run(tool.execute({"command": "echo hello"}))
    assert not result.error
    assert "hello" in result.content
