"""The composer's window: the cursor's row is always drawn (rant 2026-10-14T15:28:56).

The reported defect was a literal bound rather than a window: `terminal.render` kept the
first ten composer lines and dropped the rest, so past the ninth line of input the newest
row — and the cursor sitting on it — was never drawn, while the text stayed in the buffer
and ↑/↓ still reached it.

Two properties are asserted here, and both are what the terminal depends on:
the emitted rows never exceed the budget it was given, and the row holding the cursor is
always among them. `InputWidget._visual_rows` is the row model the arrow keys already use,
so these tests drive the display and the keys through the same list.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout

import pytest

from emrg.client.python_tui.widgets.base import RenderContext
from emrg.client.widgets import InputWidget

# The frame-level tests below mount a real Terminal (raw mode / POSIX terminal paths).
needs_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="Terminal render needs POSIX terminal behaviour"
)


def _composer(text: str, cursor: int | None = None) -> InputWidget:
    w = InputWidget()
    w.text = text
    w.cursor = len(text) if cursor is None else cursor
    return w


def _lines(w: InputWidget, budget: int, width: int = 40) -> list[str]:
    ctx = RenderContext(width=width, height=budget)
    return ["".join(s.text for s in line.spans) for line in w.render(ctx)]


def _cursor_cells(w: InputWidget, budget: int, width: int = 40) -> list[str]:
    """Every reverse-video cell in the frame, in order."""
    ctx = RenderContext(width=width, height=budget)
    return [s.text for line in w.render(ctx) for s in line.spans if s.style.reverse]


def _numbered(n: int) -> str:
    """`n` logical lines, each short enough to be one visual row at width 40."""
    return "\n".join(f"line {i}" for i in range(1, n + 1))


# ── the defect itself ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n", [1, 2, 8, 9, 10, 12, 50])
@pytest.mark.parametrize("budget", [3, 5, 12, 18, 40])
def test_the_cursor_row_is_always_drawn_and_the_count_never_exceeds_the_budget(n, budget):
    """The cursor's row is in the frame, and the frame fits the stated budget.

    This is the reported defect stated as a property: with ten rows of input and the
    cursor on the last of them, the drawn rows used to stop at the ninth.
    """
    w = _composer(_numbered(n))
    lines = _lines(w, budget)

    assert len(lines) <= budget, f"{n} rows of input drew {len(lines)} lines for a {budget}-row budget"
    assert any(f"line {n}" in line for line in lines), (
        f"the cursor sits on input row {n} and it is not drawn: {lines}"
    )
    assert _cursor_cells(w, budget) == [" "], "the cursor's own cell is on the drawn row, exactly once"


@pytest.mark.parametrize("budget", [3, 4, 5, 12, 18])
def test_a_cursor_inside_a_row_reverses_exactly_one_cell(budget):
    """A cursor between characters paints one cell, not a whole row."""
    text = _numbered(12)
    w = _composer(text, cursor=len("line 1\nline 2\nline 3") + 2)  # inside "line 4"
    assert _cursor_cells(w, budget) == ["i"]  # "l" + reversed "i" + "ne 4"
    assert len(_lines(w, budget)) <= budget


@pytest.mark.parametrize("budget", [3, 5, 12])
def test_moving_up_k_rows_shows_the_window_ending_at_row_n_minus_k(budget):
    """↑ and the drawn window agree: the last visible row is the cursor's row.

    `move_up` walks `_visual_rows`, and so does `render`, so the window follows the
    cursor rather than the cursor's row falling out of a window anchored at the top.
    """
    n = 12
    w = _composer(_numbered(n))
    available = 40 - 2  # what app.py hands the widget, `viewport_width - 2`
    for k in (1, 3, 7):
        w.cursor = len(w.text)
        for _ in range(k):
            w.move_up(available)
        lines = _lines(w, budget)
        assert any(f"line {n - k}" in line for line in lines), (
            f"after {k} row(s) up the cursor is on row {n - k} and it is not drawn: {lines}"
        )
        assert len(lines) <= budget


# ── the budget's own contract ────────────────────────────────────────────────────────────


def test_no_stated_budget_draws_the_whole_box():
    """`height == 0` is "not stated": the box renders unbounded rather than inventing a bound."""
    w = _composer(_numbered(20))
    assert not hasattr(RenderContext(width=40), "height") or RenderContext(width=40).height == 0
    lines = _lines(w, 0)
    assert len(lines) == 22, "20 input rows + the two separators, nothing dropped"
    assert [line for line in lines if "line 20" in line]


def test_the_two_separators_are_always_drawn_and_come_out_of_the_budget():
    """Both rules stay (the box never becomes a bare list), and they are inside the budget."""
    for budget in (3, 4, 12, 40):
        lines = _lines(_composer(_numbered(9)), budget)
        assert lines[0].startswith("─") and lines[-1].startswith("─")
        # content rows = budget - the two separators, whenever there is that much to show
        assert len(lines) <= budget


# ── the hint that says rows are hidden ───────────────────────────────────────────────────


def test_hidden_rows_are_announced_on_the_top_rule_and_only_then():
    """A hidden row is said out loud, and saying it costs no row."""
    w = _composer(_numbered(12))
    lines = _lines(w, 12)
    assert lines[0].startswith("─ ↑ 2 more ─"), f"rows 1-2 are hidden: {lines[0]!r}"
    assert len(lines) == 12, "the hint rides the rule that was drawn anyway"

    # Nothing hidden → no hint, and the rule is plain.
    few = _lines(_composer(_numbered(3)), 12)
    assert "more" not in few[0], f"nothing is hidden: {few[0]!r}"
    assert set(few[0]) == {"─"}

    # Scrolled to the top → still nothing hidden above.
    top = _composer(_numbered(12), cursor=0)
    assert "more" not in _lines(top, 12)[0]


def test_the_hint_counts_the_rows_above_the_window():
    """The number is the rows above the window, not a constant."""
    for n, budget in ((12, 12), (12, 8), (50, 12), (30, 5)):
        lines = _lines(_composer(_numbered(n)), budget)
        visible = budget - 2
        hidden = n - visible
        if hidden > 0:
            assert f"↑ {hidden} more" in lines[0], f"{n} rows, {budget} budget → {lines[0]!r}"
        else:
            assert "more" not in lines[0]


def test_the_hint_never_widens_the_rule_beyond_the_region():
    """The rule is the region's width whether or not it carries the hint."""
    for width in (10, 20, 40, 120):
        for n in (3, 12, 50):
            lines = _lines(_composer(_numbered(n)), 8, width=width)
            assert all(len(line) <= width for line in lines), (
                f"width {width}: {[line for line in lines if len(line) > width]!r}"
            )


