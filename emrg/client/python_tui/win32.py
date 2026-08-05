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
"""

from __future__ import annotations

import ctypes
import msvcrt
import os
from ctypes import wintypes
from typing import Any

# ── Console input/output mode flags (wincon.h) ───────────────────────────
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004  # output mode
ENABLE_PROCESSED_OUTPUT = 0x0001
ENABLE_WINDOW_INPUT = 0x0008

# Raw mode = everything off except window input (keeps console resize events)
_RAW_INPUT_MODE = ENABLE_WINDOW_INPUT
# Output mode that makes ANSI/VT sequences work
_VT_OUTPUT_MODE = ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING


class Win32Console:
    """Raw-mode / VT support for a console handle (ctypes, no pywin32)."""

    def __init__(self) -> None:
        self._kernel32 = ctypes.windll.kernel32
        self._saved_modes: dict[int, tuple[int, int]] = {}  # fd -> (in, out)

    # -- internal helpers ------------------------------------------------
    @staticmethod
    def _fd_to_handle(fd: int) -> int | None:
        """Return the Win32 HANDLE for a std fd (via _get_osfhandle)."""
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
