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
import subprocess
import time
from datetime import datetime
from pathlib import Path
import yaml

from emrg._win import win32_no_window_kwargs
from emrg.config import config_dir
from emrg.connect import connect_to_server
from websockets.exceptions import ConnectionClosed
from emrg.protocol import EvolutionLog, InstanceIdentity
from emrg.server.atomic import atomic_write_yaml
from emrg.server.git_utils import (
    INSTALL_INFO,
    _detect_git_remote,
    git_origin_url,
    https_to_ssh_url,
    is_git_connection_error,
    no_prompt_env,
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
        # Track consecutive cycles where git HEAD didn't advance (NTE).
        # After _IDLE_HALT_THRESHOLD empty cycles, switch to low-frequency
        # heartbeat full cycles instead of the old complete halt:
        #   - Scheduled runs continue at heartbeat interval (never skipped)
        #   - heartbeat = max(interval, min(interval*8, 8h)) — 60s task → 8min
        #   - Manual trigger (/trigger) or upstream git HEAD advance restores
        #     the normal frequency immediately (counter reset to 0)
        #
        # Counter is persisted to disk to survive daemon restarts.
        self._IDLE_HALT_THRESHOLD = 30
        # G129: 连续连接失败告警阈值——达到后升级为 ERROR（防静默吞掉，
        # rant 2026-08-09T08:03:46：GUI 测试覆盖真实 emrgd.port 致 10h 连不上）。
        self._CONNECT_FAIL_ALERT = 3
        self._connect_failures = 0
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

        # Derive owner/repo/git from config override → path git remote → defaults.
        # _repo_configured gates the workspace self-heal (rant 2026-08-12T18:14:46):
        # any task with a real repo (config owner/repo, or a git remote in its
        # project path) gets clone/align self-heal; the emrg evolution task
        # always counts as configured (defaults to argszero/emrg).
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
        # One-shot https-origin probe per handler lifetime (see
        # _ensure_origin_reachable) — avoids re-probing every cycle.
        self._origin_probed = False

    def _get_git_head(self) -> str | None:
        """Return current git HEAD hash, or None if not a git repo."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self._source_dir,
                capture_output=True,
                text=True,
                timeout=5,
                env=no_prompt_env(),
                **win32_no_window_kwargs(),
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    # ── Evolution workspace self-heal (rant 2026-08-06T20:42:05, 方案 C) ──
    #
    # Packaged installs run the daemon from ~/.emrg/install/source/emrg — a
    # .git-less source snapshot — so evolution cannot commit/push/PR. Each
    # cycle starts by ensuring the workspace is a usable git repo:
    #   - dev machine (source_dir is a real git repo) → untouched
    #   - otherwise → clone EMRG into ~/.emrg/evolution/emrg/, align it to the
    #     installed release tag, and self-heal projects.yml/tasks.yml entries.
    # Idempotent and failure-tolerant (no network → skip cycle, GUI unaffected).

    def _repo_url_from_install_info(self) -> str | None:
        """Read the repo URL from install-info.json 'repo' field, if present."""
        try:
            data = json.loads(INSTALL_INFO.read_text(encoding="utf-8"))
            value = data.get("repo")
            return str(value) if value else None
        except (OSError, json.JSONDecodeError, AttributeError):
            return None

    def _is_usable_git_repo(self, path: str) -> bool:
        """True if path is a git repo with a working tree we can commit to."""
        if not path or not Path(path).is_dir():
            return False
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                env=no_prompt_env(),
                **win32_no_window_kwargs(),
            )
            if result.returncode != 0 or result.stdout.strip() != "true":
                return False
            return os.access(path, os.W_OK)
        except (subprocess.SubprocessError, OSError):
            return False

    def _ensure_git_identity(self, repo_dir: Path) -> None:
        """Set git user.name/user.email if missing (fresh clones have none)."""
        name = os.environ.get("GIT_AUTHOR_NAME", "") or "EMRG Evolution"
        email = os.environ.get("GIT_AUTHOR_EMAIL", "") or "emrg@argszero.dev"
        try:
            for key, default in (("user.name", name), ("user.email", email)):
                result = subprocess.run(
                    ["git", "config", key],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=5,
                    env=no_prompt_env(),
                    **win32_no_window_kwargs(),
                )
                if not result.stdout.strip():
                    subprocess.run(
                        ["git", "config", key, default],
                        cwd=repo_dir,
                        capture_output=True,
                        timeout=5,
                        env=no_prompt_env(),
                        **win32_no_window_kwargs(),
                    )
        except (subprocess.SubprocessError, OSError):
            pass

    def _align_to_installed_version(self, repo_dir: Path) -> None:
        """Point the local master branch at the installed release tag.

        Reads ~/.emrg/install/version.txt (e.g. "0.2.7"); checks out
        ``v0.2.7`` if the tag exists, otherwise stays on the clone's
        default branch (latest master). A named branch (not detached HEAD)
        keeps the evolution flow (branch-from-master, push, PR) working.
        """
        tag = None
        try:
            version_file = Path.home() / ".emrg" / "install" / "version.txt"
            if version_file.exists():
                ver = version_file.read_text(encoding="utf-8").strip()
                if ver:
                    tag = f"v{ver}"
        except OSError:
            tag = None
        if not tag:
            return
        try:
            result = subprocess.run(
                ["git", "tag", "-l", tag],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                env=no_prompt_env(),
                **win32_no_window_kwargs(),
            )
            if result.returncode == 0 and tag in result.stdout.split():
                subprocess.run(
                    ["git", "checkout", "-B", "master", tag],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                    check=True,
                    env=no_prompt_env(),
                    **win32_no_window_kwargs(),
                )
                logger.info(
                    "TaskHandler[%s]: evolution workspace aligned to %s",
                    self.name, tag,
                )
        except (subprocess.CalledProcessError, OSError) as e:
            logger.warning(
                "TaskHandler[%s]: tag checkout %s failed (stay on master): %s",
                self.name, tag, e,
            )

    def _ensure_project_entry(self) -> None:
        """Add/update the emrg project entry in projects.yml (idempotent)."""
        projects_file = config_dir() / "projects.yml"
        try:
            entries: list[dict] = []
            if projects_file.exists():
                data = yaml.safe_load(projects_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    entries = [e for e in data if isinstance(e, dict)]
            new_path = str(self._source_dir)
            for entry in entries:
                if entry.get("name") == "emrg":
                    if entry.get("path") != new_path:
                        entry["path"] = new_path
                        entry["last_active"] = datetime.now().isoformat()
                        atomic_write_yaml(entries, projects_file, prefix=".projects_")
                        logger.info(
                            "TaskHandler[%s]: projects.yml self-heal — emrg → %s",
                            self.name, new_path,
                        )
                    return
            entries.append({
                "name": "emrg",
                "path": new_path,
                "last_active": datetime.now().isoformat(),
            })
            atomic_write_yaml(entries, projects_file, prefix=".projects_")
            logger.info(
                "TaskHandler[%s]: projects.yml self-heal — added emrg → %s",
                self.name, new_path,
            )
        except (yaml.YAMLError, OSError) as e:
            logger.warning(
                "TaskHandler[%s]: projects.yml self-heal failed: %s",
                self.name, e,
            )

    def _ensure_evolution_workspace(self) -> bool:
        """Self-heal the task workspace; returns False to skip the cycle.

        Applies to any task that has a repo configured (the emrg evolution
        task, or a paper/open-source/promote task with config owner/repo or
        a git remote in its project path — rant 2026-08-12T18:14:46).
        Returns True when the workspace is usable (existing dev repo, or a
        successful clone into ``~/.emrg/evolution/<repo>/``).
        """
        if not self._repo_configured:
            return True  # no repo configured for this task — nothing to self-heal
        if self._is_usable_git_repo(self._source_dir):
            self._ensure_origin_reachable()
            return True  # dev machine — use the existing repo as-is
        repo_url = self._repo_url_from_install_info() or self._repo_url
        evolve_dir = EVOLUTION_CWD / self._repo
        if evolve_dir.exists():
            if self._is_usable_git_repo(str(evolve_dir)):
                self._source_dir = str(evolve_dir)
                self.project_path = str(evolve_dir)
                self._ensure_origin_reachable()
                return True
            logger.warning(
                "TaskHandler[%s]: %s exists but is not a git repo — "
                "skipping self-heal to avoid data loss",
                self.name, evolve_dir,
            )
            return False
        try:
            logger.info(
                "TaskHandler[%s]: cloning %s → %s (workspace self-heal)",
                self.name, repo_url, evolve_dir,
            )
            self._clone_workspace(repo_url, evolve_dir)
            self._align_to_installed_version(evolve_dir)
            self._ensure_git_identity(evolve_dir)
            self._source_dir = str(evolve_dir)
            self.project_path = str(evolve_dir)
            self._ensure_project_entry()
            return True
        except (subprocess.CalledProcessError, OSError) as e:
            logger.warning(
                "TaskHandler[%s]: evolution workspace self-heal failed "
                "(network down?): %s — skipping cycle",
                self.name, e,
            )
            return False

    def _clone_workspace(self, repo_url: str, target: Path) -> None:
        """Clone the evolution repo, retrying via SSH when https is blocked.

        Uses a short ``http.connectTimeout`` so a blocked github.com:443
        fails fast (seconds) instead of hanging; on a connection-type
        failure the clone is retried with the SSH URL
        (``git@github.com:owner/repo.git``), which works on networks that
        block https git transport (observed on the packaged host). Other
        failures (auth / 404 / repo-specific) propagate unchanged.
        """
        cmd = ["git", "-c", "http.connectTimeout=10", "clone", repo_url, str(target)]
        reason = ""
        try:
            subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                timeout=120, check=True, env=no_prompt_env(),
                **win32_no_window_kwargs(),
            )
            return
        except subprocess.CalledProcessError as e:
            ssh_url = https_to_ssh_url(repo_url)
            if not ssh_url or not is_git_connection_error(e.stderr or ""):
                raise
            # NB: `e` is deleted when the except block exits — capture first.
            reason = (e.stderr.strip() or str(e))[:80]
        logger.warning(
            "TaskHandler[%s]: https clone failed (%s) — retrying via SSH",
            self.name, reason,
        )
        subprocess.run(
            ["git", "clone", ssh_url, str(target)],
            capture_output=True, text=True, encoding="utf-8",
            timeout=120, check=True, env=no_prompt_env(),
            **win32_no_window_kwargs(),
        )

    def _ensure_origin_reachable(self) -> None:
        """Probe the github.com https origin; switch to SSH when blocked.

        Some networks block github.com:443 while SSH port 22 stays open.
        With an https origin every evolution pull/push hangs ~75 s and the
        saturation-halt auto-resume (``git ls-remote``) never fires,
        silently starving the cycle. One cheap probe per handler lifetime
        (bounded by ``http.connectTimeout``) detects the blocked case; on
        success nothing changes; on a connection-type failure the origin is
        switched to the equivalent SSH URL so pull/push/ls-remote keep
        working. Auth/404 errors never trigger a switch.
        """
        if self._origin_probed:
            return
        self._origin_probed = True
        origin = git_origin_url(self._source_dir)
        ssh_url = https_to_ssh_url(origin)
        if not ssh_url:
            return  # not a github.com https origin — nothing to switch
        result = subprocess.run(
            ["git", "-c", "http.connectTimeout=4", "ls-remote", origin, "HEAD"],
            cwd=self._source_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            env=no_prompt_env(),
            **win32_no_window_kwargs(),
        )
        if result.returncode == 0:
            return  # reachable — keep https
        if not is_git_connection_error(result.stderr):
            return  # auth/404 etc — switching would not help
        switch = subprocess.run(
            ["git", "remote", "set-url", "origin", ssh_url],
            cwd=self._source_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            env=no_prompt_env(),
            **win32_no_window_kwargs(),
        )
        if switch.returncode == 0:
            logger.warning(
                "TaskHandler[%s]: https origin unreachable (%s) — "
                "switched origin to %s",
                self.name, (result.stderr.strip() or "")[:80], ssh_url,
            )

    def _load_saturation_state(self) -> int:
        """Restore _empty_cycles counter from disk (survives daemon restarts)."""
        try:
            if self._saturation_file.exists():
                data = json.loads(self._saturation_file.read_text(encoding="utf-8"))
                count = data.get("empty_cycles", 0)
                if count > 0:
                    logger.debug(
                        "TaskHandler[%s]: restored saturation state (%d empty cycles)",
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
                logger.debug(
                    "TaskHandler[%s] manually triggered", self.name
                )
            except asyncio.TimeoutError:
                # Normal scheduled run
                pass

            # Manual triggers always reset the saturation counter; otherwise
            # saturated ticks keep running full cycles at heartbeat cadence.
            if manual_trigger:
                if self._empty_cycles >= self._IDLE_HALT_THRESHOLD:
                    logger.info(
                        "TaskHandler[%s]: resumed via manual trigger "
                        "(was in saturation at %d empty cycles)",
                        self.name, self._empty_cycles,
                    )
                self._empty_cycles = 0
                self._save_saturation_state()

            logger.debug("TaskHandler[%s] tick", self.name)
            self._cycle_running = True
            self._next_run_at = None  # running — no next time yet
            try:
                await self._run_evolution_cycle()
            except Exception:
                logger.warning(
                    "TaskHandler[%s] crashed", self.name, exc_info=True
                )
            finally:
                self._cycle_running = False
                self._trigger_event.clear()  # clear any spurious set during cycle

        await self._write_final_summary()
        logger.info("TaskHandler[%s] stopped", self.name)

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

    def _remote_advanced(self) -> bool:
        """True if origin/master differs from the local HEAD (new upstream work).

        A saturation-halted handler never runs scheduled cycles, so it can
        never detect a HEAD change on its own — only a manual /trigger
        could resume it. If every instance halted during an idle stretch,
        new upstream work (PRs/commits from other instances or the host)
        would go unnoticed indefinitely. This cheap check (one ``git
        ls-remote`` — no fetch, no working-tree mutation) lets the halt
        auto-resume on genuine upstream activity.
        """
        try:
            local = self._get_git_head()
            if not local:
                return False
            result = subprocess.run(
                ["git", "-c", "http.connectTimeout=4", "ls-remote", "origin", "master"],
                cwd=self._source_dir,
                capture_output=True,
                text=True,
                timeout=15,
                env=no_prompt_env(),
                **win32_no_window_kwargs(),
            )
            if result.returncode != 0:
                # https github.com may be blocked while SSH port 22 works —
                # retry with the SSH form of the origin before giving up.
                ssh_url = https_to_ssh_url(git_origin_url(self._source_dir))
                if ssh_url and is_git_connection_error(result.stderr):
                    result = subprocess.run(
                        ["git", "ls-remote", ssh_url, "master"],
                        cwd=self._source_dir,
                        capture_output=True,
                        text=True,
                        timeout=15,
                        env=no_prompt_env(),
                        **win32_no_window_kwargs(),
                    )
            if result.returncode != 0:
                return False
            remote = result.stdout.strip().split()
            return bool(remote) and remote[0] != local
        except Exception:
            return False

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
        cycles, just at a reduced cadence — never skipping. Upstream advance
        auto-resumes (counter reset, normal frequency), so a saturated handler
        does not miss new work forever.
        """
        if self._empty_cycles < self._IDLE_HALT_THRESHOLD:
            return False
        if self._remote_advanced():
            logger.info(
                "TaskHandler[%s]: upstream advanced — resuming normal frequency from saturation",
                self.name,
            )
            self._empty_cycles = 0
            self._save_saturation_state()
            return False
        logger.info(
            "TaskHandler[%s]: saturation (%d empty cycles) — "
            "running full cycle at heartbeat interval (%ds) — never halting",
            self.name, self._empty_cycles, self._heartbeat_interval(),
        )
        return True

    async def _run_evolution_cycle(self) -> None:

        # Self-heal the evolution workspace first (rant 20:42 方案 C):
        # packaged installs lack a writable git repo; clone on demand.
        if not self._ensure_evolution_workspace():
            logger.warning(
                "TaskHandler[%s]: workspace not ready — skipping cycle",
                self.name,
            )
            return

        cycle_time = datetime.now()
        prompt = self._build_evolution_prompt()
        logger.info(
            "TaskHandler[%s]: prompt built (%d chars), connecting ...",
            self.name, len(prompt),
        )
        start_time = cycle_time

        # Track git HEAD to detect empty (NTE) cycles
        git_head_before = self._get_git_head()

        try:
            ws = await connect_to_server()
            logger.info("TaskHandler[%s]: connected", self.name)
            self._connect_failures = 0
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            # G129 (rant 2026-08-09T08:03:46): 连接失败不得静默吞掉——GUI 测试曾把
            # 假 port 值写进真实 ~/.emrg/emrgd.port，导致演化周期 10 小时连不上
            # daemon（WinError 1225）只留下 WARNING。累计失败达到阈值后升级为
            # ERROR 告警，提示 port 文件可能被外部覆盖（检查 ~/.emrg/emrgd.port）。
            self._connect_failures += 1
            port_path = config_dir() / "emrgd.port"
            if self._connect_failures >= self._CONNECT_FAIL_ALERT:
                logger.error(
                    "TaskHandler[%s]: cannot connect for %d consecutive cycles "
                    "(%s) — daemon unreachable. Check %s (may have been overwritten "
                    "by GUI tests or stale after daemon restart); run 'emrg server' "
                    "or restart the daemon to recover.",
                    self.name, self._connect_failures, e, port_path,
                )
            else:
                logger.warning(
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
                    truncated = "exceeded" in content.lower()
                    if truncated:
                        logger.warning(
                            "TaskHandler[%s] truncated (max tool rounds, tools=%d, duration=%ds)",
                            self.name, tool_count, duration,
                        )
                    else:
                        logger.info(
                            "TaskHandler[%s] complete (tools=%d, duration=%ds)",
                            self.name, tool_count, duration,
                        )
                    break

                if "tool_name" in resp:
                    tool_count += 1

                resp_error = resp.get("error")
                if isinstance(resp_error, str):
                    error = str(resp_error)
                    logger.warning(
                        "TaskHandler[%s] server error: %s",
                        self.name, error,
                    )
                    break
        except Exception as e:
            logger.exception("TaskHandler[%s] error", self.name)
            error = str(e)
        finally:
            try:
                await ws.close()
            except Exception:
                pass

        # Detect empty cycles: git HEAD unchanged → no work was done.
        # A truncated cycle is NOT empty — the agent wanted to work but hit
        # the tool-round cap; counting it would wrongly back off the handler.
        # An aborted cycle (server error like "session busy", or an exception)
        # is NOT empty either — the agent was blocked before reaching an NTE
        # conclusion; counting it would also advance the idle-halt backoff.
        git_head_after = self._get_git_head()
        if (
            not error
            and not truncated
            and git_head_before and git_head_after
            and git_head_before == git_head_after
        ):
            self._empty_cycles += 1
            self._save_saturation_state()
            logger.debug(
                "TaskHandler[%s]: empty cycle #%d (HEAD=%s)",
                self.name, self._empty_cycles, git_head_after[:8],
            )
        else:
            if self._empty_cycles > 0:
                reason = "truncated cycle" if truncated else "git HEAD changed"
                if error:
                    reason = f"aborted cycle ({error[:80]})"
                logger.info(
                    "TaskHandler[%s]: %s, resetting empty streak",
                    self.name, reason,
                )
            self._empty_cycles = 0
            self._save_saturation_state()

        # Aborted cycles are not evolutions: no log file, no count. Writing
        # them would inflate the evolution count (GUI growth card / toast,
        # evolution_summary) with work that never happened — the "session
        # busy" aborts observed when interactive sessions hold the daemon.
        if error:
            logger.warning(
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
        "evolution": TaskHandler,
        "paper": TaskHandler,  # same handler, different template
        "open-source": TaskHandler,  # same handler, different template
        "promote": TaskHandler,  # same handler, different template
    }

    def __init__(self, identity: InstanceIdentity) -> None:
        self.identity = identity
        self._tasks_file = config_dir() / "tasks.yml"
        self._handlers: list[TaskHandler] = []
        self._coros: list[asyncio.Task] = []
        # cfg (from tasks.yml) used to start each handler — for hot-reload diffing.
        self._handler_cfgs: dict[str, dict] = {}

    def _start_handler_for(self, cfg: dict) -> TaskHandler:
        """Create + start a handler for a task cfg; returns the handler."""
        template_path = _resolve_task_template(cfg["type"])
        handler = TaskHandler(
            name=cfg["name"],
            config=cfg.get("config", {}),
            interval=cfg.get("interval", DEFAULT_INTERVAL),
            identity=self.identity,
            template_path=template_path,
        )
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
        """Return status for all running handlers."""
        return [handler.status() for handler in self._handlers]

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
        the emrg-task entry. Without it, no TaskHandler is ever created,
        so the workspace self-heal (which lives inside the handler) cannot run.

        The projects.yml emrg entry is ensured here too (rant 02:58): the only
        other writer (_ensure_evolution_workspace's clone branch) requires a
        first tick + network. If projects.yml lacks the entry,
        _resolve_project_path("emrg") returns None and the handler's
        _source_dir degenerates to the relative string "emrg" (dangling cwd).
        The path is fixed to ~/.emrg/evolution/emrg; an existing entry is
        preserved as-is (dev machines may configure a custom path).
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
                self._start_handler_for(cfg)
            else:
                old = self._handler_cfgs.get(name)
                if old is not None and _task_cfg_signature(old) != _task_cfg_signature(cfg):
                    updated.append(name)
                    self._stop_handler(current[name])
                    self._start_handler_for(cfg)
        return {"added": added, "removed": removed, "updated": updated}

    def list_templates(self) -> list[dict]:
        """List all task types: builtin (read-only) + custom (with prompt preview)."""
        result: list[dict] = []
        for name in sorted(TASK_TEMPLATES):
            result.append({
                "name": name,
                "builtin": True,
                "template": TASK_TEMPLATES[name],
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
