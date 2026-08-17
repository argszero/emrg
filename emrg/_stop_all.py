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

- daemon:     ws protocol ``shutdown`` → ``~/.emrg/emrgd.pid`` → SIGTERM /
              ``taskkill /F /PID`` → 3s poll; port file removed once dead
- GUI:        Windows ``taskkill /IM EMRG.exe`` graceful → unconditional
              ``/F``; POSIX ps-scan (``EMRG.app`` / ``EMRG-*.AppImage``)
- TUI:        Windows CIM filter ``python.exe|pythonw.exe -m emrg`` (not
              ``emrg.server``); POSIX ps-scan
- bundled git: Windows ``install\\git\\`` prefix kill (git/ssh/plink/bash
              + fallback prefix full-kill — port of stop-emrg.cmd step 4)
- verify:     residual scan; any survivor → ``exit 1`` with a named list
              (installer aborts and shows the log, R125 semantics)
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

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
                for _ in range(20):
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
        # poll up to 3s for it to disappear
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.15)

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
    ``-m emrg`` but NOT ``emrg.server``); POSIX ps-scan."""
    if is_win():
        ps_cmd = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -match '^python(\\.exe|w\\.exe)?$' -and "
            "$_.CommandLine -match '-m emrg' -and "
            "$_.CommandLine -notmatch 'emrg\\.server' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
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


# ── Verify + exit code ──────────────────────────────────────────

def _verify_windows() -> list[str]:
    residuals: list[str] = []
    # GUI residual
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq EMRG.exe"],
            capture_output=True, text=True, timeout=10, **_no_window(),
        ).stdout
        for m in re.finditer(r"EMRG\.exe\s+(\d+)", out):
            residuals.append(f"EMRG.exe (pid {m.group(1)})")
    except (OSError, subprocess.SubprocessError, TimeoutError):
        pass
    # daemon residual (emrgd.pid still alive)
    pid = _read_pid_file()
    if pid is not None and _pid_alive(pid):
        residuals.append(f"daemon (pid {pid})")
    # bundled-git residual
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
                residuals.append(f"bundled-git {line}")
    except (OSError, subprocess.SubprocessError, TimeoutError):
        pass
    return residuals


def _verify_posix() -> list[str]:
    return [f"emrg process (pid {pid})" for pid in _stop_scan_pids(os.getpid())]


def verify() -> list[str]:
    """Scan for residual emrg processes. Returns a list of human-readable
    ``"name (pid N)"`` entries (empty = clean)."""
    return _verify_windows() if is_win() else _verify_posix()


# ── Orchestration ───────────────────────────────────────────────

def stop_all() -> int:
    """Run every stop step, then verify. Returns 0 (clean) or 1 (residuals)."""
    print("emrg stop: stopping daemon ...")
    stop_daemon()
    print("emrg stop: stopping GUI ...")
    stop_gui()
    print("emrg stop: stopping TUI clients ...")
    stop_tui()
    if is_win():
        print("emrg stop: stopping bundled git under install\\git ...")
        stop_bundled_git()
    residuals = verify()
    if residuals:
        print("emrg stop: WARNING residual process(es) still running:")
        for r in residuals:
            print(f"  - {r}")
        return 1
    print("emrg stop: all emrg processes stopped.")
    return 0


def main() -> None:
    code = stop_all()
    sys.exit(code)


if __name__ == "__main__":
    main()
