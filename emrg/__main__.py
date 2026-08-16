"""EMRG CLI entry point.

Usage:
    emrg                    Run client (auto-starts daemon if needed)
    emrg server             Run daemon in foreground
    emrg server stop        Stop the running daemon
    emrg server restart     Restart the daemon
    emrg stop               Stop ALL running emrg processes (daemon, TUI, GUI)
    emrg update             git pull + reinstall from source
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from emrg import __version__
from emrg._win import win32_no_window_kwargs
from emrg.connect import cleanup_server, connect_to_server
from websockets.exceptions import ConnectionClosed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emrg",
        description=(
            "EMRG — a self-evolving AI coding agent.\n\n"
            "Run 'emrg' without arguments to start the interactive TUI\n"
            "(reads files, runs commands, makes edits, learns from feedback)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run 'emrg <command> --help' for more on a specific command.",
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"emrg {__version__}"
    )
    parser.add_argument(
        "--init-auto-evolve",
        action="store_true",
        help="Enable auto-evolution for the current project on connect.",
    )

    sub = parser.add_subparsers(dest="command", metavar="[command]")

    # emrg server [action]
    server_parser = sub.add_parser(
        "server",
        help="Manage the EMRG daemon",
        description="Manage the EMRG daemon lifecycle.",
    )
    server_actions = server_parser.add_subparsers(dest="server_action")

    server_actions.add_parser(
        "stop", help="Stop the running daemon", description="Stop the running EMRG daemon."
    )
    server_actions.add_parser(
        "restart",
        help="Restart the daemon",
        description="Stop the running daemon and start a new one.",
    )
    # (no action = foreground run, handled in main())

    # emrg stop
    sub.add_parser(
        "stop",
        help="Stop ALL running emrg processes (daemon, TUI, GUI)",
        description="Stop every running emrg process: the daemon, TUI clients and "
        "the GUI app. Graceful stop first, force-kill stragglers. Used by the "
        "Windows installer pre-stop (stop-emrg.cmd step [0]).",
    )

    # emrg update
    sub.add_parser(
        "update",
        help="Update emrg (git pull + reinstall from source)",
        description="Update emrg by pulling latest source and reinstalling.",
    )

    # emrg rant [--project <name>] <message>
    rant_parser = sub.add_parser(
        "rant",
        help="Send feedback/complaint to EMRG for evolution analysis",
        description="Send a rant (feedback, complaint, suggestion) that the "
        "evolution system will use to discover improvement opportunities.",
    )
    rant_parser.add_argument(
        "-p", "--project",
        help="Target project (from projects.yml). Omit for emrg itself.",
        default=None,
    )
    rant_parser.add_argument(
        "message", nargs="+", help="Your rant/feedback/suggestion",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    parsed = parser.parse_args()

    init_auto_evolve = getattr(parsed, "init_auto_evolve", False)

    if parsed.command == "server":
        if parsed.server_action == "stop":
            _stop_daemon()
        elif parsed.server_action == "restart":
            _restart_daemon()
        else:
            _run_daemon()
    elif parsed.command == "rant":
        _send_rant(" ".join(parsed.message), project=parsed.project)
    elif parsed.command == "stop":
        _stop_all()
    elif parsed.command == "update":
        _run_update()
    else:
        _run_client(init_auto_evolve=init_auto_evolve)


# ── Daemon lifecycle ────────────────────────────────────────────

def _start_daemon_background() -> subprocess.Popen:
    """Start the daemon as a background subprocess. Returns the Popen handle."""
    cleanup_server()
    proc = subprocess.Popen(
        [sys.executable, "-m", "emrg.server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        # Windows: background daemon spawn must not pop a console window
        # (rant 2026-08-09T13:16:36 — cmd-window storm).
        **win32_no_window_kwargs(),
    )
    return proc


async def _send_shutdown() -> bool:
    """Send a graceful shutdown message to the daemon. Returns True on success."""
    try:
        ws = await asyncio.wait_for(connect_to_server(), timeout=3)
    except (ConnectionRefusedError, FileNotFoundError, OSError, asyncio.TimeoutError):
        return False

    try:
        await ws.send(json.dumps({"type": "shutdown"}, ensure_ascii=False))
        frame = await asyncio.wait_for(ws.recv(), timeout=3)
        try:
            await ws.close()
        except Exception:
            pass
        data = json.loads(frame)
        return data.get("type") == "shutdown_ack"
    except (ConnectionClosed, OSError, asyncio.TimeoutError, json.JSONDecodeError):
        return False
    return False


def _stop_daemon() -> None:
    """Stop the running emrg daemon gracefully (shutdown msg, fallback to SIGTERM)."""
    # Try graceful shutdown via protocol
    if asyncio.run(_send_shutdown()):
        print("daemon stopped (graceful shutdown).")
        return

    # Fallback: SIGTERM via ping PID
    try:
        async def _get_pid():
            ws = await asyncio.wait_for(connect_to_server(), timeout=3)
            await ws.send(json.dumps({"type": "ping"}, ensure_ascii=False))
            frame = await asyncio.wait_for(ws.recv(), timeout=3)
            try:
                await ws.close()
            except Exception:
                pass
            return json.loads(frame) if frame else {}

        info = asyncio.run(_get_pid())
        pid = info.get("pid", 0)
        if pid:
            print(f"stopping daemon (pid={pid}) via SIGTERM ...")
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.15)
                except OSError:
                    break
            cleanup_server()
            print("daemon stopped.")
        else:
            print("daemon not running (no pid from ping).")
    except (ConnectionClosed, OSError, asyncio.TimeoutError, json.JSONDecodeError):
        print("daemon not running.")


# ── Stop everything (`emrg stop`) ──────────────────────────────

_EMRG_CLIENT_RE = re.compile(r"-m\s+emrg(\.server)?(\s|$)")


def _match_emrg_client(cmd: str) -> bool:
    """True if a process command line belongs to an emrg process (TUI/daemon/GUI).

    Matches:
      - `python -m emrg`              (TUI client)
      - `python -m emrg.server`       (daemon; protocol/pid stop may have missed it)
      - `/Applications/EMRG.app/...`  (macOS GUI)
    Does NOT match lookalikes like `-m emrg.serverless` or `-m emrgx`.
    """
    if "EMRG.app" in cmd:
        return True
    return bool(_EMRG_CLIENT_RE.search(cmd))


def _scan_emrg_client_pids(ps_output: str, own_pid: int) -> list[int]:
    """Parse `ps -axww -o pid=,command=` output → pids of emrg processes.

    `own_pid` is excluded so `emrg stop` (itself `python -m emrg stop`) never
    kills the CLI that is running it.
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
        if _match_emrg_client(parts[1]):
            pids.append(pid)
    return pids


