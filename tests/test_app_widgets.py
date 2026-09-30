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


def _rendered(widget, width: int = 80) -> list[str]:
    return [_text(line) for line in widget.render(RenderContext(width=width))]


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
    """
    cards = [make_card(command=f"cmd {i}") for i in range(40)]
    sel = ToolSelector(cards)

    # Opens on the last card, so everything hidden is *above* the window.
    lines = _rendered(sel)
    assert any("↑" in t and "more" in t for t in lines)
    assert not any("↓" in t and "more" in t for t in lines)
    assert any(f"cmd {sel.selected_index}" in t for t in lines)

    # At the top of the list the markers swap sides — and the row is still shown.
    sel.selected_index = 0
    lines = _rendered(sel)
    assert any("cmd 0" in t for t in lines)
    assert any("↓" in t and "more" in t for t in lines)
    assert not any("↑" in t and "more" in t for t in lines)


def test_tool_selector_window_bounds_are_inside_the_list():
    sel = ToolSelector([make_card(command=f"cmd {i}") for i in range(40)])
    for index in (0, 1, 20, 38, 39):
        sel.selected_index = index
        start, end = sel.window()
        assert 0 <= start < end <= 40
        assert start <= index < end


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
