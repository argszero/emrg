"""Unit tests for the cell buffer and style cascade logic.

Tests the write_lines_to_buffer function which converts rendered Line/Span
objects into cell buffer entries. The style cascade (line.style + span.style)
is the rendering path for message history in the TUI — directly relevant to
the "记录显示高亮不正确" rant about incorrect highlighting.
"""

from __future__ import annotations

from rich.style import Style

from emrg.client.python_tui.buffer import Buffer, Cell, CellWidth, write_lines_to_buffer
from emrg.client.python_tui.widgets.base import Line, Span


def make_line(spans: list[Span], line_style: Style | None = None) -> Line:
    """Helper to create a Line with explicit style."""
    line = Line(spans=spans)
    if line_style is not None:
        line.style = line_style
    return line


def _cell_style_id(buf: Buffer, x: int, y: int) -> int:
    """Extract the style_id from a buffer cell."""
    cell = buf.get_cell(x, y)
    return cell.style_id


def _cell_char(buf: Buffer, x: int, y: int) -> str:
    """Extract the character from a buffer cell."""
    return buf.get_cell(x, y).char


# ── Basic rendering ───────────────────────────────────────────


def test_write_single_span():
    """Single span renders character with correct style."""
    buf = Buffer(width=10, height=2)
    bold = buf.style_pool.intern(Style(bold=True))
    lines = [make_line([Span(text="hello", style=Style(bold=True))])]
    write_lines_to_buffer(buf, lines)

    assert _cell_char(buf, 0, 0) == "h"
    assert _cell_style_id(buf, 0, 0) == bold
    assert _cell_char(buf, 4, 0) == "o"


def test_write_no_span_style():
    """Span with no explicit style gets style_id 0 (default)."""
    buf = Buffer(width=10, height=2)
    lines = [make_line([Span(text="x")])]
    write_lines_to_buffer(buf, lines)

    assert _cell_char(buf, 0, 0) == "x"
    assert _cell_style_id(buf, 0, 0) == 0


# ── Style cascade: line.style + span.style ────────────────────


def test_cascade_line_style_only():
    """When span has no style but line does, line style is used."""
    buf = Buffer(width=10, height=2)
    red_id = buf.style_pool.intern(Style(color="red"))
    # Span with no explicit style (defaults to Style())
    lines = [make_line([Span(text="red")], line_style=Style(color="red"))]
    write_lines_to_buffer(buf, lines)

    assert _cell_style_id(buf, 0, 0) == red_id


def test_cascade_span_overrides_line():
    """Span style takes precedence over line style."""
    buf = Buffer(width=10, height=2)
    blue_bold_id = buf.style_pool.intern(Style(bold=True, color="blue"))

    # line: red, span: bold blue → should be bold blue
    lines = [make_line(
        [Span(text="x", style=Style(bold=True, color="blue"))],
        line_style=Style(color="red"),
    )]
    write_lines_to_buffer(buf, lines)

    assert _cell_style_id(buf, 0, 0) == blue_bold_id


def test_cascade_line_plus_span_merge():
    """When both have disjoint attributes, they merge (line base, span patches)."""
    buf = Buffer(width=10, height=2)
    # line: reverse, span: red → reverse + red
    merged_id = buf.style_pool.intern(Style(reverse=True, color="red"))
    lines = [make_line(
        [Span(text="x", style=Style(color="red"))],
        line_style=Style(reverse=True),
    )]
    write_lines_to_buffer(buf, lines)

    assert _cell_style_id(buf, 0, 0) == merged_id


def test_cascade_both_empty():
    """When both line and span have empty/no style, style_id is 0."""
    buf = Buffer(width=10, height=2)
    lines = [make_line([Span(text="x")])]  # line.style = Style(), span.style = Style()
    write_lines_to_buffer(buf, lines)

    assert _cell_style_id(buf, 0, 0) == 0


def test_cascade_string_style():
    """String style on span is parsed and applied correctly."""
    buf = Buffer(width=10, height=2)
    green_id = buf.style_pool.intern(Style(color="green"))
    lines = [make_line([Span(text="x", style="green")])]
    write_lines_to_buffer(buf, lines)

    assert _cell_style_id(buf, 0, 0) == green_id


