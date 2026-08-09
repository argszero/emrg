"""Daemon 生命周期管理 + 协议客户端封装。供 TUI（app.py）与 GUI（Phase 3）共用。

分层：
    app.py / GUI → daemon_manager.py → connect.py → websockets

- connect.py 是传输层（建连 + token 握手 + 健康探测），零改动。
- daemon_manager.py 是生命周期 + 协议层，只 import connect/protocol，不 import app.py（防循环）。
- server 端（daemon.py）零改动。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from emrg._win import win32_no_window_kwargs
from emrg.connect import (
    AuthError,
    cleanup_server,
    connect_to_server,
    get_server_path,
    is_server_running_sync,
)
from emrg.protocol import TaskRequest
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


# ── daemon 生命周期（从 app.py:26-141 原样搬迁 + 改名）─────────────────

def _get_server_source_mtime() -> float:
    """Get the mtime of the newest .py file in the emrg package — used to detect code changes."""
    import glob as _glob
    emrg_dir = Path(__file__).parent.parent  # emrg/
    max_mtime = 0.0
    for py in _glob.glob(str(emrg_dir / "**/*.py"), recursive=True):
        try:
            mtime = os.stat(py).st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            pass
    return max_mtime


def _get_config_mtime() -> float:
    """Get the mtime of ~/.emrg/config.toml — used to detect config changes.

    Returns 0.0 if config doesn't exist (it's optional).
    """
    from emrg.config import config_path as _config_path
    cfg = _config_path()
    try:
        return os.stat(cfg).st_mtime
    except OSError:
        return 0.0


def is_running() -> bool:
    """Synchronous liveness probe — is the daemon accepting connections?"""
    return is_server_running_sync()


async def start_daemon() -> subprocess.Popen:
    """Start emrgd in the background and wait until it accepts connections."""
    logger.info("starting emrgd daemon...")
    cleanup_server()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "emrg.server",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
        # Windows: daemon spawn must never pop a console window
        # (rant 2026-08-09T13:16:36 — cmd-window storm).
        **win32_no_window_kwargs())
    for _ in range(15):
        await asyncio.sleep(0.3)
        if is_running():
            logger.info("emrgd started (pid=%d)", proc.pid)
            return proc
    # R124: 超时后读取 emrgd.log 尾部打印真实失败原因（rant 2026-08-05T15:54:28 关联：
    # config.toml 解析错误时 CLI 只显示 'failed to start within timeout'，吞掉真实报错）
    tail = _read_log_tail(Path.home() / ".emrg" / "emrgd.log", lines=15)
    detail = f"\n  emrgd.log 尾部:\n{tail}" if tail else ""
    raise RuntimeError(f"emrgd failed to start within timeout{detail}")


def _read_log_tail(path: Path, lines: int = 15) -> str:
    """Return the last `lines` of a log file (empty string on any error)."""
    try:
        if not path.exists():
            return ""
        data = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(data.rstrip().splitlines()[-lines:])
    except OSError:
        return ""


async def check_and_restart_if_stale() -> None:
    """Ping the server. If source has changed since server started, restart it.

    ⚠️ 内部保持裸 ws 操作（connect_to_server → ws.send/ws.recv/ws.close），
    不用 DaemonConnection——此时连接还没建立。ping 是【发-读配对】语义：
    必须读到带 started_at/pid 的 pong 才能判断是否重启。
    """
    server_path = get_server_path()

    # Port file check (daemon not started yet → fresh start via connect_to_server)
    if not Path(server_path).exists():
        return

    source_mtime = _get_server_source_mtime()
    config_mtime = _get_config_mtime()

    try:
        ws = await connect_to_server()
        await ws.send(json.dumps({"type": "ping"}))
        frame = await asyncio.wait_for(ws.recv(), timeout=3)
        try:
            await ws.close()
        except Exception:
            pass

        if frame is None:
            return

        data = json.loads(frame)
        started_at = data.get("started_at", "")
        server_pid = data.get("pid", 0)

        if started_at:
            try:
                server_start = datetime.fromisoformat(started_at).timestamp()
            except (ValueError, TypeError):
                server_start = 0

            restart_reason = ""
            if source_mtime > server_start:
                restart_reason = f"source changed (src={source_mtime:.0f} > server={server_start:.0f})"
            elif config_mtime > server_start:
                restart_reason = f"config.toml changed (cfg={config_mtime:.0f} > server={server_start:.0f})"

            if restart_reason:
                logger.info(
                    "%s, restarting (old pid=%d)", restart_reason, server_pid,
                )
                # Kill old server: SIGTERM first, SIGKILL if still alive
                try:
                    os.kill(server_pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
                cleanup_server()
                # Wait for old server to die
                for _ in range(10):
                    await asyncio.sleep(0.2)
                    if not is_running():
                        break
                else:
                    # SIGTERM didn't work — force kill
                    logger.warning("old daemon (pid=%d) didn't die, sending SIGKILL", server_pid)
                    try:
                        os.kill(server_pid, signal.SIGKILL)
                        await asyncio.sleep(0.3)
                    except (ProcessLookupError, OSError):
                        pass
    except (ConnectionRefusedError, FileNotFoundError, OSError, json.JSONDecodeError,
            asyncio.TimeoutError, ConnectionClosed):
        # G129 (rant 2026-08-09T08:03:46): only genuinely transient connection
        # failures are swallowed here — connect_to_server in ensure_connected()
        # will surface the real error. AuthError and programming errors are NOT
        # in this list: a token mismatch is a config/install problem the user
        # must see (previously hidden by a bare `except Exception`).
        logger.debug("stale check: server not reachable — connect_to_server will handle")
        pass


async def ensure_connected() -> "DaemonConnection":
    """Ensure a daemon is running and return a DaemonConnection wrapping the ws.

    内部改名：check_and_restart_if_stale / is_running / start_daemon。
    """
    await check_and_restart_if_stale()
    if not is_running():
        cleanup_server()
        await start_daemon()
    return DaemonConnection(await connect_to_server())


# ── 协议客户端封装 ─────────────────────────────────────────────────────

class DaemonConnection:
    """Protocol client over a single websockets connection.

    Wraps send/recv in typed helpers. ``recv`` never raises on timeout or
    bad frames (returns None); ``ConnectionClosed`` propagates so callers
    can implement their own reconnect/teardown semantics.
    """

    def __init__(self, ws):
        self._ws = ws

    async def send_task(self, session_id: str, cwd: str, prompt: str,
                        stream: bool = True, images: list | None = None) -> None:
        """聊天发送：TaskRequest(type="task")。images 支持 /image 粘贴图。

        内部 json.dumps(req.to_dict(), ensure_ascii=False) 以 str 发送（不 .encode()）。
        """
        req = TaskRequest(session_id=session_id, cwd=cwd, prompt=prompt, stream=stream)
        if images:
            req.images = images
        await self._ws.send(json.dumps(req.to_dict(), ensure_ascii=False))

    async def send_command(self, type_: str, **params) -> None:
        """通用命令：ping/list_*/set_*/rant/compact/... 只发不读。

        内部 json.dumps({"type": type_, **params}, ensure_ascii=False)。
        ⚠️ params 不得含 type 键（dict 展开后者覆盖前者）。
        """
        await self._ws.send(json.dumps({"type": type_, **params}, ensure_ascii=False))

    async def recv(self, timeout: float | None = None) -> dict | None:
        """单帧读取。

        - timeout=None：阻塞直到有帧（asyncio.wait_for(coro, None) 无超时）
        - timeout=N：超时返回 None（静默，不抛）
        - 坏 JSON/空帧/空白帧：返回 None + log warning（不返回 error dict）
        - ConnectionClosed：不捕获，向上传播（R11——否则断线重连功能被破坏）
        """
        try:
            frame = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if frame is None:
            return None
        text = frame.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("bad JSON frame ignored: %.120r", text[:120])
            return None

    async def read_stream(self) -> AsyncIterator[dict]:
        """事件流 yield 每帧（GUI 桥接用，Phase 2 预留）。

        recv(None) = asyncio.wait_for(coro, None) 无超时阻塞。
        """
        while True:
            data = await self.recv(None)
            if data is None:
                continue
            yield data

    async def close(self) -> None:
        await self._ws.close()
