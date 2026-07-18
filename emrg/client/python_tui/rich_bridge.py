"""Bridge: convert Rich RenderableType to cell buffer entries.

Rich produces `list[Segment]` (styled substrings) from its rendering pipeline.
Our cell buffer works with `list[Span]` and integer style IDs from StylePool.

This module converts between the two representations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import RenderableType
from rich.segment import Segment, Segments
from rich.style import Style

from emrg.client.python_tui.widgets.base import Line as TUILine, Span as TUISpan

if TYPE_CHECKING:
    from emrg.client.python_tui.buffer import StylePool


def segments_to_spans(
    segments: list[Segment] | Segments,
    pool: StylePool | None = None,
) -> list[TUISpan]:
    """Convert Rich segments to TUI spans.

    Each Rich Segment is a (text, style, control?) tuple.
    We extract the text and style, creating TUI Span objects.

    If a StylePool is provided, styles are interned for O(1) comparison.
    """
    spans: list[TUISpan] = []
    for seg in segments:
        if seg.text:
            style = seg.style if seg.style else Style()
            spans.append(TUISpan(text=seg.text, style=style))
    return spans


def segments_to_lines(
    segments: list[Segment] | Segments,
    width: int,
    pool: StylePool | None = None,
) -> list[TUILine]:
    """Convert Rich segments to TUI lines, splitting at newlines and wrapping.

    Args:
        segments: Rich formatted segments.
        width: Available width for line wrapping.
        pool: Optional style pool for interning.

    Returns:
        List of TUI Line objects suitable for cell buffer rendering.
    """
    lines: list[TUILine] = []
    current_line = TUILine()

    for seg in segments:
        if seg.text is None:
            continue

        lines_in_seg = seg.text.split("\n")
        style = seg.style if seg.style else Style()

        for i, part in enumerate(lines_in_seg):
            if i > 0:
                # Newline: flush current line, start new
                if current_line.spans:
                    lines.append(current_line)
                current_line = TUILine()

            if part:
                current_line.spans.append(TUISpan(text=part, style=style))

    # Flush last line
    if current_line.spans:
        lines.append(current_line)

    return lines


def rich_renderable_to_lines(
    renderable: RenderableType,
    width: int,
    pool: StylePool | None = None,
) -> list[TUILine]:
    """Render a Rich RenderableType to TUI lines.

    Uses Rich's segment rendering pipeline to produce styled segments,
    then converts to our internal Line/Span format.

    Args:
        renderable: Any Rich renderable (Markdown, Syntax, Table, Panel, etc.).
        width: Available terminal width.
        pool: Optional style pool for interning styles.

    Returns:
        List of TUI Line objects.
    """
    from rich.console import Console as RichConsole

    console = RichConsole(
        width=width,
        force_terminal=True,
        color_system="truecolor",
    )

    # Use Rich's render() which returns list[Segment] — not capture() which returns str
    segments: list[Segment] = console.render(renderable)
    return segments_to_lines(segments, width, pool)
