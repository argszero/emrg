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


def test_bash_command_not_found():
    """Shell reports 'command not found' on stderr."""
    tool = BashTool()
    result = _run(tool.execute({"command": "nonexistent_cmd_xyzzy_42"}))
    # The /bin/sh itself is found, so FileNotFoundError not raised;
    # instead the shell prints to stderr. Check stderr content.
    assert "not found" in result.content.lower()


def test_bash_nonexistent_workdir():
    """Invalid workdir should trigger OSError path."""
    tool = BashTool()
    result = _run(tool.execute({
        "command": "echo test",
        "workdir": "/nonexistent/path/xyzzy",
    }))
    assert result.error
