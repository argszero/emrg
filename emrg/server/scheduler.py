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
import subprocess
import time
from datetime import datetime
from pathlib import Path
import yaml

from emrg.config import config_dir
from emrg.connect import connect_to_server
from websockets.exceptions import ConnectionClosed
from emrg.protocol import EvolutionLog, InstanceIdentity
from emrg.server.atomic import atomic_write_yaml
from emrg.server.git_utils import _detect_git_remote, resolve_git_gh

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


# ── EvolutionHandler ────────────────────────────────────────────


class EvolutionHandler:
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

        # ── Saturation halt — stop burning tokens on empty cycles ───
        # Track consecutive cycles where git HEAD didn't advance (NTE).
        # After _IDLE_HALT_THRESHOLD empty cycles, switch to trigger-only:
        #   - Scheduled runs are skipped
        #   - Only manual trigger (/trigger) resumes the cycle
        #   - Counter resets on trigger or when git HEAD advances
        #
        # Counter is persisted to disk to survive daemon restarts.
        self._IDLE_HALT_THRESHOLD = 30
        self._saturation_dir = config_dir() / "saturation"
        self._saturation_dir.mkdir(parents=True, exist_ok=True)
        self._saturation_file = self._saturation_dir / f"{self.name}.json"
        self._empty_cycles = self._load_saturation_state()

        # Resolve project path from config (new schema) or fall back to
        # config.path for backward-compat with old tasks.yml entries.
        project_name = config.get("project", "")
        self._project_name = project_name
        path = _resolve_project_path(project_name) if project_name else config.get("path", "")
        self.project_path = path or name  # default to name for emrg itself

        # Derive owner/repo/git from path
        repo_spec = _detect_git_remote(path) if path else ""
        if repo_spec and "/" in repo_spec:
            self._owner, self._repo = repo_spec.split("/", 1)
            self._repo_url = f"https://github.com/{self._owner}/{self._repo}.git"
        else:
            self._owner = self.OWNER
            self._repo = self.REPO
            self._repo_url = self.EMRG_REPO_URL
        self._session_id = f"emrg-evolution-{name}"
        self._source_dir = path or name

    def _get_git_head(self) -> str | None:
        """Return current git HEAD hash, or None if not a git repo."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self._source_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _load_saturation_state(self) -> int:
        """Restore _empty_cycles counter from disk (survives daemon restarts)."""
        try:
            if self._saturation_file.exists():
                data = json.loads(self._saturation_file.read_text(encoding="utf-8"))
                count = data.get("empty_cycles", 0)
                if count > 0:
                    logger.debug(
                        "EvolutionHandler[%s]: restored saturation state (%d empty cycles)",
                        self.name, count,
                    )
                return count
        except Exception:
            pass
        return 0

    def _save_saturation_state(self) -> None:
        """Persist _empty_cycles counter to disk."""
        try:
            self._saturation_file.write_text(
                json.dumps({"empty_cycles": self._empty_cycles}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    async def run(self) -> None:
        """Run evolution cycles at configured interval.

        Uses an asyncio.Event for interruptible sleep — manual triggers
        via trigger() wake the coroutine immediately.
        """
        self._running = True
        self._start_time = time.time()
        logger.info(
            "EvolutionHandler[%s] started — every %ds", self.name, self.interval
        )

        while self._running:
            # Wait for interval or manual trigger (interruptible)
            self._next_run_at = time.time() + self.interval
            manual_trigger = False
            try:
                await asyncio.wait_for(
                    self._trigger_event.wait(),
                    timeout=self.interval,
                )
                # Manual trigger fired — clear and proceed
                self._trigger_event.clear()
                manual_trigger = True
                logger.debug(
                    "EvolutionHandler[%s] manually triggered", self.name
                )
            except asyncio.TimeoutError:
                # Normal scheduled run
                pass

            # Saturation halt: if too many empty cycles, skip scheduled runs.
            # Manual triggers always bypass the halt and reset the counter.
            if manual_trigger:
                if self._empty_cycles >= self._IDLE_HALT_THRESHOLD:
                    logger.info(
                        "EvolutionHandler[%s]: resumed via manual trigger "
                        "(was halted at %d empty cycles)",
                        self.name, self._empty_cycles,
                    )
                self._empty_cycles = 0
                self._save_saturation_state()
            elif self._empty_cycles >= self._IDLE_HALT_THRESHOLD:
                logger.info(
                    "EvolutionHandler[%s]: saturation halt — "
                    "skipping scheduled run (%d empty cycles). "
                    "Use /trigger to resume.",
                    self.name, self._empty_cycles,
                )
                continue

            logger.debug("EvolutionHandler[%s] tick", self.name)
            self._cycle_running = True
            self._next_run_at = None  # running — no next time yet
            try:
                await self._run_evolution_cycle()
            except Exception:
                logger.warning(
                    "EvolutionHandler[%s] crashed", self.name, exc_info=True
                )
            finally:
                self._cycle_running = False
                self._trigger_event.clear()  # clear any spurious set during cycle

        await self._write_final_summary()
        logger.info("EvolutionHandler[%s] stopped", self.name)

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
        return {
            "name": self.name,
            "running": self._cycle_running,
            "next_run_in_seconds": remaining,
            "interval": self.interval,
        }

    async def _run_evolution_cycle(self) -> None:
        """Connect to server, send evolution prompt, read streaming response."""

        cycle_time = datetime.now()
        prompt = self._build_evolution_prompt()
        logger.info(
            "EvolutionHandler[%s]: prompt built (%d chars), connecting ...",
            self.name, len(prompt),
        )
        start_time = cycle_time

        # Track git HEAD to detect empty (NTE) cycles
        git_head_before = self._get_git_head()

        try:
            ws = await connect_to_server()
            logger.info("EvolutionHandler[%s]: connected", self.name)
        except (ConnectionRefusedError, FileNotFoundError) as e:
            logger.warning(
                "EvolutionHandler[%s]: cannot connect: %s", self.name, e
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

        try:
            await ws.send(task_msg)

            while True:
                try:
                    resp = json.loads(await ws.recv())
                except ConnectionClosed:
                    break
                if resp.get("done"):
                    duration = int((datetime.now() - cycle_time).total_seconds())
                    logger.info(
                        "EvolutionHandler[%s] complete (tools=%d, duration=%ds)",
                        self.name, tool_count, duration,
                    )
                    break

                if "tool_name" in resp:
                    tool_count += 1

                resp_error = resp.get("error")
                if isinstance(resp_error, str):
                    error = str(resp_error)
                    logger.warning(
                        "EvolutionHandler[%s] server error: %s",
                        self.name, error,
                    )
                    break
        except Exception as e:
            logger.exception("EvolutionHandler[%s] error", self.name)
            error = str(e)
        finally:
            try:
                await ws.close()
            except Exception:
                pass

        # Detect empty cycles: git HEAD unchanged → no work was done
        git_head_after = self._get_git_head()
        if git_head_before and git_head_after and git_head_before == git_head_after:
            self._empty_cycles += 1
            self._save_saturation_state()
            logger.debug(
                "EvolutionHandler[%s]: empty cycle #%d (HEAD=%s)",
                self.name, self._empty_cycles, git_head_after[:8],
            )
        else:
            if self._empty_cycles > 0:
                logger.info(
                    "EvolutionHandler[%s]: git HEAD changed, resetting empty streak",
                    self.name,
                )
            self._empty_cycles = 0
            self._save_saturation_state()

        cycle_ts = cycle_time.isoformat()
        impact = [
            f"evolution-cycle-{cycle_ts}-complete",
            f"tools-executed={tool_count}",
        ]
        if error:
            impact.append(f"error={error[:200]}")

        log = EvolutionLog(
            timestamp=cycle_ts,
            trigger=f"evolution-{self.name}-{cycle_ts}",
            impact=impact,
            operations=["llm-reflection", "tool-execution", "self-improvement"],
        )
        await self._write_evolution_log(log)
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

    async def _write_evolution_log(self, entry: EvolutionLog) -> None:
        filename = f"evolution-{entry.timestamp.replace(':', '-')}.json"
        path = self._logs_dir / filename
        data = {
            "timestamp": entry.timestamp,
            "trigger": entry.trigger,
            "impact": entry.impact,
            "operations": entry.operations,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        # Rotate: keep at most 27 evolution log files (oldest deleted).
        # Filenames use ISO timestamps so lexical sort = chronological.
        _MAX_LOG_FILES = 27
        try:
            log_files = sorted([
                f for f in self._logs_dir.iterdir()
                if f.is_file() and f.name.startswith("evolution-")
            ])
            if len(log_files) > _MAX_LOG_FILES:
                for old in log_files[:len(log_files) - _MAX_LOG_FILES]:
                    old.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort cleanup

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
        "evolution": EvolutionHandler,
        "paper": EvolutionHandler,  # same handler, different template
        "open-source": EvolutionHandler,  # same handler, different template
        "promote": EvolutionHandler,  # same handler, different template
    }

    def __init__(self, identity: InstanceIdentity) -> None:
        self.identity = identity
        self._tasks_file = config_dir() / "tasks.yml"
        self._handlers: list[EvolutionHandler] = []
        self._coros: list[asyncio.Task] = []

    def load_and_start(self) -> list[asyncio.Task]:
        """Load tasks.yml, start all enabled tasks, return coroutine list."""
        tasks_config = self._load_tasks()
        if not tasks_config:
            # Bootstrap: if projects.yml has auto_evolve entries but
            # tasks.yml is empty, migrate them.
            self._migrate_from_projects()

        tasks_config = self._load_tasks()
        for cfg in tasks_config:
            if not cfg.get("enabled", True):
                continue
            handler_cls = self.HANDLERS.get(cfg["type"])
            if handler_cls is None:
                logger.warning(
                    "TaskScheduler: unknown type %r for task %r",
                    cfg["type"], cfg["name"],
                )
                continue
            template_name = TASK_TEMPLATES.get(cfg["type"], "evolution_prompt.md")
            template_path = Path(__file__).parent / template_name
            handler = handler_cls(
                name=cfg["name"],
                config=cfg.get("config", {}),
                interval=cfg.get("interval", 1800),
                identity=self.identity,
                template_path=template_path,
            )
            self._handlers.append(handler)
            coro = asyncio.create_task(handler.run())
            self._coros.append(coro)
            logger.info(
                "TaskScheduler: started %s[%s] every %ds",
                cfg["type"], cfg["name"], cfg.get("interval", 1800),
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
        """Return status for all running handlers."""
        return [handler.status() for handler in self._handlers]

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
