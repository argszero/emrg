"""EMRG client — TUI interface (python-tui inlined).
Keeps interactive_demo.py's input handling, renders chat in viewport.
"""

from __future__ import annotations

import asyncio, json, logging, os, signal, subprocess, sys, time
from datetime import datetime
from pathlib import Path, PurePath
from rich.cells import cell_len
from rich.style import Style
from emrg.client.python_tui import ChatRow, Diff, InputParser, StatusLine, Terminal, ToolCard
from emrg.client.python_tui.widgets.base import Line, Span, Widget
from emrg.client.python_tui.widgets.markdown import StreamingMarkdown
from emrg.connect import connect_to_server, cleanup_server, is_server_running_sync, get_server_path
from emrg.framing import read_frame, write_frame, encode_frame
from emrg.protocol import TaskRequest, TaskResponse, ToolEnd, ToolStart
from emrg.session import generate_session_id
from emrg.skills.loader import load_skills

logger = logging.getLogger(__name__)


class InputWidget(Widget):
    def __init__(self) -> None:
        self.text = ""; self.cursor = 0; self._dirty = True
    def insert(self, ch): self.text = self.text[:self.cursor] + ch + self.text[self.cursor:]; self.cursor += len(ch); self._dirty = True
    def backspace(self):
        if self.cursor > 0: self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]; self.cursor -= 1; self._dirty = True
    def delete_forward(self):
        if self.cursor < len(self.text): self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]; self._dirty = True
    def delete_word_backward(self):
        if self.cursor == 0: return
        i = self.cursor - 1
        while i >= 0 and self.text[i].isspace(): i -= 1
        while i >= 0 and not self.text[i].isspace(): i -= 1
        self.text = self.text[:i + 1] + self.text[self.cursor:]; self.cursor = i + 1; self._dirty = True
    def delete_to_beginning_of_line(self):
        n = self.text[:self.cursor].rfind("\n")
        start = n + 1 if n >= 0 else 0
        self.text = self.text[:start] + self.text[self.cursor:]
        self.cursor = start; self._dirty = True
    def delete_to_end_of_line(self):
        n = self.text[self.cursor:].find("\n")
        end = self.cursor + n if n >= 0 else len(self.text)
        self.text = self.text[:self.cursor] + self.text[end:]; self._dirty = True
    def move_left(self):
        if self.cursor > 0: self.cursor -= 1; self._dirty = True
    def move_right(self):
        if self.cursor < len(self.text): self.cursor += 1; self._dirty = True
    def move_word_left(self):
        if self.cursor == 0: return
        i = self.cursor - 1
        while i >= 0 and self.text[i].isspace(): i -= 1
        while i >= 0 and not self.text[i].isspace(): i -= 1
        self.cursor = i + 1; self._dirty = True
    def move_word_right(self):
        if self.cursor >= len(self.text): return
        i = self.cursor
        while i < len(self.text) and not self.text[i].isspace(): i += 1
        while i < len(self.text) and self.text[i].isspace(): i += 1
        self.cursor = i; self._dirty = True
    def _visual_rows(self, available: int):
        """Return list of (start, end) for each visual row, accounting for line-wrapping.

        Uses cell_len for CJK-aware display-width measurement.
        Each Chinese/CJK character occupies 2 terminal columns.
        """
        rows = []
        pos = 0
        for line in self.text.split("\n"):
            if not line:
                rows.append((pos, pos)); pos += 1; continue
            line_pos = 0
            while line_pos < len(line):
                end = line_pos; w = 0
                while end < len(line):
                    cw = cell_len(line[end])
                    if w + cw > available:
                        break
                    w += cw; end += 1
                if end == line_pos:
                    end = line_pos + 1
                rows.append((pos + line_pos, pos + end))
                line_pos = end
            pos += len(line) + 1
        if not rows: rows = [(0, 0)]
        return rows

    def _cursor_vrow(self, available: int) -> int:
        rows = self._visual_rows(available)
        for i, (s, e) in enumerate(rows):
            if s <= self.cursor <= e: return i
        return len(rows) - 1

    @staticmethod
    def _visual_offset_in_row(text: str, start: int, cursor: int) -> int:
        """Display-width column offset of cursor within text[start:cursor]."""
        col = 0
        for i in range(start, cursor):
            col += cell_len(text[i])
        return col

    @staticmethod
    def _cursor_at_visual_offset(text: str, start: int, end: int, target_col: int) -> int:
        """Character position in text[start:end] nearest to target_col display columns."""
        col = 0
        for i in range(start, end):
            cw = cell_len(text[i])
            if col + cw > target_col:
                return i  # target_col falls within this char; clamp to its start
            col += cw
        return end

    def move_up(self, available: int = 0):
        if available <= 0:
            before = self.text[:self.cursor].split("\n")
            if len(before) < 2: return
            prev = before[-2]; col = min(len(before[-1]), len(prev))
            self.cursor = sum(len(l) + 1 for l in before[:-2]) + col; self._dirty = True
        else:
            rows = self._visual_rows(available); vrow = self._cursor_vrow(available)
            if vrow <= 0: return
            prev_s, prev_e = rows[vrow - 1]
            cur_s = rows[vrow][0]
            vis_col = self._visual_offset_in_row(self.text, cur_s, self.cursor)
            self.cursor = self._cursor_at_visual_offset(self.text, prev_s, prev_e, vis_col)
            self._dirty = True

    def move_down(self, available: int = 0):
        if available <= 0:
            after = self.text[self.cursor:].split("\n")
            if len(after) < 2: return
            before = self.text[:self.cursor].split("\n"); col = min(len(before[-1]), len(after[1]))
            self.cursor = self.cursor + len(after[0]) + 1 + col; self._dirty = True
        else:
            rows = self._visual_rows(available); vrow = self._cursor_vrow(available)
            if vrow >= len(rows) - 1: return
            next_s, next_e = rows[vrow + 1]
            cur_s = rows[vrow][0]
            vis_col = self._visual_offset_in_row(self.text, cur_s, self.cursor)
            self.cursor = self._cursor_at_visual_offset(self.text, next_s, next_e, vis_col)
            self._dirty = True
    def move_home(self):
        n = self.text[:self.cursor].rfind("\n"); self.cursor = n + 1 if n >= 0 else 0; self._dirty = True
    def move_end(self):
        n = self.text[self.cursor:].find("\n"); self.cursor += n if n >= 0 else len(self.text) - self.cursor; self._dirty = True
    @property
    def dirty(self): return self._dirty
    @dirty.setter
    def dirty(self, v): self._dirty = v
    def render(self, ctx):
        pstyle = Style.parse("bold cyan"); sep_style = Style.parse("dim")
        lines = [Line(spans=[Span("─" * ctx.width, style=sep_style)], style=ctx.style)]
        raw = self.text.split("\n") if self.text else [""]
        prompt = "> "; prompt_w = len(prompt)
        available = max(1, ctx.width - prompt_w)

        cr = None; cc = None; off = 0
        for i, rl in enumerate(raw):
            if off <= self.cursor <= off + len(rl): cr = i; cc = self.cursor - off
            off += len(rl) + 1

        for ri, txt in enumerate(raw):
            if not txt:
                if ri == cr:
                    lines.append(Line(spans=[
                        Span(prompt, style=pstyle),
                        Span(" ", style=Style(reverse=True)),
                    ], style=ctx.style))
                else:
                    lines.append(Line(spans=[
                        Span(prompt, style=pstyle),
                    ], style=ctx.style))
                continue

            # Split by display width, not character count (CJK-aware)
            pos = 0
            while pos < len(txt):
                end = pos; w = 0
                while end < len(txt):
                    cw = cell_len(txt[end])
                    if w + cw > available:
                        break
                    w += cw; end += 1
                if end == pos:
                    end = pos + 1
                chunk = txt[pos:end]
                chunk_end = end

                if ri == cr:
                    c = cc or 0
                    if pos <= c < chunk_end:
                        local = c - pos
                        lines.append(Line(spans=[
                            Span(prompt, style=pstyle),
                            Span(chunk[:local], style=ctx.style),
                            Span(chunk[local], style=Style(reverse=True)),
                            Span(chunk[local+1:], style=ctx.style),
                        ], style=ctx.style))
                    elif c == chunk_end and chunk_end == len(txt):
                        lines.append(Line(spans=[
                            Span(prompt, style=pstyle),
                            Span(chunk, style=ctx.style),
                            Span(" ", style=Style(reverse=True)),
                        ], style=ctx.style))
                    else:
                        lines.append(Line(spans=[
                            Span(prompt, style=pstyle),
                            Span(chunk, style=ctx.style),
                        ], style=ctx.style))
                else:
                    lines.append(Line(spans=[
                        Span(prompt, style=pstyle),
                        Span(chunk, style=ctx.style),
                    ], style=ctx.style))

                pos = end

        lines.append(Line(spans=[Span("─" * ctx.width, style=sep_style)], style=ctx.style))
        self._dirty = False; return lines


