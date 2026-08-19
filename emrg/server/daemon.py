"""EMRG server daemon.

Mirrors the Rust emrg-server. Listens for IPC connections, processes tasks,
runs the tool-calling loop, and drives background evolution tasks via the scheduler.

IPC transport is abstracted by emrg.connect:
  - Unix Domain Socket on macOS/Linux
  - Named Pipe on Windows
"""

from __future__ import annotations

import asyncio
import base64
import errno
import json
import logging
import os
import platform
import re
import secrets
import signal
import socket as _socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from emrg._win import win32_no_window_kwargs
from emrg.config import LlmConfig, config_dir
from emrg.connect import EMRGD_PORT, cleanup_server, is_server_running_sync
from emrg.server.atomic import atomic_write_bytes, atomic_write_yaml
from emrg.server.llm import LlmClient
from emrg.server.git_utils import (
    _detect_git_remote,
    no_prompt_env,
    parse_gh_auth_user,
    resolve_git_gh,
)
from emrg.server.tool_types import ToolResult
from emrg.memory import ProjectMemoryStore, SessionMemoryStore
from emrg.protocol import (
    EvolutionLog,
    InstanceIdentity,
    ServerPong,
    TaskRequest,
)
from emrg.session import Session

# ── 日志脱敏（rant 2026-08-06T10:21:26）────────────────────────────
# tool call 参数可能含 api_key/token/authorization/password 等敏感字段，
# 递归替换值为 ***，避免 emrgd.log 泄露凭据。
# 除按键名脱敏外，字符串值内联的凭据模式（sk-*/ghp_*/Bearer/JWT/base64-JSON）
# 也会被遮蔽——防止 bash command 里 `export OPENAI_API_KEY=sk-...` 或
# base64 编码的 access_token 整体泄露（跨项目教训：明文正则匹配不到编码形式）。
_SENSITIVE_KEY_SUBSTRINGS = (
    "api_key", "token", "authorization", "password", "secret",
    "api-key", "auth", "credential", "key",
)

