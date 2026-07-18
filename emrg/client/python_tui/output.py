"""ANSI escape sequence generation and terminal output.

Builds on the cell buffer diff to produce minimal ANSI output:
- Cursor positioning (CUP, CUD, CUF, CUB, CHA)
- Style transitions (SGR sequences)
- Character writes with wide-character handling
- BSU/ESU synchronized output (DECSET ?2026)
- Insert-lines for scrollback push (CSI Ps L)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emrg.client.python_tui.buffer import Buffer, Cell


# Terminal control sequences
CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"
CURSOR_HOME = "\x1b[H"
BSU = "\x1b[?2026h"  # Begin Synchronized Update
ESU = "\x1b[?2026l"  # End Synchronized Update
RESET_SCROLL_REGION = "\x1b[r"
SAVE_CURSOR = "\x1b[s"  # DECSC (also \x1b7)
RESTORE_CURSOR = "\x1b[u"  # DECRC (also \x1b8)
CLEAR_SCREEN = "\x1b[2J"
CLEAR_TO_EOL = "\x1b[0K"


@dataclass
class TerminalCapabilities:
    """Detected terminal capabilities."""

    width: int = 80
    height: int = 24
    color_depth: int = 256  # 8, 16, 256, 16777216 (truecolor)
    sync_supported: bool = False  # DECSET ?2026 (BSU/ESU)
    kitty_keyboard: bool = False  # Kitty keyboard protocol
    bracketed_paste: bool = False  # DECSET ?2004
    mouse_sgr: bool = False  # SGR mouse (1006)
    mouse_x10: bool = False  # X10 mouse (9)
    hyperlinks: bool = False  # OSC 8


def cursor_to(x: int, y: int) -> str:
    """CUP: move cursor to (1-based row, 1-based column)."""
    return f"\x1b[{y + 1};{x + 1}H"


def cursor_up(n: int = 1) -> str:
    """CUU: move cursor up n rows."""
    return f"\x1b[{n}A" if n > 1 else "\x1b[A"


def cursor_down(n: int = 1) -> str:
    """CUD: move cursor down n rows."""
    return f"\x1b[{n}B" if n > 1 else "\x1b[B"


def cursor_forward(n: int = 1) -> str:
    """CUF: move cursor forward n columns."""
    return f"\x1b[{n}C" if n > 1 else "\x1b[C"


def cursor_back(n: int = 1) -> str:
    """CUB: move cursor back n columns."""
    return f"\x1b[{n}D" if n > 1 else "\x1b[D"


def cursor_column(col: int) -> str:
    """CHA: move cursor to column (0-based)."""
    return f"\x1b[{col + 1}G"


def scroll_region(top: int, bottom: int) -> str:
    """DECSTBM: set scroll region (1-based, inclusive)."""
    return f"\x1b[{top};{bottom}r"


def insert_lines(n: int = 1) -> str:
    """IL: insert n lines at cursor. Equivalent to CSI Ps L."""
    return f"\x1b[{n}L"


def delete_lines(n: int = 1) -> str:
    """DL: delete n lines at cursor."""
    return f"\x1b[{n}M"


def scroll_up(n: int = 1) -> str:
    """SU: scroll up n lines."""
    return f"\x1b[{n}S"


def scroll_down(n: int = 1) -> str:
    """SD: scroll down n lines."""
    return f"\x1b[{n}T"


def style_to_sgr(style: object) -> str:
    """Convert a Rich Style to an SGR ANSI escape sequence.

    Produces minimal SGR codes for the style's attributes and colors.
    Used by StylePool to compute transition strings between styles.
    """
    from rich.style import Style as RichStyle
    from rich.color import Color as RichColor
    from rich.color_triplet import ColorTriplet

    codes: list[int] = []

    # Reset if style is NoneType or default
    if style is None:
        return "\x1b[0m"

    s: RichStyle = style

    # Bold / Dim
    if s.bold:
        codes.append(1)
    elif s.dim:
        codes.append(2)

    # Italic
    if s.italic:
        codes.append(3)

    # Underline
    if s.underline:
        codes.append(4)

    # Blink
    if s.blink:
        codes.append(5)
    if s.blink2:
        codes.append(6)

    # Reverse
    if s.reverse:
        codes.append(7)

    # Conceal
    if s.conceal:
        codes.append(8)

    # Strikethrough
    if s.strike:
        codes.append(9)

    # Overline
    if s.overline:
        codes.append(53)

    # Foreground color
    if s.color is not None:
        from rich.color import ColorType
        if s.color.type == ColorType.DEFAULT:
            codes.append(39)
        elif s.color.type == ColorType.STANDARD and s.color.number is not None:
            # ANSI 16-color palette (0-7: 30-37, 8-15: 90-97)
            num = s.color.number
            codes.append(30 + num if num < 8 else 90 + (num - 8))
        elif s.color.type == ColorType.EIGHT_BIT and s.color.number is not None:
            codes.append(38)
            codes.append(5)
            codes.append(s.color.number)
        elif s.color.type is not None:
            color = s.color.get_truecolor()
            if color is not None:
                codes.append(38)
                codes.append(2)
                codes.append(color.red)
                codes.append(color.green)
                codes.append(color.blue)

    # Background color
    if s.bgcolor is not None:
        from rich.color import ColorType
        if s.bgcolor.type == ColorType.DEFAULT:
            codes.append(49)
        elif s.bgcolor.type == ColorType.STANDARD and s.bgcolor.number is not None:
            # ANSI 16-color palette (0-7: 40-47, 8-15: 100-107)
            num = s.bgcolor.number
            codes.append(40 + num if num < 8 else 100 + (num - 8))
        elif s.bgcolor.type == ColorType.EIGHT_BIT and s.bgcolor.number is not None:
            codes.append(48)
            codes.append(5)
            codes.append(s.bgcolor.number)
        elif s.bgcolor.type is not None:
            color = s.bgcolor.get_truecolor()
            if color is not None:
                codes.append(48)
                codes.append(2)
                codes.append(color.red)
                codes.append(color.green)
                codes.append(color.blue)

    if not codes:
        return "\x1b[0m"
    return "\x1b[" + ";".join(map(str, codes)) + "m"


def style_diff_sgr(from_style: object, to_style: object) -> str:
    """Compute minimal SGR transition between two Rich Styles.

    If the styles are equivalent, returns empty string (no SGR change needed).
    Otherwise emits SGR reset + to_style SGR to prevent attribute bleed
    (e.g., previous "bold magenta" leaking into next "dim").
    """
    if from_style == to_style:
        return ""
    if to_style is None:
        return "\x1b[0m"
    # Reset before emitting new style — without this, prior bold/color
    # attributes persist into the new style, causing incorrect highlighting.
    to_sgr = style_to_sgr(to_style)
    if to_sgr == "\x1b[0m":
        return to_sgr
    return "\x1b[0m" + to_sgr


def hyperlink_osc8(uri: str | None) -> str:
    """OSC 8 hyperlink sequence."""
    if uri:
        return f"\x1b]8;;{uri}\x1b\\"
    return "\x1b]8;;\x1b\\"


def write_frame(
    diffs: list[tuple[int, int, object, object]],
    style_pool: object | None = None,
    hyperlink_pool: object | None = None,
    sync: bool = False,
) -> str:
    """Convert cell diffs to ANSI output string.

    Args:
        diffs: List of (x, y, prev_cell, curr_cell) from diff_buffers.
        style_pool: StylePool for computing SGR transitions.
        hyperlink_pool: HyperlinkPool for OSC 8 transitions.
        sync: Wrap in BSU/ESU if supported.

    Returns:
        ANSI escape sequence string ready for stdout.write().
    """
    parts: list[str] = []
    if sync:
        parts.append(BSU)
        parts.append(CURSOR_HIDE)

    last_x = -1
    last_y = -1
    last_style_id = -1  # force transition on first diff cell
    last_hyperlink_id = -1
    has_output = False
    row_dirty_end: dict[int, int] = {}  # y → max x changed in that row

    for x, y, prev, curr in diffs:
        # When a WIDE character is at position x, the SPACER_TAIL at x+1
        # must be protected from the trailing CLEAR_TO_EOL cleanup.
        # If row_dirty_end stops at x, the cleanup CUP+EL would target the
        # SPACER_TAIL cell and erase it, breaking the wide character on screen.
        pw = int(getattr(prev, "width", 0))
        cw = int(getattr(curr, "width", 0))
        dirty_end = x + 1 if (pw == 1 or cw == 1) else x
        row_dirty_end[y] = max(row_dirty_end.get(y, 0), dirty_end)

        # SPACER_TAIL detection: wide chars occupy 2 cells (WIDE + SPACER_TAIL).
        # The WIDE cell already advanced the terminal 2 columns, so the
        # SPACER_TAIL cell must not reposition the cursor, write a character,
        # or emit a style transition — it represents the terminal cursor's
        # implicit position, not a cell that needs painting.
        curr_char = getattr(curr, "char", " ")
        is_spacer = cw == 2

        # Cursor positioning
        if not is_spacer:
            if y != last_y or (has_output and x == 0):
                parts.append(cursor_to(x, y))
                last_x = x
                last_y = y
                last_style_id = -1
            elif x != last_x + 1:
                parts.append(cursor_to(x, y))
                last_x = x
            else:
                last_x = x

        # Style transition
        if style_pool is not None and not is_spacer:
            curr_style = getattr(curr, "style_id", 0)
            if curr_style != last_style_id:
                sgr = style_pool.transition(last_style_id, curr_style)
                if sgr:
                    parts.append(sgr)
                last_style_id = curr_style

        # Hyperlink transition
        if hyperlink_pool is not None and not is_spacer:
            curr_link_id = getattr(curr, "hyperlink_id", 0)
            if curr_link_id != last_hyperlink_id:
                curr_uri = hyperlink_pool.get(curr_link_id)
                parts.append(hyperlink_osc8(curr_uri))
                last_hyperlink_id = curr_link_id

        # Write character
        if not is_spacer:
            parts.append(curr_char if curr_char else " ")
            has_output = True

    # Reset style and clear to end of each affected row
    # This eliminates wide-character ghost artifacts (spacer tails, orphan cursors)
    if row_dirty_end:
        if last_style_id > 0:
            parts.append("\x1b[0m")
            last_style_id = 0
        for y in sorted(row_dirty_end.keys(), reverse=True):
            last_col = row_dirty_end[y]
            # Move to one past the last changed cell, erase to end of line
            parts.append(f"\x1b[{y + 1};{last_col + 2}H")
            parts.append(CLEAR_TO_EOL)

    if sync:
        parts.append(CURSOR_SHOW)
        parts.append(ESU)

    return "".join(parts)
