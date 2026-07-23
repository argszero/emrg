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
import time
from datetime import datetime
from pathlib import Path
import yaml

from emrg.config import config_dir
from emrg.connect import connect_to_server
from emrg.framing import read_frame, write_frame
from emrg.protocol import EvolutionLog, InstanceIdentity
from emrg.server.atomic import atomic_write_yaml
from emrg.server.git_utils import _detect_git_remote

logger = logging.getLogger("emrg.server.scheduler")

# ── Module-level constants (shared with daemon) ──────────────────
EVOLUTION_CWD = Path.home() / ".emrg" / "evolution"


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


# ── EvolutionHandler ────────────────────────────────────────────


class EvolutionHandler:
    """Self-contained evolution loop for one project (or emrg itself).

    Each handler runs its own while+sleep(interval) coroutine,
    independent of all other handlers.
    """

    EMRG_REPO_URL = "https://github.com/argszero/emrg.git"
    OWNER = "argszero"
    REPO = "emrg"
    _TEMPLATE_PATH = Path(__file__).parent / "evolution_prompt.md"

    def __init__(
        self,
        name: str,
        config: dict,
        interval: int,
        identity: InstanceIdentity,
    ) -> None:
        self.name = name
        self._config = config
        self.interval = interval
        self.identity = identity
        self._running = False
        self._start_time: float | None = None
        self._logs_dir = config_dir() / "logs"
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self.evolutions: list[EvolutionLog] = []

        # Resolve project path from config (new schema) or fall back to
        # config.path for backward-compat with old tasks.yml entries.
        project_name = config.get("project", "")
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

        # Steady-state detection: skip cycles when nothing has changed.
        # Tracks git HEAD and rant count to avoid wasting LLM tokens on
        # NTE cycles when the repository is quiescent (MANIFESTO §4, §12).
        self._last_git_head: str | None = None
        self._last_rant_count: int = -1
        self._consecutive_skips: int = 0

    async def run(self) -> None:
        """Run evolution cycles at configured interval."""
        self._running = True
        self._start_time = time.time()
        seq = 0
        logger.info(
            "EvolutionHandler[%s] started — every %ds", self.name, self.interval
        )

        while self._running:
            await asyncio.sleep(self.interval)
            seq += 1
            logger.debug("EvolutionHandler[%s] tick #%d", self.name, seq)
            try:
                await self._run_evolution_cycle(seq)
            except Exception:
                logger.warning(
                    "EvolutionHandler[%s] #%d crashed", self.name, seq, exc_info=True
                )

        await self._write_final_summary()
        logger.info("EvolutionHandler[%s] stopped", self.name)

    def stop(self) -> None:
        self._running = False

    async def _run_evolution_cycle(self, seq: int) -> None:
        """Connect to server, send evolution prompt, read streaming response."""

        # ── Steady-state stimulus check ──────────────────────────
        # Skip the cycle if git HEAD and rant count are unchanged
        # since the last run — no new code, no new feedback = nothing to evolve.
        # First cycle (seq==1) always runs.
        if seq > 1:
            has_stimulus, stimulus_detail = await self._stimulus_check()
            if not has_stimulus:
                self._consecutive_skips += 1
                logger.info(
                    "EvolutionHandler[%s] #%d SKIP (steady state #%d): %s",
                    self.name, seq, self._consecutive_skips, stimulus_detail,
                )
                return
            if self._consecutive_skips > 0:
                logger.info(
                    "EvolutionHandler[%s] #%d RESUMING after %d skips: %s",
                    self.name, seq, self._consecutive_skips, stimulus_detail,
                )
                self._consecutive_skips = 0

        prompt = self._build_evolution_prompt(seq)
        logger.info(
            "EvolutionHandler[%s] #%d: prompt built (%d chars), connecting ...",
            self.name, seq, len(prompt),
        )
        start_time = datetime.now()

        try:
            reader, writer = await connect_to_server()
            logger.info("EvolutionHandler[%s] #%d: connected", self.name, seq)
        except (ConnectionRefusedError, FileNotFoundError) as e:
            logger.warning(
                "EvolutionHandler[%s] #%d: cannot connect: %s", self.name, seq, e
            )
            return

        task_msg = json.dumps(
            {
                "type": "task",
                "id": f"evolution-{seq}",
                "session_id": self._session_id,
                "cwd": self._source_dir,
                "prompt": prompt,
                "stream": True,
                "timestamp": start_time.isoformat(),
            },
            ensure_ascii=False,
        )

        tool_count = 0
        error = None

        try:
            task_bytes = task_msg.encode()
            await write_frame(writer, task_bytes)

            while True:
                frame = await read_frame(reader)
                if frame is None:
                    break
                resp = json.loads(frame.decode())

                if resp.get("done"):
                    duration = int((datetime.now() - start_time).total_seconds())
                    logger.info(
                        "EvolutionHandler[%s] #%d complete (tools=%d, duration=%ds)",
                        self.name, seq, tool_count, duration,
                    )
                    break

                if "tool_name" in resp:
                    tool_count += 1

                resp_error = resp.get("error")
                if isinstance(resp_error, str):
                    error = str(resp_error)
                    logger.warning(
                        "EvolutionHandler[%s] #%d server error: %s",
                        self.name, seq, error,
                    )
                    break
        except Exception as e:
            logger.exception("EvolutionHandler[%s] #%d error", self.name, seq)
            error = str(e)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

        impact = [
            f"evolution-cycle-#{seq}-complete",
            f"tools-executed={tool_count}",
        ]
        if error:
            impact.append(f"error={error[:200]}")

        log = EvolutionLog(
            timestamp=start_time.isoformat(),
            trigger=f"evolution-{self.name}-#{seq}",
            impact=impact,
            operations=["llm-reflection", "tool-execution", "self-improvement"],
        )
        await self._write_evolution_log(seq, log)
        self.evolutions.append(log)

    async def _stimulus_check(self) -> tuple[bool, str]:
        """Check if there are new stimuli worth running an evolution cycle for.

        Returns (has_stimulus, detail_string).

        A stimulus is:
        - A new git commit (HEAD changed since last check)
        - A new uncompleted rant for this project

        If neither has changed, the repository is in steady state and
        the evolution cycle should be skipped to conserve resources.
        """
        import subprocess

        details: list[str] = []
        has_stimulus = False

        # ── Git HEAD check ───────────────────────────────────────
        source_dir = self._source_dir
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=source_dir,
            )
            stdout, _ = await proc.communicate()
            current_head = stdout.decode().strip() if stdout else ""
        except Exception:
            current_head = ""

        if current_head and current_head != self._last_git_head:
            has_stimulus = True
            details.append(f"HEAD {current_head[:8]}")
            self._last_git_head = current_head
        elif current_head:
            details.append("HEAD unchanged")
        else:
            details.append("HEAD unknown (skipping git check)")

        # ── Rant check ───────────────────────────────────────────
        rants_file = config_dir() / "rants.jsonl"
        project_name = self._config.get("project", self.name)
        uncompleted = 0
        if rants_file.exists():
            try:
                for line in rants_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rant = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rant.get("project") != project_name:
                        continue
                    if "completed" not in rant:
                        uncompleted += 1
            except OSError:
                pass

        if uncompleted != self._last_rant_count:
            has_stimulus = True
            details.append(f"rants {self._last_rant_count}→{uncompleted}")
            self._last_rant_count = uncompleted
        else:
            details.append(f"rants={uncompleted} (unchanged)")

        detail = "; ".join(details)
        return has_stimulus, detail

    def _build_evolution_prompt(self, seq: int) -> str:
        """Build evolution prompt from template."""
        template = self._TEMPLATE_PATH.read_text(encoding="utf-8")
        if self._start_time is not None:
            uptime_seconds = int(time.time() - self._start_time)
        else:
            uptime_seconds = 0
        uptime = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m"

        return template.format(
            seq=seq,
            instance_id=self.identity.instance_id,
            host_name=self.identity.host_name,
            uptime=uptime,
            evolution_count=len(self.evolutions),
            repo_url=self._repo_url,
            evolution_cwd=str(EVOLUTION_CWD),
            local_source=self._source_dir,
            owner=self._owner,
            repo=self._repo,
            source_dir=self._source_dir,
            session_id=self._session_id,
        )

    async def _write_evolution_log(self, seq: int, entry: EvolutionLog) -> None:
        filename = f"evolution-{entry.timestamp.replace(':', '-')}-{seq}.json"
        path = self._logs_dir / filename
        data = {
            "timestamp": entry.timestamp,
            "trigger": entry.trigger,
            "impact": entry.impact,
            "operations": entry.operations,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

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
        path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))


# ── TaskScheduler ────────────────────────────────────────────────


class TaskScheduler:
    """Manages tasks from ~/.emrg/tasks.yml.

    Each enabled task gets an independent asyncio coroutine.
    The scheduler only creates/monitors tasks; handlers are self-contained.
    """

    HANDLERS: dict[str, type] = {
        "evolution": EvolutionHandler,
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
            handler = handler_cls(
                name=cfg["name"],
                config=cfg.get("config", {}),
                interval=cfg.get("interval", 1800),
                identity=self.identity,
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
