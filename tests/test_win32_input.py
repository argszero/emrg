"""Tests for win32 wide-char input (rant 2026-08-07T21:35:47).

ReadConsoleInputW returns KEY_EVENT_RECORDs whose UnicodeChar holds the
IME-confirmed UTF-16 character (the os.read byte path delivers OEM code
page bytes — GBK on Chinese systems — garbling the UTF-8 input chain).
The ctypes kernel32 call itself is Windows-only, so the tests exercise the
pure _key_event_to_bytes translation: CJK chars, ASCII/Ctrl chars, and
function keys via the legacy scan-code table.
"""

from emrg.client.python_tui.win32 import _key_event_to_bytes


class TestKeyEventToBytes:
    """KEY_EVENT_RECORD → input bytes translation (positive + negative)."""

    def test_ime_cjk_char(self):
        """IME 确认后的中文 → UTF-8 字节（不再按 GBK 交付）。"""
        assert _key_event_to_bytes(True, "中", 0) == "中".encode("utf-8")

    def test_ime_cjk_multi_char(self):
        """一次组合确认多个汉字（如"你好"）逐字符编码。"""
        assert _key_event_to_bytes(True, "你", 0) == "你".encode("utf-8")
        assert _key_event_to_bytes(True, "好", 0) == "好".encode("utf-8")

    def test_ascii_char(self):
        assert _key_event_to_bytes(True, "a", 0) == b"a"

    def test_digit_char(self):
        assert _key_event_to_bytes(True, "7", 0) == b"7"

    def test_ctrl_char(self):
        """Ctrl+C 等控制字符照常交付（0x03 < 0x20 由上层解释）。"""
        assert _key_event_to_bytes(True, "\x03", 0) == b"\x03"

    def test_return_key(self):
        assert _key_event_to_bytes(True, "\r", 0) == b"\r"

    def test_backspace_key(self):
        assert _key_event_to_bytes(True, "\x08", 0) == b"\x08"

    def test_arrow_up_scan_code(self):
        """UnicodeChar==0（功能键）→ 走 scan-code 翻译表（与 #546 同表）。"""
        assert _key_event_to_bytes(True, "\x00", 0x48) == b"\x1b[A"

    def test_arrow_down_scan_code(self):
        assert _key_event_to_bytes(True, "\x00", 0x50) == b"\x1b[B"

    def test_arrow_left_right_scan_code(self):
        assert _key_event_to_bytes(True, "\x00", 0x4B) == b"\x1b[D"
        assert _key_event_to_bytes(True, "\x00", 0x4D) == b"\x1b[C"

    def test_home_end_scan_code(self):
        assert _key_event_to_bytes(True, "\x00", 0x47) == b"\x1b[H"
        assert _key_event_to_bytes(True, "\x00", 0x4F) == b"\x1b[F"

    def test_pageup_pagedown_scan_code(self):
        assert _key_event_to_bytes(True, "\x00", 0x49) == b"\x1b[5~"
        assert _key_event_to_bytes(True, "\x00", 0x51) == b"\x1b[6~"

    def test_unknown_scan_code(self):
        """未识别扫描码 → 空（不产生垃圾字节）。"""
        assert _key_event_to_bytes(True, "\x00", 0x99) == b""

    def test_key_up_drops_cjk(self):
        """key-up 事件丢弃，避免重复字符。"""
        assert _key_event_to_bytes(False, "中", 0x48) == b""

    def test_key_up_drops_scan_code(self):
        assert _key_event_to_bytes(False, "\x00", 0x48) == b""

    def test_key_up_drops_ascii(self):
        assert _key_event_to_bytes(False, "a", 0) == b""

    def test_zero_unicode_with_unknown_scan(self):
        """修饰键（UnicodeChar==0 且非功能键）→ 空。"""
        assert _key_event_to_bytes(True, "\x00", 0) == b""


import ctypes  # noqa: E402

import emrg.client.python_tui.win32 as win32  # noqa: E402


class TestInputRecordLayout:
    """ctypes struct must match the Win32 ABI on every platform (review ❌
    finding: wintypes.WCHAR/c_wchar is 4B on POSIX, inflating the layout;
    uChar is now c_ushort so sizes are platform-stable and simulatable)."""

    def test_key_event_record_size(self):
        assert ctypes.sizeof(win32._KEY_EVENT_RECORD) == 16

    def test_input_record_size(self):
        assert ctypes.sizeof(win32._INPUT_RECORD) == 20

    def test_event_offset(self):
        # EventType (WORD) at 0; union is 4-aligned (DWORD members) → 2B pad,
        # Event at offset 4, INPUT_RECORD = 20B total (Win32 ABI)
        assert win32._INPUT_RECORD.Event.offset == 4


class TestReadConsoleUnicodeLoop:
    """Simulated INPUT_RECORD buffers through the real read loop — the part
    the pure-function tests cannot reach. _ReadConsoleInputW is faked to
    write a record into the buffer the same way kernel32 would."""

    @staticmethod
    def _monkeypatch(monkeypatch, records):
        monkeypatch.setattr(
            win32.Win32Console, "_fd_to_handle", staticmethod(lambda fd: 12345)
        )

        def _set_path(obj, path, value):
            # ctypes setattr with a dotted name silently no-ops — traverse
            parts = path.split(".")
            for p in parts[:-1]:
                obj = getattr(obj, p)
            setattr(obj, parts[-1], value)

        def fake_read(handle, buf, max_events, n_read):
            rec = ctypes.cast(buf, ctypes.POINTER(win32._INPUT_RECORD)).contents
            for field, value in records:
                _set_path(rec, field, value)
            # n_read arrives as the byref() CArgObject wrapping the DWORD
            ctypes.cast(n_read, ctypes.POINTER(ctypes.c_uint32)).contents.value = 1
            return True

        monkeypatch.setattr(win32, "_ReadConsoleInputW", fake_read)

    def test_cjk_char_through_loop(self, monkeypatch):
        self._monkeypatch(monkeypatch, [
            ("EventType", win32.KEY_EVENT),
            ("Event.bKeyDown", True),
            ("Event.wVirtualScanCode", 0),
            ("Event.uChar", ord("中")),
        ])
        assert win32.read_console_unicode(0) == "中".encode("utf-8")

    def test_arrow_scan_code_through_loop(self, monkeypatch):
        self._monkeypatch(monkeypatch, [
            ("EventType", win32.KEY_EVENT),
            ("Event.bKeyDown", True),
            ("Event.wVirtualScanCode", 0x48),
            ("Event.uChar", 0),
        ])
        assert win32.read_console_unicode(0) == b"\x1b[A"

    def test_key_up_dropped_through_loop(self, monkeypatch):
        self._monkeypatch(monkeypatch, [
            ("EventType", win32.KEY_EVENT),
            ("Event.bKeyDown", False),
            ("Event.wVirtualScanCode", 0x48),
            ("Event.uChar", ord("中")),
        ])
        assert win32.read_console_unicode(0) == b""

    def test_mouse_event_skipped_through_loop(self, monkeypatch):
        self._monkeypatch(monkeypatch, [
            ("EventType", 0x0002),  # MOUSE_EVENT — not a key, must be skipped
            ("Event.bKeyDown", True),
            ("Event.wVirtualScanCode", 0),
            ("Event.uChar", ord("a")),
        ])
        assert win32.read_console_unicode(0) == b""
