"""Task-based scheduler — replaces BackgroundThread with independent coroutines.

Each task in ~/.emrg/tasks.yml gets its own asyncio.create_task() coroutine.
The scheduler only manages lifecycle (start/stop/monitor); handlers are self-contained.

projects.yml remains for project tracking (_touch_project only).
tasks.yml controls what gets auto-evolved.

Task config schema:
  name, type, enabled, interval, last_run — common base fields.
  config — type-specific config. For evolution: config.project links to projects.yml name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
import yaml

from emrg.config import config_dir
from emrg.connect import connect_to_server
from websockets.exceptions import ConnectionClosed
from emrg.protocol import EvolutionLog, InstanceIdentity
from emrg.server.atomic import atomic_write_yaml
from emrg.server.git_utils import (
    _detect_git_remote,
    resolve_git_gh,
)

logger = logging.getLogger("emrg.server.scheduler")

# ── Module-level constants (shared with daemon) ──────────────────
EVOLUTION_CWD = Path.home() / ".emrg" / "evolution"

# Template files for each task type. All use the same format variables
# ({instance_id}, {host_name}, {uptime}, ...).
TASK_TEMPLATES: dict[str, str] = {
    "evolution": "evolution_prompt.md",
    "paper": "paper_prompt.md",
    "open-source": "open_source_prompt.md",
    "promote": "promote_prompt.md",
}

# ── Task CRUD constants (rant 2026-08-12T18:23:15 P2) ─────────────
TASK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TASK_NAME_MAX = 32
MIN_INTERVAL = 60
DEFAULT_INTERVAL = 1800


def _task_templates_dir() -> Path:
    """Directory holding user-defined task type templates."""
    return config_dir() / "task-templates"


def _custom_templates() -> list[str]:
    """Names of user-defined task types (sorted, *.md basenames)."""
    d = _task_templates_dir()
    if not d.is_dir():
        return []
    try:
        return sorted(p.stem for p in d.glob("*.md"))
    except OSError:
        return []


def _read_custom_template(name: str) -> str | None:
    """Read a user template's prompt text; None if missing."""
    p = _task_templates_dir() / f"{name}.md"
    try:
        if p.exists():
            return p.read_text(encoding="utf-8")
    except OSError:
        pass
    return None


def _write_custom_template(name: str, prompt: str) -> None:
    """Atomically write a user template."""
    d = _task_templates_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(prompt, encoding="utf-8")
    tmp.replace(p)


def _delete_custom_template(name: str) -> None:
    p = _task_templates_dir() / f"{name}.md"
    try:
        p.unlink()
    except OSError:
        pass


def _resolve_task_template(task_type: str) -> Path:
    """Resolve the prompt template for a task type.

    Built-in TASK_TEMPLATES take priority; custom task types (rant
    2026-08-12T18:14:46 P2) fall back to a user template in
    ``~/.emrg/task-templates/<type>.md``, then to the generic
    evolution prompt. Pure helper so P2 can unit-test lookup without
    a scheduler instance.
    """
    builtin = TASK_TEMPLATES.get(task_type)
    if builtin:
        return Path(__file__).parent / builtin
    user_tpl = config_dir() / "task-templates" / f"{task_type}.md"
    if user_tpl.exists():
        return user_tpl
    return Path(__file__).parent / "evolution_prompt.md"


def _task_cfg_signature(cfg: dict) -> tuple:
    """Stable signature of a task cfg for hot-reload diffing.

    Any change to type / config / interval / enabled marks the task
    as needing a handler restart.
    """
    conf = cfg.get("config") if isinstance(cfg.get("config"), dict) else {}
    return (
        cfg.get("name"),
        cfg.get("type"),
        json.dumps(conf, sort_keys=True),
        cfg.get("interval", DEFAULT_INTERVAL),
        bool(cfg.get("enabled", True)),
    )


