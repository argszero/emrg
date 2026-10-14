"""Unit tests for app widgets — ProjectSelector and ModelSelector navigation and rendering."""

from __future__ import annotations

import sys
import pytest

# R123 (#401) 后 Windows 已有原生 TUI（Win32Console），但 app.py 交互测试依赖
# 终端输入/信号行为（SIGWINCH/raw mode），Windows CI 冒烟阶段不可靠 → 仍跳过。
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="app 交互测试依赖 POSIX 终端行为（SIGWINCH/raw mode）")

from emrg.client.python_tui.widgets.base import Line, RenderContext, Span, Widget
from emrg.client.app import ProjectSelector, ModelSelector
from emrg.client.widgets import ChatHistory

# ── ProjectSelector tests ──


def make_project(name: str, repo: str = "", auto_evolve: bool = False) -> dict:
    return {"name": name, "repo": repo, "path": f"/tmp/{name}", "auto_evolve": auto_evolve}


def test_project_selector_empty():
    """Empty project list renders header only, selected_project_name is None."""
    ps = ProjectSelector([])
    ctx = RenderContext(width=80)
    lines = ps.render(ctx)

    assert len(lines) == 1  # header only
    assert ps.selected_project_name is None
    assert not ps.dirty


def test_project_selector_single():
    """Single project renders with header + project, initial index 0."""
    ps = ProjectSelector([make_project("foo")])
    ctx = RenderContext(width=80)
    lines = ps.render(ctx)

    assert len(lines) == 2  # header + 1 project
    assert ps.selected_project_name == "foo"
    assert not ps.dirty


def test_project_selector_navigation():
    """move_up/move_down clamp correctly and mark dirty."""
    projects = [make_project("a"), make_project("b"), make_project("c")]
    ps = ProjectSelector(projects)
    assert ps.selected_index == 0

    ps.move_up()
    assert ps.selected_index == 0  # clamped
    assert ps.dirty

    ps.move_down()
    assert ps.selected_index == 1
    ps.move_down()
    assert ps.selected_index == 2
    ps.move_down()
    assert ps.selected_index == 2  # clamped at last

    assert ps.selected_project_name == "c"


def test_project_selector_selected_project_name():
    """selected_project_name returns the correct name at each index."""
    projects = [make_project("x"), make_project("y"), make_project("z")]
    ps = ProjectSelector(projects)
    assert ps.selected_project_name == "x"

    ps.selected_index = 2
    assert ps.selected_project_name == "z"

    ps.selected_index = 99
    assert ps.selected_project_name is None  # out of bounds


def test_project_selector_rendering_indicators():
    """Project name and repo are shown in the selector."""
    projects = [
        make_project("auto", repo="u/auto", auto_evolve=True),
        make_project("manual", auto_evolve=False),
    ]
    ps = ProjectSelector(projects)
    ctx = RenderContext(width=80)
    lines = ps.render(ctx)

    assert len(lines) == 3  # header + 2 projects

    # First project: name and repo
    spans_text_0 = "".join(s.text for s in lines[1].spans)
    assert "auto" in spans_text_0
    assert "(u/auto)" in spans_text_0

    # Second project: name, no repo
    spans_text_1 = "".join(s.text for s in lines[2].spans)
    assert "manual" in spans_text_1


def test_project_selector_selected_highlight():
    """Selected project has reverse video and '>' prefix."""
    projects = [make_project("sel"), make_project("unsel")]
    ps = ProjectSelector(projects)
    ctx = RenderContext(width=80)
    lines = ps.render(ctx)

    # Line 1 is selected (index 0), line 2 is not
    assert lines[1].spans[0].text == "> "  # selected indicator
    assert lines[2].spans[0].text == "  "  # no indicator

    from rich.style import Style
    assert lines[1].spans[1].style == Style(reverse=True)
    assert lines[2].spans[1].style == ctx.style


def test_project_selector_dirty_flag():
    """Dirty flag resets after render, set by navigation."""
    ps = ProjectSelector([make_project("a"), make_project("b")])

    # Initially dirty
    assert ps.dirty
    ps.render(RenderContext(width=80))
    assert not ps.dirty

    # Navigation sets dirty
    ps.move_down()
    assert ps.dirty
    ps.render(RenderContext(width=80))
    assert not ps.dirty

    # Explicit setter works
    ps.dirty = True
    assert ps.dirty


def test_project_selector_no_name_field():
    """Project without 'name' field shows '?'."""
    ps = ProjectSelector([{"path": "/tmp/x"}])
    ctx = RenderContext(width=80)
    lines = ps.render(ctx)

    assert "?" in "".join(s.text for s in lines[1].spans)
    assert ps.selected_project_name == ""  # no name field → ""


# ── ModelSelector tests ──

def make_model(name: str, context_window: int = 131072) -> dict:
    return {"name": name, "context_window": context_window}


def test_model_selector_empty():
    """Empty model list renders header only, selected_model_name is None."""
    ms = ModelSelector([])
    ctx = RenderContext(width=80)
    lines = ms.render(ctx)

    assert len(lines) == 1  # header only
    assert ms.selected_model_name is None
    assert not ms.dirty


def test_model_selector_single():
    """Single model renders with header + model, initial index 0."""
    ms = ModelSelector([make_model("gpt-4")], current="gpt-4")
    ctx = RenderContext(width=80)
    lines = ms.render(ctx)

    assert len(lines) == 2  # header + 1 model
    assert ms.selected_model_name == "gpt-4"
    assert not ms.dirty


