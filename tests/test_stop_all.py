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
    _caller_context,
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
            "subprocess", "sys", "time", "pathlib", "platform", "ctypes", "ast",
            "pytest", "annotations", "__future__", "winreg", "datetime",
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


class TestCallerContext:
    """_caller_context — stop-chain forensics (rant 2026-08-19T13:11:34):
    every stop run must record who invoked it (parent pid/cmdline + argv) so
    "谁杀 daemon / 谁删文件" is traceable from the stop log alone."""

    def test_posix_queries_parent_cmdline(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(_stop_all, "is_win", lambda: False)

        class CP:
            stdout = "python -m emrg stop\n"

        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: calls.append(cmd) or CP(),
        )
        out = _caller_context()
        assert calls and "ps" in calls[0]  # POSIX parent probe
        assert "caller pid" in out
        assert "python -m emrg stop" in out
        assert "argv" in out

    def test_windows_queries_powershell_cim(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)

        class CP:
            stdout = "C:\\Python313\\python.exe -m emrg stop\n"

        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: calls.append(cmd) or CP(),
        )
        out = _caller_context()
        assert calls and "Get-CimInstance" in calls[0][-1]  # Windows parent probe
        assert "caller pid" in out
        assert "-m emrg stop" in out

    def test_probe_failure_degrades_gracefully(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: False)
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: (_ for _ in ()).throw(RuntimeError("no ps")),
        )
        out = _caller_context()
        assert "caller pid" in out
        assert "unknown parent" in out
        assert "argv" in out


class TestVerifyPosix:
    def test_no_residuals(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "_stop_scan_pids", lambda own: [])
        assert _verify_posix() == []

    def test_residuals_named(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "_stop_scan_pids", lambda own: [42, 43])
        out = _verify_posix()
        assert any("pid 42" in r for r in out)
        assert any("pid 43" in r for r in out)


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

    def test_name_re_matches_versioned_python_launchers(self):
        """The Windows name pre-filter must match versioned launchers too.

        #826 follow-up (pm25coder finding): bin/emrgd.cmd's daemon fallback
        chain ends at ``python-dist\\python3.13.exe`` (#576), but the original
        ``^python(\\.exe|w\\.exe)?$`` pattern missed every versioned name —
        a degraded install would run the daemon under a name both stop_daemon()
        and verify() ignore, reproducing DeleteFile code 5 with verify clean.
        The pattern is loose (the ``-m emrg`` cmdline filter is the strong
        discriminator) but must still reject non-python images.
        """
        import re

        pat = re.compile(_stop_all._WIN_PY_NAME_RE)
        for name in (
            "python.exe",
            "pythonw.exe",
            "python3.exe",
            "python3w.exe",
            "python3.13.exe",
            "pythonw3.13.exe",
            "python3.13w.exe",
        ):
            assert pat.match(name), f"versioned launcher not matched: {name}"
        for name in ("py.exe", "node.exe", "git.exe", "python3.dll"):
            assert not pat.match(name), f"non-python image wrongly matched: {name}"

    def test_ps_template_embeds_name_re(self, monkeypatch):
        """The rendered PowerShell must actually use the widened pattern."""
        calls: list = []
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return type("CP", (), {"stdout": ""})()

        monkeypatch.setattr(_stop_all.subprocess, "run", fake_run)
        _stop_all._scan_windows_python_emrg(1)
        ps = calls[0][-1]
        assert f"$_.Name -match '{_stop_all._WIN_PY_NAME_RE}'" in ps
        # the old narrow pattern must be gone
        assert "^python(\\.exe|w\\.exe)?$" not in ps

    def test_posix_returns_empty(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: False)
        assert _stop_all._scan_windows_python_emrg(1) == []

    def test_subprocess_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)

        def boom(*a, **k):
            raise OSError("powershell unavailable")

        monkeypatch.setattr(_stop_all.subprocess, "run", boom)
        assert _stop_all._scan_windows_python_emrg(1) == []


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
        # hermeticity (#738): check_install_writable() probes the REAL
        # ~/.emrg/install (hardcoded expanduser path) — on a machine where EMRG
        # is installed+running this would report real locked files and the
        # "clean" assertion would fail. Isolate it (verified pre-existing on
        # master dba96a7, #829's lock-probe addition).
        monkeypatch.setattr(_stop_all, "check_install_writable", lambda: [])
        monkeypatch.setattr(_stop_all, "_windows_lock_owners", lambda kill, stdout=None: [])
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
        monkeypatch.setattr(_stop_all, "find_install_module_holders", lambda: [])
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


