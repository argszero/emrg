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
from datetime import datetime
from pathlib import Path

# Build stamp printed at the start of every run so the operator can tell at a
# glance which stop_all.py generation executed (rant 2026-08-17T21:06:31).
_STOP_ALL_STAMP = "built 2026-08-19 (read-only lock probe + stop-chain caller log — rants 13:08:41 + 13:11:34)"

# Fixed daemon port (host rant 2026-08-19T08:05:21): the daemon binds a fixed
# loopback port as its single-instance admission. This module is pure stdlib
# (runs standalone inside the installer) so it cannot import emrg.connect —
# keep in sync with emrg.connect.EMRGD_PORT.
_EMRGD_PORT = 56031

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

# Windows python interpreter image names (TUI `python.exe` / daemon
# `pythonw.exe`, plus versioned launchers `python3.exe` / `python3.13.exe` /
# `pythonw3.13.exe` / `python3.13w.exe` — bin/emrgd.cmd's fallback chain ends
# at `python-dist\python3.13.exe`, #576). Loose on purpose: the `-m emrg`
# command-line filter is the strong discriminator; the name only pre-filters
# the process list (degraded installs where the pid file is missing/stale are
# exactly the DeleteFile-code-5 scenario #826 targets — a versioned launcher
# must not slip past the scan and keep locking install\ files).
_WIN_PY_NAME_RE = r"^python.*\.exe$"


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
        "$_.Name -match '{name_re}' -and "
        "$_.CommandLine -match '-m emrg' }} | "
        "ForEach-Object {{ Write-Output $_.ProcessId }}"
    ).format(own=own_pid, name_re=_WIN_PY_NAME_RE)
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
    # Fixed-port shutdown (rant 2026-08-19T08:05:21): the daemon always
    # listens on _EMRGD_PORT; the port file only supplies the auth token. If
    # the file is missing/stale, fall through to the pid + cmdline paths.
    try:
        text = port_path.read_text(encoding="utf-8").split()
        token = text[1] if len(text) == 2 else ""
    except (OSError, ValueError):
        token = ""
    if token and ws_graceful_shutdown(_EMRGD_PORT, token):
        # wait for the daemon to exit + remove its pid file
        # (~10s grace: old stop-emrg.cmd v2 polled emrgd.pid up to
        # 10s; a busy daemon mid-tool-loop needs the full window)
        for _ in range(60):
            pid = _read_pid_file()
            if pid is None or not _pid_alive(pid):
                break
            time.sleep(0.15)

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
            "$_.Name -match '{name_re}' -and "
            "$_.CommandLine -match '-m emrg' -and "
            "$_.CommandLine -notmatch 'emrg\\.server' }} | "
            "ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
        ).format(own=own, name_re=_WIN_PY_NAME_RE)
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
# Detail per OWNER (not just targets): each line carries a 4th column tagging
# whether the owner was excluded by the ancestor chain (self + Inno setup.exe)
# or is a real target — so the operator can see WHO the owners were and WHY
# nothing was killed (rant 2026-08-18T09:40:40: 3 owners found but all
# excluded → targets=0 with zero output = detector looked blind).
foreach ($pid in $owners) {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$pid" -ErrorAction SilentlyContinue
  $name = ''
  $cmd = ''
  if ($p) {
    $name = [string]$p.Name
    if ($p.CommandLine) { $cmd = [string]$p.CommandLine }
  }
  if ($cmd.Length -gt 150) { $cmd = $cmd.Substring(0, 150) }
  if ($kill -and -not $exclude.Contains($pid)) {
    # taskkill /F /PID = TerminateProcess, returns immediately (rant
    # 2026-08-18T16:09:45): Stop-Process hangs on refusing/waiting targets
    # → the kill-mode RM scan blew its 60s timeout. Matches _kill_pid_windows.
    & taskkill /F /PID $pid 2>$null | Out-Null
    Write-Output ("killed file-lock owner: PID {0} {1} | {2}" -f $pid, $name, $cmd)
    if ($cmd -match 'browser[-_]?harness') { $killedHint = $true }
  } else {
    $tag = if ($exclude.Contains($pid)) { 'excluded' } else { 'target' }
    Write-Output ("{0}`t{1}`t{2}`t{3}" -f $pid, $name, $cmd, $tag)
  }
}
# The excluded ancestor chain (incl. self PID) — answers "who was skipped?".
Write-Output ("excluded-chain`t{0}" -f ($exclude -join ','))
if ($kill -and $owners.Count -gt 0 -and $targets.Count -eq 0) {
  Write-Output ("WARNING all {0} owner(s) excluded: {1}" -f $owners.Count, ($owners -join ','))
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


# Last Restart-Manager scan found NO external (non-self) lock owner — the
# evidence for the self-lock final guard (rant 2026-08-18T16:09:45): when
# lock-probe reports locked files but every RM owner is the stop_all runtime
# itself / its ancestor chain (or RM found nobody), the installer cannot win
# and the operator should re-run (a fresh installer process holds no locks).
_rm_no_external_owner: bool = False


def _print_rm_diag(stdout: str) -> None:
    """Log the Restart Manager scan summary (files scanned / owners found /
    elapsed / registration failures) so a scan can never be silently idle
    (rant 2026-08-17T21:04:32). Also records whether NO external (non-self)
    owner was found — the self-lock evidence for the final guard
    (rant 2026-08-18T16:09:45: stop_all runs from install\\python-dist so its
    own interpreter is the only lock holder → installer would still fail)."""
    global _rm_no_external_owner
    _rm_no_external_owner = False  # one scan per call — no stale evidence
    d = _lock_owner_diag(stdout)
    if d:
        if d["owners"] == 0 or "owner(s) excluded" in stdout:
            _rm_no_external_owner = True
        print(
            f"emrg stop: rm-scan files={d['files']} owners={d['owners']} "
            f"elapsed={d['elapsed_ms']}ms reg_fail={d['reg_fail']}"
        )
        if d["reg_fail"]:
            print(
                f"emrg stop: WARNING {d['reg_fail']} resource-batch registration(s) "
                "failed - some file-lock owners may be missed"
            )


# ── Module-holder enumeration (rant 2026-08-18T16:24:01) ─────────
#
# Diagnostic-script proof: the v0.2.48 DeleteFile code 5 lock holder was
# PID 9280 — a browser-harness CHILD process that loaded install\lib's
# websockets speedups.pyd (inherited PYTHONPATH). Restart Manager never
# reported it (the 2 owners it found were excluded ancestors) and a
# CreateFileW probe reports OK even when DeleteFile fails — LoadLibrary
# image-section locks are only visible via Process.Modules enumeration.
# This scan names the real holders; taskkill /F /T kills their tree.

_MODULE_HOLDER_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$root = Join-Path $env:USERPROFILE '.emrg\install'
if (-not (Test-Path $root)) { exit 0 }
$cut = $root.Length + 1
# Self + full ancestor chain (stop_all runs from install\python-dist\python.exe
# → itself loads install\python313.dll; ancestors include Inno setup.exe —
# NEVER kill them, same safety as the RM scan).
$exclude = New-Object 'System.Collections.Generic.HashSet[int]'
$cur = [int]$PID
for ($g = 0; $g -lt 64 -and $cur -gt 0; $g++) {
  [void]$exclude.Add($cur)
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
  if (-not $proc) { break }
  $cur = [int]$proc.ParentProcessId
}
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
  $p = $_
  try {
    $mods = @($p.Modules | Where-Object { $_.FileName -like "$root\*" })
  } catch { $mods = @() }
  if ($mods.Count -eq 0) { return }
  $files = @($mods | ForEach-Object { $_.FileName.Substring($cut) })
  $parent = ''
  $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue
  if ($pp) { $parent = [string]$pp.ParentProcessId }
  $tag = if ($exclude.Contains([int]$p.Id)) { 'excluded' } else { 'target' }
  Write-Output ("holder`t{0}`t{1}`t{2}`t{3}`t{4}`t{5}" -f $p.Id, $p.ProcessName, $p.Path, $parent, ($files -join '|'), $tag)
}
"""


def _module_holder_ps() -> str:
    """Run the module-holder enumeration (Windows only). Returns "" on POSIX
    or when PowerShell is unavailable (best-effort like every other step)."""
    if not is_win():
        return ""
    ps_cmd = "& { " + _MODULE_HOLDER_PS + " }"
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", ps_cmd],
            capture_output=True, text=True, timeout=60, **_no_window(),
        ).stdout
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return ""


def _parse_module_holders(stdout: str) -> list[tuple[int, str, str, int, list[str], str]]:
    """Parse ``holder<TAB>pid<TAB>name<TAB>exe<TAB>parent<TAB>files<TAB>tag``
    lines → ``[(pid, name, exe, parent_pid, [files...], tag), ...]``."""
    holders: list[tuple[int, str, str, int, list[str], str]] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if not parts or parts[0] != "holder" or len(parts) < 7:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        files = [f for f in parts[5].split("|") if f]
        try:
            parent = int(parts[4]) if parts[4] else 0
        except ValueError:
            parent = 0
        holders.append((pid, parts[2], parts[3], parent, files, parts[6]))
    return holders


def find_install_module_holders() -> list[tuple[int, str, str, int, list[str], str]]:
    """Windows: enumerate processes that loaded modules from ``install\\`` —
    the ONLY detector that can see DLL/.pyd image-section locks (rant
    2026-08-18T16:24:01). Returns [] on POSIX / when unavailable."""
    if not is_win():
        return []
    return _parse_module_holders(_module_holder_ps())


def _kill_tree_windows(pid: int) -> None:
    """Kill a process and its whole tree on Windows (taskkill /F /T —
    TerminateProcess, returns immediately; /T also kills children so a
    holder that is a child of a daemon releases its modules when the tree
    dies, rant 2026-08-18T16:24:01)."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=10, **_no_window(),
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        pass