def test_model_selector_navigation():
    """move_up/move_down clamp correctly and mark dirty."""
    models = [make_model("a"), make_model("b"), make_model("c")]
    ms = ModelSelector(models)
    assert ms.selected_index == 0

    ms.move_up()
    assert ms.selected_index == 0  # clamped
    assert ms.dirty

    ms.move_down()
    assert ms.selected_index == 1
    ms.move_down()
    assert ms.selected_index == 2
    ms.move_down()
    assert ms.selected_index == 2  # clamped at last

    assert ms.selected_model_name == "c"


def test_model_selector_selected_model_name():
    """selected_model_name returns the correct name at each index."""
    models = [make_model("x"), make_model("y"), make_model("z")]
    ms = ModelSelector(models)
    assert ms.selected_model_name == "x"

    ms.selected_index = 2
    assert ms.selected_model_name == "z"

    ms.selected_index = 99
    assert ms.selected_model_name is None  # out of bounds


def test_model_selector_current_marker():
    """Current model shows ★ current marker."""
    models = [make_model("deepseek-chat"), make_model("gpt-4")]
    ms = ModelSelector(models, current="deepseek-chat")
    ctx = RenderContext(width=80)
    lines = ms.render(ctx)

    assert len(lines) == 3  # header + 2 models
    # First model is current, should have ★ marker
    spans_text_0 = "".join(s.text for s in lines[1].spans)
    assert "★ current" in spans_text_0

    # Second model is not current
    spans_text_1 = "".join(s.text for s in lines[2].spans)
    assert "★ current" not in spans_text_1


def test_model_selector_selected_highlight():
    """Selected model has reverse video and '>' prefix."""
    models = [make_model("sel"), make_model("unsel")]
    ms = ModelSelector(models)
    ctx = RenderContext(width=80)
    lines = ms.render(ctx)

    assert lines[1].spans[0].text == "> "
    assert lines[2].spans[0].text == "  "

    from rich.style import Style
    assert lines[1].spans[1].style == Style(reverse=True)
    assert lines[2].spans[1].style == ctx.style


def test_model_selector_dirty_flag():
    """Dirty flag resets after render, set by navigation."""
    ms = ModelSelector([make_model("a"), make_model("b")])

    assert ms.dirty
    ms.render(RenderContext(width=80))
    assert not ms.dirty

    ms.move_down()
    assert ms.dirty
    ms.render(RenderContext(width=80))
    assert not ms.dirty


def test_model_selector_no_name_field():
    """Model without 'name' field shows '?'."""
    ms = ModelSelector([{"context_window": 65536}])
    ctx = RenderContext(width=80)
    lines = ms.render(ctx)

    assert "?" in "".join(s.text for s in lines[1].spans)
    assert ms.selected_model_name == ""  # no name field → ""


# ── ChatHistory line-cache tests (rant 2026-08-03T14:22:06) ──

class _CountingWidget(Widget):
    """Widget that counts render() invocations to verify line-level caching."""

    def __init__(self, label: str = "x"):
        self.label = label
        self.render_calls = 0
        self._dirty = True

    @property
    def dirty(self): return self._dirty
    @dirty.setter
    def dirty(self, v): self._dirty = v

    def render(self, ctx):
        self.render_calls += 1
        self._dirty = False
        return [Line(spans=[Span(text=self.label)])]


def test_chat_history_line_cache_reuses_clean_rows():
    """Clean rows are NOT re-rendered; only dirty rows re-render."""
    chat = ChatHistory()
    w1, w2, w3 = _CountingWidget("a"), _CountingWidget("b"), _CountingWidget("c")
    chat.add(w1); chat.add(w2); chat.add(w3)
    ctx = RenderContext(width=80)

    lines = chat.render(ctx)
    assert [s.text for s in lines[0].spans] == ["a"]
    assert w1.render_calls == w2.render_calls == w3.render_calls == 1

    # No row dirty → render reuses cache entirely
    lines = chat.render(ctx)
    assert w1.render_calls == 1 and w2.render_calls == 1 and w3.render_calls == 1
    assert len(lines) == 3

    # One row dirty → only that row re-renders
    w2.dirty = True
    lines = chat.render(ctx)
    assert w1.render_calls == 1
    assert w2.render_calls == 2
    assert w3.render_calls == 1
    assert len(lines) == 3


def test_chat_history_line_cache_remove_sync():
    """remove() keeps _line_cache in sync with rows (no stale index)."""
    chat = ChatHistory()
    w1, w2, w3 = _CountingWidget("a"), _CountingWidget("b"), _CountingWidget("c")
    chat.add(w1); chat.add(w2); chat.add(w3)
    chat.render(RenderContext(width=80))
    assert w1.render_calls == 1

    chat.remove(w2)
    lines = chat.render(RenderContext(width=80))
    # w1/w3 are clean (cached), w2 gone — no new renders
    assert w1.render_calls == 1 and w3.render_calls == 1
    assert len(lines) == 2
    assert "".join(s.text for s in lines[0].spans) == "a"
    assert "".join(s.text for s in lines[1].spans) == "c"

    # Removing a row not in the list is a no-op
    chat.remove(_CountingWidget("ghost"))
    assert len(chat.rows) == 2


class _HeightAwareWidget(Widget):
    """A row whose line count depends on the height it is handed — a selector."""

    def __init__(self):
        self.heights: list[int] = []
        self._dirty = True

    @property
    def dirty(self): return self._dirty
    @dirty.setter
    def dirty(self, v): self._dirty = v

    def render(self, ctx):
        self.heights.append(getattr(ctx, "height", 0))
        self._dirty = False
        return [Line(spans=[Span(f"h{h}")]) for h in [getattr(ctx, "height", 0)] * max(1, getattr(ctx, "height", 0) or 1)]