def test_cascade_line_none_span_bold():
    """When line.style is None and span has style, span style is used."""
    buf = Buffer(width=10, height=2)
    bold_id = buf.style_pool.intern(Style(bold=True))
    # line_style=None explicitly
    line = Line(spans=[Span(text="x", style=Style(bold=True))])
    line.style = None
    write_lines_to_buffer(buf, [line])

    assert _cell_style_id(buf, 0, 0) == bold_id


def test_cascade_span_none_line_bold():
    """When span style is None and line has style, line style is used."""
    buf = Buffer(width=10, height=2)
    bold_id = buf.style_pool.intern(Style(bold=True))
    line = Line(spans=[Span(text="x")])  # span.style defaults to Style()
    line.spans[0].style = None
    line.style = Style(bold=True)
    write_lines_to_buffer(buf, [line])

    assert _cell_style_id(buf, 0, 0) == bold_id


# ── Multi-line and multi-span ─────────────────────────────────


def test_multi_line_rendering():
    """Multiple lines render at correct y offsets."""
    buf = Buffer(width=10, height=5)
    red_id = buf.style_pool.intern(Style(color="red"))
    blue_id = buf.style_pool.intern(Style(color="blue"))
    lines = [
        make_line([Span(text="a", style=Style(color="red"))]),
        make_line([Span(text="b", style=Style(color="blue"))]),
    ]
    write_lines_to_buffer(buf, lines)

    assert _cell_style_id(buf, 0, 0) == red_id
    assert _cell_char(buf, 0, 0) == "a"
    assert _cell_style_id(buf, 0, 1) == blue_id
    assert _cell_char(buf, 0, 1) == "b"


def test_multi_span_line_style_cascade():
    """Each span in a line gets the line+span style cascade."""
    buf = Buffer(width=20, height=2)
    bold_cyan_id = buf.style_pool.intern(Style(bold=True, color="cyan"))
    dim_id = buf.style_pool.intern(Style(dim=True))

    lines = [make_line([
        Span(text="prefix", style=Style(bold=True, color="cyan")),
        Span(text="body", style=Style(dim=True)),
    ])]
    write_lines_to_buffer(buf, lines)

    # prefix: bold cyan
    assert _cell_style_id(buf, 0, 0) == bold_cyan_id
    assert _cell_char(buf, 0, 0) == "p"
    # body starts at x=6
    assert _cell_style_id(buf, 6, 0) == dim_id
    assert _cell_char(buf, 6, 0) == "b"


def test_start_row_offset():
    """start_row parameter offsets line rendering."""
    buf = Buffer(width=10, height=5)
    lines = [make_line([Span(text="x")])]
    write_lines_to_buffer(buf, lines, start_row=2)

    assert _cell_char(buf, 0, 0) == " "  # row 0: empty
    assert _cell_char(buf, 0, 2) == "x"  # row 2: rendered


def test_truncation_at_buffer_height():
    """Lines beyond buffer height are silently dropped."""
    buf = Buffer(width=10, height=0)
    lines = [make_line([Span(text="x")])]
    # Should not crash
    write_lines_to_buffer(buf, lines)


# ── Wide character handling ───────────────────────────────────


def test_wide_character_rendering():
    """Wide (CJK) characters occupy two cells with SPACER_TAIL."""
    buf = Buffer(width=10, height=2)

    lines = [make_line([Span(text="中文")])]
    write_lines_to_buffer(buf, lines)

    # First char: WIDE
    cell0 = buf.get_cell(0, 0)
    assert cell0.char == "中"
    assert cell0.width == CellWidth.WIDE

    # Second cell: SPACER_TAIL
    cell1 = buf.get_cell(1, 0)
    assert cell1.width == CellWidth.SPACER_TAIL

    # Third char: second CJK char
    cell2 = buf.get_cell(2, 0)
    assert cell2.char == "文"
    assert cell2.width == CellWidth.WIDE


def test_newlines_skipped():
    """Newline characters in span text are not rendered."""
    buf = Buffer(width=10, height=2)
    lines = [make_line([Span(text="a\nb")])]
    write_lines_to_buffer(buf, lines)

    assert _cell_char(buf, 0, 0) == "a"
    assert _cell_char(buf, 1, 0) == "b"
    assert _cell_char(buf, 2, 0) == " "  # no newline rendered
