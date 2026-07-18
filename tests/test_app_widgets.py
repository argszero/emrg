"""Unit tests for app widgets — ProjectSelector navigation and rendering."""

from __future__ import annotations

from emrg.client.python_tui.widgets.base import RenderContext
from emrg.client.app import ProjectSelector


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
    """auto_evolve project shows 🔄, manual shows 💬, repo shown if present."""
    projects = [
        make_project("auto", repo="u/auto", auto_evolve=True),
        make_project("manual", auto_evolve=False),
    ]
    ps = ProjectSelector(projects)
    ctx = RenderContext(width=80)
    lines = ps.render(ctx)

    assert len(lines) == 3  # header + 2 projects

    # First project: auto_evolve → 🔄, with repo
    spans_text_0 = "".join(s.text for s in lines[1].spans)
    assert "🔄" in spans_text_0
    assert "auto" in spans_text_0
    assert "(u/auto)" in spans_text_0

    # Second project: manual → 💬, no repo
    spans_text_1 = "".join(s.text for s in lines[2].spans)
    assert "💬" in spans_text_1
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
