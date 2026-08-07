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
from emrg.server.git_utils import (
    INSTALL_INFO,
    _detect_git_remote,
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
                env=no_prompt_env(),
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
                )
                if not result.stdout.strip():
                    subprocess.run(
                        ["git", "config", key, default],
                        cwd=repo_dir,
                        capture_output=True,
                        timeout=5,
                        env=no_prompt_env(),
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
                )
                logger.info(
                    "EvolutionHandler[%s]: evolution workspace aligned to %s",
                    self.name, tag,
                )
        except (subprocess.CalledProcessError, OSError) as e:
            logger.warning(
                "EvolutionHandler[%s]: tag checkout %s failed (stay on master): %s",
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
                            "EvolutionHandler[%s]: projects.yml self-heal — emrg → %s",
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
                "EvolutionHandler[%s]: projects.yml self-heal — added emrg → %s",
                self.name, new_path,
            )
        except (yaml.YAMLError, OSError) as e:
            logger.warning(
                "EvolutionHandler[%s]: projects.yml self-heal failed: %s",
                self.name, e,
            )

    def _ensure_evolution_workspace(self) -> bool:
        """Self-heal the evolution workspace; returns False to skip the cycle.

        Only applies to the EMRG self-evolution task (config.project == emrg).
        Returns True when the workspace is usable (existing dev repo, or a
        successful clone into ``~/.emrg/evolution/emrg/``).
        """
        if self._project_name != "emrg" or self._repo != self.REPO:
            return True  # paper/open-source/promote tasks: not our concern
        if self._is_usable_git_repo(self._source_dir):
            return True  # dev machine — use the existing repo as-is
        repo_url = self._repo_url_from_install_info() or self._repo_url
        evolve_dir = EVOLUTION_CWD / self.REPO
        if evolve_dir.exists():
            if self._is_usable_git_repo(str(evolve_dir)):
                self._source_dir = str(evolve_dir)
                self.project_path = str(evolve_dir)
                return True
            logger.warning(
                "EvolutionHandler[%s]: %s exists but is not a git repo — "
                "skipping self-heal to avoid data loss",
                self.name, evolve_dir,
            )
            return False
        try:
            logger.info(
                "EvolutionHandler[%s]: cloning %s → %s (workspace self-heal)",
                self.name, repo_url, evolve_dir,
            )
            subprocess.run(
                ["git", "clone", repo_url, str(evolve_dir)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
                check=True,
                env=no_prompt_env(),
            )
            self._align_to_installed_version(evolve_dir)
            self._ensure_git_identity(evolve_dir)
            self._source_dir = str(evolve_dir)
            self.project_path = str(evolve_dir)
            self._ensure_project_entry()
            return True
        except (subprocess.CalledProcessError, OSError) as e:
            logger.warning(
                "EvolutionHandler[%s]: evolution workspace self-heal failed "
                "(network down?): %s — skipping cycle",
                self.name, e,
            )
            return False

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
            elif self._saturation_halt_active():
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
                ["git", "ls-remote", "origin", "master"],
                cwd=self._source_dir,
                capture_output=True,
                text=True,
                timeout=10,
                env=no_prompt_env(),
            )
            if result.returncode != 0:
                return False
            remote = result.stdout.strip().split()
            return bool(remote) and remote[0] != local
        except Exception:
            return False

    def _saturation_halt_active(self) -> bool:
        """Whether a scheduled tick should be skipped due to saturation halt.

        Extracted from the run loop so the halt decision is testable:
        at/above the threshold the tick is skipped UNLESS the upstream
        remote advanced (auto-resume: reset the counter and run the cycle,
        so a halted handler does not miss new work forever).
        """
        if self._empty_cycles < self._IDLE_HALT_THRESHOLD:
            return False
        if self._remote_advanced():
            logger.info(
                "EvolutionHandler[%s]: upstream advanced — resuming from saturation halt",
                self.name,
            )
            self._empty_cycles = 0
            self._save_saturation_state()
            return False
        logger.info(
            "EvolutionHandler[%s]: saturation halt — "
            "skipping scheduled run (%d empty cycles). "
            "Use /trigger to resume.",
            self.name, self._empty_cycles,
        )
        return True

    async def _run_evolution_cycle(self) -> None:

        # Self-heal the evolution workspace first (rant 20:42 方案 C):
        # packaged installs lack a writable git repo; clone on demand.
        if not self._ensure_evolution_workspace():
            logger.warning(
                "EvolutionHandler[%s]: workspace not ready — skipping cycle",
                self.name,
            )
            return

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
                            "EvolutionHandler[%s] truncated (max tool rounds, tools=%d, duration=%ds)",
                            self.name, tool_count, duration,
                        )
                    else:
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
                "EvolutionHandler[%s]: empty cycle #%d (HEAD=%s)",
                self.name, self._empty_cycles, git_head_after[:8],
            )
        else:
            if self._empty_cycles > 0:
                reason = "truncated cycle" if truncated else "git HEAD changed"
                if error:
                    reason = f"aborted cycle ({error[:80]})"
                logger.info(
                    "EvolutionHandler[%s]: %s, resetting empty streak",
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
                "EvolutionHandler[%s]: cycle aborted (%s) — not counted as evolution",
                self.name, error[:200],
            )
            return

        cycle_ts = cycle_time.isoformat()
        impact = [
            f"evolution-cycle-{cycle_ts}-{'truncated' if truncated else 'complete'}",
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
        # Self-heal: packaged installs may lack the emrg self-evolution task.
        # This must run here (not inside the handler) because a missing task
        # means no EvolutionHandler is ever started (rant 20:42 方案 C).
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

    def _ensure_self_evolution_task(self) -> None:
        """Ensure projects.yml has an emrg entry and tasks.yml has the task.

        Packaged installs (or first runs) may lack tasks.yml entirely, or lack
        the emrg-task entry. Without it, no EvolutionHandler is ever created,
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
