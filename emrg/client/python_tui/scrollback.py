"""Scrollback management — push rendered content above the viewport.

Implements Codex's insert-history pattern:
1. Set scroll region to (1 .. viewport_top)
2. Write content lines with \\r\\n at viewport top
3. Old content scrolls into terminal's native scrollback
4. Reset scroll region, restore cursor

All content pushed to scrollback is natively selectable/copyable.
"""

from __future__ import annotations

from rich.style import Style as RichStyle

from emrg.client.python_tui.output import (
    RESET_SCROLL_REGION,
    SAVE_CURSOR,
    RESTORE_CURSOR,
    cursor_to,
    scroll_region,
    insert_lines,
    style_to_sgr,
)
from emrg.client.python_tui.widgets.base import Line, Span

_SGR_RESET = "\x1b[0m"


def _span_style_to_sgr(style: RichStyle | str) -> str:
    """Convert a Rich Style or style string to SGR.

    Delegates to output.style_to_sgr for Rich Style objects.
    Handles string-style declarations used by some widgets.
    """
    if isinstance(style, str):
        # Convert string-based styles to Rich Style for unified SGR output
        from rich.style import Style
        try:
            rs = Style.parse(style)
            return style_to_sgr(rs)
        except Exception:
            return ""
    if isinstance(style, RichStyle):
        return style_to_sgr(style)
    return ""


def push_lines_to_scrollback(
    lines: list[Line],
    viewport_top: int,
    screen_height: int,
    *,
    write_fn=None,
) -> None:
    """Push rendered lines above the viewport into terminal scrollback.

    Uses Codex's Standard mode: scroll region + insert-lines.
    Content above the viewport scrolls into the terminal's native history.

    Args:
        lines: Rendered content lines to push.
        viewport_top: Row where the viewport starts (0-based).
        screen_height: Total terminal height in rows.
        write_fn: Function to write ANSI output (default: sys.stdout.write).
    """
    import sys

    out = write_fn or sys.stdout.write

    if not lines or viewport_top <= 0:
        return

    scroll_limit = max(1, viewport_top)

    out(SAVE_CURSOR)

    # Set scroll region to area above viewport
    out(scroll_region(1, scroll_limit))

    # Move cursor to bottom of scroll region (just above viewport) and write
    # \r\n before each line: \r\n at the bottom of a scroll region causes
    # the region to scroll up, pushing old content into terminal history.
    # This is Codex's Standard mode — no insert_lines needed.
    out(cursor_to(0, scroll_limit - 1))
    for line in lines:
        out("\r\n")
        for span in line.spans:
            sgr = _span_style_to_sgr(span.style)
            if sgr:
                out(sgr)
                out(span.text)
                out(_SGR_RESET)
            else:
                out(span.text)

    # Reset scroll region to full screen, restore cursor
    out(RESET_SCROLL_REGION)
    out(RESTORE_CURSOR)
    out("\r")

    # Flush to terminal immediately — scrollback push should be visible
    sys.stdout.flush()
