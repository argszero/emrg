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
    log_mark = _log_mark(_log_path())
    # The child's own stderr used to go to DEVNULL, and `_configure_logging()`
    # installs emrgd's handler *inside* the child — so a start that died before
    # that call (an import error, a syntax error in a patch, a missing module)
    # left no evidence anywhere: emrgd.log was empty and the host was told
    # nothing at all. That is the second symptom of issue #1276, and it is the
    # one case where the diagnostic has no other channel to read from.
    #
    # A file, not the terminal: the reason stderr was discarded is that the
    # daemon must not write into the client's TUI (server logs are files), and
    # redirecting it into a file keeps that property while giving the failure a
    # place to be read from. Nothing is duplicated into it: emrgd adds its
    # StreamHandler only when `sys.stderr.isatty()`, which is false for a file
    # exactly as it was for DEVNULL (`emrg/server/__main__.py`).
    stderr_path = _start_stderr_path()
    stderr_handle = _truncate_start_stderr(stderr_path)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "emrg.server",
            stdout=subprocess.DEVNULL,
            stderr=stderr_handle if stderr_handle is not None else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
            # Windows: daemon spawn must never pop a console window
            # (rant 2026-08-09T13:16:36 — cmd-window storm).
            **win32_no_window_kwargs())
    finally:
        # The child holds its own descriptor; the parent's copy would leak.
        if stderr_handle is not None:
            stderr_handle.close()
    # The channel is handed on only when it was really opened: `None` says the
    # child's stderr went to DEVNULL, which the report must not turn into
    # "the child wrote nothing to its own stderr" (`_startup_failure_detail`,
    # which reads this parameter as "captured here, or not captured at all").
    await _await_daemon_ready(
        proc, _log_path(), log_mark,
        stderr_path=stderr_path if stderr_handle is not None else None,
    )
    logger.info("emrgd started (pid=%d)", proc.pid)
    return proc


def _log_path() -> Path:
    """Where emrgd writes — resolved per call, not at import.

    A module-level constant would freeze ``Path.home()`` at import time, which is
    wrong in exactly the case this diagnostic is for: tests and the GUI's
    isolated-HOME runs point HOME elsewhere after the module is loaded.
    """
    return Path.home() / ".emrg" / "emrgd.log"


def _start_stderr_path() -> Path:
    """Where a spawned daemon's **own stderr** goes (issue #1276, item 4).

    Resolved per call for the same reason `_log_path` is. Separate from
    `emrgd.log` on purpose: that file is the daemon's *logging* output, written by
    a handler the child installs partway through its startup, so the failures this
    file exists for are exactly the ones that happen before it and therefore never
    reach `emrgd.log`.
    """
    return Path.home() / ".emrg" / "emrgd-start.err"