# ── CJK: the row model is the one the keys use ───────────────────────────────────────────


def test_a_cjk_row_is_wrapped_by_display_width_and_the_cursor_follows_it():
    """A wide character counts two columns, so the window matches what is on screen."""
    text = "長い日本語のテキストです長い日本語のテキストです"
    w = _composer(text, cursor=6)  # 'テ', the 7th character, inside the first visual row
    lines = _lines(w, 6, width=20)
    assert _cursor_cells(w, 6, width=20) == ["テ"]
    assert len(lines) <= 6


class _OneLine:
    """A widget for a one-row region, so the budget's other terms are real."""

    dirty = True

    def render(self, ctx):
        from emrg.client.python_tui.widgets.base import Line, Span

        return [Line(spans=[Span(text=" " * ctx.width)])]


def _painted(term) -> list[tuple[str, bool]]:
    """The frame the terminal actually painted: each row's text and whether it holds a
    reverse-video cell.

    Read from the **front buffer**, not from `_rendered_cache`: the cache keeps what each
    widget returned, while the defect this file is about was applied to the value the
    composer region was drawn from — so a test reading the cache cannot see it (measured:
    restoring `[:10]` left such a test green).
    """
    buf = term._front_buffer
    out = []
    for y in range(buf.height):
        chars = []
        reverse = False
        for x in range(buf.width):
            cell = buf.get_cell(x, y)
            chars.append(cell.char)
            style = buf.style_pool.get(cell.style_id) if cell.style_id else None
            if style is not None and style.reverse:
                reverse = True
        out.append(("".join(chars).rstrip(), reverse))
    return out