class TestIndependentLockProbe:
    """check_install_writable — INDEPENDENT of Restart Manager (rant
    2026-08-17T21:06:05): simulates the installer's overwrite with an
    exclusive open, so a broken RM detector can never blind verify."""

    def _mk_root(self, tmp_path, files=("a.txt", "sub/b.txt")):
        root = tmp_path / "install"
        for f in files:
            p = root / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
        return str(root)

    def test_check_locked_files_collects_unopenable(self, tmp_path):
        root = self._mk_root(tmp_path)
        opened = set()

        def try_open(path):
            opened.add(path)
            if path.endswith("b.txt"):
                raise OSError("locked")

        assert _stop_all._check_locked_files(root, try_open=try_open) == [
            str(tmp_path / "install" / "sub" / "b.txt")
        ]
        assert opened == {
            str(tmp_path / "install" / "a.txt"),
            str(tmp_path / "install" / "sub" / "b.txt"),
        }

    def test_check_locked_files_clean(self, tmp_path):
        root = self._mk_root(tmp_path)
        assert _stop_all._check_locked_files(root, try_open=lambda p: None) == []

    def test_check_locked_files_empty_root(self, tmp_path):
        root = tmp_path / "empty-install"
        root.mkdir()
        assert _stop_all._check_locked_files(str(root), try_open=lambda p: None) == []

    def test_check_install_writable_posix_noop(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: False)
        assert _stop_all.check_install_writable() == []

    def test_check_install_writable_no_install_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all.os.path, "expanduser", lambda _: str(tmp_path))
        assert _stop_all.check_install_writable() == []

    def test_check_install_writable_returns_locked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all.os.path, "expanduser", lambda _: str(tmp_path))
        emrg_root = str(tmp_path / ".emrg" / "install")
        self._mk_root(tmp_path / ".emrg", files=("a.txt",))  # creates ~/.emrg/install
        monkeypatch.setattr(_stop_all, "_check_locked_files", lambda r: [emrg_root + "/a.txt"])
        assert _stop_all.check_install_writable() == [emrg_root + "/a.txt"]

    def test_check_install_writable_probe_error_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all.os.path, "expanduser", lambda _: str(tmp_path))
        self._mk_root(tmp_path)

        def boom(root):
            raise RuntimeError("ctypes unavailable")

        monkeypatch.setattr(_stop_all, "_check_locked_files", boom)
        assert _stop_all.check_install_writable() == []  # best-effort, never raise

    def test_verify_categories_include_lock_probe(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all, "_read_pid_file", lambda: None)
        monkeypatch.setattr(_stop_all, "_scan_windows_python_emrg", lambda own: [])
        monkeypatch.setattr(_stop_all, "_lock_owner_ps", lambda kill: "")
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: type("CP", (), {"stdout": ""}),
        )
        monkeypatch.setattr(
            _stop_all, "check_install_writable",
            lambda: [r"C:\\Users\\me\\.emrg\\install\\lib\\websockets\\speedups.cp313-win_amd64.pyd"],
        )
        cats = _stop_all._verify_windows_categories()
        names = [n for n, _ in cats]
        # rant 2026-08-18T16:24:01 — module-holder 枚举是主检测（DLL 模块锁
        # 只能靠 Modules 枚举点名），createfile-probe 降级为补充
        assert names == ["GUI", "daemon", "cmdline-scan", "module-holder", "RM re-scan", "createfile-probe", "bundled-git"]
        locked = dict(cats)["createfile-probe"]
        assert any("locked file" in r and "speedups" in r for r in locked)
        summary = _stop_all._verify_windows_summary()
        assert "createfile-probe 1" in summary
        assert "GUI 0" in summary

    def test_stop_all_retries_lock_kill(self, monkeypatch, capsys):
        """RM killed owners but locks linger → re-probe + retry up to 2×;
        lock residuals after escalation are ADVISORY — the install continues
        and the overwrite is the final arbiter (rant 2026-08-18T21:24:48 #2c/#5,
        superseding the pre-21:24:48 abort-on-lock behavior)."""
        calls: list[str] = []

        def rec(name):
            def _fn(*a, **k):
                calls.append(name)
            return _fn

        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all, "stop_gui", rec("gui"))
        monkeypatch.setattr(_stop_all, "stop_tui", rec("tui"))
        monkeypatch.setattr(_stop_all, "stop_daemon", rec("daemon"))
        monkeypatch.setattr(_stop_all, "stop_bundled_git", rec("bundled_git"))
        monkeypatch.setattr(_stop_all, "stop_lock_owners", rec("lock_owners"))
        monkeypatch.setattr(_stop_all.time, "sleep", lambda *a, **k: None)
        locked = iter([[r"C:\\locked.pyd"], [r"C:\\locked.pyd"], []])
        monkeypatch.setattr(_stop_all, "check_install_writable", lambda: next(locked))
        monkeypatch.setattr(
            _stop_all, "verify",
            lambda: ["locked file (installer overwrite would fail): C:\\locked.pyd"],
        )
        # lock residual only → exit 0 (advisory), not 1
        assert _stop_all.stop_all() == 0
        out = capsys.readouterr().out
        # initial kill + 2 retries
        assert calls.count("lock_owners") == 3
        assert "still locked after kill (retry 1/2)" in out
        assert "retry 2/2" in out
        # advisory, install continues
        assert "lock-related residual" in out
        assert "install continues" in out
        assert "exit code 0 (clean)" in out

    def test_stop_all_process_residual_still_aborts(self, monkeypatch, capsys):
        """Only EMRG PROCESS residuals (daemon/gui/python/git) abort; lock
        residuals do not (rant 2026-08-18T21:24:48 #2c/#5)."""
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all.time, "sleep", lambda *a, **k: None)
        # ⛔ 最高原则（宿主 2026-08-18T22:58）：必须隔离 stop_daemon——本机运行
        # pytest 时真实 stop_all() 会连 ws 发 shutdown 杀死正在运行的 emrgd
        # （2026-08-20 16:18/16:25 两次实证）。同文件其它 stop_all() 测试均有此隔离。
        monkeypatch.setattr(_stop_all, "stop_daemon", lambda: None)
        monkeypatch.setattr(_stop_all, "check_install_writable", lambda: [])
        monkeypatch.setattr(
            _stop_all, "verify",
            lambda: ["daemon (pid 1234)", "file-lock owner (pid 9400, python.exe)"],
        )
        assert _stop_all.stop_all() == 1
        out = capsys.readouterr().out
        assert "exit code 1 (1 residual)" in out
        assert "daemon (pid 1234)" in out
        assert "lock-related residual" not in out


