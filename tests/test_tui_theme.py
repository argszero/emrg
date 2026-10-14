"""The TUI's light/dark theme — detection, the palette, and the single remap point.

Rant 2026-10-14T14:48:32 (issue #2246). The reading that matters is taken **from the rendered
output**, not from the constants the palette is written with (issue #2246, requirement 5): the
buffer interns a style and the pool hands it back, so a test that recomputes the ratio from
*that* colour is measuring what the terminal would be told, and one that asserts
``LIGHT_COLORS["cyan"] == "#0b6a72"`` is measuring nothing at all.

Every arm here is mutation-checked: the cycle that wrote it removed the remap, removed the probe's
bound, and made the degrade rule vacuous, and confirmed each turn the matching test red.
"""
from __future__ import annotations

import io
import os
import select
import sys

import pytest
from rich.style import Style as RichStyle

from emrg.client.python_tui import theme
from emrg.client.python_tui.buffer import Buffer, StylePool, write_lines_to_buffer
from emrg.client.python_tui.widgets.base import Line, Span

WHITE = (255, 255, 255)


@pytest.fixture(autouse=True)
def _clean_theme():
    """Every test starts on the default theme and leaves it that way.

    The theme is process-wide state by design (it is resolved once and read on every render), so
    a test that flips it would otherwise leak into whichever test runs next.
    """
    theme.reset_theme()
    yield
    theme.reset_theme()


def _light(background=WHITE):
    theme.set_theme(theme.Theme(theme.LIGHT, background, "config"))


def _span_color(text: str, style) -> tuple[int, int, int]:
    """The colour the **rendered buffer** holds for ``text``, as sRGB.

    This is the frame-level reading: a span goes in, and the colour comes back out of the style
    pool the renderer actually uses — the same path a terminal would be sent.
    """
    buf = Buffer(20, 1)
    pool = StylePool()
    write_lines_to_buffer(buf, [Line(spans=[Span(text, style=style)])], 0, pool)
    stored = pool.get(buf.get_cell(0, 0).style_id)
    assert stored is not None, "the cell carries no style at all"
    assert stored.color is not None, f"the rendered style kept no colour: {stored}"
    return tuple(stored.color.get_truecolor())


# ── requirement 5.1: the contrast arithmetic ─────────────────────────────────────────────────


def test_contrast_ratio_matches_the_wcag_values():
    assert theme.contrast_ratio(WHITE, (0, 0, 0)) == pytest.approx(21.0, abs=0.01)
    assert theme.contrast_ratio(WHITE, WHITE) == pytest.approx(1.0, abs=0.01)
    # order-independent, which is what lets it be called with (colour, background) either way
    assert theme.contrast_ratio((1, 2, 3), WHITE) == theme.contrast_ratio(WHITE, (1, 2, 3))
    assert theme.relative_luminance((0, 0, 0)) == 0.0
    assert theme.relative_luminance(WHITE) == pytest.approx(1.0, abs=1e-9)


def test_is_dark_background_splits_at_the_midpoint():
    assert theme.is_dark_background((0, 0, 0))
    assert not theme.is_dark_background(WHITE)
    assert theme.is_dark_background((64, 64, 64))
    assert not theme.is_dark_background((235, 235, 235))


# ── requirement 5.2: both OSC 11 reply spellings ─────────────────────────────────────────────


@pytest.mark.parametrize("reply,expected", [
    ("\x1b]11;rgb:ffff/ffff/ffff\x1b\\", (255, 255, 255)),
    ("\x1b]11;rgb:0000/0000/0000\x1b\\", (0, 0, 0)),
    ("\x1b]11;rgb:0b0b/6a6a/7272\x1b\\", (11, 106, 114)),
    ("\x1b]11;#ffffff\x1b\\", WHITE),
    ("\x1b]11;#0b6a72\x1b\\", (11, 106, 114)),
    ("\x1b]11;rgb:ffff/ffff/ffff\x07", WHITE),      # BEL-terminated
    ("\x1b]11;#ffffff", WHITE),                      # bare ESC-terminated
])
def test_parse_osc11_accepts_every_spelling(reply, expected):
    assert theme.parse_osc11(reply) == expected


def test_parse_osc11_rejects_what_it_cannot_read():
    assert theme.parse_osc11("") is None
    assert theme.parse_osc11("\x1b]11;?\x1b\\") is None          # the query echoed back
    assert theme.parse_osc11("\x1b]11;rgb:zz/zz/zz\x1b\\") is None
    assert theme.parse_osc11("\x1b]11;rgb:ffff\x1b\\") is None   # too few channels
    # a reply that is not OSC 11 at all must not be read as one
    assert theme.parse_osc11("\x1b]10;rgb:ffff/ffff/ffff\x1b\\") is None