def _frame(height: int, text: str) -> list[tuple[str, bool]]:
    from emrg.client.python_tui.terminal import Terminal

    term = Terminal()
    term.caps.height = height
    widget = InputWidget()
    widget.text = text
    widget.cursor = len(text)
    term.mount(composer=widget, status=_OneLine(), chat=_OneLine())
    with redirect_stdout(io.StringIO()):
        term.render(full=True)
    return _painted(term)


@needs_posix
def test_the_painted_frame_holds_the_cursor_row_at_every_height():
    """Frame level: the cursor's row and its cell are on the screen the terminal painted.

    This is the reported defect read where it was reported — off the screen. With ten rows
    of input the composer emitted twelve and the terminal kept the first ten, so the row
    holding the cursor, and with it the cursor, was never painted at any terminal size.
    """
    text = _numbered(40)
    counts = {}
    for height in (13, 24, 45):
        frame = _frame(height, text)
        rows = [i for i, (line, _) in enumerate(frame) if "line 40" in line]
        assert rows, f"h={height}: the row the cursor is on is not on the screen"
        assert frame[rows[0]][1], f"h={height}: no cursor cell painted on that row"
        counts[height] = sum(1 for line, _ in frame if "> line " in line)

    # The window tracks the terminal's height. Anchored on the height and NOT on the
    # function's own answer — a test that recomputes the value under test agrees with any
    # mutation of it (measured: a literal ten survived an earlier version of this test).
    assert counts[13] < counts[24] < counts[45], (
        f"a taller screen must show more of the same input, not the same amount: {counts}"
    )


@needs_posix
def test_the_composer_region_never_exceeds_the_room_the_screen_has():
    """A short terminal gets a short composer, not a region running off the screen."""
    text = _numbered(40)
    for height in range(8, 30):
        term_rooms = max(height - 6, 4)
        painted = sum(1 for line, _ in _frame(height, text) if "> line " in line)
        assert painted + 2 <= term_rooms, (
            f"h={height}: {painted} input rows + 2 rules for a screen that leaves {term_rooms}"
        )


@needs_posix
def test_the_terminals_composer_floor_is_the_widgets_own_floor():
    """Two carriers for one number: the test compares them so they cannot drift.

    `InputWidget` draws both rules plus at least one row of text whatever budget it is
    given, so a stated budget below that floor would be exceeded. The terminal must
    therefore never state one — its floor has to be at least the widget's.
    """
    from emrg.client.python_tui.terminal import Terminal

    assert Terminal.COMPOSER_MIN_ROWS >= InputWidget.SEPARATOR_ROWS + 1
    for height in range(1, 60):
        term = Terminal()
        term.caps.height = height
        budget = term._composer_budget(max(height - 6, 4), 2)
        assert budget >= InputWidget.SEPARATOR_ROWS + 1, (
            f"a {height}-row terminal hands the composer a {budget}-row budget"
        )
        assert len(_lines(_composer(_numbered(40)), budget)) <= budget


@needs_posix
def test_a_long_paste_cannot_squeeze_the_chat_area_off_the_screen():
    """The budget leaves the chat area its floor however long the input grows."""
    from emrg.client.python_tui.terminal import Terminal

    term = Terminal()
    term.caps.height = 24
    max_vp = max(term.caps.height - 6, 4)
    budget = term._composer_budget(max_vp, 2)
    assert max_vp - 2 - budget >= Terminal.MIN_CHAT_ROWS - 1, (
        "the composer's budget must leave the chat area its floor"
    )