class TestClassifyLockedFilesRelaxation:
    """python-dist self-held relaxation (rant 2026-08-18T21:24:48 #3): a
    locked file under python-dist\\ with no external target holder is
    self-held — stop_all runs from python-dist and lazily-loaded stdlib
    modules hold DLL locks the enumeration misses."""

    def _root(self):
        return r"C:\Users\me\.emrg\install"

    def _locked(self, rel):
        return [rf"C:\Users\me\.emrg\install\{rel}"]

    def test_pydist_unattributable_is_self_held(self):
        # select.pyd locked, enumeration lists NO holder for it → self-held
        self_held, residual = _stop_all._classify_locked_files(
            self._locked(r"python-dist\select.pyd"), [], self._root()
        )
        assert self_held == ["python-dist/select.pyd"]
        assert residual == []

    def test_pydist_excluded_holder_is_self_held(self):
        # python313.dll locked, holder = self (tag excluded) → self-held
        mh = [(11572, "python.exe", r"C:\...\python-dist\python.exe", 0,
               [r"C:\Users\me\.emrg\install\python-dist\python313.dll"], "excluded")]
        self_held, residual = _stop_all._classify_locked_files(
            self._locked(r"python-dist\python313.dll"), mh, self._root()
        )
        assert self_held == ["python-dist/python313.dll"]
        assert residual == []

    def test_pydist_external_target_is_residual(self):
        # python-dist DLL held by an EXTERNAL target → residual (strict)
        mh = [(9280, "python.exe", r"C:\...\browser_harness\python.exe", 0,
               [r"C:\Users\me\.emrg\install\python-dist\_socket.pyd"], "target")]
        self_held, residual = _stop_all._classify_locked_files(
            self._locked(r"python-dist\_socket.pyd"), mh, self._root()
        )
        assert self_held == []
        assert residual == ["python-dist/_socket.pyd"]

    def test_lib_unattributable_is_residual(self):
        # lib\ lock with no holder → residual (relaxation is python-dist only)
        self_held, residual = _stop_all._classify_locked_files(
            self._locked(r"lib\websockets\speedups.cp313-win_amd64.pyd"), [], self._root()
        )
        assert self_held == []
        assert residual == ["lib/websockets/speedups.cp313-win_amd64.pyd"]

    def test_is_lock_residual_prefixes(self):
        assert _stop_all._is_lock_residual("locked file (advisory): x")
        assert _stop_all._is_lock_residual("install-module holder (pid 1, x)")
        assert _stop_all._is_lock_residual("file-lock owner (pid 1, x)")
        assert not _stop_all._is_lock_residual("daemon (pid 1)")
        assert not _stop_all._is_lock_residual("EMRG.exe (pid 1)")
        assert not _stop_all._is_lock_residual("lock-probe failed (error: x)")


