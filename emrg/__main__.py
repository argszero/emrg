"""EMRG CLI entry point.

Usage:
    emrg                    Run client (auto-starts daemon if needed)
    emrg server             Run daemon in foreground
    emrg server stop        Stop the running daemon
    emrg server restart     Restart the daemon
    emrg update             git pull + reinstall from source
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from emrg import __version__
from emrg.connect import cleanup_server, connect_to_server


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
    )
    return proc


async def _send_shutdown() -> bool:
    """Send a graceful shutdown message to the daemon. Returns True on success."""
    try:
        reader, writer = await asyncio.wait_for(connect_to_server(), timeout=3)
    except (ConnectionRefusedError, FileNotFoundError, OSError, asyncio.TimeoutError):
        return False

    try:
        writer.write(json.dumps({"type": "shutdown"}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=3)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        if line:
            data = json.loads(line.decode().strip())
            return data.get("type") == "shutdown_ack"
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
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
            reader, writer = await asyncio.wait_for(connect_to_server(), timeout=3)
            writer.write(json.dumps({"type": "ping"}).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=3)
            writer.close()
            return json.loads(line.decode().strip()) if line else {}

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
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        print("daemon not running.")


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
    async def _do() -> None:
        try:
            reader, writer = await asyncio.wait_for(connect_to_server(), timeout=3)
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

        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()

        line = await asyncio.wait_for(reader.readline(), timeout=5)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

        if line:
            resp = json.loads(line.decode().strip())
            if resp.get("ok"):
                print(f"rant recorded ({resp.get('count', 0)} total). The evolution system will review it.")
            else:
                print(f"error: {resp.get('error', 'unknown')}")
        else:
            print("rant sent (no response).")

    asyncio.run(_do())


# ── Client ────────────────────────────────────────────────────

def _run_client(init_auto_evolve: bool = False) -> None:
    # Client logs go to ./.emrg/emrg-client.log
    log_dir = Path.cwd() / ".emrg"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "emrg-client.log"

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        filename=str(log_path),
        filemode="a",
    )

    from emrg.config import ensure_config
    from emrg.client.app import run_client

    ensure_config()
    run_client(init_auto_evolve=init_auto_evolve)


# ── Update ────────────────────────────────────────────────────

def _run_update() -> None:
    """git pull the latest source and reinstall via uv tool install."""
    source_dir = _find_source_dir()
    if source_dir is None:
        print("Error: cannot find emrg source directory.", file=sys.stderr)
        print(
            "Reinstall with: git clone https://github.com/argszero/emrg.git",
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
            timeout=10,
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

    Tries in order:
    1. Editable install: emrg.__file__ → parent → parent is the git repo
    2. Current directory: if user is inside the source tree
    """
    import emrg

    candidates: list[Path] = []

    # Editable install path
    pkg_dir = Path(emrg.__file__).resolve().parent  # emrg/emrg/
    candidates.append(pkg_dir.parent)  # emrg/

    # Current working directory (for wheel installs)
    candidates.append(Path.cwd())

    # Walk up from cwd (in case user is in a subdirectory)
    for p in Path.cwd().parents:
        candidates.append(p)

    for source_dir in candidates:
        git_dir = source_dir / ".git"
        if git_dir.exists():
            return source_dir

    return None


if __name__ == "__main__":
    main()