# ── requirement 5.4: COLORFGBG ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value,is_light", [
    ("15;0", False),    # light fg on a dark bg — the common dark spelling
    ("0;15", True),     # dark fg on a white bg
    ("0;7", True),
    ("15;8", False),
    ("", None),
    ("nonsense", None),
    ("0;999", None),    # an index this palette has no colour for
])
def test_parse_colorfgbg(value, is_light):
    rgb = theme.parse_colorfgbg(value)
    if is_light is None:
        assert rgb is None
    else:
        assert rgb is not None
        assert theme.is_dark_background(rgb) is not is_light


def test_detection_prefers_colorfgbg_over_the_probe(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "0;15")
    called = []
    monkeypatch.setattr(theme, "probe_background",
                        lambda *a, **k: called.append(1) or (0, 0, 0))
    assert theme.detected_background() == theme.parse_colorfgbg("0;15")
    assert not called, "the probe must not run when the environment already answered"


def test_detection_probes_when_colorfgbg_says_nothing(monkeypatch):
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setattr(theme, "probe_background", lambda *a, **k: (250, 250, 245))
    assert theme.detected_background() == (250, 250, 245)


# ── requirement 5.5: every light accent, measured from the rendered output ───────────────────


@pytest.mark.parametrize("name", sorted(set(theme.LIGHT_COLORS) - theme.LIGHT_UNUSED))
def test_every_light_colour_clears_aa_against_the_background(name):
    """The ratio is recomputed from the colour the **buffer** stored, not from the constant.

    This is the assertion the rant asks for by name ("do not only assert constants"): if the remap
    were removed, the rendered style would still be the terminal's palette name and this would
    either find no colour at all or find one with the failing ratio.
    """
    _light(WHITE)
    rendered = _span_color("x", name)
    assert theme.contrast_ratio(rendered, WHITE) >= theme.MIN_CONTRAST


@pytest.mark.parametrize("name", sorted(set(theme.LIGHT_COLORS) - theme.LIGHT_UNUSED))
def test_the_light_palette_is_readable_on_an_off_white_background_too(name):
    """A "light" terminal is not necessarily pure white.

    Measured backgrounds in the wild are off-white or pale grey; the palette has to hold there as
    well, which is why its members were chosen with margin rather than at the line.
    """
    background = (230, 230, 230)
    _light(background)
    rendered = _span_color("x", name)
    assert theme.contrast_ratio(rendered, background) >= theme.MIN_CONTRAST


def test_dim_is_replaced_by_a_readable_colour_on_light():
    _light(WHITE)
    buf = Buffer(20, 1)
    pool = StylePool()
    write_lines_to_buffer(buf, [Line(spans=[Span("x", style="dim")])], 0, pool)
    stored = pool.get(buf.get_cell(0, 0).style_id)
    assert stored.dim is False, "the dim attribute survives, and a terminal renders it as a wash"
    assert theme.contrast_ratio(tuple(stored.color.get_truecolor()), WHITE) >= theme.MIN_CONTRAST


def test_a_colour_that_cannot_clear_the_ratio_degrades_to_the_default():
    """The rule that makes "against the background that was really detected" true.

    A mid-grey background cannot carry the light palette; every role must then fall back to the
    terminal's own foreground rather than draw a colour that fails.
    """
    _light((128, 128, 128))
    buf = Buffer(20, 1)
    pool = StylePool()
    write_lines_to_buffer(buf, [Line(spans=[Span("x", style="cyan")])], 0, pool)
    stored = pool.get(buf.get_cell(0, 0).style_id)
    assert stored.color is None, f"a failing colour was kept: {stored}"


def test_light_does_not_use_the_yellow_family_or_white():
    """Both are unusable on a light background: yellow fails the ratio, white is invisible."""
    _light(WHITE)
    for name in sorted(theme.LIGHT_UNUSED | theme.LIGHT_INVISIBLE):
        buf = Buffer(20, 1)
        pool = StylePool()
        write_lines_to_buffer(buf, [Line(spans=[Span("x", style=name)])], 0, pool)
        stored = pool.get(buf.get_cell(0, 0).style_id)
        assert stored.color is None, f"{name} survived on a light background: {stored}"


# ── requirement 5.7: a flip really changes what the same span renders as ─────────────────────


def test_the_same_span_changes_when_the_theme_flips():
    """Both directions of the flip, measured through the buffer each time."""
    dark_before = _span_color("x", "bold cyan")           # default theme: dark
    dark_style_before = _stored_style("bold cyan")

    _light(WHITE)
    after = _span_color("x", "bold cyan")
    style_after = _stored_style("bold cyan")

    assert after != dark_before, "the flip changed nothing about the rendered colour"
    assert style_after.color != dark_style_before.color
    assert theme.contrast_ratio(after, WHITE) >= theme.MIN_CONTRAST

    theme.reset_theme()
    back = _span_color("x", "bold cyan")
    assert back == dark_before, "returning to dark must return the original appearance exactly"