class TestLockOwnerDetailAnnotation:
    def test_ps_template_has_owner_detail_annotation(self):
        """kill=False owner lines carry a 4th excluded|target column; the
        excluded ancestor chain + WARNING-all-excluded are emitted."""
        ps = _stop_all._LOCK_OWNER_PS
        assert "{0}`t{1}`t{2}`t{3}" in ps        # 4-col owner line
        assert "'excluded'" in ps and "'target'" in ps  # tag literals
        assert "excluded-chain" in ps             # chain dump (incl. self PID)
        assert "WARNING all" in ps and "$targets.Count" in ps
        assert "$exclude -join" in ps or "$exclude)" in ps

    def test_parser_filters_excluded_owners(self, monkeypatch):
        """Owners tagged `excluded` (self + ancestor chain) must NOT surface
        as verify residuals; `target` owners (and legacy 3-col lines) do."""
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(
            _stop_all, "_lock_owner_ps",
            lambda kill: (
                "9400\tpython.exe\tC:\\...\\browser_harness\\Scripts\\python.exe -m browser_harness.daemon\ttarget\n"
                "555\tpythonw.exe\tsome -m emrg.server cmdline\texcluded\n"
                "666\tsetup.exe\tC:\\...\\inno setup.exe\texcluded\n"
            ),
        )
        owners = _stop_all._windows_lock_owners(kill=False)
        assert owners == [
            (9400, "python.exe",
             "C:\\...\\browser_harness\\Scripts\\python.exe -m browser_harness.daemon"),
        ]

    def test_parser_legacy_3col_lines_still_parsed(self, monkeypatch):
        """Backward compatibility: pre-annotation 3-column lines default to
        target (existing verify output must keep working)."""
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(
            _stop_all, "_lock_owner_ps",
            lambda kill: "9400\tpython.exe\tC:\\...\\browser_harness\\Scripts\\python.exe\n",
        )
        owners = _stop_all._windows_lock_owners(kill=False)
        assert owners == [(9400, "python.exe",
                           "C:\\...\\browser_harness\\Scripts\\python.exe")]


# ── Lock-probe fail-closed (rant 2026-08-18T09:40:40) ────────────