def test_chat_history_re_renders_a_row_when_the_height_changes():
    """A height change is not a dirty flag, so the cache needs its own key.

    A selector windows itself against the space it was given, so serving the cache
    after a resize would keep a window sized for a screen the terminal no longer
    has — the row would be clean, cached, and wrong (rant 2026-10-14T14:09:10).
    """
    chat = ChatHistory()
    row = _HeightAwareWidget()
    chat.add(row)

    tall = chat.render(RenderContext(width=80, height=20))
    assert len(tall) == 20
    assert row.heights == [20]

    # Same height → still cached, no second render
    chat.render(RenderContext(width=80, height=20))
    assert row.heights == [20]

    # Resize → the row re-renders at the new height rather than serving the old one
    short = chat.render(RenderContext(width=80, height=4))
    assert len(short) == 4
    assert row.heights == [20, 4]


# ── Modifier-prefixed CSI arrows (rant 2026-08-21T11:36:56) ─────────────────


def test_csi_alt_arrow_mapping():
    """Option/Ctrl+←/→ map to word movement; unmodified/other keys don't."""
    from emrg.client.app import _csi_modifier_action

    assert _csi_modifier_action(b"\x1b[1;3D") == "word_left"    # Option+←
    assert _csi_modifier_action(b"\x1b[1;3C") == "word_right"   # Option+→
    assert _csi_modifier_action(b"\x1b[1;5D") == "word_left"    # Ctrl+←
    assert _csi_modifier_action(b"\x1b[1;5C") == "word_right"   # Ctrl+→
    assert _csi_modifier_action(b"\x1b[1;7D") == "word_left"    # Alt+Ctrl+←
    # Kitty keyboard protocol: key code 68='D' (Left) / 67='C' (Right)
    assert _csi_modifier_action(b"\x1b[68;3u") == "word_left"
    assert _csi_modifier_action(b"\x1b[67;3u") == "word_right"
    # No-op sequences
    assert _csi_modifier_action(b"\x1b[D") is None       # bare ← (no modifier)
    assert _csi_modifier_action(b"\x1b[1;2D") is None    # Shift+← (mod 2)
    assert _csi_modifier_action(b"\x1b[3~") is None      # Delete key
    assert _csi_modifier_action(b"\x1b[1;3A") is None    # Alt+↑ (not mapped)
    assert _csi_modifier_action(b"abc") is None          # not a CSI sequence


def test_input_widget_move_word():
    """move_word_left/right jump across whitespace-delimited words."""
    from emrg.client.widgets import InputWidget

    w = InputWidget()
    w.text = "foo bar baz"
    w.cursor = len(w.text)
    w.move_word_left()
    assert w.cursor == 8   # start of 'baz'
    w.move_word_left()
    assert w.cursor == 4   # start of 'bar'
    w.move_word_left()
    assert w.cursor == 0   # start of 'foo'
    w.move_word_left()
    assert w.cursor == 0   # already at start — no-op
    w.move_word_right()
    assert w.cursor == 4
    w.move_word_right()
    assert w.cursor == 8
    w.move_word_right()
    assert w.cursor == 11  # end
    w.move_word_right()
    assert w.cursor == 11  # already at end — no-op


# ── SessionSelector tests (rant 2026-09-30T10:27:20) ──


def make_session(sid: str, created: str, updated: str | None = "", title: str = "",
                 msgs: int = 1) -> dict:
    s = {"session_id": sid, "created_at": created, "message_count": msgs, "title": title}
    if updated is not None:
        s["updated_at"] = updated
    return s


def _row_text(lines, index: int) -> str:
    return "".join(sp.text for sp in lines[index].spans)


def test_session_selector_row_shows_last_activity():
    """每行显示最后活动时刻（列表就是按它排序的），而不再是创建时刻。

    Rant 2026-09-30T10:27:20：行序按 updated_at 倒序，行内却显示 created_at ⇒
    「越用越往下沉」在界面上不可见。显示的必须是排序所依据的那一列。
    """
    from emrg.client.widgets import SessionSelector

    sessions = [
        make_session("s_active", "2026-07-27T11:03:00", "2026-09-30T10:26:00"),
        make_session("s_idle", "2026-09-23T09:00:00", "2026-09-23T09:00:00"),
    ]
    ctx = RenderContext(width=120)
    lines = SessionSelector(sessions).render(ctx)

    active = _row_text(lines, 1)
    assert "2026-09-30 10:26" in active, active
    assert "2026-07-27 11:03" not in active, active  # 创建时刻不再占这一列
    assert "2026-09-23 09:00" in _row_text(lines, 2)


def test_session_selector_row_falls_back_to_created_at():
    """老 meta 无 updated_at → 回退 created_at，不留空、不抛错。"""
    from emrg.client.widgets import SessionSelector

    ctx = RenderContext(width=120)
    lines = SessionSelector([make_session("s_old", "2026-05-01T08:30:00", updated=None)]).render(ctx)
    row = _row_text(lines, 1)
    assert "2026-05-01 08:30" in row, row


def test_session_selector_row_prefix_is_empty_string_when_no_updated_at():
    """updated_at 显式为 None/"" 时不得让整行塌成 'None' 文本。"""
    from emrg.client.widgets import SessionSelector

    ctx = RenderContext(width=120)
    lines = SessionSelector([make_session("s_empty", "2026-05-01T08:30:00", updated="")]).render(ctx)
    row = _row_text(lines, 1)
    assert "None" not in row, row
    assert "2026-05-01 08:30" in row, row


