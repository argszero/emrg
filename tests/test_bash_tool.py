"""Tests for the bash tool."""

import asyncio
import sys

import pytest

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


@pytest.mark.skipif(sys.platform == "win32", reason="cmd.exe error message differs")
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


# ── Non-interactive env guards (rant 2026-08-07T10:17:27, Windows GCM) ──


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")
def test_bash_child_gets_no_prompt_env():
    """Child processes inherit GIT_TERMINAL_PROMPT=0 and GCM_INTERACTIVE=never.

    These guards prevent Git Credential Manager GUI popups when the daemon
    (a non-interactive background process) runs git/gh commands on Windows.
    """
    tool = BashTool()
    result = _run(tool.execute({
        "command": 'echo "GTP=$GIT_TERMINAL_PROMPT GCM=$GCM_INTERACTIVE ASKPASS=[$GIT_ASKPASS]"',
    }))
    assert not result.error
    assert "GTP=0" in result.content
    assert "GCM=never" in result.content
    assert "ASKPASS=[]" in result.content


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")
def test_bash_child_env_overridable_by_command():
    """The command itself can still override the guards for its own children."""
    tool = BashTool()
    result = _run(tool.execute({
        "command": "GIT_TERMINAL_PROMPT=1 sh -c 'echo $GIT_TERMINAL_PROMPT'",
    }))
    assert not result.error
    assert result.content.strip().endswith("1")