class SessionSelector(Widget):
    """Interactive session picker — arrow-key navigation with highlight.

    Renders a list of sessions with the selected one in reverse video.
    Used by /resume when invoked without arguments.
    """

    def __init__(self, sessions: list[dict] | None = None):
        self.sessions: list[dict] = sessions or []
        self.selected_index: int = 0
        self._dirty: bool = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def move_up(self) -> None:
        if self.selected_index > 0:
            self.selected_index -= 1
            self._dirty = True

    def move_down(self) -> None:
        if self.selected_index < len(self.sessions) - 1:
            self.selected_index += 1
            self._dirty = True

    @property
    def selected_session_id(self) -> str | None:
        if 0 <= self.selected_index < len(self.sessions):
            return self.sessions[self.selected_index].get("session_id", "")
        return None

    def render(self, ctx):
        lines: list[Line] = []
        pstyle = Style.parse("bold cyan")
        lines.append(Line(
            spans=[Span("○ ", style="dim"), Span("Select a session (↑↓/j/k to move, Enter to confirm, Esc to cancel):", style="bold")],
            style=ctx.style,
        ))
        for i, s in enumerate(self.sessions):
            sid = s.get("session_id", "?")
            title = s.get("title", "")
            created = s.get("created_at", "")[:16].replace("T", " ")
            msgs = s.get("message_count", 0)
            compacts = s.get("compact_count", 0)
            extra = f" (compacted ×{compacts})" if compacts > 0 else ""
            label = f"  {sid}"
            if title:
                label += f"  [{title}]"
            label += f"  |  {created}  |  {msgs} msgs{extra}"
            if i == self.selected_index:
                spans = [
                    Span("> ", style=pstyle),
                    Span(label, style=Style(reverse=True)),
                ]
            else:
                spans = [
                    Span("  ", style=ctx.style),
                    Span(label, style=ctx.style),
                ]
            lines.append(Line(spans=spans, style=ctx.style))
        self._dirty = False
        return lines


class ProjectSelector(Widget):
    """Interactive project picker — arrow-key navigation with highlight.

    Renders a list of projects from projects.yml with the selected one in
    reverse video. Used by /rant when invoked without @project.
    """

    def __init__(self, projects: list[dict] | None = None):
        self.projects: list[dict] = projects or []
        self.selected_index: int = 0
        self._dirty: bool = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def move_up(self) -> None:
        if self.selected_index > 0:
            self.selected_index -= 1
            self._dirty = True

    def move_down(self) -> None:
        if self.selected_index < len(self.projects) - 1:
            self.selected_index += 1
            self._dirty = True

    @property
    def selected_project_name(self) -> str | None:
        if 0 <= self.selected_index < len(self.projects):
            return self.projects[self.selected_index].get("name", "")
        return None

    def render(self, ctx):
        lines: list[Line] = []
        pstyle = Style.parse("bold cyan")
        lines.append(Line(
            spans=[Span("○ ", style="dim"), Span("Select a project (↑↓/j/k to move, Enter to confirm, Esc to cancel):", style="bold")],
            style=ctx.style,
        ))
        for i, p in enumerate(self.projects):
            name = p.get("name", "?")
            repo = p.get("repo", "")
            label = f"  {name}"
            if repo:
                label += f"  ({repo})"
            if i == self.selected_index:
                spans = [
                    Span("> ", style=pstyle),
                    Span(label, style=Style(reverse=True)),
                ]
            else:
                spans = [
                    Span("  ", style=ctx.style),
                    Span(label, style=ctx.style),
                ]
            lines.append(Line(spans=spans, style=ctx.style))
        self._dirty = False
        return lines


class ModelSelector(Widget):
    """Interactive model picker — arrow-key navigation with highlight.

    Renders a list of available LLM models with the selected one in reverse
    video. Used by /model when invoked without arguments.
    """

    def __init__(self, models: list[dict] | None = None, current: str = ""):
        self.models: list[dict] = models or []
        self.current: str = current
        self.selected_index: int = 0
        self._dirty: bool = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def move_up(self) -> None:
        if self.selected_index > 0:
            self.selected_index -= 1
            self._dirty = True

    def move_down(self) -> None:
        if self.selected_index < len(self.models) - 1:
            self.selected_index += 1
            self._dirty = True

    @property
    def selected_model_name(self) -> str | None:
        if 0 <= self.selected_index < len(self.models):
            return self.models[self.selected_index].get("name", "")
        return None

    def render(self, ctx):
        lines: list[Line] = []
        pstyle = Style.parse("bold cyan")
        active_marker = Style.parse("bold green")
        lines.append(Line(
            spans=[Span("○ ", style="dim"),
                   Span("Select a model (↑↓/j/k to move, Enter to confirm, Esc to cancel):",
                        style="bold")],
            style=ctx.style,
        ))
        for i, m in enumerate(self.models):
            name = m.get("name", "?")
            ctx_win = m.get("context_window", 0)
            is_current = name == self.current
            label = f"  {name}"
            if ctx_win:
                label += f"  (context: {ctx_win:,})"
            if is_current:
                label += "  ★ current"
            if i == self.selected_index:
                spans = [
                    Span("> ", style=pstyle),
                    Span(label, style=Style(reverse=True)),
                ]
            else:
                spans = [
                    Span("  ", style=ctx.style),
                    Span(label, style=active_marker if is_current else ctx.style),
                ]
            lines.append(Line(spans=spans, style=ctx.style))
        self._dirty = False
        return lines


# Command help text for autocomplete dropdown
_COMMAND_HELP: dict[str, str] = {
    "/resume":  "Switch to a session by [id] or interactively (↑↓/j/k to pick)",
    "/sessions": "Browse and switch between saved sessions (↑↓/j/k to navigate)",
    "/compact":  "Compress conversation history to save context",
    "/memory":   "Browse and search memories [session|project|<id>]",
    "/rename":   "Rename current session [title]",
    "/clear":    "Clear current session history and start fresh",
    "/rant":     "Send feedback to the evolution system [/rant | /rant @<project> <msg>]",
    "/model":    "Switch LLM model [/model | /model <name>]",
    "/skills":   "List loaded skills (user + project)",
    "/version":  "Show EMRG version and instance info",
    "/help":     "Show keyboard shortcuts and commands",
}