def _stored_style(style) -> RichStyle:
    buf = Buffer(20, 1)
    pool = StylePool()
    write_lines_to_buffer(buf, [Line(spans=[Span("x", style=style)])], 0, pool)
    return pool.get(buf.get_cell(0, 0).style_id)


def test_dark_mode_is_the_identity_for_every_named_colour():
    """Requirement 6: the dark terminal's look must not change — not even in styling detail."""
    theme.reset_theme()
    for name in ["cyan", "green", "magenta", "red", "yellow", "white", "black", "dim",
                 "bold cyan", "bright_red"]:
        parsed = RichStyle.parse(name)
        assert theme.themed_style(parsed) is parsed


# ── requirement 5.3: the probe is bounded and falls back ─────────────────────────────────────


class _FakeIn:
    """Stands in for the input side of a pty: a real fd, reported as a tty."""

    def __init__(self, fd: int):
        self._fd = fd

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd


class _FakeOut:
    """Stands in for stdout: a sink with a real fd, so nothing is echoed back as keystrokes."""

    def __init__(self, fd: int):
        self._fd = fd
        self.written = ""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd

    def write(self, text: str) -> int:
        self.written += text
        return len(text)

    def flush(self) -> None:
        pass


_needs_posix_tty = pytest.mark.skipif(
    sys.platform == "win32", reason="pty/termios are POSIX; the probe degrades to None on Windows"
)


