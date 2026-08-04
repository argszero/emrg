"""Unit tests for app widgets — ProjectSelector and ModelSelector navigation and rendering."""

from __future__ import annotations

import sys
import pytest

# TUI (python_tui) 依赖 POSIX-only fcntl/termios/tty——Windows 跳过（Windows 冒烟不跑 TUI）
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="TUI is POSIX-only (fcntl/termios)")

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