class CommandDropdown(Widget):
    """Command autocomplete dropdown — filters commands as you type after '/'."""

    def __init__(self, prefix: str = "/"):
        self.prefix: str = prefix
        self.visible: bool = False
        self._all_commands: list[str] = list(_COMMAND_HELP.keys())
        self._matching: list[str] = []
        self.selected_index: int = 0
        self._dirty: bool = True
        self._recompute(prefix)

    def _recompute(self, prefix: str) -> None:
        self.prefix = prefix
        self._matching = [c for c in self._all_commands if c.startswith(prefix)]
        if self.selected_index >= len(self._matching) and self._matching:
            self.selected_index = len(self._matching) - 1
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def move_up(self) -> None:
        if self.selected_index > 0:
            self.selected_index -= 1
            self._dirty = True

    def move_down(self) -> None:
        if self.selected_index < len(self._matching) - 1:
            self.selected_index += 1
            self._dirty = True

    @property
    def selected_command(self) -> str | None:
        if 0 <= self.selected_index < len(self._matching):
            return self._matching[self.selected_index]
        return None

    def render(self, ctx):
        if not self.visible:
            return []
        lines: list[Line] = []
        sel_style = Style(reverse=True)
        dim_style = Style.parse("dim")
        cmd_style = Style.parse("bold yellow")

        if not self._matching:
            lines.append(Line(
                spans=[Span("  No matching commands", style=dim_style)],
                style=ctx.style,
            ))
        else:
            lines.append(Line(
                spans=[Span("  Commands (↑↓ to select, Enter to confirm, Esc to cancel):", style=dim_style)],
                style=ctx.style,
            ))
            for i, cmd in enumerate(self._matching):
                desc = _COMMAND_HELP.get(cmd, "")
                label = f"  {cmd}"
                if desc:
                    label += f"  —  {desc}"
                if i == self.selected_index:
                    spans = [Span(label, style=sel_style)]
                else:
                    spans = [Span(label, style=cmd_style)]
                lines.append(Line(spans=spans, style=ctx.style))
        self._dirty = False
        return lines


class ChatHistory(Widget):
    """Chat message list — holds ChatRow and ToolCard widgets."""

    def __init__(self):
        self.rows: list[Widget] = []
        self._dirty = True

    @property
    def dirty(self): return self._dirty
    @dirty.setter
    def dirty(self, v): self._dirty = v

    def add(self, role_or_widget, content=None):
        if isinstance(role_or_widget, Widget):
            self.rows.append(role_or_widget)
        else:
            self.rows.append(ChatRow(role=role_or_widget, content=content or ""))
        self._dirty = True

    def remove(self, row):
        """Remove a widget from the chat — used for transient UI overlays."""
        try:
            self.rows.remove(row)
            self._dirty = True
        except ValueError:
            pass

    def update_last(self, content):
        for row in reversed(self.rows):
            if isinstance(row, ChatRow):
                row.content = content
                row.dirty = True
                self._dirty = True
                return

    def last_tool_card(self):
        for row in reversed(self.rows):
            if isinstance(row, ToolCard):
                return row
        return None

    def last_markdown(self):
        for row in reversed(self.rows):
            if isinstance(row, StreamingMarkdown):
                return row
        return None

    def render(self, ctx):
        lines = []
        for row in self.rows:
            if isinstance(row, Widget):
                lines.extend(row.render(ctx))
        self._dirty = False
        return lines


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

def _try_connect():
    return is_server_running_sync()

def is_server_running(): return _try_connect()

async def start_server_daemon():
    logger.info("starting emrgd daemon...")
    cleanup_server()
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "emrg.server",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        start_new_session=True, close_fds=True)
    for _ in range(15):
        await asyncio.sleep(0.3)
        if is_server_running(): logger.info("emrgd started (pid=%d)", proc.pid); return
    raise RuntimeError("emrgd failed to start within timeout")

async def _check_and_restart_if_stale():
    """Ping the server. If source has changed since server started, restart it."""
    server_path = get_server_path()

    # Unix socket file check (not applicable on Windows)
    if os.name != "nt":
        if not Path(server_path).exists():
            return  # will start fresh via connect_to_server

    source_mtime = _get_server_source_mtime()
    config_mtime = _get_config_mtime()

    try:
        reader, writer = await connect_to_server()
        await write_frame(writer, json.dumps({"type": "ping"}).encode())
        frame = await asyncio.wait_for(read_frame(reader), timeout=3)
        writer.close()
        try: await writer.wait_closed()
        except (ConnectionError, OSError): pass

        if frame is None:
            return

        data = json.loads(frame.decode())
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
                    if not is_server_running():
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
            asyncio.TimeoutError, Exception):
        pass  # Server not reachable — connect_to_server will handle

async def client_connect_to_server():
    await _check_and_restart_if_stale()
    if not is_server_running():
        cleanup_server()
        await start_server_daemon()
    return await connect_to_server()


class SelectorState:
    """Unified state for interactive selectors (session, project, model).

    Consolidates 9 separate variables into 3 typed instances to eliminate
    nonlocal declaration errors (rant #31).
    """
    __slots__ = ('active', 'widget', 'pending')

    def __init__(self) -> None:
        self.active: bool = False
        self.widget: 'SessionSelector | ProjectSelector | ModelSelector | None' = None
        self.pending: bool = False


