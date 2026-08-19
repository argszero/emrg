"""Tests for the Composer widget (multi-line input rendering).

rant 2026-08-19T14:25:55: pasted multi-line text must render as one line
per logical line — the first line carries the prompt, continuation lines a
same-width indent, and the cursor is drawn on the line it currently sits
in. (The old single-Line render was flattened by the buffer, which skips
\\n characters → long auto-wrapped blob.)
"""

from __future__ import annotations

from emrg.client.python_tui.widgets.base import RenderContext
from emrg.client.python_tui.widgets.composer import Composer


def _render_text(text: str, cursor: int | None = None) -> list[str]:
    composer = Composer()
    if text:
        composer._text = text
        composer._cursor = len(text) if cursor is None else cursor
    ctx = RenderContext(width=80)
    lines = composer.render(ctx)
    return ["".join(s.text for s in line.spans) for line in lines]


def test_composer_single_line():
    """Single-line input keeps the prompt on the only line."""
    out = _render_text("hello")
    assert len(out) == 1
    assert out[0].startswith("> ")
    assert "hello" in out[0]


def test_composer_multiline_one_line_per_logical_line():
    """Multi-line text renders one Line per logical line (rant 14:25:55)."""
    out = _render_text("line1\nline2\nline3")
    assert len(out) == 3
    assert out[0].startswith("> ")
    assert out[0][2:].strip().startswith("line1")
    # Continuation lines: same-width indent, no prompt symbol
    assert out[1].startswith("  ")
    assert out[1].lstrip().startswith("line2")
    assert out[2].startswith("  ")
    assert out[2].lstrip().startswith("line3")


def test_composer_multiline_no_prompt_on_continuation():
    """Only the first line carries '> '; continuation uses indent only."""
    out = _render_text("a\nb")
    assert out[0].startswith("> ")
    assert out[1].startswith("  ")
    assert not out[1].lstrip().startswith(">")
    assert out[1].strip() == "b"


def test_composer_cursor_mid_first_line():
    """Cursor in the middle of the first line is drawn there."""
    composer = Composer()
    composer._text = "abcd\nef"
    composer._cursor = 2  # between 'ab' and 'cd'
    ctx = RenderContext(width=80)
    lines = composer.render(ctx)
    first = "".join(s.text for s in lines[0].spans)
    # prompt '> ' + prefix 'ab' + cursor 'c' + suffix 'd'
    assert first == "> abcd"
    assert lines[0].spans[2].text == "c"  # the cursor span


def test_composer_cursor_on_second_line():
    """Cursor on a continuation line is drawn there, first line stays plain."""
    composer = Composer()
    composer._text = "ab\ncdef"
    composer._cursor = 3 + 1  # 'a'=0,'b'=1,'\n'=2,'c'=3 → cursor at 'd' (idx 4? no)
    # offsets: a0 b1 \n2 c3 d4 e5 f6 → cursor 4 = 'd'
    composer._cursor = 4
    ctx = RenderContext(width=80)
    lines = composer.render(ctx)
    second = "".join(s.text for s in lines[1].spans)
    # prefix 'c' + cursor 'd' + suffix 'ef'
    assert second.lstrip().startswith("cd")


def test_composer_empty_placeholder():
    """Empty composer renders a single dim prompt line."""
    out = _render_text("")
    assert len(out) == 1
    assert out[0].startswith("> ")
