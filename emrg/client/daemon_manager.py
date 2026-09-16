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


# Rant 2026-08-09T13:16:36 ⑤（防风暴总闸）：daemon 启动失败时不得无限重拉——
# TUI app.py _reconnect 循环每 1s 调 ensure_connected → start_daemon 会每 1s
# spawn 一个新 daemon 进程（Windows 上每个 spawn 都是 cmd 窗口来源）。单个
# "连接生命周期"内最多 _MAX_SPAWN_ATTEMPTS 次 spawn，超限抛错提示宿主手动
# `emrg server`；成功连接后归零。
_MAX_SPAWN_ATTEMPTS = 3
_spawn_attempts = 0


async def start_daemon() -> subprocess.Popen:
    """Start emrgd in the background and wait until it accepts connections."""
    global _spawn_attempts
    if _spawn_attempts >= _MAX_SPAWN_ATTEMPTS:
        raise RuntimeError(
            f"daemon failed to start after {_MAX_SPAWN_ATTEMPTS} attempts — "
            "please run 'emrg server' manually and check emrgd.log"
        )
    _spawn_attempts += 1
    logger.info("starting emrgd daemon (attempt %d/%d)...", _spawn_attempts, _MAX_SPAWN_ATTEMPTS)
    cleanup_server()
    # Mark the log before the child can write to it: a failure report may only
    # quote what *this* attempt appended (issue #1276).
    log_mark = _log_size(_log_path())
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "emrg.server",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
        # Windows: daemon spawn must never pop a console window
        # (rant 2026-08-09T13:16:36 — cmd-window storm).
        **win32_no_window_kwargs())
    await _await_daemon_ready(proc, _log_path(), log_mark)
    logger.info("emrgd started (pid=%d)", proc.pid)
    return proc


def _log_path() -> Path:
    """Where emrgd writes — resolved per call, not at import.

    A module-level constant would freeze ``Path.home()`` at import time, which is
    wrong in exactly the case this diagnostic is for: tests and the GUI's
    isolated-HOME runs point HOME elsewhere after the module is loaded.
    """
    return Path.home() / ".emrg" / "emrgd.log"


