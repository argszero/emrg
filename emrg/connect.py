"""WebSocket-based IPC connection layer for EMRG (Phase 1).

Replaces the platform-adaptive Unix Domain Socket / Named Pipe transport
with a unified TCP loopback + WebSocket transport:

    ws://127.0.0.1:<port>   (local, all platforms)
    wss://<host>:<port>     (remote, Phase 5 — same protocol + TLS + token)

The daemon writes its dynamic port and auth token to ``~/.emrg/emrgd.port``
(``port\\n token``, mode 0o600). Clients read that file, connect, and send
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

    Reads ``~/.emrg/emrgd.port``, connects to ``ws://127.0.0.1:<port>``,
    sends the first-frame auth message and waits for the ``auth_ok``
    confirmation. Returns the connected WebSocket object (single ws — no
    ``(reader, writer)`` tuple anymore).

    Raises:
        AuthError: auth rejected (bad/missing token, or daemon version mismatch).
        ConnectionRefusedError / OSError / FileNotFoundError: daemon not running.
    """
    port_path = Path(get_server_path())
    port, token = port_path.read_text(encoding="utf-8").split()
    ws = await connect(
        f"ws://127.0.0.1:{port}",
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

    Uses blocking TCP connect to the port in ``emrgd.port``. Reads only the
    first line (port), never the token — this is a low-cost liveness probe;
    real auth happens on the first frame of a real connection.
    """
    port_path = Path(get_server_path())
    try:
        port = int(port_path.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError):
        return False
    sock = None
    try:
        sock = _socket.create_connection(("127.0.0.1", port), timeout=timeout)
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        if sock is not None:
            sock.close()
