"""Stop every running EMRG process — pure standard library (no emrg imports).

This module converges the old ``bin/stop-emrg.cmd`` logic into one Python
implementation (host rant 2026-08-17T10:32:27 — "不要 stop-emrg.cmd 了，
所有动作都在 emrg stop 命令里完成"). It is the single source of truth for
stopping the daemon / TUI / GUI / bundled git before a Windows installer
overwrites ``~/.emrg/install`` (the pythonw daemon holds file locks that
Inno's CloseApplications cannot see).

It runs in three contexts, which is why it must stay pure-stdlib:

1. ``emrg stop`` → ``emrg.__main__._stop_all`` delegates to :func:`stop_all`
   and propagates the exit code.
2. ``python -m emrg._stop_all`` (module mode, same code path).
3. Standalone script: the Inno installer extracts this single file to
   ``{tmp}`` and executes it with the *runtime's* Python
   (``{app}\\bin\\python-dist\\python.exe``), where no emrg package /
   third-party modules are importable (``sys.path[0]`` is ``{tmp}``).

Steps (mirroring the old stop-emrg.cmd flow, all best-effort):

- GUI:        Windows ``taskkill /IM EMRG.exe`` graceful → unconditional
              ``/F``; POSIX ps-scan (``EMRG.app`` / ``EMRG-*.AppImage``)
- TUI:        Windows CIM filter ``python.exe|pythonw.exe -m emrg`` (not
              ``emrg.server``); POSIX ps-scan
- daemon:     ws protocol ``shutdown`` → ``~/.emrg/emrgd.pid`` → SIGTERM /
              ``taskkill /F /PID`` → 3s poll → cmdline-scan fallback
              (missing/stale pid file → kill any ``python*.exe -m emrg(.server)``,
              rant 2026-08-17T17:03:38); port file removed once dead
- bundled git: Windows ``install\\git\\`` prefix kill (git/ssh/plink/bash
              + fallback prefix full-kill — port of stop-emrg.cmd step 4)
- verify:     residual scan; any survivor → ``exit 1`` with a named list
              (installer aborts and shows the log, R125 semantics)

Order is deliberate: **clients (GUI/TUI) first, daemon LAST** (host rant
2026-08-17T14:15:33). Both GUI and TUI auto-spawn the daemon when they
detect it missing — stopping the daemon first while clients are alive makes
them immediately re-spawn it, so the stop "stops nothing" and the installer
still hits locked files. With the daemon last, no client remains to bring
it back, and verify() sees the true final state.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# Build stamp printed at the start of every run so the operator can tell at a
# glance which stop_all.py generation executed (rant 2026-08-17T21:06:31).
_STOP_ALL_STAMP = "built 2026-08-17 (rm-deadloop-fix + lock-probe)"

_EMRG_CLIENT_RE = re.compile(r"-m\s+emrg(\.server)?(\s|$)")
_APPIMAGE_RE = re.compile(r"EMRG-[\w.\-]*AppImage(\s|$)")


def is_win() -> bool:
    """True on Windows (incl. Git Bash launched python)."""
    return sys.platform == "win32"


def config_dir() -> Path:
    """The EMRG runtime directory (~/.emrg), matching emrg.config.config_dir."""
    return Path(os.path.expanduser("~")) / ".emrg"


def _no_window() -> dict:
    """subprocess kwargs that suppress console windows on Windows (#592)."""
    if is_win():
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


# ── Process matching ────────────────────────────────────────────

def match_cmdline(cmd: str) -> bool:
    """True if a command line belongs to an emrg process.

    Matches ``-m emrg`` / ``-m emrg.server`` (TUI + daemon), ``EMRG.app``
    (macOS GUI) and ``EMRG-*.AppImage`` (Linux AppImage). Does NOT match
    lookalikes such as ``-m emrg.serverless`` or ``-m emrgx``.
    """
    if "EMRG.app" in cmd:
        return True
    if _APPIMAGE_RE.search(cmd):
        return True
    return bool(_EMRG_CLIENT_RE.search(cmd))


def scan_pids(ps_output: str, own_pid: int) -> list[int]:
    """Parse ``ps -axww -o pid=,command=`` output → pids of emrg processes.

    ``own_pid`` is excluded so a running ``emrg stop`` never kills itself.
    """
    pids: list[int] = []
    for line in ps_output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == own_pid:
            continue
        if match_cmdline(parts[1]):
            pids.append(pid)
    return pids


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _read_pid_file() -> int | None:
    """Read ~/.emrg/emrgd.pid → int pid, or None if missing/invalid."""
    try:
        raw = (config_dir() / "emrgd.pid").read_text(encoding="utf-8").strip()
        pid = int(raw)
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def _kill_pid_windows(pid: int) -> None:
    """Force-kill a pid on Windows (taskkill /F — TerminateProcess)."""
    subprocess.run(
        ["taskkill", "/F", "/PID", str(pid)],
        capture_output=True,
        ** _no_window(),
    )


def _kill_pid_posix(pid: int, grace: float = 3.0) -> None:
    """SIGTERM → short grace → SIGKILL on POSIX."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.15)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _scan_windows_python_emrg(own_pid: int) -> list[int]:
    """Scan python.exe/pythonw.exe whose command line matches ``-m emrg`` /
    ``-m emrg.server`` (TUI + daemon), excluding ``own_pid``.

    Command line is the only reliable identity on Windows (rant
    2026-08-17T17:03:38): the daemon's ``emrgd.pid`` can be missing/stale/
    mismatched (GUI spawn, crash restart, external unlink — #593 family), so a
    live daemon would otherwise survive the pid-file path and keep locking the
    ``websockets`` C extensions under ``install\\`` — the installer then fails
    with ``DeleteFile failed; code 5`` while verify() reports clean.
    """
    if not is_win():
        return []
    # Literal PowerShell script-block braces must be escaped as {{ }} — same
    # contract as stop_tui() (str.format() would raise on unescaped braces).
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Where-Object {{ $_.ProcessId -ne {own} -and "
        "$_.Name -match '^python(\\.exe|w\\.exe)?$' -and "
        "$_.CommandLine -match '-m emrg' }} | "
        "ForEach-Object {{ Write-Output $_.ProcessId }}"
    ).format(own=own_pid)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10, **_no_window(),
        ).stdout
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return []
    return [int(p) for p in out.split() if p.strip().isdigit()]


# ── Minimal WebSocket client (RFC 6455, stdlib only) ────────────

def _ws_recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("ws: connection closed")
        buf += chunk
    return buf


def _ws_send_text(sock: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    mask = secrets.token_bytes(4)
    header = bytearray([0x81])  # FIN + text frame
    ln = len(payload)
    if ln < 126:
        header.append(0x80 | ln)
    elif ln < 65536:
        header.append(0x80 | 126)
        header.extend(ln.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(ln.to_bytes(8, "big"))
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _ws_recv_text(sock: socket.socket, timeout: float) -> str | None:
    sock.settimeout(timeout)
    b1, b2 = _ws_recv_exact(sock, 2)
    opcode = b1 & 0x0F
    ln = b2 & 0x7F
    if ln == 126:
        ln = int.from_bytes(_ws_recv_exact(sock, 2), "big")
    elif ln == 127:
        ln = int.from_bytes(_ws_recv_exact(sock, 8), "big")
    mask = _ws_recv_exact(sock, 4) if (b2 & 0x80) else None
    payload = _ws_recv_exact(sock, ln)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if opcode != 0x1:  # not a text frame
        return None
    return payload.decode("utf-8", "replace")


def ws_graceful_shutdown(port: int, token: str, timeout: float = 3.0) -> bool:
    """Send the daemon a graceful ``shutdown`` over a minimal WS connection.

    Mirrors emrg.connect.connect_to_server + _send_shutdown but with only the
    standard library (this module must run standalone inside the installer).
    Returns True when the daemon acked ``shutdown_ack``.
    """
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return False
    try:
        sock.sendall(
            (
                f"GET / HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode("ascii")
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                return False
            resp += chunk
        if not resp.startswith(b"HTTP/1.1 101"):
            return False
        # auth handshake (first frame) → auth_ok
        _ws_send_text(sock, json.dumps({"type": "auth", "token": token}))
        ack = _ws_recv_text(sock, timeout)
        if not ack:
            return False
        try:
            if json.loads(ack).get("type") != "auth_ok":
                return False
        except json.JSONDecodeError:
            return False
        # shutdown → shutdown_ack
        _ws_send_text(sock, json.dumps({"type": "shutdown"}))
        ack = _ws_recv_text(sock, timeout)
        if not ack:
            return False
        try:
            return json.loads(ack).get("type") == "shutdown_ack"
        except json.JSONDecodeError:
            return False
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ── Individual stop steps ───────────────────────────────────────

def stop_daemon() -> None:
    """Stop the daemon: ws shutdown → pid file → SIGTERM/taskkill /F → poll.

    Also removes ``~/.emrg/emrgd.port`` once the daemon pid is confirmed dead
    (the daemon itself removes it on graceful shutdown; a force-killed daemon
    cannot, so we clean it up — the next daemon start re-asserts both files).
    """
    port_path = config_dir() / "emrgd.port"
    try:
        port_tok = port_path.read_text(encoding="utf-8").split()
        if len(port_tok) == 2:
            if ws_graceful_shutdown(int(port_tok[0]), port_tok[1]):
                # wait for the daemon to exit + remove its pid file
                # (~10s grace: old stop-emrg.cmd v2 polled emrgd.pid up to
                # 10s; a busy daemon mid-tool-loop needs the full window)
                for _ in range(60):
                    pid = _read_pid_file()
                    if pid is None or not _pid_alive(pid):
                        break
                    time.sleep(0.15)
    except (OSError, ValueError):
        pass  # port file missing/corrupt → fall through to pid path

    pid = _read_pid_file()
    if pid is not None and _pid_alive(pid):
        if is_win():
            _kill_pid_windows(pid)
        else:
            _kill_pid_posix(pid)
        # poll up to 10s for it to disappear (matches old v2 grace window)
        for _ in range(60):
            if not _pid_alive(pid):
                break
            time.sleep(0.15)

    # Fallback: pid file missing/stale/mismatched → the live daemon (or any
    # TUI client stop_tui could not reach) would otherwise survive and keep
    # locking files under install\. Scan the command line — the only reliable
    # identity on Windows (rant 2026-08-17T17:03:38; returns [] on POSIX).
    for pid in _scan_windows_python_emrg(os.getpid()):
        _kill_pid_windows(pid)

    # Port file cleanup: the daemon removes it on graceful shutdown; a
    # force-killed daemon cannot, so remove it once the pid is confirmed gone
    # (the next daemon start re-asserts both files).
    daemon_gone = pid is None or not _pid_alive(pid)
    if daemon_gone:
        try:
            port_path.unlink()
        except OSError:
            pass


def _ps_output() -> str | None:
    try:
        return subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=10,
            **_no_window(),
        ).stdout
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None


def _stop_scan_pids(own_pid: int) -> list[int]:
    out = _ps_output()
    if out is None:
        return []
    return scan_pids(out, own_pid)


def stop_gui() -> None:
    """Stop the GUI app: Windows EMRG.exe (graceful then unconditional /F);
    POSIX ps-scan for EMRG.app / EMRG-*.AppImage (SIGTERM → SIGKILL)."""
    kw = _no_window()
    if is_win():
        # graceful first, then unconditional /F (host 2026-08-10T01:27:07Z:
        # long-lived GUI sessions ignore WM_CLOSE — /F must not be gated)
        subprocess.run(["taskkill", "/IM", "EMRG.exe"], capture_output=True, **kw)
        time.sleep(0.5)
        subprocess.run(["taskkill", "/F", "/IM", "EMRG.exe"], capture_output=True, **kw)
        return
    pids = [p for p in _stop_scan_pids(os.getpid())
            if p != os.getpid()]
    for pid in pids:
        _kill_pid_posix(pid)


def stop_tui() -> None:
    """Stop TUI clients: Windows CIM filter (python.exe|pythonw.exe running
    ``-m emrg`` but NOT ``emrg.server``); POSIX ps-scan.

    On Windows the invoking PID (a user-run ``emrg stop`` = ``python.exe
    -m emrg stop``) matches the ``-m emrg`` filter and would kill the CLI
    itself before ``stop_bundled_git`` + ``verify`` run — exclude it
    (same contract as the POSIX branch's ``own_pid`` exclusion)."""
    if is_win():
        own = os.getpid()
        # Literal PowerShell script-block braces must be escaped as {{ }} —
        # otherwise str.format() treats them as replacement fields and raises
        # ValueError: unexpected '{' in field name at runtime on Windows.
        ps_cmd = (
            "Get-CimInstance Win32_Process | "
            "Where-Object {{ $_.ProcessId -ne {own} -and "
            "$_.Name -match '^python(\\.exe|w\\.exe)?$' -and "
            "$_.CommandLine -match '-m emrg' -and "
            "$_.CommandLine -notmatch 'emrg\\.server' }} | "
            "ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
        ).format(own=own)
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, **_no_window(),
        )
        return
    pids = [p for p in _stop_scan_pids(os.getpid())
            if p != os.getpid()]
    for pid in pids:
        _kill_pid_posix(pid)


def stop_bundled_git() -> None:
    """Windows only: kill processes under ``install\\git\\`` (git/ssh/plink/
    bash first, then fallback prefix full-kill) — port of stop-emrg.cmd step 4.
    Never touches system Git (outside the prefix)."""
    if not is_win():
        return
    ps_cmd = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "$prefix=\"$env:USERPROFILE\\.emrg\\install\\git\\*\"; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -like $prefix -and "
        "$_.Name -in @('git.exe','ssh.exe','plink.exe','bash.exe') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }; "
        "Start-Sleep -Milliseconds 300; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -like $prefix } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }; "
        "Start-Sleep -Milliseconds 300"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", ps_cmd],
        capture_output=True, **_no_window(),
    )


# ── Restart Manager lock-owner scan (rant 2026-08-17T17:55:42) ─────
#
# Generic DeleteFile-code-5 fix. The 0.2.43 install failure was traced (via a
# verified find_lock_owner.ps1) to an EXTERNAL process — the browser-harness
# daemon, a standalone uv CPython under AppData\Roaming\uv\tools — locking
# files under install\. emrgd.pid/emrgd.port were empty and no `-m emrg`
# process existed, so the EMRG cmdline scan (17:03:38) could never see it.
# Restart Manager (rstrtmgr.dll) reports the ACTUAL owners of locked files,
# which covers both EMRG and foreign processes. The template is fully static
# (no str.format on the Python side) so the literal PowerShell/C# braces need
# no ``{{ }}`` escaping — the ``& { ... }`` wrapper only receives the kill
# flag as a positional argument.

_LOCK_OWNER_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$kill = ($args[0] -eq $true)
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Collections.Generic;
public static class RM {
  [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] static extern int RmStartSession(out uint h, int f, string k);
  [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)] static extern int RmRegisterResources(uint h, uint n, string[] r, uint a, IntPtr p, uint b, IntPtr q);
  [DllImport("rstrtmgr.dll")] static extern int RmGetList(uint h, out uint n, ref uint m, [In,Out] RM_PROCESS_INFO[] i, ref uint r);
  [DllImport("rstrtmgr.dll")] static extern int RmEndSession(uint h);
  [StructLayout(LayoutKind.Sequential)] struct RM_UNIQUE_PROCESS { public int dwProcessId; public System.Runtime.InteropServices.ComTypes.FILETIME ProcessStartTime; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct RM_PROCESS_INFO { public RM_UNIQUE_PROCESS Process; [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string strAppName; [MarshalAs(UnmanagedType.ByValTStr, SizeConst=64)] public string strServiceShortName; public int ApplicationType; public uint AppStatus; public uint TSSessionId; public bool bRestartable; }
  public static int LastRegFail = 0;
  public static int[] Who(string[] files) {
    uint h;
    if (RmStartSession(out h, 0, Guid.NewGuid().ToString()) != 0) return new int[0];
    try {
      const int BATCH = 500;
      int regFail = 0;
      for (int i = 0; i < files.Length; i += BATCH) {
        int cnt = Math.Min(BATCH, files.Length - i);
        string[] batch = new string[cnt];
        Array.Copy(files, i, batch, 0, cnt);
        // Check the return value — a failed batch must be visible, never
        // silently ignored (rant 2026-08-17T21:04:32).
        if (RmRegisterResources(h, (uint)cnt, batch, 0, IntPtr.Zero, 0, IntPtr.Zero) != 0) regFail++;
      }
      LastRegFail = regFail;
      uint n = 0, reason = 0;
      int rc = 0;
      const int MAX_ATTEMPTS = 3;
      // RmGetList's pdwProcCount (m) is IN/OUT: input = buffer capacity,
      // output = number of entries written. The old code passed m=0 forever,
      // so every call returned ERROR_MORE_DATA(234) -> infinite loop -> zero
      // owners reported -> installer still hit DeleteFile code 5. Fix:
      // preallocate 50 entries (m=50), on 234 resize to n and retry, hard
      // capped at MAX_ATTEMPTS so an abnormal API can NEVER dead-loop
      // (rant 2026-08-17T21:04:32).
      uint m = 50;
      RM_PROCESS_INFO[] infos = new RM_PROCESS_INFO[50];
      for (int attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
        rc = RmGetList(h, out n, ref m, infos, ref reason);
        if (rc != 234) break;
        m = n;
        infos = new RM_PROCESS_INFO[n];
      }
      List<int> res = new List<int>();
      if (rc == 0) {
        uint count = Math.Min(n, m);
        if (count > (uint)infos.Length) count = (uint)infos.Length;
        for (uint i = 0; i < count; i++) res.Add(infos[i].Process.dwProcessId);
      }
      return res.ToArray();
    } finally {
      RmEndSession(h);
    }
  }
}
'@
$root = Join-Path $env:USERPROFILE '.emrg\install'
if (-not (Test-Path $root)) { exit 0 }
$files = @(Get-ChildItem $root -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$owners = New-Object 'System.Collections.Generic.HashSet[int]'
if ($files.Length -gt 0) {
  foreach ($p in [RM]::Who([string[]]$files)) { [void]$owners.Add($p) }
}
$sw.Stop()
# Exclude self + the full ancestor chain: stop_all runs from
# install\python-dist\python.exe, which itself loads install\python313.dll
# etc. and would be reported as an owner; the chain also contains the Inno
# installer setup.exe — NEVER kill it (rant 2026-08-17T17:55:42 safety).
$exclude = New-Object 'System.Collections.Generic.HashSet[int]'
$cur = [int]$PID
for ($g = 0; $g -lt 64 -and $cur -gt 0; $g++) {
  [void]$exclude.Add($cur)
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
  if (-not $proc) { break }
  $cur = [int]$proc.ParentProcessId
}
$targets = @($owners | Where-Object { -not $exclude.Contains($_) })
$killedHint = $false
foreach ($pid in $targets) {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$pid" -ErrorAction SilentlyContinue
  $name = ''
  $cmd = ''
  if ($p) {
    $name = [string]$p.Name
    if ($p.CommandLine) { $cmd = [string]$p.CommandLine }
  }
  if ($cmd.Length -gt 150) { $cmd = $cmd.Substring(0, 150) }
  if ($kill) {
    Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    Write-Output ("killed file-lock owner: PID {0} {1} | {2}" -f $pid, $name, $cmd)
    if ($cmd -match 'browser[-_]?harness') { $killedHint = $true }
  } else {
    Write-Output ("{0}`t{1}`t{2}" -f $pid, $name, $cmd)
  }
}
if ($kill -and $killedHint) {
  Write-Output 'hint: browser-harness daemon stopped - restart it after the installer completes'
}
# Structured diagnostics so the Python side can log files/owners/elapsed and
# RmRegisterResources failures — no more silent idle scans (rant 2026-08-17T21:04:32).
Write-Output ("rm-diag`t{0}`t{1}`t{2}`t{3}" -f $files.Length, $owners.Count, $sw.ElapsedMilliseconds, [RM]::LastRegFail)
"""


def _lock_owner_ps(kill: bool) -> str:
    """Run the Restart Manager lock-owner scan under ``install\\`` (Windows only).

    ``kill=True`` stops every non-EMRG/ancestor owner (stop_lock_owners);
    ``kill=False`` emits ``PID<TAB>name<TAB>cmdline`` lines for the verify
    step. Returns "" on POSIX or when PowerShell/RM is unavailable (best-effort,
    like every other stop step). The script is fully static — no str.format() —
    so the literal PowerShell/C# braces need no ``{{ }}`` escaping.
    """
    if not is_win():
        return ""
    ps_cmd = "& { " + _LOCK_OWNER_PS + " } " + ("$true" if kill else "$false")
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", ps_cmd],
            capture_output=True, text=True, timeout=60, **_no_window(),
        ).stdout
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return ""


def _lock_owner_diag(stdout: str) -> dict | None:
    """Parse the ``rm-diag`` line emitted by ``_LOCK_OWNER_PS``.

    Shape: ``rm-diag<TAB>files<TAB>owners<TAB>elapsed_ms<TAB>reg_fail``.
    Returns a dict or None when absent/unparseable (e.g. RM unavailable).
    """
    for line in stdout.splitlines():
        parts = line.split("\t")
        if parts and parts[0] == "rm-diag" and len(parts) >= 5:
            try:
                return {
                    "files": int(parts[1]),
                    "owners": int(parts[2]),
                    "elapsed_ms": int(parts[3]),
                    "reg_fail": int(parts[4]),
                }
            except ValueError:
                return None
    return None


def _print_rm_diag(stdout: str) -> None:
    """Log the Restart Manager scan summary (files scanned / owners found /
    elapsed / registration failures) so a scan can never be silently idle
    (rant 2026-08-17T21:04:32)."""
    d = _lock_owner_diag(stdout)
    if not d:
        return
    print(
        f"emrg stop: rm-scan files={d['files']} owners={d['owners']} "
        f"elapsed={d['elapsed_ms']}ms reg_fail={d['reg_fail']}"
    )
    if d["reg_fail"]:
        print(
            f"emrg stop: WARNING {d['reg_fail']} resource-batch registration(s) "
            "failed - some file-lock owners may be missed"
        )


def _windows_lock_owners(kill: bool, stdout: str | None = None) -> list[tuple[int, str, str]]:
    """Parse ``_lock_owner_ps`` output → ``[(pid, name, cmdline_150), ...]``.

    ``stdout`` may be supplied by the caller (avoids a second PowerShell
    invocation when the diag line is needed too); None → run the scan.
    """
    if stdout is None:
        stdout = _lock_owner_ps(kill)
    owners: list[tuple[int, str, str]] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0].strip().isdigit():
            continue
        pid = int(parts[0])
        name = parts[1] if len(parts) > 1 else ""
        cmd = parts[2] if len(parts) > 2 else ""
        owners.append((pid, name, cmd))
    return owners


# ── Independent lock probe (rant 2026-08-17T21:06:05) ─────────────
# The RM scan and verify previously shared the same _windows_lock_owners
# function — when the detector broke (RmGetList dead-loop → empty result),
# verify went blind too and the installer overwrote locked files. This probe
# simulates the installer's overwrite directly (exclusive open = DeleteFile
# would fail) and is INDEPENDENT of Restart Manager, so a broken RM can never
# silently pass verify.

def _iter_install_files(root: str) -> list[str]:
    """All files under ``root`` (``~/.emrg/install``) — deterministic order."""
    files: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            files.append(os.path.join(dirpath, fn))
    return files


def _win_exclusive_open(path: str) -> None:
    """Open an existing file with ``dwShareMode=0`` (FileShare.None) — the exact
    semantic the Inno installer needs to overwrite/delete it. Raises OSError
    when another process holds the file (DeleteFile code 5 would occur)."""
    import ctypes

    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_SHARE_NONE = 0
    kernel32 = ctypes.windll.kernel32
    h = kernel32.CreateFileW(path, GENERIC_READ, FILE_SHARE_NONE, None,
                             OPEN_EXISTING, 0, None)
    if h == 0 or h == -1:
        raise OSError(f"CreateFileW failed for {path} (file is locked)")
    kernel32.CloseHandle(h)


def _check_locked_files(root: str, try_open=None) -> list[str]:
    """Return files under ``root`` that cannot be opened exclusively.

    ``try_open`` is injectable so the traversal/collection logic is testable on
    POSIX (default: Windows FileShare.None via :func:`_win_exclusive_open`).
    """
    if try_open is None:
        try_open = _win_exclusive_open
    locked: list[str] = []
    for path in _iter_install_files(root):
        try:
            try_open(path)
        except OSError:
            locked.append(path)
    return locked


def check_install_writable() -> list[str]:
    """Windows: probe ``install\\`` for files locked against overwrite.

    Independent of Restart Manager — the installer's DeleteFile would fail on
    every returned path. Returns [] when the probe is unavailable (POSIX, no
    install dir, or probe error) — best-effort like every other stop step.
    """
    if not is_win():
        return []
    root = os.path.join(os.path.expanduser("~"), ".emrg", "install")
    if not os.path.isdir(root):
        return []
    try:
        return _check_locked_files(root)
    except Exception:
        return []


def stop_lock_owners() -> None:
    """Windows only: stop every process holding a lock on files under install\\.

    The generic DeleteFile-code-5 fix (rant 2026-08-17T17:55:42): Restart
    Manager finds ANY owner — including non-EMRG processes (e.g. the
    browser-harness daemon) that the cmdline scan can never see. Self + the
    ancestor chain (the running python + Inno setup.exe) are excluded. Prints
    PID/name/cmdline of every stopped process. Best-effort: RM unavailable →
    silently skipped, verify() surfaces any survivor.
    """
    if not is_win():
        return
    stdout = _lock_owner_ps(kill=True)
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            print(f"emrg stop: {line}")
    _print_rm_diag(stdout)


# ── Verify + exit code ──────────────────────────────────────────

def _verify_windows_categories() -> list[tuple[str, list[str]]]:
    """Windows residual scan, one ``(category, residual_strings)`` entry per
    check — so the operator can see each check's result instead of guessing
    (rant 2026-08-17T21:06:31 #3)."""
    cats: list[tuple[str, list[str]]] = []

    # GUI residual
    gui: list[str] = []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq EMRG.exe"],
            capture_output=True, text=True, timeout=10, **_no_window(),
        ).stdout
        for m in re.finditer(r"EMRG\.exe\s+(\d+)", out):
            gui.append(f"EMRG.exe (pid {m.group(1)})")
    except (OSError, subprocess.SubprocessError, TimeoutError):
        pass
    cats.append(("GUI", gui))

    # daemon residual (emrgd.pid still alive)
    daemon: list[str] = []
    pid = _read_pid_file()
    if pid is not None and _pid_alive(pid):
        daemon.append(f"daemon (pid {pid})")
    cats.append(("daemon", daemon))

    # python emrg process residual (TUI/daemon by command line — covers the
    # pid-file blind spot: a live daemon with a missing/stale pid file would
    # otherwise pass verify and the installer would overwrite locked files)
    py = [f"python emrg process (pid {p})" for p in _scan_windows_python_emrg(os.getpid())]
    cats.append(("cmdline-scan", py))

    # file-lock owners under install\ (Restart Manager — generic code-5 fix:
    # covers ANY process holding locked files, incl. non-EMRG ones such as the
    # browser-harness daemon; self + ancestor chain excluded; rant 2026-08-17T17:55:42)
    rm_out = _lock_owner_ps(kill=False)
    rm = [
        f"file-lock owner (pid {o_pid}, {name or 'unknown'})"
        for o_pid, name, _cmd in _windows_lock_owners(kill=False, stdout=rm_out)
    ]
    cats.append(("RM re-scan", rm))
    _print_rm_diag(rm_out)

    # install-writability probe — INDEPENDENT of Restart Manager so a broken
    # detector can never blind verify (rant 2026-08-17T21:06:05)
    locked = check_install_writable()
    cats.append(("lock-probe", [f"locked file (installer overwrite would fail): {p}" for p in locked]))

    # bundled-git residual
    bg: list[str] = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$p = Get-CimInstance Win32_Process | Where-Object { "
             "$_.ExecutablePath -like \"$env:USERPROFILE\\.emrg\\install\\git\\*\" }; "
             "if ($p) { $p | ForEach-Object { Write-Output "
             "(\"{0} (pid {1})\" -f $_.Name, $_.ProcessId) } }"],
            capture_output=True, text=True, timeout=10, **_no_window(),
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line:
                bg.append(f"bundled-git {line}")
    except (OSError, subprocess.SubprocessError, TimeoutError):
        pass
    cats.append(("bundled-git", bg))
    return cats


def _verify_windows_summary() -> str:
    """One-line per-category verify summary, e.g.
    ``GUI 0 / daemon 0 / cmdline-scan 0 / RM re-scan 0 / lock-probe 0 locked /
    bundled-git 0`` (rant 2026-08-17T21:06:31 #3)."""
    cats = _verify_windows_categories()
    return " / ".join(f"{name} {len(items)}" for name, items in cats)


def _verify_windows() -> list[str]:
    residuals: list[str] = []
    for _name, items in _verify_windows_categories():
        residuals.extend(items)
    return residuals


def _verify_posix() -> list[str]:
    return [f"emrg process (pid {pid})" for pid in _stop_scan_pids(os.getpid())]


def verify() -> list[str]:
    """Scan for residual emrg processes. Returns a list of human-readable
    ``"name (pid N)"`` entries (empty = clean)."""
    return _verify_windows() if is_win() else _verify_posix()


# ── Orchestration ───────────────────────────────────────────────

def _step_plan() -> list[tuple[str, object]]:
    """Ordered stop steps. Clients (GUI/TUI) FIRST, daemon LAST (rant
    2026-08-17T14:15:33): both clients auto-spawn the daemon when it
    disappears, so stopping the daemon first would let a live client
    immediately bring it back — leaving locked files for the installer.
    Bundled git + RM lock-owner kill are Windows-only."""
    if is_win():
        return [
            ("GUI", stop_gui),
            ("TUI", stop_tui),
            ("daemon", stop_daemon),
            ("bundled git", stop_bundled_git),
            ("file-lock owners", stop_lock_owners),
        ]
    return [
        ("GUI", stop_gui),
        ("TUI", stop_tui),
        ("daemon", stop_daemon),
    ]


def stop_all() -> int:
    """Run every stop step, then verify. Returns 0 (clean) or 1 (residuals).

    Logging follows the standard from rant 2026-08-17T21:06:31: header with
    build stamp / python / platform / pid, ``[N/T] step -> result (elapsed)``
    per step, per-category verify summary, exit-code line with total elapsed,
    and NO silent failures (every step is wrapped so an exception still shows
    ``ERROR <step>: <reason>`` and the run continues to the final exit code).
    """
    t0 = time.monotonic()
    print(
        f"emrg stop: stop_all.py {_STOP_ALL_STAMP} | "
        f"python {platform.python_version()} {platform.system()}-{platform.machine()} "
        f"| pid {os.getpid()}"
    )
    steps = _step_plan()
    for i, (name, fn) in enumerate(steps, 1):
        s = time.monotonic()
        try:
            fn()
            print(f"emrg stop: [{i}/{len(steps)}] {name} -> done ({time.monotonic() - s:.1f}s)")
        except Exception as e:  # never silent (rant 2026-08-17T21:06:31 #4)
            print(
                f"emrg stop: ERROR [{i}/{len(steps)}] {name}: {e} "
                f"({type(e).__name__}) ({time.monotonic() - s:.1f}s)"
            )
    # Kill retry: RM may have killed owners but locks can linger — re-probe
    # with the INDEPENDENT writability check and retry (Try-again semantics,
    # rant 2026-08-17T21:06:05 #3); anything still locked flows into verify →
    # installer aborts with a named list instead of a code-5 dialog.
    if is_win():
        for attempt in range(1, 3):
            locked = check_install_writable()
            if not locked:
                break
            print(
                f"emrg stop: {len(locked)} file(s) still locked after kill "
                f"(retry {attempt}/2) ..."
            )
            s = time.monotonic()
            try:
                stop_lock_owners()
            except Exception as e:
                print(f"emrg stop: ERROR retry {attempt}/2: {e} ({type(e).__name__})")
            time.sleep(0.3)
            print(f"emrg stop: retry {attempt}/2 done ({time.monotonic() - s:.1f}s)")
    residuals = verify()
    if residuals:
        print("emrg stop: WARNING residual process(es) still running:")
        for r in residuals:
            print(f"  - {r}")
        if is_win():
            try:
                print("emrg stop: verify: " + _verify_windows_summary() + " -> RESIDUAL")
            except Exception:
                pass
        print(
            f"emrg stop: exit code 1 ({len(residuals)} residual) "
            f"({time.monotonic() - t0:.1f}s)"
        )
        return 1
    if is_win():
        try:
            print("emrg stop: verify: " + _verify_windows_summary() + " -> CLEAN")
        except Exception:
            pass
    print("emrg stop: all emrg processes stopped.")
    print(f"emrg stop: exit code 0 (clean) ({time.monotonic() - t0:.1f}s)")
    return 0


def main() -> None:
    code = stop_all()
    sys.exit(code)


if __name__ == "__main__":
    main()
