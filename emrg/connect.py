"""Platform-adaptive IPC connection layer for EMRG.

Maps the Claude Code pattern to Python's asyncio API:

    Claude Code (Node.js):
        net.connect() unified — same API, path format auto-selects UDS vs Named Pipe
            socket.connect('/tmp/xxx.sock')       → UDS
            socket.connect('\\\\.\\pipe\\xxx')    → Named Pipe

    EMRG (Python):
        asyncio does NOT unify — different functions per transport
            await asyncio.start_unix_server(path=...)    → UDS (Unix)
            await asyncio.open_unix_connection(...)      → UDS (Unix)
            await asyncio.start_server(path=...)          → Named Pipe (Windows)
            await asyncio.open_connection(...)            → Named Pipe (Windows)

        This module wraps the platform dispatch so callers never touch
        the raw functions. The pattern is the same as Claude Code:
        platform detect → native IPC.
"""

from __future__ import annotations

import asyncio
import logging
import os

from emrg.config import config_dir

logger = logging.getLogger(__name__)

# ── Connection identifier ───────────────────────────────────────
# Unix:  ~/.emrg/emrgd.sock
# Win32: \\.\pipe\emrgd
CONNECT_ID = "emrgd"


def get_server_path() -> str:
    """Return the server bind address, platform-adaptive."""
    if os.name == "nt":
        return rf"\\.\pipe\{CONNECT_ID}"
    return str(config_dir() / f"{CONNECT_ID}.sock")


async def start_server(
    handler,
    *,
    host: str | None = None,
    port: int | None = None,
) -> asyncio.AbstractServer:
    """Start the emrgd server, platform-adaptive.

    Unix:  asyncio.start_unix_server on CONNECT_ID.sock
    Win32: asyncio.start_server on CONNECT_ID (Named Pipe)
    """
    if os.name == "nt":
        server = await asyncio.start_server(
            handler,
            host=host,
            port=port,
            path=rf"\\.\pipe\{CONNECT_ID}",
        )
    else:
        sock_path = config_dir() / f"{CONNECT_ID}.sock"
        if sock_path.exists():
            sock_path.unlink()
        server = await asyncio.start_unix_server(handler, path=str(sock_path))
    return server


async def connect_to_server(
    *,
    host: str | None = None,
    port: int | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the emrgd server, platform-adaptive.

    Unix:  asyncio.open_unix_connection to CONNECT_ID.sock
    Win32: asyncio.open_connection to CONNECT_ID (Named Pipe)
    """
    if os.name == "nt":
        return await asyncio.open_connection(
            host=host,
            port=port,
            path=rf"\\.\pipe\{CONNECT_ID}",
        )
    sock_path = config_dir() / f"{CONNECT_ID}.sock"
    return await asyncio.open_unix_connection(str(sock_path))


def cleanup_server() -> None:
    """Cleanup server resources on shutdown.

    Unix:   remove the socket file only (PID file is managed by daemon)
    Win32:  nothing — named pipes are kernel-managed
    """
    if os.name == "nt":
        return
    sock_path = config_dir() / f"{CONNECT_ID}.sock"
    if sock_path.exists():
        sock_path.unlink()
        logger.debug("removed socket file: %s", sock_path)


def is_server_running_sync(timeout: float = 2.0) -> bool:
    """Synchronous health-check probe (for client startup).

    Uses blocking socket calls so it can be called before the asyncio
    event loop is running.

    Unix:   AF_UNIX connect to the socket file
    Win32:  AF_INET connect to 127.0.0.1:<port> … not used for named pipe
            (named pipe health check requires the asyncio loop; clients
             should use connect_to_server + try/except instead)

    NOTE: on Windows this function always returns False. The client
    should fall back to an async probe via connect_to_server().
    """
    if os.name == "nt":
        return False

    import socket as _socket

    sock_path = config_dir() / f"{CONNECT_ID}.sock"
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(sock_path))
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False
    finally:
        sock.close()