def _log_size(path: Path) -> int:
    """Byte size of the log, or 0 when it cannot be read — the pre-spawn mark."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_log_tail(path: Path, lines: int = 15, since: int = 0) -> str:
    """Return the last `lines` of what was appended after byte offset `since`.

    `since` is the point of the diagnostic (issue #1276): the tail of the whole
    file is *history*, and history printed as the reason a start failed sent the
    host looking for a cause in a previous run's normal shutdown. Only what this
    attempt wrote can explain this attempt; when it wrote nothing, the caller
    must say so rather than show an older run.
    """
    try:
        if not path.exists():
            return ""
        with open(path, "rb") as handle:
            handle.seek(max(0, since))
            data = handle.read().decode("utf-8", errors="replace")
        return "\n".join(data.rstrip().splitlines()[-lines:])
    except OSError:
        return ""


def _child_exit_code(proc) -> int | None:
    """The child's exit status if it has already exited, else None (issue #1276).

    Only an ``int`` counts. The first version asked ``is not None``, and the
    existing suite caught why that is wrong: a subprocess-like stand-in (or any
    object whose ``returncode`` is not the documented int-or-None) then reports
    an exit that never happened, and the wait fails fast on a *live* child —
    turning a diagnostic fix into a startup regression.
    """
    code = getattr(proc, "returncode", None)
    return code if isinstance(code, int) else None


def _startup_failure_detail(log_path: Path, since: int, proc) -> str:
    """What is actually known about a start that did not come up (issue #1276).

    Still the fix for rant 2026-08-05T15:54:28 (R124): a config.toml parse error
    used to surface as a bare "failed to start within timeout" with the real
    error swallowed. The tail is still read — from this attempt's bytes only.

    Two facts, and neither may be replaced by a guess: whether the child is
    still alive (and its exit code if not), and whether this attempt appended
    anything to the log at all.
    """
    tail = _read_log_tail(log_path, lines=15, since=since)
    code = _child_exit_code(proc)
    if tail:
        return f"\n  emrgd.log 尾部（本次启动新增）:\n{tail}"
    alive = "still running" if code is None else f"already exited (exit={code})"
    return (
        f"\n  this start attempt wrote nothing to emrgd.log"
        f"{'' if log_path.exists() else ' (the file does not exist)'};"
        f" the child is {alive}. Any output earlier in the file is from a previous run."
    )


async def _await_daemon_ready(
    proc,
    log_path: Path,
    since: int,
    probe=None,
    *,
    attempts: int = 15,
    delay: float = 0.3,
) -> None:
    """Wait for the daemon to accept connections; raise with what is known.

    Fail fast on a child that has already exited (issue #1276 item 3): the loop
    used to sleep out the whole window without ever asking, so a child dying at
    import or config-parse stage cost 4.5s and reported no exit code at all.
    The exit code is the one fact a silent child leaves behind, because the spawn
    discards its stderr.
    """
    probe = probe or is_running
    for _ in range(attempts):
        await asyncio.sleep(delay)
        if probe():
            return
        code = _child_exit_code(proc)
        if code is not None:
            raise RuntimeError(
                f"emrgd exited during startup (exit={code})"
                + _startup_failure_detail(log_path, since, proc)
            )
    raise RuntimeError(
        f"emrgd failed to start within {attempts * delay:.1f}s"
        + _startup_failure_detail(log_path, since, proc)
    )


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
                # Kill old server: SIGTERM first, SIGKILL if still alive.
                # ⚠️ (rant 2026-08-18T12:49:09 ②) The old daemon must be TRULY
                # dead before the port file is removed and a new daemon spawns.
                # Previously cleanup_server() deleted the port file BEFORE the
                # wait, so is_running() (a port-file probe) returned False
                # instantly and a new daemon spawned while the old one was still
                # shutting down → multiple emrg.server instances on different
                # ports. Wait on the old PID itself (POSIX os.kill(pid,0) probe),
                # then remove the port file only after it is gone.
                try:
                    os.kill(server_pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

                def _old_pid_alive() -> bool:
                    if sys.platform == "win32":
                        # os.kill(pid, 0) would TerminateProcess on Windows —
                        # never use it as a liveness probe. Windows SIGTERM is
                        # an immediate hard kill, so the port probe suffices.
                        return is_running()
                    try:
                        os.kill(server_pid, 0)
                        return True
                    except ProcessLookupError:
                        return False
                    except OSError:
                        return True  # EPERM → process exists

                for _ in range(50):  # up to 10s for graceful shutdown
                    await asyncio.sleep(0.2)
                    if not _old_pid_alive():
                        break
                else:
                    # SIGTERM didn't work — force kill
                    logger.warning("old daemon (pid=%d) didn't die, sending SIGKILL", server_pid)
                    try:
                        os.kill(server_pid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
                    for _ in range(10):  # up to 2s for SIGKILL to land
                        await asyncio.sleep(0.2)
                        if not _old_pid_alive():
                            break
                # Old daemon is gone — now safe to remove its port file
                cleanup_server()
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
    global _spawn_attempts
    await check_and_restart_if_stale()
    if not is_running():
        cleanup_server()
        await start_daemon()
    conn = DaemonConnection(await connect_to_server())
    # 连接生命周期成功 → spawn 节流计数归零（对照 GUI daemon_client.js auth_ok）
    _spawn_attempts = 0
    return conn


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
                        images: list | None = None, id: str | None = None) -> str:
        """聊天发送：TaskRequest(type="task")。images 支持 /image 粘贴图。

        内部 json.dumps(req.to_dict(), ensure_ascii=False) 以 str 发送（不 .encode()）。
        `id` 显式指定请求 id（P1 queue requeue 复用原 id 以匹配 queued_requeue）；
        返回最终请求 id（未指定时为内部生成的 uuid）。
        """
        req = TaskRequest(session_id=session_id, cwd=cwd, prompt=prompt)
        if id:
            req.id = id
        if images:
            req.images = images
        await self._ws.send(json.dumps(req.to_dict(), ensure_ascii=False))
        return req.id

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