async def interactive(init_auto_evolve: bool = False):
    if not sys.stdin.isatty():
        print("This client requires a real terminal (TTY).", file=sys.stderr); return

    try: reader, writer = await client_connect_to_server()
    except Exception as e:
        print(f"Failed to connect to emrgd: {e}", file=sys.stderr); return

    # Session setup
    cwd = os.getcwd()
    session_id = generate_session_id(Path(cwd))

    # Send init_auto_evolve if requested (before ping, so daemon
    # processes it before any user interaction starts)
    if init_auto_evolve:
        await write_frame(writer, json.dumps({
            "type": "init_auto_evolve",
            "cwd": cwd,
        }).encode())
        # Read the response to consume it
        try:
            await asyncio.wait_for(read_frame(reader), timeout=5)
        except asyncio.TimeoutError:
            pass

    await write_frame(writer, json.dumps({"type": "ping"}).encode())
    term = Terminal(); stdin_fd = sys.stdin.fileno()
    status = StatusLine(left=session_id, center="connecting...")
    inp = InputWidget(); chat = ChatHistory()
    term.mount(status=status, composer=inp, chat=chat)

    loop = asyncio.get_event_loop()
    history, paste_mode, stream_buffer = [], False, ""
    history_index: int = -1  # -1 = editing, 0..len-1 = navigating history
    history_saved_input: str = ""  # saved input when navigating history
    busy = False; server_id = ""; need_new_assistant = False; session_title = ""
    msg_count = 0
    _welcomed = False  # show welcome message once on first connect
    _request_start: float = 0.0  # timestamp when current request started
    _elapsed_task: asyncio.Task | None = None  # background timer task

    def _short_path(p: str) -> str:
        home = os.path.expanduser("~")
        if p.startswith(home):
            p = "~" + p[len(home):]
        if len(p) > 30:
            p = "…" + p[-29:]
        return p

    def _update_right() -> None:
        if msg_count > 0:
            status.update(right=f"{msg_count} msgs  {_short_path(cwd)}")
        else:
            status.update(right="Enter=send  Esc=quit  /help")
    _update_right()

    _status_base: str = ""  # base center text without timer, for elapsed timer overlay
    _last_center: str = ""  # last center text set via status.update, for timer overlay

    async def _run_elapsed_timer() -> None:
        """Background task: update status line elapsed time every second while busy."""
        nonlocal _request_start
        while busy:
            elapsed = int(time.time() - _request_start)
            mins, secs = divmod(elapsed, 60)
            timer = f"⏱{mins}:{secs:02d}" if mins > 0 else f"⏱{secs}s"
            status.elapsed = timer
            term.render()
            await asyncio.sleep(1)

    tool_args: dict[str, dict] = {}  # track tool arguments by tool_call_id for diff rendering

    # Selector state — each selector has an active flag, widget ref, and pending flag.
    session_sel = SelectorState()
    project_sel = SelectorState()
    model_sel = SelectorState()
    _rant_project: str | None = None  # Set after project selection, used on next Enter

    # Command autocomplete state (shows dropdown when user types /)
    _autocomplete_active = False
    _autocomplete_widget: CommandDropdown | None = None

    # Render throttling: limit renders during streaming to ~60fps
    _last_render_time = 0.0
    _RENDER_MIN_INTERVAL = 0.016  # ~60fps

    def _render_throttled():
        nonlocal _last_render_time
        now = time.monotonic()
        if now - _last_render_time >= _RENDER_MIN_INTERVAL:
            _last_render_time = now
            term.render()

    async def read_server():
        nonlocal stream_buffer, status, history, chat, busy, server_id, need_new_assistant, session_id, session_title, msg_count, tool_args, _welcomed
        nonlocal _last_center, _elapsed_task, reader, writer

        async def _reconnect():
            """Attempt reconnection — blocks until successful."""
            nonlocal reader, writer, busy, _elapsed_task
            # stop elapsed timer
            if _elapsed_task is not None:
                _elapsed_task.cancel(); _elapsed_task = None
            busy = False  # pending request is lost
            chat.add("system", "⏸ server connection lost — reconnecting...")
            status.update(center="reconnecting...")
            term.render()
            # close stale connection
            try: writer.close(); await writer.wait_closed()
            except Exception: pass
            while True:
                try:
                    await asyncio.sleep(1)
                    reader, writer = await client_connect_to_server()
                    await write_frame(writer, json.dumps({"type": "ping"}).encode())
                    chat.add("system", "✓ server reconnected")
                    status.update(center=server_id or "emrg")
                    term.render()
                    return
                except Exception:
                    continue

        while True:
            try: frame = await asyncio.wait_for(read_frame(reader), timeout=0.1)
            except asyncio.TimeoutError: continue
            except ValueError as e:
                logger.exception("server connection lost: %s", e)
                await _reconnect()
                continue
            if frame is None:
                await _reconnect()
                continue
            text = frame.decode().strip()
            if not text: continue
            try:
                data = json.loads(text)
                if "uptime_seconds" in data:
                    ident = data.get("identity", {}); hid = ident.get("instance_id", "?")[:8]
                    host = ident.get("host_name", "?")
                    model = data.get("model", "")
                    server_id = f"{hid} @ {host}"
                    if model:
                        server_id += f" [{model}]"
                    if not _welcomed:
                        _welcomed = True
                        import emrg
                        ver = getattr(emrg, "__version__", "dev")
                        chat.add("system", f"EMRG {ver}  |  {server_id}\nType /help for shortcuts, or just start chatting.")
                    status.update(left=session_title or session_id, center=server_id); term.render(); continue

                # Tool lifecycle: create a ToolCard on start, update on end.
                if data.get("type") == "tool_start":
                    ts = ToolStart.from_dict(data)
                    tool_args[ts.tool_call_id] = ts.arguments  # track for diff rendering
                    card = ToolCard(
                        name=ts.tool_name,
                        command=_format_args(ts.arguments, ts.tool_name),
                        status="running",
                        expanded=False,
                    )
                    chat.add(card)
                    _last_center = f"running {ts.tool_name}..."
                    status.update(center=_last_center); _render_throttled()
                    continue

                if data.get("type") == "tool_end":
                    te = ToolEnd.from_dict(data)
                    # Show diff for successful edit operations
                    if te.tool_name == "edit" and not te.error and te.tool_call_id in tool_args:
                        args = tool_args.pop(te.tool_call_id)
                        old_str = args.get("old_string", "")
                        new_str = args.get("new_string", "")
                        if old_str or new_str:
                            diff_widget = Diff(
                                old=old_str,
                                new=new_str,
                                old_label="old",
                                new_label="new",
                                mode="unified",
                            )
                            chat.add(diff_widget)
                    # Show summary for successful write operations
                    elif te.tool_name == "write" and not te.error and te.tool_call_id in tool_args:
                        args = tool_args.pop(te.tool_call_id)
                        fp = args.get("file_path", "?")
                        short_fp = f"…/{PurePath(fp).name}" if len(fp) > 50 else fp
                        content_len = len(args.get("content", ""))
                        chat.add("system", f"✓ Wrote {content_len} bytes to {short_fp}")
                    elif te.tool_call_id in tool_args:
                        tool_args.pop(te.tool_call_id)  # cleanup non-edit tools
                    card = chat.last_tool_card()
                    if card and card.name == te.tool_name:
                        card.update(
                            "failed" if te.error else "done",
                            output=te.content,
                        )
                    else:
                        # Fallback: no matching start card
                        prefix = "✗ " if te.error else "✓ "
                        chat.add("tool", f"{prefix}{te.tool_name} result")
                    chat.dirty = True  # ToolCard updated inside; force ChatHistory re-render
                    need_new_assistant = True
                    _last_center = server_id or "emrg"
                    status.update(center=_last_center); term.render()
                    continue

                resp = TaskResponse.from_dict(data)
                if resp.delta and resp.content:
                    if need_new_assistant:
                        md = StreamingMarkdown()
                        md.feed(resp.content)
                        chat.add(md)
                        stream_buffer = resp.content
                        need_new_assistant = False
                    else:
                        stream_buffer += resp.content
                        md = chat.last_markdown()
                        if md:
                            md.feed(resp.content)
                            chat.dirty = True
                        else:
                            # Fallback: no StreamingMarkdown found, use plain text
                            chat.update_last(stream_buffer)
                    logger.debug("ROWS after delta: %d [%s]", len(chat.rows),
                        ', '.join(f'{r.role}={r.content[:30]}' for r in chat.rows if isinstance(r, ChatRow)))
                    _last_center = "streaming..."
                    status.update(center=_last_center); _render_throttled()
                if resp.done:
                    logger.debug("DONE: stream_buffer=%r", stream_buffer[:80])
                    busy = False
                    # Cancel elapsed timer
                    if _elapsed_task:
                        _elapsed_task.cancel()
                        _elapsed_task = None
                    status.elapsed = ""
                    if stream_buffer:
                        # Final flush: if streaming markdown, it already has content;
                        # if plain text fallback, update the ChatRow
                        md = chat.last_markdown()
                        if not md:
                            chat.update_last(stream_buffer)
                        stream_buffer = ""
                    # Show hints from server (e.g. max tool rounds exceeded)
                    if resp.content and ("Exceeded" in resp.content or "exceeded" in resp.content.lower()):
                        chat.add("system", f"⚠ {resp.content}  Try '继续' to resume.")
                    _last_center = server_id or "emrg"
                    status.update(center=_last_center)
                    msg_count += 1; _update_right()
                    term.render()
                if "error" in data:
                    err = data["error"]; logger.error("server error: %s", err)
                    chat.add("system", f"Error: {err}"); term.render()

                # Clear result
                if data.get("type") == "clear_result":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Clear failed: {err}")
                    else:
                        # Clear the TUI chat display
                        chat.rows.clear()
                        chat.dirty = True
                        chat.add("system", "Session cleared — starting fresh.")
                        msg_count = 0
                        _update_right()
                    status.update(center=server_id or "emrg")
                    term.render()
                    continue

                # Compact result
                if data.get("type") == "compact_result":
                    # Skip progress notifications (auto-compact "compacting..." messages)
                    if data.get("auto") and data.get("messages_compacted", 1) == 0:
                        continue
                    compacted = data.get("messages_compacted", 0)
                    summary = data.get("summary", "")
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Compact failed: {err}")
                    else:
                        chat.add("system",
                            f"Compact complete — {compacted} messages compressed into summary.\n"
                            f"Summary: {summary[:200]}..."
                        )
                    busy = False
                    msg_count = max(0, msg_count - compacted)
                    _update_right()
                    status.elapsed = ""
                    status.update(center=server_id or "emrg"); term.render()
                    continue

                # Sessions list
                if data.get("type") == "sessions_list":
                    nonlocal session_sel
                    sessions = data.get("sessions", [])
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                        session_sel.pending = False
                    elif session_sel.pending and sessions:
                        # Enter interactive selection mode
                        session_sel.pending = False
                        session_sel.widget = SessionSelector(sessions)
                        session_sel.active = True
                        chat.add(session_sel.widget)
                        status.update(center="select session: ↑↓ Enter Esc  (j/k vim)")
                    else:
                        session_sel.pending = False
                        if sessions:
                            # /sessions also enters interactive selection mode
                            session_sel.widget = SessionSelector(sessions)
                            session_sel.active = True
                            chat.add(session_sel.widget)
                            status.update(center="select session: ↑↓ Enter Esc  (j/k vim)")
                        else:
                            chat.add("system", "No saved sessions yet. Start chatting to create one.")
                    term.render()
                    continue

                # Projects list
                if data.get("type") == "projects_list":
                    nonlocal project_sel
                    projects = data.get("projects", [])
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                        project_sel.pending = False
                    elif project_sel.pending and projects:
                        project_sel.pending = False
                        project_sel.widget = ProjectSelector(projects)
                        project_sel.active = True
                        chat.add(project_sel.widget)
                        status.update(center="select project: ↑↓ Enter Esc  (j/k vim)")
                    else:
                        project_sel.pending = False
                        if projects:
                            project_sel.widget = ProjectSelector(projects)
                            project_sel.active = True
                            chat.add(project_sel.widget)
                            status.update(center="select project: ↑↓ Enter Esc  (j/k vim)")
                        else:
                            chat.add("system", "No projects configured. Use emrg in a git repo to auto-register.")
                    term.render()
                    continue

                # Models list response (for /model interactive picker)
                if data.get("type") == "models_list":
                    nonlocal model_sel
                    models = data.get("models", [])
                    current = data.get("current", "")
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                        model_sel.pending = False
                    elif models:
                        model_sel.pending = False
                        model_sel.widget = ModelSelector(models, current)
                        model_sel.active = True
                        chat.add(model_sel.widget)
                        status.update(center="select model: ↑↓ Enter Esc  (j/k vim)")
                    else:
                        model_sel.pending = False
                        chat.add("system", "No models configured. Add [[llm.models]] to ~/.emrg/config.toml.")
                    term.render()
                    continue

                # Model set response
                if data.get("type") == "model_set":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Model switch failed: {err}")
                    else:
                        model_name = data.get("model", "")
                        ctx_win = data.get("context_window", 0)
                        previous = data.get("previous", "")
                        chat.add("system",
                                 f"Model switched: {previous} → {model_name}"
                                 f" (context: {ctx_win:,})")
                        # Update server_id so all subsequent status updates show the new model
                        base_id = server_id.split(" [")[0] if " [" in server_id else server_id
                        server_id = f"{base_id} [{model_name}]" if base_id else f"emrg [{model_name}]"
                        status.update(center=server_id)
                    term.render()
                    continue

                # Resume result
                if data.get("type") == "resume_result":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Resume failed: {err}")
                        term.render()
                        continue

                    new_sid = data.get("session_id", "")
                    meta = data.get("meta", {})

                    # Switch session
                    session_id = new_sid

                    # Clear and replay history from disk
                    chat.rows.clear()
                    chat.dirty = True

                    hist_path = Path(cwd) / ".emrg" / "sessions" / session_id / "history.jsonl"
                    record_count = 0
                    if hist_path.exists():
                        for line in hist_path.read_text(encoding="utf-8").splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                r = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            record_count += 1
                            rtype = r.get("type", "")
                            if rtype == "message":
                                role = r.get("role", "")
                                content = r.get("content", "")
                                if role == "user":
                                    chat.add("user", content)
                                elif role == "assistant":
                                    # Use StreamingMarkdown for color rendering (rant #28)
                                    md = StreamingMarkdown()
                                    md.feed(content)
                                    chat.add(md)
                                elif role == "system":
                                    chat.add("system", content)
                                elif role == "tool":
                                    chat.add("tool", content)
                            elif rtype == "summary":
                                chat.add("system",
                                    f"[Session summary from compact #"
                                    f"{r.get('compact_id', '?')}]: "
                                    f"{r.get('content', '')[:300]}")
                            elif rtype == "tool_call":
                                pass
                            elif rtype == "tool_result":
                                chat.add("tool", f"  result: {r.get('content', '')[:500]}")

                    title_extra = ""
                    if meta.get("title"):
                        title_extra = f" [{meta['title']}]"
                        session_title = meta["title"]
                    else:
                        session_title = ""
                    chat.add("system",
                        f"Resumed session {session_id}{title_extra} "
                        f"({meta.get('message_count', record_count)} messages, "
                        f"created {str(meta.get('created_at', ''))[:16].replace('T', ' ')})")
                    status.update(left=session_title or session_id, center=server_id or "emrg")
                    term.set_title(session_title or session_id)
                    # Set message count from loaded session
                    msg_count = meta.get("message_count", record_count)
                    _update_right()
                    term.render()
                    continue

                # Rename result
                if data.get("type") == "rename_result":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Rename failed: {err}")
                    else:
                        new_title = data.get("title", "")
                        session_title = new_title
                        chat.add("system", f"Session renamed to: {new_title}")
                        status.update(left=session_title, center=server_id or "emrg")
                        term.set_title(session_title)
                    term.render()
                    continue

                # Memories list
                if data.get("type") == "memories_list":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                    else:
                        scope = data.get("scope", "project")
                        directory = data.get("directory", "")
                        memories = data.get("memories", [])
                        lines = [f"Memories ({scope}):", f"  Directory: {directory}", ""]
                        if memories:
                            for m in memories:
                                status_tag = f" [{m.get('status', '')}]" if m.get('status') != 'active' else ""
                                lines.append(
                                    f"  [{m.get('type', '?')}] {m.get('title', '?')}{status_tag}"
                                )
                                lines.append(f"    id: {m.get('id', '?')}  file: {m.get('file', '?')}")
                                created = m.get('created_at', '')[:16].replace('T', ' ')
                                event = m.get('event_at', '')[:10]
                                lines.append(f"    recorded: {created}  event: {event}")
                            lines.append("")
                            lines.append("Type /memory <id> to read a specific memory.")
                            lines.append("Type /memory session for session memories.")
                            lines.append("Type /memory project for project memories.")
                        else:
                            lines.append("  (no memories yet)")
                        chat.add("system", "\n".join(lines))
                    term.render()
                    continue

                # Memory content (read)
                if data.get("type") == "memory_content":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                    else:
                        mem_file = data.get("file", "?")
                        mem_path = data.get("path", "?")
                        mem_body = data.get("body", "")
                        frontmatter = data.get("frontmatter", {})
                        lines = [
                            f"Memory: {frontmatter.get('title', mem_file)}",
                            f"  id: {data.get('memory_id', '?')}",
                            f"  type: {frontmatter.get('type', '?')}",
                            f"  scope: {frontmatter.get('scope', '?')}",
                            f"  status: {frontmatter.get('status', '?')}",
                            f"  file: {mem_path}",
                            "",
                            mem_body,
                        ]
                        chat.add("system", "\n".join(lines))
                        status.update(center=server_id or "emrg")
                    term.render()
                    continue

            except json.JSONDecodeError: pass

    read_task = asyncio.create_task(read_server())

    # ── SIGWINCH (terminal resize) handler ─────────────────
    _resize_event = asyncio.Event()

    def _on_sigwinch() -> None:
        _resize_event.set()

    loop.add_signal_handler(signal.SIGWINCH, _on_sigwinch)

    def _handle_selector_nav(data: bytes, widget) -> bool:
        """Handle arrow key and j/k navigation for any selector widget.
        Returns True if navigation was handled, False otherwise.
        """
        if len(data) >= 3 and data[0] == 0x1B and data[1] == 0x5B:
            c = data[2]
            if c == 0x41:  # Up
                widget.move_up()
                return True
            elif c == 0x42:  # Down
                widget.move_down()
                return True
        if data == b"j":
            widget.move_down()
            return True
        if data == b"k":
            widget.move_up()
            return True
        return False

    async def handle_key(data: bytes) -> bool:
        nonlocal inp, status, history, paste_mode, stream_buffer, writer, chat, busy, need_new_assistant, session_id, session_title, msg_count, cwd
        nonlocal session_sel, project_sel, model_sel
        nonlocal history_index, history_saved_input
        nonlocal _autocomplete_active, _autocomplete_widget
        nonlocal _request_start, _last_center, _elapsed_task
        if len(data) == 0: return True
        if data == b"\x1b[200~": paste_mode = True; return True
        if data == b"\x1b[201~": paste_mode = False; term.render(); return True

        # ── ESC interrupt when busy ──────────────────────────
        # Mimics Claude Code: Esc stops the current response mid-turn,
        # keeping work done so far. Dialogs (selector/autocomplete) are
        # handled below — this only fires when LLM is actively responding.
        if data == b"\x1b" and busy:
            busy = False
            if _elapsed_task:
                _elapsed_task.cancel()
                _elapsed_task = None
            status.elapsed = ""
            chat.add("system", "⏸ Interrupted — response stopped. You can continue.")
            _last_center = server_id or "emrg"
            status.update(center=_last_center)
            chat.dirty = True; term.render()
            return True

        # ── Session selector mode ──────────────────────────
        if session_sel.active and session_sel.widget:
            if data == b"\x1b":  # Esc — cancel selection
                session_sel.active = False
                chat.add("system", "Session selection cancelled.")
                session_sel.widget = None
                status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if data == b"\r" or data == b"\n":  # Enter — confirm
                sid = session_sel.widget.selected_session_id
                session_sel.active = False
                session_sel.widget = None
                if sid:
                    await write_frame(writer, json.dumps({
                        "type": "resume_session",
                        "session_id": sid,
                        "cwd": cwd,
                    }).encode())
                    status.update(center=f"resuming {sid}...")
                    term.render()
                else:
                    chat.add("system", "No session selected.")
                    status.update(center=server_id or "emrg")
                    term.render()
                return True
            if _handle_selector_nav(data, session_sel.widget):
                chat.dirty = True; term.render()
                return True
            # Ignore other keys when in selector mode
            return True

        # ── Project selector mode ──────────────────────────
        if project_sel.active and project_sel.widget:
            if data == b"\x1b":  # Esc — cancel selection
                project_sel.active = False
                chat.add("system", "Project selection cancelled.")
                project_sel.widget = None
                status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if data == b"\r" or data == b"\n":  # Enter — confirm
                pname = project_sel.widget.selected_project_name
                project_sel.active = False
                project_sel.widget = None
                if pname:
                    nonlocal _rant_project
                    _rant_project = pname
                    chat.add("system", f"Rant to project '@{pname}' — type your message and press Enter:")
                    status.update(center=f"rant to @{pname}")
                else:
                    chat.add("system", "No project selected.")
                    status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if _handle_selector_nav(data, project_sel.widget):
                chat.dirty = True; term.render()
                return True
            # Ignore other keys when in project selector mode
            return True

        # ── Model selector mode ──────────────────────────
        if model_sel.active and model_sel.widget:
            if data == b"\x1b":  # Esc — cancel selection
                model_sel.active = False
                chat.add("system", "Model selection cancelled.")
                model_sel.widget = None
                status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if data == b"\r" or data == b"\n":  # Enter — confirm
                mname = model_sel.widget.selected_model_name
                model_sel.active = False
                model_sel.widget = None
                if mname:
                    await write_frame(writer, json.dumps({
                        "type": "set_model",
                        "model": mname,
                    }).encode())
                    status.update(center=f"switching model to {mname}...")
                else:
                    chat.add("system", "No model selected.")
                    status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if _handle_selector_nav(data, model_sel.widget):
                chat.dirty = True; term.render()
                return True
            # Ignore other keys when in model selector mode
            return True

        # ── Command autocomplete: recompute on every keystroke ──
        if not session_sel.active and not busy:
            text_stripped = inp.text.lstrip()
            if text_stripped.startswith("/"):
                cmd_prefix = text_stripped.split(None, 1)[0]
                # If the prefix already is a full command (with args or not),
                # don't show autocomplete — let Enter submit directly
                if cmd_prefix in _COMMAND_HELP:
                    if _autocomplete_active:
                        _autocomplete_active = False
                        if _autocomplete_widget:
                            chat.remove(_autocomplete_widget)
                        chat.dirty = True
                elif not _autocomplete_active:
                    _autocomplete_active = True
                    _autocomplete_widget = CommandDropdown(prefix=cmd_prefix)
                    _autocomplete_widget.visible = True
                    chat.add(_autocomplete_widget)
                    chat.dirty = True
                else:
                    _autocomplete_widget._recompute(cmd_prefix)
                    chat.dirty = True
            elif _autocomplete_active:
                _autocomplete_active = False
                if _autocomplete_widget:
                    chat.remove(_autocomplete_widget)
                chat.dirty = True

        # ── Command autocomplete: intercept navigation keys ──
        if _autocomplete_active and _autocomplete_widget:
            if data == b"\x1b":  # Esc — dismiss and clear input
                _autocomplete_active = False
                chat.remove(_autocomplete_widget)
                # Clear the partial command so autocomplete doesn't re-trigger
                inp.text = ""; inp.cursor = 0; inp.dirty = True
                chat.dirty = True; term.render()
                return True
            if data == b"\r" or data == b"\n":  # Enter — complete
                cmd = _autocomplete_widget.selected_command
                if cmd:
                    old_prefix = inp.text.lstrip().split(None, 1)[0]
                    rest = inp.text.lstrip()[len(old_prefix):]
                    leading = inp.text[:len(inp.text) - len(inp.text.lstrip())]
                    inp.text = leading + cmd + rest
                    inp.cursor = len(inp.text)
                    inp.dirty = True
                _autocomplete_active = False
                chat.remove(_autocomplete_widget)
                chat.dirty = True; term.render()
                return True
            if len(data) >= 3 and data[0] == 0x1B and data[1] == 0x5B:
                c = data[2]
                if c == 0x41:  # Up
                    _autocomplete_widget.move_up()
                    chat.dirty = True; term.render()
                    return True
                elif c == 0x42:  # Down
                    _autocomplete_widget.move_down()
                    chat.dirty = True; term.render()
                    return True
            # Tab in autocomplete mode: cycle selection
            if data == b"\t" or data[0] == 0x09:
                cmds = _autocomplete_widget._matching
                if cmds:
                    _autocomplete_widget.selected_index = (
                        (_autocomplete_widget.selected_index + 1) % len(cmds)
                    )
                    _autocomplete_widget._dirty = True
                    chat.dirty = True; term.render()
                return True
            # For any other key: fall through to normal processing
            # (autocomplete will recompute on the next keystroke)

        b = data[0]
        if b in (0x03, 0x04): return False
        # Ctrl+A (home), Ctrl+E (end), Ctrl+U (delete to line start),
        # Ctrl+W (delete word), Ctrl+K (kill to end of line)
        if b == 0x01: inp.move_home(); term.render(); return True
        if b == 0x05: inp.move_end(); term.render(); return True
        if b == 0x15: inp.delete_to_beginning_of_line(); term.render(); return True
        if b == 0x17: inp.delete_word_backward(); term.render(); return True
        if b == 0x0B: inp.delete_to_end_of_line(); term.render(); return True
        if b >= 0x80:
            try:
                for c in data.decode("utf-8"): inp.insert(c)
            except UnicodeDecodeError: pass
            if not paste_mode: term.render()
            return True

        # Tab: command completion (when / prefix) or tool card toggle
        if b == 0x09:
            text = inp.text.lstrip()
            if text.startswith("/"):
                cmd_prefix = text.split(None, 1)[0]
                # If autocomplete already handles this (partial command), skip
                if cmd_prefix not in _COMMAND_HELP:
                    # Partial command — let autocomplete intercept handle it
                    pass
                # else: full command typed, Tab does nothing (Enter to submit)
            else:
                # Tool card toggle
                tool_cards = [r for r in chat.rows if isinstance(r, ToolCard)]
                if tool_cards:
                    changed = False
                    for tc in tool_cards:
                        if tc.output and not tc.expanded:
                            tc.toggle()
                            changed = True
                            break
                    if not changed and tool_cards:
                        tool_cards[-1].toggle()
                    chat.dirty = True; term.render()
            return True

        if b == 0x1B and len(data) >= 3:
            if data[1] == 0x5B:
                c = data[2]
                if c == 0x41:  # Up
                    avail = max(1, term.viewport.viewport_width - 2)
                    if inp._cursor_vrow(avail) == 0:
                        # Cursor on first visual row → navigate command history
                        if history:
                            if history_index == -1:
                                history_saved_input = inp.text
                                history_index = len(history) - 1
                            elif history_index > 0:
                                history_index -= 1
                            inp.text = history[history_index]
                            inp.cursor = len(inp.text)
                            inp.dirty = True
                    else:
                        inp.move_up(avail)
                elif c == 0x42:  # Down
                    avail = max(1, term.viewport.viewport_width - 2)
                    rows = inp._visual_rows(avail)
                    if inp._cursor_vrow(avail) >= len(rows) - 1:
                        # Cursor on last visual row → navigate command history forward
                        if history_index >= 0:
                            if history_index < len(history) - 1:
                                history_index += 1
                                inp.text = history[history_index]
                            else:
                                history_index = -1
                                inp.text = history_saved_input
                            inp.cursor = len(inp.text)
                            inp.dirty = True
                    else:
                        inp.move_down(avail)
                elif c == 0x43: inp.move_right()
                elif c == 0x44: inp.move_left()
                elif c == 0x48: inp.move_home()
                elif c == 0x46: inp.move_end()
                elif c == 0x33 and len(data) >= 4 and data[3] == 0x7E: inp.delete_forward()
            term.render(); return True
        if b in (0x7F, 0x08):
            inp.backspace()
            if not paste_mode: term.render()
            return True
        if b == 0x0D:
            if paste_mode:
                if not inp.text.endswith("\n"): inp.insert("\n")
                term.render(); return True
            logger.debug("ENTER: text=%r busy=%s len=%d", inp.text, busy, len(inp.text))
            if busy:
                logger.debug("ENTER blocked by busy")
                term.render(); return True
            text = inp.text.strip()
            if text:
                if text.lower() in ("quit", "exit"): return False

                # If a rant project was selected, use this message as the rant
                if _rant_project:
                    payload = {
                        "type": "rant",
                        "message": text,
                        "project": _rant_project,
                        "timestamp": datetime.now().isoformat(),
                    }
                    await write_frame(writer, json.dumps(payload).encode())

                    chat.add("system", f"Rant recorded (@{_rant_project}). The evolution system will review it.")
                    _rant_project = None
                    status.update(center=server_id or "emrg")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /memory command
                if text.lower().startswith("/memory"):
                    parts = text.split(None, 1)
                    sub = parts[1].strip() if len(parts) > 1 else ""
                    scope = "project"
                    mem_id = ""
                    if sub:
                        # /memory session → list session memories
                        # /memory <id> → read specific memory
                        if sub.lower() == "session":
                            scope = "session"
                        elif sub.lower() == "project":
                            scope = "project"
                        else:
                            mem_id = sub
                            # Could be a read request — send as read
                            await write_frame(writer, json.dumps({
                                "type": "read_memory",
                                "scope": scope,
                                "memory_id": mem_id,
                                "session_id": session_id,
                                "cwd": cwd,
                            }).encode())
        
                            status.update(center="reading memory...")
                            inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                            return True

                    # List memories
                    await write_frame(writer, json.dumps({
                        "type": "list_memories",
                        "scope": scope,
                        "session_id": session_id,
                        "cwd": cwd,
                    }).encode())

                    status.update(center=f"listing {scope} memories...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /compact command
                if text.lower() == "/compact":
                    await write_frame(writer, json.dumps({
                        "type": "compact",
                        "session_id": session_id,
                        "cwd": cwd,
                    }).encode())

                    status.update(center="compacting...")
                    chat.add("system", "Compact requested — summarizing conversation...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /rename command
                if text.lower().startswith("/rename"):
                    parts = text.split(None, 1)
                    title = parts[1].strip() if len(parts) > 1 else ""
                    await write_frame(writer, json.dumps({
                        "type": "rename_session",
                        "session_id": session_id,
                        "cwd": cwd,
                        "title": title,
                    }).encode())

                    if title:
                        status.update(center=f"renaming to {title}...")
                        chat.add("system", f"Renaming session to: {title}")
                    else:
                        status.update(center="generating title...")
                        chat.add("system", "Auto-generating title...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /sessions command
                if text.lower() == "/sessions":
                    await write_frame(writer, json.dumps({
                        "type": "list_sessions",
                        "cwd": cwd,
                    }).encode())

                    status.update(center="listing sessions...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /skills command
                if text.lower() == "/skills":
                    skills = load_skills()
                    if skills:
                        lines = ["**Loaded Skills:**", ""]
                        for s in skills:
                            lines.append(f"- **{s.name}** ({s.source}) — {s.description}")
                        chat.add("system", "\n".join(lines))
                    else:
                        chat.add("system", "No skills loaded. Add .md files to ~/.emrg/skills/ or .emrg/skills/")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /version command
                if text.lower() == "/version":
                    import emrg
                    ver = getattr(emrg, "__version__", "dev")
                    chat.add("system", f"EMRG {ver}  |  {server_id}\nSession: {session_title or session_id}\nCWD: {cwd}")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /help command
                if text.lower() == "/help":
                    help_text = """Keyboard Shortcuts
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Editing
  Type text           Insert at cursor
  Enter               Send message
  Esc                 Interrupt response (when busy) / close dialog
  Ctrl+C / Ctrl+D     Quit
  Backspace           Delete left
  Del (Fn+Delete)     Delete right
  Ctrl+U              Delete to line start
  Ctrl+W              Delete word left
  Ctrl+K              Delete to end of line
  ← →                 Move cursor
  Ctrl+A / Ctrl+E     Jump to line start/end
  ↑ ↓                 Navigate history / move between lines
  Home / End          Jump to line start/end
  Opt+Enter           Insert newline (not send)

Navigation
  Scroll/mouse wheel   Browse history (terminal native)
  Tab                  Complete command (/ prefix) or toggle tool card
  /                    Type / to show command menu (↑↓ to select, type to filter)
  j / k                Vim-style up/down in session picker

Commands
  /help               Show this help
  /skills             List loaded skills (user + project)
  /version            Show EMRG version and instance info
  /compact            Compress conversation history
  /clear              Clear current session and start fresh
  /memory [session|project|<id>]  Browse memories
  /sessions           Interactive session picker (↑↓/j/k to select)
  /resume [id]        Switch to session (no args = interactive picker, ↑↓/j/k)
  /rename [title]     Rename current session
  /rant <msg>         Send feedback to evolution system
  /rant @<project> <msg>  Rant to a specific project
  /rant               Interactive project picker, then type message
  /model [name]        Switch LLM model (no args = interactive picker)
  quit / exit         Exit EMRG

Streaming
  ● assistant         Markdown + syntax-highlighted
  ◇ tool              Green tool prefix
  ○ system            Dim system messages
  > user              Cyan user messages"""
                    chat.add("system", help_text)
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /clear command
                if text.lower() == "/clear":
                    await write_frame(writer, json.dumps({
                        "type": "clear_session",
                        "session_id": session_id,
                        "cwd": cwd,
                    }).encode())

                    status.update(center="clearing session...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /rant command
                if text.lower().startswith("/rant"):
                    parts = text.split(None, 2)
                    message = parts[1].strip() if len(parts) > 1 else ""
                    project = None
                    if message.startswith("@"):
                        # /rant @<project> <message> — project-targeted rant
                        sub = text.split(None, 2)
                        project = sub[1][1:]  # strip @
                        message = sub[2].strip() if len(sub) > 2 else ""
                    if not message:
                        if project:
                            chat.add("system", "Usage: /rant @<project> <message>")
                            inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                            return True
                        # /rant without args → interactive project selector
                        project_sel.pending = True
                        await write_frame(writer, json.dumps({
                            "type": "list_projects",
                        }).encode())
    
                        status.update(center="loading projects...")
                        inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                        return True
                    payload = {
                        "type": "rant",
                        "message": message,
                        "timestamp": datetime.now().isoformat(),
                    }
                    if project:
                        payload["project"] = project
                    await write_frame(writer, json.dumps(payload).encode())

                    target = f" (@{project})" if project else ""
                    chat.add("system", f"Rant recorded{target}. The evolution system will review it.")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /model command
                if text.lower().startswith("/model"):
                    parts = text.split(None, 1)
                    model_arg = parts[1].strip() if len(parts) > 1 else ""
                    if model_arg:
                        # /model <name> → direct switch
                        await write_frame(writer, json.dumps({
                            "type": "set_model",
                            "model": model_arg,
                        }).encode())
    
                        status.update(center=f"switching model to {model_arg}...")
                    else:
                        # /model without args → interactive picker
                        model_sel.pending = True
                        await write_frame(writer, json.dumps({
                            "type": "list_models",
                        }).encode())
    
                        status.update(center="loading models...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /resume command
                if text.lower().startswith("/resume"):
                    parts = text.split(None, 1)
                    if len(parts) < 2:
                        # No argument: enter interactive session selection
                        session_sel.pending = True
                        await write_frame(writer, json.dumps({
                            "type": "list_sessions",
                            "cwd": cwd,
                        }).encode())
    
                        status.update(center="loading sessions...")
                    else:
                        target_sid = parts[1].strip()
                        # Deactivate selector if active
                        session_sel.active = False
                        session_sel.widget = None
                        session_sel.pending = False
                        await write_frame(writer, json.dumps({
                            "type": "resume_session",
                            "session_id": target_sid,
                            "cwd": cwd,
                        }).encode())
    
                        status.update(center=f"resuming {target_sid}...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                busy = True; need_new_assistant = True  # rant #32: force new StreamingMarkdown per response
                _request_start = time.time()
                # Cancel any stale timer and start a new one
                if _elapsed_task:
                    _elapsed_task.cancel()
                _elapsed_task = asyncio.create_task(_run_elapsed_timer())
                logger.debug("SUBMIT: text=%r", text)
                chat.add("user", inp.text)
                history.append(text); stream_buffer = ""
                history_index = -1  # reset history navigation on submit
                chat.add("assistant", "")
                msg_count += 1; _update_right()
                logger.debug("ROWS after asst: %d [%s]", len(chat.rows),
                    ', '.join(f'{r.role}={r.content[:20]}' for r in chat.rows if isinstance(r, ChatRow)))
                _last_center = "thinking..."
                status.update(center=_last_center)
                term.render()
                req = TaskRequest(session_id=session_id, cwd=cwd, prompt=text, stream=True)
                await write_frame(writer, json.dumps(req.to_dict()).encode())
            inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render(); return True
        if b == 0x1B and len(data) >= 2 and data[1] in (0x0D, 0x0A):
            inp.insert("\n")
            if not paste_mode: term.render()
            return True
        if b == 0x1B: return True
        if 0x20 <= b <= 0x7E:
            inp.insert(chr(b))
            if not paste_mode: term.render()
            return True
        # Multi-byte UTF-8 (CJK, emoji, etc.) — decode the full sequence
        if b >= 0x80:
            try:
                char = data.decode("utf-8")
                inp.insert(char)
            except UnicodeDecodeError:
                pass
            if not paste_mode: term.render()
            return True
        if b == 0x0A:
            if paste_mode:
                if not inp.text.endswith("\n"): inp.insert("\n")
            else: inp.insert("\n")
            if not paste_mode: term.render()
            return True
        return True

    parser = InputParser()
    try:
        while True:
            # Race stdin read against SIGWINCH resize event
            read_ft = loop.run_in_executor(None, os.read, stdin_fd, 16)
            resize_ft = asyncio.ensure_future(_resize_event.wait())
            done, pending = await asyncio.wait(
                [read_ft, resize_ft], return_when=asyncio.FIRST_COMPLETED)

            # Cancel the asyncio future that didn't fire (resize_ft is cheap to cancel)
            for ft in pending:
                if ft is resize_ft:
                    ft.cancel()
                    try: await ft
                    except (asyncio.CancelledError, Exception): pass

            # Process resize immediately (real-time, no keypress needed)
            if _resize_event.is_set():
                _resize_event.clear()
                try: term.handle_resize()
                except Exception:
                    logger.debug("resize handler failed", exc_info=True)

            # Skip data processing if stdin read didn't complete
            if read_ft not in done:
                continue

            data = read_ft.result()
            if not data: break

            for seq in parser.feed(data):
                if not await handle_key(seq): return
            while parser.has_pending():
                try:
                    more = await asyncio.wait_for(
                        loop.run_in_executor(None, os.read, stdin_fd, 8), timeout=0.05)
                    for seq in parser.feed(more):
                        if not await handle_key(seq): return
                except TimeoutError:
                    # Flush standalone Escape (Claude Code style: 50ms timer for lone ESC)
                    if parser._buf == bytearray(b'\x1b'):
                        parser._buf.clear()
                        if not await handle_key(b'\x1b'): return
                    break
    except Exception: logger.exception("TUI main loop crashed")
    finally:
        read_task.cancel()
        try: await read_task
        except (asyncio.CancelledError, Exception): pass
        writer.close()
        try: await writer.wait_closed()
        except (ConnectionError, OSError): pass
        term.shutdown(); sys.stdout.write("\n"); sys.stdout.flush()


def _format_args(args: dict, tool_name: str = "") -> str:
    """Format tool arguments for compact, human-readable display in the ToolCard header.

    Instead of raw JSON, shows the most relevant argument for each tool type.
    """
    if not args:
        return ""

    # Tool-specific human-readable formats
    if tool_name == "bash":
        cmd = args.get("command", "")
        workdir = args.get("workdir")
        if cmd:
            # Show first non-empty line, truncate long commands
            first_line = ""
            for raw_line in cmd.split("\n"):
                stripped = raw_line.strip()
                if stripped:
                    first_line = stripped
                    break
            if not first_line:
                first_line = cmd.split("\n")[0].strip()
            # Prefix with workdir if set
            prefix = f"[{workdir}] " if workdir else ""
            remaining = 70 - len(prefix)
            # Guard: always show at least 8 chars of the command
            if remaining < 8:
                remaining = 8
                # Truncate the workdir to fit — keep last 20 chars
                if workdir:
                    short_dir = workdir if len(workdir) <= 20 else "…" + workdir[-19:]
                    prefix = f"[{short_dir}] "
                    remaining = 70 - len(prefix)
                    if remaining < 8:
                        remaining = 8
            if len(first_line) > remaining:
                first_line = first_line[:remaining - 3] + "..."
            return prefix + first_line
    elif tool_name in ("read", "write", "edit"):
        fp = args.get("file_path", "")
        if fp:
            name = PurePath(fp).name
            # Compact path display
            short = "…/" + name if len(fp) > 50 else fp
            # Add context: size for write, range for read
            if tool_name == "write":
                content_len = len(args.get("content", ""))
                if content_len >= 1024:
                    short += f" ({content_len // 1024}KB)"
                elif content_len > 0:
                    short += f" ({content_len}B)"
            elif tool_name == "read":
                offset = args.get("offset")
                limit = args.get("limit")
                if offset and limit:
                    short += f" [{offset}:{offset + limit}]"
                elif offset:
                    short += f" [from L{offset}]"
            return short

    # Fallback: JSON dump (truncated)
    arg_str = json.dumps(args, ensure_ascii=False)
    if len(arg_str) > 60:
        arg_str = arg_str[:57] + "..."
    return arg_str


def run_client(init_auto_evolve: bool = False): asyncio.run(interactive(init_auto_evolve=init_auto_evolve))