# 字符串值内联凭据模式（保守匹配，宁多勿漏）
_INLINE_SECRET_PATTERNS = (
    # sk- 密钥：sk- 后必须 ≥16 位纯字母数字（OpenAI/DeepSeek），或 sk-proj-（OpenAI 项目）
    # 或 sk-ant-apiNN-（Anthropic）—— 排除 "task-evolution" 等路径片段误伤
    re.compile(r"(sk-(?:proj-)?[A-Za-z0-9]{16,})"),
    re.compile(r"(sk-ant-api[0-9]+-[A-Za-z0-9]{16,})"),           # Anthropic
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,})"),                  # GitHub PAT / OAuth / gist token
    re.compile(r"(xox[baprs]-[A-Za-z0-9\-]{10,})"),               # Slack token
    re.compile(r"(AKIA[0-9A-Z]{16})"),                            # AWS access key id
    re.compile(r"(Bearer\s+[A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE),  # Bearer 令牌
    re.compile(r"(Authorization\s*[:=]\s*[A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE),
    re.compile(r"((api[_-]?key|apikey|password|passwd|secret|token)\s*[:=]\s*[^\s,;\"']+)", re.IGNORECASE),
    # JWT：三段 base64url（eyJ... 开头）—— 一段即泄露签名密钥
    re.compile(r"(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)"),
)


def _redact_string(s: str) -> str:
    """遮蔽字符串值内联的凭据模式；base64-JSON 含敏感键时整段遮蔽。"""
    out = s
    for pat in _INLINE_SECRET_PATTERNS:
        out = pat.sub("***", out)
    # base64 编码的 JSON（跨项目教训：access_token 以编码形式进日志，明文正则匹配不到）
    for b64 in re.findall(r"[A-Za-z0-9+/]{40,}={0,2}", out):
        try:
            decoded = base64.b64decode(b64, validate=True)
            if any(k in decoded for k in (b"access_token", b"api_key", b"apikey", b"authorization", b"password", b"secret")):
                out = out.replace(b64, "***")
        except Exception:
            continue
    return out


def _redact(value):
    """递归脱敏 dict/list 中的敏感字段值与字符串内联凭据（不修改原对象）。"""
    if isinstance(value, dict):
        return {
            k: ("***" if any(s in k.lower() for s in _SENSITIVE_KEY_SUBSTRINGS)
                else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value

from emrg.tools import ToolRegistry
from emrg.tools.bash_tool import BashTool
from emrg.tools.read_tool import ReadTool
from emrg.tools.write_tool import WriteTool
from emrg.tools.edit_tool import EditTool
from emrg.tools.glob_tool import GlobTool
from emrg.tools.grep_tool import GrepTool
from emrg.tools.submit_rant_tool import SubmitRantTool
from emrg.skills.loader import load_skills
from emrg.skills.registry import ensure_catalog_file, load_catalog_skills, skill_is_managed
from emrg.server.rants import append_rant
from emrg.server.scheduler import TaskScheduler

logger = logging.getLogger(__name__)

# ── Jinja2 template environment for system prompt ──
_jinja_env = None

def _get_jinja_env() -> "jinja2.Environment":
    """Lazy-init the Jinja2 environment pointing at the prompts directory."""
    global _jinja_env
    if _jinja_env is None:
        import jinja2  # type: ignore[import-untyped]
        _jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(Path(__file__).parent / "prompts"),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _jinja_env


# ── Module-level constants ──
EVOLUTION_CWD = Path.home() / ".emrg" / "evolution"

# Windows TIME_WAIT retry: SO_EXCLUSIVEADDRUSE (the only anti-hijack option on
# Windows) blocks rebinding while accepted connections linger in TIME_WAIT.
# serve() treats EADDRINUSE-with-no-listener as a TIME_WAIT remnant and retries
# the bind for up to this many attempts at this interval (~10s), so a crashed
# daemon restarts without a 30-120s stall.
_TIME_WAIT_RETRIES = 20
_TIME_WAIT_RETRY_DELAY = 0.5


def _create_fixed_port_socket(port: int) -> _socket.socket:
    """Create + bind the daemon's fixed loopback listening socket.

    This is the daemon's ONLY single-instance admission (host rant
    2026-08-19T08:05:21): the kernel refuses a second bind on the same
    (addr, port) with EADDRINUSE — pure resource exclusivity with no file to
    forge/delete (PID files were the unreliable mechanism), no race window,
    and automatic release when the process dies. Raises OSError(EADDRINUSE)
    when another socket already owns the port; the caller treats a *live*
    listener as "emrgd already running" and exits itself.

    Socket options:
    - Windows: SO_EXCLUSIVEADDRUSE only. It forbids any other socket from
      binding the same port (SO_REUSEADDR alone would allow port hijacking).
      SO_EXCLUSIVEADDRUSE and SO_REUSEADDR are MUTUALLY EXCLUSIVE on Windows
      (the second setsockopt fails with WSAEINVAL 10022 — verified on the
      Windows CI matrix). The cost is that a closed listening socket with
      accepted connections lingering in TIME_WAIT blocks rebinding; serve()
      handles that with a listener-probe + bounded retry so a crashed daemon
      still restarts quickly (rant acceptance: "无 TIME_WAIT 卡死").
    - POSIX: SO_REUSEADDR only. It permits rebinding while TIME_WAIT sockets
      linger but does NOT allow two listeners on the same addr (that would be
      SO_REUSEPORT, which we deliberately never set) — exclusivity is kept.
    """
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        sock.setblocking(False)
        return sock
    except OSError:
        try:
            sock.close()
        except OSError:
            pass
        raise


class EmrgServer:
    """EMRG daemon — listens on Unix socket, processes tasks with tool calling."""

    def __init__(self, llm_config: LlmConfig) -> None:
        runtime_dir = config_dir()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        # Ensure skills directory exists for evolution-installed skills
        (runtime_dir / "skills").mkdir(exist_ok=True)
        # Installable-skills catalog baseline (rant 2026-08-08T10:14:29):
        # the catalog is itself a skill (skill-catalog.md); on upgrades or
        # user deletion the daemon re-writes it from the embedded baseline.
        try:
            ensure_catalog_file()
        except Exception:
            logger.debug("could not ensure skill catalog", exc_info=True)

        host_name = platform.node()
        self.identity = InstanceIdentity(
            instance_id="emrg-" + os.urandom(4).hex(),
            host_name=host_name,
            fork_source=os.environ.get("EMRG_UPSTREAM"),
            branch_id="master",
        )

        self.start_time = datetime.now()
        self.evolutions: list[EvolutionLog] = []
        self.llm = LlmClient(llm_config)
        self._running = False
        self._scheduler: Optional[TaskScheduler] = None
        self._max_tool_rounds = llm_config.max_tool_rounds
        self._projects_log = runtime_dir / "projects.yml"
        self._rants_log = runtime_dir / "rants.jsonl"

        # ── Phase 2 broadcast model (protocol-contract §2.6) ──
        self._session_subscribers: dict[str, set] = {}  # session_id → set[ws]
        self._session_busy: dict[str, bool] = {}        # session_id → active task?
        # P1 queue-injection (rant 2026-08-10T21:55:37): per-session FIFO of
        # (TaskRequest, allow_tools) received while a tool loop is busy —
        # injected at the next round boundary (aligned with codex steer_input).
        self._session_pending: dict[str, list[tuple[TaskRequest, bool]]] = {}
        self._all_connections: set = set()              # all authenticated connections

        # Device-flow auth (rant 10:17 Stage 2b): background gh auth login --web task
        self._pending_web_auth: Optional[asyncio.Task] = None
        self._pending_web_auth_proc: Optional[asyncio.subprocess.Process] = None

        # Build tool registry
        self.tools = ToolRegistry()
        self.tools.register(BashTool())
        self.tools.register(ReadTool())
        self.tools.register(WriteTool())
        self.tools.register(EditTool())
        self.tools.register(GlobTool())
        self.tools.register(GrepTool())
        self.tools.register(SubmitRantTool())
        logger.info("tools registered: %s", self.tools.names)

        # Load skills
        self.skills = load_skills()
        if self.skills:
            logger.info("skills loaded: %s", [s.name for s in self.skills])

    async def serve(self) -> None:
        """Start listening for IPC connections (fixed-port, platform-adaptive)."""
        self._running = True

        # ── Single-instance admission: fixed-port bind exclusivity (ONLY) ───
        # (host rant 2026-08-19T08:05:21) PID files are unreliable (plain
        # files — content can be overwritten/deleted, and os.kill(pid,0)
        # liveness probes misjudge: observed dual instances PID 3924+2592 on
        # 08-19) and random ports (port=0) make port exclusivity useless. The
        # fixed-port bind IS the admission: the kernel refuses a second bind
        # (EADDRINUSE) — no file to forge, no race window, auto-released on
        # crash. No transitional compatibility with old-format daemons
        # (rant: "升级后即唯一生效").
        try:
            sock = _create_fixed_port_socket(EMRGD_PORT)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            # The port is taken. Distinguish a LIVE daemon from a Windows
            # TIME_WAIT remnant: only a live listener accepts connections.
            # (Windows SO_EXCLUSIVEADDRUSE blocks rebinding while accepted
            # connections linger in TIME_WAIT — SO_REUSEADDR cannot be combined
            # with it, WSAEINVAL 10022; POSIX SO_REUSEADDR never hits this.)
            if is_server_running_sync(timeout=0.5):
                logger.error(
                    "emrgd already running on 127.0.0.1:%d (EADDRINUSE, "
                    "fixed-port admission) — new instance exiting itself. "
                    "Stop it first (emrg server stop).",
                    EMRGD_PORT,
                )
                self._running = False
                return
            # No listener behind the port → TIME_WAIT remnant. Retry the bind
            # for a bounded window so a crashed daemon restarts without a
            # 30-120s stall (rant acceptance: "无 TIME_WAIT 卡死").
            logger.warning(
                "port 127.0.0.1:%d busy but no daemon listening — "
                "TIME_WAIT remnant, retrying bind (%d x %.1fs)",
                EMRGD_PORT, _TIME_WAIT_RETRIES, _TIME_WAIT_RETRY_DELAY,
            )
            for _ in range(_TIME_WAIT_RETRIES):
                await asyncio.sleep(_TIME_WAIT_RETRY_DELAY)
                try:
                    sock = _create_fixed_port_socket(EMRGD_PORT)
                    break
                except OSError as retry_exc:
                    if retry_exc.errno != errno.EADDRINUSE:
                        raise
                    if is_server_running_sync(timeout=0.5):
                        logger.error(
                            "emrgd already running on 127.0.0.1:%d (became "
                            "live during TIME_WAIT retry) — new instance "
                            "exiting itself.",
                            EMRGD_PORT,
                        )
                        self._running = False
                        return
            else:
                logger.error(
                    "port 127.0.0.1:%d busy (TIME_WAIT) but no daemon "
                    "listening after %d retries — giving up.",
                    EMRGD_PORT, _TIME_WAIT_RETRIES,
                )
                self._running = False
                return

        # ── PID file: diagnostics only (rant 08-05:21 — no longer an
        # admission gate). Written AFTER the fixed-port bind succeeded, so only
        # the process that actually owns the port writes it; stop_all and
        # diagnostics may still read it.
        runtime_dir = config_dir()
        pid_file = runtime_dir / "emrgd.pid"
        try:
            pid_file.write_text(str(os.getpid()), encoding="utf-8")
            logger.debug("pid file written (diagnostic only): %s (pid=%d)", pid_file, os.getpid())
        except OSError:
            logger.warning("could not write diagnostic pid file %s", pid_file, exc_info=True)

        self._server = await serve(
            self._handle_client,
            sock=sock,
            max_size=16 * 1024 * 1024,
            # keepalive 超时放宽：TUI 回答结束时全量渲染可阻塞事件循环数秒，
            # 默认 ping_timeout=20 会导致服务器 CLOSE 1011 踢连接（rant 14:22:06）。
            # 保留 ping_interval=20（liveness 检测），容忍 300s 的 pong 延迟。
            ping_timeout=300,
        )
        port = EMRGD_PORT
        self._auth_token = secrets.token_urlsafe(32)
        self._assert_port_file(port)
        # Rant 2026-08-09T18:47:37 B4：启动完成一行自证——pid/port/port 文件路径/写入成功，
        # 宿主拿到 emrgd.log 就知道 daemon 到底起没起、写没写对文件。
        logger.info(
            "emrgd listening on 127.0.0.1:%d | identity=%s | pid=%d | port_file=%s | port_file_written_ok=%s",
            port,
            self.identity.instance_id[:8],
            os.getpid(),
            config_dir() / "emrgd.port",
            (config_dir() / "emrgd.port").exists(),
        )

        # Rant 2026-08-09T13:16:36 root-cause self-heal: G43 stale-port logic
        # deleted a healthy daemon's emrgd.port after a transient ws failure →
        # the daemon's OWN scheduler lost the file (93× "cannot connect") while
        # GUI respawns hit the PID lock and exited. The daemon re-asserts its
        # port file periodically so any external deletion self-heals.
        self._port_keepalive_task = asyncio.create_task(self._port_keepalive_loop())

        self._scheduler = TaskScheduler(self.identity)
        self._scheduler.load_and_start()

        # Global cross-project session index (rant 2026-08-13T16:42:22):
        # backfill the index from every on-disk session (registered projects +
        # unregistered ones under ~/.emrg) so sessions created before this
        # feature are discoverable by other projects. Best-effort — never
        # crashes startup.
        self._rebuild_sessions_index()

        # Background deterministic skill update check (rant 2026-08-08T10:14:29):
        # runs at startup + every 24h — refreshes managed skills to their
        # latest GitHub releases. Never installs a CLI silently, never touches
        # host-modified skill copies.
        self._skills_ttl_task = asyncio.create_task(self._skills_ttl_loop())

        # Auto update-check prompt (rant 2026-08-10T07:12:12): runs at startup
        # + every [update] ttl_hours (default 24h). ONLY checks the latest
        # release and persists state — clients (TUI/GUI) decide how to show
        # the one-time prompt. No auto download/install, silent on failure.
        self._update_check_task = asyncio.create_task(self._update_check_loop())

        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            self._skills_ttl_task.cancel()
            self._update_check_task.cancel()
            try:
                await self._skills_ttl_task
            except (asyncio.CancelledError, Exception):
                pass
            self._port_keepalive_task.cancel()
            try:
                await self._port_keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._scheduler.stop_all()
            await self._scheduler.wait_all()
            await self.llm.close()
            cleanup_server()
            # Remove PID file
            try:
                if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    pid_file.unlink()
                    logger.debug("pid file removed: %s", pid_file)
            except OSError:
                pass

    async def _port_keepalive_loop(self) -> None:
        """Re-assert the port file if it was deleted or overwritten.

        Rant 2026-08-09T13:16:36 root cause: a client's stale-port unlink
        (G43) can remove a healthy daemon's emrgd.port after one transient
        ws failure. The daemon's own scheduler reads that file to reconnect,
        so it then fails forever while the PID lock blocks new spawns —
        the zombie state behind the Windows v0.2.15 storm. Re-writing the
        file every 60s makes the daemon self-healing.
        """
        port_path = config_dir() / "emrgd.port"
        while self._running:
            await asyncio.sleep(60)
            try:
                if not port_path.exists():
                    port = self._server.sockets[0].getsockname()[1]
                    self._assert_port_file(port)
                    logger.warning(
                        "emrgd.port was missing — re-asserted (external deletion?)"
                    )
            except (OSError, IndexError, AttributeError):
                pass

    def _assert_port_file(self, port: int) -> None:
        """(Re)write the port/token file for the current listener."""
        atomic_write_bytes(
            f"{port}\n{self._auth_token}",
            config_dir() / "emrgd.port",
            mode=0o600,
        )

    def _rebuild_sessions_index(self) -> None:
        """Backfill the global cross-project session index at startup.

        Rant 2026-08-13T16:42:22: sessions created before this feature (or in
        projects not registered in projects.yml) would otherwise be invisible
        to other projects. Best-effort — failures are logged at debug level
        and never crash the daemon.
        """
        from emrg.sessions_index import rebuild_sessions_index

        try:
            project_paths: list[str] = []
            if self._projects_log.exists():
                try:
                    data = yaml.safe_load(self._projects_log.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        project_paths = [
                            e.get("path", "")
                            for e in data
                            if isinstance(e, dict) and e.get("path")
                        ]
                except (yaml.YAMLError, OSError):
                    pass
            count = rebuild_sessions_index(config_dir(), project_paths)
            logger.info("sessions index rebuilt: %d sessions indexed", count)
        except Exception:
            logger.debug("sessions index rebuild failed", exc_info=True)

    async def _skills_ttl_loop(self) -> None:
        """Background deterministic skill update check (startup + every 24h).

        Design (rant 2026-08-08T10:14:29 §6): the check is deterministic
        logic, not LLM thinking — on each tick, refresh every managed=true
        skill whose latest GitHub release differs from the recorded version.
        Failures are logged at debug level and never crash the daemon.
        """
        from emrg.skills.installer import _UPDATE_TTL_SECONDS, run_update_check_once

        while True:
            result = await run_update_check_once()
            if result.get("updated"):
                logger.info("skills auto-updated: %s", result["updated"])
                self.skills = load_skills()
            elif result.get("errors"):
                logger.warning("skills update errors: %s", result["errors"])
            await asyncio.sleep(_UPDATE_TTL_SECONDS)

    async def _update_check_loop(self) -> None:
        """Background auto update-check + auto-download (rants 07:12:12, 12:10:12).

        Runs at startup + every [update] ttl_hours (default 1h). Checks the
        latest release via api.github.com and persists state to
        ~/.emrg/.last_update_check.json. When a newer version exists and
        [update] auto_download is enabled, the installer is downloaded in a
        background task (stream + Range resume + SHA256 verify) — NEVER
        auto-installed. Failures are silent (never crash, never log noise);
        the next TTL retries. Disabled entirely when [update] check = false.
        """
        from emrg.config import load_update_config
        from emrg.update_check import (
            load_state,
            run_update_check_once,
            should_check,
        )

        update_cfg = load_update_config()
        if not update_cfg.check:
            logger.debug("auto update-check disabled by config ([update] check=false)")
            return

        ttl = max(3600, int(update_cfg.ttl_hours or 1) * 3600)
        while True:
            state = load_state()
            if should_check(state, ttl):
                result = await run_update_check_once()
                if result.get("checked"):
                    logger.debug(
                        "update check: latest=%s", result.get("latest_version")
                    )
                    await self._maybe_auto_download(
                        result.get("latest_version"), update_cfg.auto_download
                    )
            await asyncio.sleep(ttl)

    async def _maybe_auto_download(self, latest_version: str, auto_download: bool) -> None:
        """Kick off a background installer download when a newer version exists.

        rant 2026-08-12T12:10:12: auto-download runs in its own task so the
        check loop / chat is never blocked. Skipped when auto_download is
        disabled, the version is not newer than the running one, or the same
        version is already downloaded (and verified).
        """
        if not auto_download or not latest_version:
            return
        import emrg
        from emrg.update_check import (
            is_newer,
            load_state,
            parse_version,
        )

        state = load_state()
        current = getattr(emrg, "__version__", "0")
        if not is_newer(parse_version(latest_version), parse_version(current)):
            return
        if state.get("downloaded_version") == latest_version:
            return  # already downloaded + verified
        asyncio.create_task(self._auto_download_update(latest_version))

    async def _auto_download_update(self, version: str) -> None:
        """Background installer download + state persist + client notify.

        Never raises; failures are silent and retried at the next TTL. On
        success the downloaded_* fields are persisted to the update state
        file and connected clients get an update_downloaded broadcast so the
        GUI can show the "ready to install" prompt (rant 2026-08-12T12:10:12).
        """
        from emrg.update_check import (
            download_release_asset,
            load_state,
            save_state,
        )

        try:
            result = await download_release_asset(version)
        except Exception:
            logger.debug("update auto-download failed (retry next TTL)", exc_info=True)
            return
        if not result:
            return  # silent — next TTL retries
        try:
            state = load_state()
            state.update(result)
            save_state(state)
        except Exception:
            pass
        logger.info(
            "update auto-downloaded: %s -> %s",
            result.get("downloaded_version"),
            result.get("downloaded_path"),
        )
        try:
            await self._broadcast_all({"type": "update_downloaded", **result})
        except Exception:
            pass

    def _evolution_count(self) -> int:
        """Total completed evolution cycles across scheduler handlers + disk.

        The daemon's own ``self.evolutions`` list is a legacy from the
        pre-scheduler BackgroundThread design (#95) and is never appended;
        the scheduler's handlers own the real per-cycle logs. Aggregate from
        the scheduler, falling back to the legacy list only when the
        scheduler is unavailable (e.g. test harnesses mock it away).

        The scheduler's in-memory count resets to 0 on daemon restart, while
        the ``evolution-*.json`` log files persist — so also count valid log
        files on disk and return the max. This keeps the GUI growth card /
        evolution toast consistent with the ``recent`` list (which reads the
        same files) across restarts instead of showing 0.
        """
        in_memory = 0
        sched = getattr(self, "_scheduler", None)
        if sched is not None:
            try:
                total = sched.total_evolutions()
                if isinstance(total, int):
                    in_memory = total
            except Exception:
                pass
        else:
            in_memory = len(self.evolutions)

        disk = 0
        try:
            logs_dir = config_dir() / "logs"
            for f in logs_dir.glob("evolution-*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if data.get("timestamp"):
                        disk += 1
                except (json.JSONDecodeError, OSError):
                    continue  # corrupt/partial write — don't count
        except OSError:
            pass
        return max(in_memory, disk)

    async def _handle_client(self, ws) -> None:
        """Handle a single WebSocket client connection.

        First frame must be an auth message (``{"type": "auth", "token": ...}``);
        the connection is rejected with a 10s timeout otherwise. On success an
        ``auth_ok`` confirmation frame is sent before the normal protocol loop.
        """
        last_session_id: str | None = None
        last_cwd: str | None = None
        _tool_task: asyncio.Task | None = None
        _cancel_event: asyncio.Event | None = None
        try:
            # ── First-frame auth (local & remote unified) ──
            # Timeout is mandatory: a client that connects and never sends auth
            # would otherwise hang this coroutine forever (socket+coroutine leak).
            try:
                auth_msg = await asyncio.wait_for(ws.recv(), timeout=10)
                auth = json.loads(auth_msg)
            except (ConnectionClosed, json.JSONDecodeError, asyncio.TimeoutError):
                await ws.close()
                return
            # auth must be a dict: json.loads can return list/str, .get() would raise
            if not isinstance(auth, dict) or auth.get("type") != "auth" or not secrets.compare_digest(
                str(auth.get("token", "")), self._auth_token
            ):
                logger.warning("auth failed — rejecting connection")
                await ws.close()
                return
            # Confirm auth so the client can distinguish auth failure from a
            # transient disconnect (prevents infinite reconnect loops).
            await self._send(ws, {"type": "auth_ok"})
            self._all_connections.add(ws)

            while True:
                try:
                    msg = await ws.recv()
                except ConnectionClosed:          # disconnect: exception, not None
                    break
                try:
                    data = json.loads(msg)
                    if not isinstance(data, dict):
                        await self._send(ws, {"error": "message must be a JSON object"})
                        continue
                except json.JSONDecodeError as e:
                    await self._send(ws, {"error": f"invalid json: {e}"})
                    continue

                # Track session for disconnect-time consolidation
                # Phase 2 broadcast: maintain subscription on session_id change
                # (protocol-contract §2.6.2 — the read loop is the only place
                # last_session_id is updated; task/cancel/compact all pass here).
                if data.get("session_id"):
                    new_sid = data["session_id"]
                    if new_sid != last_session_id:
                        if last_session_id:  # unsubscribe from previous session
                            self._session_subscribers.get(last_session_id, set()).discard(ws)
                        self._session_subscribers.setdefault(new_sid, set()).add(ws)
                        last_session_id = new_sid
                if data.get("cwd"):
                    last_cwd = data["cwd"]
                    self._touch_project(last_cwd)

                # ── Cancel: interrupt running tool loop ──────────
                if data.get("type") == "cancel":
                    if _cancel_event:
                        _cancel_event.set()
                    if _tool_task and not _tool_task.done():
                        _tool_task.cancel()
                        try:
                            await _tool_task
                        except asyncio.CancelledError:
                            pass
                    await self._send(ws, {
                        "type": "cancelled",
                        "session_id": data.get("session_id", ""),
                    })
                    _tool_task = None
                    _cancel_event = None
                    continue

                # ── Task: run tool loop in background (non-blocking) ─
                if data.get("type") == "task":
                    session_id = data.get("session_id", "")
                    cwd = data.get("cwd", "")
                    if not session_id or not cwd:
                        await self._send(ws, {
                            "error": "task requires session_id and cwd",
                        })
                        continue
                    # Phase 2 session-level lock (protocol-contract §2.6.5):
                    # one active task per session — concurrent tasks queue.
                    # P1 (rant 21:55:37): construct req + allow_tools FIRST
                    # (the busy branch must append req to the pending queue),
                    # then check busy.
                    try:
                        req = TaskRequest(
                            id=data.get("id", ""),
                            session_id=session_id,
                            cwd=cwd,
                            prompt=data.get("prompt", ""),
                            timestamp=data.get("timestamp", ""),
                            images=data.get("images"),
                        )
                    except Exception as e:
                        await self._send(ws, {"error": f"invalid task: {e}"})
                        continue
                    # WorkBuddy P2 (rant 21:35): Ask mode — pure chat, no tools.
                    # mode="ask" → LLM gets an empty tool set so it can only reply.
                    allow_tools = data.get("mode", "auto") != "ask"
                    if self._session_busy.get(session_id):
                        # Queue the task; injected at the next round boundary.
                        self._session_pending.setdefault(session_id, []).append((req, allow_tools))
                        await self._broadcast(session_id, {
                            "type": "task_queued",
                            "request_id": req.id,
                            "session_id": session_id,
                            "position": len(self._session_pending[session_id]),
                        })
                        continue
                    # Cancel previous task if still running
                    if _tool_task and not _tool_task.done():
                        if _cancel_event:
                            _cancel_event.set()
                        _tool_task.cancel()
                    session = self._get_or_create_session(session_id, Path(cwd))
                    logger.info(
                        'task received: session=%s prompt="%s" → routing via LLM',
                        session_id, _redact_string(req.prompt[:60]),
                    )
                    _cancel_event = asyncio.Event()
                    self._session_busy[session_id] = True  # lock (released in *locked wrapper)
                    _tool_task = asyncio.create_task(
                        self._run_tool_loop_locked(req, ws, session, _cancel_event, allow_tools=allow_tools)
                    )
                    continue

                await self._process_message(data, ws)
        except Exception:
            logger.warning("client error", exc_info=True)
        finally:
            # Cancel any running tool task on disconnect
            if _tool_task and not _tool_task.done():
                if _cancel_event:
                    _cancel_event.set()
                _tool_task.cancel()
                try:
                    await _tool_task
                except asyncio.CancelledError:
                    pass
            # Phase 2 broadcast: unsubscribe on disconnect (protocol-contract §2.6.2)
            if last_session_id:
                self._session_subscribers.get(last_session_id, set()).discard(ws)
            self._all_connections.discard(ws)
            try:
                await ws.close()
            except Exception:
                pass

            # Consolidate session memories on disconnect
            if last_session_id and last_cwd:
                try:
                    await self._consolidate_session_memories(last_session_id, Path(last_cwd))
                except Exception:
                    logger.debug("session memory consolidation failed", exc_info=True)

    async def _send(self, ws, data: dict) -> bool:
        """Send a JSON message to the client.

        Returns True on success, False if the client disconnected.
        Callers should check the return value and stop if False.
        """
        try:
            await ws.send(json.dumps(data, ensure_ascii=False))
            return True
        except (ConnectionClosed, OSError):
            logger.debug("client disconnected during send")
            return False

    # ── Phase 2 broadcast (protocol-contract §2.6.3) ────────────

    async def _broadcast(self, session_id: str, data: dict) -> None:
        """Send data to all subscribers of session_id (including the originator).

        Best-effort: a single dead subscriber must not affect the others.
        """
        for w in list(self._session_subscribers.get(session_id, ())):
            try:
                await self._send(w, data)
            except Exception:
                pass  # individual subscriber failure is non-fatal

    async def _broadcast_all(self, data: dict, exclude=None) -> None:
        """Send data to ALL authenticated connections (global state, e.g. model_set)."""
        for w in list(self._all_connections):
            if w is exclude:
                continue
            try:
                await self._send(w, data)
            except Exception:
                pass  # individual connection failure is non-fatal

    # ── Project tracking ─────────────────────────────────────

    def _touch_project(self, cwd: str) -> None:
        """Record a project as active in ~/.emrg/projects.yml.

        Used by the evolution cycle to discover which projects have
        recent user activity and analyze their sessions for improvement ideas.

        Normalizes the path via realpath() so symlinked directories don't
        cause duplicate entries.
        owner/repo is detected at runtime from git remote, not stored.
        """
        cwd = os.path.realpath(cwd)
        # Don't track the evolution engine's own workspace as a project
        evolution_cwd = str(EVOLUTION_CWD.resolve())
        if cwd == evolution_cwd or cwd.startswith(evolution_cwd + os.sep):
            return
        # Don't track the home directory as a project
        home = os.path.expanduser("~")
        if cwd == home:
            return
        self._projects_log.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat()

        # Read existing entries (normalize by realpath to avoid duplicates)
        projects: dict[str, dict] = {}
        if self._projects_log.exists():
            try:
                data = yaml.safe_load(self._projects_log.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for entry in data:
                        if isinstance(entry, dict) and entry.get("path"):
                            key = os.path.realpath(entry["path"])
                            # Strip dead fields from pre-task-scheduler era
                            entry.pop("auto_evolve", None)
                            entry.pop("interval", None)
                            projects[key] = entry
            except (yaml.YAMLError, TypeError, OSError):
                logger.warning(
                    "_touch_project: failed to parse %s, rebuilding",
                    self._projects_log,
                    exc_info=True,
                )

        # Update or add entry
        if cwd in projects:
            projects[cwd]["last_active"] = now
        else:
            # Check if cwd is a subdirectory of an existing project.
            # Prefer the longest matching parent path (most specific).
            parent = ""
            for known_path in projects:
                if cwd.startswith(known_path + os.sep):
                    if len(known_path) > len(parent):
                        parent = known_path
            # Determine whether to merge into parent or create a new entry.
            # If cwd is itself a git repo root, or if the parent is the home
            # directory (which is not a real project), create a new entry.
            is_git_root = os.path.isdir(os.path.join(cwd, ".git"))
            if parent and not is_git_root and parent != home:
                # Merge into parent: child directory with no git repo of its own
                projects[parent]["last_active"] = now
            elif parent == home or (parent and is_git_root):
                # Parent is home dir or cwd is a git repo → create new entry
                name = os.path.basename(cwd.rstrip("/"))
                projects[cwd] = {
                    "name": name,
                    "path": cwd,
                    "last_active": now,
                }
                logger.info("new project tracked (git root under parent): %s", name)
                # Clean up stale home-dir entry if it exists
                if home in projects:
                    del projects[home]
                    logger.info("removed stale home-dir project entry")
            else:
                name = os.path.basename(cwd.rstrip("/"))
                projects[cwd] = {
                    "name": name,
                    "path": cwd,
                    "last_active": now,
                }
                logger.info("new project tracked: %s", name)

        # Build sorted YAML list
        entries = sorted(projects.values(), key=lambda e: e.get("path", ""))

        atomic_write_yaml(entries, self._projects_log, prefix=".projects_")

    async def _check_github_auth(self) -> dict:
        """Detect whether GitHub auth is configured (rant 2026-08-07T10:17:27).

        Runs the bundled ``gh auth status`` with a 10s timeout in a
        prompt-free environment. Returns:
            {"authenticated": bool, "user": str|None, "method": "gh"|"none"}
        Never raises; any failure degrades to {"authenticated": False, ...}.
        """
        _, gh = resolve_git_gh()
        if not gh:
            return {"authenticated": False, "user": None, "method": "none"}
        try:
            proc = await asyncio.create_subprocess_exec(
                gh, "auth", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=no_prompt_env(),
                **win32_no_window_kwargs(),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode("utf-8", errors="replace")
            user = parse_gh_auth_user(output)
            if user:
                return {"authenticated": True, "user": user, "method": "gh"}
            return {"authenticated": False, "user": None, "method": "none"}
        except (asyncio.TimeoutError, OSError, ValueError):
            return {"authenticated": False, "user": None, "method": "none"}

    async def _github_connect(self, token: str) -> dict:
        """Authenticate gh with a PAT (rant 2026-08-07T10:17:27 Stage 2).

        Runs ``gh auth login --with-token`` (token via stdin) followed by
        ``gh auth setup-git`` so git uses gh as its credential helper and
        push/pull/fetch never falls back to a GCM popup on Windows.
        Returns:
            {"ok": bool, "user": str|None, "error": str|None}
        Never raises; any failure degrades to {"ok": False, "error": ...}.
        """
        token = (token or "").strip()
        if not token:
            return {"ok": False, "user": None, "error": "empty token"}
        _, gh = resolve_git_gh()
        if not gh:
            return {"ok": False, "user": None, "error": "gh binary not found"}
        try:
            proc = await asyncio.create_subprocess_exec(
                gh, "auth", "login", "--with-token",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=no_prompt_env(),
                **win32_no_window_kwargs(),
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(token.encode("utf-8") + b"\n"), timeout=30
            )
            if proc.returncode != 0:
                output = stdout.decode("utf-8", errors="replace").strip()
                return {
                    "ok": False,
                    "user": None,
                    "error": output or f"gh auth login failed ({proc.returncode})",
                }
            # Re-verify via the same parser used by github_status.
            user = (await self._check_github_auth()).get("user")
            # setup-git: git must use gh as credential helper, otherwise
            # git push/pull/fetch would still trigger GCM (acceptance item).
            setup_ok = await self._gh_setup_git(gh)
            return {
                "ok": True,
                "user": user,
                "error": None if setup_ok else "auth ok but gh auth setup-git failed",
            }
        except (asyncio.TimeoutError, OSError, ValueError):
            return {"ok": False, "user": None, "error": "gh auth login failed"}

    async def _gh_setup_git(self, gh: str) -> bool:
        """Run ``gh auth setup-git`` so git uses gh as credential helper."""
        try:
            proc = await asyncio.create_subprocess_exec(
                gh, "auth", "setup-git",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=no_prompt_env(),
                **win32_no_window_kwargs(),
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            return proc.returncode == 0
        except (asyncio.TimeoutError, OSError, ValueError):
            return False

    async def _github_disconnect(self) -> dict:
        """Log out of gh (rant 2026-08-07T10:17:27 Stage 2).

        Returns {"ok": bool, "error": str|None}. Never raises.
        """
        _, gh = resolve_git_gh()
        if not gh:
            return {"ok": False, "error": "gh binary not found"}
        try:
            proc = await asyncio.create_subprocess_exec(
                gh, "auth", "logout", "--hostname", "github.com",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=no_prompt_env(),
                **win32_no_window_kwargs(),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                output = stdout.decode("utf-8", errors="replace").strip()
                return {
                    "ok": False,
                    "error": output or f"gh auth logout failed ({proc.returncode})",
                }
            return {"ok": True, "error": None}
        except (asyncio.TimeoutError, OSError, ValueError):
            return {"ok": False, "error": "gh auth logout failed"}

    # ── Device-flow auth (rant 2026-08-07T10:17:27 Stage 2b) ────────────
    # gh auth login --web prints a one-time code + device URL even with
    # stdin closed (probe-verified). We return those to the GUI, keep the
    # gh process alive in the background until the host authorizes in the
    # browser (or a timeout kills it), and the GUI discovers completion by
    # polling github_status.
    _GH_DEVICE_CODE_RE = re.compile(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})")
    _GH_DEVICE_URL_RE = re.compile(r"(https://github\.com/login/device)")
    _GH_DEVICE_TIMEOUT = 300

    async def _github_connect_web_start(self) -> dict:
        """Start a device-flow login. Returns {ok, code, url, user?, error?}.

        Cancels any previously pending device flow (only one may run).
        If already authenticated, short-circuits with ok=True + user so the
        GUI can just reflect the connected state.
        """
        status = await self._check_github_auth()
        if status.get("authenticated"):
            return {"ok": True, "code": None, "url": None,
                    "user": status.get("user"), "error": "already_authenticated"}
        _, gh = resolve_git_gh()
        if not gh:
            return {"ok": False, "code": None, "url": None,
                    "user": None, "error": "gh binary not found"}
        await self._github_connect_web_cancel()
        try:
            proc = await asyncio.create_subprocess_exec(
                gh, "auth", "login", "--web",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=no_prompt_env(),
                **win32_no_window_kwargs(),
            )
        except (OSError, ValueError):
            return {"ok": False, "code": None, "url": None,
                    "user": None, "error": "failed to start gh auth login --web"}
        code = None
        url = None
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace")
                if code is None:
                    m = self._GH_DEVICE_CODE_RE.search(line)
                    if m:
                        code = m.group(1)
                if url is None:
                    m = self._GH_DEVICE_URL_RE.search(line)
                    if m:
                        url = m.group(1)
                if code and url:
                    break
            if not (code and url):
                proc.kill()
                return {"ok": False, "code": None, "url": None,
                        "user": None, "error": "gh auth login --web produced no device code"}
        except (asyncio.CancelledError, OSError, ValueError):
            return {"ok": False, "code": None, "url": None,
                    "user": None, "error": "failed reading device code"}
        # Keep the process alive until the host authorizes; the GUI polls
        # github_status. On success gh writes its config and exits 0; on
        # timeout we kill it so a stale flow never lingers. The proc is also
        # kept on self so a cancel racing the task start still kills it.
        self._pending_web_auth_proc = proc
        self._pending_web_auth = asyncio.create_task(
            self._gh_web_auth_wait(proc)
        )
        return {"ok": True, "code": code, "url": url, "user": None, "error": None}

    async def _gh_web_auth_wait(self, proc) -> None:
        """Background: await gh auth login --web completion, timeout-kill."""
        try:
            await asyncio.wait_for(proc.communicate(), timeout=self._GH_DEVICE_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError, OSError, ValueError):
            try:
                proc.kill()
            except (OSError, ValueError):
                pass
        finally:
            if self._pending_web_auth is not None:
                self._pending_web_auth = None
            if getattr(self, "_pending_web_auth_proc", None) is proc:
                self._pending_web_auth_proc = None

    async def _github_connect_web_cancel(self) -> None:
        """Kill any pending device-flow gh process."""
        task = getattr(self, "_pending_web_auth", None)
        proc = getattr(self, "_pending_web_auth_proc", None)
        self._pending_web_auth = None
        self._pending_web_auth_proc = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if proc is not None:
            try:
                proc.kill()
            except (OSError, ValueError):
                pass

    async def _task_vibe_check(self, task_name: str, prompt: str, completion_summary: str) -> dict:
        """One-shot structured LLM ask (Ask mode, no tools, no history).

        Asks whether a just-finished scheduled task produced meaningful value.
        The agent must answer in strict JSON:
        ``{"meaningful": bool, "recommend_slowdown": bool, "reason": str, "done": str}``.

        ``done`` (rant 2026-08-18T21:32:32) is a natural-language summary of
        what meaningful work was done this cycle, for humans to read in the
        GUI task recent-runs table. Old models / old parsing omit it → "".

        Rant 2026-08-19T07:10:40 (root cause): the done frame used to carry an
        empty ``content``, so ``completion_summary`` here was empty and the
        memoryless LLM could not judge what happened. The done frame now
        carries the agent's full final reply (daemon.py done broadcast), so
        this prompt is evidence-driven: judge from the real final reply, not
        from an empty shell. System + user messages live in
        ``prompts/vibe_check.j2`` (same live-reload mechanism as system.j2).

        Raises on any failure (caller sends ``ok: false``); the scheduler
        conservatively leaves its empty-cycle counter unchanged then.
        """
        template = _get_jinja_env().get_template("vibe_check.j2")
        system = template.render(
            task_name=task_name or "",
            prompt=(prompt or "")[:2000],
            completion_summary=(completion_summary or "")[:3000],
        )
        msg = await self.llm.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": "请基于以上任务信息，严格按 system 中要求的 JSON 格式回答。"},
            ],
            tools=[],
        )
        content = (msg.get("content") or "").strip()
        # Tolerate markdown fences if the model wraps the JSON in ```json ... ```
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("vibe check response is not a JSON object")
        return {
            "meaningful": bool(data.get("meaningful")),
            "recommend_slowdown": bool(data.get("recommend_slowdown")),
            "reason": str(data.get("reason", ""))[:200],
            "done": str(data.get("done", ""))[:500],
        }

    def _build_system_prompt(self, session: Session | None = None) -> str:
        """Build the system prompt via Jinja2 template.

        Data collection here; rendering delegated to prompts/system.j2.
        """
        ctx: dict[str, Any] = {}

        # ── Environment ──
        ctx["current_time"] = datetime.now().astimezone().isoformat(timespec="seconds")
        ctx["os_name"] = platform.system()
        ctx["platform_detail"] = platform.platform()
        # Global config dir (~/.emrg) — injected so system.j2 can reference the
        # cross-project sessions index and other global data files by path.
        ctx["config_dir"] = str(config_dir())

        # ── Working Directory ──
        if session:
            ctx["working_dir"] = str(session.cwd)

        # ── Project Context Files ──
        if session:
            ctx["project_context"] = self._collect_project_context(session)

        # ── Skills ──
        if self.skills:
            ctx["skills"] = [
                {"name": s.name, "source": s.source, "path": s.path,
                 "description": s.description}
                for s in self.skills
            ]

        # ── Memory ──
        if session:
            mem = self._collect_memory_data(session)
            if mem:
                ctx.update(mem)

        # ── History ──
        if session:
            ctx["session"] = self._collect_history_data(session)

        template = _get_jinja_env().get_template("system.j2")
        rendered = template.render(**ctx)

        # Debug aid: save rendered system prompt to session dir
        if session:
            try:
                path = session.dir_path / "system.md"
                path.write_text(rendered, encoding="utf-8")
            except OSError:
                logger.debug("failed to write system.md", exc_info=True)

        return rendered

    def _collect_project_context(self, session: Session) -> list[dict[str, str]]:
        """Read project context files, return structured data for template."""
        candidates = ["CLAUDE.md", "AGENTS.md", "Agent.md", "MANIFESTO.md"]
        found: list[dict[str, str]] = []

        for name in candidates:
            path = session.cwd / name
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                    max_chars = 8000
                    if len(content) > max_chars:
                        content = content[:max_chars] + (
                            f"\n\n... [truncated {len(content) - max_chars} chars]"
                        )
                    found.append({"name": name, "content": content})
                except (OSError, UnicodeDecodeError):
                    logger.debug("failed to read %s", path, exc_info=True)

        return found

    def _collect_memory_data(self, session: Session) -> dict[str, Any] | None:
        """Read memory indexes, return structured data for template."""
        data: dict[str, Any] = {"has_memories": False}

        project_dir = session.cwd / ".emrg" / "memory"
        pindex_path = project_dir / "MEMORY.md"
        if pindex_path.exists():
            data["project_memory_index"] = pindex_path.read_text(encoding="utf-8")
            data["project_memory_dir"] = str(project_dir)
            data["project_memory_index_path"] = str(pindex_path)
            data["has_memories"] = True

        smem_dir = session.memory_dir
        sindex_path = smem_dir / "MEMORY.md"
        if sindex_path.exists():
            data["session_memory_index"] = sindex_path.read_text(encoding="utf-8")
            data["session_memory_dir"] = str(smem_dir)
            data["session_memory_index_path"] = str(sindex_path)
            data["has_memories"] = True

        return data if data["has_memories"] else None

    def _collect_history_data(self, session: Session) -> dict[str, str]:
        """Return structured session/history data for template."""
        return {
            "id": session.session_id,
            "dir_path": str(session.dir_path),
            "date_str": datetime.now().strftime("%y%m%d"),
        }

    async def _process_message(
        self, msg: dict, ws
    ) -> None:
        """Process a single message and send responses."""
        msg_type = msg.get("type", "")

        if msg_type == "ping":
            elapsed = int(
                (datetime.now() - self.start_time).total_seconds()
            )
            pong = ServerPong(
                identity={
                    "instance_id": self.identity.instance_id,
                    "host_name": self.identity.host_name,
                    "fork_source": self.identity.fork_source,
                    "branch_id": self.identity.branch_id,
                },
                uptime_seconds=max(0, elapsed),
                evolution_count=self._evolution_count(),
            )
            await self._send(ws, {
                "type": "pong",
                "identity": pong.identity,
                "uptime_seconds": pong.uptime_seconds,
                "evolution_count": pong.evolution_count,
                "started_at": self.start_time.isoformat(),
                "pid": os.getpid(),
                "model": self.llm.config.model,
            })
            return

        elif msg_type == "init_auto_evolve":
            cwd = msg.get("cwd", "")
            if cwd:
                self._touch_project(cwd)
                # Create a task entry in tasks.yml
                name = os.path.basename(cwd.rstrip("/"))
                if self._scheduler:
                    self._scheduler.create_task(
                        name=name, task_type="evolution",
                        config={"project": name}, interval=600,
                    )
                await self._send(ws, {
                    "ok": True,
                    "message": f"auto_evolve enabled for {cwd}",
                })
            else:
                await self._send(ws, {
                    "ok": False,
                    "error": "init_auto_evolve requires cwd",
                })
            return

        elif msg_type == "list_tasks":
            if self._scheduler:
                tasks = self._scheduler.list_tasks()
            else:
                tasks = []
            await self._send(ws, {
                "type": "tasks_list",
                "tasks": tasks,
            })

        elif msg_type == "trigger_task":
            name = msg.get("name", "").strip()
            if not name:
                await self._send(ws, {
                    "type": "trigger_result",
                    "error": "trigger_task requires task name",
                })
                return
            if self._scheduler:
                result = self._scheduler.trigger_task(name)
                if result is None:
                    await self._send(ws, {
                        "type": "trigger_result",
                        "error": f"task '{name}' not found",
                    })
                else:
                    await self._send(ws, {
                        "type": "trigger_result",
                        **result,
                    })
            else:
                await self._send(ws, {
                    "type": "trigger_result",
                    "error": "scheduler not running",
                })

        elif msg_type == "task_create":
            # rant 2026-08-12T18:23:15 P2 — task CRUD with hot reload
            if not self._scheduler:
                await self._send(ws, {"type": "task_result", "error": "scheduler not running"})
                return
            ok, res = self._scheduler.task_create(
                name=msg.get("name", "").strip(),
                task_type=msg.get("task_type", "").strip(),
                project=msg.get("project", "").strip(),
                interval=msg.get("interval"),
                enabled=msg.get("enabled", True),
                repo=msg.get("repo"),
                description=msg.get("description"),
            )
            if not ok:
                await self._send(ws, {"type": "task_result", "error": res})
                return
            summary = await self._scheduler.apply_tasks(self._scheduler._load_tasks())
            await self._send(ws, {"type": "task_result", "ok": True, "task": res, "summary": summary})

        elif msg_type == "task_update":
            if not self._scheduler:
                await self._send(ws, {"type": "task_result", "error": "scheduler not running"})
                return
            fields = {k: msg[k] for k in ("task_type", "project", "interval", "enabled", "repo", "description") if k in msg}
            if "task_type" in fields:
                fields["type"] = fields.pop("task_type")
            ok, res = self._scheduler.task_update(msg.get("name", "").strip(), **fields)
            if not ok:
                await self._send(ws, {"type": "task_result", "error": res})
                return
            summary = await self._scheduler.apply_tasks(self._scheduler._load_tasks())
            await self._send(ws, {"type": "task_result", "ok": True, "task": res, "summary": summary})

        elif msg_type == "task_delete":
            if not self._scheduler:
                await self._send(ws, {"type": "task_result", "error": "scheduler not running"})
                return
            ok, err = self._scheduler.task_delete(msg.get("name", "").strip())
            if not ok:
                await self._send(ws, {"type": "task_result", "error": err})
                return
            summary = await self._scheduler.apply_tasks(self._scheduler._load_tasks())
            await self._send(ws, {"type": "task_result", "ok": True, "summary": summary})

        elif msg_type == "task_template_list":
            if not self._scheduler:
                await self._send(ws, {"type": "templates_list", "templates": []})
                return
            await self._send(ws, {
                "type": "templates_list",
                "templates": self._scheduler.list_templates(),
            })

        elif msg_type == "task_template_create":
            if not self._scheduler:
                await self._send(ws, {"type": "template_result", "error": "scheduler not running"})
                return
            ok, err = self._scheduler.template_create(
                msg.get("name", "").strip(), msg.get("prompt", "")
            )
            await self._send(ws, {"type": "template_result", "ok": ok, **({"error": err} if not ok else {})})

        elif msg_type == "task_template_update":
            if not self._scheduler:
                await self._send(ws, {"type": "template_result", "error": "scheduler not running"})
                return
            ok, err = self._scheduler.template_update(
                msg.get("name", "").strip(), msg.get("prompt", "")
            )
            await self._send(ws, {"type": "template_result", "ok": ok, **({"error": err} if not ok else {})})

        elif msg_type == "task_template_delete":
            if not self._scheduler:
                await self._send(ws, {"type": "template_result", "error": "scheduler not running"})
                return
            ok, err = self._scheduler.template_delete(msg.get("name", "").strip())
            await self._send(ws, {"type": "template_result", "ok": ok, **({"error": err} if not ok else {})})

        elif msg_type == "compact":
            cwd = msg.get("cwd", "")
            session_id = msg.get("session_id", "")

            if not session_id or not cwd:
                await self._send(ws, {
                    "type": "compact_result",
                    "error": "compact requires session_id and cwd",
                })
                return

            session = self._get_or_create_session(session_id, Path(cwd))
            await self._handle_compact(session, ws)

        elif msg_type == "list_sessions":
            cwd = msg.get("cwd", "")
            if not cwd:
                await self._send(ws, {
                    "type": "sessions_list",
                    "error": "list_sessions requires cwd",
                })
                return
            await self._handle_list_sessions(Path(cwd), ws)

        elif msg_type == "resume_session":
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")
            if not session_id or not cwd:
                await self._send(ws, {
                    "type": "resume_result",
                    "error": "resume_session requires session_id and cwd",
                })
                return
            await self._handle_resume_session(session_id, Path(cwd), ws)

        elif msg_type == "rename_session":
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")
            title = msg.get("title", "")
            if not session_id or not cwd:
                await self._send(ws, {
                    "type": "rename_result",
                    "error": "rename requires session_id and cwd",
                })
                return

            session = self._get_or_create_session(session_id, Path(cwd))
            if not title:
                # Auto-generate title via LLM
                title = await self._generate_session_title(session)
            session.rename(title)
            await self._send(ws, {
                "type": "rename_result",
                "session_id": session_id,
                "title": title,
            })

        elif msg_type == "list_memories":
            scope = msg.get("scope", "project")
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")

            if scope == "session" and (not session_id or not cwd):
                await self._send(ws, {
                    "type": "memories_list",
                    "error": "session scope requires session_id and cwd",
                })
                return

            await self._handle_list_memories(scope, session_id, cwd, ws)

        elif msg_type == "read_memory":
            scope = msg.get("scope", "project")
            memory_id = msg.get("memory_id", "")
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")

            if not memory_id:
                await self._send(ws, {
                    "type": "memory_content",
                    "error": "read_memory requires memory_id",
                })
                return

            if scope == "session" and (not session_id or not cwd):
                await self._send(ws, {
                    "type": "memory_content",
                    "error": "session scope requires session_id and cwd",
                })
                return

            await self._handle_read_memory(scope, memory_id, session_id, cwd, ws)

        elif msg_type == "list_files":
            # GUI right-panel workspace file browser (rant 2026-08-11T12:20:35 P1.1)
            path_str = msg.get("path", "")
            if not path_str:
                await self._send(ws, {
                    "type": "files_list",
                    "error": "list_files requires path",
                })
                return
            await self._handle_list_files(path_str, ws)

        elif msg_type == "read_file":
            # GUI right-panel file viewer (rant 2026-08-11T12:20:35 P1.1)
            path_str = msg.get("path", "")
            if not path_str:
                await self._send(ws, {
                    "type": "file_content",
                    "error": "read_file requires path",
                })
                return
            await self._handle_read_file(
                path_str,
                msg.get("start_line"),
                msg.get("line_limit"),
                ws,
            )

        elif msg_type == "rant":
            # Store user rant/feedback for evolution analysis
            rant_message = msg.get("message", "").strip()
            if not rant_message:
                await self._send(ws, {"error": "rant requires a message"})
                return

            # Optional project targeting (multi-project support)
            project = msg.get("project", "").strip()

            # Shared write logic (rant 2026-08-17T11:51:59): daemon ``rant``
            # command and the submit_rant tool use the same append_rant, so
            # the file format / sort / daemon-authoritative timestamp stay
            # consistent no matter which path recorded the rant.
            count = append_rant(self._rants_log, rant_message, project)

            logger.info("rant recorded (%d total)%s: %s",
                count, f" project={project}" if project else "", _redact_string(rant_message[:100]))
            await self._send(ws, {"ok": True, "count": count})

        elif msg_type == "list_rants":
            # Rant panel (rant 2026-08-13T14:10:14 P4): read ~/.emrg/rants.jsonl,
            # optional status filter (pending/in_progress/completed/"" = all).
            try:
                filter_status = str(msg.get("status", "") or "").strip()
                rants = []
                if self._rants_log.exists():
                    with open(self._rants_log, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                r = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if filter_status and r.get("status", "pending") != filter_status:
                                continue
                            rants.append(r)
                # 时间倒序（最新在前，面板列表惯例）
                rants.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
                await self._send(ws, {
                    "type": "rants_list",
                    "rants": rants,
                })
            except OSError as e:
                logger.exception("list_rants: failed to read %s", self._rants_log)
                await self._send(ws, {"type": "rants_list", "rants": [], "error": str(e)})

        elif msg_type == "skills_available":
            # Installable-skills catalog (rant 2026-08-08T10:14:29): list
            # catalog skills with installed/managed status.
            entries = load_catalog_skills()
            installed = {s.name for s in self.skills}
            result = [
                {
                    "name": e.get("name", ""),
                    "description": e.get("description", ""),
                    "installed": e.get("name", "") in installed,
                    "managed": skill_is_managed(e.get("name", "")),
                }
                for e in entries
            ]
            await self._send(ws, {"type": "skills_available_result", "skills": result})

        elif msg_type == "skills_install":
            # /skills install <name> — host-confirmed CLI install, then
            # self-publish skill files into ~/.emrg/skills/.
            from emrg.skills.installer import install_skill

            name = msg.get("name", "").strip()
            confirmed = bool(msg.get("confirmed", False))
            if not name:
                await self._send(ws, {
                    "type": "skills_install_result",
                    "error": "skills_install requires a skill name",
                })
                return
            result = await install_skill(name, confirmed=confirmed)
            if result.get("ok"):
                # Reload so the next system-prompt build includes the skill
                # (design: "下次构建系统提示即含该技能", no daemon restart needed).
                self.skills = load_skills()
            await self._send(ws, {"type": "skills_install_result", "name": name, **result})

        elif msg_type == "skills_update":
            # /skills update — refresh managed skills to latest releases.
            from emrg.skills.installer import update_managed_skills

            result = await update_managed_skills()
            await self._send(ws, {"type": "skills_update_result", **result})

        elif msg_type == "list_models":
            await self._handle_list_models(ws)

        elif msg_type == "set_model":
            model_name = msg.get("model", "").strip()
            if not model_name:
                await self._send(ws, {
                    "type": "model_set",
                    "error": "set_model requires model name",
                })
                return
            await self._handle_set_model(model_name, ws)

        elif msg_type == "list_projects":
            await self._handle_list_projects(ws)

        elif msg_type == "remove_project":
            # P1 GUI multi-session (rant 2026-08-10T15:07:19): drop a
            # projects.yml entry by name (disk session data preserved).
            name = msg.get("name", "").strip()
            if not name:
                await self._send(ws, {
                    "type": "project_removed",
                    "removed": False,
                    "error": "remove_project requires name",
                })
                return
            await self._handle_remove_project(name, ws)

        elif msg_type == "evolution_summary":
            # WorkBuddy P3 (rant 21:35): self-evolution visibility.
            # Low-cost: read evolution log files (~/.emrg/logs/evolution-*.json)
            # written by TaskHandler; return count + recent N summaries.
            limit = msg.get("limit", 5)
            try:
                logs_dir = config_dir() / "logs"
                files = sorted(
                    logs_dir.glob("evolution-*.json"),
                    key=lambda p: p.name,
                    reverse=True,
                )[: max(1, min(int(limit), 20))]
                recent = []
                for f in files:
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        recent.append({
                            "timestamp": data.get("timestamp", ""),
                            "impact": data.get("impact", []),
                            "operations": data.get("operations", []),
                        })
                    except (json.JSONDecodeError, OSError):
                        continue
                await self._send(ws, {
                    "type": "evolution_summary",
                    "count": self._evolution_count(),
                    "recent": recent,
                })
            except OSError:
                await self._send(ws, {
                    "type": "evolution_summary",
                    "count": self._evolution_count(),
                    "recent": [],
                })

        elif msg_type == "github_status":
            # Windows GCM rant (2026-08-07T10:17:27): GUI queries whether
            # GitHub auth is configured so it can show a connect banner only
            # when evolution actually needs GitHub.
            auth = await self._check_github_auth()
            await self._send(ws, {"type": "github_status", **auth})

        elif msg_type == "update_check":
            # Auto update-check prompt (rant 2026-08-10T07:12:12): TUI/GUI
            # query the daemon's latest known release. Returns the cached
            # latest_version (populated at startup + every TTL) plus a
            # has_update flag computed against the running version. No auto
            # download/install; the client shows a one-time prompt.
            # force:true (rant 2026-08-11T09:18:16) — GUI manual check
            # button: run a fresh GitHub fetch first instead of the cache.
            import emrg
            from emrg.config import load_update_config
            from emrg.update_check import (
                is_newer,
                load_state,
                parse_version,
                run_update_check_once,
            )

            if msg.get("force"):
                try:
                    await run_update_check_once()
                except Exception:
                    logger.debug("forced update check failed", exc_info=True)
            current = getattr(emrg, "__version__", "0")
            state = load_state()
            latest = state.get("latest_version") or ""
            has_update = bool(
                latest and is_newer(parse_version(latest), parse_version(current))
            )
            await self._send(ws, {
                "type": "update_check",
                "current_version": current,
                "latest_version": latest,
                "has_update": has_update,
                "prompted_version": state.get("prompted_version") or "",
                # rant 2026-08-12T12:10:12: auto-download state — GUI shows the
                # "ready to install" button when downloaded_version is newer.
                "downloaded_version": state.get("downloaded_version") or "",
                "downloaded_path": state.get("downloaded_path") or "",
                "downloaded_sha": state.get("downloaded_sha") or "",
                "enabled": load_update_config().check,
            })

        elif msg_type == "update_check_prompted":
            # Idempotency (rant 07:12:12 §4): a client records that it showed
            # the prompt for this version — same version never re-prompted.
            from emrg.update_check import load_state, mark_prompted

            version = msg.get("version", "")
            if version:
                mark_prompted(load_state(), version)
            await self._send(ws, {"type": "update_check_prompted", "ok": True})

        elif msg_type == "github_connect":
            # Windows GCM rant Stage 2: GUI PAT auth — gh auth login
            # --with-token + gh auth setup-git (git no longer touches GCM).
            token = msg.get("token", "")
            result = await self._github_connect(token)
            await self._send(ws, {"type": "github_connect_result", **result})

        elif msg_type == "github_disconnect":
            # Windows GCM rant Stage 2: GUI disconnect — gh auth logout.
            result = await self._github_disconnect()
            await self._send(ws, {"type": "github_disconnect_result", **result})

        elif msg_type == "github_connect_web":
            # Windows GCM rant Stage 2b: device-flow login — gh auth login
            # --web; the GUI shows the one-time code and polls github_status
            # until the host authorizes in the browser.
            result = await self._github_connect_web_start()
            await self._send(ws, {"type": "github_connect_web_result", **result})

        elif msg_type == "clear_session":
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")
            if not session_id or not cwd:
                await self._send(ws, {
                    "type": "clear_result",
                    "error": "clear_session requires session_id and cwd",
                })
                return
            session = self._get_or_create_session(session_id, Path(cwd))
            session.clear()
            # P1 (rant 21:55:37) Change F: clearing a session also drops its
            # pending queue (queued messages are stale after clear).
            dropped = self._session_pending.pop(session_id, [])
            if dropped:
                await self._broadcast(session_id, {
                    "type": "queued_cancelled",
                    "session_id": session_id,
                })
            await self._send(ws, {
                "type": "clear_result",
                "session_id": session_id,
                "ok": True,
            })
            logger.info("session cleared: %s", session_id)

        elif msg_type == "delete_session":
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")
            if not session_id or not cwd:
                await self._send(ws, {
                    "type": "session_deleted",
                    "error": "delete_session requires session_id and cwd",
                })
                return

            session_dir = Path(cwd) / ".emrg" / "sessions" / session_id
            if not session_dir.exists():
                await self._send(ws, {
                    "type": "session_deleted",
                    "error": f"Session {session_id} not found",
                })
                return

            deleted = Session.delete(session_id, Path(cwd))
            if deleted:
                # P1 (rant 21:55:37) Change F: deleting the session also
                # drops its pending queue.
                dropped = self._session_pending.pop(session_id, [])
                if dropped:
                    await self._broadcast(session_id, {
                        "type": "queued_cancelled",
                        "session_id": session_id,
                    })
                await self._send(ws, {
                    "type": "session_deleted",
                    "session_id": session_id,
                    "ok": True,
                })
                logger.info("session deleted: %s", session_id)
            else:
                await self._send(ws, {
                    "type": "session_deleted",
                    "error": f"Failed to delete session {session_id}",
                })

        elif msg_type == "list_history":
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")
            if not session_id or not cwd:
                await self._send(ws, {
                    "type": "history_list",
                    "error": "list_history requires session_id and cwd",
                })
                return
            session = self._get_or_create_session(session_id, Path(cwd))
            records = session._read_history()
            # Collect user messages with their record index
            user_messages: list[dict] = []
            for i, r in enumerate(records):
                if r.get("type") == "message" and r.get("role") == "user":
                    content = r.get("content", "")
                    # Truncate long messages for display
                    preview = content[:80] + ("…" if len(content) > 80 else "")
                    user_messages.append({
                        "record_index": i,
                        "content": content,
                        "preview": preview,
                        "timestamp": r.get("timestamp", ""),
                    })
            # Optional pagination (rant 2026-08-13T14:15:12): limit/offset
            # count from the NEWEST message backwards (offset=0 = latest).
            # Absent limit = full list (backward compatible, used by /rewind).
            limit = msg.get("limit")
            offset = msg.get("offset", 0)
            has_more = False
            if limit is not None:
                total = len(user_messages)
                end = max(0, total - offset)
                start = max(0, end - limit)
                has_more = start > 0
                user_messages = user_messages[start:end]
            await self._send(ws, {
                "type": "history_list",
                "session_id": session_id,
                "messages": user_messages,
                "has_more": has_more,
            })

        elif msg_type == "rewind_session":
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")
            record_index = msg.get("record_index")
            if not session_id or not cwd or record_index is None:
                await self._send(ws, {
                    "type": "rewind_result",
                    "error": "rewind_session requires session_id, cwd, and record_index",
                })
                return
            session = self._get_or_create_session(session_id, Path(cwd))
            records = session._read_history()
            if record_index < 0 or record_index >= len(records):
                await self._send(ws, {
                    "type": "rewind_result",
                    "error": f"record_index {record_index} out of range (0-{len(records)-1})",
                })
                return
            # Truncate: keep records up to (not including) record_index
            truncated = records[:record_index]
            session._write_history(truncated)
            # Update meta
            session._message_count = sum(
                1 for r in truncated if r.get("type") == "message"
            )
            session._updated_at = datetime.now().isoformat()
            session._save_meta()
            await self._send(ws, {
                "type": "rewind_result",
                "session_id": session_id,
                "ok": True,
                "record_index": record_index,
                "removed_count": len(records) - len(truncated),
            })
            logger.info("session rewound: %s at index %d (removed %d records)",
                        session_id, record_index, len(records) - len(truncated))

        elif msg_type == "task_vibe_check":
            # Rant 2026-08-17T11:39:19: scheduler asks the agent, after a
            # scheduled task completes, whether the round produced meaningful
            # value — replacing the git-HEAD empty-cycle heuristic (HEAD
            # measures commits, not value: analysis/memory work without a
            # commit was miscounted as empty, and a no-op round over someone
            # else's push counted as work). One-shot Ask-mode LLM call (no
            # tools, no session history) with a strict JSON contract.
            task_name = msg.get("task_name", "")
            prompt = msg.get("prompt", "")
            summary = msg.get("completion_summary", "")
            try:
                result = await self._task_vibe_check(task_name, prompt, summary)
                await self._send(ws, {
                    "type": "vibe_check_result",
                    "ok": True,
                    "result": result,
                })
            except Exception as e:  # noqa: BLE001 — best-effort, never fatal
                logger.warning("task_vibe_check failed: %s", e)
                await self._send(ws, {
                    "type": "vibe_check_result",
                    "ok": False,
                    "error": str(e)[:200],
                })

        elif msg_type == "shutdown":
            logger.info("shutdown requested by client")
            await self._send(ws, {"type": "shutdown_ack"})
            try:
                await ws.close()
            except Exception:
                pass
            self._server.close()

        else:
            await self._send(ws, {
                "error": "unknown message type",
                "received": msg_type,
            })

    def _get_or_create_session(self, session_id: str, cwd: Path) -> Session:
        """Load an existing session or create a new one."""
        session_dir = cwd / ".emrg" / "sessions" / session_id
        if session_dir.exists() and (session_dir / "meta.json").exists():
            return Session.load(session_id, cwd)
        return Session.create_with_id(session_id, cwd)

    @staticmethod
    def _build_user_content(text: str, images: list[dict] | None, vision: bool = False) -> list[dict] | str:
        """Build OpenAI vision content array when images are present.
        
        Returns plain str if no images, list[dict] for vision format.
        If vision=False, images are degraded to text placeholders.
        """
        if not images:
            return text
        if not vision:
            # Model doesn't support vision: degrade images to text placeholders
            labels = [img.get("label", "?") for img in images]
            return f"[用户粘贴了 {len(images)} 张图片: {', '.join(labels)}。当前模型不支持图片理解，请回复用户告知此限制。]\n\n{text}"
        content: list[dict] = []
        last_pos = 0
        for img in sorted(images, key=lambda i: i.get("position", -1)):
            pos = img.get("position", -1)
            # Text before this image
            if pos >= 0 and last_pos < pos:
                chunk = text[last_pos:pos]
                if chunk.strip():
                    content.append({"type": "text", "text": chunk})
                last_pos = pos
            # Encode image
            try:
                b64 = base64.b64encode(Path(img["path"]).read_bytes()).decode()
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                })
            except (OSError, FileNotFoundError) as e:
                logger.warning("failed to read image %s: %s", img.get("path"), e)
                content.append({"type": "text", "text": f"[Image unavailable: {img.get('label', '?')}]"})
        # Remaining text after last image
        if last_pos < len(text):
            chunk = text[last_pos:]
            if chunk.strip():
                content.append({"type": "text", "text": chunk})
        # OpenAI requires at least one text element
        if not any(item.get("type") == "text" for item in content):
            content.insert(0, {"type": "text", "text": "请分析这张图片"})
        return content

    # ── Phase 2 session-lock wrappers (protocol-contract §2.6.5) ──
    # The caller fire-and-forgets with asyncio.create_task() and never awaits,
    # so the lock MUST be released inside the task (wrapper finally) — not in
    # the caller. cancel path: _tool_task.cancel() → task cancelled → wrapper
    # finally runs → lock released. This also roots out the multi-connection
    # write race (§5): session writes are serialized by the single active task.

    async def _inject_pending_messages(
        self, session: Session, messages: list[dict],
    ) -> tuple[int, bool]:
        """Pop the session's pending queue and inject it into `messages`.

        P1 queue-injection (rant 2026-08-10T21:55:37), aligned with codex
        steer_input: messages sent while the tool loop is busy are queued per
        session and injected at the next round boundary (after the current
        round's tools, before the next LLM request).

        Uses pop() for atomic removal — while we await broadcasts, the read
        loop may append new messages to the same list; popping the whole list
        means those land in a fresh list (setdefault) and are injected next
        round. Never dropped.

        Each injected message is persisted (append_message) so auto-compact
        rebuilding from history keeps it, and a ``steer_committed`` broadcast
        tells clients the message was committed into the turn.

        Returns ``(injected_count, ask_injected)`` — ``ask_injected`` is True
        when any queued message was Ask mode (mode=ask); the caller must use
        an empty tool set for the round that processes it.
        """
        sid = session.session_id
        pending = self._session_pending.pop(sid, [])
        if not pending:
            return 0, False
        ask_injected = any(not allow for _, allow in pending)
        for preq, _ in pending:
            pcontent = self._build_user_content(
                preq.prompt, preq.images, self.llm.config.vision
            )
            messages.append({"role": "user", "content": pcontent})
            record: dict = {"type": "message", "role": "user", "content": preq.prompt}
            if preq.images:
                record["images"] = preq.images
            session.append_message(record)
            await self._broadcast(sid, {
                "type": "steer_committed",
                "request_id": preq.id,
                "session_id": sid,
            })
        return len(pending), ask_injected

    async def _run_tool_loop_locked(
        self, req: TaskRequest, ws, session: Session,
        cancel_event: asyncio.Event | None = None,
        allow_tools: bool = True,
    ) -> None:
        """Run _run_tool_loop and release the session busy lock on exit."""
        session_id = session.session_id
        normal_end = False
        try:
            await self._run_tool_loop(req, ws, session, cancel_event, allow_tools)
            normal_end = True
        finally:
            self._session_busy[session_id] = False
            # P1 (rant 21:55:37): messages still queued when the loop ends are
            # not lost. We do NOT start a follow-up task here (_tool_task /
            # _cancel_event are read-loop locals — a hand-off would break
            # cancel + busy tracking); instead:
            #   normal end → queued_requeue → clients auto re-send (busy is
            #     now released, the re-send goes through the normal path)
            #   cancel / error / disconnect → queued_cancelled (queue dropped)
            pending = self._session_pending.pop(session_id, [])
            if pending:
                # A cancel (even one caught and returned from inside the loop)
                # must NOT auto-requeue: the user stopped the turn. Exception /
                # disconnect also drop the queue. Only a clean turn end
                # re-sends the queued messages.
                if normal_end and not (cancel_event and cancel_event.is_set()):
                    await self._broadcast(session_id, {
                        "type": "queued_requeue",
                        "session_id": session_id,
                        "request_ids": [r.id for r, _ in pending],
                    })
                else:
                    await self._broadcast(session_id, {
                        "type": "queued_cancelled",
                        "session_id": session_id,
                    })

    async def _run_tool_loop(
        self, req: TaskRequest, ws, session: Session,
        cancel_event: asyncio.Event | None = None,
        allow_tools: bool = True,
    ) -> None:
        """Run the streaming tool-calling loop with session persistence.

        The core loop:
        1. Load history from session, append current user message
        2. Send messages + tools to LLM
        3. Stream deltas to client (text content)
        4. If finish_reason == "tool_calls": execute tools, persist results,
           notify client, loop back to step 2
        5. If finish_reason == "stop": persist final answer, done
        6. Safety: max_tool_rounds prevents infinite loops

        Supports cancellation via cancel_event (checked between rounds) and
        asyncio task cancellation (interrupts streaming mid-round).

        allow_tools=False (Ask mode, WorkBuddy P2) sends an empty tool set —
        the LLM can only reply in plain chat, the loop exits after round 1.
        """
        system_prompt = self._build_system_prompt(session)
        history_messages = session.get_messages_for_llm()

        # Persist user message (with image references if present)
        user_record: dict = {
            "type": "message",
            "role": "user",
            "content": req.prompt,
        }
        if req.images:
            user_record["images"] = req.images
        session.append_message(user_record)

        user_content = self._build_user_content(req.prompt, req.images, self.llm.config.vision)
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            *history_messages,
            {"role": "user", "content": user_content},
        ]
        tools_base = self.tools.to_openai_tools() if allow_tools else []
        # P1 (rant 21:55:37): injection rounds do NOT consume the round budget;
        # force_ask latches "an Ask message was injected outside the round-top
        # injection (stop / Case 3 / loop-end)" so the next round uses an empty
        # tool set for it.
        force_ask = False
        round_num = 1
        while True:
            if round_num > self._max_tool_rounds:
                # P1 (rant 21:55:37): round budget exhausted but messages
                # still queued — process them with a fresh round budget
                # instead of stranding them; only fall back to the
                # "Exceeded maximum" error when the queue is empty.
                _n, _ask = await self._inject_pending_messages(session, messages)
                if _n:
                    force_ask = _ask
                    round_num = 1
                    continue

                # Exceeded max tool rounds
                logger.warning("max tool rounds (%d) exceeded for task %s",
                               self._max_tool_rounds, req.id)
                await self._broadcast(session.session_id, {
                    "request_id": req.id,
                    "content": f"Exceeded maximum tool call rounds ({self._max_tool_rounds}).",
                    "done": True,
                    "delta": False,
                    "session_id": session.session_id,
                })

                # Fire-and-forget: reflect on whether to save memories
                self._maybe_reflect_memory(session, req.prompt, full_content)
                return

            tools_openai = tools_base
            # Check for cancellation between rounds
            if cancel_event and cancel_event.is_set():
                logger.info("tool loop cancelled by client at round %d", round_num)
                await self._broadcast(session.session_id, {
                    "request_id": req.id,
                    "content": "",
                    "done": True,
                    "cancelled": True,
                    "session_id": session.session_id,
                })
                return

            # P1 queue-injection: drain pending at the round boundary (after
            # this round's tools, before the next LLM request — codex steer).
            _injected, _ask = await self._inject_pending_messages(session, messages)
            if _ask or force_ask:
                tools_openai = []
            force_ask = False

            logger.debug("tool loop round %d: %d messages, %d tools",
                         round_num, len(messages), len(tools_openai))

            # Auto-compact: if token count exceeds threshold, compact before this round
            if self.llm.config.auto_compact_threshold > 0.0:
                estimated = self._estimate_tokens(messages)
                trigger_at = int(
                    self.llm.config.context_window
                    * self.llm.config.auto_compact_threshold
                )
                if estimated > trigger_at:
                    logger.info(
                        "auto-compact triggered: ~%d tokens > %d (threshold=%.0f%%)",
                        estimated, trigger_at,
                        self.llm.config.auto_compact_threshold * 100,
                    )
                    # Notify client (broadcast to all session subscribers)
                    await self._broadcast(session.session_id, {
                        "type": "compact_result",
                        "session_id": session.session_id,
                        "messages_compacted": 0,
                        "summary": f"Auto-compacting... (context ~{estimated} tokens, threshold {trigger_at})",
                        "auto": True,
                    })
                    try:
                        records = session._read_history()
                        try:
                            summary = await self._do_compact(session, records)
                        except RuntimeError as e:
                            err_msg = str(e).lower()
                            if "context" in err_msg or "too long" in err_msg or "400" in str(e):
                                logger.warning(
                                    "auto-compact: normal failed, trying chunked: %s", e
                                )
                                summary = await self._chunked_compact(records)
                            else:
                                raise
                        count = session.compact(summary, keep_recent=5)
                        logger.info("auto-compact done: %d messages compacted", count)
                        await self._broadcast(session.session_id, {
                            "type": "compact_result",
                            "session_id": session.session_id,
                            "messages_compacted": count,
                            "summary": summary,
                            "auto": True,
                        })
                        # Rebuild messages from compacted history
                        history_messages = session.get_messages_for_llm()
                        messages = [
                            {"role": "system", "content": system_prompt},
                            *history_messages,
                            {"role": "user", "content": req.prompt},
                        ]
                    except Exception:
                        logger.exception("auto-compact failed")

            # Streaming call to LLM
            content_parts: list[str] = []
            reasoning_parts: list[str] = []  # think block, llm.jsonl only (rant 2026-08-18T09:43:23)
            tc_by_index: dict[int, dict] = {}
            final_finish = None
            final_usage: dict | None = None

            try:
                async for delta in self.llm.chat_stream(messages, tools=tools_openai):
                    # Check for cancellation mid-stream (ESC in client)
                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError()

                    c = delta.get("content")
                    if c:
                        content_parts.append(c)
                        await self._broadcast(session.session_id, {
                            "request_id": req.id,
                            "content": c,
                            "done": False,
                            "delta": True,
                            "session_id": session.session_id,
                        })

                    # Accumulate reasoning (think) — NOT broadcast, NOT persisted
                    # into session messages; only lands in the llm.jsonl response
                    # record via _log_llm_exchange (rant 2026-08-18T09:43:23).
                    r = delta.get("reasoning")
                    if r:
                        reasoning_parts.append(r)

                    # Track accumulated tool calls for finalization
                    tcs = delta.get("tool_calls")
                    if tcs:
                        for tc in tcs:
                            idx = tc.get("index", 0) if "index" in tc else 0
                            tc_by_index[idx] = tc

                    fr = delta.get("finish_reason")
                    if fr:
                        final_finish = fr

                    usage = delta.get("usage")
                    if usage:
                        final_usage = usage
            except asyncio.CancelledError:
                logger.info("tool loop cancelled mid-stream in round %d", round_num)
                await self._broadcast(session.session_id, {
                    "request_id": req.id,
                    "content": "",
                    "done": True,
                    "cancelled": True,
                    "session_id": session.session_id,
                })
                return
            except Exception as e:
                logger.exception("LLM stream error in round %d", round_num)
                await self._broadcast(session.session_id, {
                    "error": f"LLM error: {e}. Check config at ~/.emrg/config.toml",
                })
                # Send done so the client knows the stream is over.
                # Without this, the client stays in its read loop → deadlock.
                await self._broadcast(session.session_id, {
                    "done": True,
                    "request_id": req.id,
                })
                return

            full_content = "".join(content_parts)
            full_reasoning = "".join(reasoning_parts) or None
            logger.debug("round %d finish: %s, tool_calls=%d, content_len=%d",
                         round_num, final_finish, len(tc_by_index), len(full_content))

            # Case 1: Final text answer — no more tool calls
            if final_finish == "stop" or (final_finish and not tc_by_index):
                # Log LLM response
                self._log_llm_exchange(
                    session, [dict(m) for m in messages], tools_openai,
                    full_content, final_finish, final_usage,
                    reasoning=full_reasoning,
                )

                # Persist assistant message
                session.append_message({
                    "type": "message",
                    "role": "assistant",
                    "content": full_content,
                })

                # Append the assistant reply to the local messages so the
                # LLM context stays coherent when queued messages are
                # injected after this round (mirrors Case 2's assistant
                # tool_calls message).
                messages.append({"role": "assistant", "content": full_content})

                # P1 (rant 21:55:37): messages queued mid-round (after the
                # round-top drain) must not end the turn — inject and continue.
                # Injection round does not consume the round budget.
                _n, _ask = await self._inject_pending_messages(session, messages)
                if _n:
                    force_ask = _ask
                    continue

                await self._broadcast(session.session_id, {
                    "request_id": req.id,
                    "content": full_content or "",
                    "done": True,
                    "delta": False,
                    "session_id": session.session_id,
                    # rant 21:52:18: authoritative current-context message count.
                    "context_messages": len(messages),
                })

                # Fire-and-forget: reflect on whether to save memories
                self._maybe_reflect_memory(session, req.prompt, full_content)
                return

            # Case 2: LLM wants to call tools
            if tc_by_index or final_finish == "tool_calls":
                tool_calls = [tc_by_index[i] for i in sorted(tc_by_index.keys())]

                # Log LLM request/response for this tool-call round
                self._log_llm_exchange(
                    session, [dict(m) for m in messages], tools_openai,
                    full_content, final_finish, final_usage,
                    tool_calls=[
                        {"id": tc.get("id", ""), "type": "function",
                         "function": {"name": tc.get("function", {}).get("name", ""),
                                      "arguments": tc.get("function", {}).get("arguments", "")}}
                        for tc in tool_calls
                    ],
                    reasoning=full_reasoning,
                )

                # Build the assistant message with tool_calls
                assistant_msg: dict = {"role": "assistant", "content": full_content or None}
                openai_tool_calls: list[dict] = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    openai_tool_calls.append({
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", ""),
                        },
                    })
                assistant_msg["tool_calls"] = openai_tool_calls
                messages.append(assistant_msg)

                # Persist assistant message WITH embedded tool_calls
                session.append_message({
                    "type": "message",
                    "role": "assistant",
                    "content": full_content,
                    "tool_calls": [
                        {
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tc.get("function", {}).get("name", ""),
                                "arguments": tc.get("function", {}).get("arguments", ""),
                            },
                        }
                        for tc in tool_calls
                    ],
                })

                # Execute each tool.  tool_calls are already embedded in the
                # assistant message above, so we only persist tool_results here.
                for tc in openai_tool_calls:
                    tc_id = tc["id"]
                    tc_name = tc["function"]["name"]
                    tc_args_str = tc["function"]["arguments"]

                    try:
                        args = json.loads(tc_args_str) if tc_args_str else {}
                    except json.JSONDecodeError:
                        args = {}

                    # Rant 2026-08-17T12:03:13: log the human-readable purpose
                    # alongside the tool name so background/reflection calls
                    # (memory reflection / consolidation) are understandable
                    # without context.
                    tool_obj = self.tools.get(tc_name)
                    purpose = tool_obj.definition().purpose if tool_obj else "unknown tool"
                    logger.info("tool call: %s — %s (%s)", tc_name, purpose,
                                json.dumps(_redact(args), ensure_ascii=False)[:200])

                    # Notify client (broadcast to all session subscribers)
                    await self._broadcast(session.session_id, {
                        "type": "tool_start",
                        "request_id": req.id,
                        "tool_name": tc_name,
                        "tool_call_id": tc_id,
                        "arguments": args,
                    })

                    # Inject session cwd as default for filesystem tools
                    if tc_name in ("bash", "glob") and "workdir" not in args:
                        args["workdir"] = str(session.cwd)
                    elif tc_name == "grep" and "path" not in args:
                        args["path"] = str(session.cwd)

                    # Execute
                    tool = self.tools.get(tc_name)
                    if tool:
                        try:
                            result = await tool.execute(args)
                            result.tool_call_id = tc_id
                            result.name = tc_name
                        except Exception as e:
                            logger.warning("tool %s failed: %s", tc_name, e)
                            result = ToolResult(
                                tool_call_id=tc_id,
                                name=tc_name,
                                content=f"Tool execution error: {e}",
                                error=True,
                            )
                    else:
                        result = ToolResult(
                            tool_call_id=tc_id,
                            name=tc_name,
                            content=f"Unknown tool: {tc_name}. Available: {self.tools.names}",
                            error=True,
                        )

                    # Persist tool_result (tool_calls are already in the
                    # assistant message, no separate tool_call record needed).
                    session.append_message({
                        "type": "tool_result",
                        "tool_name": tc_name,
                        "tool_call_id": tc_id,
                        "content": result.content,
                        "error": result.error,
                    })

                    # Add tool result to conversation
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": result.content,
                    })

                    # Notify client of result (broadcast to all session subscribers)
                    await self._broadcast(session.session_id, {
                        "type": "tool_end",
                        "request_id": req.id,
                        "tool_name": tc_name,
                        "tool_call_id": tc_id,
                        "content": result.content,
                        "error": result.error,
                    })

                # Log LLM request for this round (before continuing).
                # Tool rounds consume the round budget; injection rounds do
                # not (they `continue` from the stop / Case 3 branches
                # without incrementing).
                round_num += 1
                continue

            # Case 3: Max tokens or other stop — done
            self._log_llm_exchange(
                session, [dict(m) for m in messages], tools_openai,
                full_content, final_finish, final_usage,
                reasoning=full_reasoning,
            )

            session.append_message({
                "type": "message",
                "role": "assistant",
                "content": full_content,
            })

            # Append the assistant reply to the local messages so the LLM
            # context stays coherent when queued messages are injected
            # after this round.
            messages.append({"role": "assistant", "content": full_content})

            # P1 (rant 21:55:37): messages queued mid-round must not end the
            # turn — inject and continue (injection round does not consume
            # the round budget).
            _n, _ask = await self._inject_pending_messages(session, messages)
            if _n:
                force_ask = _ask
                continue

            await self._broadcast(session.session_id, {
                "request_id": req.id,
                "content": full_content or "",
                "done": True,
                "delta": False,
                "session_id": session.session_id,
                # rant 21:52:18: current LLM context size (system + history +
                # user + all tool results + assistant replies) — authoritative
                # for the TUI status bar message count.
                "context_messages": len(messages),
            })

            # Fire-and-forget: reflect on whether to save memories
            self._maybe_reflect_memory(session, req.prompt, full_content)
            return

    def _log_llm_exchange(
        self, session: Session, messages, tools,
        content: str, finish_reason: str = "stop",
        usage=None, tool_calls=None,
        reasoning: str | None = None,
    ) -> None:
        """Log a complete LLM request/response exchange to the session.

        Centralizes the identical append_llm patterns from _run_tool_loop,
        ensuring consistent logging format.

        ``reasoning`` (rant 2026-08-18T09:43:23) is written ONLY into the
        response record (when the model produced a think block); the request
        record stays untouched so llm.jsonl history/context is not bloated.
        """
        session.append_llm({
            "type": "request",
            "model": self.llm.config.model,
            "messages": messages,
            "tools": tools,
            "payload": self.llm.last_payload,
        })
        response: dict = {
            "type": "response",
            "content": content,
            "finish_reason": finish_reason,
            "http_status": self.llm.last_response_status,
            "response_headers": self.llm.last_response_headers,
        }
        if usage is not None:
            response["usage"] = usage
        if tool_calls is not None:
            response["tool_calls"] = tool_calls
        if reasoning is not None:
            response["reasoning"] = reasoning
        session.append_llm(response)

    # ── Token estimation helpers ──────────────────────────────

    @staticmethod
    def _count_chars_for_tokens(text: str) -> int:
        """Estimate token count for a string, accounting for CJK vs ASCII.

        CJK characters consume ~1.5-2 chars/token (DeepSeek/OpenAI tokenizers).
        ASCII/English/code consumes ~4 chars/token.
        Using a flat 'len // 3' underestimates Chinese by ~2x, which can cause
        auto-compact to miss its trigger window.

        Returns estimated token count.
        """
        cjk = 0
        ascii_chars = 0
        for ch in text:
            cp = ord(ch)
            # CJK Unified Ideographs + Extensions + Compat, CJK Symbols, Kana, Hangul
            if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                0x20000 <= cp <= 0x2A6DF or 0xF900 <= cp <= 0xFAFF or
                0x2F800 <= cp <= 0x2FA1F or 0x3000 <= cp <= 0x303F or
                0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF or
                0xAC00 <= cp <= 0xD7AF or 0xFF00 <= cp <= 0xFFEF):
                cjk += 1
            else:
                ascii_chars += 1
        return (cjk // 2) + (ascii_chars // 4)

    def _estimate_tokens(self, messages: list[dict]) -> int:
        """Rough token estimation from OpenAI-format messages.

        Character-aware: CJK ≈ 2 chars/token, ASCII ≈ 4 chars/token.
        Adds +3 tokens per message for role/content metadata overhead.
        """
        total = 0
        for m in messages:
            total += 3  # role/name overhead
            content = m.get("content") or ""
            if isinstance(content, str):
                total += self._count_chars_for_tokens(content)
            for tc in (m.get("tool_calls") or []):
                tc_str = json.dumps(tc, ensure_ascii=False)
                total += self._count_chars_for_tokens(tc_str)
        return total

    @staticmethod
    def _estimate_text(text: str) -> int:
        """Rough token count for a plain text string."""
        return EmrgServer._count_chars_for_tokens(text)

    @staticmethod
    def _estimate_single(record: dict) -> int:
        """Rough token count for a single history record."""
        content = record.get("content", "")
        if isinstance(content, str):
            return EmrgServer._count_chars_for_tokens(content) + 3
        return 3

    @staticmethod
    def _records_to_text(records: list[dict]) -> str:
        """Convert history records to compact text for summarization."""
        parts: list[str] = []
        for r in records:
            ts = r.get("timestamp", "")[:19]
            rtype = r.get("type", "")
            if rtype == "message":
                content = r.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                parts.append(f"[{ts}] {r['role']}: {content}")
            elif rtype == "tool_call":
                parts.append(
                    f"[{ts}] tool_call: {r.get('tool_name', '')}"
                    f"({json.dumps(r.get('arguments', {}), ensure_ascii=False)})"
                )
            elif rtype == "tool_result":
                c = r.get("content", "")
                parts.append(f"[{ts}] tool_result: {c[:500]}")
            elif rtype == "summary":
                parts.append(f"[{ts}] [PREVIOUS SUMMARY]: {r.get('content', '')}")
        return "\n".join(parts)

    @staticmethod
    def _truncate_record(record: dict, max_tokens: int) -> dict:
        """Truncate an oversized record's content to fit max_tokens.

        Uses conservative char estimate: max_tokens * 2 (CJK worst case ~2 chars/token).
        After truncation, the record is re-estimated by _estimate_single to verify.
        """
        record = dict(record)
        content = record.get("content", "")
        max_chars = max_tokens * 2  # conservative: CJK ~2 chars/token
        if len(content) > max_chars:
            record["content"] = content[:max_chars] + "\n...[truncated for compact]"
        return record

    # ── Compact ──────────────────────────────────────────────

    async def _handle_compact(
        self, session: Session, ws
    ) -> None:
        """Handle a compact request: summarize history via LLM and replace old messages."""
        records = session._read_history()
        if len(records) <= 5:
            await self._broadcast(session.session_id, {
                "type": "compact_result",
                "session_id": session.session_id,
                "messages_compacted": 0,
                "summary": "Not enough messages to compact.",
            })
            return

        # Try normal compact first; fall back to chunked on context error
        try:
            summary = await self._do_compact(session, records)
        except RuntimeError as e:
            err_msg = str(e).lower()
            if "context" in err_msg or "too long" in err_msg or "400" in str(e):
                logger.warning("normal compact failed, trying chunked: %s", e)
                try:
                    summary = await self._chunked_compact(records)
                except Exception as e2:
                    logger.exception("chunked compact also failed")
                    await self._broadcast(session.session_id, {
                        "type": "compact_result",
                        "session_id": session.session_id,
                        "messages_compacted": 0,
                        "error": f"Compact failed (both normal and chunked): {e2}",
                    })
                    return
            else:
                raise

        # Apply compact
        count = session.compact(summary, keep_recent=5)

        await self._broadcast(session.session_id, {
            "type": "compact_result",
            "session_id": session.session_id,
            "messages_compacted": count,
            "summary": summary,
        })
        logger.info("compact complete: %d messages compacted", count)

    async def _do_compact(self, session: Session, records: list[dict]) -> str:
        """Normal compact: summarize all records in one LLM call."""
        compact_prompt = (
            "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary "
            "for another LLM that will continue this conversation.\n\n"
            "Include in your summary:\n"
            "1. Current progress and key decisions made\n"
            "2. Important context, constraints, and user preferences\n"
            "3. What remains to be done\n"
            "4. Any critical data, examples, or references needed to continue\n\n"
            "Be concise but comprehensive. The next LLM must be able to pick up exactly "
            "where you left off without losing any critical context.\n\n"
            "Here is the conversation to summarize:"
        )
        history_text = self._records_to_text(records)
        compact_messages: list[dict] = [
            {"role": "user", "content": compact_prompt + "\n\n" + history_text},
        ]
        logger.info("compact: summarizing %d records (%d chars)", len(records), len(history_text))

        msg = await self.llm.chat(compact_messages, tools=None)
        summary = msg.get("content", "Summary unavailable.")

        # Log compact LLM call
        session.append_llm({
            "type": "request",
            "model": self.llm.config.model,
            "messages": compact_messages,
            "tools": None,
            "compact": True,
            "payload": self.llm.last_payload,
        })
        session.append_llm({
            "type": "response",
            "content": summary,
            "compact": True,
            "http_status": self.llm.last_response_status,
            "response_headers": self.llm.last_response_headers,
        })
        return summary

    async def _chunked_compact(
        self, records: list[dict], keep_recent: int = 5
    ) -> str:
        """Adaptive chunked compact: split on LLM feedback, not estimates.

        Instead of pre-estimating token counts, tries to summarize chunks
        and binary-splits on "maximum context length" errors.  This is
        inherently accurate — if a chunk fits, it fits; if it doesn't,
        we split and retry each half independently.
        """
        to_compact = records[:-keep_recent]
        logger.info(
            "chunked compact: %d records to summarize via adaptive splitting",
            len(to_compact),
        )
        return await self._adaptive_chunk_summarize(to_compact, 0)

    async def _adaptive_chunk_summarize(
        self, records: list[dict], depth: int, max_depth: int = 8
    ) -> str:
        """Summarize records, binary-splitting on context-limit errors."""
        if not records:
            return ""

        # Single record that's too large → truncate and recurse
        if len(records) == 1:
            try:
                text = self._records_to_text(records)
                msg = await self.llm.chat([{
                    "role": "user",
                    "content": f"Summarize this conversation segment:\n\n{text}",
                }], tools=None)
                return msg.get("content", "")
            except RuntimeError as e:
                err = str(e)
                if ("context length" in err or "length limit" in err) and depth < max_depth:
                    record = self._truncate_record(
                        records[0],
                        self.llm.config.context_window // 2,
                    )
                    return await self._adaptive_chunk_summarize([record], depth + 1, max_depth)
                raise

        chunk_text = self._records_to_text(records)
        try:
            msg = await self.llm.chat([{
                "role": "user",
                "content": f"Summarize this conversation segment:\n\n{chunk_text}",
            }], tools=None)
            return msg.get("content", "")
        except RuntimeError as e:
            err = str(e)
            if ("context length" in err or "length limit" in err) and depth < max_depth:
                mid = len(records) // 2
                if mid == 0:
                    mid = 1
                summary_a = await self._adaptive_chunk_summarize(
                    records[:mid], depth + 1, max_depth,
                )
                summary_b = await self._adaptive_chunk_summarize(
                    records[mid:], depth + 1, max_depth,
                )
                combined = f"{summary_a}\n---\n{summary_b}"
                # Merge sub-summaries
                try:
                    msg = await self.llm.chat([{
                        "role": "user",
                        "content": (
                            "Merge these two conversation summaries into one "
                            f"coherent summary:\n\n{combined}"
                        ),
                    }], tools=None)
                    return msg.get("content", "")
                except RuntimeError:
                    return combined  # can't merge — return raw combined
            raise

    async def _merge_summaries(
        self, summaries: list[str], max_per_chunk: int, merge_batch: int
    ) -> str:
        """Recursively merge summaries until they fit in one LLM call."""
        combined = "\n---\n".join(summaries)
        if self._estimate_text(combined) <= merge_batch:
            msg = await self.llm.chat([{
                "role": "user",
                "content": (
                    "Merge these conversation segment summaries into one "
                    "coherent summary:\n\n" + combined
                ),
            }], tools=None)
            return msg.get("content", "")

        # Group summaries into batches
        batches: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        for s in summaries:
            st = self._estimate_text(s)
            if current_tokens + st > merge_batch and current:
                batches.append(current)
                current = []
                current_tokens = 0
            current.append(s)
            current_tokens += st
        if current:
            batches.append(current)

        logger.info(
            "merge_summaries: %d summaries → %d batches",
            len(summaries), len(batches),
        )

        merged: list[str] = []
        for batch in batches:
            m = await self._merge_summaries(batch, max_per_chunk, merge_batch)
            merged.append(m)

        if len(merged) == 1:
            return merged[0]
        return await self._merge_summaries(merged, max_per_chunk, merge_batch)

    async def _handle_list_sessions(
        self, cwd: Path, ws
    ) -> None:
        """List all sessions for the given cwd."""
        sessions = Session.list_sessions(cwd)
        await self._send(ws, {
            "type": "sessions_list",
            "sessions": sessions,
        })

    async def _handle_list_projects(
        self, ws
    ) -> None:
        """Read projects.yml and return all project entries.

        Ordered by each project's latest session activity (created_at desc,
        parallel scan) — P6 of the GUI multi-session rant (2026-08-10T15:07:19)
        so the open/new-session dialogs show the most recently active projects
        first.

        No evolution-workspace filter (rant 2026-08-07T10:48:00): projects.yml
        only contains explicitly registered entries, and on packaged installs
        the emrg project's only path IS ~/.emrg/evolution/emrg — filtering it
        hid emrg from /rant entirely. _touch_project still skips evolution
        subdirs so evolution cycles' cwd is never auto-tracked as a user
        project.
        """
        projects: list[dict] = []
        try:
            if self._projects_log.exists():
                data = yaml.safe_load(self._projects_log.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    entries = [p for p in data if isinstance(p, dict)]
                    # P6 验收（rant 2026-08-10T15:07:19）：项目按"该项目最新会话活跃"倒序
                    # ——并行扫描各项目 sessions 目录取最大 created_at（GUI 单连接无法并发
                    # list_sessions：_pending 按 respType 键控会互相覆盖，故 daemon 侧聚合）。
                    async def _latest_session_at(entry: dict) -> str:
                        p = entry.get("path", "")
                        if not p:
                            return ""
                        try:
                            sessions = Session.list_sessions(Path(p))
                            return sessions[0].get("created_at", "") if sessions else ""
                        except (OSError, ValueError, json.JSONDecodeError):
                            return ""

                    ats = await asyncio.gather(*(_latest_session_at(e) for e in entries))
                    ordered = sorted(zip(entries, ats), key=lambda t: t[1], reverse=True)
                    # rant 2026-08-19T01:05:47 — _detect_git_remote runs a sync
                    # git subprocess per project; offload to worker threads so a
                    # slow git probe never freezes the event loop (websocket
                    # clients would otherwise time out during list_projects).
                    repos = await asyncio.gather(*(
                        asyncio.to_thread(_detect_git_remote, p.get("path", ""))
                        for p, _ in ordered
                    ))
                    projects = [
                        {"name": p.get("name", ""),
                         "repo": repo,
                         "path": p.get("path", ""),
                         "latest_session_at": at}
                        for (p, at), repo in zip(ordered, repos)
                    ]
        except (yaml.YAMLError, OSError):
            logger.exception("Failed to read projects.yml")
        await self._send(ws, {
            "type": "projects_list",
            "projects": projects,
        })

    async def _handle_remove_project(self, name: str, ws) -> None:
        """Remove a project entry from projects.yml by name.

        P1 of the GUI multi-session feature (rant 2026-08-10T15:07:19):
        deletes only the projects.yml entry — on-disk session data under
        <path>/.emrg/sessions/ is preserved (a later _touch_project
        re-registers the project). Mirrors _touch_project's read path and
        atomic_write_yaml. Responses:
          {"type": "project_removed", "removed": true,  "name": name}
          {"type": "project_removed", "removed": false, "name": name, ...}
        """
        if not self._projects_log.exists():
            await self._send(ws, {
                "type": "project_removed",
                "removed": False,
                "name": name,
            })
            return
        try:
            data = yaml.safe_load(self._projects_log.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            logger.exception("remove_project: failed to read %s", self._projects_log)
            await self._send(ws, {
                "type": "project_removed",
                "removed": False,
                "name": name,
                "error": "failed to read projects.yml",
            })
            return
        if not isinstance(data, list):
            await self._send(ws, {
                "type": "project_removed",
                "removed": False,
                "name": name,
            })
            return
        remaining = [
            e for e in data
            if not (isinstance(e, dict) and e.get("name") == name)
        ]
        if len(remaining) == len(data):
            await self._send(ws, {
                "type": "project_removed",
                "removed": False,
                "name": name,
            })
            return
        try:
            atomic_write_yaml(remaining, self._projects_log, prefix=".projects_")
        except OSError:
            logger.exception("remove_project: failed to write %s", self._projects_log)
            await self._send(ws, {
                "type": "project_removed",
                "removed": False,
                "name": name,
                "error": "failed to write projects.yml",
            })
            return
        logger.info("remove_project: removed %s from projects.yml", name)
        await self._send(ws, {
            "type": "project_removed",
            "removed": True,
            "name": name,
        })

    async def _handle_list_models(
        self, ws
    ) -> None:
        """Return available models for /model switching.

        Builds a merged list: the current default model from [llm] config,
        plus any additional models from [[llm.models]]. The default model
        always appears first.
        """
        default_name = self.llm.config.model
        default_ctx = self.llm.config.context_window
        models_config = self.llm.config.models or []

        seen: set[str] = set()
        merged: list[dict] = []

        # Default model always first
        merged.append({"name": default_name, "context_window": default_ctx})
        seen.add(default_name)

        for m in models_config:
            if m.get("name") and m["name"] not in seen:
                merged.append(dict(m))
                seen.add(m["name"])

        await self._send(ws, {
            "type": "models_list",
            "models": merged,
            "current": default_name,
        })

    async def _handle_set_model(
        self, model_name: str, ws
    ) -> None:
        """Switch the runtime LLM model and context_window.

        Not persisted — on restart, reverts to config.toml defaults.
        The model_name must be either the default model or in [[llm.models]].
        If not found in [[llm.models]], the context_window is kept as-is.
        """
        old_model = self.llm.config.model
        old_ctx = self.llm.config.context_window

        # Find the matching [[llm.models]] entry (if any) to resolve
        # context_window and optional model name override.
        new_ctx: int | None = None
        new_vision: bool | None = None
        api_model: str = model_name  # default: use display name as API model
        for m in (self.llm.config.models or []):
            if m.get("name") == model_name:
                new_ctx = m.get("context_window")
                api_model = m.get("model", model_name)
                if "vision" in m:
                    new_vision = m["vision"]
                break

        self.llm.config.model = api_model
        if new_ctx is not None:
            self.llm.config.context_window = new_ctx
        if new_vision is not None:
            self.llm.config.vision = new_vision

        logger.info(
            "model switched: %s → %s (api=%s, context_window: %d → %d)",
            old_model, model_name, api_model, old_ctx, self.llm.config.context_window,
        )

        await self._send(ws, {
            "type": "model_set",
            "model": model_name,
            "context_window": self.llm.config.context_window,
            "previous": old_model,
        })
        # Phase 2 broadcast: model is global daemon state — all connected
        # clients must see the same model (protocol-contract §2.6.3).
        # The requester already got model_set above; exclude it from _broadcast_all.
        await self._broadcast_all({
            "type": "model_set",
            "model": model_name,
            "context_window": self.llm.config.context_window,
            "previous": old_model,
        }, exclude=ws)

    async def _handle_resume_session(
        self, session_id: str, cwd: Path, ws
    ) -> None:
        """Validate a session exists and return metadata.

        The client reads history.jsonl directly from disk for display.
        We only confirm the session exists — no records over the wire.
        """
        session_dir = cwd / ".emrg" / "sessions" / session_id
        if not session_dir.exists():
            await self._send(ws, {
                "type": "resume_result",
                "session_id": session_id,
                "error": f"Session {session_id} not found",
            })
            return

        session = Session.load(session_id, cwd)

        await self._send(ws, {
            "type": "resume_result",
            "session_id": session_id,
            "meta": {
                "message_count": session.message_count,
                "compact_count": session.compact_count,
                "created_at": session._created_at,
                "updated_at": session._updated_at,
                "title": session.title,
            },
        })

    # ── GUI workspace panel: list_files / read_file (rant 2026-08-11T12:20:35 P1.1) ──
    # 单目录条目上限：超出截断 + truncated 提示（与 ReadTool 的防爆理念一致）
    _MAX_LIST_ENTRIES = 5000
    # read_file 1MB 上限：更大文件引导用系统工具打开（避免 WebSocket 帧过大）
    _MAX_READ_FILE_SIZE = 1 * 1024 * 1024
    # 显式 line_limit 的上限（对齐 ReadTool.MAX_LINES）
    _MAX_READ_LINES = 2000

    async def _handle_list_files(self, path_str: str, ws) -> None:
        """List one directory level (workspace file browser data source).

        Returns {"type": "files_list", path, entries: [{name, path, type}],
        truncated?} — fields deliberately limited to name/type/path (no
        size/mtime: per-entry stat is expensive on 5000-entry directories).
        """
        raw = Path(path_str).expanduser()
        if not raw.is_absolute():
            await self._send(ws, {
                "type": "files_list",
                "error": "path must be absolute",
            })
            return
        path = raw.resolve()
        try:
            if not path.exists():
                await self._send(ws, {
                    "type": "files_list",
                    "error": f"path not found: {path}",
                })
                return
            if not path.is_dir():
                await self._send(ws, {
                    "type": "files_list",
                    "error": f"not a directory: {path}",
                })
                return
            entries = sorted(
                path.iterdir(),
                # 目录在前、按名排序（对齐 ReadTool）；符号链接不跟随 →
                # is_dir(follow_symlinks=False) 为 False → 归入 file 类不可展开
                key=lambda p: (not p.is_dir(follow_symlinks=False), p.name),
            )
            result = []
            truncated = False
            for e in entries[: self._MAX_LIST_ENTRIES]:
                is_dir = e.is_dir(follow_symlinks=False)
                result.append({
                    "name": e.name,
                    "path": str(e),
                    "type": "dir" if is_dir else "file",
                })
            if len(entries) > self._MAX_LIST_ENTRIES:
                truncated = True
            await self._send(ws, {
                "type": "files_list",
                "path": str(path),
                "entries": result,
                "truncated": truncated,
            })
        except OSError as exc:
            await self._send(ws, {
                "type": "files_list",
                "error": f"cannot list directory: {exc}",
            })

    async def _handle_read_file(
        self, path_str: str, start_line, line_limit, ws
    ) -> None:
        """Read a text file (workspace panel viewer data source).

        Returns {"type": "file_content", path, content, truncated?, binary?,
        error?}. Binary files report binary=True with empty content (image
        preview is rendered via file:// URL in the renderer, no base64).
        """
        raw = Path(path_str).expanduser()
        if not raw.is_absolute():
            await self._send(ws, {
                "type": "file_content",
                "error": "path must be absolute",
            })
            return
        path = raw.resolve()
        try:
            if not path.exists():
                await self._send(ws, {
                    "type": "file_content",
                    "error": f"file not found: {path}",
                })
                return
            if path.is_dir():
                await self._send(ws, {
                    "type": "file_content",
                    "error": f"is a directory: {path}",
                })
                return
            file_size = path.stat().st_size
            if file_size > self._MAX_READ_FILE_SIZE:
                await self._send(ws, {
                    "type": "file_content",
                    "path": str(path),
                    "error": "文件过大，用系统工具打开",
                })
                return
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                await self._send(ws, {
                    "type": "file_content",
                    "path": str(path),
                    "binary": True,
                })
                return
        except OSError as exc:
            await self._send(ws, {
                "type": "file_content",
                "error": f"cannot read file: {exc}",
            })
            return

        all_lines = text.split("\n")
        total = len(all_lines)
        try:
            start = max(1, int(start_line or 1))
        except (TypeError, ValueError):
            start = 1
        try:
            limit = int(line_limit) if line_limit is not None else None
        except (TypeError, ValueError):
            limit = None
        if limit is not None:
            limit = min(limit, self._MAX_READ_LINES)
            selected = all_lines[start - 1 : start - 1 + limit]
            truncated = start - 1 + limit < total
        else:
            selected = all_lines[start - 1 :]
            truncated = False
        await self._send(ws, {
            "type": "file_content",
            "path": str(path),
            "content": "\n".join(selected),
            "truncated": truncated,
            "total_lines": total,
        })

    async def _handle_list_memories(
        self, scope: str, session_id: str, cwd: str, ws
    ) -> None:
        """List memories: return the MEMORY.md index and memory directory info."""
        if scope == "project":
            cwd_path = Path(cwd) if cwd else Path.cwd()
            store = ProjectMemoryStore(cwd_path)
            index_path = store.index_path
        else:
            session_dir = Path(cwd) / ".emrg" / "sessions" / session_id
            if not session_dir.exists():
                await self._send(ws, {
                    "type": "memories_list",
                    "error": f"Session {session_id} not found",
                })
                return
            store = SessionMemoryStore(session_dir)
            index_path = store.index_path

        memories = store.list()
        index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""

        await self._send(ws, {
            "type": "memories_list",
            "scope": scope,
            "directory": str(store.directory),
            "index_path": str(index_path),
            "index": index_text,
            "memories": [
                {
                    "id": m.id,
                    "file": m.filename,
                    "title": m.display_title,
                    "type": m.type,
                    "status": m.status,
                    "event_at": m.event_at,
                    "created_at": m.created_at,
                    "updated_at": m.updated_at,
                }
                for m in memories
            ],
        })

    async def _handle_read_memory(
        self, scope: str, memory_id: str, session_id: str, cwd: str,
        ws,
    ) -> None:
        """Read a specific memory file by id and return its full content."""
        if scope == "project":
            cwd_path = Path(cwd) if cwd else Path.cwd()
            store = ProjectMemoryStore(cwd_path)
        else:
            session_dir = Path(cwd) / ".emrg" / "sessions" / session_id
            if not session_dir.exists():
                await self._send(ws, {
                    "type": "memory_content",
                    "error": f"Session {session_id} not found",
                })
                return
            store = SessionMemoryStore(session_dir)

        mem = store.get(memory_id)
        if mem is None:
            await self._send(ws, {
                "type": "memory_content",
                "error": f"Memory not found: {memory_id}",
            })
            return

        await self._send(ws, {
            "type": "memory_content",
            "scope": scope,
            "memory_id": memory_id,
            "file": mem.filename,
            "path": str(store.directory / mem.filename),
            "content": mem.to_markdown(),
            "frontmatter": {
                "id": mem.id,
                "event_at": mem.event_at,
                "created_at": mem.created_at,
                "updated_at": mem.updated_at,
                "source_session": mem.source_session,
                "type": mem.type,
                "scope": mem.scope,
                "status": mem.status,
                "title": mem.title,
            },
            "body": mem.body,
        })

    async def _generate_session_title(self, session: Session) -> str:
        """Use LLM to generate a short title from session history."""
        records = session._read_history()
        if not records:
            return session.session_id

        # Collect user messages to build context
        user_texts: list[str] = []
        for r in records:
            if r.get("type") == "message" and r.get("role") == "user":
                user_texts.append(r.get("content", ""))

        context = "\n".join(user_texts[-5:])  # last 5 user messages
        if not context:
            return session.session_id

        title_prompt = (
            "Generate a short, descriptive kebab-case title (2-4 words) for "
            "this conversation based on the user's requests below. Reply with "
            "ONLY the title, nothing else.\n\n"
            f"{context[:2000]}"
        )

        try:
            msg = await self.llm.chat(
                [{"role": "user", "content": title_prompt}],
                tools=None,
            )
            title = msg.get("content", "").strip().strip("\"'")
            # Cap length and clean
            title = title[:80].replace("\n", " ").replace("  ", " ")
            if not title:
                title = session.session_id
            logger.info("auto-generated title for %s: %s", session.session_id, title)
            return title
        except Exception:
            logger.exception("title generation failed")
            return session.session_id

    # ── Memory reflection & consolidation ─────────────────────

    def _maybe_reflect_memory(
        self, session: Session, user_prompt: str, assistant_content: str
    ) -> None:
        """Fire-and-forget: ask LLM to reflect on the exchange and save memories.

        This runs as a background task — it does not block the user response.
        Only triggers if the exchange looks substantive (has meaningful content).
        """
        # Skip trivial exchanges
        if not assistant_content or len(assistant_content.strip()) < 20:
            return

        async def _reflect():
            try:
                store = session.memory_store
                existing = store.list()
                existing_summary = "\n".join(
                    f"- [{m.type}] {m.title} (id={m.id})"
                    for m in existing
                ) if existing else "(none yet)"

                prompt = (
                    "You are the memory reflection module of EMRG. "
                    "Review the following exchange and decide if anything "
                    "should be remembered. If so, use the write tool to create "
                    "or update memory files.\n\n"
                    "## Current memories\n"
                    f"{existing_summary}\n\n"
                    "## Memory directories\n"
                    f"Session: `{session.memory_dir}/` (session-scope, this session only)\n"
                    f"Project: `{session.cwd}/.emrg/memory/` (project-scope, cross-session)\n\n"
                    "## Exchange to reflect on\n"
                    f"User: {user_prompt[:500]}\n"
                    f"Assistant: {assistant_content[:1000]}\n\n"
                    "**Instructions**:\n"
                    "- If the user shared a preference or gave feedback → create a `user` or `feedback` memory\n"
                    "- If a technical decision was made → create a `decision` memory\n"
                    "- If something was learned about the project → create a `project` memory\n"
                    "- If a task was started/in progress/done → create a `task` memory\n"
                    "- Update existing memories if this exchange supersedes or refines them\n"
                    "- If nothing worth remembering happened, just reply 'no new memories' briefly\n"
                    "- Prefer session-scope for tentative/evolving knowledge; "
                    "project-scope for stable, cross-session facts\n"
                    "\n"
                    "Memory format (YAML frontmatter + Markdown):\n"
                    "```\n"
                    "---\n"
                    'id: "<8-char hex>"\n'
                    'event_at: "<ISO 8601>"\n'
                    'created_at: "<ISO 8601>"\n'
                    'updated_at: "<ISO 8601>"\n'
                    'type: "<user|feedback|project|reference|decision|task>"\n'
                    'scope: "<session|project>"\n'
                    'status: "active"\n'
                    "---\n\n"
                    "# Title\n\nBody content.\n"
                    "```\n"
                    "- After creating/updating .md files, update the MEMORY.md index in that "
                    "directory to keep the index in sync."
                )

                # Run a mini tool loop: LLM can create/update memory files via tools
                tools = self.tools.to_openai_tools()
                messages: list[dict] = [{"role": "user", "content": prompt}]
                max_rounds = 4

                for _round in range(max_rounds):
                    msg = await self.llm.chat(messages, tools=tools)
                    content = msg.get("content", "")
                    tool_calls = msg.get("tool_calls")

                    if not tool_calls:
                        logger.debug(
                            "memory reflection: id=%s round=%d result=%s",
                            session.session_id, _round + 1, content[:150],
                        )
                        break

                    # Execute tools
                    logger.info(
                        "memory reflection: id=%s round=%d tool_calls=%d",
                        session.session_id, _round + 1, len(tool_calls),
                    )
                    assistant_msg: dict = {"role": "assistant", "content": content or None}
                    openai_tool_calls = []

                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        tc_id = tc.get("id", "")
                        tc_name = fn.get("name", "")
                        tc_args_str = fn.get("arguments", "")
                        try:
                            args = json.loads(tc_args_str) if tc_args_str else {}
                        except json.JSONDecodeError:
                            args = {}

                        openai_tool_calls.append({
                            "id": tc_id, "type": "function",
                            "function": {"name": tc_name, "arguments": tc_args_str},
                        })

                    # IMPORTANT: assistant message with tool_calls must come BEFORE
                    # tool result messages (OpenAI/DeepSeek API requirement).
                    assistant_msg["tool_calls"] = openai_tool_calls
                    messages.append(assistant_msg)

                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        tc_id = tc.get("id", "")
                        tc_name = fn.get("name", "")
                        tc_args_str = fn.get("arguments", "")
                        try:
                            args = json.loads(tc_args_str) if tc_args_str else {}
                        except json.JSONDecodeError:
                            args = {}

                        tool = self.tools.get(tc_name)
                        if tool:
                            try:
                                result = await tool.execute(args)
                                result_text = result.content
                            except Exception as e:
                                result_text = f"Error: {e}"
                        else:
                            result_text = f"Unknown tool: {tc_name}"

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": result_text,
                        })
                        # Rant 2026-08-17T12:03:13: include the human-readable purpose
                        purpose = tool.definition().purpose if tool else "unknown tool"
                        logger.debug(
                            "memory reflection: id=%s round=%d tool %s — %s → %s%s",
                            session.session_id, _round + 1, tc_name, purpose,
                            _redact_string(result_text[:100]),
                            "…" if len(result_text) > 100 else "",
                        )

            except Exception:
                logger.debug("memory reflection failed", exc_info=True)

        asyncio.create_task(_reflect())

    async def _consolidate_session_memories(
        self, session_id: str, cwd: Path
    ) -> None:
        """On client disconnect: consolidate session-level memories.

        If the session has ≥3 memories, ask LLM to:
        - Merge overlapping/duplicate memories
        - Identify memories worth promoting to project scope
        - Mark done tasks as superseded
        """
        session_dir = cwd / ".emrg" / "sessions" / session_id
        if not session_dir.exists():
            return

        store = SessionMemoryStore(session_dir)

        if store.count < 3:
            return

        memories = store.list()
        mem_list = "\n".join(
            f"- [{m.type}] {m.title} (file: {m.filename}, status: {m.status})"
            for m in memories
        )

        logger.info(
            "consolidating %d session memories for %s", store.count, session_id,
        )

        try:
            prompt = (
                "You are the memory consolidation module of EMRG. "
                "A session is ending. Review its memories and consolidate.\n\n"
                "## Session memories\n"
                f"{mem_list}\n\n"
                "**Instructions**:\n"
                "1. **Merge**: if 2+ memories cover the same topic, merge them into one "
                "(edit the file, mark old ones `status: merged`)\n"
                "2. **Promote**: if a memory has lasting value beyond this session, "
                "move it from the session memory dir to the project memory dir "
                f"(`{cwd}/.emrg/memory/`), update `scope` to `project`, and update "
                "both MEMORY.md indexes\n"
                "3. **Clean**: mark completed tasks as `status: superseded`\n"
                "4. **Skip**: if everything looks fine, just reply 'no consolidation needed'\n"
                "\n"
                f"Use the read/edit/write tools to make these changes. "
                f"Session memory dir: `{store.directory}/` "
                f"Project memory dir: `{cwd}/.emrg/memory/`"
            )

            # Tool loop: LLM may call read/edit/write tools for consolidation.
            # Feed tool results back so the LLM can act on them.
            tools_openai = self.tools.to_openai_tools()
            messages: list[dict] = [{"role": "user", "content": prompt}]
            max_rounds = 4
            for _round in range(max_rounds):
                msg = await self.llm.chat(messages, tools=tools_openai)
                tool_calls = msg.get("tool_calls")
                if not tool_calls:
                    break

                assistant_msg: dict = {"role": "assistant", "content": msg.get("content") or None}
                openai_tool_calls = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    openai_tool_calls.append({
                        "id": tc.get("id", ""), "type": "function",
                        "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments", "")},
                    })
                assistant_msg["tool_calls"] = openai_tool_calls
                messages.append(assistant_msg)

                for tc in tool_calls:
                    fn = tc.get("function", {})
                    tc_id = tc.get("id", "")
                    tc_name = fn.get("name", "")
                    tc_args_str = fn.get("arguments", "")
                    try:
                        args = json.loads(tc_args_str) if tc_args_str else {}
                    except json.JSONDecodeError:
                        args = {}

                    tool = self.tools.get(tc_name)
                    if tool:
                        try:
                            result = await tool.execute(args)
                            result_text = result.content
                        except Exception as e:
                            result_text = f"Error: {e}"
                    else:
                        result_text = f"Unknown tool: {tc_name}"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": result_text,
                    })
                    # Rant 2026-08-17T12:03:13: include the human-readable purpose
                    purpose = tool.definition().purpose if tool else "unknown tool"
                    logger.debug(
                        "consolidation tool: %s — %s → %s%s",
                        tc_name, purpose, _redact_string(result_text[:100]),
                        "…" if len(result_text) > 100 else "",
                    )
        except Exception:
            logger.debug("memory consolidation failed", exc_info=True)


async def run_server(llm_config: LlmConfig) -> None:
    """Run the EMRG server until interrupted."""
    server = EmrgServer(llm_config)
    try:
        await server.serve()
    except KeyboardInterrupt:
        logger.info("shutdown signal received")
