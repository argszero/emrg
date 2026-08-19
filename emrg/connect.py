"""WebSocket-based IPC connection layer for EMRG (Phase 1).

Replaces the platform-adaptive Unix Domain Socket / Named Pipe transport
with a unified TCP loopback + WebSocket transport:

    ws://127.0.0.1:<port>   (local, all platforms)
    wss://<host>:<port>     (remote, Phase 5 — same protocol + TLS + token)

The daemon listens on the fixed port ``127.0.0.1:EMRGD_PORT`` (56031, rant
2026-08-19T08:05:21 — fixed-port bind exclusivity is the single-instance
admission) and writes its auth token to ``~/.emrg/emrgd.port``
(``port\\n token``, mode 0o600). Clients read that file for the token, and
connect to the fixed port.
Clients then send
a first-frame auth message; the daemon confirms with ``auth_ok`` before the
normal protocol loop. Auth failure raises :class:`AuthError` so callers can
distinguish it from a transient disconnect (which should be retried).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket as _socket
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from emrg.config import config_dir

logger = logging.getLogger(__name__)

# ── Connection identifier ───────────────────────────────────────
# Port/token file lives at ~/.emrg/emrgd.port (port\n token, mode 0o600)
CONNECT_ID = "emrgd"

# Fixed daemon port (host rant 2026-08-19T08:05:21): the daemon binds a FIXED
# loopback port so kernel-level bind exclusivity (EADDRINUSE) is the single-
# instance admission — no PID file to forge/delete, no race window. The port
# file still carries the auth token; the port itself is now a constant.
# Keep in sync with emrg._stop_all._EMRGD_PORT (that module is pure stdlib).
EMRGD_PORT = 56031


class AuthError(Exception):
    """Raised when the daemon rejects the auth handshake.

    Distinct from transient connection failures: a token mismatch is a
    configuration/install problem, not something reconnection can fix.
    """


def get_server_path() -> str:
    """Return the path of the daemon port/token file."""
    return str(config_dir() / f"{CONNECT_ID}.port")


async def connect_to_server():
    """Connect to the emrgd server over WebSocket.

    Reads the auth token from ``~/.emrg/emrgd.port``, connects to the FIXED
    daemon port ``ws://127.0.0.1:<EMRGD_PORT>`` (rant 2026-08-19T08:05:21 —
    the port is a constant; the file only carries the token), sends the
    first-frame auth message and waits for the ``auth_ok`` confirmation.
    Returns the connected WebSocket object (single ws — no
    ``(reader, writer)`` tuple anymore).

    Raises:
        AuthError: auth rejected (bad/missing token, or daemon version mismatch).
        ConnectionRefusedError / OSError / FileNotFoundError: daemon not running.
    """
    port_path = Path(get_server_path())
    _, token = port_path.read_text(encoding="utf-8").split()
    # proxy=None: loopback connections must never go through a system proxy.
    # websockets 17 defaults proxy=True and reads the OS proxy settings — when a
    # Windows system proxy is enabled (e.g. 10.10.0.28:6501 for HN/Reddit access),
    # the ws://127.0.0.1 handshake is sent to the proxy → InvalidMessage → all
    # Python clients (TUI `emrg`, scheduler internal connections) cannot reach the
    # local daemon, while the Node.js GUI is unaffected (2026-08-14 incident; root
    # cause of continuous emrg-task/emrg-promote-task crashes since 2026-08-13).
    ws = await connect(
        f"ws://127.0.0.1:{EMRGD_PORT}",
        proxy=None,
        max_size=16 * 1024 * 1024,
    )
    await ws.send(json.dumps({"type": "auth", "token": token}))
    # Wait for auth_ok: received → ready; ConnectionClosed (rejected) /
    # timeout (no response) → AuthError.
    try:
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    except (ConnectionClosed, asyncio.TimeoutError):
        await ws.close()
        raise AuthError("authentication failed — check token / daemon version")
    if ack.get("type") != "auth_ok":
        await ws.close()
        raise AuthError(f"unexpected auth response: {ack!r}")
    return ws


def cleanup_server() -> None:
    """Remove the daemon port/token file on shutdown."""
    port_path = Path(get_server_path())
    if port_path.exists():
        port_path.unlink()
        logger.debug("removed port file: %s", port_path)


def is_server_running_sync(timeout: float = 2.0) -> bool:
    """Synchronous health-check probe (for client startup).

    Blocking TCP connect to the FIXED daemon port ``127.0.0.1:EMRGD_PORT``
    (rant 2026-08-19T08:05:21). No port-file read: the fixed port is the
    ground truth, so a missing/stale ``emrgd.port`` never makes the probe
    report "not running" while a daemon is actually alive (the dual-instance
    root cause). Real auth happens on the first frame of a real connection.
    """
    sock = None
    try:
        sock = _socket.create_connection(("127.0.0.1", EMRGD_PORT), timeout=timeout)
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        if sock is not None:
            sock.close()