# ── ToolCard + ToolSelector (rant 2026-09-30T09:17:54, the host's option C) ──
#
# The defect these pin: Tab expanded tool cards *in place*, and it could not work —
# the terminal's viewport is write-only, so the card the handler picked (the earliest
# unexpanded one) was almost always already in the scrollback and the keystroke
# changed nothing. In-place expansion is gone; the detail lives in a selector that
# re-renders on every keystroke, and with it gone the selector is the **only** way to
# read a tool's output — hence the "whole output, not a prefix" guard below.

from emrg.client.python_tui import ToolCard
from emrg.client.widgets import ToolSelector, _format_tool_input


def _text(line: Line) -> str:
    return "".join(s.text for s in line.spans)


def _rendered(widget, width: int = 80, height: int = 0) -> list[str]:
    return [_text(line) for line in widget.render(RenderContext(width=width, height=height))]


def make_card(name: str = "bash", command: str = "ls -la", status: str = "done",
              output: str = "", tool_call_id: str = "", arguments: dict | None = None,
              elapsed: float = 0.0) -> ToolCard:
    return ToolCard(
        name=name, command=command, status=status, output=output,
        tool_call_id=tool_call_id, arguments=arguments or {}, elapsed=elapsed,
    )


def test_tool_card_row_is_the_name_and_the_command_only():
    """The collapsed row is `icon name: command`, with no expand affordance.

    A chevron would promise an interaction the TUI no longer has — the same defect
    the removed Tab handler was. The card has one line; everything else is read in
    the selector.
    """
    card = make_card()

    assert _rendered(card) == ["✓ bash: ls -la"]


def test_tool_card_status_drives_its_glyph():
    """One icon table, read by the row and by the selector."""
    assert _rendered(make_card(status="failed"))[0].startswith("✗")
    assert _rendered(make_card(status="running"))[0].startswith("◐")
    assert make_card(status="failed").icon == "✗"


def test_tool_card_without_a_command_shows_only_its_name():
    assert _rendered(make_card(command="")) == ["✓ bash"]


def test_tool_card_owns_its_arguments_and_elapsed():
    """The card is the holder of the material the detail pane reads.

    `app.py` used to park the arguments in a separate dict and pop them at
    `tool_end`, so nothing was left on the card to show as input.
    """
    card = make_card(arguments={"command": "ls"}, elapsed=1.25)

    assert card.arguments == {"command": "ls"}
    assert card.elapsed == 1.25
    card.update("done", output="out", elapsed=2.5)
    assert (card.output, card.elapsed) == ("out", 2.5)


def test_tool_selector_opens_on_the_most_recent_call():
    """Tab is pressed about the tool that just ran; older ones are reached with ↑."""
    sel = ToolSelector([make_card(command="one"), make_card(command="two")])

    assert sel.selected_index == 1
    assert sel.selected_card.command == "two"


def test_tool_selector_navigation_stops_at_both_ends():
    sel = ToolSelector([make_card(command="one"), make_card(command="two")])

    sel.move_up(); sel.move_up()
    assert sel.selected_index == 0
    sel.move_down(); sel.move_down(); sel.move_down()
    assert sel.selected_index == 1


def test_tool_selector_shows_all_three_sections_of_the_selected_tool():
    """Name, input and output — the information parity the ruling asked for."""
    sel = ToolSelector([make_card(
        name="bash", command="ls -la", tool_call_id="c1",
        arguments={"command": "ls -la", "workdir": "/tmp"},
        output="file1\nfile2", status="done", elapsed=0.4,
    )])

    text = _rendered(sel)

    assert any("Tool details" in t for t in text)
    assert any("tool:   bash" in t for t in text)
    assert any("input:" in t for t in text)
    assert any('"workdir": "/tmp"' in t for t in text)
    assert any("output:" in t for t in text)
    assert any("file1" in t for t in text)
    assert any("file2" in t for t in text)


def test_tool_selector_shows_the_whole_output_not_a_prefix():
    """The invariant: with in-place expansion gone, this is the only view.

    A truncated detail would leave a tool's result unreadable in the TUI, which is
    what the removed Tab handler already amounted to.
    """
    body = "\n".join(f"line {i}" for i in range(120))
    sel = ToolSelector([make_card(output=body)])

    text = "\n".join(_rendered(sel))

    assert "line 0\n" in text + "\n"
    assert "line 119" in text
    assert "…" not in text.split("output:")[1]


def test_tool_selector_says_when_there_is_no_output_yet():
    sel = ToolSelector([make_card(status="running", output="")])

    assert any("(no output yet)" in t for t in _rendered(sel))


def test_tool_selector_windows_a_long_list_and_keeps_the_selection_on_screen():
    """A list longer than the window must still show the row that is selected.

    Without a window, forty tool calls would push the selected row off the top of
    the viewport — which is exactly the defect the old in-place expansion had.
    The height is stated explicitly: with `height=0` ("not stated") the widget
    renders the whole list rather than inventing a bound it was not given.
    """
    cards = [make_card(command=f"cmd {i}") for i in range(40)]
    sel = ToolSelector(cards)

    # Opens on the last card, so everything hidden is *above* the window.
    lines = _rendered(sel, height=24)
    assert any("↑" in t and "more" in t for t in lines)
    assert not any("↓" in t and "more" in t for t in lines)
    assert any(f"cmd {sel.selected_index}" in t for t in lines)

    # At the top of the list the markers swap sides — and the row is still shown.
    sel.selected_index = 0
    lines = _rendered(sel, height=24)
    assert any("cmd 0" in t for t in lines)
    assert any("↓" in t and "more" in t for t in lines)
    assert not any("↑" in t and "more" in t for t in lines)


