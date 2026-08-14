"""Tests for the bash tool."""

import asyncio
import os
import sys

import pytest

from emrg.tools.bash_tool import BashTool, _decode_output, _translate_windows_heredocs


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


# ── _decode_output (rant 2026-08-08T09:35:30, Windows GBK output) ──
# On zh-CN Windows, cmd.exe/dir/echo emit GBK/cp936 bytes while git/gh emit
# UTF-8. Decoding GBK bytes as UTF-8 produces U+FFFD garbage (and logging
# that to a GBK-code-page handler crashes logging). _decode_output tries the
# locale code page strictly first, then UTF-8 — both must decode correctly.

def test_decode_output_gbk_bytes_on_windows(monkeypatch):
    """GBK bytes (zh-CN cmd output) decode correctly on a simulated nt path."""
    # The test machine's locale is UTF-8 — force the zh-CN code page.
    monkeypatch.setattr("emrg.tools.bash_tool.locale.getpreferredencoding",
                        lambda default=False: "gbk")
    assert _decode_output("中文文件名.txt".encode("gbk"), os_name="nt") == "中文文件名.txt"


def test_decode_output_utf8_bytes_on_windows(monkeypatch):
    """UTF-8 bytes (git/gh output) decode correctly on a simulated nt path."""
    monkeypatch.setattr("emrg.tools.bash_tool.locale.getpreferredencoding",
                        lambda default=False: "gbk")
    assert _decode_output("中文".encode("utf-8"), os_name="nt") == "中文"


def test_decode_output_posix_unchanged():
    """POSIX path keeps the original UTF-8-with-replacement behavior."""
    assert _decode_output("hello".encode("utf-8"), os_name="posix") == "hello"
    # Invalid UTF-8 on POSIX still degrades to replacement (never raises).
    assert "\ufffd" in _decode_output(b"\xff\xfe\x00", os_name="posix")


def test_decode_output_empty_and_none():
    """Empty/None bytes yield '' without errors."""
    assert _decode_output(b"") == ""
    assert _decode_output(None) == ""


def test_decode_output_fallback_chain_no_mojibake(monkeypatch):
    """A UTF-8 string must not be silently mojibake'd by the GBK first try.

    If the first decode were non-strict (errors='replace'), UTF-8 bytes would
    decode as GBK to garbage and never reach the UTF-8 fallback — the
    regression this test pins.
    """
    monkeypatch.setattr("emrg.tools.bash_tool.locale.getpreferredencoding",
                        lambda default=False: "gbk")
    payload = "git log 输出中文".encode("utf-8")
    assert _decode_output(payload, os_name="nt") == "git log 输出中文"


def test_client_log_handler_uses_utf8_encoding():
    """_run_client's RotatingFileHandler must be UTF-8 (rant #556 symmetry).

    Daemon side got encoding='utf-8' in #556; the client side was missed,
    crashing on zh-CN Windows when a log line contains U+FFFD. Assert the
    handler receives encoding='utf-8' and errors='backslashreplace'.
    """
    import logging.handlers as lh_mod
    from unittest.mock import patch

    captured = {}

    class _SpyHandler(lh_mod.RotatingFileHandler):
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            # Avoid actually opening a file
            raise RuntimeError("stop")

    import emrg.__main__ as main_mod

    # _run_client does `from logging.handlers import RotatingFileHandler`
    # at call time — patch the source module, not emrg.__main__.
    with patch.object(lh_mod, "RotatingFileHandler", _SpyHandler):
        with pytest.raises(RuntimeError, match="stop"):
            main_mod._run_client()
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "backslashreplace"


# ── Windows heredoc translation (host sessions 2026-08-14T21:26/21:36) ──
# cmd.exe (the bash tool's subprocess shell on Windows) cannot parse bash
# heredocs: `browser-harness <<'PY' ... PY` fails with "此时不应有 <<". The
# translation rewrites the first heredoc to `cmd < tempfile` so Windows agents
# run the identical commands as POSIX. All tests are pure-function (no
# subprocess) and run on every platform; a real cmd.exe integration test is
# gated to Windows.

def test_heredoc_no_heredoc_unchanged():
    cmd, path = _translate_windows_heredocs("echo hello & dir")
    assert cmd == "echo hello & dir"
    assert path is None


def test_heredoc_quoted_delimiter():
    cmd = "browser-harness <<'PY'\nnew_tab(\"https://example.com\")\nprint(page_info())\nPY\n"
    rewritten, path = _translate_windows_heredocs(cmd)
    assert path is not None
    assert rewritten.startswith("browser-harness < \"")
    assert rewritten.endswith('"')
    try:
        with open(path, encoding="utf-8") as f:
            # bash keeps the newline before the terminator line in the body
            assert f.read() == 'new_tab("https://example.com")\nprint(page_info())\n'
    finally:
        os.unlink(path)


