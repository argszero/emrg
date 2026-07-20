"""EMRG server daemon.

Mirrors the Rust emrg-server. Listens for IPC connections, processes tasks,
runs the tool-calling loop, and drives a background evolution thread.

IPC transport is abstracted by emrg.connect:
  - Unix Domain Socket on macOS/Linux
  - Named Pipe on Windows
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import signal
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from emrg.config import LlmConfig, config_dir
from emrg.connect import connect_to_server, start_server, cleanup_server
from emrg.server.llm import LlmClient
from emrg.server.tool_types import ToolResult
from emrg.memory import ProjectMemoryStore, SessionMemoryStore
from emrg.protocol import (
    EvolutionLog,
    InstanceIdentity,
    ServerPong,
    TaskRequest,
)
from emrg.session import Session

from emrg.tools import ToolRegistry
from emrg.tools.bash_tool import BashTool
from emrg.tools.read_tool import ReadTool
from emrg.tools.write_tool import WriteTool
from emrg.tools.edit_tool import EditTool
from emrg.tools.glob_tool import GlobTool
from emrg.tools.grep_tool import GrepTool
from emrg.skills.loader import build_skills_context, load_skills

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are EMRG, an evolving AI agent running as a micro-kernel daemon (emrgd). "
    "You are concise, direct, and helpful. "
    "Your host interacts with you via a TUI. "
    "You have access to tools — use them to read files, run shell commands, "
    "and make edits. When you need to see a file, use the read tool. "
    "When you need to run a command, use the bash tool. "
    "Respond helpfully and briefly.\n"
    "\n"
    "## Tool Usage\n"
    "- **read before edit**: always read a file before editing it to get exact content\n"
    "- **read with offset/limit**: use `offset` and `limit` parameters to read "
    "large files in chunks (default limit: 1000 lines)\n"
    "- **bash for exploration**: use bash to list files, run tests, check git status, "
    "and execute shell commands. Set `timeout` (default: 30s) and `workdir` "
    "to control execution.\n"
    "- **grep for content search**: use grep with regex patterns to find text "
    "across files — replaces platform-dependent 'bash grep'. "
    "Use `ignore_case`, `context_before`/`context_after`, and `glob` "
    "filtering to narrow results.\n"
    "- **glob for file discovery**: use glob with patterns like '**/*.py' to find "
    "files by name. Use `workdir` to search in a specific directory.\n"
    "- **edit for targeted changes**: prefer edit over write for existing files — "
    "it's safer and shows diffs. Set `replace_all` for multiple occurrences\n"
    "- **write for new files**: use write for creating new files or full rewrites\n"
    "- **parallel calls**: when tools are independent, invoke them in parallel "
    "for speed"
)

MEMORY_MANAGEMENT_PROMPT = (
    "## Memory Management\n"
    "\n"
    "After each response, briefly consider whether anything from this exchange "
    "should be remembered. If so, create or update a memory file in the "
    "appropriate memory directory.\n"
    "\n"
    "**Memory file format** (YAML frontmatter + Markdown body):\n"
    "```\n"
    "---\n"
    "id: a1b2c3d4\n"
    "event_at: 2026-01-15T14:30:00\n"
    "created_at: 2026-01-15T14:31:00\n"
    "updated_at: 2026-01-15T14:31:00\n"
    "type: decision\n"
    "scope: project\n"
    "status: active\n"
    "---\n\n"
    "# Title Goes Here\n\nBody content in Markdown.\n"
    "```\n"
    "- `type`: user | feedback | project | reference | decision | task\n"
    "- `scope`: session (this session only) | project (cross-session)\n"
    "- `status`: active | superseded | merged\n"
    "\n"
    "When organizing memories:\n"
    "1. **Update** before creating — check if an existing memory covers this topic\n"
    "2. **Merge** related memories — if 3+ files cover the same topic, consolidate\n"
    "3. **Split** broad memories — if a file mixes unrelated topics, split it\n"
    "4. **Clean** stale memories — if a memory is no longer relevant (task done,\n"
    "   decision changed), mark it as superseded\n"
    "\n"
    "When modifying or consolidating memories, check the timestamps to gauge\n"
    "how settled the memory likely is:\n"
    "\n"
    "- `event_at` tells you WHEN the event happened — older events are more settled\n"
    "- `updated_at` tells you when it was last changed — frequently modified files\n"
    "  are still evolving, while untouched files have likely stabilized\n"
    "- Use your judgment: a memory from yesterday may change tomorrow; a memory\n"
    "  from last month has probably stood the test of time\n"
    "- When in doubt, append rather than delete, and note what changed and why\n"
    "- If a body explicitly says \"temporary\" / \"for now\" / \"placeholder\", it's\n"
    "  safe to replace or remove when circumstances change\n"
    "\n"
    "Session-scope memories that have lasting value can be promoted to project "
    "scope by moving the file to `.emrg/memory/` and updating both MEMORY.md indexes."
)


class BackgroundThread:
    """Background evolution thread: heartbeat of EMRG life.

    Evolution cycles connect to the server via connect_to_server()
    (platform-adaptive IPC), acting as an internal client.
    """

    # ── Fixed constants ──────────────────────────────────────
    EVOLUTION_CWD = Path.home() / ".emrg" / "evolution"
    EMRG_REPO_URL = "https://github.com/argszero/emrg.git"
    OWNER = "argszero"
    REPO = "emrg"
    SOURCE_DIR = "source/emrg"  # relative to EVOLUTION_CWD
    SESSION_ID = "emrg-evolution"
    _TEMPLATE_PATH = Path(__file__).parent / "evolution_prompt.md"

    def __init__(self, identity: InstanceIdentity, interval: int = 1800) -> None:
        self.identity = identity
        self.interval = interval
        self.evolutions: list[EvolutionLog] = []
        self._running = False
        self._start_time: float | None = None
        self._logs_dir = config_dir() / "logs"
        self._projects_log = config_dir() / "projects.yml"
        self.EVOLUTION_CWD.mkdir(parents=True, exist_ok=True)

    async def run(self) -> None:
        """Run evolution cycles at configured interval (default 30 min).

        Runs all auto_evolve projects concurrently in each tick.
        Falls back to emrg self-evolution if no projects configured.
        """
        self._running = True
        self._start_time = time.time()
        seq = 0
        logger.info(
            "background thread started — evolution cycle every %ds", self.interval
        )

        while self._running:
            await asyncio.sleep(self.interval)
            seq += 1
            logger.debug("background tick #%d", seq)

            # Load auto_evolve projects and run all concurrently
            projects = self._get_auto_evolve_projects()
            if projects:
                logger.debug(
                    "evolution #%d: running %d project(s) concurrently",
                    seq, len(projects),
                )
                # Fire all projects concurrently, isolated failures
                tasks = []
                for i, project in enumerate(projects):
                    sub_seq = seq * 1000 + i  # unique seq per project
                    tasks.append(
                        self._run_evolution_cycle(sub_seq, project=project)
                    )
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        logger.warning("evolution #%d: project failed: %s", seq, r)
            else:
                try:
                    await self._run_evolution_cycle(seq, project=None)
                except Exception:
                    logger.warning("evolution #%d crashed", seq, exc_info=True)

        await self._write_final_summary()
        logger.info("background thread stopped — the heartbeat is still")

    def stop(self) -> None:
        self._running = False

    # ── Evolution cycle ──────────────────────────────────────

    async def _run_evolution_cycle(self, seq: int, project: dict | None = None) -> None:
        """Send evolution task to the server, read streaming response.

        Connects to the server as an internal client, sends a task
        with the evolution prompt, and reads responses until done.

        If project is provided, uses project-specific session_id and cwd;
        otherwise falls back to emrg self-evolution defaults.
        """
        prompt = self._build_evolution_prompt(seq, project=project)

        # Derive session_id and cwd from project config
        if project:
            session_id = f"emrg-evolution-{project.get('name', 'unknown')}"
            cwd = project.get("path", str(self.EVOLUTION_CWD))
        else:
            session_id = self.SESSION_ID
            cwd = str(self.EVOLUTION_CWD)
        logger.info(
            "evolution #%d: prompt built (%d chars), connecting to server ...",
            seq, len(prompt),
        )

        start_time = datetime.now()

        try:
            reader, writer = await connect_to_server()
            logger.info("evolution #%d: connected, sending task ...", seq)
        except (ConnectionRefusedError, FileNotFoundError) as e:
            logger.warning("evolution #%d: cannot connect to server: %s", seq, e)
            return

        task_msg = json.dumps({
            "type": "task",
            "id": f"evolution-{seq}",
            "session_id": session_id,
            "cwd": cwd,
            "prompt": prompt,
            "stream": True,
            "timestamp": start_time.isoformat(),
        }) + "\n"

        tool_count = 0
        error = None

        try:
            writer.write(task_msg.encode())
            await writer.drain()
            logger.info("evolution #%d: task sent, waiting for LLM response ...", seq)

            # Read streaming responses until done
            while True:
                line = await reader.readline()
                if not line:
                    logger.info("evolution #%d: server closed connection", seq)
                    break
                resp = json.loads(line.strip())

                if resp.get("done"):
                    duration = int(
                        (datetime.now() - start_time).total_seconds()
                    )
                    logger.info(
                        "evolution #%d complete (tools=%d, duration=%ds)",
                        seq, tool_count, duration,
                    )
                    break

                # Log tool calls for observability
                if "tool_name" in resp:
                    tool_count += 1
                    logger.info(
                        "evolution #%d tool #%d: %s (err=%s)",
                        seq, tool_count,
                        resp.get("tool_name"), resp.get("error"),
                    )

                # Capture errors from the response stream.
                # error can be a boolean (tool_end.error=True = tool failed,
                # which is normal and handled by the tool loop) or a string
                # (explicit fatal error from the server, e.g. LLM unavailable).
                # Only break on string errors — boolean tool errors are fine.
                resp_error = resp.get("error")
                if isinstance(resp_error, str):
                    error = str(resp_error)
                    logger.warning("evolution #%d server error: %s", seq, error)
                    break
        except Exception as e:
            logger.exception("evolution #%d error", seq)
            error = str(e)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

        # Write evolution log entry
        impact = [
            f"evolution-cycle-#{seq}-complete",
            f"tools-executed={tool_count}",
        ]
        if error:
            impact.append(f"error={error[:200]}")

        log = EvolutionLog(
            timestamp=start_time.isoformat(),
            trigger=f"background-cycle-#{seq}",
            impact=impact,
            operations=["llm-reflection", "tool-execution", "self-improvement"],
        )
        await self._write_evolution_log(seq, log)
        self.evolutions.append(log)

    # ── Prompt building ──────────────────────────────────────

    def _build_evolution_prompt(self, seq: int, project: dict | None = None) -> str:
        """Read evolution prompt template from source dir.

        Template: emrg/server/evolution_prompt.md
        Variables: {seq}, {instance_id}, {host_name}, {uptime},
                   {evolution_count}, {repo_url}, {evolution_cwd},
                   {owner}, {repo}, {source_dir}, {session_id}

        If project is provided, derives source_dir from the project path
        and owner/repo from the project's repo field.
        """
        template = self._TEMPLATE_PATH.read_text()
        if self._start_time is not None:
            uptime_seconds = int(time.time() - self._start_time)
        else:
            uptime_seconds = 0
        uptime = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m"

        # Derive source_dir, owner/repo, repo_url, and session_id from project
        if project:
            source_dir = project.get("path", self.SOURCE_DIR)
            local_source = source_dir  # project path is already absolute
            repo_spec = project.get("repo", "")
            if repo_spec and "/" in repo_spec:
                owner, repo = repo_spec.split("/", 1)
                repo_url = f"https://github.com/{owner}/{repo}.git"
            else:
                owner, repo = self.OWNER, self.REPO
                repo_url = self.EMRG_REPO_URL
            session_id = f"emrg-evolution-{project.get('name', 'unknown')}"
        else:
            source_dir = self.SOURCE_DIR
            local_source = str(self.EVOLUTION_CWD / self.SOURCE_DIR)
            owner, repo = self.OWNER, self.REPO
            repo_url = self.EMRG_REPO_URL
            session_id = self.SESSION_ID

        return template.format(
            seq=seq,
            instance_id=self.identity.instance_id,
            host_name=self.identity.host_name,
            uptime=uptime,
            evolution_count=len(self.evolutions),
            repo_url=repo_url,
            evolution_cwd=str(self.EVOLUTION_CWD),
            local_source=local_source,
            owner=owner,
            repo=repo,
            source_dir=source_dir,
            session_id=session_id,
        )

    # ── Project discovery ─────────────────────────────────────

    def _get_auto_evolve_projects(self) -> list[dict]:
        """Read projects.yml, return list of auto_evolve-enabled projects.

        Returns empty list if the file doesn't exist or is unreadable.
        """
        if not self._projects_log.exists():
            return []
        try:
            data = yaml.safe_load(self._projects_log.read_text())
        except (yaml.YAMLError, OSError):
            logger.warning(
                "Failed to parse %s", self._projects_log, exc_info=True
            )
            return []
        if not isinstance(data, list):
            return []
        return [
            entry for entry in data
            if isinstance(entry, dict) and entry.get("auto_evolve")
        ]

    # ── Log persistence ──────────────────────────────────────

    async def _write_evolution_log(self, seq: int, entry: EvolutionLog) -> None:
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        filename = f"evolution-{entry.timestamp.replace(':', '-')}-{seq}.json"
        path = self._logs_dir / filename
        data = {
            "timestamp": entry.timestamp,
            "trigger": entry.trigger,
            "impact": entry.impact,
            "operations": entry.operations,
        }
        path.write_text(json.dumps(data, indent=2))
        logger.debug("evolution log written: %s", path)

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
        path.write_text(json.dumps(summary, indent=2))
        logger.info("final summary written: %s", path)


class EmrgServer:
    """EMRG daemon — listens on Unix socket, processes tasks with tool calling."""

    # NDJSON safety: asyncio's readline() has a default 64KB buffer.
    # Cap lines at ~60KB to stay well under the buffer.
    _MAX_LINE_BYTES = 60 * 1024

    def __init__(self, llm_config: LlmConfig) -> None:
        runtime_dir = config_dir()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        # Ensure skills directory exists for evolution-installed skills
        (runtime_dir / "skills").mkdir(exist_ok=True)

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
        self._bg: Optional[BackgroundThread] = None
        self._max_tool_rounds = llm_config.max_tool_rounds
        self._projects_log = runtime_dir / "projects.yml"
        self._rants_log = runtime_dir / "rants.jsonl"

        # Build tool registry
        self.tools = ToolRegistry()
        self.tools.register(BashTool())
        self.tools.register(ReadTool())
        self.tools.register(WriteTool())
        self.tools.register(EditTool())
        self.tools.register(GlobTool())
        self.tools.register(GrepTool())
        logger.info("tools registered: %s", self.tools.names)

        # Load skills
        self.skills = load_skills()
        if self.skills:
            logger.info("skills loaded: %s", [s.name for s in self.skills])

    async def serve(self) -> None:
        """Start listening for IPC connections (platform-adaptive)."""
        self._running = True

        # ── PID file: prevent duplicate daemon instances ───
        runtime_dir = config_dir()
        pid_file = runtime_dir / "emrgd.pid"
        try:
            # Atomic create — fails if file already exists
            fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            logger.debug("pid file written: %s (pid=%d)", pid_file, os.getpid())
        except FileExistsError:
            # PID file exists — check if the old process is still alive
            try:
                old_pid_s = pid_file.read_text().strip()
                old_pid = int(old_pid_s)
                os.kill(old_pid, 0)
                # Old process is alive — but is its socket still there?
                sock_path = runtime_dir / "emrgd.sock"
                if not sock_path.exists():
                    # Socket gone → zombie daemon, force-kill and take over
                    logger.warning(
                        "old daemon (pid=%d) alive but socket gone — force-killing", old_pid
                    )
                    os.kill(old_pid, signal.SIGKILL)
                    try:
                        os.waitpid(old_pid, 0)
                    except ChildProcessError:
                        pass
                    pid_file.unlink()
                    fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode())
                    os.close(fd)
                    logger.debug("pid file written: %s (pid=%d)", pid_file, os.getpid())
                else:
                    logger.error(
                        "emrgd already running (pid=%d). "
                        "Stop it first or remove %s if stale.",
                        old_pid, pid_file,
                    )
                    self._running = False
                    return
            except (ValueError, OSError):
                # Stale PID file — remove and retry
                logger.warning("stale pid file (pid %s gone), removing", old_pid_s)
                pid_file.unlink()
                fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                logger.debug("pid file written: %s (pid=%d)", pid_file, os.getpid())

        self._server = await start_server(self._handle_client)
        logger.info(
            "emrgd listening | identity=%s",
            self.identity.instance_id[:8],
        )

        self._bg = BackgroundThread(self.identity, self.llm.config.evolution_interval)
        bg_task = asyncio.create_task(self._bg.run())

        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            self._bg.stop()
            bg_task.cancel()
            try:
                await bg_task
            except asyncio.CancelledError:
                pass
            await self.llm.close()
            cleanup_server()
            # Remove PID file
            try:
                if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
                    pid_file.unlink()
                    logger.debug("pid file removed: %s", pid_file)
            except OSError:
                pass

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection."""
        last_session_id: str | None = None
        last_cwd: str | None = None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as e:
                    err = json.dumps({"error": f"invalid json: {e}"}) + "\n"
                    writer.write(err.encode())
                    await writer.drain()
                    continue

                # Track session for disconnect-time consolidation
                if msg.get("session_id"):
                    last_session_id = msg["session_id"]
                if msg.get("cwd"):
                    last_cwd = msg["cwd"]
                    self._touch_project(last_cwd)

                await self._process_message(msg, writer)
        except Exception:
            logger.warning("client error", exc_info=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

            # Consolidate session memories on disconnect
            if last_session_id and last_cwd:
                try:
                    await self._consolidate_session_memories(last_session_id, Path(last_cwd))
                except Exception:
                    logger.debug("session memory consolidation failed", exc_info=True)

    async def _send(self, writer: asyncio.StreamWriter, data: dict) -> bool:
        """Write a JSON line to the client.

        Returns True on success, False if the client disconnected.
        Callers should check the return value and stop if False.
        """
        try:
            encoded = (json.dumps(data, ensure_ascii=False) + "\n").encode()
            if len(encoded) > self._MAX_LINE_BYTES:
                # Truncate large string fields to fit within NDJSON 64KB safety limit.
                # Priority: content > summary > index (most common large fields).
                for field in ("content", "summary", "index"):
                    value = data.get(field, "")
                    if isinstance(value, str) and len(value) > 100:
                        # overhead = everything except this field's string content
                        # We estimate using byte-length ratio on the field's chars
                        field_bytes = len(value.encode("utf-8"))
                        overhead = len(encoded) - field_bytes
                        max_chars = self._MAX_LINE_BYTES - overhead - 100
                        if max_chars > 200:
                            data[field] = value[:max_chars] + (
                                f"\n...[truncated {len(value) - max_chars} chars for NDJSON safety]"
                            )
                            encoded = (json.dumps(data, ensure_ascii=False) + "\n").encode()
                            if len(encoded) <= self._MAX_LINE_BYTES:
                                break
            writer.write(encoded)
            await writer.drain()
            return True
        except (ConnectionResetError, BrokenPipeError, OSError):
            logger.debug("client disconnected during send")
            return False

    # ── Project tracking ─────────────────────────────────────

    def _touch_project(self, cwd: str) -> None:
        """Record a project as active in ~/.emrg/projects.yml.

        Used by the evolution cycle to discover which projects have
        recent user activity and analyze their sessions for improvement ideas.

        Normalizes the path via realpath() so symlinked directories don't
        cause duplicate entries.

        New projects default to auto_evolve=False. Users can edit the YAML
        to set auto_evolve=True and fill in repo (owner/repo) for projects
        they want automatically evolved.
        """
        cwd = os.path.realpath(cwd)
        # Don't track the evolution engine's own workspace as a project
        evolution_cwd = str(self.EVOLUTION_CWD.resolve())
        if cwd == evolution_cwd or cwd.startswith(evolution_cwd + os.sep):
            return
        self._projects_log.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat()

        # Read existing entries (normalize by realpath to avoid duplicates)
        projects: dict[str, dict] = {}
        if self._projects_log.exists():
            try:
                data = yaml.safe_load(self._projects_log.read_text())
                if isinstance(data, list):
                    for entry in data:
                        if isinstance(entry, dict) and entry.get("path"):
                            key = os.path.realpath(entry["path"])
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
            name = os.path.basename(cwd.rstrip("/"))
            projects[cwd] = {
                "name": name,
                "path": cwd,
                "repo": "TODO: fill in owner/repo",
                "auto_evolve": False,
                "interval": 1800,
                "last_active": now,
            }
            logger.info("new project tracked: %s (auto_evolve=false)", name)

        # Build sorted YAML list
        entries = sorted(projects.values(), key=lambda e: e.get("path", ""))

        # Atomic write: write to temp file then rename to avoid corruption
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._projects_log.parent),
            prefix=".projects_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                yaml.safe_dump(
                    entries, f,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
            os.replace(tmp_path, self._projects_log)
        except OSError:
            logger.warning(
                "_touch_project: atomic write failed for %s", cwd, exc_info=True
            )
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _build_system_prompt(self, session: Session | None = None) -> str:
        """Build the system prompt, including skill context, memory, and history."""
        parts = [SYSTEM_PROMPT]

        # ── Project Context Files (CLAUDE.md / AGENTS.md / Agent.md / MANIFESTO.md) ──
        if session:
            ctx_section = self._build_project_context_section(session)
            if ctx_section:
                parts.append(ctx_section)

        if self.skills:
            skills_ctx = build_skills_context(self.skills)
            if skills_ctx:
                parts.append(skills_ctx)

        # ── Memory Section ──
        if session:
            memory_section = self._build_memory_section(session)
            if memory_section:
                parts.append(memory_section)

        # ── History Section ──
        if session:
            history_section = self._build_history_section(session)
            if history_section:
                parts.append(history_section)

        # ── Memory Management Guidance ──
        parts.append(MEMORY_MANAGEMENT_PROMPT)

        return "\n\n".join(parts)

    def _build_project_context_section(self, session: Session) -> str:
        """Read project context files from cwd.

        Claude Code reads CLAUDE.md; Codex reads AGENTS.md / Agent.md.
        EMRG also reads MANIFESTO.md — the project's design constitution.
        Including any of them provides project-specific instructions to the LLM.
        """
        candidates = ["CLAUDE.md", "AGENTS.md", "Agent.md", "MANIFESTO.md"]
        found: list[str] = []

        for name in candidates:
            path = session.cwd / name
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                    # Trim to avoid blowing up the system prompt
                    max_chars = 8000
                    if len(content) > max_chars:
                        content = content[:max_chars] + (
                            f"\n\n... [truncated {len(content) - max_chars} chars]"
                        )
                    found.append(f"### {name}\n\n{content}")
                except (OSError, UnicodeDecodeError):
                    logger.debug("failed to read %s", path, exc_info=True)

        if not found:
            return ""

        return (
            "## Project Context\n\n"
            "The following project instruction files were found in the working "
            "directory. Follow any instructions they contain. "
            "EMRG is compatible with Claude Code's CLAUDE.md, Codex's "
            "AGENTS.md/Agent.md, and MANIFESTO.md conventions.\n\n"
            + "\n\n".join(found)
        )

    def _build_memory_section(self, session: Session) -> str:
        """Build the memory section: project + session MEMORY.md indexes.

        Goal: LLM knows WHAT memories exist and WHERE to read them.
        """
        lines = ["## Memory"]

        # Project-level memories
        project_dir = session.cwd / ".emrg" / "memory"
        pindex_path = project_dir / "MEMORY.md"
        if pindex_path.exists():
            pindex = pindex_path.read_text(encoding="utf-8")
            lines.append("### Project Memory (long-term, cross-session)")
            lines.append(f"Directory: `{project_dir}/`")
            lines.append(f"Index: `{pindex_path}`")
            lines.append("")
            lines.append(pindex)
            lines.append("")

        # Session-level memories
        smem_dir = session.memory_dir
        sindex_path = smem_dir / "MEMORY.md"
        if sindex_path.exists():
            sindex = sindex_path.read_text(encoding="utf-8")
            lines.append("### Session Memory (this session only)")
            lines.append(f"Directory: `{smem_dir}/`")
            lines.append(f"Index: `{sindex_path}`")
            lines.append("")
            lines.append(sindex)
            lines.append("")

        if pindex_path.exists() or sindex_path.exists():
            lines.append(
                "**To read a memory**: use the `read` tool with the full path.\n"
                "**To create/update a memory**: use `write`/`edit` tools to write "
                "the .md file, then update MEMORY.md index.\n"
                "**To clean up**: mark stale memories as `status: superseded` "
                "rather than deleting them."
            )
        else:
            lines.append(
                "*No memories yet. Create `.emrg/memory/MEMORY.md` (project) or "
                "use this session's memory directory to start building knowledge.*"
            )

        return "\n".join(lines)

    def _build_history_section(self, session: Session) -> str:
        """Build the session history section of the system prompt.

        Goal: LLM knows WHERE the conversation history lives and HOW to look back.
        """
        today = datetime.now().strftime("%y%m%d")
        lines = [
            "## Session & History",
            f"- Session ID: `{session.session_id}`",
            f"- Session directory: `{session.dir_path}/`",
            f"- **Current history** (may be compacted): `{session.dir_path}/history.jsonl`",
            f"- **Daily full history** (never compacted): `{session.dir_path}/history_{today}.jsonl`",
            f"- Daily files are named `history_YYMMDD.jsonl`",
            f"- LLM raw log: `{session.dir_path}/llm.jsonl`",
            "",
            "**To read history**: use the `read` tool on `history.jsonl` for the current",
            "context, or on a specific `history_YYMMDD.jsonl` file for older messages.",
            "Each line is a JSON record with `type`, `role`, `content`, `timestamp` fields.",
            "Message records: `type=message`, tool calls: `type=tool_call`/`tool_result`,",
            "compacted summaries: `type=summary`.",
        ]
        return "\n".join(lines)

    async def _process_message(
        self, msg: dict, writer: asyncio.StreamWriter
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
                evolution_count=len(self._bg.evolutions) if self._bg else len(self.evolutions),
            )
            await self._send(writer, {
                "identity": pong.identity,
                "uptime_seconds": pong.uptime_seconds,
                "evolution_count": pong.evolution_count,
                "started_at": self.start_time.isoformat(),
                "pid": os.getpid(),
            })
            return

        elif msg_type == "task":
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")

            if not session_id or not cwd:
                await self._send(writer, {
                    "error": "task requires session_id and cwd",
                })
                return

            try:
                req = TaskRequest(
                    id=msg.get("id", ""),
                    session_id=session_id,
                    cwd=cwd,
                    prompt=msg.get("prompt", ""),
                    timestamp=msg.get("timestamp", ""),
                    stream=msg.get("stream", False),
                )
            except Exception as e:
                await self._send(writer, {"error": f"invalid task: {e}"})
                return

            # Load or create session
            session = self._get_or_create_session(session_id, Path(cwd))

            logger.info(
                'task received: session=%s prompt="%s" → routing via LLM (stream=%s)',
                session_id, req.prompt[:60], req.stream,
            )

            if req.stream:
                await self._run_tool_loop(req, writer, session)
            else:
                await self._run_chat_once(req, writer, session)

        elif msg_type == "compact":
            cwd = msg.get("cwd", "")
            session_id = msg.get("session_id", "")

            if not session_id or not cwd:
                await self._send(writer, {
                    "type": "compact_result",
                    "error": "compact requires session_id and cwd",
                })
                return

            session = self._get_or_create_session(session_id, Path(cwd))
            await self._handle_compact(session, writer)

        elif msg_type == "list_sessions":
            cwd = msg.get("cwd", "")
            if not cwd:
                await self._send(writer, {
                    "type": "sessions_list",
                    "error": "list_sessions requires cwd",
                })
                return
            await self._handle_list_sessions(Path(cwd), writer)

        elif msg_type == "resume_session":
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")
            if not session_id or not cwd:
                await self._send(writer, {
                    "type": "resume_result",
                    "error": "resume_session requires session_id and cwd",
                })
                return
            await self._handle_resume_session(session_id, Path(cwd), writer)

        elif msg_type == "rename_session":
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")
            title = msg.get("title", "")
            if not session_id or not cwd:
                await self._send(writer, {
                    "type": "rename_result",
                    "error": "rename requires session_id and cwd",
                })
                return

            session = self._get_or_create_session(session_id, Path(cwd))
            if not title:
                # Auto-generate title via LLM
                title = await self._generate_session_title(session)
            session.rename(title)
            await self._send(writer, {
                "type": "rename_result",
                "session_id": session_id,
                "title": title,
            })

        elif msg_type == "list_memories":
            scope = msg.get("scope", "project")
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")

            if scope == "session" and (not session_id or not cwd):
                await self._send(writer, {
                    "type": "memories_list",
                    "error": "session scope requires session_id and cwd",
                })
                return

            await self._handle_list_memories(scope, session_id, cwd, writer)

        elif msg_type == "read_memory":
            scope = msg.get("scope", "project")
            memory_id = msg.get("memory_id", "")
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")

            if not memory_id:
                await self._send(writer, {
                    "type": "memory_content",
                    "error": "read_memory requires memory_id",
                })
                return

            if scope == "session" and (not session_id or not cwd):
                await self._send(writer, {
                    "type": "memory_content",
                    "error": "session scope requires session_id and cwd",
                })
                return

            await self._handle_read_memory(scope, memory_id, session_id, cwd, writer)

        elif msg_type == "rant":
            # Store user rant/feedback for evolution analysis
            rant_message = msg.get("message", "").strip()
            if not rant_message:
                await self._send(writer, {"error": "rant requires a message"})
                return

            entry = {
                "timestamp": msg.get("timestamp", datetime.now().isoformat()),
                "message": rant_message,
            }
            # Optional project targeting (multi-project support)
            project = msg.get("project", "").strip()
            if project:
                entry["project"] = project

            self._rants_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self._rants_log, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            # Count total rants
            count = 0
            if self._rants_log.exists():
                with open(self._rants_log) as f:
                    count = sum(1 for _ in f)

            logger.info("rant recorded (%d total)%s: %s",
                count, f" project={project}" if project else "", rant_message[:100])
            await self._send(writer, {"ok": True, "count": count})

        elif msg_type == "list_projects":
            await self._handle_list_projects(writer)

        elif msg_type == "clear_session":
            session_id = msg.get("session_id", "")
            cwd = msg.get("cwd", "")
            if not session_id or not cwd:
                await self._send(writer, {
                    "type": "clear_result",
                    "error": "clear_session requires session_id and cwd",
                })
                return
            session = self._get_or_create_session(session_id, Path(cwd))
            session.clear()
            await self._send(writer, {
                "type": "clear_result",
                "session_id": session_id,
                "ok": True,
            })
            logger.info("session cleared: %s", session_id)

        elif msg_type == "shutdown":
            logger.info("shutdown requested by client")
            await self._send(writer, {"type": "shutdown_ack"})
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._server.close()

        else:
            await self._send(writer, {
                "error": "unknown message type",
                "received": msg_type,
            })

    def _get_or_create_session(self, session_id: str, cwd: Path) -> Session:
        """Load an existing session or create a new one."""
        session_dir = cwd / ".emrg" / "sessions" / session_id
        if session_dir.exists() and (session_dir / "meta.json").exists():
            return Session.load(session_id, cwd)
        return Session.create_with_id(session_id, cwd)

    async def _run_chat_once(
        self, req: TaskRequest, writer: asyncio.StreamWriter, session: Session
    ) -> None:
        """Non-streaming single-turn chat (no tool loop)."""
        system_prompt = self._build_system_prompt(session)
        history_messages = session.get_messages_for_llm()
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            *history_messages,
            {"role": "user", "content": req.prompt},
        ]
        tools = self.tools.to_openai_tools()

        # Persist user message
        session.append_message({
            "type": "message",
            "role": "user",
            "content": req.prompt,
        })

        try:
            msg = await self.llm.chat(messages, tools=tools)
            content = msg.get("content", "")

            # Log LLM request/response
            session.append_llm({
                "type": "request",
                "model": self.llm.config.model,
                "messages": messages,
                "tools": tools,
            })
            session.append_llm({
                "type": "response",
                "content": content,
                "tool_calls": msg.get("tool_calls"),
                "finish_reason": msg.get("finish_reason", "stop"),
            })

            # Persist assistant message
            session.append_message({
                "type": "message",
                "role": "assistant",
                "content": content or "",
            })

            if not await self._send(writer, {
                "request_id": req.id,
                "content": content or "",
                "done": True,
                "delta": False,
                "session_id": session.session_id,
            }):
                return  # client disconnected

            # Fire-and-forget: reflect on whether to save memories
            self._maybe_reflect_memory(session, req.prompt, content or "")
        except Exception as e:
            logger.exception("LLM error")
            await self._send(writer, {
                "error": f"LLM error: {e}. Check config at ~/.emrg/config.toml",
            })

    async def _run_tool_loop(
        self, req: TaskRequest, writer: asyncio.StreamWriter, session: Session
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
        """
        system_prompt = self._build_system_prompt(session)
        history_messages = session.get_messages_for_llm()

        # Persist user message
        session.append_message({
            "type": "message",
            "role": "user",
            "content": req.prompt,
        })

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            *history_messages,
            {"role": "user", "content": req.prompt},
        ]
        tools_openai = self.tools.to_openai_tools()

        # For LLM logging: collect the full message exchange
        llm_request_messages = [dict(m) for m in messages]

        for round_num in range(1, self._max_tool_rounds + 1):
            logger.debug("tool loop round %d: %d messages, %d tools",
                         round_num, len(messages), len(tools_openai))

            # Auto-compact: if token count exceeds threshold, compact before this round
            if round_num > 1 and self.llm.config.auto_compact_threshold > 0.0:
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
                    # Notify client
                    await self._send(writer, {
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
                        await self._send(writer, {
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
            tc_by_index: dict[int, dict] = {}
            final_finish = None
            final_usage: dict | None = None

            try:
                async for delta in self.llm.chat_stream(messages, tools=tools_openai):
                    c = delta.get("content")
                    if c:
                        content_parts.append(c)
                        if not await self._send(writer, {
                            "request_id": req.id,
                            "content": c,
                            "done": False,
                            "delta": True,
                            "session_id": session.session_id,
                        }):
                            return  # client disconnected

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
            except Exception as e:
                logger.exception("LLM stream error in round %d", round_num)
                await self._send(writer, {
                    "error": f"LLM error: {e}. Check config at ~/.emrg/config.toml",
                })
                # Send done so the client knows the stream is over.
                # Without this, the client stays in its read loop → deadlock.
                await self._send(writer, {
                    "done": True,
                    "request_id": req.id,
                })
                return

            full_content = "".join(content_parts)
            logger.debug("round %d finish: %s, tool_calls=%d, content_len=%d",
                         round_num, final_finish, len(tc_by_index), len(full_content))

            # Case 1: Final text answer — no more tool calls
            if final_finish == "stop" or (final_finish and not tc_by_index):
                # Log LLM response
                session.append_llm({
                    "type": "request",
                    "model": self.llm.config.model,
                    "messages": [dict(m) for m in messages],
                    "tools": tools_openai,
                })
                session.append_llm({
                    "type": "response",
                    "content": full_content,
                    "finish_reason": final_finish,
                    "usage": final_usage,
                })

                # Persist assistant message
                session.append_message({
                    "type": "message",
                    "role": "assistant",
                    "content": full_content,
                })

                if not await self._send(writer, {
                    "request_id": req.id,
                    "content": "",
                    "done": True,
                    "delta": False,
                    "session_id": session.session_id,
                }):
                    return  # client disconnected

                # Fire-and-forget: reflect on whether to save memories
                self._maybe_reflect_memory(session, req.prompt, full_content)
                return

            # Case 2: LLM wants to call tools
            if tc_by_index or final_finish == "tool_calls":
                tool_calls = [tc_by_index[i] for i in sorted(tc_by_index.keys())]

                # Log LLM request/response for this tool-call round
                session.append_llm({
                    "type": "request",
                    "model": self.llm.config.model,
                    "messages": [dict(m) for m in messages],
                    "tools": tools_openai,
                })
                session.append_llm({
                    "type": "response",
                    "content": full_content,
                    "tool_calls": [
                        {"id": tc.get("id", ""), "type": "function",
                         "function": {"name": tc.get("function", {}).get("name", ""),
                                      "arguments": tc.get("function", {}).get("arguments", "")}}
                        for tc in tool_calls
                    ],
                    "finish_reason": final_finish,
                    "usage": final_usage,
                })

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

                    logger.info("tool call: %s(%s)", tc_name,
                                json.dumps(args, ensure_ascii=False)[:200])

                    # Notify client (best-effort — fail means client is gone)
                    if not await self._send(writer, {
                        "type": "tool_start",
                        "request_id": req.id,
                        "tool_name": tc_name,
                        "tool_call_id": tc_id,
                        "arguments": args,
                    }):
                        return  # client disconnected

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

                    # Notify client of result (best-effort)
                    if not await self._send(writer, {
                        "type": "tool_end",
                        "request_id": req.id,
                        "tool_name": tc_name,
                        "tool_call_id": tc_id,
                        "content": result.content,
                        "error": result.error,
                    }):
                        return  # client disconnected

                # Log LLM request for this round (before continuing)
                continue

            # Case 3: Max tokens or other stop — done
            session.append_llm({
                "type": "request",
                "model": self.llm.config.model,
                "messages": messages,
                "tools": tools_openai,
            })
            session.append_llm({
                "type": "response",
                "content": full_content,
                "finish_reason": final_finish,
                "usage": final_usage,
            })

            session.append_message({
                "type": "message",
                "role": "assistant",
                "content": full_content,
            })

            if not await self._send(writer, {
                "request_id": req.id,
                "content": full_content or "",
                "done": True,
                "delta": False,
                "session_id": session.session_id,
            }):
                return  # client disconnected

            # Fire-and-forget: reflect on whether to save memories
            self._maybe_reflect_memory(session, req.prompt, full_content)
            return

        # Exceeded max tool rounds
        logger.warning("max tool rounds (%d) exceeded for task %s",
                       self._max_tool_rounds, req.id)
        if not await self._send(writer, {
            "request_id": req.id,
            "content": f"Exceeded maximum tool call rounds ({self._max_tool_rounds}).",
            "done": True,
            "delta": False,
            "session_id": session.session_id,
        }):
            return  # client disconnected

        # Fire-and-forget: reflect on whether to save memories
        self._maybe_reflect_memory(session, req.prompt, full_content)

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
                parts.append(f"[{ts}] {r['role']}: {r.get('content', '')}")
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
        self, session: Session, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a compact request: summarize history via LLM and replace old messages."""
        records = session._read_history()
        if len(records) <= 5:
            await self._send(writer, {
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
                    await self._send(writer, {
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

        await self._send(writer, {
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
        })
        session.append_llm({
            "type": "response",
            "content": summary,
            "compact": True,
        })
        return summary

    async def _chunked_compact(
        self, records: list[dict], keep_recent: int = 5
    ) -> str:
        """Token-aware chunked compact for over-context-window histories.

        Uses greedy bin-packing by estimated token count to shard records
        into chunks that each fit within the LLM context window, then
        recursively merges the per-chunk summaries.
        """
        ctx_window = self.llm.config.context_window
        max_tokens = self.llm.config.max_tokens
        max_per_chunk = ctx_window - max_tokens - 2000  # room for prompt + output
        merge_batch = max_per_chunk

        to_compact = records[:-keep_recent]
        total_tokens = sum(self._estimate_single(r) for r in to_compact)
        logger.info(
            "chunked compact: %d records, ~%d tokens, max_per_chunk=%d",
            len(to_compact), total_tokens, max_per_chunk,
        )

        # Step 1: Greedy token-aware sharding
        chunks: list[list[dict]] = []
        current_chunk: list[dict] = []
        current_tokens = 0

        for record in to_compact:
            rec_tokens = self._estimate_single(record)
            # Single record too large → truncate
            if rec_tokens > max_per_chunk:
                record = self._truncate_record(record, max_per_chunk)
                rec_tokens = self._estimate_single(record)
            if current_tokens + rec_tokens > max_per_chunk and current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_tokens = 0
            current_chunk.append(record)
            current_tokens += rec_tokens
        if current_chunk:
            chunks.append(current_chunk)

        logger.info("chunked compact: %d chunks created", len(chunks))

        # Step 2: Summarize each chunk
        summaries: list[str] = []
        for idx, chunk in enumerate(chunks):
            chunk_text = self._records_to_text(chunk)
            msg = await self.llm.chat([{
                "role": "user",
                "content": (
                    f"Summarize this conversation segment ({idx + 1}/{len(chunks)}). "
                    "Include key decisions, context, and unresolved items:\n\n"
                    f"{chunk_text}"
                ),
            }], tools=None)
            summaries.append(msg.get("content", ""))
            logger.debug("chunked compact: chunk %d/%d done", idx + 1, len(chunks))

        if len(summaries) == 1:
            return summaries[0]

        # Step 3: Recursively merge summaries
        return await self._merge_summaries(summaries, max_per_chunk, merge_batch)

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
        self, cwd: Path, writer: asyncio.StreamWriter
    ) -> None:
        """List all sessions for the given cwd."""
        sessions = Session.list_sessions(cwd)
        await self._send(writer, {
            "type": "sessions_list",
            "sessions": sessions,
        })

    async def _handle_list_projects(
        self, writer: asyncio.StreamWriter
    ) -> None:
        """Read projects.yml and return all project entries."""
        evolution_cwd = str(self.EVOLUTION_CWD.resolve())
        projects: list[dict] = []
        try:
            if self._projects_log.exists():
                data = yaml.safe_load(self._projects_log.read_text())
                if isinstance(data, list):
                    projects = [
                        {"name": p.get("name", ""), "repo": p.get("repo", ""),
                         "path": p.get("path", ""), "auto_evolve": p.get("auto_evolve", False)}
                        for p in data
                        if isinstance(p, dict)
                        and not str(p.get("path", "")).startswith(evolution_cwd)
                    ]
        except (yaml.YAMLError, OSError) as e:
            logger.exception("Failed to read projects.yml")
        await self._send(writer, {
            "type": "projects_list",
            "projects": projects,
        })

    async def _handle_resume_session(
        self, session_id: str, cwd: Path, writer: asyncio.StreamWriter
    ) -> None:
        """Validate a session exists and return metadata.

        The client reads history.jsonl directly from disk for display.
        We only confirm the session exists — no records over the wire.
        """
        session_dir = cwd / ".emrg" / "sessions" / session_id
        if not session_dir.exists():
            await self._send(writer, {
                "type": "resume_result",
                "session_id": session_id,
                "error": f"Session {session_id} not found",
            })
            return

        session = Session.load(session_id, cwd)

        await self._send(writer, {
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

    async def _handle_list_memories(
        self, scope: str, session_id: str, cwd: str, writer: asyncio.StreamWriter
    ) -> None:
        """List memories: return the MEMORY.md index and memory directory info."""
        if scope == "project":
            cwd_path = Path(cwd) if cwd else Path.cwd()
            store = ProjectMemoryStore(cwd_path)
            index_path = store.index_path
        else:
            session_dir = Path(cwd) / ".emrg" / "sessions" / session_id
            if not session_dir.exists():
                await self._send(writer, {
                    "type": "memories_list",
                    "error": f"Session {session_id} not found",
                })
                return
            store = SessionMemoryStore(session_dir)
            index_path = store.index_path

        memories = store.list()
        index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""

        await self._send(writer, {
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
        writer: asyncio.StreamWriter,
    ) -> None:
        """Read a specific memory file by id and return its full content."""
        if scope == "project":
            cwd_path = Path(cwd) if cwd else Path.cwd()
            store = ProjectMemoryStore(cwd_path)
        else:
            session_dir = Path(cwd) / ".emrg" / "sessions" / session_id
            if not session_dir.exists():
                await self._send(writer, {
                    "type": "memory_content",
                    "error": f"Session {session_id} not found",
                })
                return
            store = SessionMemoryStore(session_dir)

        mem = store.get(memory_id)
        if mem is None:
            await self._send(writer, {
                "type": "memory_content",
                "error": f"Memory not found: {memory_id}",
            })
            return

        await self._send(writer, {
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
        except Exception as e:
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
                        logger.debug("memory reflection tool: %s → %s", tc_name, result_text[:100])

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
                    logger.debug("consolidation tool: %s → %s", tc_name, result_text[:100])
        except Exception:
            logger.debug("memory consolidation failed", exc_info=True)


async def run_server(llm_config: LlmConfig) -> None:
    """Run the EMRG server until interrupted."""
    server = EmrgServer(llm_config)
    try:
        await server.serve()
    except KeyboardInterrupt:
        logger.info("shutdown signal received")
