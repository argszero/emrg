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


class TestStopAllOrder:
    """stop_all() must stop clients FIRST and the daemon LAST (rant
    2026-08-17T14:15:33): GUI/TUI auto-spawn the daemon when they notice it
    missing, so stopping the daemon first would let a live client re-spawn
    it — the installer then still hits locked files."""

    def _record_order(self, monkeypatch):
        order: list[str] = []

        def _rec(name: str):
            def _fn(*a, **k):
                order.append(name)
            return _fn

        monkeypatch.setattr(_stop_all, "stop_gui", _rec("stop_gui"))
        monkeypatch.setattr(_stop_all, "stop_tui", _rec("stop_tui"))
        monkeypatch.setattr(_stop_all, "stop_daemon", _rec("stop_daemon"))
        monkeypatch.setattr(_stop_all, "stop_bundled_git", _rec("stop_bundled_git"))
        monkeypatch.setattr(_stop_all, "stop_lock_owners", _rec("stop_lock_owners"))
        monkeypatch.setattr(_stop_all, "verify", lambda: [])
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        return order

    def test_clients_before_daemon(self, monkeypatch):
        order = self._record_order(monkeypatch)
        assert stop_all() == 0
        assert order == [
            "stop_gui", "stop_tui", "stop_daemon", "stop_bundled_git",
            "stop_lock_owners",
        ]
        # daemon MUST come after both clients — a client alive when the
        # daemon dies would re-spawn it (auto-spawn mechanisms in GUI/TUI)
        assert order.index("stop_daemon") > order.index("stop_gui")
        assert order.index("stop_daemon") > order.index("stop_tui")
        # Restart Manager lock-owner kill must run AFTER bundled-git and
        # BEFORE verify (it is the last cleanup before the residual check)
        assert order.index("stop_lock_owners") > order.index("stop_bundled_git")

    def test_posix_skips_bundled_git(self, monkeypatch):
        order = self._record_order(monkeypatch)
        monkeypatch.setattr(_stop_all, "is_win", lambda: False)
        assert stop_all() == 0
        assert order == ["stop_gui", "stop_tui", "stop_daemon"]


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


class TestScanWindowsPythonEmrg:
    """_scan_windows_python_emrg — the cmdline fallback (rant 2026-08-17T17:03:38).

    Windows installer hit DeleteFile code 5 because a live python process
    holding websockets' C extension was missed: stop_daemon() only trusted the
    pid file, stop_tui() deliberately excluded emrg.server, and verify() never
    scanned python processes. The command line is the only reliable identity.
    """

    def test_parses_pids_from_cim_output(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return type("CP", (), {"stdout": "  101\n  202\nnot-a-pid\n\n"})()

        monkeypatch.setattr(_stop_all.subprocess, "run", fake_run)
        assert _stop_all._scan_windows_python_emrg(9999) == [101, 202]
        ps = calls[0][-1]
        # matches `-m emrg` AND `-m emrg.server` (daemon), excludes own pid
        assert "-match '-m emrg'" in ps
        assert "-ne 9999" in ps
        assert "Write-Output $_.ProcessId" in ps
        # must NOT exclude emrg.server (that was stop_tui's blind spot)
        assert "emrg\\.server" not in ps

    def test_renders_template_without_valueerror(self, monkeypatch):
        import os

        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: type("CP", (), {"stdout": ""}),
        )
        assert _stop_all._scan_windows_python_emrg(os.getpid()) == []

    def test_posix_returns_empty(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: False)
        assert _stop_all._scan_windows_python_emrg(1) == []

    def test_subprocess_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)

        def boom(*a, **k):
            raise OSError("powershell unavailable")

        monkeypatch.setattr(_stop_all.subprocess, "run", boom)
        assert _stop_all._scan_windows_python_emrg(1) == []