def _escalate_kill_windows(pid: int) -> str:
    """Escalation kill for a lock-holder that survived the stop phase (rant
    2026-08-18T21:24:48 #2c): ``taskkill /F /T`` first → ancestor chain
    (parent tree) kill → ``Stop-Process -Force`` fallback. Returns a one-line
    outcome for the per-file disposition log."""
    log: list[str] = []
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, timeout=10, **_no_window(),
        )
        log.append(f"taskkill /F /T rc={getattr(r, 'returncode', '?')}")
        if getattr(r, "returncode", 1) == 0 and not _pid_alive(pid):
            return "; ".join(log) + " => killed"
    except (OSError, subprocess.SubprocessError, TimeoutError) as e:
        log.append(f"taskkill err={type(e).__name__}")
    # Ancestor chain (parent tree): walk ProcessId → ParentProcessId via CIM
    # and kill each ancestor with its own tree (the holder may be a child
    # whose parent keeps it / the lock alive).
    try:
        ps = (
            "powershell -NoProfile -Command "
            '"$p=Get-CimInstance Win32_Process -Filter \\"ProcessId=' + str(pid) + '\\"; '
            '$chain=@(); while($p -and $p.ParentProcessId -and $p.ParentProcessId -ne 0){'
            '$par=Get-CimInstance Win32_Process -Filter ("ProcessId="+$p.ParentProcessId); '
            'if(-not $par){break}; $chain+=$par; $p=$par}; '
            '$chain | ForEach-Object { Write-Output ("{0} {1}" -f $_.ProcessId,$_.Name) }"'
        )
        out = subprocess.run(ps, capture_output=True, text=True, timeout=15, **_no_window()).stdout
        for line in out.splitlines():
            parts = line.split()
            if not parts or not parts[0].strip().isdigit():
                continue
            apid, aname = int(parts[0]), " ".join(parts[1:])
            r = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(apid)],
                capture_output=True, text=True, timeout=10, **_no_window(),
            )
            log.append(f"parent {apid} ({aname}) rc={getattr(r, 'returncode', '?')}")
    except (OSError, subprocess.SubprocessError, TimeoutError) as e:
        log.append(f"parent-tree err={type(e).__name__}")
    if not _pid_alive(pid):
        return "; ".join(log) + " => killed"
    # Final fallback: Stop-Process -Force (CIM/WMIC-class stop).
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=10, **_no_window(),
        )
        log.append(f"Stop-Process rc={getattr(r, 'returncode', '?')}")
        if not _pid_alive(pid):
            return "; ".join(log) + " => killed"
    except (OSError, subprocess.SubprocessError, TimeoutError) as e:
        log.append(f"Stop-Process err={type(e).__name__}")
    return "; ".join(log) + " => SURVIVED"