class TestLockProbeFailClosed:
    def test_probe_error_sets_global_and_prints_error(self, monkeypatch, tmp_path, capsys):
        """A probe exception is NOT silently swallowed as 'clean': it records
        _lock_probe_error + prints `lock-probe ERROR ... FAIL CLOSED`."""
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all.os.path, "expanduser", lambda _: str(tmp_path))
        root = tmp_path / ".emrg" / "install"
        root.mkdir(parents=True)
        (root / "a.txt").write_text("x", encoding="utf-8")

        def boom(root_dir):
            raise RuntimeError("ctypes unavailable")

        monkeypatch.setattr(_stop_all, "_check_locked_files", boom)
        _stop_all._lock_probe_error = None
        assert _stop_all.check_install_writable() == []  # never raises
        assert _stop_all._lock_probe_error == "RuntimeError: ctypes unavailable"
        out = capsys.readouterr().out
        assert "lock-probe ERROR: RuntimeError: ctypes unavailable" in out
        assert "FAIL CLOSED" in out

    def test_probe_prints_scanned_count(self, monkeypatch, tmp_path, capsys):
        """Scan stats: `createfile-probe scanned N files -> M locked (Xms)` — a
        bare `0 locked` without a scan count is untrustworthy. The probe is
        supplementary (rant 2026-08-18T16:24:01); DLL module locks need the
        module-holder scan."""
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all.os.path, "expanduser", lambda _: str(tmp_path))
        root = tmp_path / ".emrg" / "install"
        (root / "sub").mkdir(parents=True)
        (root / "a.txt").write_text("x", encoding="utf-8")
        (root / "sub" / "b.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(_stop_all, "_check_locked_files", lambda root_dir: [])
        _stop_all._lock_probe_error = None
        assert _stop_all.check_install_writable() == []
        out = capsys.readouterr().out
        assert "createfile-probe scanned 2 files -> 0 locked" in out

    def test_verify_surfaces_probe_error_as_residual(self, monkeypatch, tmp_path):
        """探测失败 ≠ 干净: a failed probe becomes a verify residual → exit 1
        (installer aborts instead of overwriting locked files)."""
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all.os.path, "expanduser", lambda _: str(tmp_path))
        root = tmp_path / ".emrg" / "install"
        root.mkdir(parents=True)
        (root / "a.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(_stop_all, "_read_pid_file", lambda: None)
        monkeypatch.setattr(_stop_all, "_scan_windows_python_emrg", lambda own: [])
        monkeypatch.setattr(_stop_all, "_lock_owner_ps", lambda kill: "")
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: type("CP", (), {"stdout": ""}),
        )

        def boom(root_dir):
            raise OSError("probe exploded")

        monkeypatch.setattr(_stop_all, "_check_locked_files", boom)
        _stop_all._windows_cats_cache = None
        try:
            out = _stop_all._verify_windows()
            assert any("lock-probe failed (error: OSError: probe exploded)" in r for r in out)
        finally:
            _stop_all._windows_cats_cache = None


# ── Verify single-scan (rant 2026-08-18T09:40:40 #4) ─────────────

class TestVerifySingleScan:
    def test_summary_reuses_cache_no_second_scan(self, monkeypatch, tmp_path, capsys):
        """verify() + _verify_windows_summary() must run the PowerShell RM
        scan ONCE — the previous code ran it twice (~2s+ wasted, duplicated
        rm-scan log lines)."""
        monkeypatch.setattr(_stop_all, "is_win", lambda: True)
        monkeypatch.setattr(_stop_all, "_read_pid_file", lambda: None)
        monkeypatch.setattr(_stop_all, "_scan_windows_python_emrg", lambda own: [])
        monkeypatch.setattr(_stop_all.os.path, "expanduser", lambda _: str(tmp_path))
        calls = {"n": 0}

        def fake_ps(kill):
            calls["n"] += 1
            return "rm-diag\t3\t0\t10\t0\n"

        monkeypatch.setattr(_stop_all, "_lock_owner_ps", fake_ps)
        monkeypatch.setattr(
            _stop_all.subprocess, "run",
            lambda cmd, **kw: type("CP", (), {"stdout": ""}),
        )
        _stop_all._windows_cats_cache = None
        try:
            assert _stop_all._verify_windows() == []
            assert calls["n"] == 1
            summary = _stop_all._verify_windows_summary()
            assert "RM re-scan 0" in summary
            assert calls["n"] == 1  # cached — no second PowerShell scan
            # a fresh categories() call refreshes the cache
            _stop_all._verify_windows_categories()
            assert calls["n"] == 2
        finally:
            _stop_all._windows_cats_cache = None