class TestStopDaemonCmdlineFallback:
    """stop_daemon() must kill a live daemon even when emrgd.pid is missing
    (rant 2026-08-17T17:03:38 acceptance #1: delete the pid file but keep the
    pythonw -m emrg.server process alive → cmdline scan still kills it)."""

    def test_kills_python_emrg_when_pid_file_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_stop_all, "config_dir", lambda: tmp_path)
        killed: list[int] = []
        monkeypatch.setattr(_stop_all, "_kill_pid_windows", lambda pid: killed.append(pid))
        monkeypatch.setattr(_stop_all, "_scan_windows_python_emrg", lambda own: [777])
        monkeypatch.setattr(_stop_all, "_pid_alive", lambda pid: False)
        _stop_all.stop_daemon()
        assert killed == [777]

    def test_no_pids_no_kill(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_stop_all, "config_dir", lambda: tmp_path)
        killed: list[int] = []
        monkeypatch.setattr(_stop_all, "_kill_pid_windows", lambda pid: killed.append(pid))
        monkeypatch.setattr(_stop_all, "_scan_windows_python_emrg", lambda own: [])
        monkeypatch.setattr(_stop_all, "_pid_alive", lambda pid: False)
        _stop_all.stop_daemon()
        assert killed == []


class TestVerifyWindowsPythonResidual:
    """verify() must report a live python emrg process even without a pid file
    (rant 2026-08-17T17:03:38 acceptance #2: residual → named list + exit 1)."""

    def test_reports_python_emrg_residual(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all, "_read_pid_file", lambda: None)
        monkeypatch.setattr(_stop_all, "_scan_windows_python_emrg", lambda own: [555])
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: type("CP", (), {"stdout": ""}),
        )
        out = _stop_all._verify_windows()
        assert any("python emrg process (pid 555)" in r for r in out)

    def test_no_python_residual_when_clean(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all, "_read_pid_file", lambda: None)
        monkeypatch.setattr(_stop_all, "_scan_windows_python_emrg", lambda own: [])
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: type("CP", (), {"stdout": ""}),
        )
        assert _stop_all._verify_windows() == []