def test_tool_selector_list_rows_fit_the_height_it_is_given():
    """The list half is bounded by the height; the detail half is not.

    The detail is deliberately unbounded — it is the only view from which a whole
    tool output can be read (`test_tool_selector_shows_the_whole_output_not_a_prefix`
    pins that) — so the invariant is that the list *yields*, not that the frame fits.
    """
    sel = ToolSelector([make_card(command=f"cmd {i}") for i in range(40)])
    for height in (12, 24, 40):
        for index in (0, 20, 39):
            sel.selected_index = index
            lines = _rendered(sel, height=height)
            list_rows = [t for t in lines if "cmd " in t or "more" in t]
            assert len(list_rows) <= height - ToolSelector.DETAIL_MIN_ROWS, (height, index)
            assert any(f"cmd {index}" in t for t in list_rows)


def test_tool_selector_window_bounds_are_inside_the_list():
    sel = ToolSelector([make_card(command=f"cmd {i}") for i in range(40)])
    ctx = RenderContext(width=80, height=24)
    for index in (0, 1, 20, 38, 39):
        sel.selected_index = index
        start, end = sel.window(ctx)
        assert 0 <= start < end <= 40
        assert start <= index < end


def test_tool_selector_window_follows_the_height_it_is_given():
    """No second magic number: the same terminal height must move both lists.

    `WINDOW = 10` was a constant chosen for one terminal size, which is the
    defect the session list had in a different spelling (rant 2026-10-14T14:09:10).
    """
    sel = ToolSelector([make_card(command=f"cmd {i}") for i in range(60)])
    sel.selected_index = 30
    wide = sel.window(RenderContext(width=80, height=40))
    narrow = sel.window(RenderContext(width=80, height=16))
    assert wide[1] - wide[0] > narrow[1] - narrow[0]
    # the selection still lands inside both
    assert wide[0] <= 30 < wide[1]
    assert narrow[0] <= 30 < narrow[1]


# ── SessionSelector windowing (rant 2026-10-14T14:09:10) ─────────────────
#
# The selector used to emit every row and let the terminal draw the tail, so a
# selection above the fold was not on screen at all — no row, no highlight.
# These assert on the frame the widget emits, with the height the terminal
# actually hands it, because the exit-code-shaped check cannot see this defect:
# the widget was always "correct", it just emitted more than could be shown.


def _rendered_at(widget, height: int, width: int = 120) -> list[Line]:
    return widget.render(RenderContext(width=width, height=height))


def _highlighted(lines: list[Line]) -> list[str]:
    """The text of every row the widget marked as the selection."""
    out = []
    for ln in lines:
        for sp in ln.spans:
            if getattr(sp.style, "reverse", False):
                out.append(sp.text)
    return out


def _twenty_sessions():
    from emrg.client.widgets import SessionSelector
    return SessionSelector([
        make_session(f"s_{i:02d}", "2026-05-01T08:30:00", updated=f"2026-05-{i+1:02d}T09:00:00")
        for i in range(20)
    ])


def _visible(lines: list[Line], height: int) -> list[Line]:
    """The part of an emitted frame the terminal actually paints.

    `terminal.render` shows the **tail**: `chat_lines = all_chat_lines[-max_chat:]`,
    with everything above it pushed into native scrollback, written once and never
    redrawn.  So "the widget emitted the row" is not the property under test — "the
    row is in the tail" is, and it is the one that fails when windowing is removed.
    """
    return lines[-height:] if height else lines


def test_session_selector_keeps_the_selection_on_screen_at_both_ends():
    """Index 0 is the failing case: its row was pushed out of the viewport.

    The assertion is about the *painted* frame, not the emitted one: with 20
    sessions and a 15-row area the tail holds ~13 rows, so the old unbounded
    renderer put session 0 in scrollback with no highlight on screen at all.
    """
    for index in (0, 19):
        sel = _twenty_sessions()
        sel.selected_index = index
        lines = _rendered_at(sel, height=15)
        painted = _visible(lines, 15)
        marked = _highlighted(painted)
        assert marked, f"nothing highlighted at index {index}"
        assert any(f"s_{index:02d}" in t for t in marked), (index, marked)
        # and the highlighted row is one that is actually painted
        assert any(f"s_{index:02d}" in _text(ln) for ln in painted)


def test_session_selector_emits_no_more_lines_than_the_height():
    """The invariant: more rows than the chat area holds are lost silently."""
    sel = _twenty_sessions()
    for height in (3, 8, 15, 24):
        for index in range(20):
            sel.selected_index = index
            lines = _rendered_at(sel, height=height)
            assert len(lines) <= height, (height, index, len(lines))


def test_session_selector_marks_the_rows_it_hides():
    sel = _twenty_sessions()
    sel.selected_index = 19
    text = "\n".join(_text(ln) for ln in _rendered_at(sel, height=15))
    assert "↑ " in text and "more" in text       # rows above the window
    assert "↓ " not in text                      # nothing below the last session

    sel.selected_index = 0
    text = "\n".join(_text(ln) for ln in _rendered_at(sel, height=15))
    assert "↓ " in text and "more" in text
    assert "↑ " not in text


def test_session_selector_without_a_stated_height_renders_the_whole_list():
    """`height=0` means "not stated" — no bound is invented for it."""
    sel = _twenty_sessions()
    lines = _rendered_at(sel, height=0)
    assert len(lines) == 21  # title + every session
    assert len(_highlighted(lines)) == 1