def test_heredoc_unquoted_delimiter():
    cmd = "cat <<EOF\nline1\nline2\nEOF\n"
    rewritten, path = _translate_windows_heredocs(cmd)
    assert path is not None
    assert rewritten.startswith("cat < \"")
    try:
        with open(path, encoding="utf-8") as f:
            assert f.read() == "line1\nline2\n"
    finally:
        os.unlink(path)


def test_heredoc_tab_stripping():
    cmd = "cat <<-EOF\n\talpha\n\t\tbeta\nEOF\n"
    rewritten, path = _translate_windows_heredocs(cmd)
    assert path is not None
    try:
        with open(path, encoding="utf-8") as f:
            # bash <<- strips ALL leading tabs from each body line (verified
            # empirically: "alpha\n\t\tbeta" -> "alpha\nbeta")
            assert f.read() == "alpha\nbeta\n"
    finally:
        os.unlink(path)


def test_heredoc_unterminated_left_untouched():
    cmd = "browser-harness <<'PY'\nprint('never closed')\n"
    rewritten, path = _translate_windows_heredocs(cmd)
    assert rewritten == cmd
    assert path is None


def test_heredoc_tail_preserved():
    cmd = "cat <<EOF\nbody\nEOF\n& echo after\n"
    rewritten, path = _translate_windows_heredocs(cmd)
    assert path is not None
    assert rewritten.endswith('" & echo after')
    try:
        with open(path, encoding="utf-8") as f:
            assert f.read() == "body\n"
    finally:
        os.unlink(path)


def test_heredoc_body_word_does_not_terminate():
    cmd = "cat <<EOF\nEOF is just a word here\nreal EOF\nEOF\n"
    rewritten, path = _translate_windows_heredocs(cmd)
    assert path is not None
    try:
        with open(path, encoding="utf-8") as f:
            assert f.read() == "EOF is just a word here\nreal EOF\n"
    finally:
        os.unlink(path)


def test_heredoc_empty_body():
    cmd = "cat <<EOF\nEOF\n"
    rewritten, path = _translate_windows_heredocs(cmd)
    assert path is not None
    try:
        with open(path, encoding="utf-8") as f:
            # verified empirically: bash delivers 0 bytes for an empty heredoc
            assert f.read() == ""
    finally:
        os.unlink(path)


def test_heredoc_multiline_prefix_preserved_opener_line2():
    """Multi-line commands keep everything before the opener line (review #797 ❌).

    `echo a\necho b <<'PY'` previously rewrote to `echo b < temp`, silently
    dropping `echo a`. The identical POSIX command shape must be preserved.
    """
    cmd = "echo a\necho b <<'PY'\nbody\nPY\n"
    rewritten, path = _translate_windows_heredocs(cmd)
    assert path is not None
    assert rewritten.startswith("echo a\necho b < \"")
    try:
        with open(path, encoding="utf-8") as f:
            assert f.read() == "body\n"
    finally:
        os.unlink(path)


def test_heredoc_multiline_prefix_preserved_opener_line3():
    """Opener on line 3 — both preceding lines survive the rewrite."""
    cmd = "echo a\necho b\necho c <<'PY'\nbody\nPY\n"
    rewritten, path = _translate_windows_heredocs(cmd)
    assert path is not None
    assert rewritten.startswith("echo a\necho b\necho c < \"")
    try:
        with open(path, encoding="utf-8") as f:
            assert f.read() == "body\n"
    finally:
        os.unlink(path)


def test_heredoc_cd_prefix_workdir_pattern():
    """The plausible agent pattern `cd /tmp\\npython <<'PY'` keeps its cd."""
    cmd = "cd /tmp\npython -X utf8 <<'PY'\nprint('ok')\nPY\n"
    rewritten, path = _translate_windows_heredocs(cmd)
    assert path is not None
    assert rewritten.startswith("cd /tmp\npython -X utf8 < \"")
    try:
        with open(path, encoding="utf-8") as f:
            assert f.read() == "print('ok')\n"
    finally:
        os.unlink(path)


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe heredoc translation is Windows-only")
def test_bash_heredoc_integration_windows():
    """On Windows, `python <<'PY'` executes via the temp-file stdin redirect."""
    tool = BashTool()
    result = _run(tool.execute({
        "command": (
            f"\"{sys.executable}\" -X utf8 <<'PY'\n"
            "import sys\n"
            "print('HEREDOC_OK')\n"
            "PY\n"
        ),
    }))
    assert not result.error
    assert "HEREDOC_OK" in result.content
