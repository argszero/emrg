"""EMRG client — TUI widget classes extracted from app.py.

Contains InputWidget, selectors (Session/Project/Model/Rewind/Command),
ChatHistory, _COMMAND_HELP, and SelectorState.
"""

from __future__ import annotations

import json

from rich.cells import cell_len
from rich.style import Style
from emrg.client.python_tui import ChatRow, ToolCard
from emrg.client.python_tui.widgets.base import Line, Span, Widget
from emrg.client.python_tui.widgets.chat_row import format_message_time
from emrg.client.python_tui.widgets.markdown import StreamingMarkdown, UserMarkdown


def windowed_range(count: int, selected: int, size: int) -> tuple[int, int]:
    """The half-open ``[start, end)`` an on-screen window of ``size`` items occupies.

    Kept as one function because this arithmetic had two hand-written copies before
    issue #2248 (`SessionSelector.window` and `ToolSelector.window`), and the whole
    point of the window is that the row ``selected`` names stays inside it: a second
    copy is a second chance to get that wrong, in a place no reader looks.

    The window is centred on the selection where it can be and clamped at both ends,
    which is what keeps `start <= selected < end` true for every input — including
    `size == 1`, where the caller has room for exactly the selected row.

    An empty list has no window at all, and that is stated first rather than left to the
    clamping: `min(size, count)` would otherwise floor at 1 and hand back `(0, 1)` for a
    list of nothing, which a caller drawing `for i in range(start, end)` turns into an
    `IndexError`. Caught by `test_windowed_range_keeps_the_selection_inside_for_every_size`
    one cycle after the same shape was written by hand (issue #2248).
    """
    if count <= 0:
        return 0, 0
    size = max(1, min(size, count))
    start = min(max(0, selected - size // 2), max(0, count - size))
    return start, start + size


def window_plan(avail: int, count: int, selected: int, *,
                fixed_rows: int = 1) -> tuple[int, int, int, bool]:
    """How a list widget lays itself out in ``avail`` rows. Returns a 4-tuple.

    ``(chrome_rows, start, end, markers)`` — the rows the widget's own chrome (its
    title, its rules) may spend, the half-open slice of the list to draw, and whether
    the two edge markers are affordable.

    This is the ONE producer of the rule every list selector obeys (issue #2244 for the
    first two, #2248 for the rest): **the rows emitted may never exceed ``avail``**, the
    row ``selected`` names is always among them, and rows left out are announced on the
    edge they left by. It matters because the terminal paints only the *tail* of what a
    widget emits and writes the rest into native scrollback once — never redrawn — so a
    widget that emits more than the area holds loses the rows above the fold, the
    reverse-video selection among them.

    The two degenerate cases are stated rather than left to the caller, because both are
    load-bearing:

    - ``avail == 0`` means "the caller stated no height". The whole list is returned with
      no markers: a widget must not invent a bound it was not given.
    - When the markers would cost more than the room left, the list is cut *without* them
      rather than announcing a window that no longer fits — a marker is not worth hiding
      the rows it marks. Only a terminal too short to hold `fixed_rows + 1` rows reaches
      this, and then the chrome yields instead: the list is what the caller asked for.
    """
    if avail <= 0:
        return fixed_rows, 0, count, False
    chrome = fixed_rows if avail > fixed_rows else 0
    room = avail - chrome
    if count <= room:
        return chrome, 0, count, False
    size = room - 2
    if size >= 1:
        start, end = windowed_range(count, selected, size)
        return chrome, start, end, True
    start, end = windowed_range(count, selected, max(1, room))
    return chrome, start, end, False


class InputWidget(Widget):
    #: The top and bottom rules the box always draws. A caller states the box's whole
    #: row budget through `RenderContext.height`, so these come out of it rather than
    #: being drawn on top of it (rant 2026-10-14T15:28:56).
    SEPARATOR_ROWS = 2

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
        return self._vrow_of(self._visual_rows(available), self.cursor)

    @staticmethod
    def _vrow_of(rows: list, cursor: int) -> int:
        """The visual row holding `cursor`, given rows from `_visual_rows`.

        Split out so a caller that has already built the rows does not rebuild them:
        the display and the keys must walk the same list (rant 2026-10-14T15:28:56).
        """
        for i, (s, e) in enumerate(rows):
            if s <= cursor <= e: return i
        return max(0, len(rows) - 1)

    @staticmethod
    def _window_start(cursor_vrow: int, visible: int, total: int) -> int:
        """The first visual row to draw, so that `cursor_vrow` is inside the window.

        Overflow leaves from the **top**: the rows above the fold scroll out of the
        box, which is what keeps the cursor's own row — and with it the cursor — on
        the screen. Cutting the tail instead drops the newest rows, which is the
        reported defect.
        """
        if total <= visible:
            return 0
        return max(0, min(cursor_vrow - visible + 1, total - visible))

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
        #: The two separators are part of the budget, not extra rows paid for out of it.
        budget = max(0, getattr(ctx, "height", 0) or 0)
        available = max(1, ctx.width - len("> "))
        # One row model, shared with the keys: `_visual_rows` is what ↑/↓ move through
        # (app.py routes them here with this same `available`), so the rows drawn are
        # the rows the cursor is known to be on (rant 2026-10-14T15:28:56). `budget == 0`
        # is "the caller stated no height" — the box then draws everything it has, which
        # is the contract `RenderContext.height` documents.
        rows = self._visual_rows(available)
        total = len(rows)
        visible = total if budget <= 0 else max(1, budget - self.SEPARATOR_ROWS)
        top = self._window_start(self._vrow_of(rows, self.cursor), visible, total)

        lines = [Line(spans=[Span(self._top_separator(ctx.width, top), style=sep_style)],
                      style=ctx.style)]
        prompt = "> "
        for vi in range(top, min(top + visible, total)):
            s, e = rows[vi]
            chunk = self.text[s:e]
            # A row ends its logical line when the character after it is a newline (or
            # there is none) — that is what separates "wrapped" from "a new line of
            # input", and it is the condition the cursor-at-the-end placeholder needs.
            line_end = e >= len(self.text) or self.text[e] == "\n"
            spans = [Span(prompt, style=pstyle)]
            if s <= self.cursor < e:
                local = self.cursor - s
                spans.append(Span(chunk[:local], style=ctx.style))
                spans.append(Span(chunk[local], style=Style(reverse=True)))
                spans.append(Span(chunk[local + 1:], style=ctx.style))
            elif self.cursor == e and line_end:
                # The cursor sits past the row's last character: it needs a cell of its
                # own, or the screen shows no cursor at all on an otherwise full row.
                spans.append(Span(chunk, style=ctx.style))
                spans.append(Span(" ", style=Style(reverse=True)))
            else:
                spans.append(Span(chunk, style=ctx.style))
            lines.append(Line(spans=spans, style=ctx.style))

        lines.append(Line(spans=[Span("─" * ctx.width, style=sep_style)], style=ctx.style))
        self._dirty = False; return lines

    def _top_separator(self, width: int, hidden_above: int) -> str:
        """The top rule, announcing the rows that scrolled out of the box.

        Requirement of rant 2026-10-14T15:28:56: a hidden row must be said out loud,
        and saying it must not cost a row — so it rides the rule that is drawn anyway.
        """
        bar = "─" * width
        if hidden_above <= 0:
            return bar
        hint = f" ↑ {hidden_above} more "
        if cell_len(hint) >= width:
            return bar
        return "─" + hint + "─" * (width - cell_len(hint) - 1)


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
        avail = getattr(ctx, "height", 0) or 0
        n = len(self.messages)
        # Two chrome rows (the title and the rule under it), paid for out of the same
        # budget as the list and its markers (issue #2248).
        chrome, start, end, markers = window_plan(avail, n, self.selected_index, fixed_rows=2)
        if chrome >= 1:
            lines.append(Line(
                spans=[Span("↶ ", style="dim"), Span("Rewind session — select a message to rewind to (↑↓/j/k to move, Enter to confirm, Esc to cancel):", style="bold")],
                style=ctx.style,
            ))
        if chrome >= 2:
            lines.append(Line(
                spans=[Span("  " + "─" * (ctx.width - 4), style="dim")],
                style=ctx.style,
            ))
        if markers and start > 0:
            lines.append(Line(
                spans=[Span(f"  ↑ {start} more", style="dim")],
                style=ctx.style,
            ))
        for i in range(start, end):
            m = self.messages[i]
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
        if markers and end < n:
            lines.append(Line(
                spans=[Span(f"  ↓ {n - end} more", style="dim")],
                style=ctx.style,
            ))
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
        avail = getattr(ctx, "height", 0) or 0
        n = len(self.sessions)
        # Layout, stated once because it *is* the invariant: the emitted rows may
        # never exceed `avail`. See `window_plan` for the two degenerate cases —
        # `avail == 0` ("no height stated") and a terminal too short for the markers.
        title_rows, start, end, markers = window_plan(avail, n, self.selected_index)
        if title_rows:
            lines.append(Line(
                spans=[Span("○ ", style="dim"), Span("Select a session (↑↓/j/k to move, Enter to confirm, Esc to cancel):", style="bold")],
                style=ctx.style,
            ))
        if markers and start > 0:
            lines.append(Line(
                spans=[Span(f"  ↑ {start} more", style="dim")],
                style=ctx.style,
            ))
        for i in range(start, end):
            s = self.sessions[i]
            sid = s.get("session_id", "?")
            title = s.get("title", "")
            # 显示列表**排序所依据**的时刻（rant 2026-09-30T10:27:20）：行序按最后活动
            # 倒序，就显示最后活动 ⇒ 「越用越往下沉」在行内可见。老 meta 无 updated_at
            # 时回退 created_at。创建时刻仍由 /resume 的提示行给出，信息不丢。
            last_active = (s.get("updated_at") or s.get("created_at", ""))[:16].replace("T", " ")
            msgs = s.get("message_count", 0)
            compacts = s.get("compact_count", 0)
            extra = f" (compacted ×{compacts})" if compacts > 0 else ""
            label = f"  {sid}"
            if title:
                label += f"  [{title}]"
            if self.current_session_id and sid == self.current_session_id:
                label += "  (current)"
            label += f"  |  {last_active}  |  {msgs} msgs{extra}"
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
        if markers and end < len(self.sessions):
            lines.append(Line(
                spans=[Span(f"  ↓ {len(self.sessions) - end} more", style="dim")],
                style=ctx.style,
            ))
        self._dirty = False
        return lines


class ToolSelector(Widget):
    """Interactive tool-detail picker for Tab — arrow-key navigation + detail pane.

    Rant 2026-09-30T09:17:54 (the host's option C). The collapsed chat row is a
    tool's name plus the agent's intent; **this** is where the rest is read.

    It exists because in-place expansion cannot work. The terminal's viewport is
    write-only — `emrg/client/python_tui/terminal.py` draws into native scrollback
    — so a card that has scrolled past cannot be repainted, which is exactly why
    the old Tab handler looked like it did nothing: it expanded the *earliest*
    unexpanded card, and that one is almost always off-screen. A selector
    re-renders on every keystroke and its output lands at the bottom of the
    viewport. That is why the detail is drawn *after* the list rather than beside
    each row, and why the list is windowed: with one line per tool, a session of
    forty calls would push both the selected row and the detail off the top, which
    would reproduce the defect this widget replaces.

    It is the only view from which an older tool's output can be read at all, so
    the detail shows the output to the end rather than a prefix.
    """

    #: Rows the detail block below the list needs at minimum (blank, `tool:`,
    #: `input:`, `output:`, one body row) and the list's own chrome (two header
    #: rows plus the two edge markers).  The list window is what is *left* over,
    #: not a constant: the terminal draws only the tail of what a widget emits,
    #: so a fixed 10 was right for one terminal size and wrong for every other
    #: (rant 2026-10-14T14:09:10).
    LIST_CHROME_ROWS = 4
    DETAIL_MIN_ROWS = 5

    def __init__(self, cards: list[ToolCard] | None = None):
        self.cards: list[ToolCard] = list(cards or [])
        # Open on the most recent call: Tab is pressed about the tool that just
        # ran, and the older ones are reached with ↑ from there.
        self.selected_index: int = max(0, len(self.cards) - 1)
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
        if self.selected_index < len(self.cards) - 1:
            self.selected_index += 1
            self._dirty = True

    @property
    def selected_card(self) -> ToolCard | None:
        if 0 <= self.selected_index < len(self.cards):
            return self.cards[self.selected_index]
        return None

    def window(self, ctx) -> tuple[int, int]:
        """The half-open range of card indices on screen, containing the selection.

        The list's size comes from the context's available height, minus this
        widget's own chrome and the minimum the detail block needs — the detail is
        unbounded by design (it is the only view from which an older tool's output
        can be read), so the list yields to it rather than assuming room.
        """
        n = len(self.cards)
        avail = getattr(ctx, "height", 0) or 0
        size = n if not avail else max(1, avail - self.LIST_CHROME_ROWS - self.DETAIL_MIN_ROWS)
        return windowed_range(n, self.selected_index, size)

    @staticmethod
    def _duration(card: ToolCard) -> str:
        return f"{card.elapsed:.1f}s" if card.elapsed else ""

    def render(self, ctx):
        lines: list[Line] = []
        pstyle = Style.parse("bold cyan")
        lines.append(Line(
            spans=[Span("🔧 ", style="dim"), Span(
                "Tool details (↑↓/j/k to move, Esc to close):", style="bold"
            )],
            style=ctx.style,
        ))
        lines.append(Line(
            spans=[Span("  " + "─" * (ctx.width - 4), style="dim")],
            style=ctx.style,
        ))

        start, end = self.window(ctx)
        if start > 0:
            lines.append(Line(
                spans=[Span(f"  ↑ {start} more", style="dim")],
                style=ctx.style,
            ))
        for i in range(start, end):
            card = self.cards[i]
            duration = self._duration(card)
            label = f"  {card.icon} {card.name}: {card.command}"
            if duration:
                label += f"  ({duration})"
            if i == self.selected_index:
                spans = [Span("> ", style=pstyle), Span(label, style=Style(reverse=True))]
            else:
                spans = [Span("  ", style="dim"), Span(label, style=ctx.style)]
            lines.append(Line(spans=spans, style=ctx.style))
        if end < len(self.cards):
            lines.append(Line(
                spans=[Span(f"  ↓ {len(self.cards) - end} more", style="dim")],
                style=ctx.style,
            ))

        card = self.selected_card
        if card is not None:
            lines.append(Line(spans=[Span("", style=ctx.style)], style=ctx.style))
            lines.append(Line(
                spans=[Span("tool:   ", style="dim"), Span(card.name, style=ctx.style)],
                style=ctx.style,
            ))
            lines.append(Line(
                spans=[Span("input:  ", style="dim")],
                style=ctx.style,
            ))
            for input_line in _format_tool_input(card.arguments):
                lines.append(Line(
                    spans=[Span(f"    {input_line}", style="dim")],
                    style=ctx.style,
                ))
            lines.append(Line(spans=[Span("output: ", style="dim")], style=ctx.style))
            output = card.output or ""
            if not output.strip():
                lines.append(Line(
                    spans=[Span("    (no output yet)", style="dim")],
                    style=ctx.style,
                ))
            else:
                # The whole output, not a prefix: with in-place expansion gone
                # this is the only place a tool's result can be read.
                for output_line in output.split("\n"):
                    lines.append(Line(
                        spans=[Span(f"    {output_line}", style=ctx.style)],
                        style=ctx.style,
                    ))

        self._dirty = False
        return lines


def _format_tool_input(arguments: dict) -> list[str]:
    """A tool call's arguments as readable lines for the detail pane."""
    if not arguments:
        return ["(none)"]
    try:
        text = json.dumps(arguments, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(arguments)
    return text.split("\n")


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
        avail = getattr(ctx, "height", 0) or 0
        n = len(self.projects)
        chrome, start, end, markers = window_plan(avail, n, self.selected_index)
        if chrome:
            lines.append(Line(
                spans=[Span("○ ", style="dim"), Span("Select a project (↑↓/j/k to move, Enter to confirm, Esc to cancel):", style="bold")],
                style=ctx.style,
            ))
        if markers and start > 0:
            lines.append(Line(
                spans=[Span(f"  ↑ {start} more", style="dim")],
                style=ctx.style,
            ))
        for i in range(start, end):
            p = self.projects[i]
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
        if markers and end < n:
            lines.append(Line(
                spans=[Span(f"  ↓ {n - end} more", style="dim")],
                style=ctx.style,
            ))
        self._dirty = False
        return lines


class SandboxSelector(Widget):
    """Interactive sandbox-tier picker — the TUI's `/sandbox` entry point.

    Rant 2026-09-30T09:30:16. The options are the daemon's mode vocabulary, held
    locally only so the list can be drawn without a round trip: picking one sends
    `set_sandbox` to the daemon, which validates it and persists it. Nothing here
    decides a tier — the same rule as the model picker, which also only names a
    choice.
    """

    def __init__(self, modes: list[str] | None = None, current: str = ""):
        self.modes: list[str] = list(modes or [])
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
        if self.selected_index < len(self.modes) - 1:
            self.selected_index += 1
            self._dirty = True

    @property
    def selected_mode(self) -> str | None:
        if 0 <= self.selected_index < len(self.modes):
            return self.modes[self.selected_index]
        return None

    def render(self, ctx):
        lines: list[Line] = []
        pstyle = Style.parse("bold cyan")
        avail = getattr(ctx, "height", 0) or 0
        n = len(self.modes)
        # The list is the daemon's three-tier vocabulary, so this windows for the same
        # reason as the rest and reaches it only on a very short terminal (issue #2248):
        # the rule is the rule, and "too short to matter" is the assumption that made
        # `ToolSelector.WINDOW = 10` look harmless.
        chrome, start, end, markers = window_plan(avail, n, self.selected_index)
        if chrome:
            lines.append(Line(
                spans=[Span("○ ", style="dim"),
                       Span("Select this session's sandbox tier (↑↓/j/k to move, Enter to confirm, Esc to cancel):",
                            style="bold")],
                style=ctx.style,
            ))
        if markers and start > 0:
            lines.append(Line(
                spans=[Span(f"  ↑ {start} more", style="dim")],
                style=ctx.style,
            ))
        for i in range(start, end):
            mode = self.modes[i]
            label = f"  {mode}"
            if mode == self.current:
                label += "  (current)"
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
        if markers and end < n:
            lines.append(Line(
                spans=[Span(f"  ↓ {n - end} more", style="dim")],
                style=ctx.style,
            ))
        self._dirty = False
        return lines


TASK_SELECTOR_TITLES: dict[str, str] = {    # Rant 2026-09-17T18:36:08: the same list serves two commands, and the title
    # is the only thing that tells the host which one they are in the middle of —
    # Enter means "trigger this task" in one and "open this task's session" in
    # the other. Derived from `intent` rather than passed alongside it, so the
    # two can never disagree.
    "trigger": "Select a task to trigger (↑↓/j/k to move, Enter to confirm, Esc to cancel):",
    "session": "Select a task to open its session (↑↓/j/k to move, Enter to confirm, Esc to cancel):",
}


class TaskSelector(Widget):
    """Interactive task picker — arrow-key navigation with highlight.

    Renders a list of scheduled tasks from the server with the selected one
    in reverse video. Used by /trigger and /task-session when invoked without
    arguments; `intent` decides which command the Enter key carries out.
    """

    def __init__(self, tasks: list[dict] | None = None, intent: str = "trigger"):
        if intent not in TASK_SELECTOR_TITLES:
            raise ValueError(
                f"unknown task selector intent {intent!r} — "
                f"expected one of {', '.join(sorted(TASK_SELECTOR_TITLES))}"
            )
        self.tasks: list[dict] = tasks or []
        self.intent: str = intent
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

    @property
    def selected_task(self) -> dict | None:
        """The whole selected row — /task-session needs `project_path` and
        `session_id`, which `selected_task_name` does not carry."""
        if 0 <= self.selected_index < len(self.tasks):
            return self.tasks[self.selected_index]
        return None

    def render(self, ctx):
        lines: list[Line] = []
        pstyle = Style.parse("bold cyan")
        avail = getattr(ctx, "height", 0) or 0
        n = len(self.tasks)
        chrome, start, end, markers = window_plan(avail, n, self.selected_index)
        if chrome:
            lines.append(Line(
                spans=[Span("○ ", style="dim"), Span(TASK_SELECTOR_TITLES[self.intent], style="bold")],
                style=ctx.style,
            ))
        if markers and start > 0:
            lines.append(Line(
                spans=[Span(f"  ↑ {start} more", style="dim")],
                style=ctx.style,
            ))
        for i in range(start, end):
            t = self.tasks[i]
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
        if markers and end < n:
            lines.append(Line(
                spans=[Span(f"  ↓ {n - end} more", style="dim")],
                style=ctx.style,
            ))
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
        avail = getattr(ctx, "height", 0) or 0
        n = len(self.models)
        chrome, start, end, markers = window_plan(avail, n, self.selected_index)
        if chrome:
            lines.append(Line(
                spans=[Span("○ ", style="dim"),
                       Span("Select a model (↑↓/j/k to move, Enter to confirm, Esc to cancel):",
                            style="bold")],
                style=ctx.style,
            ))
        if markers and start > 0:
            lines.append(Line(
                spans=[Span(f"  ↑ {start} more", style="dim")],
                style=ctx.style,
            ))
        for i in range(start, end):
            m = self.models[i]
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
        if markers and end < n:
            lines.append(Line(
                spans=[Span(f"  ↓ {n - end} more", style="dim")],
                style=ctx.style,
            ))
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
    "/sandbox":  "Set this session's sandbox tier, or its extra writable roots "
                 "[/sandbox | /sandbox <mode> | /sandbox add|remove|list <path>]",
    "/trigger":  "List or manually trigger scheduled tasks [/trigger | /trigger <name>]",
    "/task-session": "Open a scheduled task's session (no args = interactive picker)",
    "/skills":   "List loaded skills (user + project)",
    "/version":  "Show EMRG version and instance info",
    "/image":    "Insert clipboard image into input field",
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
            # This is the copy that overflows on this host with no invented fixture at
            # all: `_COMMAND_HELP` holds more entries than a 24-row terminal has room
            # for, so opening `/` used to push the title and the first commands out of
            # the painted tail — with the cursor at index 0, nothing on screen was
            # highlighted (issue #2248).
            avail = getattr(ctx, "height", 0) or 0
            n = len(self._matching)
            chrome, start, end, markers = window_plan(avail, n, self.selected_index)
            if chrome:
                lines.append(Line(
                    spans=[Span("  Commands (↑↓ to select, Enter to confirm, Esc to cancel):", style=dim_style)],
                    style=ctx.style,
                ))
            if markers and start > 0:
                lines.append(Line(
                    spans=[Span(f"  ↑ {start} more", style=dim_style)],
                    style=ctx.style,
                ))
            for i in range(start, end):
                cmd = self._matching[i]
                desc = _COMMAND_HELP.get(cmd, "")
                label = f"  {cmd}"
                if desc:
                    label += f"  —  {desc}"
                if i == self.selected_index:
                    spans = [Span(label, style=sel_style)]
                else:
                    spans = [Span(label, style=cmd_style)]
                lines.append(Line(spans=spans, style=ctx.style))
            if markers and end < n:
                lines.append(Line(
                    spans=[Span(f"  ↓ {n - end} more", style=dim_style)],
                    style=ctx.style,
                ))
        self._dirty = False
        return lines


class ChatHistory(Widget):
    """Chat message list — holds ChatRow and ToolCard widgets."""

    def __init__(self):
        self.rows: list[Widget] = []
        self._line_cache: list[list | None] = []  # row.render() 结果缓存，与 rows 一一对应
        self._dirty = True
        self._cache_height: int | None = None  # 缓存行渲染时的可用高度

    @property
    def dirty(self): return self._dirty
    @dirty.setter
    def dirty(self, v): self._dirty = v

    def add(self, role_or_widget, content=None, timestamp=None):
        """Append a row. `timestamp` is the daemon's moment for the message.

        It is threaded here rather than stamped by the widget: every row's time
        has to be the value the daemon persisted, or a reopen would show a
        different one (rant 2026-10-09T09:25:00).

        Returns the row, so a caller that does not yet know the moment — the row
        it just echoed locally, before the daemon has written the record — can
        set it when the daemon's frame arrives.
        """
        if isinstance(role_or_widget, Widget):
            self.rows.append(role_or_widget)
        elif role_or_widget == "user":
            # Plan B (rant 2026-08-18T18:52:45, superseding 18:50:14): user
            # messages render as markdown (free width wrap, CJK handling)
            # while keeping the "> " prefix + cyan role visual.
            self.rows.append(UserMarkdown(content or "", timestamp=timestamp))
        else:
            self.rows.append(ChatRow(role=role_or_widget, content=content or "",
                                     timestamp=timestamp))
        self._line_cache.append(None)  # 新 row 无缓存
        self._dirty = True
        return self.rows[-1]

    def remove(self, row):
        """Remove a widget from the chat — used for transient UI overlays."""
        try:
            idx = self.rows.index(row)
            self.rows.remove(row)
            if idx < len(self._line_cache):
                del self._line_cache[idx]
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
            if isinstance(row, UserMarkdown):
                row.text = content
                row.dirty = True
                self._dirty = True
                return

    def last_tool_card(self):
        for row in reversed(self.rows):
            if isinstance(row, ToolCard):
                return row
        return None

    def tool_card_by_id(self, tool_call_id: str) -> ToolCard | None:
        """The card for a specific tool call, or None.

        Position is not an identity: concurrent calls stack, and a replay pairs a
        result with the call it answers rather than with whatever came last
        (rant 2026-09-30T09:17:54).
        """
        if not tool_call_id:
            return None
        for row in reversed(self.rows):
            if isinstance(row, ToolCard) and row.tool_call_id == tool_call_id:
                return row
        return None

    def last_markdown(self):
        for row in reversed(self.rows):
            if isinstance(row, StreamingMarkdown):
                return row
        return None

    def render(self, ctx):
        lines = []
        # 防御：缓存长度与 rows 对齐（正常路径 add/remove 已同步维护）
        while len(self._line_cache) < len(self.rows):
            self._line_cache.append(None)
        # A row's line count can depend on the height it was handed — a selector
        # windows itself against the space it has (rant 2026-10-14T14:09:10) — and
        # a height change is not a dirty flag: after a resize or a composer that
        # grew a row, serving the old cache would keep a window sized for a screen
        # the terminal no longer has.  Width needs no such key: the terminal
        # already clears this cache on resize.
        height = getattr(ctx, "height", 0)
        if height != self._cache_height:
            self._line_cache = [None] * len(self.rows)
            self._cache_height = height
        for i, row in enumerate(self.rows):
            if isinstance(row, Widget):
                cached = self._line_cache[i]
                if row.dirty or cached is None:
                    cached = row.render(ctx)  # 仅 dirty 行重算（rant 14:22:06 行级缓存）
                    self._line_cache[i] = cached
                lines.extend(cached)
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
