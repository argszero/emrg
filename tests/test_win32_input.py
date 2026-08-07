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
