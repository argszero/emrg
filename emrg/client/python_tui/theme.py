"""Terminal background detection and the light/dark palettes.

Rant 2026-10-14T14:48:32 (issue #2246): every colour the TUI draws was a *terminal palette name*
(``"bold cyan"``, ``"green"``, ``"dim"`` …), so the terminal decided what it actually was. That is
right on a dark theme and fails on every light one — measured against white, cyan came out at
2.96:1, green 2.48:1, magenta 3.89:1 and dim grey 3.69:1, where AA body text needs 4.5:1.

This module holds the two things that fix it and keeps them in one place:

  * **what the background is** — ``COLORFGBG`` when the terminal sets it (free), otherwise an OSC 11
    query with a bounded timeout and a sentinel so a silent terminal cannot hang the client, and
    ``None`` when neither answers, which means "keep the dark appearance" rather than a guess;
  * **what a named colour becomes** — a light palette whose members are chosen to clear 4.5:1
    against the background that was *really detected*, with a role degrading to the terminal's
    default foreground when it cannot.

The remap is applied from exactly one place, ``buffer.py::_normalize_style``: every span and line
style in the client already funnels through it, so no widget knows a theme exists (issue #2246,
requirement 2).

Nothing here changes what a dark terminal looks like: in dark mode ``themed_style`` returns the
style it was handed, unchanged.
"""
from __future__ import annotations

import os
import select
import sys
from dataclasses import dataclass
from typing import Optional

# ── the modes ────────────────────────────────────────────────────────────────────────────────

DARK = "dark"
LIGHT = "light"
AUTO = "auto"
THEME_MODES = (AUTO, DARK, LIGHT)

#: The host's decision (rant 2026-10-14T14:48:32, taken at 14:47): detection first, a setting
#: overrides it. `auto` is therefore the default both here and in `[client] theme`.
DEFAULT_MODE = AUTO

# ── the light palette ────────────────────────────────────────────────────────────────────────
#
# Each colour is measured against a *white* background here; the ratio that matters is recomputed
# at runtime against the background the probe actually returned, and a role that cannot reach
# ``MIN_CONTRAST`` against that one degrades to the terminal default (``themed_style``). The
# numbers are not repeated in this comment — the tests recompute them from these values
# (`tests/test_tui_theme.py::test_every_light_colour_clears_aa_against_the_background`), because a
# ratio written in prose goes stale the moment a colour moves.
LIGHT_COLORS = {
    "cyan": "#0b6a72",
    "bright_cyan": "#0b6a72",
    "green": "#0a6b2d",
    "bright_green": "#0a6b2d",
    "magenta": "#a1149b",
    "bright_magenta": "#a1149b",
    "red": "#b3261e",
    "bright_red": "#b3261e",
    "blue": "#1a5fb4",
    "bright_blue": "#1a5fb4",
    "bright_black": "#5c5c5c",
    # `black` is left alone: it is the most readable foreground a light background has.
}

#: What ``dim`` becomes on a light background. ``dim`` is a rich *attribute*, not a colour, but a
#: terminal renders it as a washed-out grey — measured 3.69:1 against white, which is the same
#: failure as the named colours. On light it is replaced by this colour and the attribute dropped.
LIGHT_DIM = "#5c5c5c"

#: Not used on a light background. Every yellow that keeps its hue fails the ratio, and the host's
#: rule is to drop the family rather than reinterpret it as an amber (issue #2246, requirement 2).
#: A role asking for one degrades to the terminal's default foreground.
LIGHT_UNUSED = frozenset({"yellow", "bright_yellow"})

#: ``white`` on a light background is invisible text. Degraded by the same rule as yellow.
LIGHT_INVISIBLE = frozenset({"white", "bright_white"})

#: Left exactly as they are. ``black`` is the most readable foreground a light background offers,
#: and remapping it would be a change for its own sake; the terminal default is a decision the
#: terminal has already made about its own background.
LIGHT_KEEP = frozenset({"black", "default"})

#: AA for body text. The reason it is 4.5 and not 3.0: the host's report is about reading, and
#: 4.5:1 is the WCAG 2.x threshold for normal-size text; 3.0 is the large-text allowance.
MIN_CONTRAST = 4.5