# ── PYTHONPATH observability (rant 2026-08-18T09:40:40) ──────────

class TestPythonPathObservability:
    def test_env_posix(self, monkeypatch):
        monkeypatch.setattr(_stop_all, "is_win", lambda: False)
        monkeypatch.setattr(_stop_all.os, "environ", {"PYTHONPATH": "C:/x"})
        assert _stop_all._pythonpath_env() == "PYTHONPATH=C:/x"
        monkeypatch.setattr(_stop_all.os, "environ", {})
        assert _stop_all._pythonpath_env() == "PYTHONPATH=(unset)"

    def test_install_warning_positive_and_negative(self):
        # install-dir references → warn (both Windows and POSIX separators)
        assert _stop_all._pythonpath_install_warning(
            r"PYTHONPATH(User)=C:\Users\me\.emrg\install\lib") is not None
        assert _stop_all._pythonpath_install_warning(
            "PYTHONPATH(process)=/home/me/.emrg/install") is not None
        assert _stop_all._pythonpath_install_warning(
            "PYTHONPATH(Machine)=C:\\python313;C:\\tools") is None
        assert _stop_all._pythonpath_install_warning("PYTHONPATH(User)=(unset)") is None
        assert _stop_all._pythonpath_install_warning("") is None
        # unrelated install\lib path must NOT warn (pm25coder review note, PR #832)
        assert _stop_all._pythonpath_install_warning(
            r"PYTHONPATH(User)=C:\python\install\lib") is None


# ── Fixed-path dual-write log (rant 2026-08-18T11:20:54) ────────

class TestTeeDualWrite:
    def test_tee_writes_to_both(self):
        class _FakeFile:
            def __init__(self):
                self.data = []

            def write(self, d):
                self.data.append(d)

            def flush(self):
                pass

        orig = _FakeFile()
        logf = _FakeFile()
        tee = _stop_all._Tee(orig, logf)
        tee.write("line1\n")
        tee.write("line2\n")
        tee.flush()
        assert orig.data == ["line1\n", "line2\n"]
        assert logf.data == ["line1\n", "line2\n"]

    def test_open_stop_log_creates_logs_dir(self, monkeypatch, tmp_path):
        """~/.emrg/logs is created and the file matches the timestamp pattern
        (stop_all-YYYYMMDD-HHMMSS.log)."""
        monkeypatch.setattr(_stop_all.os.path, "expanduser", lambda _: str(tmp_path))
        f = _stop_all._open_stop_log()
        assert f is not None
        name = f.name
        f.close()
        assert str(tmp_path / ".emrg" / "logs") in name
        import re
        assert re.search(r"stop_all-\d{8}-\d{6}\.log$", name), name

    def test_open_stop_log_oserror_returns_none(self, monkeypatch, tmp_path):
        def boom(*a, **k):
            raise OSError("cannot create logs dir")

        monkeypatch.setattr(_stop_all.os, "makedirs", boom)
        assert _stop_all._open_stop_log() is None

    def test_stop_all_prints_tee_path(self, monkeypatch, tmp_path, capsys):
        """stop_all() with a tee open prints the fixed-path line; stdout output
        still flows (POSIX regression: emrg stop output unchanged)."""
        monkeypatch.setattr(_stop_all, "is_win", lambda: False)
        monkeypatch.setattr(_stop_all.os.path, "expanduser", lambda _: str(tmp_path))
        monkeypatch.setattr(_stop_all, "stop_gui", lambda: None)
        monkeypatch.setattr(_stop_all, "stop_tui", lambda: None)
        monkeypatch.setattr(_stop_all, "stop_daemon", lambda: None)
        monkeypatch.setattr(_stop_all, "_stop_scan_pids", lambda own: [])
        monkeypatch.setattr(_stop_all, "verify", lambda: [])
        assert _stop_all.stop_all() == 0
        out = capsys.readouterr().out
        assert "log also written to" in out
        assert str(tmp_path / ".emrg" / "logs") in out
        assert "exit code 0 (clean)" in out
