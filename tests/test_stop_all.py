"""Unit tests for emrg/_stop_all.py — the pure-stdlib process-stopper.

Windows installer pre-stop converged into Python (host rant 2026-08-17T10:32:27:
"不要 stop-emrg.cmd 了，所有动作都在 emrg stop 命令里完成"). This module must
stay importable WITHOUT the emrg package (the Inno installer extracts the single
file to {tmp} and runs it with the runtime's Python), so it may only use the
standard library. Tests here run on POSIX (CI) and exercise the pure logic +
orchestration; the Windows-only branches are covered by the textual wiring tests
in test_installer_stop.py + the real installer runs on Windows hosts.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from emrg import _stop_all
from emrg._stop_all import (
    _read_pid_file,
    _verify_posix,
    match_cmdline,
    scan_pids,
    stop_all,
    ws_graceful_shutdown,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestPureStdlib:
    def test_no_nonstdlib_imports(self):
        """The module must import only stdlib — the installer runs it standalone
        with the runtime python where no third-party packages are importable."""
        src = (REPO_ROOT / "emrg" / "_stop_all.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        allowed = {
            "base64", "json", "os", "re", "secrets", "signal", "socket",
            "subprocess", "sys", "time", "pathlib", "ast", "pytest", "annotations",
            "__future__",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top in allowed, f"non-stdlib import: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or node.module.split(".")[0] in allowed, \
                    f"non-stdlib from-import: {node.module}"

    def test_has_main_guard(self):
        src = (REPO_ROOT / "emrg" / "_stop_all.py").read_text(encoding="utf-8")
        assert '__name__ == "__main__"' in src


class TestMatchCmdline:
    def test_matches_tui_and_daemon(self):
        assert match_cmdline("python -m emrg")
        assert match_cmdline("python -m emrg --init-auto-evolve")
        assert match_cmdline("python -m emrg.server")
        assert match_cmdline("pythonw -m emrg.server")

    def test_matches_gui(self):
        assert match_cmdline("/Applications/EMRG.app/Contents/MacOS/EMRG")
        assert match_cmdline("EMRG-0.2.41-x86_64.AppImage --no-sandbox")
        assert match_cmdline("/opt/EMRG-0.2.41-arm64.AppImage")

    def test_does_not_match_lookalikes(self):
        assert not match_cmdline("python -m emrg.serverless")
        assert not match_cmdline("python -m emrgx")
        assert not match_cmdline("git fetch origin master")
        assert not match_cmdline("python -m pytest tests/")
        assert not match_cmdline("EMRGX.app/Contents/MacOS/EMRGX")
        assert not match_cmdline("python -X dev main.py -m emrgistry")


class TestScanPids:
    def test_parses_and_excludes_own_pid(self):
        ps_out = (
            "  100 /usr/bin/python -m emrg\n"
            "  200 /usr/bin/python -m emrg.server\n"
            "  300 /usr/bin/python -m pytest\n"
            "  400 /Applications/EMRG.app/Contents/MacOS/EMRG\n"
            "  500 /opt/EMRG-0.2.41-x86_64.AppImage\n"
        )
        pids = scan_pids(ps_out, own_pid=200)
        assert pids == [100, 400, 500]

    def test_empty_and_malformed_lines(self):
        ps_out = "  100 /usr/bin/python -m emrg\n\n   \nnot-a-pid  /bin/ls\n"
        assert scan_pids(ps_out, own_pid=9999) == [100]

    def test_no_matches(self):
        assert scan_pids("  1 /sbin/launchd\n  2 /usr/libexec/foo\n", own_pid=9999) == []


class TestPidFile:
    def test_read_pid_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_stop_all, "config_dir", lambda: tmp_path)
        assert _read_pid_file() is None  # missing
        (tmp_path / "emrgd.pid").write_text("1234\n", encoding="utf-8")
        assert _read_pid_file() == 1234
        (tmp_path / "emrgd.pid").write_text("abc\n", encoding="utf-8")
        assert _read_pid_file() is None  # invalid
        (tmp_path / "emrgd.pid").write_text("-5\n", encoding="utf-8")
        assert _read_pid_file() is None  # non-positive


class TestWsGracefulShutdown:
    def test_connection_refused_returns_false(self):
        # port 1 on loopback: nothing listens → ConnectionRefused → False fast
        assert ws_graceful_shutdown(1, "token", timeout=1.0) is False

    def test_unreachable_port_returns_false(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("network unreachable")

        monkeypatch.setattr(_stop_all.socket, "create_connection", _boom)
        assert ws_graceful_shutdown(12345, "token") is False


class TestVerifyPosix:
    def test_no_residuals(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "_stop_scan_pids", lambda own: [])
        assert _verify_posix() == []

    def test_residuals_named(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "_stop_scan_pids", lambda own: [42, 43])
        out = _verify_posix()
        assert any("pid 42" in r for r in out)
        assert any("pid 43" in r for r in out)


class TestStopAllExitCode:
    """exit code semantics: 0 = clean, 1 = residual processes remain
    (the installer aborts on non-zero and shows the residual list)."""

    def _patch_steps(self, monkeypatch, residuals):
        monkeypatch.setattr(_stop_all, "stop_daemon", lambda: None)
        monkeypatch.setattr(_stop_all, "stop_gui", lambda: None)
        monkeypatch.setattr(_stop_all, "stop_tui", lambda: None)
        monkeypatch.setattr(_stop_all, "stop_bundled_git", lambda: None)
        monkeypatch.setattr(_stop_all, "verify", lambda: residuals)

    def test_clean_returns_0(self, monkeypatch, capsys):
        self._patch_steps(monkeypatch, residuals=[])
        assert stop_all() == 0
        out = capsys.readouterr().out
        assert "all emrg processes stopped" in out

    def test_residual_returns_1_and_lists_them(self, monkeypatch, capsys):
        self._patch_steps(monkeypatch, residuals=["EMRG.exe (pid 1234)", "daemon (pid 99)"])
        assert stop_all() == 1
        out = capsys.readouterr().out
        assert "WARNING residual process(es) still running" in out
        assert "EMRG.exe (pid 1234)" in out
        assert "daemon (pid 99)" in out

    def test_main_exits_with_code(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "stop_all", lambda: 1)
        with pytest.raises(SystemExit) as exc:
            _stop_all.main()
        assert exc.value.code == 1


class TestStopTuiPsTemplate:
    """Windows stop_tui() must render its PowerShell template without raising.

    Regression: the template's literal script-block braces (``Where-Object {``
    / ``ForEach-Object {``) were fed to str.format() unescaped, raising
    ValueError: unexpected '{' in field name at runtime on Windows — crashing
    `emrg stop` before stop_bundled_git + verify ever ran. The existing
    TestStopAllExitCode monkeypatches stop_tui entirely, so CI never rendered
    the template; this test pins the render path itself.
    """

    def test_stop_tui_renders_ps_template_win(self, monkeypatch):
        import os

        calls: list = []
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: calls.append(cmd) or type("CP", (), {})(),
        )

        _stop_all.stop_tui()  # must not raise ValueError

        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "powershell"
        ps = cmd[-1]
        # invoking-PID exclusion substituted into the template
        assert f"-ne {os.getpid()}" in ps
        # literal PowerShell script-block braces survived the format() call
        assert "Where-Object { $_.ProcessId" in ps
        assert "ForEach-Object { Stop-Process" in ps


class TestMainDelegatesToStopAll:
    def test_emrg_stop_cli_exits_nonzero(self):
        """`emrg stop` must sys.exit with the stop_all() code (installer gate)."""
        import emrg.__main__ as m

        assert hasattr(m, "_stop_all")
        src = Path(m.__file__).read_text(encoding="utf-8")
        assert "sys.exit(_stop_all())" in src
        assert "from emrg._stop_all import stop_all" in src