@_needs_posix_tty
def test_the_probe_reads_a_reply_from_a_real_pty():
    """The read path itself, on a real pty — not a mock of the thing under test."""
    master, slave = os.openpty()
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.write(master, b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\")
        out = _FakeOut(devnull)
        rgb = theme.probe_background(500, stdin=_FakeIn(slave), stdout=out)
        assert rgb == WHITE
        # the query really went out, wrapped for whatever multiplexer is (not) in force
        assert "]11;?" in out.written
    finally:
        os.close(master); os.close(slave); os.close(devnull)


@_needs_posix_tty
def test_a_silent_terminal_does_not_hang_the_probe():
    """The bound is the point: a terminal that never answers must not wedge the client.

    The reading is the round trip's own duration — a probe that waited on a blocking read would
    not return at all, which is exactly the failure this guards.
    """
    import time

    master, slave = os.openpty()
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        started = time.monotonic()
        rgb = theme.probe_background(200, stdin=_FakeIn(slave), stdout=_FakeOut(devnull))
        elapsed = time.monotonic() - started
        assert rgb is None
        assert elapsed < 3.0, f"the probe took {elapsed:.1f}s — it is not bounded"
    finally:
        os.close(master); os.close(slave); os.close(devnull)


def test_a_stream_that_is_not_a_tty_is_never_queried():
    out = io.StringIO()
    assert theme.probe_background(50, stdin=io.StringIO(), stdout=out) is None
    assert out.getvalue() == "", "a non-tty must not be written to at all"


@_needs_posix_tty
def test_the_da1_sentinel_ends_the_wait_early():
    """A terminal that answers DA1 but not OSC 11 must not cost the whole timeout.

    The sentinel is what makes the common case — a terminal that does not implement OSC 11 — cheap
    rather than slow, and this is the arm that pins it.
    """
    import time

    master, slave = os.openpty()
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.write(master, b"\x1b[?62;c")  # DA1: "here I am", and no colour follows
        started = time.monotonic()
        rgb = theme.probe_background(2000, stdin=_FakeIn(slave), stdout=_FakeOut(devnull))
        elapsed = time.monotonic() - started
        assert rgb is None
        assert elapsed < 1.0, f"the sentinel did not end the wait: {elapsed:.2f}s"
    finally:
        os.close(master); os.close(slave); os.close(devnull)


def test_the_probe_reads_the_sentinel_reply_that_follows_the_colour(monkeypatch):
    """The colour reply ends the *question*, not the *read*.

    A terminal that writes the two answers separately — Apple Terminal does — leaves the DA1 reply
    behind if the probe returns the moment it parses the colour, and the client's input reader then
    takes that 7-byte ``ESC[?1;2c`` for keystrokes (v0.3.12; host, 2026-10-14T22:39:49). So the
    probe must go on reading until the sentinel has been consumed, and that is what this pins: the
    second chunk is read. The chunks are injected rather than timed, so the reading does not depend
    on how fast a pty delivers two writes.
    """
    chunks = iter([b"\x1b]11;rgb:ffff/ffff/ffff\x07", b"\x1b[?1;2c"])
    read: list[bytes] = []

    def fake_read(fd, n):
        chunk = next(chunks, b"")
        read.append(chunk)
        return chunk

    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        monkeypatch.setattr(theme.os, "read", fake_read)
        monkeypatch.setattr(theme.select, "select", lambda r, w, x, t: (list(r), [], []))
        rgb = theme.probe_background(150, stdin=_FakeIn(fd), stdout=_FakeOut(fd))
    finally:
        os.close(fd)

    assert rgb == WHITE, "the colour reply is still the answer the probe returns"
    assert read == [b"\x1b]11;rgb:ffff/ffff/ffff\x07", b"\x1b[?1;2c"], (
        f"the sentinel reply was left unread: {read!r}"
    )


@_needs_posix_tty
def test_the_probe_leaves_nothing_queued_when_both_answers_arrive():
    """The end-to-end shape on a real pty: asked two questions, queue empty afterwards."""
    master, slave = os.openpty()
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.write(master, b"\x1b]11;rgb:ffff/ffff/ffff\x07\x1b[?1;2c")
        rgb = theme.probe_background(500, stdin=_FakeIn(slave), stdout=_FakeOut(devnull))
        assert rgb == WHITE
        left = b""
        while select.select([slave], [], [], 0)[0]:
            chunk = os.read(slave, 64)
            if not chunk:
                break
            left += chunk
        assert left == b"", f"{left!r} was left for the input reader to take for keystrokes"
    finally:
        os.close(master); os.close(slave); os.close(devnull)


# ── requirement 5.6: the config override beats detection ─────────────────────────────────────


def _config_with(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text("[llm]\napi_key = \"x\"\n" + body, encoding="utf-8")
    return path


def test_configured_mode_reads_the_client_section(tmp_path):
    assert theme.configured_mode(_config_with(tmp_path, '[client]\ntheme = "light"\n')) == "light"
    assert theme.configured_mode(_config_with(tmp_path, '[client]\ntheme = "dark"\n')) == "dark"


@pytest.mark.parametrize("body", [
    "",                                   # no [client] section at all
    "[client]\ntheme = \"chartreuse\"\n",  # not one of the three
    "[client]\nother = 1\n",               # the key is absent
])
def test_a_config_that_does_not_say_falls_back_to_auto(tmp_path, body):
    assert theme.configured_mode(_config_with(tmp_path, body)) == "auto"


def test_a_missing_config_is_auto_rather_than_an_error(tmp_path):
    assert theme.configured_mode(tmp_path / "nope.toml") == "auto"


def test_the_config_override_beats_detection(monkeypatch, tmp_path):
    """`theme = "light"` on a terminal that answers "dark" must still be light — the override is
    the host's, and detection is only the default (the plan the host chose)."""
    monkeypatch.setenv("COLORFGBG", "15;0")           # a dark terminal
    resolved = theme.resolve_theme(_config_with(tmp_path, '[client]\ntheme = "light"\n'),
                                   probe=False)
    assert resolved.mode == theme.LIGHT
    assert resolved.source == "config"

    resolved = theme.resolve_theme(_config_with(tmp_path, '[client]\ntheme = "dark"\n'),)
    assert resolved.mode == theme.DARK


def test_auto_uses_detection_and_nothing_answered_keeps_dark(monkeypatch, tmp_path):
    cfg = _config_with(tmp_path, "")
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert theme.resolve_theme(cfg, probe=False).mode == theme.LIGHT

    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setattr(theme, "probe_background", lambda *a, **k: None)
    resolved = theme.resolve_theme(cfg, probe=False)
    assert resolved.mode == theme.DARK, "an unmeasurable background must keep today's appearance"
    assert resolved.background is None


def test_ensure_theme_probes_once_per_process(monkeypatch, tmp_path):
    """Requirement 1's "resolve once, never per render" — counted, not assumed."""
    calls = []

    def _counting(*a, **k):
        calls.append(1)
        return None

    monkeypatch.setenv("COLORFGBG", "")
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setattr(theme, "probe_background", _counting)
    theme.reset_theme()

    first = theme.ensure_theme(tmp_path / "none.toml")
    second = theme.ensure_theme(tmp_path / "none.toml")
    assert len(calls) == 1, f"the terminal was queried {len(calls)} times"
    assert first is second


# ── requirement 4: the code-block theme follows the terminal ─────────────────────────────────


def test_code_blocks_follow_the_theme():
    assert theme.code_theme() == "monokai"
    _light(WHITE)
    assert theme.code_theme() == "friendly"


def test_the_code_theme_name_is_one_rich_and_pygments_actually_have():
    """A name neither library knows would raise at the first fenced block, not here."""
    from pygments.styles import get_all_styles

    available = set(get_all_styles())
    theme.reset_theme()
    assert theme.code_theme() in available
    _light(WHITE)
    assert theme.code_theme() in available