#: The background assumed when the terminal answered but gave no usable colour (a mode forced by
#: configuration with nothing detected). White is the worst case for a dark-on-light palette, so a
#: palette that clears it clears any lighter-than-this background.
ASSUMED_LIGHT_BACKGROUND = (255, 255, 255)


# ── contrast arithmetic (WCAG 2.x) ───────────────────────────────────────────────────────────


def _srgb_to_linear(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG relative luminance of an sRGB triple: 0.0 at black, 1.0 at white."""
    r, g, b = rgb
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """The WCAG contrast ratio between two sRGB colours, from 1.0 to 21.0 (order-independent)."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def is_dark_background(rgb: tuple[int, int, int]) -> bool:
    """Whether a background reads as dark.

    The threshold is 0.5 of the WCAG luminance scale, which runs 0.0 at black to 1.0 at white, so
    0.5 is its midpoint. A background in the darker half gets the palette that is right for it and
    one in the lighter half gets the light palette; the latter is also the safe side for an
    ambiguous mid-grey, because a light palette degrades a role it cannot place while a dark
    palette would simply draw the failing accent.
    """
    return relative_luminance(rgb) < 0.5


# ── parsing: pure, so every form is testable without a terminal ──────────────────────────────


def parse_hex(text: str) -> Optional[tuple[int, int, int]]:
    """``#rrggbb`` / ``rrggbb`` (with an optional shortened form) as an sRGB triple."""
    s = text.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return None
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return None


def parse_colorfgbg(value: str) -> Optional[tuple[int, int, int]]:
    """The background colour out of a ``COLORFGBG`` value, or ``None`` when it does not say.

    The variable is a terminal's palette *index* pair, ``foreground;background`` (``15;0`` on a
    dark terminal, ``0;15`` on a light one); older emulators write only one number. There is no RGB
    here, so the index is mapped through the standard 16-colour palette — which is why the caller
    treats this as a hint and prefers a real OSC 11 answer when it has one.
    """
    parts = [p.strip() for p in str(value).replace(",", ";").split(";") if p.strip()]
    if not parts:
        return None
    try:
        index = int(parts[-1])
    except ValueError:
        return None
    if index < 0:
        return None
    return ANSI16_RGB.get(index)


#: The xterm 16-colour palette, as RGB. `COLORFGBG` names an index into a terminal's palette and
#: nothing in the environment says which one, so these are the conventional values.
ANSI16_RGB = {
    0: (0, 0, 0),
    1: (205, 49, 49),
    2: (13, 188, 121),
    3: (229, 229, 16),
    4: (36, 114, 200),
    5: (188, 63, 188),
    6: (17, 168, 205),
    7: (229, 229, 229),
    8: (102, 102, 102),
    9: (241, 76, 76),
    10: (35, 209, 139),
    11: (245, 245, 67),
    12: (59, 142, 234),
    13: (214, 112, 214),
    14: (41, 184, 219),
    15: (255, 255, 255),
}


def parse_osc11(reply: str) -> Optional[tuple[int, int, int]]:
    """The colour out of an OSC 11 reply, in either spelling a terminal uses.

    xterm answers ``\\x1b]11;rgb:1111/2222/3333\\x1b\\\\`` (four hex digits per channel) and
    others answer ``\\x1b]11;#rrggbb\\x1b\\\\`` or terminate with BEL; the value is taken up to
    whichever terminator arrived. Both are accepted because the terminal, not this code, chooses.
    """
    marker = "]11;"
    at = reply.find(marker)
    if at < 0:
        return None
    value = reply[at + len(marker):]
    for terminator in ("\x1b\\", "\x07", "\x1b"):
        cut = value.find(terminator)
        if cut >= 0:
            value = value[:cut]
            break
    value = value.strip()
    if value.startswith("#"):
        return parse_hex(value)
    if value.startswith("rgb:"):
        channels = value[4:].replace(",", "/").split("/")
        if len(channels) < 3:
            return None
        out = []
        for channel in channels[:3]:
            channel = channel.strip()
            if not channel or any(ch not in "0123456789abcdefABCDEF" for ch in channel):
                return None
            # Four hex digits per channel (`1111` = 0x11) is the usual form; take the high byte so
            # a terminal that answers with a wider or narrower value still yields sRGB.
            out.append(int(channel[:2], 16) if len(channel) >= 2 else int(channel, 16))
        return (out[0], out[1], out[2])
    return None


# ── the theme itself ─────────────────────────────────────────────────────────────────────────


@dataclass
class Theme:
    """The resolved appearance: which way round the terminal is, and what it told us.

    ``background`` is the colour that was *measured*; it is ``None`` when the mode was forced by
    configuration and nothing was detected. Keeping it (rather than a bare boolean) is what lets
    ``themed_style`` decide a role's fate against the real background instead of against white.
    """

    mode: str = DARK
    background: Optional[tuple[int, int, int]] = None
    source: str = "default"  # "config" | "colorfgbg" | "osc11" | "default"

    @property
    def is_light(self) -> bool:
        return self.mode == LIGHT


_current = Theme()
#: The one process-wide answer. Resolved at startup and read on every render, so the probe must
#: never run again — issue #2246, requirement 1 ("resolve once per process, never per render").
_resolved = False


def get_theme() -> Theme:
    return _current


def set_theme(theme: Theme) -> None:
    global _current, _resolved
    _current = theme
    _resolved = True


def reset_theme() -> None:
    """Forget the resolved theme. Tests use this; the client never does."""
    global _current, _resolved
    _current = Theme()
    _resolved = False


def resolve_theme(config_path=None, *, probe=True) -> Theme:
    """Decide the theme: configuration first, then detection, then today's dark appearance.

    ``probe=False`` keeps this pure — no terminal is touched — which is what the tests use to
    drive every branch of the decision without a real tty.
    """
    mode = configured_mode(config_path)
    if mode == DARK:
        return Theme(DARK, None, "config")
    if mode == LIGHT:
        return Theme(LIGHT, detected_background(probe=probe) or ASSUMED_LIGHT_BACKGROUND, "config")
    background = detected_background(probe=probe)
    if background is None:
        # Nothing answered. The host's rule is explicit: keep today's appearance rather than guess
        # a palette for a background nobody measured (issue #2246, requirement 1).
        return Theme(DARK, None, "default")
    return Theme(DARK if is_dark_background(background) else LIGHT, background, "colorfgbg"
                 if os.environ.get("COLORFGBG") and parse_colorfgbg(os.environ["COLORFGBG"])
                 else "osc11")


def ensure_theme(config_path=None, *, probe=True) -> Theme:
    """Resolve the theme **once per process** and return the cached answer.

    Every render path reads ``get_theme()``; this is the one that pays for the probe, and only the
    first call does. ``source == "default"`` is a real answer ("nothing was measurable"), so the
    once-only rule cannot be expressed by re-testing the theme's contents — it needs this flag.
    """
    if not _resolved:
        set_theme(resolve_theme(config_path, probe=probe))
    return get_theme()


def configured_mode(config_path=None) -> str:
    """``[client] theme`` from ``~/.emrg/config.toml``, or ``auto``.

    Read through ``emrg.config`` so the key means the same thing to every reader. Any failure —
    no file, an unreadable one, a value that is not one of the three — falls back to ``auto``:
    a theme is not worth refusing to start the client over, and the client must survive a config
    the daemon would also have to.
    """
    try:
        from emrg.config import load_config
    except Exception:  # noqa: BLE001 - an import failure is "no setting", not a crash
        return DEFAULT_MODE
    try:
        cfg = load_config(config_path)
        mode = getattr(getattr(cfg, "client", None), "theme", None)
    except Exception:  # noqa: BLE001 - see above
        return DEFAULT_MODE
    return mode if mode in THEME_MODES else DEFAULT_MODE


def detected_background(*, probe: bool = True) -> Optional[tuple[int, int, int]]:
    """The terminal's background colour, or ``None``.

    ``COLORFGBG`` is free and is tried first; the OSC 11 query costs a round trip and is asked only
    when the variable said nothing.
    """
    fgbg = os.environ.get("COLORFGBG")
    if fgbg:
        rgb = parse_colorfgbg(fgbg)
        if rgb is not None:
            return rgb
    if not probe:
        return None
    return probe_background()


# ── the probe: the only code here that talks to a terminal ───────────────────────────────────

#: How long to wait for an OSC 11 reply, in milliseconds. Bounded, and the bound is the point:
#: Claude Code waits 2000 ms and codex's own comment calls crossterm's 2 s "too slow"; codex budgets
#: 100 ms, which is the shape this borrows. A terminal that is going to answer a query on a local
#: pty answers in single-digit milliseconds, so 150 ms clears every real case with margin while
#: costing at most that much on the one that never replies — and 150 ms is not perceptible at
#: startup. It is not smaller than codex's 100 ms because the wait ends the moment the reply
#: arrives (the read is not a sleep), so the extra 50 ms is only ever paid by a terminal that is
#: not going to answer at all.
OSC11_TIMEOUT_MS = 150

#: OSC 11: "what is your background colour?" — the same query xterm, kitty, iTerm2 and Ghostty
#: answer. Terminated with ST rather than BEL, which every one of them accepts.
_OSC11_QUERY = "\x1b]11;?\x1b\\"

#: Device Attributes (primary), sent *after* the query as a sentinel. A terminal answers DA1 even
#: when it does not implement OSC 11, so its reply marks "the terminal has spoken and there is no
#: colour coming" and the wait can end early instead of running out the clock. It is why a
#: terminal that ignores OSC 11 does not cost the full timeout.
_SENTINEL_QUERY = "\x1b[c"

#: DA1 replies start with this and end with `c`; a reply proves the bytes were consumed.
_DA1_PREFIX = "\x1b[?"


def _wrap_for_multiplexer(payload: str) -> str:
    """Pass a control sequence through tmux or screen, which otherwise swallow it.

    Both multiplexers forward only what is inside a DCS envelope: tmux answers ``ESC Ptmux;``
    followed by the sequence with every ESC doubled, screen answers ``ESC P`` plainly. Without this
    the query is eaten by the multiplexer and the client would decide "nothing answered" on a
    terminal that would have.
    """
    term = os.environ.get("TERM", "")
    if os.environ.get("TMUX") or term.startswith("tmux"):
        return "\x1bPtmux;" + payload.replace("\x1b", "\x1b\x1b") + "\x1b\\"
    if term.startswith("screen") or os.environ.get("STY"):
        return "\x1bP" + payload + "\x1b\\"
    return payload


def probe_background(
    timeout_ms: int = OSC11_TIMEOUT_MS,
    *,
    stdin=None,
    stdout=None,
) -> Optional[tuple[int, int, int]]:
    """Ask the terminal for its background colour. Bounded, and never raises.

    Returns ``None`` whenever the question cannot be asked or was not answered: not a tty, a write
    that fails, a terminal that stays silent. ``None`` is a measurement result — "not known" — and
    the caller turns it into "keep the dark appearance", never into a guess.

    The wait ends on whichever comes first: the OSC 11 reply, the DA1 sentinel's reply, or the
    timeout. The terminal's mode is restored in ``finally`` because a client that hangs is worse
    than a client that does not know its background.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        if not (stdin.isatty() and stdout.isatty()):
            return None
    except Exception:  # noqa: BLE001 - a stream with no isatty (a test double) is "not a tty"
        return None

    fd_in = stdin.fileno()
    fd_out = stdout.fileno()
    saved = None
    try:
        import termios

        try:
            saved = termios.tcgetattr(fd_in)
            raw = termios.tcgetattr(fd_in)
            # No echo, no canonical line discipline: the reply must not be echoed to the screen
            # and must be readable without a newline. The client's own raw mode may already be in
            # force; setting it here makes the probe safe to call on its own.
            raw[3] = raw[3] & ~(termios.ECHO | termios.ICANON)
            termios.tcsetattr(fd_in, termios.TCSANOW, raw)
        except Exception:  # noqa: BLE001 - no termios control (Windows, a pipe): just try to ask
            saved = None

        try:
            stdout.write(_wrap_for_multiplexer(_OSC11_QUERY + _SENTINEL_QUERY))
            stdout.flush()
        except Exception:  # noqa: BLE001
            return None

        deadline = timeout_ms / 1000.0
        buf = ""
        while True:
            try:
                ready, _, _ = select.select([fd_in], [], [], max(0.0, deadline))
            except Exception:  # noqa: BLE001 - Windows cannot select on a file descriptor
                return None
            if not ready:
                break
            try:
                chunk = os.read(fd_in, 64)
            except Exception:  # noqa: BLE001
                break
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            rgb = parse_osc11(buf)
            if rgb is not None:
                return rgb
            if _DA1_PREFIX in buf and buf.rstrip().endswith("c"):
                # The sentinel answered and no OSC 11 reply came with it: this terminal does not
                # report its background, and nothing more is coming. Stop now — this is the whole
                # reason the sentinel is sent, and it is what keeps the common case (a terminal
                # with no OSC 11 support) from costing the full timeout.
                break
            # Otherwise keep waiting for the rest of a split reply, still inside the original
            # bound: `select` is what makes the wait safe, so a partial answer needs no guessed
            # grace period and a silent terminal still cannot hold the client.
        return parse_osc11(buf)
    except Exception:  # noqa: BLE001 - the probe must never take the client down with it
        return None
    finally:
        if saved is not None:
            try:
                import termios

                termios.tcsetattr(fd_in, termios.TCSANOW, saved)
            except Exception:  # noqa: BLE001
                pass


# ── the remap: where the theme meets a style ─────────────────────────────────────────────────


def themed_style(style):
    """``style`` as the resolved theme would draw it.

    In dark mode this is the identity — the whole point, since the dark terminal's look must not
    change (issue #2246, requirement 6). In light mode a named palette colour is replaced by the
    light palette's entry, and ``dim`` by :data:`LIGHT_DIM`; a colour that cannot clear
    :data:`MIN_CONTRAST` against the detected background, and the families that are not used on
    light at all, drop their colour so the terminal's default foreground — readable on its own
    background by definition — takes over.

    A truecolour style is returned untouched: a widget that named an exact colour has already made
    this decision for itself.
    """
    theme = get_theme()
    if not theme.is_light or style is None:
        return style

    from rich.color import Color, ColorType
    from rich.style import Style

    background = theme.background or ASSUMED_LIGHT_BACKGROUND
    out = style

    color = getattr(style, "color", None)
    if color is not None and color.type == ColorType.STANDARD:
        name = color.name or ""
        if name not in LIGHT_KEEP:
            hex_value = None
            if name not in LIGHT_UNUSED and name not in LIGHT_INVISIBLE:
                hex_value = LIGHT_COLORS.get(name)
            rgb = parse_hex(hex_value) if hex_value else None
            # The ratio is checked against the background that was really detected, not against the
            # white the palette was designed on: a darker "light" terminal can push a colour under
            # the line, and then this role degrades like any other (issue #2246, requirement 2).
            if rgb is not None and contrast_ratio(rgb, background) >= MIN_CONTRAST:
                out = out + Style(color=Color.from_rgb(*rgb))
            else:
                # `style + Style(color=None)` would mean "no change", not "no colour" — rich's
                # combine takes the first value that is not None, so clearing needs `without_color`.
                out = out.without_color

    if getattr(style, "dim", False):
        # The attribute is dropped either way: a terminal renders `dim` as a wash, which is the
        # 3.69:1 grey the report measured. A style that named its own colour keeps that colour and
        # is simply drawn at full strength; one that did not gets the readable grey.
        out = out + Style(dim=False)
        if color is None:
            rgb = parse_hex(LIGHT_DIM)
            if rgb is not None and contrast_ratio(rgb, background) >= MIN_CONTRAST:
                out = out + Style(color=Color.from_rgb(*rgb))

    return out


def code_theme() -> str:
    """The pygments theme for fenced code blocks, for the resolved background.

    ``monokai`` is a dark theme: on a light terminal a fenced block is a black board in the middle
    of a white page (issue #2246, requirement 4). The light alternative is ``friendly``, which
    ships with pygments — no new dependency.
    """
    return "friendly" if get_theme().is_light else "monokai"