def test_session_selector_still_renders_a_short_list_whole():
    from emrg.client.widgets import SessionSelector
    sel = SessionSelector([make_session("s_only", "2026-05-01T08:30:00")])
    lines = _rendered_at(sel, height=15)
    assert len(lines) == 2  # title + the one session, no markers
    # "more" is the marker's word; the title's own "↑↓/j/k" hint is not a marker
    assert "more" not in "\n".join(_text(ln) for ln in lines)


def test_tool_selector_of_an_empty_session_has_nothing_selected():
    sel = ToolSelector([])

    assert sel.selected_card is None
    assert sel.selected_index == 0
    assert any("Tool details" in t for t in _rendered(sel))


def test_format_tool_input_reads_a_dict_and_admits_an_empty_one():
    assert _format_tool_input({}) == ["(none)"]
    assert '"command": "ls"' in "\n".join(_format_tool_input({"command": "ls"}))
    # An unparsable argument reached the card as `_raw`; it is shown as it came.
    assert "_raw" in "\n".join(_format_tool_input({"_raw": "not json"}))


# ── The Tab wiring, which lives inside the key-handler closure ──────────
#
# `handle_key` and `run_client` are one closure, so the keystroke path cannot be
# called from a test without a terminal and a socket. What is checkable is the
# wiring's *shape* in the source — the same technique `test_session_sandbox.py`
# uses for the tier boundary. It is a weaker guard than a behavioural one and this
# comment is the statement of that limit: it pins that the branch is written the
# way the requirement needs, not that a real keypress reaches it.

from pathlib import Path


def _app_source() -> str:
    return (Path(__file__).resolve().parents[1] / "emrg" / "client" / "app.py").read_text(
        encoding="utf-8"
    )


def test_tab_opens_the_tool_selector_and_no_longer_expands_in_place():
    """Tab's non-`/` branch opens the selector; the old toggle is gone."""
    src = _app_source()
    tab = src.index("# Tab: command completion (when / prefix) or the tool-detail selector")
    branch = src[tab:tab + 2200]

    assert "tool_sel.widget = ToolSelector(tool_cards)" in branch
    # The removed affordance: no consumer may expand a card in place any more.
    assert ".toggle()" not in branch
    assert "tc.expanded" not in branch


def test_the_card_row_has_no_expand_affordance_left():
    """`ToolCard` keeps no state a keystroke could toggle."""
    card_src = (
        Path(__file__).resolve().parents[1]
        / "emrg" / "client" / "python_tui" / "widgets" / "tool_card.py"
    ).read_text(encoding="utf-8")

    assert "def toggle" not in card_src
    assert "self.expanded" not in card_src
    assert "expanded:" not in card_src  # no such dataclass field
    assert "arguments:" in card_src     # the card owns its arguments instead


def test_the_live_result_completes_the_card_found_by_its_call_id():
    """The live half of the pairing, pinned at the seam a unit test cannot reach.

    A mutation that stops `tool_end` from completing its card leaves every other
    guard green — the replay path is tested behaviourally, this one is inside the
    stream handler and needs a socket. So its shape is pinned here: the card is
    found by the daemon's id (`last_tool_card()` stays only as the fallback for a
    stream that lost the start frame), the summaries read that card's arguments,
    and the update carries the duration to the selector.
    """
    src = _app_source()
    branch = src[src.index('if data.get("type") == "tool_end":'):]
    branch = branch[:branch.index('if data.get("type") == "cancelled":')]

    assert "chat.tool_card_by_id(te.tool_call_id)" in branch
    assert "card.update(" in branch            # the card is actually completed,
    assert "card.arguments" in branch          # the summaries read the card,
    assert "tool_args" not in branch           # not a dict that was popped
    assert "elapsed=elapsed" in branch         # and the duration reaches the selector


def test_the_replay_caller_adds_a_card_as_the_widget_it_is():
    """`_replay_rows` returning a card is worth nothing if the caller flattens it.

    The mapping is unit-tested; the one line that puts its card on the chat is not,
    because it needs a terminal. This pins that line's shape.
    """
    src = _app_source()
    branch = src[src.index('elif kind == "tool_card":'):]
    branch = branch[:branch.index("else:")]

    assert "chat.add(content)" in branch


def test_the_tool_selector_closes_on_esc_before_the_turn_cancel_can_see_it():
    """Order is load-bearing: this panel is opened *during* a turn.

    The Esc-interrupt for a busy turn fires before every other dialog, by design.
    The tool panel is a read-only viewer whose whole point is to be opened while a
    turn is running, so if it sat below that check, Esc would kill the turn the
    host was reading about instead of closing the panel.
    """
    src = _app_source()
    panel = src.index("if tool_sel.active and tool_sel.widget:")
    cancel = src.index("# ── ESC interrupt when busy ──")

    assert panel < cancel
    block = src[panel:panel + 1200]
    assert 'data == b"\\x1b"' in block          # Esc …
    assert "tool_sel.active = False" in block   # … closes the panel,
    assert "chat.remove(tool_sel.widget)" in block  # and takes it off the chat



# ── every list selector windows itself (issue #2248) ─────────────────────────
#
# Five more copies of the #2244 defect lived in `emrg/client/widgets.py`: each emitted
# one row per item and let the terminal draw the tail, so a selection above the fold was
# on screen nowhere — no row, no highlight. An ordinary unit test cannot see it, because
# the widget was not "wrong": it emitted every row it had, and the loss happened in the
# terminal. The property is about the *painted* frame, so these read `lines[-avail:]` —
# the tail rule `terminal.render` applies — and both degenerate cases (`height=0`, and a
# terminal too short for the markers) are asserted rather than left implicit.