def _escalate_locked_files(
    locked: list[str],
    mh_holders: list[tuple[int, str, str, int, list[str], str]],
    root: str,
) -> None:
    """Final escalation + per-file disposition log (rant 2026-08-18T21:24:48
    #2c/#4): for each still-locked file, attribute holders from the three data
    sources (module-holder enumeration / Restart Manager / self), kill
    external holders with escalation, and log the full chain — file path /
    holder PIDs / attribution source / action / result. Never raises; any
    survivors are logged as advisory and the install continues (the installer's
    own overwrite is the final arbiter)."""
    if not is_win() or not locked or not root:
        return
    try:
        root_n = os.path.normpath(root).replace("\\", "/").rstrip("/")
        holders_by_rel: dict[str, list[tuple[str, int, str]]] = {}
        for pid, name, _exe, _parent, files, tag in mh_holders:
            for f in files:
                rel = os.path.normpath(f).replace("\\", "/")
                if root_n and rel.startswith(root_n + "/"):
                    rel = rel[len(root_n) + 1:]
                src = "self/excluded" if tag == "excluded" else "module-holder"
                holders_by_rel.setdefault(rel, []).append((src, pid, name or ""))
        rm_owners = _windows_lock_owners(kill=False)
        print("emrg stop: escalation — locks surviving the stop phase:")
        for p in locked:
            rel = os.path.normpath(p).replace("\\", "/")
            if root_n and rel.startswith(root_n + "/"):
                rel = rel[len(root_n) + 1:]
            holders = list(holders_by_rel.get(rel, []))
            for o_pid, o_name, _cmd in rm_owners:  # RM reports actual owners
                if not any(h[1] == o_pid for h in holders):
                    holders.append(("rm", o_pid, o_name))
            chain = ", ".join(f"{src}:{pid}:{name}" for src, pid, name in holders) or "none found"
            actions: list[str] = []
            for src, pid, name in holders:
                if src == "self/excluded":
                    actions.append(f"self pid {pid} — released on stop_all exit")
                    continue
                actions.append(f"pid {pid} ({name}): " + _escalate_kill_windows(pid))
            print(
                f"emrg stop:   locked {rel} | holders [{chain}] | "
                + (" | ".join(actions) if actions else "no external holder")
            )
    except Exception as e:  # best-effort — never break the stop flow
        print(f"emrg stop: ERROR escalation: {e} ({type(e).__name__})")


# Any EXTERNAL (non-self/ancestor) module holder was found and killed —
# evidence for the self-lock final guard: when lock-probe still reports
# locked files but no external holder exists, the lock is stop_all's own
# runtime (rant 2026-08-18T16:09:45) and the installer cannot win.
_module_holder_external_found: bool = False