class TestLockOwners:
    """stop_lock_owners — Restart Manager generic file-lock fix
    (rant 2026-08-17T17:55:42: 0.2.43 DeleteFile code 5 root cause = an
    EXTERNAL browser-harness daemon locking install\\ files; emrgd.pid/port
    empty and no -m emrg process, so the cmdline scan could never see it)."""

    def test_posix_noop(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: False)
        called: list = []
        monkeypatch.setattr(
            _stop_all, "_lock_owner_ps", lambda kill: called.append(kill) or "x"
        )
        _stop_all.stop_lock_owners()
        assert called == []

    def test_parses_tab_separated_output(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(
            _stop_all, "_lock_owner_ps",
            lambda kill: (
                "9400\tpython.exe\tC:\\...\\browser_harness\\Scripts\\python.exe -m browser_harness.daemon\n"
                "not-a-line\n"
                "555\tpythonw.exe\tsome -m emrg.server cmdline\n"
            ),
        )
        owners = _stop_all._windows_lock_owners(kill=False)
        assert owners == [
            (9400, "python.exe",
             "C:\\...\\browser_harness\\Scripts\\python.exe -m browser_harness.daemon"),
            (555, "pythonw.exe", "some -m emrg.server cmdline"),
        ]

    def test_subprocess_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)

        def boom(*a, **k):
            raise OSError("no powershell")

        monkeypatch.setattr(_stop_all.subprocess, "run", boom)
        assert _stop_all._lock_owner_ps(kill=False) == ""
        assert _stop_all._windows_lock_owners(kill=False) == []

    def test_kill_flag_rendered(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: calls.append(cmd) or type("CP", (), {"stdout": ""}),
        )
        _stop_all._lock_owner_ps(kill=True)
        ps = calls[0][-1]
        assert ps.startswith("& {")
        assert ps.rstrip().endswith("$true")
        _stop_all._lock_owner_ps(kill=False)
        assert calls[1][-1].rstrip().endswith("$false")

    def test_ps_template_contains_rm_key_elements(self):
        ps = _stop_all._LOCK_OWNER_PS
        assert "rstrtmgr.dll" in ps                      # Restart Manager API
        assert "RmRegisterResources" in ps
        assert "RmGetList" in ps
        assert "234" in ps                               # ERROR_MORE_DATA retry
        assert "BATCH" in ps and "500" in ps             # batch registration (perf)
        assert "$env:USERPROFILE" in ps                  # no hardcoded user
        assert "ParentProcessId" in ps                   # ancestor-chain exclusion
        assert "Substring(0, 150)" in ps                 # cmdline truncation
        assert "Stop-Process" in ps                      # kill
        assert "browser" in ps                           # browser-harness hint

    def test_ps_template_rm_get_list_cannot_deadloop(self):
        """Rant 2026-08-17T21:04:32 — the old C# loop passed m=0 forever
        (RmGetList's pdwProcCount is in/out: input=capacity, output=written)
        → every call returned ERROR_MORE_DATA(234) → infinite loop → zero
        owners killed → installer still hit DeleteFile code 5."""
        ps = _stop_all._LOCK_OWNER_PS
        # Preallocated capacity on the first call (not 0)
        assert "uint m = 50;" in ps
        assert "new RM_PROCESS_INFO[50]" in ps
        # Resize to n on 234, m passed as array capacity (ref in/out)
        assert "m = n;" in ps
        assert "new RM_PROCESS_INFO[n]" in ps
        # Hard loop cap — an abnormal API can never spin forever
        assert "MAX_ATTEMPTS" in ps and "attempt < MAX_ATTEMPTS" in ps
        assert "const int MAX_ATTEMPTS = 3;" in ps
        # No unbounded do/while(rc == 234) construct remains
        assert "while (rc == 234);" not in ps
        # Result capped by buffer length as well as n/m
        assert "Math.Min(n, m)" in ps
        assert "infos.Length" in ps
        # RmRegisterResources failure is checked, not silent
        assert "RmRegisterResources(h, (uint)cnt, batch, 0, IntPtr.Zero, 0, IntPtr.Zero) != 0" in ps
        assert "LastRegFail" in ps
        # Structured diagnostics line (files/owners/elapsed/reg_fail)
        assert "rm-diag" in ps
        assert "$sw.ElapsedMilliseconds" in ps

    @pytest.mark.parametrize(
        "scenario",
        [
            # (needed_seq, expected_rc, expected_count)
            # no owners at all
            ([0], 0, 0),
            # few owners fit in the initial 50-slot buffer
            ([3], 0, 3),
            # exactly the buffer capacity
            ([50], 0, 50),
            # one resize: 120 owners > 50 → 234 → resize to 120 → success
            ([120], 0, 120),
            # two resizes then success: 60→234(resize 60), 80→234(resize 80),
            # then 40 fits → rc=0, count 40
            ([60, 80, 40], 0, 40),
            # monotonic growth past capacity: 3 attempts all 234 → hard cap
            # terminates with rc=234 (no owners), NEVER dead-loops
            ([60, 80, 120], 234, 0),
            # pathological: API always returns 234 → cap terminates, empty result
            ([234, 234, 234, 234], 234, 0),
        ],
    )
    def test_rm_get_list_loop_logic(self, scenario):
        """Pure-Python model of the fixed C# RmGetList loop (rant
        2026-08-17T21:04:32) — parameterized over real RM behaviors. The loop
        must terminate in EVERY case (never dead-loop) and return the right
        owner count."""
        needed_seq, expected_rc, expected_count = scenario
        MAX_ATTEMPTS = 3
        calls = 0

        def get_list(infos_cap):
            """Mirror rstrtmgr.dll: m (input capacity) vs n (needed).
            rc=234 (ERROR_MORE_DATA) when needed > capacity; else rc=0 with
            m = written count = min(capacity, needed). After the sequence is
            exhausted the needed count stabilizes at its last value."""
            nonlocal calls
            idx = min(calls, len(needed_seq) - 1)
            calls += 1
            needed = needed_seq[idx]
            if needed == 234:  # pathological: API never converges
                return 234, needed, 0
            if needed > infos_cap:
                return 234, needed, 0
            return 0, needed, min(infos_cap, needed)

        n, reason = 0, 0
        rc = 0
        m = 50
        infos_len = 50
        attempts = 0
        for attempt in range(MAX_ATTEMPTS):
            attempts += 1
            rc, n, m = get_list(infos_len)
            if rc != 234:
                break
            infos_len = n
            m = n
        count = min(n, m) if rc == 0 else 0
        if count > infos_len:
            count = infos_len
        assert rc == expected_rc
        assert count == expected_count
        assert attempts <= MAX_ATTEMPTS  # hard cap — never dead-loops

    def test_lock_owner_diag_parsed(self):
        stdout = (
            "9400\tpython.exe\tC:\\...\\browser_harness\\Scripts\\python.exe -m browser_harness.daemon\n"
            "rm-diag\t1234\t2\t1500\t0\n"
        )
        assert _stop_all._lock_owner_diag(stdout) == {
            "files": 1234, "owners": 2, "elapsed_ms": 1500, "reg_fail": 0,
        }
        # no diag line / malformed → None
        assert _stop_all._lock_owner_diag("9400\tpython.exe\tx\n") is None
        assert _stop_all._lock_owner_diag("rm-diag\tabc\t2\t3\t0\n") is None
        assert _stop_all._lock_owner_diag("") is None

    def test_stop_lock_owners_logs_diag(self, monkeypatch, capsys):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(
            _stop_all, "_lock_owner_ps", lambda kill: (
                "killed file-lock owner: PID 9400 python.exe | C:\\...\\browser_harness\n"
                "rm-diag\t1234\t1\t900\t1\n"
            ),
        )
        _stop_all.stop_lock_owners()
        out = capsys.readouterr().out
        assert "killed file-lock owner: PID 9400 python.exe" in out
        assert "rm-scan files=1234 owners=1 elapsed=900ms reg_fail=1" in out
        assert "WARNING 1 resource-batch registration(s) failed" in out

    def test_verify_windows_logs_rm_diag(self, monkeypatch, capsys):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all, "_read_pid_file", lambda: None)
        monkeypatch.setattr(_stop_all, "_scan_windows_python_emrg", lambda own: [])
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: type("CP", (), {"stdout": ""}),
        )
        monkeypatch.setattr(
            _stop_all, "_lock_owner_ps", lambda kill: (
                "9400\tpython.exe\tC:\\...\\browser_harness\\Scripts\\python.exe -m browser_harness.daemon\n"
                "rm-diag\t1234\t1\t900\t0\n"
            ),
        )
        out = _stop_all._verify_windows()
        assert any("file-lock owner (pid 9400, python.exe)" in r for r in out)
        assert "rm-scan files=1234 owners=1 elapsed=900ms reg_fail=0" in capsys.readouterr().out

    def test_ps_template_renders_without_valueerror(self, monkeypatch):
        """The template must render without raising — braces are literal (no
        str.format), so the {{ }} escaping contract does not apply here."""
        calls: list = []
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: calls.append(cmd) or type("CP", (), {"stdout": ""}),
        )
        _stop_all._lock_owner_ps(kill=False)  # must not raise ValueError
        ps = calls[0][-1]
        assert "Where-Object { " in ps       # literal PowerShell braces intact
        assert "{0}" in ps                   # -f format placeholders intact

    def test_verify_reports_lock_owner_residual(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all, "_read_pid_file", lambda: None)
        monkeypatch.setattr(_stop_all, "_scan_windows_python_emrg", lambda own: [])
        monkeypatch.setattr(
            _stop_all, "_windows_lock_owners",
            lambda kill, stdout=None: [(9400, "python.exe",
                                        "C:\\...\\browser_harness\\Scripts\\python.exe -m browser_harness.daemon")],
        )
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: type("CP", (), {"stdout": ""}),
        )
        out = _stop_all._verify_windows()
        assert any("file-lock owner (pid 9400, python.exe)" in r for r in out)


class TestMainDelegatesToStopAll:
    def test_emrg_stop_cli_exits_nonzero(self):
        """`emrg stop` must sys.exit with the stop_all() code (installer gate)."""
        import emrg.__main__ as m

        assert hasattr(m, "_stop_all")
        src = Path(m.__file__).read_text(encoding="utf-8")
        assert "sys.exit(_stop_all())" in src
        assert "from emrg._stop_all import stop_all" in src