def _rewind_case(count: int = 40):
    from emrg.client.widgets import RewindSelector
    widget = RewindSelector([
        {"preview": f"msg {i}", "timestamp": "2026-05-01T08:30:00", "record_index": i}
        for i in range(count)
    ])
    return widget, count


def _project_case(count: int = 40):
    from emrg.client.widgets import ProjectSelector
    return ProjectSelector([make_project(f"proj{i}") for i in range(count)]), count


def _task_case(count: int = 40):
    from emrg.client.widgets import TaskSelector
    return TaskSelector(
        [{"name": f"task{i}", "interval": 60, "running": False} for i in range(count)]
    ), count


def _model_case(count: int = 40):
    from emrg.client.widgets import ModelSelector
    return ModelSelector([make_model(f"model{i}") for i in range(count)]), count


def _command_case():
    """No fixture at all: `CommandDropdown` builds its list from `_COMMAND_HELP`.

    This is the case the issue calls out as reproducing with nothing invented — the app's
    own command table is longer than a 24-row terminal's chat area, so opening `/` used to
    push the title and the first commands out of the painted tail.
    """
    from emrg.client.widgets import CommandDropdown, _COMMAND_HELP
    dropdown = CommandDropdown("/")
    dropdown.visible = True
    return dropdown, len(_COMMAND_HELP)


def _sandbox_case():
    """The honest edge case: a realistic list (the daemon's three tiers) that only
    overflows on a terminal with two rows of room for it."""
    from emrg.client.widgets import SandboxSelector
    modes = ["read-only", "workspace-write", "danger-full-access"]
    return SandboxSelector(modes, current="read-only"), len(modes)


# (builder, rows the terminal gives the widget, rows of chrome the widget declares)
CASES = [
    pytest.param(_rewind_case, 15, 2, id="rewind"),
    pytest.param(_project_case, 15, 1, id="project"),
    pytest.param(_task_case, 15, 1, id="task"),
    pytest.param(_model_case, 15, 1, id="model"),
    pytest.param(_command_case, 15, 1, id="command-dropdown"),
    pytest.param(_sandbox_case, 2, 1, id="sandbox-on-a-short-terminal"),
]


def _set(widget, index: int):
    widget.selected_index = index
    widget.dirty = True
    return widget


@pytest.mark.parametrize("builder,avail,chrome_rows", CASES)
def test_every_list_selector_paints_the_selected_row(builder, avail, chrome_rows):
    """The whole point of the window: the selection is on screen at both ends.

    Index 0 is the failing case in every one of these — the old renderer emitted the
    whole list, the terminal painted the tail, and the highlighted row was written into
    scrollback, which is never redrawn.
    """
    widget, n = builder()
    for index in (0, n - 1):
        lines = _set(widget, index).render(RenderContext(width=120, height=avail))
        assert len(_highlighted(lines)) == 1, f"index {index}: not exactly one selection"
        assert _highlighted(_visible(lines, avail)), (
            f"index {index} of {n} is not painted in the {avail} rows the terminal shows "
            f"— its reverse-video row went into scrollback"
        )


@pytest.mark.parametrize("builder,avail,chrome_rows", CASES)
def test_every_list_selector_emits_no_more_rows_than_it_was_given(builder, avail, chrome_rows):
    """The invariant over every index and a spread of heights, including absurd ones."""
    widget, n = builder()
    for height in (1, 2, 3, avail, avail + 4):
        for index in range(n):
            lines = _set(widget, index).render(RenderContext(width=120, height=height))
            assert len(lines) <= height, (height, index, len(lines))


@pytest.mark.parametrize("builder,avail,chrome_rows", CASES)
def test_every_list_selector_without_a_stated_height_renders_the_whole_list(builder, avail, chrome_rows):
    """`height=0` is "no height stated": no bound is invented and no marker is printed."""
    widget, n = builder()
    lines = _set(widget, 0).render(RenderContext(width=120, height=0))
    text = "\n".join(_text(ln) for ln in lines)
    assert " more" not in text, text
    assert len(_highlighted(lines)) == 1
    assert len(lines) >= n, "every item must be drawn when no bound was given"


@pytest.mark.parametrize("builder,avail,chrome_rows", CASES)
def test_every_list_selector_announces_what_it_hides(builder, avail, chrome_rows):
    """Rows left out are announced on the edge they left by — silence is the defect.

    The marker is read as the line it *starts*, not as a substring: the dropdown's own
    title reads "Commands (↑↓ to select, ...)", so a substring probe passes on a widget
    that announces nothing.
    """
    from emrg.client.widgets import window_plan

    widget, n = builder()
    if n <= avail:
        pytest.skip("this list fits the height it is given, so it hides nothing")
    if not window_plan(avail, n, 0, fixed_rows=chrome_rows)[3]:
        pytest.skip(f"{avail} rows cannot afford {chrome_rows} chrome row(s) and a marker")

    def marks(index):
        lines = _set(widget, index).render(RenderContext(width=120, height=avail))
        return [t for t in (_text(ln).strip() for ln in lines) if t[:2] in ("↑ ", "↓ ")]

    at_last = marks(n - 1)
    assert at_last and at_last[0].startswith("↑ "), at_last       # rows above the window
    assert not any(m.startswith("↓ ") for m in at_last), at_last  # nothing below the last

    at_first = marks(0)
    assert at_first and at_first[0].startswith("↓ "), at_first
    assert not any(m.startswith("↑ ") for m in at_first), at_first