def _resolve_project_path(name: str) -> str | None:
    """Resolve a project name to its path from projects.yml."""
    projects_file = config_dir() / "projects.yml"
    if not projects_file.exists():
        return None
    try:
        data = yaml.safe_load(projects_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(data, list):
        return None
    for entry in data:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry.get("path")
    return None


def _load_project_config(name: str, source_dir: str) -> dict:
    """Return the full projects.yml entry matching the current project.

    Matches by ``name`` first, then by ``path`` against ``source_dir``.
    Returns an empty dict when no match is found. Custom fields added by
    the user in projects.yml are preserved, so templates can reference
    them via ``{{ project.<field> }}`` without code changes.
    """
    projects_file = config_dir() / "projects.yml"
    if not projects_file.exists():
        return {}
    try:
        data = yaml.safe_load(projects_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return {}
    if not isinstance(data, list):
        return {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if name and entry.get("name") == name:
            return entry
        if source_dir and entry.get("path") == source_dir:
            return entry
    return {}


# ── TaskHandler ────────────────────────────────────────────


class TaskHandler:
    """Self-contained evolution loop for one project (or emrg itself).

    Each handler runs its own while+sleep(interval) coroutine,
    independent of all other handlers.
    """

    EMRG_REPO_URL = "https://github.com/argszero/emrg.git"
    OWNER = "argszero"
    REPO = "emrg"

    def __init__(
        self,
        name: str,
        config: dict,
        interval: int,
        identity: InstanceIdentity,
        template_path: Path | None = None,
    ) -> None:
        self.name = name
        # Rant 2026-08-19T10:18:44: per-task logger — LoggerAdapter injects a
        # `task` extra; the daemon's custom Formatter renders it as a dedicated
        # [task] column in emrgd.log (scheduler lines are otherwise hard to
        # tell apart when several tasks interleave). TaskScheduler-level logs
        # keep the module logger (no task extra → "-" column).
        self._logger = logging.LoggerAdapter(logger, {"task": self.name})
        self._template_path = template_path or (Path(__file__).parent / "evolution_prompt.md")
        self._config = config
        self.interval = interval
        self.identity = identity
        self._running = False
        self._start_time: float | None = None
        self._trigger_event = asyncio.Event()
        self._cycle_running = False
        self._next_run_at: float | None = None
        self._logs_dir = config_dir() / "logs"
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self.evolutions: list[EvolutionLog] = []

        # ── Saturation — slow down, never stop (rant 2026-08-09T09:35:55) ──
        # Track consecutive empty cycles (rant 2026-08-17T11:39:19: the agent
        # itself answers whether a round was meaningful — git HEAD compares
        # commits, not value, so it was removed as the empty-cycle oracle).
        # After the threshold of empty cycles, switch to low-frequency
        # heartbeat full cycles instead of the old complete halt:
        #   - Scheduled runs continue at heartbeat interval (never skipped)
        #   - heartbeat = max(interval, min(interval*8, 8h)) — 60s task → 8min
        #   - Manual trigger (/trigger) or upstream git HEAD advance restores
        #     the normal frequency immediately (counter reset to 0)
        #   - The agent's recommend_slowdown votes (3) tighten the threshold
        #     from 30 to 10 (host 2026-08-17T11:39:19)
        #
        # Counter is persisted to disk to survive daemon restarts.
        self._IDLE_HALT_THRESHOLD = 30
        self._SLOWDOWN_VOTES_TO_TIGHTEN = 3
        self._TIGHTENED_THRESHOLD = 10
        # G129: 连续连接失败告警阈值——达到后升级为 ERROR（防静默吞掉，
        # rant 2026-08-09T08:03:46：GUI 测试覆盖真实 emrgd.port 致 10h 连不上）。
        self._CONNECT_FAIL_ALERT = 3
        self._connect_failures = 0
        self._saturation_dir = config_dir() / "saturation"
        self._saturation_dir.mkdir(parents=True, exist_ok=True)
        self._saturation_file = self._saturation_dir / f"{self.name}.json"
        self._empty_cycles, self._slowdown_hits = self._load_saturation_state()

        # Resolve project path from config (new schema) or fall back to
        # config.path for backward-compat with old tasks.yml entries.
        project_name = config.get("project", "")
        self._project_name = project_name
        path = _resolve_project_path(project_name) if project_name else config.get("path", "")
        self.project_path = path or name  # default to name for emrg itself

        # Derive owner/repo/git from config override → path git remote → defaults.
        # (rant 2026-08-19T14:20:52: workspace self-heal deleted — these fields
        # remain for prompt context {repo}/{owner} and task-config resolution;
        # the agent manages its own git workspace via tools.)
        repo_spec = _detect_git_remote(path) if path else ""
        cfg_owner = config.get("owner")
        cfg_repo = config.get("repo")
        if cfg_owner and cfg_repo:
            self._owner, self._repo = cfg_owner, cfg_repo
            self._repo_url = f"https://github.com/{self._owner}/{self._repo}.git"
            self._repo_configured = True
        elif repo_spec and "/" in repo_spec:
            self._owner, self._repo = repo_spec.split("/", 1)
            self._repo_url = f"https://github.com/{self._owner}/{self._repo}.git"
            self._repo_configured = True
        else:
            self._owner = self.OWNER
            self._repo = self.REPO
            self._repo_url = self.EMRG_REPO_URL
            self._repo_configured = project_name == "emrg"
        self._session_id = f"emrg-evolution-{name}"
        self._source_dir = path or name

    # ── Saturation state (restored from disk across daemon restarts) ──

    def _load_saturation_state(self) -> tuple[int, int]:
        """Restore (empty_cycles, slowdown_hits) from disk (daemon restarts)."""
        try:
            if self._saturation_file.exists():
                data = json.loads(self._saturation_file.read_text(encoding="utf-8"))
                count = int(data.get("empty_cycles", 0) or 0)
                slowdown = int(data.get("slowdown_hits", 0) or 0)
                if count > 0 or slowdown > 0:
                    self._logger.debug(
                        "TaskHandler[%s]: restored saturation state (%d empty cycles, %d slowdown votes)",
                        self.name, count, slowdown,
                    )
                return count, slowdown
        except Exception:
            pass
        return 0, 0

    def _save_saturation_state(self) -> None:
        """Persist (empty_cycles, slowdown_hits) to disk."""
        try:
            self._saturation_file.write_text(
                json.dumps(
                    {"empty_cycles": self._empty_cycles,
                     "slowdown_hits": self._slowdown_hits},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _saturation_threshold(self) -> int:
        """Empty-cycle threshold before dropping to heartbeat cadence.

        The agent's recommend_slowdown votes (3) tighten the threshold from
        30 to 10 — the agent itself keeps reporting the task has no value
        (rant 2026-08-17T11:39:19)."""
        if self._slowdown_hits >= self._SLOWDOWN_VOTES_TO_TIGHTEN:
            return self._TIGHTENED_THRESHOLD
        return self._IDLE_HALT_THRESHOLD

    async def run(self) -> None:
        """Run evolution cycles at configured interval.

        Uses an asyncio.Event for interruptible sleep — manual triggers
        via trigger() wake the coroutine immediately.
        """
        self._running = True
        self._start_time = time.time()
        self._logger.info(
            "TaskHandler[%s] started — every %ds", self.name, self.interval
        )

        while self._running:
            # Saturation → wait at the heartbeat interval (low-frequency full
            # cycle, rant 2026-08-09T09:35:55); otherwise the normal interval.
            # Manual trigger wakes immediately either way. Never skip a cycle.
            wait_timeout = (
                self._heartbeat_interval()
                if self._saturation_heartbeat_active()
                # Rant 2026-08-09T13:16:36: exponential backoff while the
                # daemon is unreachable — stops the retry/window storm.
                else self._connect_backoff()
            )
            # Diagnostic log (rant 2026-08-18T20:48:45): expose which
            # scheduling mode drove the wait — normal | heartbeat | backoff —
            # so the saturation/backoff state machine is traceable end-to-end.
            if self._empty_cycles >= self._saturation_threshold():
                _mode = "heartbeat"
            elif self._connect_failures > 0:
                _mode = "backoff"
            else:
                _mode = "normal"
            self._logger.debug(
                "TaskHandler[%s]: next run in %ds (mode=%s)",
                self.name, int(wait_timeout), _mode,
            )
            # Wait for interval or manual trigger (interruptible)
            self._next_run_at = time.time() + wait_timeout
            manual_trigger = False
            try:
                await asyncio.wait_for(
                    self._trigger_event.wait(),
                    timeout=wait_timeout,
                )
                # Manual trigger fired — clear and proceed
                self._trigger_event.clear()
                manual_trigger = True
                self._logger.debug(
                    "TaskHandler[%s] manually triggered", self.name
                )
            except asyncio.TimeoutError:
                # Normal scheduled run
                pass

            # Manual triggers always reset the saturation counter; otherwise
            # saturated ticks keep running full cycles at heartbeat cadence.
            if manual_trigger:
                if self._empty_cycles >= self._saturation_threshold():
                    self._logger.info(
                        "TaskHandler[%s]: resumed via manual trigger "
                        "(was in saturation at %d empty cycles)",
                        self.name, self._empty_cycles,
                    )
                self._empty_cycles = 0
                self._slowdown_hits = 0
                self._save_saturation_state()

            self._logger.debug("TaskHandler[%s] tick", self.name)
            self._cycle_running = True
            self._next_run_at = None  # running — no next time yet
            try:
                await self._run_evolution_cycle()
            except Exception:
                self._logger.warning(
                    "TaskHandler[%s] crashed", self.name, exc_info=True
                )
            finally:
                self._cycle_running = False
                self._trigger_event.clear()  # clear any spurious set during cycle

        await self._write_final_summary()
        self._logger.info("TaskHandler[%s] stopped", self.name)

    def stop(self) -> None:
        self._running = False

    def trigger(self) -> tuple[str, str | None]:
        """Manually trigger an immediate evolution cycle.

        Returns (result, detail):
          - ("running", detail) — cycle is currently executing
          - ("triggered", detail) — cycle will start immediately
        """
        if self._cycle_running:
            return ("running", "task is currently executing")
        if self._next_run_at is not None:
            remaining = max(0, int(self._next_run_at - time.time()))
            if remaining > 0:
                detail = (
                    f"next run moved from ~{remaining}s from now to immediately"
                )
            else:
                detail = "triggered immediately"
        else:
            detail = "triggered immediately"
        self._trigger_event.set()
        return ("triggered", detail)

    def status(self) -> dict:
        """Return current handler status."""
        remaining = None
        if self._next_run_at is not None:
            remaining = max(0, int(self._next_run_at - time.time()))
        # rant 2026-08-18T10:45:52: expose the LAST-execution dimension so the
        # GUI tasks panel can show when a task last ran, what it did, and
        # whether it is being throttled (saturation). All data is in-memory /
        # on disk already — no extra I/O beyond the in-memory evolutions list.
        last_run_at: str | None = None
        last_cycle_summary: str | None = None
        if self.evolutions:
            last = self.evolutions[-1]
            last_run_at = last.timestamp
            # rant 2026-08-19T07:06:45 (host-finalized): NO machine impact
            # fallback — empty summary shows as None (GUI renders "-").
            last_cycle_summary = last.summary if last.summary else None
        # rant 2026-08-18T21:32:32: last 5 run records for the GUI accordion
        # subtable — {timestamp, summary, impact, meaningful,
        # recommend_slowdown, tool_count}; all in-memory, no extra I/O.
        recent_runs = []
        for log in self.evolutions[-5:]:
            recent_runs.append({
                "timestamp": log.timestamp,
                "summary": log.summary,
                "impact": list(log.impact),
                "meaningful": log.meaningful,
                "recommend_slowdown": log.recommend_slowdown,
                "tool_count": log.tool_count,
            })
        saturation = {
            "empty_cycles": self._empty_cycles,
            "threshold": self._saturation_threshold(),
            "heartbeat_interval": self._heartbeat_interval(),
            "heartbeat_active": self._saturation_heartbeat_active(),
        }
        return {
            "name": self.name,
            "running": self._cycle_running,
            "next_run_in_seconds": remaining,
            "interval": self.interval,
            "last_run_at": last_run_at,
            "last_cycle_summary": last_cycle_summary,
            "recent_runs": recent_runs,
            "saturation": saturation,
        }

    def _heartbeat_interval(self) -> int:
        """Low-frequency heartbeat interval (rant 2026-08-09T09:35:55):
        heartbeat = max(task_interval, min(task_interval * 8, 8 hours)).
        Protection = slow down, never stop. Long-interval tasks (>= 8h)
        keep their original cadence (the 8x/8h caps don't apply).
        """
        return max(self.interval, min(self.interval * 8, 8 * 3600))

    def _connect_backoff(self) -> float:
        """Exponential backoff while the daemon is unreachable.

        Rant 2026-08-09T13:16:36 (v0.2.15 Windows regression): when the
        daemon is down (emrgd.port missing), every tick's connect failure
        returned immediately and the loop re-ran at full interval — with
        multiple handlers that produced a per-second retry/window storm.
        Backoff = max(30s, interval * 2^n) capped at 10 minutes, where n
        is the consecutive-failure count. Returns the normal interval when
        there are no consecutive failures.
        """
        if self._connect_failures <= 0:
            return float(self.interval)
        n = min(self._connect_failures, 10)  # cap the exponent growth
        backoff = max(30.0, float(self.interval) * (2 ** n))
        return min(backoff, 600.0)  # never wait longer than 10 minutes

    def _saturation_heartbeat_active(self) -> bool:
        """Whether this tick should run at the low-frequency heartbeat interval
        instead of the normal interval.

        Replaces the old complete saturation halt (rant 2026-08-09T09:35:55):
        at/above the empty-cycle threshold the handler keeps running full
        cycles, just at a reduced cadence — never skipping. Saturation
        judgment depends ONLY on the empty-cycle count (rant 2026-08-18T20:32:07):
        no upstream network check — recovery happens naturally when a cycle
        produces output and the empty counter resets.
        """
        if self._empty_cycles < self._saturation_threshold():
            return False
        self._logger.info(
            "TaskHandler[%s]: saturation (%d empty cycles) — "
            "running full cycle at heartbeat interval (%ds) — never halting",
            self.name, self._empty_cycles, self._heartbeat_interval(),
        )
        return True

    async def _request_vibe_check(self, ws, prompt: str, completion_summary: str) -> dict | None:
        """Ask the daemon for a structured vibe check on the SAME connection.

        Sends ``task_vibe_check`` and waits for ``vibe_check_result`` (~20s).
        Fully defensive — any failure/timeout returns None; the caller
        conservatively leaves the empty-cycle counter unchanged.
        """
        try:
            await ws.send(json.dumps({
                "type": "task_vibe_check",
                "session_id": self._session_id,
                "cwd": self._source_dir,
                "task_name": self.name,
                "prompt": (prompt or "")[:2000],
                "completion_summary": (completion_summary or "")[:3000],
            }, ensure_ascii=False))
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                remaining = max(0.5, deadline - time.monotonic())
                try:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                except asyncio.TimeoutError:
                    break
                except ConnectionClosed:
                    break
                if frame.get("type") != "vibe_check_result":
                    continue
                if not frame.get("ok"):
                    self._logger.warning(
                        "TaskHandler[%s]: vibe check error: %s",
                        self.name, (frame.get("error") or "")[:120],
                    )
                    return None
                result = frame.get("result") or {}
                return {
                    "meaningful": result.get("meaningful"),
                    "recommend_slowdown": result.get("recommend_slowdown"),
                    "reason": result.get("reason", ""),
                    "done": result.get("done", ""),
                }
        except Exception:
            self._logger.debug("TaskHandler[%s]: vibe check failed", self.name, exc_info=True)
        return None

    async def _run_evolution_cycle(self) -> None:

        # Rant 2026-08-19T14:20:52 — workspace self-heal deleted: git workspace
        # management is the agent's job (bash tools). The cycle proceeds
        # directly to prompt build / daemon connection; if the configured
        # source path is invalid the agent discovers it via tool errors.
        cycle_time = datetime.now()
        prompt = self._build_evolution_prompt()
        self._logger.info(
            "TaskHandler[%s]: prompt built (%d chars), connecting ...",
            self.name, len(prompt),
        )
        start_time = cycle_time

        try:
            ws = await connect_to_server()
            self._logger.info("TaskHandler[%s]: connected", self.name)
            self._connect_failures = 0
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            # G129 (rant 2026-08-09T08:03:46): 连接失败不得静默吞掉——GUI 测试曾把
            # 假 port 值写进真实 ~/.emrg/emrgd.port，导致演化周期 10 小时连不上
            # daemon（WinError 1225）只留下 WARNING。累计失败达到阈值后升级为
            # ERROR 告警，提示 port 文件可能被外部覆盖（检查 ~/.emrg/emrgd.port）。
            self._connect_failures += 1
            port_path = config_dir() / "emrgd.port"
            if self._connect_failures >= self._CONNECT_FAIL_ALERT:
                self._logger.error(
                    "TaskHandler[%s]: cannot connect for %d consecutive cycles "
                    "(%s) — daemon unreachable. Check %s (may have been overwritten "
                    "by GUI tests or stale after daemon restart); run 'emrg server' "
                    "or restart the daemon to recover.",
                    self.name, self._connect_failures, e, port_path,
                )
            else:
                self._logger.warning(
                    "TaskHandler[%s]: cannot connect (%d/%d): %s",
                    self.name, self._connect_failures, self._CONNECT_FAIL_ALERT, e,
                )
            return

        task_msg = json.dumps(
            {
                "type": "task",
                "id": f"evolution-{cycle_time.isoformat()}",
                "session_id": self._session_id,
                "cwd": self._source_dir,
                "prompt": prompt,
                "stream": True,
                "timestamp": cycle_time.isoformat(),
            },
            ensure_ascii=False,
        )

        tool_count = 0
        error = None
        truncated = False
        completion_content = ""

        try:
            await ws.send(task_msg)

            while True:
                try:
                    resp = json.loads(await ws.recv())
                except ConnectionClosed:
                    break
                if resp.get("done"):
                    duration = int((datetime.now() - cycle_time).total_seconds())
                    # Distinguish truncation from successful completion: the
                    # daemon's max-tool-rounds frame (daemon.py "Exceeded
                    # maximum tool call rounds") is a done frame too — without
                    # this check a truncated cycle is misreported as complete
                    # and (when HEAD is unchanged) counted as an *empty* cycle,
                    # wrongly advancing the idle-halt backoff (mem repo lesson:
                    # truncation must be flagged, not silently treated as done).
                    content = resp.get("content") or ""
                    completion_content = content
                    truncated = "exceeded" in content.lower()
                    if truncated:
                        self._logger.warning(
                            "TaskHandler[%s] truncated (max tool rounds, tools=%d, duration=%ds)",
                            self.name, tool_count, duration,
                        )
                    else:
                        self._logger.info(
                            "TaskHandler[%s] complete (tools=%d, duration=%ds)",
                            self.name, tool_count, duration,
                        )
                    break

                if "tool_name" in resp:
                    tool_count += 1

                resp_error = resp.get("error")
                if isinstance(resp_error, str):
                    error = str(resp_error)
                    self._logger.warning(
                        "TaskHandler[%s] server error: %s",
                        self.name, error,
                    )
                    break

            # Empty-cycle detection (rant 2026-08-17T11:39:19): after a clean
            # completion, ask the agent via task_vibe_check whether the round
            # was meaningful. Done on the SAME ws connection (daemon replies
            # with vibe_check_result). Any failure → None → counter untouched.
            vibe_result = None
            if not error and not truncated:
                vibe_result = await self._request_vibe_check(
                    ws, prompt=prompt,
                    completion_summary=completion_content[:3000],
                )
        except Exception as e:
            self._logger.exception("TaskHandler[%s] error", self.name)
            error = str(e)
        finally:
            try:
                await ws.close()
            except Exception:
                pass

        # Empty-cycle accounting (rant 2026-08-17T11:39:19): the AGENT decides
        # whether the round was meaningful (task_vibe_check structured answer),
        # not git HEAD — HEAD compares commits, so an agent that did analysis /
        # memory work without a commit was miscounted as empty, and a no-op
        # round over someone else's push counted as work.
        #   - meaningful: false → empty cycle (advance the backoff)
        #   - meaningful: true  → reset the empty streak (+ slowdown votes)
        #   - recommend_slowdown: true → +1 slowdown vote (3 votes tighten
        #     the saturation threshold from 30 to 10)
        #   - vibe check unavailable (ok=false / timeout / parse error) →
        #     conservative: count unchanged (neither advance nor reset)
        # A truncated cycle is NOT empty — the agent wanted to work but hit the
        # tool-round cap; counting it would wrongly back off the handler.
        # An aborted cycle (server error like "session busy", or an exception)
        # is NOT empty either — the agent was blocked before reaching an NTE
        # conclusion; counting it would also advance the idle-halt backoff.
        if not error and not truncated and vibe_result is not None:
            meaningful = vibe_result.get("meaningful")
            recommend = bool(vibe_result.get("recommend_slowdown"))
            if meaningful is False:
                self._empty_cycles += 1
                if recommend:
                    self._slowdown_hits += 1
                self._save_saturation_state()
                self._logger.info(
                    "TaskHandler[%s]: empty cycle #%d (agent: %s%s)",
                    self.name, self._empty_cycles,
                    (vibe_result.get("reason") or "")[:100],
                    f"; slowdown votes {self._slowdown_hits}/{self._SLOWDOWN_VOTES_TO_TIGHTEN}"
                    if recommend else "",
                )
            elif meaningful is True:
                if self._empty_cycles > 0 or self._slowdown_hits > 0:
                    self._logger.info(
                        "TaskHandler[%s]: agent reported meaningful work, "
                        "resetting empty streak (%d) + slowdown votes (%d)",
                        self.name, self._empty_cycles, self._slowdown_hits,
                    )
                self._empty_cycles = 0
                self._slowdown_hits = 0
                self._save_saturation_state()
        elif not error and not truncated:
            # vibe check failed/timeout — conservative: don't count, don't reset
            self._logger.info(
                "TaskHandler[%s]: vibe check unavailable — empty streak unchanged",
                self.name,
            )
        else:
            if self._empty_cycles > 0 or self._slowdown_hits > 0:
                reason = "truncated cycle" if truncated else "aborted cycle"
                if error:
                    reason = f"aborted cycle ({error[:80]})"
                self._logger.info(
                    "TaskHandler[%s]: %s, resetting empty streak",
                    self.name, reason,
                )
            self._empty_cycles = 0
            self._slowdown_hits = 0
            self._save_saturation_state()

        # Aborted cycles are not evolutions: no log file, no count. Writing
        # them would inflate the evolution count (GUI growth card / toast,
        # evolution_summary) with work that never happened — the "session
        # busy" aborts observed when interactive sessions hold the daemon.
        if error:
            self._logger.warning(
                "TaskHandler[%s]: cycle aborted (%s) — not counted as evolution",
                self.name, error[:200],
            )
            return

        cycle_ts = cycle_time.isoformat()
        # Rant 2026-08-12T18:03:26: cycle records are now memory entries
        # (memory/cycle-<ts>.md, written by the agent per evolution_prompt §6),
        # not standalone evolution-cycle-*.md files — keep the impact tag
        # aligned with the new naming.
        impact = [
            f"cycle-{cycle_ts}-{'truncated' if truncated else 'complete'}",
            f"tools-executed={tool_count}",
        ]
        if truncated:
            impact.append("truncated=max-tool-rounds")

        # rant 2026-08-18T21:32:32: persist the agent's own summary of what
        # meaningful work was done (vibe check "done" field) + the vibe flags,
        # so the GUI task recent-runs table shows real value, not a machine
        # string. Rant 2026-08-19T07:06:45 (host-finalized): NO fallback to the
        # completion first line — summary uses only the vibe check "done"
        # field; empty stays empty (GUI shows "-"), never a machine fallback.
        summary = ""
        meaningful = None
        recommend = False
        if vibe_result is not None:
            summary = str(vibe_result.get("done") or "")[:500]
            meaningful = vibe_result.get("meaningful")
            recommend = bool(vibe_result.get("recommend_slowdown"))

        log = EvolutionLog(
            timestamp=cycle_ts,
            trigger=f"evolution-{self.name}-{cycle_ts}",
            impact=impact,
            operations=["llm-reflection", "tool-execution", "self-improvement"],
            summary=summary,
            meaningful=meaningful,
            recommend_slowdown=recommend,
            tool_count=tool_count,
        )
        # Rant 2026-08-19T14:18:40 — no disk archival: the evolution log lives
        # in the in-memory list only (GUI recent-runs + evolution_count both
        # read self.evolutions); evolution-*.json was never consumed.
        self.evolutions.append(log)

    def _build_evolution_prompt(self) -> str:
        """Build evolution prompt from a Jinja2 template.

        Uses ``jinja2.Undefined`` so any ``{{ var }}`` not present in the
        context renders as an empty string instead of raising (the old
        ``str.format()`` crashed with KeyError on missing placeholders).
        Templates also receive ``task`` (the tasks.yml config dict) and
        ``project`` (the matching projects.yml entry) so users can reference
        custom fields via ``{{ task.x }}`` / ``{{ project.x }}`` without
        code changes.
        """
        import jinja2

        if self._start_time is not None:
            uptime_seconds = int(time.time() - self._start_time)
        else:
            uptime_seconds = 0
        uptime = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m"

        git_path, gh_path = resolve_git_gh()

        context = {
            "instance_id": self.identity.instance_id,
            "host_name": self.identity.host_name,
            "uptime": uptime,
            "evolution_count": len(self.evolutions),
            "repo_url": self._repo_url,
            "evolution_cwd": str(EVOLUTION_CWD),
            "local_source": str(self._source_dir),
            "owner": self._owner,
            "repo": self._repo,
            "source_dir": str(self._source_dir),
            "session_id": self._session_id,
            "timestamp": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "task": self._config,
            "project": _load_project_config(self._project_name, str(self._source_dir)),
            "git_path": git_path,
            "gh_path": gh_path,
        }

        env = jinja2.Environment(undefined=jinja2.Undefined)
        template = env.from_string(self._template_path.read_text(encoding="utf-8"))
        return template.render(**context)

    async def _write_final_summary(self) -> None:
        if not self.evolutions:
            return
        summary = {
            "shutdown_at": datetime.now().isoformat(),
            "total_evolutions": len(self.evolutions),
            "first_evolution": self.evolutions[0].timestamp,
            "last_evolution": self.evolutions[-1].timestamp,
        }
        path = self._logs_dir / "summary.json"
        path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


# ── TaskScheduler ────────────────────────────────────────────────


class TaskScheduler:
    """Manages tasks from ~/.emrg/tasks.yml.

    Each enabled task gets an independent asyncio coroutine.
    The scheduler only creates/monitors tasks; handlers are self-contained.
    """

    HANDLERS: dict[str, type] = {
        "evolution": TaskHandler,
        "paper": TaskHandler,  # same handler, different template
        "open-source": TaskHandler,  # same handler, different template
        "promote": TaskHandler,  # same handler, different template
    }

    def __init__(self, identity: InstanceIdentity) -> None:
        self.identity = identity
        self._tasks_file: Path | None = None
        self._handlers: list[TaskHandler] = []
        self._coros: list[asyncio.Task] = []
        # cfg (from tasks.yml) used to start each handler — for hot-reload diffing.
        self._handler_cfgs: dict[str, dict] = {}

    @property
    def _tasks_file(self) -> Path:
        """tasks.yml path — resolved lazily so config_dir() patches work
        (hermeticity guard #738: tests patch config_dir after construction,
        and tests may override _tasks_file directly with a tmp path)."""
        if self.__tasks_file is None:
            self.__tasks_file = config_dir() / "tasks.yml"
        return self.__tasks_file

    @_tasks_file.setter
    def _tasks_file(self, value: Path | None) -> None:
        self.__tasks_file = value

    def _build_handler(self, cfg: dict) -> TaskHandler:
        """Construct a TaskHandler for a task cfg (pure sync construction).

        May block briefly (git remote detection in ``TaskHandler.__init__``
        + template lookup) — callers in the event loop must offload via
        ``asyncio.to_thread`` (rant 2026-08-19T01:05:47: no blocking calls
        in the loop).
        """
        template_path = _resolve_task_template(cfg["type"])
        return TaskHandler(
            name=cfg["name"],
            config=cfg.get("config", {}),
            interval=cfg.get("interval", DEFAULT_INTERVAL),
            identity=self.identity,
            template_path=template_path,
        )

    def _start_handler_for(self, cfg: dict) -> TaskHandler:
        """Create + start a handler for a task cfg; returns the handler.

        Boot path (no websocket clients connected yet) — sync construction
        is acceptable here.
        """
        handler = self._build_handler(cfg)
        self._handlers.append(handler)
        self._handler_cfgs[handler.name] = cfg
        self._coros.append(asyncio.create_task(handler.run()))
        return handler

    async def _start_handler_async(self, cfg: dict) -> TaskHandler:
        """Hot-reload path (apply_tasks, on the event loop while serving):
        offload the sync handler construction to a worker thread so a slow
        git probe never freezes the loop (rant 2026-08-19T01:05:47)."""
        handler = await asyncio.to_thread(self._build_handler, cfg)
        self._handlers.append(handler)
        self._handler_cfgs[handler.name] = cfg
        self._coros.append(asyncio.create_task(handler.run()))
        return handler

    def _stop_handler(self, handler: TaskHandler) -> None:
        """Stop a handler and cancel its coroutine (hot-reload removal/restart)."""
        handler.stop()
        try:
            idx = self._handlers.index(handler)
        except ValueError:
            idx = -1
        if idx >= 0:
            coro = self._coros[idx]
            coro.cancel()
            del self._coros[idx]
            del self._handlers[idx]
        self._handler_cfgs.pop(handler.name, None)

    def load_and_start(self) -> list[asyncio.Task]:
        """Load tasks.yml, start all enabled tasks, return coroutine list."""
        # Self-heal: packaged installs may lack the emrg self-evolution task.
        # This must run here (not inside the handler) because a missing task
        # means no TaskHandler is ever started (rant 20:42 方案 C).
        self._ensure_self_evolution_task()

        tasks_config = self._load_tasks()
        if not tasks_config:
            # Bootstrap: if projects.yml has auto_evolve entries but
            # tasks.yml is empty, migrate them.
            self._migrate_from_projects()

        tasks_config = self._load_tasks()
        for cfg in tasks_config:
            if not cfg.get("enabled", True):
                continue
            handler_cls = self.HANDLERS.get(cfg["type"], TaskHandler)
            if handler_cls is None:
                logger.warning(
                    "TaskScheduler: unknown type %r for task %r",
                    cfg["type"], cfg["name"],
                )
                continue
            self._start_handler_for(cfg)
            logger.info(
                "TaskScheduler: started %s[%s] every %ds",
                cfg["type"], cfg["name"], cfg.get("interval", DEFAULT_INTERVAL),
            )

        return self._coros

    def stop_all(self) -> None:
        """Stop all running handlers."""
        for handler in self._handlers:
            handler.stop()
        for coro in self._coros:
            coro.cancel()

    def trigger_task(self, name: str) -> dict | None:
        """Manually trigger a task by name. Returns result dict or None if not found."""
        for handler in self._handlers:
            if handler.name == name:
                result, detail = handler.trigger()
                return {"name": name, "result": result, "detail": detail}
        return None

    def list_tasks(self) -> list[dict]:
        """Return status for all running handlers.

        MUST stay pure in-memory (rant 2026-08-18T20:48:45): this is the
        /trigger + tasks-panel request path — any I/O here stalls every WS
        message. The per-handler timing below exists to catch exactly that:
        total >200ms → WARNING with a per-handler breakdown, >50ms → DEBUG.
        """
        start = time.monotonic()
        results = []
        per_handler: list[str] = []
        for handler in self._handlers:
            h_start = time.monotonic()
            results.append(handler.status())
            per_handler.append(f"{handler.name}={1000 * (time.monotonic() - h_start):.1f}ms")
        elapsed_ms = 1000 * (time.monotonic() - start)
        if elapsed_ms > 200:
            logger.warning(
                "TaskScheduler: list_tasks took %.1fms (>200ms — "
                "status() must stay in-memory) — per-handler: %s",
                elapsed_ms, ", ".join(per_handler),
            )
        elif elapsed_ms > 50:
            logger.debug(
                "TaskScheduler: list_tasks took %.1fms — per-handler: %s",
                elapsed_ms, ", ".join(per_handler),
            )
        return results

    def total_evolutions(self) -> int:
        """Total completed evolution cycles across all running handlers."""
        return sum(len(handler.evolutions) for handler in self._handlers)

    async def wait_all(self) -> None:
        """Wait for all handler coroutines to finish (after cancel)."""
        for coro in self._coros:
            try:
                await coro
            except asyncio.CancelledError:
                pass

    def _load_tasks(self) -> list[dict]:
        """Read tasks.yml and return list of task configs."""
        if not self._tasks_file.exists():
            return []
        try:
            data = yaml.safe_load(self._tasks_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            logger.warning("TaskScheduler: failed to parse %s", self._tasks_file)
            return []
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict)]

    def _save_tasks(self, tasks: list[dict]) -> None:
        """Atomically write tasks.yml."""
        atomic_write_yaml(tasks, self._tasks_file, prefix=".tasks_")

    def _migrate_from_projects(self) -> None:
        """One-time migration: auto_evolve=True entries -> tasks.yml."""
        projects_file = config_dir() / "projects.yml"
        if not projects_file.exists():
            return
        try:
            data = yaml.safe_load(projects_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            return
        if not isinstance(data, list):
            return

        new_tasks = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if entry.get("auto_evolve"):
                project_name = entry.get("name", "unknown")
                new_tasks.append({
                    "name": project_name,
                    "type": "evolution",
                    "config": {"project": project_name},
                    "interval": entry.get("interval", 1800),
                    "enabled": True,
                    "last_run": None,
                })
                logger.info(
                    "TaskScheduler: migrated %s → tasks.yml", entry.get("name")
                )

        if new_tasks:
            self._save_tasks(new_tasks)
            logger.info(
                "TaskScheduler: migrated %d auto_evolve entries to tasks.yml",
                len(new_tasks),
            )

    def _ensure_self_evolution_task(self) -> None:
        """Ensure projects.yml has an emrg entry and tasks.yml has the task.

        Packaged installs (or first runs) may lack tasks.yml entirely, or lack
        the emrg-task entry. Without it, no TaskHandler is ever created, so
        the emrg task never runs.

        The projects.yml emrg entry is ensured here too (rant 02:58): if
        projects.yml lacks the entry, _resolve_project_path("emrg") returns
        None and the handler's _source_dir degenerates to the relative string
        "emrg" (dangling cwd). The path is fixed to ~/.emrg/evolution/emrg;
        an existing entry is preserved as-is (dev machines may configure a
        custom path).
        """
        # 1. projects.yml — add name=emrg entry if missing (preserve existing).
        projects_file = config_dir() / "projects.yml"
        try:
            entries: list[dict] = []
            if projects_file.exists():
                data = yaml.safe_load(projects_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    entries = [e for e in data if isinstance(e, dict)]
            if not any(e.get("name") == "emrg" for e in entries):
                entries.append({
                    "name": "emrg",
                    "path": str(EVOLUTION_CWD / "emrg"),
                    "last_active": datetime.now().isoformat(),
                })
                atomic_write_yaml(entries, projects_file, prefix=".projects_")
                logger.info(
                    "TaskScheduler: self-heal — added emrg entry to projects.yml"
                )
            else:
                # Repair a stale emrg entry whose path no longer exists
                # (2026-08-12 incident: a pytest temp dir leaked into
                # projects.yml by a test run; the dir is deleted after the
                # suite, leaving the entry pointing at a dead path forever —
                # list_projects/GUI pickers show a wrong path and the handler
                # re-resolves a dangling dir every cycle). Dev machines with a
                # real custom checkout keep their path (is_dir() True).
                for entry in entries:
                    if entry.get("name") != "emrg":
                        continue
                    existing = entry.get("path")
                    if existing and Path(existing).is_dir():
                        break  # real checkout — preserved as-is
                    entry["path"] = str(EVOLUTION_CWD / "emrg")
                    entry["last_active"] = datetime.now().isoformat()
                    atomic_write_yaml(entries, projects_file, prefix=".projects_")
                    logger.info(
                        "TaskScheduler: self-heal — repaired stale emrg entry "
                        "%r -> %s", existing, entry["path"],
                    )
                    break
        except (yaml.YAMLError, OSError) as e:
            logger.warning(
                "TaskScheduler: projects.yml self-heal failed: %s", e
            )

        # 2. tasks.yml — add emrg-task if missing (existing logic unchanged).
        tasks = self._load_tasks()
        for t in tasks:
            cfg = t.get("config") if isinstance(t.get("config"), dict) else {}
            if t.get("type") == "evolution" and cfg.get("project") == "emrg":
                return  # already present — idempotent
        tasks.append({
            "name": "emrg-task",
            "type": "evolution",
            "config": {"project": "emrg"},
            "interval": 60,
            "enabled": True,
            "last_run": None,
        })
        self._save_tasks(tasks)
        logger.info("TaskScheduler: self-heal — added emrg-task to tasks.yml")

    def create_task(self, name: str, task_type: str, config: dict, interval: int) -> None:
        """Add a new task entry (used by init_auto_evolve).

        config is a dict of type-specific settings (e.g. {'project': 'emrg'}).
        """
        tasks = self._load_tasks()

        # Update existing or append new — match by name
        for t in tasks:
            if t.get("name") == name:
                t["enabled"] = True
                t["interval"] = interval
                t["type"] = task_type
                t["config"] = config
                self._save_tasks(tasks)
                logger.info("TaskScheduler: updated task %s", name)
                return

        tasks.append({
            "name": name,
            "type": task_type,
            "config": config,
            "interval": interval,
            "enabled": True,
            "last_run": None,
        })
        self._save_tasks(tasks)
        logger.info("TaskScheduler: created task %s", name)

    # ── Task CRUD + hot reload (rant 2026-08-12T18:23:15 P2) ────────

    @staticmethod
    def _validate_task_fields(
        name: str, task_type: str, project: str, interval: int,
    ) -> str | None:
        """Return an error string, or None when the fields are valid."""
        if not TASK_NAME_RE.match(name) or len(name) > TASK_NAME_MAX:
            return f"invalid task name {name!r} (^[a-z0-9][a-z0-9-]*$, <= {TASK_NAME_MAX} chars)"
        if task_type not in TASK_TEMPLATES and task_type not in _custom_templates():
            return f"unknown task type {task_type!r} (not builtin, no custom template)"
        if not project or _resolve_project_path(project) is None:
            return f"project {project!r} is not registered in projects.yml"
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < MIN_INTERVAL:
            return f"interval must be an integer >= {MIN_INTERVAL}"
        return None

    def task_create(
        self, name: str, task_type: str, project: str,
        interval: int | None = None, enabled: bool = True,
        repo: str | None = None, description: str | None = None,
    ) -> tuple[bool, str | dict]:
        """Create a task. Returns (ok, error) or (ok, task-dict)."""
        interval = DEFAULT_INTERVAL if interval is None else interval
        err = self._validate_task_fields(name, task_type, project, interval)
        if err:
            return False, err
        tasks = self._load_tasks()
        if any(t.get("name") == name for t in tasks):
            return False, f"task {name!r} already exists"
        cfg: dict = {"project": project}
        if repo:
            cfg["repo"] = repo
        task: dict = {
            "name": name,
            "type": task_type,
            "config": cfg,
            "interval": interval,
            "enabled": bool(enabled),
            "last_run": None,
        }
        if description:
            task["description"] = description
        tasks.append(task)
        self._save_tasks(tasks)
        logger.info("TaskScheduler: task %s created (type=%s)", name, task_type)
        return True, task

    def task_update(self, name: str, **fields) -> tuple[bool, str | dict]:
        """Update a task's fields. Returns (ok, error) or (ok, task-dict)."""
        tasks = self._load_tasks()
        task = next((t for t in tasks if t.get("name") == name), None)
        if task is None:
            return False, f"task {name!r} not found"
        new_type = fields.get("type", task.get("type", "evolution"))
        new_project = fields.get("project")
        if new_project is None:
            cfg = task.get("config") if isinstance(task.get("config"), dict) else {}
            new_project = cfg.get("project", "")
        new_interval = fields.get("interval", task.get("interval", DEFAULT_INTERVAL))
        err = self._validate_task_fields(name, new_type, new_project, new_interval)
        if err:
            return False, err
        if "type" in fields:
            task["type"] = fields["type"]
        if "project" in fields or "repo" in fields:
            cfg = task.get("config") if isinstance(task.get("config"), dict) else {}
            if "project" in fields:
                cfg["project"] = fields["project"]
            if "repo" in fields:
                if fields["repo"]:
                    cfg["repo"] = fields["repo"]
                else:
                    cfg.pop("repo", None)
            task["config"] = cfg
        if "interval" in fields:
            task["interval"] = fields["interval"]
        if "enabled" in fields:
            task["enabled"] = bool(fields["enabled"])
        if "description" in fields:
            task["description"] = fields["description"]
        self._save_tasks(tasks)
        logger.info("TaskScheduler: task %s updated", name)
        return True, task

    def task_delete(self, name: str) -> tuple[bool, str]:
        """Delete a task by name. Returns (ok, error)."""
        tasks = self._load_tasks()
        before = len(tasks)
        tasks = [t for t in tasks if t.get("name") != name]
        if len(tasks) == before:
            return False, f"task {name!r} not found"
        self._save_tasks(tasks)
        logger.info("TaskScheduler: task %s deleted", name)
        return True, ""

    async def apply_tasks(self, tasks: list[dict]) -> dict:
        """Hot-reload tasks from a new config list (rant 2026-08-12T18:23:15 P2).

        Writes tasks.yml atomically, diffs against running handlers, and
        starts/stops/restarts handlers as needed — no daemon restart.
        Idempotent; returns {"added": [...], "removed": [...], "updated": [...]}.
        """
        self._save_tasks(tasks)
        enabled = {
            t.get("name"): t for t in tasks
            if isinstance(t, dict) and t.get("enabled", True)
        }
        current = {h.name: h for h in list(self._handlers)}
        added: list[str] = []
        removed: list[str] = []
        updated: list[str] = []
        for name, handler in list(current.items()):
            if name not in enabled:
                removed.append(name)
                self._stop_handler(handler)
        for name, cfg in enabled.items():
            if name not in current:
                added.append(name)
                await self._start_handler_async(cfg)
            else:
                old = self._handler_cfgs.get(name)
                if old is not None and _task_cfg_signature(old) != _task_cfg_signature(cfg):
                    updated.append(name)
                    self._stop_handler(current[name])
                    await self._start_handler_async(cfg)
        return {"added": added, "removed": removed, "updated": updated}

    def list_templates(self) -> list[dict]:
        """List all task types: builtin (read-only) + custom (with prompt preview).

        rant 2026-08-15T09:17:45/09:20:12：builtin 也附带 ``prompt`` 正文，
        GUI 只读 Monaco 查看器得以展示真实提示词（读失败回退文件名占位）。
        """
        result: list[dict] = []
        for name in sorted(TASK_TEMPLATES):
            prompt = ""
            try:
                p = Path(__file__).parent / TASK_TEMPLATES[name]
                if p.exists():
                    prompt = p.read_text(encoding="utf-8")
            except OSError:
                pass
            result.append({
                "name": name,
                "builtin": True,
                "template": TASK_TEMPLATES[name],
                "prompt": prompt,
            })
        for name in _custom_templates():
            result.append({
                "name": name,
                "builtin": False,
                "template": f"{name}.md",
                "prompt": _read_custom_template(name) or "",
            })
        return result

    def template_create(self, name: str, prompt: str) -> tuple[bool, str]:
        """Create a custom task type template. Returns (ok, error)."""
        if not TASK_NAME_RE.match(name) or len(name) > TASK_NAME_MAX:
            return False, f"invalid template name {name!r} (^[a-z0-9][a-z0-9-]*$, <= {TASK_NAME_MAX} chars)"
        if name in TASK_TEMPLATES:
            return False, f"builtin task type {name!r} is read-only"
        if not prompt or not prompt.strip():
            return False, "template prompt must not be empty"
        if _read_custom_template(name) is not None:
            return False, f"template {name!r} already exists"
        _write_custom_template(name, prompt)
        logger.info("TaskScheduler: custom template %s created", name)
        return True, ""

    def template_update(self, name: str, prompt: str) -> tuple[bool, str]:
        """Update a custom task type template. Returns (ok, error)."""
        if name in TASK_TEMPLATES:
            return False, f"builtin task type {name!r} is read-only"
        if _read_custom_template(name) is None:
            return False, f"template {name!r} not found"
        if not prompt or not prompt.strip():
            return False, "template prompt must not be empty"
        _write_custom_template(name, prompt)
        logger.info("TaskScheduler: custom template %s updated", name)
        return True, ""

    def template_delete(self, name: str) -> tuple[bool, str]:
        """Delete a custom task type template. Returns (ok, error).

        Refuses when tasks reference the type (host decision, rant 18:23:15).
        """
        if name in TASK_TEMPLATES:
            return False, f"builtin task type {name!r} is read-only"
        if _read_custom_template(name) is None:
            return False, f"template {name!r} not found"
        tasks = self._load_tasks()
        refs = [t.get("name") for t in tasks if t.get("type") == name]
        if refs:
            return False, (
                f"cannot delete type {name!r}: {len(refs)} task(s) use it "
                f"({', '.join(str(r) for r in refs[:5])})"
            )
        _delete_custom_template(name)
        logger.info("TaskScheduler: custom template %s deleted", name)
        return True, ""
