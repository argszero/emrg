"""The style pool's "unknown terminal state" sentinel must force an absolute transition.

The frame writer sends only the cells that changed, and it tracks the terminal's style
so it can emit the *difference* between two cells. Two moments break that model, and at
both of them it sets the sentinel `-1` instead of a style id:

* the start of a frame, before anything has been written;
* every time the cursor moves to a new line — ``x != last_x + 1`` or a new ``y`` — because
  the attributes in force at a fresh row are not something the diff knows.

``-1`` therefore means *the terminal's attributes here are not known*, and a transition
out of it must be **absolute** (reset, then the target style) rather than the empty
string a diff of two equal styles produces.

Resolving the sentinel to the *default* style broke exactly that. Measured on the
2026-10-13 TUI report: with a selected (reverse-video) row directly above an ordinary
row, the frame came out as

    <ESC>[1;1H<ESC>[0m<ESC>[7mSSSSSSSS<ESC>[2;1Hpppppppp

— row 2 is written with **no SGR at all**, so the terminal was still in reverse video
from row 1 and the whole row rendered highlighted. The same mechanism leaves the
*previously* selected row highlighted when the selection moves away: that row's cells
are rewritten as plain spaces while the terminal is still in reverse, because
``transition(-1, 0)`` answered "" and nothing cleared it.

Both arms below are stated in both directions: an unknown state must emit the reset, and
two *known* equal styles must still emit nothing, so the fix cannot be "always emit SGR".
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.style import Style

from emrg.client.python_tui.buffer import (
    Buffer,
    Cell,
    HyperlinkPool,
    StylePool,
    diff_buffers,
    write_lines_to_buffer,
)
from emrg.client.python_tui.output import write_frame
from emrg.client.python_tui.widgets.base import RenderContext
from emrg.client.widgets import SessionSelector

#: The sentinel the frame writer uses for "the terminal's attributes here are unknown".
UNKNOWN = -1

#: An SGR sequence — the only escape that changes character attributes.
SGR = re.compile(r"\x1b\[[0-9;]*m")

REPO = Path(__file__).resolve().parent.parent


def _pool() -> tuple[StylePool, int, int]:
    """A pool with the default style (0) and a reverse-video style interned."""
    pool = StylePool()
    plain = pool.intern(Style())  # already 0, re-interning is idempotent
    reverse = pool.intern(Style(reverse=True))
    assert plain == 0 and reverse != 0, "the pool's two styles did not intern as expected"
    return pool, plain, reverse


# ── the sentinel itself ───────────────────────────────────────────────────────


def test_the_sentinel_does_not_resolve_to_the_default_style() -> None:
    """`-1` is "unknown", not "default" — a different value, or nothing can tell them apart."""
    pool, plain, _ = _pool()
    assert pool.get(UNKNOWN) != pool.get(plain), (
        "the unknown-state sentinel resolves to the default style, so a transition out of "
        "it is indistinguishable from default→default and answers the empty string"
    )
    assert pool.get(UNKNOWN) is None, (
        "the sentinel must resolve to None — the value `style_diff_sgr` already treats as "
        "'no style to diff from' and renders absolutely"
    )


def test_an_unknown_state_followed_by_the_default_style_emits_a_reset() -> None:
    """The defect: this is the transition that clears a leftover reverse video."""
    pool, plain, _ = _pool()
    assert pool.transition(UNKNOWN, plain) == "\x1b[0m", (
        "moving to a fresh row whose first cell is default-styled emitted no SGR, so any "
        "attribute the terminal was still carrying (a selected row's reverse video) "
        "survives into that row"
    )


def test_an_unknown_state_followed_by_a_real_style_is_absolute() -> None:
    """The other side: the target style is sent whole, with no dependence on a prior state."""
    pool, _, reverse = _pool()
    out = pool.transition(UNKNOWN, reverse)
    assert out == "\x1b[0m\x1b[7m", (
        f"an unknown state did not produce a full reset + target style, got {out!r}"
    )


def test_two_known_equal_styles_still_emit_nothing() -> None:
    """The reverse direction — the fix must not become 'always emit an SGR'."""
    pool, plain, reverse = _pool()
    assert pool.transition(plain, plain) == ""
    assert pool.transition(reverse, reverse) == ""


def test_a_known_transition_is_unchanged() -> None:
    """A diff between two *known* styles keeps its own answer."""
    pool, plain, reverse = _pool()
    assert pool.transition(reverse, plain) == "\x1b[0m"


# ── the frame the report came from ────────────────────────────────────────────


def _frame(rows: list[list[int]], width: int, pool: StylePool) -> str:
    """Render `rows` (a style id per cell) as a diff against a blank frame."""
    prev = Buffer(width=width, height=len(rows))
    curr = Buffer(width=width, height=len(rows))
    for y, row in enumerate(rows):
        for x, style_id in enumerate(row):
            curr.set_cell(x, y, Cell(char="p", style_id=style_id))
    return write_frame(diff_buffers(prev, curr), style_pool=pool)


#: Any escape sequence: CSI (SGR and cursor moves alike) or OSC (hyperlinks).
ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _row_marker(y: int) -> str:
    """The cursor move to a 1-based row, as `cursor_to` writes it."""
    return f"\x1b[{y + 1};1H"


def _escapes_right_after(frame: str, marker: str) -> list[str]:
    """The escapes the frame emits immediately after `marker`, before any character.

    This is the window that matters: the writer positions the cursor, then has to say what
    the *style* of that row's first cell is. Reading a wider window would pick up the
    previous row's own style and pass on a frame that never clears it — measured: the
    wider window did exactly that against the unfixed tree.
    """
    rest = frame[frame.index(marker) + len(marker) :]
    out: list[str] = []
    pos = 0
    while pos < len(rest):
        m = ESCAPE.match(rest, pos)
        if m is None:
            break
        out.append(m.group(0))
        pos = m.end()
    return out


def test_a_row_below_a_selected_row_is_not_left_highlighted() -> None:
    """The reported frame: reverse-video row 0, ordinary row 1 — row 1 must be cleared."""
    pool, plain, reverse = _pool()
    frame = _frame([[reverse] * 8, [plain] * 8], 8, pool)
    after = _escapes_right_after(frame, _row_marker(1))
    assert "\x1b[0m" in after, (
        "the writer moved the cursor to the row below the selected one and emitted no SGR, "
        "so the terminal — still in reverse video from the row above — renders that whole "
        f"row highlighted (report 2026-10-13). Escapes after the move: {after!r}. "
        f"Frame: {frame!r}"
    )


def test_a_row_that_is_no_longer_selected_is_cleared() -> None:
    """The report's second symptom: the highlight on the row the selection left behind."""
    pool, plain, reverse = _pool()
    prev = Buffer(width=8, height=2)
    curr = Buffer(width=8, height=2)
    for x in range(8):
        prev.set_cell(x, 0, Cell(char="p", style_id=reverse))
        prev.set_cell(x, 1, Cell(char="p", style_id=plain))
        curr.set_cell(x, 0, Cell(char="p", style_id=plain))
        curr.set_cell(x, 1, Cell(char="p", style_id=reverse))
    frame = write_frame(diff_buffers(prev, curr), style_pool=pool)
    after = _escapes_right_after(frame, _row_marker(0))
    assert "\x1b[0m" in after, (
        "the row the selection moved away from was rewritten with no SGR, so the terminal "
        f"keeps rendering it in reverse (report 2026-10-13). Escapes after the move: "
        f"{after!r}. Frame: {frame!r}"
    )


