"""Raw stdin → structured events.

Parses raw terminal input bytes into structured events:
- Key presses (with modifiers: ctrl, meta, shift)
- Mouse events (SGR protocol)
- Bracketed paste
- Terminal resize (SIGWINCH)

Equivalent to Claude Code's parse-keypress.ts (26KB). That implementation handles
VT sequences, Kitty keyboard protocol, xterm modifyOtherKeys, SGR mouse, and
terminal response parsing. This is the Python equivalent — start with basic VT
and add Kitty protocol support progressively.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto


class KeyName(Enum):
    """Named keys beyond printable ASCII."""

    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    HOME = auto()
    END = auto()
    PAGE_UP = auto()
    PAGE_DOWN = auto()
    INSERT = auto()
    DELETE = auto()
    BACKSPACE = auto()
    RETURN = auto()
    TAB = auto()
    ESCAPE = auto()
    SPACE = auto()
    F1 = auto()
    F2 = auto()
    F3 = auto()
    F4 = auto()
    F5 = auto()
    F6 = auto()
    F7 = auto()
    F8 = auto()
    F9 = auto()
    F10 = auto()
    F11 = auto()
    F12 = auto()


@dataclass
class KeyEvent:
    """A structured keyboard event."""

    name: KeyName | None = None
    char: str | None = None  # Printable character (if any)
    ctrl: bool = False
    meta: bool = False  # Alt/Option
    shift: bool = False
    sequence: str | None = None  # Raw escape sequence


@dataclass
class MouseEvent:
    """A structured mouse event (SGR protocol)."""

    x: int = 0
    y: int = 0
    button: int = 0  # 0=left, 1=middle, 2=right, 64=wheel-up, 65=wheel-down
    kind: str = "press"  # 'press', 'release', 'drag', 'wheel'


@dataclass
class PasteEvent:
    """Bracketed paste event."""

    text: str


@dataclass
class ResizeEvent:
    """Terminal resize event."""

    width: int
    height: int


# Union event type
Event = KeyEvent | MouseEvent | PasteEvent | ResizeEvent
EventHandler = Callable[[Event], None]


# VT escape sequence parsing tables

# SS3 sequences (ESC O ...)
_SS3_MAP: dict[str, KeyName] = {
    "A": KeyName.UP,
    "B": KeyName.DOWN,
    "C": KeyName.RIGHT,
    "D": KeyName.LEFT,
    "H": KeyName.HOME,
    "F": KeyName.END,
    "P": KeyName.F1,
    "Q": KeyName.F2,
    "R": KeyName.F3,
    "S": KeyName.F4,
}

# CSI sequences (ESC [ ... ~)
_CSI_TILDE_MAP: dict[int, KeyName] = {
    1: KeyName.HOME,
    2: KeyName.INSERT,
    3: KeyName.DELETE,
    4: KeyName.END,
    5: KeyName.PAGE_UP,
    6: KeyName.PAGE_DOWN,
    11: KeyName.F1,
    12: KeyName.F2,
    13: KeyName.F3,
    14: KeyName.F4,
    15: KeyName.F5,
    17: KeyName.F6,
    18: KeyName.F7,
    19: KeyName.F8,
    20: KeyName.F9,
    21: KeyName.F10,
    23: KeyName.F11,
    24: KeyName.F12,
}

# CSI sequences (ESC [ letter) with modifiers
_CSI_LETTER_MOD: dict[str, tuple[int, KeyName]] = {
    "A": (1, KeyName.UP),
    "B": (1, KeyName.DOWN),
    "C": (1, KeyName.RIGHT),
    "D": (1, KeyName.LEFT),
    "H": (1, KeyName.HOME),
    "F": (1, KeyName.END),
    "Z": (2, KeyName.TAB),  # shift-tab
}


def parse_keypress(data: bytes) -> KeyEvent | None:
    """Parse a raw key sequence into a structured KeyEvent.

    Handles:
    - Single byte (ASCII printable, ctrl chars)
    - ESC sequence (SS3/C SI with tilde or letter)
    - CSI u (Kitty protocol): ESC [ codepoint [; modifier] u

    Returns None if the sequence is incomplete or unhandled.
    """
    if not data:
        return None

    # Single byte
    if len(data) == 1:
        b = data[0]
        # Ctrl chars: 0x01-0x1A → 'a'-'z'
        if b < 0x20:
            if b == 0x09:  # TAB
                return KeyEvent(name=KeyName.TAB)
            if b == 0x0D:  # CR / Return
                return KeyEvent(name=KeyName.RETURN)
            if b == 0x08 or b == 0x7F:  # Backspace
                return KeyEvent(name=KeyName.BACKSPACE)
            if b == 0x1B:  # ESC
                return KeyEvent(name=KeyName.ESCAPE)
            # Ctrl+letter
            if 0x01 <= b <= 0x1A:
                return KeyEvent(
                    char=chr(b + 0x60), ctrl=True, sequence=chr(b)
                )
            return KeyEvent(char=chr(b), sequence=chr(b))
        # Printable ASCII
        if 0x20 <= b <= 0x7E:
            return KeyEvent(char=chr(b), sequence=chr(b))

    # ESC sequence
    if data[0] == 0x1B:
        seq = data[1:].decode("ascii", errors="replace")

        # SS3: ESC O ...
        if seq.startswith("O") and len(seq) == 2:
            key = _SS3_MAP.get(seq[1])
            if key:
                return KeyEvent(name=key, sequence=seq)

        # CSI: ESC [ ...
        if seq.startswith("["):
            inner = seq[1:]

            # CSI ... ~  (tilde-terminated)
            if inner.endswith("~"):
                num_str = inner[:-1]
                # CSI u (Kitty protocol): codepoint [; modifier] u
                if "u" in num_str:
                    return _parse_kitty_csi_u(num_str)
                try:
                    n = int(num_str)
                    key = _CSI_TILDE_MAP.get(n)
                    if key:
                        return KeyEvent(name=key, sequence=seq)
                except ValueError:
                    pass

            # CSI letter (no tilde)
            if len(inner) == 1:
                key = _SS3_MAP.get(inner)  # same letter codes
                if key:
                    return KeyEvent(name=key, sequence=seq)

    return None


def _parse_kitty_csi_u(inner: str) -> KeyEvent | None:
    """Parse Kitty keyboard protocol CSI u sequence: codepoint [; modifier] u.

    Modifier bits (from Kitty protocol spec):
        1=shift, 2=alt, 4=ctrl, 8=super, 16=hyper, 32=meta
    """
    try:
        parts = inner.rstrip("u").split(";")
        codepoint = int(parts[0])
        modifier = int(parts[1]) if len(parts) > 1 else 0

        # Map common codepoints to named keys
        name: str | None = None
        if codepoint == 13:
            name = "return"
        elif codepoint == 9:
            name = "tab"
        elif codepoint == 27:
            name = "escape"
        elif codepoint == 127:
            name = "backspace"
        elif codepoint == 32:
            name = "space"

        char = chr(codepoint) if 32 <= codepoint <= 126 else None

        return KeyEvent(
            name=name,
            char=char,
            shift=bool(modifier & 1),
            meta=bool(modifier & 2),  # alt
            ctrl=bool(modifier & 4),
            sequence=f"CSI {inner}",
        )
    except (ValueError, OverflowError):
        return None


def parse_mouse_sgr(data: bytes) -> MouseEvent | None:
    """Parse SGR mouse sequence: ESC [ < btn ; col ; row M/m."""
    if len(data) < 6 or data[0:3] != b"\x1b[<":
        return None
    try:
        s = data[3:].decode("ascii", errors="replace")
        kind = "release" if s.endswith("m") else "press"
        parts = s[:-1].split(";")
        if len(parts) != 3:
            return None
        btn = int(parts[0])
        col = int(parts[1]) - 1
        row = int(parts[2]) - 1

        # Wheel events
        if btn & 64:
            return MouseEvent(
                x=col, y=row, button=btn, kind="wheel"
            )
        # Drag vs press
        if btn & 32:
            return MouseEvent(
                x=col, y=row, button=btn & 3, kind="drag"
            )
        return MouseEvent(
            x=col, y=row, button=btn & 3, kind=kind
        )
    except (ValueError, UnicodeDecodeError):
        return None


def _utf8_len(b: int) -> int:
    """Return expected UTF-8 sequence length from first byte, or 1."""
    if b < 0x80:
        return 1
    if b < 0xC0:
        return 0
    if b < 0xE0:
        return 2
    if b < 0xF0:
        return 3
    return 4


class InputParser:
    """Accumulates raw stdin bytes and yields complete byte sequences.

    Internal buffer and consumption logic mechanically transplanted from
    interactive_demo.py's esc_buf loop.

    Usage:
        parser = InputParser()
        for seq in parser.feed(data):
            handle_key(seq)
        if parser.has_pending():
            # incomplete sequence waiting — read more bytes
            ...
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Append raw bytes; return complete sequences consumed from buffer."""
        self._buf.extend(data)
        results: list[bytes] = []

        while len(self._buf) > 0:
            b = self._buf[0]
            # Determine bytes needed for this sequence
            need = 1
            if b == 0x1B:
                need = 2  # ESC+CR (2 bytes for Option+Enter) or ESC+[ (3+ bytes)
            elif b >= 0x80:
                need = _utf8_len(b)
            # Wait for complete sequence if not enough bytes yet
            if len(self._buf) < need:
                break

            # Try to consume a complete key sequence
            consumed = 0
            if self._buf[0] == 0x1B and len(self._buf) >= 2:
                # ESC CR or ESC LF = Option/Alt+Enter → newline (2 bytes)
                if self._buf[1] in (0x0D, 0x0A):
                    consumed = 2
                # Bracketed paste begin/end markers: ESC[200~ (6 bytes) / ESC[201~ (6 bytes)
                elif self._buf[1] == 0x5B and len(self._buf) >= 6:
                    if self._buf[2:6] == b"200~" or self._buf[2:6] == b"201~":
                        consumed = 6
                elif len(self._buf) >= 3:
                    # Arrow keys, home, end: ESC [ X (3 bytes)
                    csi_arrows = (0x41, 0x42, 0x43, 0x44, 0x48, 0x46)
                    if self._buf[1] == 0x5B and self._buf[2] in csi_arrows:
                        consumed = 3
                    # Delete: ESC [ 3 ~ (4 bytes)
                    elif (self._buf[1] == 0x5B and len(self._buf) >= 4
                          and self._buf[2] == 0x33 and self._buf[3] == 0x7E):
                        consumed = 4
                    else:
                        consumed = 1  # Unknown ESC — consume just ESC
                else:
                    pass  # wait for more bytes
            elif self._buf[0] >= 0x80:
                # Multi-byte UTF-8 — wait for complete sequence
                need = _utf8_len(self._buf[0])
                if len(self._buf) >= need:
                    consumed = need
                # else: not enough bytes yet — will wait for next read
            elif self._buf[0] in (0x0D, 0x0A, 0x03, 0x04, 0x7F, 0x08):
                consumed = 1  # Single-byte control char
            elif 0x20 <= self._buf[0] <= 0x7E:
                consumed = 1  # Single-byte printable
            else:
                consumed = 1  # Unknown — consume and ignore

            if consumed > 0:
                seq = bytes(self._buf[:consumed])
                del self._buf[:consumed]
                results.append(seq)
            else:
                break

        return results

    def has_pending(self) -> bool:
        """True if buffer contains incomplete sequence waiting for more bytes."""
        return len(self._buf) > 0