def _windows_lock_owners(kill: bool, stdout: str | None = None) -> list[tuple[int, str, str]]:
    """Parse ``_lock_owner_ps`` output → ``[(pid, name, cmdline_150), ...]``.

    ``stdout`` may be supplied by the caller (avoids a second PowerShell
    invocation when the diag line is needed too); None → run the scan.

    Since v0.2.45+ the owner lines carry a 4th ``excluded|target`` column
    (rant 2026-08-18T09:40:40) — ancestors (self + Inno setup.exe) are
    tagged ``excluded`` and are NOT returned here (verify must never list
    the running stop process itself as a residual); the full detail with
    tags stays visible in the raw log via stop_lock_owners.
    """
    if stdout is None:
        stdout = _lock_owner_ps(kill)
    owners: list[tuple[int, str, str]] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0].strip().isdigit():
            continue
        tag = parts[3] if len(parts) > 3 else "target"
        if tag == "excluded":
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
    """Open an existing file with DELETE access + FILE_SHARE_NONE — the
    exact sharing semantic the Inno installer's DeleteFile needs. Raises
    OSError when another process holds the file (DeleteFile code 5 would
    occur).

    DeleteFile semantics (rant 2026-08-18T16:09:45): a DLL loaded via
    LoadLibrary holds the file with FILE_SHARE_READ only — GENERIC_READ +
    FILE_SHARE_NONE probing succeeds (read sharing is granted) → false
    "0 locked" while the installer's DeleteFile still fails (the image
    section handle does not share FILE_SHARE_DELETE). Requesting DELETE
    access fails with ERROR_SHARING_VIOLATION on exactly the files
    DeleteFile would fail on. FILE_SHARE_NONE is kept as a complement —
    either condition failing means locked.

    ⚠️ NO delete-on-close disposition (rant 2026-08-19T13:08:41 — data-loss
    bug): the v0.2.4x probe opened with the delete-on-close flag and cleared
    it afterwards via the file-disposition-info API — but that clear only
    works on Windows 10 1903+; on older systems (or any failed/best-effort
    clear) the disposition stays set and closing the handle DELETES the
    probed file. The disposition flag adds nothing to the access check
    (DELETE access + share-none alone reproduces DeleteFile's sharing
    semantics), so the probe now opens with plain FILE_ATTRIBUTE_NORMAL and
    never sets a delete disposition — it can never delete anything, only
    ask "would DeleteFile succeed?"."""
    import ctypes

    GENERIC_DELETE = 0x00010000
    OPEN_EXISTING = 3
    FILE_SHARE_NONE = 0
    FILE_ATTRIBUTE_NORMAL = 0x80
    kernel32 = ctypes.windll.kernel32
    # 64-bit handle truncation fix (rant 2026-08-18T09:40:40): ctypes defaults
    # the restype of a foreign function to c_int — a 64-bit HANDLE gets
    # truncated, INVALID_HANDLE_VALUE(-1) becomes 0xFFFFFFFF and a valid
    # handle can alias a failure → probe reports "0 locked" when files ARE
    # locked. Pin the full signature explicitly.
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    h = kernel32.CreateFileW(path, GENERIC_DELETE, FILE_SHARE_NONE, None,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    # With restype=c_void_p a NULL handle arrives as None (not 0) — cover both
    # forms; INVALID_HANDLE_VALUE is c_void_p(-1).value (pm25coder review note,
    # PR #832). A failed DELETE open = the installer's DeleteFile would fail.
    if not h or h == ctypes.c_void_p(-1).value:
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


# Last lock-probe failure (rant 2026-08-18T09:40:40): a probe exception is
# NO LONGER silently swallowed as "clean" — it is recorded here, printed as
# `lock-probe ERROR`, and surfaced as a verify residual so the installer
# aborts instead of overwriting locked files. Reset on every probe attempt.
_lock_probe_error: str | None = None


def _install_root() -> str:
    """Windows install dir (``~/.emrg/install``) — single source of truth."""
    return os.path.join(os.path.expanduser("~"), ".emrg", "install")


def check_install_writable() -> list[str]:
    """Windows: probe ``install\\`` for files locked against overwrite.

    Independent of Restart Manager — the installer's DeleteFile would fail on
    every returned path. Returns [] when the probe is unavailable (POSIX, no
    install dir) — best-effort like every other stop step. On a PROBE ERROR
    (exception) it also returns [] (never raises) but records the failure in
    ``_lock_probe_error`` and prints ``lock-probe ERROR`` — verify() then
    surfaces it as a residual and the installer aborts (fail-closed), instead
    of the old silent ``except Exception: return []`` that reported
    ``0 locked`` while files were actually locked (rant 2026-08-18T09:40:40).
    """
    global _lock_probe_error
    _lock_probe_error = None
    if not is_win():
        return []
    root = _install_root()
    if not os.path.isdir(root):
        return []
    files = _iter_install_files(root)
    t0 = time.monotonic()
    try:
        locked = _check_locked_files(root)
    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        _lock_probe_error = f"{type(e).__name__}: {e}"
        print(
            f"emrg stop: lock-probe ERROR: {_lock_probe_error} "
            f"(scanned {len(files)} files, {elapsed:.0f}ms) — FAIL CLOSED"
        )
        return []
    elapsed = (time.monotonic() - t0) * 1000
    # Scanned-file count / elapsed observability (rant 2026-08-18T09:40:40):
    # "0 locked" is only trustworthy when the probe actually scanned files.
    print(
        f"emrg stop: createfile-probe scanned {len(files)} files "
        f"-> {len(locked)} locked ({elapsed:.0f}ms) [supplementary — DLL "
        "module locks need the module-holder scan (rant 2026-08-18T16:24:01)]"
    )
    return locked


def stop_lock_owners() -> None:
    """Windows only: stop every process holding a lock on files under install\\.

    Two detectors, in order (rant 2026-08-18T16:24:01 — diagnostic-script
    proof that neither RM nor CreateFileW probing can see DLL module locks):

    1. **module-holder enumeration** (PRIMARY): ``Get-Process`` +
       ``$_.Modules.FileName`` filtered by the install prefix names the
       actual processes that loaded DLLs/.pyd from install\\ (e.g. a
       browser-harness child that inherited install\\lib in PYTHONPATH →
       loaded websockets speedups). Each holder is killed with
       ``taskkill /F /T /PID`` (tree kill — the holder may be a child
       whose parent also must go).
    2. **Restart Manager** (auxiliary, rant 2026-08-17T17:55:42): finds ANY
       owner incl. non-EMRG processes. Self + ancestor chain excluded.
    """
    if not is_win():
        return
    # 1. module-holder enumeration — the only detector that can see DLL
    #    image-section locks (CreateFileW probes cannot, RM missed PID 9280).
    holders = find_install_module_holders()
    _module_holder_external = False
    for pid, name, exe, parent, files, tag in holders:
        _module_holder_external = _module_holder_external or tag == "target"
        if tag == "excluded":
            print(
                f"emrg stop: module-holder excluded PID {pid} {name} | exe {exe} "
                f"| parent {parent} | loads [{', '.join(files[:5])}]"
            )
            continue
        print(
            f"emrg stop: killing module-holder PID {pid} {name} | exe {exe} "
            f"| parent {parent} | loads [{', '.join(files[:5])}]"
        )
        _kill_tree_windows(pid)
        if "browser" in (name + " " + (exe or "")).lower():
            print("emrg stop: hint: browser-harness daemon stopped - restart it after the installer completes")
    if holders:
        print(
            f"emrg stop: module-holders {len(holders)} "
            f"(external targets: {int(_module_holder_external)})"
        )
    # 2. Restart Manager — auxiliary (catches non-DLL file locks).
    stdout = _lock_owner_ps(kill=True)
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            print(f"emrg stop: {line}")
    _print_rm_diag(stdout)
    global _module_holder_external_found
    _module_holder_external_found = _module_holder_external


# ── Verify + exit code ──────────────────────────────────────────

# Cache of the last _verify_windows_categories() result (rant
# 2026-08-18T09:40:40 #4): stop_all previously ran the FULL Windows verify
# TWICE per run — once via verify() and again via _verify_windows_summary()
# (two rm-scan PowerShell invocations, ~2s+ wasted, duplicated log lines).
# verify() always refreshes; _verify_windows_summary() reuses the freshest
# result when available.
_windows_cats_cache: list[tuple[str, list[str]]] | None = None


def _classify_locked_files(
    locked: list[str],
    mh_holders: list[tuple[int, str, str, int, list[str], str]],
    root: str,
) -> tuple[list[str], list[str]]:
    """Split createfile-probe locked files into self-held vs residual.

    Rant 2026-08-18T18:57:09: when stop_all itself runs from
    install\\python-dist\\python.exe, the probe reports the interpreter's own
    DLLs (python313.dll, select.pyd, ...) as locked — but those locks belong
    to the stop_all process (module-holder tag ``excluded``) and are released
    the moment stop_all exits, BEFORE the installer overwrites (installer runs
    stop_all synchronously via ewWaitUntilTerminated). Counting them as
    residuals aborts a perfectly fine install.

    Returns ``(self_held, residual)`` install-relative paths:
    - self_held: locked file attributed ONLY to excluded (self/ancestor) holders,
      or (rant 2026-08-18T21:24:48 #3) a file under ``python-dist\\`` with no
      external target holder — stop_all always runs from
      install\\python-dist\\python.exe, whose interpreter + lazily-loaded
      stdlib modules (select/_ctypes/_hashlib/_socket, ...) hold DLL locks
      that module-holder enumeration does NOT list (v0.2.49: 12 locked vs 5
      enumerated modules); such locks release when stop_all exits.
    - residual:  locked file with an external (``target``) holder, or one that
      cannot be attributed to any known holder (conservative — could be a
      plain non-DLL lock held by an external process that loaded no module).
    """
    if not locked or not root:
        return [], list(locked)
    # Separator-agnostic: module-holder files arrive with backslashes (PS
    # Substring), locked paths are native. Normalize both to forward slashes
    # so the attribution works identically on Windows and in POSIX unit tests.
    def _norm(p: str) -> str:
        return p.replace("\\", "/")

    root_n = _norm(root)
    def _to_rel(p: str) -> str:
        n = _norm(p)
        if n.startswith(root_n + "/"):
            # Normalize again: on Windows os.path.relpath() returns
            # backslash-separated results, but the locked-file lookup keys are
            # forward-slash — an un-normalized key would miss (external target
            # holder misclassified as self-held on Windows).
            return _norm(os.path.relpath(n, root_n))
        return n  # already install-relative (PS Substring / fixture form)

    tag_by_rel: dict[str, set[str]] = {}
    for _pid, _name, _exe, _parent, files, tag in mh_holders:
        for f in files:
            # Key by the SAME rel path the locked-file lookup uses below —
            # full-path keys never matched the rel lookup, so holder tags
            # (esp. ``target``) were lost and every python-dist file was
            # mis-attributed as self-held (test_pydist_external_target_is_residual).
            # Holder files may already be install-relative: os.path.relpath()
            # against root would mangle those on POSIX (CWD prefix), so only
            # convert genuine absolute paths.
            rel_f = _to_rel(f)
            tag_by_rel.setdefault(rel_f, set()).add(tag)
    self_held: list[str] = []
    residual: list[str] = []
    for p in locked:
        rel = _norm(os.path.relpath(_norm(p), root_n))
        tags = tag_by_rel.get(rel, set())
        if tags == {"excluded"}:
            self_held.append(rel)
        elif "/python-dist/" in "/" + rel and "target" not in tags:
            # Rant 2026-08-18T21:24:48 #3 — self-held relaxation: stop_all
            # itself runs from install\python-dist\python.exe; its runtime +
            # lazily-loaded stdlib modules hold python-dist DLL locks that the
            # module-holder enumeration does not cover (v0.2.49: 12 locked vs
            # 5 enumerated modules → 7 falsely "unattributable" → old
            # self-held check misjudged them as residual and aborted a fine
            # install). python-dist\ + no external target holder ⇒ self-held:
            # released when stop_all exits, installer continues.
            self_held.append(rel)
        else:
            # No tags (unattributable) OR has an external target holder.
            residual.append(rel)
    return self_held, residual


def _verify_windows_categories() -> list[tuple[str, list[str]]]:
    """Windows residual scan, one ``(category, residual_strings)`` entry per
    check — so the operator can see each check's result instead of guessing
    (rant 2026-08-17T21:06:31 #3). Result is cached in ``_windows_cats_cache``
    so _verify_windows_summary() does not re-run the expensive scan."""
    global _windows_cats_cache
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

    # file-lock owners under install\ — module-holder enumeration (PRIMARY,
    # rant 2026-08-18T16:24:01: the only detector that sees DLL/.pyd image-
    # section locks; RM missed the browser-harness child, CreateFileW probes
    # cannot see module locks at all). Any external holder = residual.
    mh_out = _module_holder_ps()
    mh_holders = _parse_module_holders(mh_out)
    mh = [
        f"install-module holder (pid {pid}, {name or 'unknown'}, loads {', '.join(files[:3])})"
        for pid, name, _exe, _parent, files, tag in mh_holders
        if tag == "target"
    ]
    cats.append(("module-holder", mh))

    # file-lock owners under install\ (Restart Manager — auxiliary generic
    # code-5 fix: covers ANY process holding locked files, incl. non-EMRG
    # ones such as the browser-harness daemon; self + ancestor chain
    # excluded; rant 2026-08-17T17:55:42)
    rm_out = _lock_owner_ps(kill=False)
    rm = [
        f"file-lock owner (pid {o_pid}, {name or 'unknown'})"
        for o_pid, name, _cmd in _windows_lock_owners(kill=False, stdout=rm_out)
    ]
    cats.append(("RM re-scan", rm))
    _print_rm_diag(rm_out)

    # install-writability probe — SUPPLEMENTARY ONLY (rant 2026-08-18T16:24:01:
    # a CreateFileW probe cannot see DLL module locks — DELETE+SHARE_NONE
    # reported OK yet DeleteFile still failed on the speedups pyd; the real
    # verdict comes from the module-holder category above). Kept because it
    # still catches plain (non-DLL) file locks and a broken RM. A probe
    # FAILURE is not "0 locked": it becomes a residual → installer aborts
    # (rant 2026-08-18T09:40:40 fail-closed).
    global _lock_probe_error
    _lock_probe_error = None
    locked = check_install_writable()
    probe_items = []
    if _lock_probe_error:
        probe_items.append(f"lock-probe failed (error: {_lock_probe_error})")
    # Self-held attribution (rant 2026-08-18T18:57:09 + 21:24:48 #3): when
    # stop_all runs from install\python-dist\python.exe, that interpreter MUST
    # load its own python-dist DLLs (python313.dll, select.pyd, ...) — those
    # image-section locks are held by the stop_all process itself (module-holder
    # tag ``excluded`` = self + ancestor chain) and are RELEASED when stop_all
    # exits, before the installer starts overwriting (make-installer.sh uses
    # ewWaitUntilTerminated). They are NOT residuals — counting them aborts
    # the install while nothing is actually wrong. Since 21:24:48 the same
    # holds for ANY locked file under python-dist\ with no external target
    # holder (lazily-loaded stdlib modules enumeration misses).
    self_held, residual_locked = _classify_locked_files(
        locked, mh_holders, _install_root() if is_win() else ""
    )
    # Final escalation (rant 2026-08-18T21:24:48 #2c): locks that survive the
    # stop phase get a hard re-kill of their external holders (taskkill /F /T →
    # parent tree → Stop-Process), then the probe + classification run again.
    # Only TRULY unkillable locks are logged — the install CONTINUES and the
    # installer's own overwrite is the final arbiter (21:24:48 #2c/#5).
    if residual_locked and is_win():
        _escalate_locked_files(locked, mh_holders, _install_root())
        locked = check_install_writable()
        if not _lock_probe_error:
            _self2, residual_locked = _classify_locked_files(
                locked, mh_holders, _install_root()
            )
            self_held = sorted(set(self_held + _self2))
    # Advisory (non-aborting) after escalation — detailed chain already logged
    # per file by _escalate_locked_files; exit stays 0 unless EMRG process
    # residuals exist (rant 2026-08-18T21:24:48).
    for p in residual_locked:
        probe_items.append(f"locked file (advisory, install continues): {p}")
    if self_held:
        print(
            f"emrg stop: WARNING {len(self_held)} file(s) locked by stop_all "
            f"runtime itself (python-dist DLL) — self-held, released when "
            f"stop_all exits; installer continues"
        )
    cats.append(("createfile-probe", probe_items))

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
    _windows_cats_cache = cats
    return cats


def _verify_windows_summary() -> str:
    """One-line per-category verify summary, e.g.
    ``GUI 0 / daemon 0 / cmdline-scan 0 / RM re-scan 0 / lock-probe 0 locked /
    bundled-git 0`` (rant 2026-08-17T21:06:31 #3).

    Reuses the freshest ``_verify_windows_categories()`` result when present
    (single-scan, rant 2026-08-18T09:40:40 #4); falls back to a fresh scan
    only when nothing has been cached yet.
    """
    cats = _windows_cats_cache if _windows_cats_cache is not None else _verify_windows_categories()
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

def _pythonpath_env() -> str:
    """User/Machine PYTHONPATH on Windows (registry), else the process env —
    observability for the "an unrelated python imports from install\\lib and
    locks C extensions" root-cause (rant 2026-08-18T09:40:40: browser-harness
    uses its own uv python but loaded install\\lib\\websockets — PYTHONPATH
    pollution would make ANY python process import from install\\lib)."""
    if not is_win():
        p = os.environ.get("PYTHONPATH", "")
        return f"PYTHONPATH={p or '(unset)'}"
    try:
        import winreg

        entries: list[str] = []
        for hive, key, label in (
            (winreg.HKEY_CURRENT_USER, r"Environment", "User"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", "Machine"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    val, _ = winreg.QueryValueEx(k, "PYTHONPATH")
                    entries.append(f"PYTHONPATH({label})={val}")
            except OSError:
                entries.append(f"PYTHONPATH({label})=(unset)")
        proc = os.environ.get("PYTHONPATH")
        if proc:
            entries.append(f"PYTHONPATH(process)={proc}")
        return " | ".join(entries)
    except Exception as e:  # registry read is best-effort
        return f"PYTHONPATH(registry read failed: {type(e).__name__}: {e})"


def _pythonpath_install_warning(line: str) -> str | None:
    """Warn when any PYTHONPATH references the install dir (C-extension lock
    root cause, rant 2026-08-18T09:40:40). Pure function → unit-testable.

    Matches only the install dir anchored on ``~/.emrg/install`` (both path
    separators) — bare ``install\\lib`` substrings are NOT matched, so an
    unrelated ``C:\\python\\install\\lib`` never warns spuriously (pm25coder
    review note, PR #832).
    """
    if not line or "PYTHONPATH" not in line or "(unset)" in line:
        return None
    lowered = line.lower()
    for marker in (r".emrg\install", r"/.emrg/install"):
        if marker in lowered:
            return ("PYTHONPATH references ~/.emrg/install — any python "
                    "process may import from install\\lib and lock C "
                    "extensions; clear it before running the installer")
    return None


class _Tee:
    """Duplicate write()/flush() to BOTH the original stdout and a log file
    (rant 2026-08-18T11:20:54). The Inno installer redirects stop_all stdout
    to a random temp dir ({tmp}\\stop_all.log) that is deleted when the
    install ends or is cancelled — the tee keeps a persistent fixed-path copy
    (~/.emrg/logs/stop_all-<ts>.log) for post-mortem forensics. All existing
    print() calls keep working untouched (they write to sys.stdout, which is
    replaced with a _Tee during stop_all)."""

    def __init__(self, orig, f):
        self.orig = orig
        self.f = f

    def write(self, data):
        self.orig.write(data)
        self.f.write(data)
        # Crash-safe: if the installer force-kills this process mid-write,
        # append-mode + per-write flush guarantees the fixed-path copy has
        # everything printed so far (rant: 不依赖 finally).
        self.f.flush()
        return len(data)

    def flush(self):
        self.orig.flush()
        self.f.flush()

    def isatty(self):
        return self.orig.isatty() if hasattr(self.orig, "isatty") else False

    def fileno(self):
        return self.orig.fileno()


def _open_stop_log() -> object | None:
    """Open the fixed-path dual-write log (rant 2026-08-18T11:20:54):
    ``~/.emrg/logs/stop_all-YYYYMMDD-HHMMSS.log`` (local time; the timestamp
    name makes concurrent stop_all runs naturally isolated). Returns the file
    object or None on any failure — best-effort, never breaks the stop flow.
    The handle closes naturally at process exit (no finally dependency)."""
    try:
        d = os.path.join(os.path.expanduser("~"), ".emrg", "logs")
        os.makedirs(d, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        return open(os.path.join(d, f"stop_all-{ts}.log"), "a", encoding="utf-8")
    except OSError:
        return None


def _caller_context() -> str:
    """Best-effort "who called emrg stop" line (rant 2026-08-19T13:11:34):
    parent pid + parent command line + our argv — so a post-mortem can
    answer "谁杀 daemon / 谁删文件" (which process invoked the stop chain).
    Pure stdlib; any failure degrades to the pid-only form, never raises."""
    ppid = os.getppid()
    parent = ""
    try:
        if is_win():
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={ppid}').CommandLine"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        else:
            out = subprocess.run(
                ["ps", "-o", "command=", "-p", str(ppid)],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        if out:
            parent = out.splitlines()[0][:160]
    except Exception:
        pass
    argv = " ".join(sys.argv) or "(none)"
    return f"caller pid {ppid} ({parent or 'unknown parent'}) | argv: {argv}"


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


def _is_lock_residual(r: str) -> bool:
    """True when a verify residual is lock-related (external lock holder or
    locked file) rather than an EMRG process residual (rant 2026-08-18T21:24:48
    #2c/#5: lock residuals are advisory after escalation — install continues;
    process residuals still abort)."""
    return r.startswith((
        "locked file",
        "install-module holder",
        "file-lock owner",
    ))


def stop_all() -> int:
    """Run every stop step, then verify. Returns 0 (clean) or 1 (residuals).

    Logging follows the standard from rant 2026-08-17T21:06:31: header with
    build stamp / python / platform / pid, ``[N/T] step -> result (elapsed)``
    per step, per-category verify summary, exit-code line with total elapsed,
    and NO silent failures (every step is wrapped so an exception still shows
    ``ERROR <step>: <reason>`` and the run continues to the final exit code).
    """
    t0 = time.monotonic()
    # Dual-write log to a fixed path (rant 2026-08-18T11:20:54): the Inno
    # {tmp} redirect vanishes when the install ends/cancels — tee a persistent
    # copy to ~/.emrg/logs/stop_all-<ts>.log. Every print below automatically
    # lands in both. The line is also printed so the Inno-side log and the
    # operator both see the fixed location.
    _log_f = _open_stop_log()
    if _log_f is not None:
        sys.stdout = _Tee(sys.stdout, _log_f)
        print(f"emrg stop: log also written to {_log_f.name}")
    print(
        f"emrg stop: stop_all.py {_STOP_ALL_STAMP} | "
        f"python {platform.python_version()} {platform.system()}-{platform.machine()} "
        f"| pid {os.getpid()}"
    )
    # Who called + when (rant 2026-08-19T13:11:34): every stop run must be
    # attributable — parent pid/parent cmdline/argv + wall-clock start. This
    # is the forensics trail for "谁杀 daemon / 谁删文件".
    print(f"emrg stop: started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"emrg stop: {_caller_context()}")
    # Self-lock observability (rant 2026-08-18T16:09:45): when the installer
    # runs stop_all with install\python-dist\python.exe, that interpreter's
    # site config (._pth/.pth) may import install\lib modules → the runtime
    # itself locks the very files it was asked to delete. Print the runtime
    # so the operator can tell at a glance whether the probe's locked files
    # are self-inflicted.
    _pydist = is_win() and "python-dist" in sys.executable.lower()
    print(f"emrg stop: self pid {os.getpid()} (python-dist runtime: {_pydist})")
    # User/Machine PYTHONPATH observability (rant 2026-08-18T09:40:40) — a
    # polluted PYTHONPATH makes any python process import from install\lib
    # and lock C extensions; surface it before any stop/kill logic.
    _pp = _pythonpath_env()
    print(f"emrg stop: {_pp}")
    _pp_warn = _pythonpath_install_warning(_pp)
    if _pp_warn:
        print(f"emrg stop: WARNING {_pp_warn}")
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
        locked: list[str] = []
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
        # Self-lock final guard (rant 2026-08-18T16:09:45 + 16:24:01, refined
        # 18:57:09): after both kill retries the probe still reports locked
        # files but neither the module-holder enumeration nor RM found an
        # EXTERNAL owner — the lock holder is stop_all's own runtime
        # (python-dist loaded install\lib modules). The installer runs stop_all
        # synchronously (ewWaitUntilTerminated), so these locks are released
        # when stop_all exits and the overwrite proceeds — advisory only, NOT
        # a hard abort (the pre-18:57:09 guard wrongly blocked installs whose
        # only locks were python-dist DLLs held by stop_all itself).
        if locked and not _module_holder_external_found and _rm_no_external_owner:
            print(
                f"emrg stop: WARNING {len(locked)} file(s) locked by the "
                f"stop_all runtime itself (python-dist DLL) — released when "
                f"stop_all exits; installer continues"
            )
    residuals = verify()
    # Lock-related residuals are ADVISORY after escalation (rant
    # 2026-08-18T21:24:48 #2c/#5): an unkillable external lock holder is
    # logged in detail and the install CONTINUES — the installer's own
    # overwrite is the final arbiter ("若仍有杀不掉的锁：日志详细记录但安装
    # 继续，最终成功与否以实际覆盖为准"). Only EMRG process residuals
    # (GUI / daemon / python-emrg / bundled-git) and probe failures abort.
    lock_res = [r for r in residuals if _is_lock_residual(r)]
    proc_res = [r for r in residuals if not _is_lock_residual(r)]
    # Advisory only when the install ACTUALLY continues: a process residual
    # below returns exit 1, so the "install continues" message would be a lie.
    if lock_res and not proc_res:
        print(
            f"emrg stop: WARNING {len(lock_res)} lock-related residual(s) "
            f"after escalation — install continues, overwrite is the final "
            f"arbiter (rant 21:24:48):"
        )
        for r in lock_res:
            print(f"  - {r}")
    if proc_res:
        print("emrg stop: WARNING residual process(es) still running:")
        for r in proc_res:
            print(f"  - {r}")
        if is_win():
            try:
                print("emrg stop: verify: " + _verify_windows_summary() + " -> RESIDUAL")
            except Exception:
                pass
        print(
            f"emrg stop: exit code 1 ({len(proc_res)} residual) "
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