# ── the sibling trap, one line from the same call site ────────────────────────


def test_the_hyperlink_pool_refuses_the_negative_sentinel_too() -> None:
    """`-1` indexes the *last* entry of a list, so the same lookup shape leaks a stale value.

    The frame writer never passes `-1` to this pool today (a cell's `hyperlink_id` is
    decoded as an unsigned field), so this is latent rather than live — pinned because the
    two pools answer the same question the same way, and the style pool's answer was wrong.
    """
    pool = HyperlinkPool()
    pool.intern("https://example.invalid/first")
    last = pool.intern("https://example.invalid/last")
    assert pool.get(last) == "https://example.invalid/last"
    assert pool.get(UNKNOWN) is None, (
        "the hyperlink pool resolved the -1 sentinel by negative indexing and returned the "
        "last interned URI, which would emit a stale hyperlink"
    )


# ── the whole pipeline, through the real widget ───────────────────────────────

_CSI = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])")

#: Rows of the picker the arms drive — enough that the bled rows are visible as a run.
SESSIONS = 14


class _Screen:
    """A minimal terminal: applies a frame's escapes and remembers each cell's attributes.

    Reverse video is what the report is about, so the model tracks the SGR 7 flag per cell
    across frames — which is exactly the property the writer's diff model gets wrong: it
    assumed a new row starts from the terminal's default attributes, and it does not.
    """

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.cells: list[list[frozenset[int] | None]] = [[None] * width for _ in range(height)]
        self.x = 0
        self.y = 0
        self.attr: set[int] = set()

    def feed(self, frame: str) -> None:
        pos = 0
        while pos < len(frame):
            m = _CSI.match(frame, pos)
            if m is not None:
                params, final = m.group(1), m.group(2)
                if final == "H":
                    nums = [int(p) if p else 1 for p in params.split(";")] or [1]
                    self.y = nums[0] - 1
                    self.x = (nums[1] - 1) if len(nums) > 1 else 0
                elif final == "m":
                    for p in (params.split(";") if params else ["0"]):
                        code = int(p) if p else 0
                        if code == 0:
                            self.attr.clear()
                        elif code == 7:
                            self.attr.add(7)
                        elif code == 27:
                            self.attr.discard(7)
                        else:
                            self.attr.add(code)
                pos = m.end()
                continue
            ch = frame[pos]
            if ch == "\x1b":  # an escape this model does not interpret: skip it whole
                j = pos + 1
                while j < len(frame) and not ("@" <= frame[j] <= "~"):
                    j += 1
                pos = j + 1
                continue
            if 0 <= self.y < self.height and 0 <= self.x < self.width:
                self.cells[self.y][self.x] = frozenset(self.attr)
            self.x += 1
            pos += 1

    def reverse_rows(self) -> list[int]:
        """Rows holding at least one cell the terminal would render in reverse video."""
        return [
            y
            for y in range(self.height)
            if any(c is not None and 7 in c for c in self.cells[y])
        ]


