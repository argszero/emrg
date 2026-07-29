"""EMRG client — TUI widget classes extracted from app.py.

Contains InputWidget, selectors (Session/Project/Model/Rewind/Command),
ChatHistory, _COMMAND_HELP, and SelectorState.
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.style import Style
from emrg.client.python_tui import ChatRow, ToolCard
from emrg.client.python_tui.widgets.base import Line, Span, Widget
from emrg.client.python_tui.widgets.markdown import StreamingMarkdown


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


class RewindSelector(Widget):
    """Interactive message picker for /rewind — arrow-key navigation with highlight.

    Renders a list of user messages with the selected one in reverse video.
    Selecting a message rewinds the session to that point (truncates after it).
    """

    def __init__(self, messages: list[dict] | None = None):
        self.messages: list[dict] = messages or []
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
        if self.selected_index < len(self.messages) - 1:
            self.selected_index += 1
            self._dirty = True

    @property
    def selected_record_index(self) -> int | None:
        if 0 <= self.selected_index < len(self.messages):
            return self.messages[self.selected_index].get("record_index")
        return None

    def render(self, ctx):
        lines: list[Line] = []
        pstyle = Style.parse("bold yellow")
        lines.append(Line(
            spans=[Span("↶ ", style="dim"), Span("Rewind session — select a message to rewind to (↑↓/j/k to move, Enter to confirm, Esc to cancel):", style="bold")],
            style=ctx.style,
        ))
        lines.append(Line(
            spans=[Span("  " + "─" * (ctx.width - 4), style="dim")],
            style=ctx.style,
        ))
        for i, m in enumerate(self.messages):
            preview = m.get("preview", m.get("content", "")[:80])
            ts = m.get("timestamp", "")[:16].replace("T", " ")
            label = f"  [{i+1}] {preview}"
            if ts:
                label += f"  ({ts})"
            if i == self.selected_index:
                spans = [
                    Span("> ", style=pstyle),
                    Span(label, style=Style(reverse=True)),
                ]
            else:
                spans = [Span("  ", style="dim"), Span(label, style=ctx.style)]
            lines.append(Line(spans=spans, style=ctx.style))
        self._dirty = False
        return lines


class SessionSelector(Widget):
    """Interactive session picker — arrow-key navigation with highlight.

    Renders a list of sessions with the selected one in reverse video.
    Used by /resume and /delete when invoked without arguments.

    If current_session_id is set, that session is displayed first and
    marked with (current) in the label.
    """

    def __init__(self, sessions: list[dict] | None = None,
                 current_session_id: str | None = None):
        self.sessions: list[dict] = sessions or []
        self.selected_index: int = 0
        self.current_session_id = current_session_id
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
            if self.current_session_id and sid == self.current_session_id:
                label += "  (current)"
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


class TaskSelector(Widget):
    """Interactive task picker — arrow-key navigation with highlight.

    Renders a list of scheduled tasks from the server with the selected one
    in reverse video. Used by /trigger when invoked without arguments.
    """

    def __init__(self, tasks: list[dict] | None = None):
        self.tasks: list[dict] = tasks or []
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
        if self.selected_index < len(self.tasks) - 1:
            self.selected_index += 1
            self._dirty = True

    @property
    def selected_task_name(self) -> str | None:
        if 0 <= self.selected_index < len(self.tasks):
            return self.tasks[self.selected_index].get("name", "")
        return None

    def render(self, ctx):
        lines: list[Line] = []
        pstyle = Style.parse("bold cyan")
        lines.append(Line(
            spans=[Span("○ ", style="dim"), Span("Select a task to trigger (↑↓/j/k to move, Enter to confirm, Esc to cancel):", style="bold")],
            style=ctx.style,
        ))
        for i, t in enumerate(self.tasks):
            name = t.get("name", "?")
            running = t.get("running", False)
            next_in = t.get("next_run_in_seconds")
            interval = t.get("interval", 0)
            if running:
                status_str = "● RUNNING"
            elif next_in is not None:
                status_str = f"◌ next in ~{next_in}s"
            else:
                status_str = "◌ idle"
            label = f"  {name}  [{status_str}]  (every {interval}s)"
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
    "/rewind":   "Rewind session — pick a user message to truncate history to",
    "/compact":  "Compress conversation history to save context",
    "/memory":   "Browse and search memories [session|project|<id>]",
    "/rename":   "Rename current session [title]",
    "/delete":   "Delete a session [/delete | /delete <session_id>]",
    "/clear":    "Clear current session history and start fresh",
    "/rant":     "Send feedback to the evolution system [/rant | /rant @<project> <msg>]",
    "/model":    "Switch LLM model [/model | /model <name>]",
    "/trigger":  "List or manually trigger scheduled tasks [/trigger | /trigger <name>]",
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