def _stop_pids(pids: list[int]) -> list[int]:
    """Graceful SIGTERM → short grace → SIGKILL. Returns pids that survived."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    alive: list[int] = []
    for _ in range(20):  # ~3s grace window
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        if not alive:
            break
        time.sleep(0.15)
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return alive


def _stop_posix_clients() -> None:
    """Kill TUI/GUI emrg client processes on POSIX (ps scan + SIGTERM/SIGKILL)."""
    try:
        out = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=10,
            **win32_no_window_kwargs(),
        ).stdout
    except (OSError, subprocess.SubprocessError, TimeoutError):
        print("emrg stop: could not scan processes (ps unavailable).")
        return
    pids = _scan_emrg_client_pids(out, os.getpid())
    if not pids:
        print("emrg stop: no other emrg client processes found.")
        return
    print(f"emrg stop: found {len(pids)} client process(es), stopping ...")
    survivors = _stop_pids(pids)
    if survivors:
        print(f"emrg stop: WARNING {len(survivors)} process(es) survived SIGKILL: {survivors}")
    else:
        print("emrg stop: client processes stopped.")


def _stop_windows_clients() -> None:
    """Kill GUI (EMRG.exe) + TUI (python -m emrg, excluding daemon) on Windows.

    Mirrors bin/stop-emrg.cmd steps [1]/[2]: graceful GUI stop then unconditional
    /F fallback (host 2026-08-10T01:27:07Z lesson), TUI via PowerShell command
    line filter (wmic-free, Win11 24H2+ safe).
    """
    kw = win32_no_window_kwargs()
    # GUI: graceful first, then unconditional force (no survivor gate)
    subprocess.run(["taskkill", "/IM", "EMRG.exe"], capture_output=True, **kw)
    time.sleep(0.5)
    subprocess.run(["taskkill", "/F", "/IM", "EMRG.exe"], capture_output=True, **kw)
    # TUI: python.exe running `-m emrg` but NOT `emrg.server` (daemon)
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match '-m emrg' -and "
        "$_.CommandLine -notmatch 'emrg\\.server' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_cmd],
        capture_output=True, **kw,
    )


def _stop_all() -> None:
    """Stop every running emrg process: daemon, TUI, GUI.

    Graceful stop first, force-kill stragglers — the CLI counterpart of
    bin/stop-emrg.cmd (host request 2026-08-15: `emrg stop` must check all
    open emrg TUI/GUI/server processes and stop them all).
    """
    print("emrg stop: stopping daemon ...")
    _stop_daemon()
    if sys.platform == "win32":
        _stop_windows_clients()
    else:
        _stop_posix_clients()
    print("emrg stop: done.")


def _restart_daemon() -> None:
    """Stop and restart the daemon."""
    print("restarting daemon ...")
    _stop_daemon()

    # Wait a beat for the old socket to be cleaned up
    time.sleep(0.3)

    proc = _start_daemon_background()
    print(f"daemon started (pid={proc.pid}).")


# ── Foreground daemon ───────────────────────────────────────────

def _run_daemon() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy httpcore/httpx DEBUG logs (rant #24)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from emrg.config import load_config

    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    from emrg.server.daemon import run_server

    asyncio.run(run_server(config.llm))


# ── Rant ──────────────────────────────────────────────────────

def _send_rant(message: str, project: str | None = None) -> None:
    """Send a rant/feedback message to the daemon for evolution analysis."""
    # Parse @project from message if -p/--project not explicitly set
    # (matching TUI /rant @project behavior — app.py:1365-1369)
    if project is None and message.startswith("@"):
        parts = message.split(None, 1)
        project = parts[0][1:]  # strip @
        message = parts[1] if len(parts) > 1 else ""

    async def _do() -> None:
        try:
            ws = await asyncio.wait_for(connect_to_server(), timeout=3)
        except (ConnectionError, FileNotFoundError, OSError, asyncio.TimeoutError):
            print("daemon not running. Start it first with: emrg")
            return

        payload: dict = {
            "type": "rant",
            "message": message,
            "timestamp": datetime.now().isoformat(),
        }
        if project:
            payload["project"] = project

        await ws.send(json.dumps(payload, ensure_ascii=False))

        frame = await asyncio.wait_for(ws.recv(), timeout=5)
        try:
            await ws.close()
        except Exception:
            pass

        resp = json.loads(frame)
        if resp.get("ok"):
            print(f"rant recorded ({resp.get('count', 0)} total). The evolution system will review it.")
        else:
            print(f"error: {resp.get('error', 'unknown')}")

    asyncio.run(_do())


# ── Client ────────────────────────────────────────────────────

def _run_client(init_auto_evolve: bool = False) -> None:
    # Client logs go to ./.emrg/emrg-client.log
    log_dir = Path.cwd() / ".emrg"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "emrg-client.log"

    from logging.handlers import RotatingFileHandler

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            RotatingFileHandler(
                str(log_path), maxBytes=10 * 1024 * 1024, backupCount=3,
                # encoding="utf-8" — symmetric with the daemon's #556 fix:
                # the default locale code page (GBK on zh-CN Windows) cannot
                # encode U+FFFD and logging.emit would crash with "--- Logging
                # error ---", polluting the shared TUI terminal. errors=
                # "backslashreplace" is defense-in-depth: logging must never
                # crash on exotic characters (rant 2026-08-08T09:35:30).
                encoding="utf-8", errors="backslashreplace",
            ),
        ],
    )

    # Suppress noisy third-party DEBUG logs
    logging.getLogger("markdown_it").setLevel(logging.WARNING)

    from emrg.config import ensure_config
    from emrg.client.app import run_client

    ensure_config()
    run_client(init_auto_evolve=init_auto_evolve)


# ── Update ────────────────────────────────────────────────────

def _run_update() -> None:
    """git pull the latest source and reinstall via uv tool install.

    Packaged mode (rant #12 §9 R86): when no source dir is detectable the
    binary install cannot self-update — print a pointer to GitHub Releases
    and exit (v1.1 adds binary self-update).
    """
    source_dir = _find_source_dir()
    if source_dir is None:
        print(
            "EMRG is installed in packaged mode — self-update is not supported yet.\n"
            "Please download the new version from GitHub Releases:\n"
            "  https://github.com/argszero/emrg/releases",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"source: {source_dir}")

    # Step 0: stop the running daemon (if any)
    _stop_daemon()

    # Step 1: git pull (10s timeout, skip if stuck)
    print("→ git pull ...")
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            **win32_no_window_kwargs(),
        )
        if result.returncode != 0:
            print(f"git pull failed:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)
        print(result.stdout.strip() or "Already up to date.")
    except subprocess.TimeoutExpired:
        print("git pull timed out (>10s), skipping to install ...")

    # Step 2: reinstall
    print("→ uv tool install --reinstall -e . ...")
    result = subprocess.run(
        ["uv", "tool", "install", "--reinstall", "-e", "."],
        cwd=source_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        **win32_no_window_kwargs(),
    )
    if result.returncode != 0:
        print(f"reinstall failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(result.stdout.strip())

    # Step 3: show version info
    print(
        f"emrg {__version__} — update complete (daemon will restart on next emrg)"
    )


def _find_source_dir() -> Path | None:
    """Find the emrg source directory (the git repo root).

    R86 (rant #12 §9): only the emrg package's parent directory is a valid
    source dir. NEVER fall back to the current working directory or walk up
    from cwd — in packaged mode a user running ``emrg update`` from any git
    repo would otherwise have an unrelated repo pulled/upgraded (dangerous).
    """
    import emrg

    # Editable install path: emrg.__file__ → parent → parent is the git repo
    pkg_dir = Path(emrg.__file__).resolve().parent  # .../site-packages/emrg/
    source_dir = pkg_dir.parent  # repo root when installed with -e .
    git_dir = source_dir / ".git"
    if git_dir.exists():
        return source_dir
    return None


if __name__ == "__main__":
    main()