def _sessions(n: int) -> list[dict]:
    return [
        {
            "session_id": f"s{i}",
            "title": f"session {i}",
            "created_at": "2026-10-01T10:00:00",
            "updated_at": f"2026-10-13T{10 + i:02d}:00:00",
            "message_count": i,
        }
        for i in range(n)
    ]


class _Picker:
    """Drives the real `SessionSelector` through the real render pipeline, frame by frame."""

    WIDTH = 120

    def __init__(self, n: int = SESSIONS) -> None:
        self.selector = SessionSelector(_sessions(n))
        self.height = len(_sessions(n)) + 1  # the header line, then one row per session
        self._prev = Buffer(width=self.WIDTH, height=self.height)
        self.screen = _Screen(self.WIDTH, self.height)

    def frame(self) -> str:
        lines = self.selector.render(RenderContext(width=self.WIDTH))
        curr = Buffer(width=self.WIDTH, height=self.height)
        write_lines_to_buffer(curr, lines)
        out = write_frame(diff_buffers(self._prev, curr), style_pool=curr.style_pool)
        self._prev = curr
        self.screen.feed(out)
        return out

    def move_down(self, times: int = 1) -> str:
        for _ in range(times):
            self.selector.move_down()
        return self.frame()


def test_the_picker_highlights_one_row_and_keeps_doing_so() -> None:
    """The frame-level requirement: the reverse rows are the cursor row, and always just it.

    Requirement 2 of the report: drive the picker from index 0 to index 4 and assert the
    old highlight is cleared and the number of reverse rows is a constant 1. Measured on
    the unfixed tree with this same model, the opening frame reads
    `[1, 2, ..., 14]` — every session row, the whole list — and every later frame reads
    the same, so the highlight neither moves nor ever clears. Fixed, it reads `[1]`,
    `[2]`, … `[5]`, and `[4]` after ↑.
    """
    picker = _Picker()
    picker.frame()  # opening frame
    rows = picker.screen.reverse_rows()
    assert rows == [1], (
        f"opening the picker highlighted {len(rows)} row(s) {rows!r} instead of just the "
        "cursor row (row 1, below the header) — the reverse video set by the selected row "
        "leaks into the rows repainted after it"
    )

    for step in range(1, 5):
        picker.move_down()
        rows = picker.screen.reverse_rows()
        assert rows == [1 + step], (
            f"after {step} ↓ the reverse rows are {rows!r}, expected only the cursor row "
            f"{[1 + step]!r} — the highlight left behind on row {step} was never cleared"
        )

    # ↑ back onto a row already visited: same invariant, and it is the row the cursor is on.
    picker.selector.move_up()
    picker.frame()
    assert picker.screen.reverse_rows() == [4], (
        "moving back up left a row highlighted that the cursor is no longer on"
    )