@pytest.mark.parametrize("builder,avail,chrome_rows", CASES)
def test_every_list_selector_states_the_number_of_rows_it_is_hiding(builder, avail, chrome_rows):
    """A count that is not the number hidden is worse than no count, and neither edge
    excuses the other.

    The expected set comes from the one producer, with the chrome the widget itself
    declares, and is compared as a *set* rather than one marker at a time — so a widget
    that announces the right number above and a wrong one below fails here too. A widget
    whose chrome changes fails loudly, on purpose.
    """
    from emrg.client.widgets import window_plan

    widget, n = builder()
    checked = 0
    for index in (0, n - 1):
        _, start, end, markers = window_plan(avail, n, index, fixed_rows=chrome_rows)
        if not markers:
            continue
        lines = _set(widget, index).render(RenderContext(width=120, height=avail))
        got = [t for t in (_text(ln).strip() for ln in lines) if t[:2] in ("↑ ", "↓ ")]
        want = ([f"↑ {start} more"] if start else []) + ([f"↓ {n - end} more"] if end < n else [])
        assert got == want, (index, got, want)
        checked += 1
    if not checked:
        pytest.skip("this height cannot afford a marker at either end")


def test_the_hosts_own_command_list_fits_the_tail_on_a_24_row_terminal():
    """The issue's no-fixture reproduction, stated as its own test."""
    from emrg.client.widgets import CommandDropdown, _COMMAND_HELP
    dropdown = CommandDropdown("/")
    dropdown.visible = True
    assert len(_COMMAND_HELP) > 15, "this fixture stopped being longer than the area"

    for index in (0, len(_COMMAND_HELP) - 1):
        lines = _set(dropdown, index).render(RenderContext(width=120, height=15))
        assert _highlighted(_visible(lines, 15)), f"index {index} has no painted highlight"


# ── the one producer of the windowing rule ───────────────────────────────────


def test_the_windowing_rule_has_one_producer():
    """A second copy of this arithmetic is a second chance to lose the selection.

    It was hand-written twice before #2248 (`SessionSelector.window` and
    `ToolSelector.window`). The grep is about the *expression*, not a helper name: the
    same arithmetic under another name is precisely the shape this guards.
    """
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parent.parent / "emrg" / "client" / "widgets.py"
    hits = re.findall(r"\w+\s*-\s*\w+\s*//\s*2", src.read_text(encoding="utf-8"))
    assert len(hits) == 1, f"the window arithmetic is written {len(hits)} times: {hits}"


def test_the_orphaned_session_selector_window_stays_removed():
    """#2248 left `SessionSelector.window` behind with no caller, and its shape was the defect.

    It delegated to `windowed_range` but computed no *plan*: it charged neither the title
    row nor the two edge markers `SessionSelector.render` draws, so a caller that picked it
    up would emit `chrome + 2 + size` rows for a budget of `avail`. That is the "emits more
    rows than the area holds" regression #2244/#2248 exist to prevent, which is why the
    orphan was deleted rather than kept as an unused compatibility API — and why the removal
    is named here, so it cannot come back as one.
    """
    from emrg.client.widgets import SessionSelector, ToolSelector

    assert not hasattr(SessionSelector, "window"), (
        "SessionSelector.window is back: it is a window of rows with no plan, so anything "
        "calling it emits more rows than the height it was handed — lay out through "
        "`window_plan` in `render` instead"
    )
    # The other pre-#2248 copy is a live API, and removing it would be the mistake in the
    # other direction: `ToolSelector.render` and two tests call it, and it subtracts its own
    # chrome and detail rows — that is a plan, not a bare range.
    assert hasattr(ToolSelector, "window")


def test_windowed_range_keeps_the_selection_inside_for_every_size():
    """The property the helper exists to guarantee, over the whole parameter space.

    The empty list is in here on purpose: `min(size, count)` floors at 1, so an
    unguarded helper hands back `(0, 1)` for a list of nothing and the caller's
    `for i in range(start, end)` raises `IndexError` on `items[0]`. That is not
    hypothetical — it is what this test caught in the first draft of #2248.
    """
    from emrg.client.widgets import windowed_range
    for count in (0, 1, 2, 5, 40):
        for size in (0, 1, 2, 3, 10, 40, 100):
            for selected in range(max(1, count)):
                start, end = windowed_range(count, selected, size)
                assert 0 <= start <= end <= count, (count, size, selected, start, end)
                if count:
                    assert start <= selected < end, (count, size, selected, start, end)


def test_window_plan_states_both_degenerate_cases():
    """`avail=0`, and a terminal too short to afford the chrome or the markers."""
    from emrg.client.widgets import window_plan

    # no height stated: the whole list, no markers, the chrome still charged for
    assert window_plan(0, 40, 7) == (1, 0, 40, False)
    # everything fits
    assert window_plan(20, 5, 2) == (1, 0, 5, False)
    # an empty list has nothing to draw, at any height
    assert window_plan(15, 0, 0) == (1, 0, 0, False)
    # a real window, with the markers affordable
    chrome, start, end, markers = window_plan(15, 40, 7)
    assert (chrome, markers) == (1, True) and start <= 7 < end and end - start == 12
    # one row: the chrome yields and the list keeps the row the caller asked for
    assert window_plan(1, 40, 7) == (0, 7, 8, False)
    # two rows: the chrome is paid, and no marker is affordable beside it
    assert window_plan(2, 40, 7) == (1, 7, 8, False)
    # and the emitted rows never exceed what was given, at any height
    for avail in range(0, 30):
        for selected in (0, 3, 39):
            chrome, start, end, markers = window_plan(avail, 40, selected)
            emitted = chrome + (end - start) + (2 if markers else 0)
            assert emitted <= avail or avail == 0, (avail, selected, emitted)