def _truncate_start_stderr(path: Path):
    """Open ``path`` for the child's stderr, **truncated**, or None if it cannot be opened.

    Truncated rather than appended so that everything a failure report quotes from
    it is this attempt's bytes: the defect the log-delta reader exists for is
    history presented as the cause of *this* failure (issue #1276), and a file that
    is only ever appended to would reintroduce it one level down. ``None`` means
    the caller falls back to ``DEVNULL`` — a diagnostic must never be able to make
    a start fail.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return open(path, "wb")
    except OSError:
        return None


def _read_start_stderr(path: Path | None, lines: int = 40) -> str:
    """The last ``lines`` of the child's own stderr for this attempt, or ``""``.

    ``lines`` is larger than the log tail's 15 because the useful part of a
    traceback is the *end* of it — the exception line and its cause — and a
    Python traceback is longer than fifteen lines. An unreadable or absent file
    answers ``""``: "could not be read" and "nothing was written" are both
    reported as text by the caller, never as an exception out of a diagnostic.
    """
    if path is None:
        return ""
    try:
        data = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(data.rstrip().splitlines()[-lines:])


def _log_mark(path: Path) -> tuple[int, int | None]:
    """The pre-spawn mark of the log: ``(byte size, inode)``, or ``(0, None)``.

    **A size alone is not a mark, because emrgd's log is *replaced*, not appended
    to.** ``emrg/server/__main__.py`` installs a ``RotatingFileHandler``
    (``maxBytes``, ``backupCount``), so between this mark and the read an offset
    can come to index a different file — measured on the handler's own
    ``doRollover()``, in both size regimes:

    * the pre-spawn log is near the cap, so the mark lands *past* the new file's
      end: the read answers ``""`` and the host is told "this start attempt wrote
      nothing to emrgd.log" while the new file holds this attempt's error text —
      R124's symptom (a real cause swallowed) reintroduced by a diagnostic;
    * the pre-spawn log is short, so the mark lands *inside* a line of the new
      file: a fragment is presented as what this attempt wrote.

    The identity discriminates both, and the size is not the discriminator: the
    inode changed in *both* regimes, so a ``size_now < mark`` test catches the
    first and misses the second. The size is still carried, for the one case the
    inode cannot see: a log truncated in place keeps its inode and shrinks.
    """
    try:
        st = path.stat()
    except OSError:
        return 0, None
    return st.st_size, st.st_ino


def _read_log_tail(path: Path, lines: int = 15, since: tuple[int, int | None] = (0, None)) -> str:
    """Return the last `lines` of what this attempt appended after the mark `since`.

    `since` is a mark from `_log_mark` — ``(byte offset, inode)``, never a bare
    offset — and that is the point of the diagnostic (issue #1276): the tail of
    the whole file is *history*, and history printed as the reason a start failed
    sent the host looking for a cause in a previous run's normal shutdown. Only
    what this attempt wrote can explain this attempt; when it wrote nothing, the
    caller must say so rather than show an older run.

    A mark whose inode is no longer the file's, or whose offset is now past the
    end, is read from the start instead: the file it marked is gone, so every byte
    of the current one belongs to this attempt. ``(0, None)`` means "no mark" and
    reads the whole file, which is what the whole-file tail caller wants.
    """
    offset, ino = since
    try:
        if not path.exists():
            return ""
        current = path.stat()
        if (ino is not None and current.st_ino != ino) or current.st_size < offset:
            offset = 0
        with open(path, "rb") as handle:
            handle.seek(offset)
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


def _startup_failure_detail(
    log_path: Path,
    since: tuple[int, int | None],
    proc,
    stderr_path: Path | None = None,
) -> str:
    """What is actually known about a start that did not come up (issue #1276).

    Still the fix for rant 2026-08-05T15:54:28 (R124): a config.toml parse error
    used to surface as a bare "failed to start within timeout" with the real
    error swallowed. The tail is still read — from this attempt's bytes only.

    Three facts, and none may be replaced by a guess: whether the child is still
    alive (and its exit code if not), whether this attempt appended anything to
    the log, and — the one the other two cannot see — what the child itself wrote
    to its own stderr before it died. A child that fails at import stage writes
    *only* to stderr, so before this section existed the host was shown an empty
    log and told nothing at all (issue #1276, second symptom).

    ``stderr_path`` is where the child's stderr was captured, and ``None`` means
    it was **not captured**: the caller could not open the file and the child's
    stderr went to DEVNULL instead. "The child wrote nothing" and "nothing was
    read from that channel" are two different facts, so the text below keeps them
    distinct — the earlier shape passed the path on unconditionally and could
    quote an *older* attempt's bytes as this failure's cause, and the paragraph
    that replaced the quote still claimed silence about a channel nobody had
    opened.
    """
    tail = _read_log_tail(log_path, lines=15, since=since)
    child_err = _read_start_stderr(stderr_path)
    if stderr_path is None:
        child_section = "\n  emrgd 自身 stderr: 未捕获（本次启动没有读到这一路）"
    elif child_err:
        child_section = f"\n  emrgd 自身 stderr（本次启动新增）:\n{child_err}"
    else:
        child_section = ""
    code = _child_exit_code(proc)
    if tail:
        return child_section + f"\n  emrgd.log 尾部（本次启动新增）:\n{tail}"
    if child_err:
        return child_section + (
            f"\n  this start attempt wrote nothing to emrgd.log"
            f"{'' if log_path.exists() else ' (the file does not exist)'};"
            f" the child is {'still running' if code is None else f'already exited (exit={code})'}."
        )
    alive = "still running" if code is None else f"already exited (exit={code})"
    # The silence claim is a measurement of a channel, so it needs one to have
    # been read: it is made only when a path was given and came back empty.
    stderr_fact = (
        "the child's own stderr was not captured"
        if stderr_path is None
        else "the child wrote nothing to its own stderr"
    )
    return (
        f"\n  this start attempt wrote nothing to emrgd.log"
        f"{'' if log_path.exists() else ' (the file does not exist)'},"
        f" and {stderr_fact};"
        f" the child is {alive}. Any output earlier in the file is from a previous run."
    )


async def _await_daemon_ready(
    proc,
    log_path: Path,
    since: tuple[int, int | None],
    probe=None,
    *,
    attempts: int = 15,
    delay: float = 0.3,
    stderr_path: Path | None = None,
) -> None:
    """Wait for the daemon to accept connections; raise with what is known.

    Fail fast on a child that has already exited (issue #1276 item 3): the loop
    used to sleep out the whole window without ever asking, so a child dying at
    import or config-parse stage cost 4.5s and reported no exit code at all.
    The exit code is one fact a silent child leaves behind; the other is
    ``stderr_path``'s content, where the child's own output was captured instead
    of discarded (issue #1276 item 4) — a child that dies at import writes
    *only* there, so without it the failure report had nothing to quote.
    ``None`` is not a path: it says the capture did not happen, and the report
    says so instead of claiming the channel was silent.
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
                + _startup_failure_detail(log_path, since, proc, stderr_path)
            )
    raise RuntimeError(
        f"emrgd failed to start within {attempts * delay:.1f}s"
        + _startup_failure_detail(log_path, since, proc, stderr_path)
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
