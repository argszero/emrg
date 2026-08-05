"""Unit tests for ANSI escape sequence generation (output.py).

Tests style_to_sgr and style_diff_sgr — the core functions responsible for
terminal color/highlight rendering. Directly relevant to the "记录显示高亮
不正确" rant about incorrect highlight display in the TUI.
"""

from __future__ import annotations

from rich.style import Style

from emrg.client.python_tui.output import style_diff_sgr, style_to_sgr


# ── style_to_sgr ──────────────────────────────────────────────


def test_style_to_sgr_none():
    """None style produces reset."""
    assert style_to_sgr(None) == "\x1b[0m"


def test_style_to_sgr_default():
    """Empty Rich Style produces reset (no attributes)."""
    assert style_to_sgr(Style()) == "\x1b[0m"


def test_style_to_sgr_bold():
    """Bold attribute → SGR 1."""
    assert style_to_sgr(Style(bold=True)) == "\x1b[1m"


def test_style_to_sgr_dim():
    """Dim attribute → SGR 2."""
    assert style_to_sgr(Style(dim=True)) == "\x1b[2m"


def test_style_to_sgr_italic():
    """Italic → SGR 3."""
    assert style_to_sgr(Style(italic=True)) == "\x1b[3m"


def test_style_to_sgr_underline():
    """Underline → SGR 4."""
    assert style_to_sgr(Style(underline=True)) == "\x1b[4m"


def test_style_to_sgr_reverse():
    """Reverse video → SGR 7."""
    assert style_to_sgr(Style(reverse=True)) == "\x1b[7m"


def test_style_to_sgr_strikethrough():
    """Strikethrough → SGR 9."""
    assert style_to_sgr(Style(strike=True)) == "\x1b[9m"


def test_style_to_sgr_fg_standard():
    """ANSI 16-color foreground (red=1 → 31)."""
    assert style_to_sgr(Style(color="red")) == "\x1b[31m"


def test_style_to_sgr_fg_bright():
    """Bright ANSI color (bright_red=9 → 91)."""
    assert style_to_sgr(Style(color="bright_red")) == "\x1b[91m"


def test_style_to_sgr_bg_standard():
    """ANSI 16-color background (blue=4 → 44)."""
    assert style_to_sgr(Style(bgcolor="blue")) == "\x1b[44m"


def test_style_to_sgr_combined():
    """Multiple attributes combined (bold + reverse + red)."""
    sgr = style_to_sgr(Style(bold=True, reverse=True, color="red"))
    assert "\x1b[1" in sgr
    assert "7" in sgr
    assert "31" in sgr
    assert sgr.startswith("\x1b[")
    assert sgr.endswith("m")


def test_style_to_sgr_bold_dim_conflict():
    """bold=True takes precedence over dim=True (Rich behavior)."""
    sgr = style_to_sgr(Style(bold=True, dim=True))
    assert "1" in sgr
    assert "2" not in sgr


# ── style_diff_sgr ────────────────────────────────────────────


def test_diff_both_none():
    """No transition needed between two None styles."""
    assert style_diff_sgr(None, None) == ""


def test_diff_same_default():
    """No transition needed between identical default styles."""
    assert style_diff_sgr(Style(), Style()) == ""


def test_diff_same_bold():
    """No SGR emitted when from==to (bold→bold)."""
    assert style_diff_sgr(Style(bold=True), Style(bold=True)) == ""


def test_diff_same_complex():
    """No SGR emitted when styles are identical (bold+reverse+red)."""
    s = Style(bold=True, reverse=True, color="red")
    assert style_diff_sgr(s, s) == ""


def test_diff_none_to_bold():
    """Transition from None to bold: reset + bold."""
    assert style_diff_sgr(None, Style(bold=True)) == "\x1b[0m\x1b[1m"


def test_diff_default_to_bold():
    """Transition from default to bold: reset + bold."""
    assert style_diff_sgr(Style(), Style(bold=True)) == "\x1b[0m\x1b[1m"


def test_diff_bold_to_red():
    """Transition bold→red: reset + red (bold attribute must not bleed)."""
    result = style_diff_sgr(Style(bold=True), Style(color="red"))
    assert result == "\x1b[0m\x1b[31m"


def test_diff_to_none():
    """Transition to None: reset only (no trailing SGR)."""
    assert style_diff_sgr(Style(bold=True), None) == "\x1b[0m"


def test_diff_to_default_style():
    """Transition to default Style that maps to reset: single reset."""
    # Style() produces "\x1b[0m" in style_to_sgr
    result = style_diff_sgr(Style(bold=True), Style())
    assert result == "\x1b[0m"
    # Must not double-reset: "\x1b[0m\x1b[0m" would be wrong
    assert result.count("\x1b[0m") == 1


def test_diff_complex_to_complex():
    """Transition between complex styles: full reset + new style."""
    from_s = Style(bold=True, color="blue")
    to_s = Style(dim=True, color="red")
    result = style_diff_sgr(from_s, to_s)
    assert result == "\x1b[0m\x1b[2;31m"


def test_diff_reverse_to_plain():
    """Reverse video must be properly cleared on transition (rant: highlight bug)."""
    from_s = Style(reverse=True, color="white")
    to_s = Style(color="green")
    result = style_diff_sgr(from_s, to_s)
    # Must reset (clears reverse) then apply green
    assert result.startswith("\x1b[0m")
    assert "32" in result  # green
    assert "7" not in result  # reverse must be gone


def test_diff_bold_to_bold_red():
    """Bold→bold+red: reset + bold+red (bold must not leak from prior)."""
    from_s = Style(bold=True)
    to_s = Style(bold=True, color="red")
    result = style_diff_sgr(from_s, to_s)
    # Since styles differ, we get reset followed by the full to-style
    assert result.startswith("\x1b[0m")
    assert "1" in result
    assert "31" in result


def test_diff_empty_string_on_equivalent_styles():
    """Style objects with different identity but equal semantics → no SGR."""
    a = Style(bold=True, color="red")
    b = Style(bold=True, color="red")
    assert a is not b  # different objects
    assert style_diff_sgr(a, b) == ""  # but semantically equal → no transition
