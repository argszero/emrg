"""Win32 console helpers for the TUI (Windows only).

Provides raw-mode / ANSI-VT enabling via ctypes (no pywin32 dependency —
keeps the packaged runtime lean). Used by Terminal when sys.platform ==
"win32"; POSIX terminals keep using termios (rant 2026-08-05T15:54:28).

Raw mode on Windows mirrors the POSIX termios.tcgetattr/tty.setraw contract:
  - disable line input / echo / processed input so os.read returns raw keys
  - enable VT processing so ANSI escape sequences (CURSOR_HIDE etc.) work on
    cmd/conhost (Windows Terminal enables VT by default)
  - set the fd to binary mode so CRLF translation is off (key sequences like
    the arrow prefix ESC [ A arrive byte-identical to POSIX)

ENABLE_VIRTUAL_TERMINAL_INPUT (rant 2026-08-07T10:38:21) makes conhost
deliver function/arrow keys as ANSI ESC [ A/B/C/D sequences (fixing the
legacy 0xE0 scan-code problem) and is kept as the raw-mode input flag.
However it does NOT fix CJK IME input: character keystrokes still arrive
through the os.read byte stream encoded in the console input code page
(GBK/CP936 on Chinese systems), which the UTF-8-assuming input chain
garbles (rant 2026-08-07T21:35:47, host-verified on v0.2.11).

The reliable path for Unicode characters is the wide-char API
ReadConsoleInputW — it returns KEY_EVENT_RECORDs whose UnicodeChar holds
the IME-confirmed UTF-16 character. read_console_unicode() is the primary
stdin reader on Windows; the VT-input byte path remains only as a fallback
for older conhost versions.

The module imports cleanly on POSIX (guarded msvcrt / windll access) so
its pure KEY_EVENT → bytes translation logic is unit-testable everywhere.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any

try:
    import msvcrt
except ImportError:  # POSIX — module imported only for its pure helpers
    msvcrt = None  # type: ignore[assignment]

from emrg.client.python_tui.events import _LEGACY_SCAN_TO_ANSI

# ── Console input/output mode flags (wincon.h) ───────────────────────────
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004  # output mode
ENABLE_PROCESSED_OUTPUT = 0x0001
ENABLE_WINDOW_INPUT = 0x0008
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200  # input mode (rant 2026-08-07T10:38:21)

# Raw mode = everything off except window input + VT input.
# ENABLE_VIRTUAL_TERMINAL_INPUT makes conhost deliver UTF-8 keystrokes
# (CJK IME works) and standard ANSI arrow sequences (ESC [ A/B/C/D).
# Without it: OEM code page bytes (GBK on Chinese systems) garble the
# UTF-8-assuming input chain, and arrows arrive as legacy 0xE0 scan codes.
_RAW_INPUT_MODE = ENABLE_WINDOW_INPUT | ENABLE_VIRTUAL_TERMINAL_INPUT
# Output mode that makes ANSI/VT sequences work
_VT_OUTPUT_MODE = ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING


class Win32Console:
    """Raw-mode / VT support for a console handle (ctypes, no pywin32)."""

    def __init__(self) -> None:
        try:
            self._kernel32 = ctypes.windll.kernel32
        except AttributeError:  # POSIX — imported only for pure helpers
            self._kernel32 = None
        self._saved_modes: dict[int, tuple[int, int]] = {}  # fd -> (in, out)

    # -- internal helpers ------------------------------------------------
    @staticmethod
    def _fd_to_handle(fd: int) -> int | None:
        """Return the Win32 HANDLE for a std fd (via _get_osfhandle)."""
        if msvcrt is None:  # POSIX
            return None
        try:
            return msvcrt.get_osfhandle(fd)
        except OSError:
            return None

    def _get_console_mode(self, handle: int) -> int | None:
        mode = wintypes.DWORD()
        if not self._kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        return int(mode.value)

    def _set_console_mode(self, handle: int, mode: int) -> bool:
        return bool(self._kernel32.SetConsoleMode(handle, wintypes.DWORD(mode)))

    # -- public API -------------------------------------------------------
    def enable_raw_mode(self, fd: int) -> bool:
        """Enable raw + VT mode on a console fd. Returns True on success."""
        handle = self._fd_to_handle(fd)
        if handle is None:
            return False
        in_mode = self._get_console_mode(handle)
        out_mode = self._get_console_mode(self._fd_to_handle(1))
        if in_mode is None:
            return False
        self._saved_modes[fd] = (in_mode, out_mode if out_mode is not None else 0)
        ok_in = self._set_console_mode(handle, _RAW_INPUT_MODE)
        if not ok_in:
            # Pre-Win10-1607 consoles reject ENABLE_VIRTUAL_TERMINAL_INPUT —
            # fall back to window-input-only raw mode (InputParser in
            # events.py defensively translates legacy 0xE0 scan codes).
            ok_in = self._set_console_mode(handle, ENABLE_WINDOW_INPUT)
        ok_out = True
        if out_mode is not None:
            ok_out = self._set_console_mode(self._fd_to_handle(1), _VT_OUTPUT_MODE)
        # Binary mode: no CRLF translation (key sequences byte-identical to POSIX)
        try:
            msvcrt.setmode(fd, os.O_BINARY)
        except OSError:
            pass
        return bool(ok_in and ok_out)

    def disable_raw_mode(self, fd: int) -> None:
        """Restore the console modes saved by enable_raw_mode."""
        saved = self._saved_modes.pop(fd, None)
        if saved is None:
            return
        in_mode, out_mode = saved
        handle = self._fd_to_handle(fd)
        if handle is not None and in_mode:
            self._set_console_mode(handle, in_mode)
        out_handle = self._fd_to_handle(1)
        if out_handle is not None and out_mode:
            self._set_console_mode(out_handle, out_mode)

    @property
    def active(self) -> bool:
        return bool(self._saved_modes)


# Module-level singleton — saved console modes must survive across
# _enter_raw_mode / _exit_raw_mode calls (a fresh instance per call would
# lose _saved_modes and fail to restore the terminal).
win32_console = Win32Console()


# ── Wide-char input (rant 2026-08-07T21:35:47) ─────────────────────────
# CJK IME-confirmed text must be read via ReadConsoleInputW (UTF-16
# KEY_EVENT_RECORDs), not the os.read byte stream (OEM code page — GBK on
# Chinese systems — garbles the UTF-8-assuming input chain).

KEY_EVENT = 0x0001


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", wintypes.WCHAR),  # union { WCHAR UnicodeChar; CHAR AsciiChar; }
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _INPUT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventType", wintypes.WORD),
        ("Event", _KEY_EVENT_RECORD),
    ]


try:
    _k32 = ctypes.windll.kernel32
except AttributeError:  # POSIX — no kernel32
    _k32 = None

_ReadConsoleInputW = None
_FlushConsoleInputBuffer = None
if _k32 is not None:
    _ReadConsoleInputW = _k32.ReadConsoleInputW
    _ReadConsoleInputW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_INPUT_RECORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ReadConsoleInputW.restype = wintypes.BOOL
    _FlushConsoleInputBuffer = _k32.FlushConsoleInputBuffer
    _FlushConsoleInputBuffer.argtypes = [wintypes.HANDLE]
    _FlushConsoleInputBuffer.restype = wintypes.BOOL


def _key_event_to_bytes(b_key_down: bool, unicode_char: str, scan_code: int) -> bytes:
    """Translate one KEY_EVENT_RECORD to input bytes (pure, unit-testable).

    - key-up events are dropped (avoids duplicate characters)
    - UnicodeChar != 0 → UTF-8 encode (covers ASCII, Ctrl chars, and
      IME-confirmed CJK — the whole point of ReadConsoleInputW)
    - UnicodeChar == 0 (function/arrow keys) → legacy scan-code table
      (same _LEGACY_SCAN_TO_ANSI map the VT-input path uses)
    """
    if not b_key_down:
        return b""
    if unicode_char and unicode_char != "\x00":
        return unicode_char.encode("utf-8", errors="replace")
    return _LEGACY_SCAN_TO_ANSI.get(scan_code, b"")


def flush_console_input(fd: int) -> None:
    """Flush the console input buffer (drop stale byte-stream residue)."""
    if _FlushConsoleInputBuffer is None:
        return
    handle = Win32Console._fd_to_handle(fd)
    if handle is not None:
        _FlushConsoleInputBuffer(handle)


def read_console_unicode(fd: int, max_events: int = 32) -> bytes:
    """Read console input via ReadConsoleInputW, returning UTF-8 bytes.

    Character keys (incl. IME-confirmed CJK) are UTF-8 encoded; function
    keys (UnicodeChar == 0) are translated to ANSI CSI via the scan-code
    table. Returns b"" when no key events are pending — callers should
    sleep briefly to avoid busy-polling.
    """
    if _ReadConsoleInputW is None:
        return b""
    handle = Win32Console._fd_to_handle(fd)
    if handle is None:
        return b""
    records = (_INPUT_RECORD * max_events)()
    n_read = wintypes.DWORD(0)
    if not _ReadConsoleInputW(handle, records, max_events, ctypes.byref(n_read)):
        return b""
    out = bytearray()
    for i in range(n_read.value):
        rec = records[i]
        if rec.EventType != KEY_EVENT:
            continue
        key = rec.Event
        out.extend(
            _key_event_to_bytes(
                bool(key.bKeyDown), key.uChar, int(key.wVirtualScanCode)
            )
        )
    return bytes(out)
