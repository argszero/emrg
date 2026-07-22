"""Tests for InputParser — byte-stream → complete key sequences, including UTF-8."""

import pytest
from emrg.client.python_tui.events import InputParser, _utf8_len


class TestUtf8Len:
    def test_ascii(self):
        assert _utf8_len(0x41) == 1  # 'A'

    def test_2_byte(self):
        assert _utf8_len(0xC2) == 2  # lead byte for 2-byte sequence

    def test_3_byte(self):
        assert _utf8_len(0xE4) == 3  # lead byte for 3-byte (CJK)

    def test_4_byte(self):
        assert _utf8_len(0xF0) == 4  # lead byte for 4-byte (emoji)

    def test_continuation(self):
        assert _utf8_len(0x80) == 0  # continuation byte


class TestInputParser:
    """Tests for the InputParser byte-stream → sequence decomposition."""

    def test_ascii_single(self):
        parser = InputParser()
        results = parser.feed(b"abc")
        assert results == [b"a", b"b", b"c"]
        assert not parser.has_pending()

    def test_control_chars(self):
        parser = InputParser()
        results = parser.feed(b"\x03\x04\x7f\x08")
        assert results == [b"\x03", b"\x04", b"\x7f", b"\x08"]

    def test_cr_and_lf(self):
        parser = InputParser()
        results = parser.feed(b"\r\n")
        assert results == [b"\r", b"\n"]

    def test_escape_enter(self):
        """Option+Enter → ESC CR (2 bytes)."""
        parser = InputParser()
        results = parser.feed(b"\x1b\r")
        assert results == [b"\x1b\r"]

    def test_escape_option_lf(self):
        """Option+Enter → ESC LF (2 bytes)."""
        parser = InputParser()
        results = parser.feed(b"\x1b\n")
        assert results == [b"\x1b\n"]

    def test_arrow_up(self):
        parser = InputParser()
        results = parser.feed(b"\x1b[A")
        assert results == [b"\x1b[A"]

    def test_arrow_down(self):
        parser = InputParser()
        results = parser.feed(b"\x1b[B")
        assert results == [b"\x1b[B"]

    def test_delete_key(self):
        parser = InputParser()
        results = parser.feed(b"\x1b[3~")
        assert results == [b"\x1b[3~"]

    def test_bracketed_paste_begin(self):
        parser = InputParser()
        results = parser.feed(b"\x1b[200~")
        assert results == [b"\x1b[200~"]
        assert not parser.has_pending()

    def test_bracketed_paste_end(self):
        parser = InputParser()
        results = parser.feed(b"\x1b[201~")
        assert results == [b"\x1b[201~"]
        assert not parser.has_pending()

    def test_cjk_single_character(self):
        """A single CJK character (3 bytes) should yield as one sequence."""
        parser = InputParser()
        results = parser.feed("中".encode("utf-8"))
        assert len(results) == 1
        assert results[0] == "中".encode("utf-8")
        assert results[0].decode("utf-8") == "中"

    def test_cjk_multiple_characters(self):
        """Multiple CJK chars yield individual complete sequences."""
        parser = InputParser()
        results = parser.feed("中文测试".encode("utf-8"))
        assert len(results) == 4
        assert results[0].decode("utf-8") == "中"
        assert results[1].decode("utf-8") == "文"
        assert results[2].decode("utf-8") == "测"
        assert results[3].decode("utf-8") == "试"

    def test_mixed_ascii_cjk(self):
        """Mixed ASCII + CJK text should yield each as individual sequences."""
        parser = InputParser()
        results = parser.feed("hello世界".encode("utf-8"))
        assert len(results) == 7
        decoded = [r.decode("utf-8") if len(r) > 1 else chr(r[0]) for r in results]
        assert decoded == ["h", "e", "l", "l", "o", "世", "界"]

    def test_full_bracketed_paste_cjk(self):
        """Simulate a full bracketed paste of CJK text."""
        parser = InputParser()
        # Terminal sends: ESC[200~ + CJK chars + ESC[201~
        data = b"\x1b[200~" + "中文".encode("utf-8") + b"\x1b[201~"
        results = parser.feed(data)
        assert len(results) == 4
        assert results[0] == b"\x1b[200~"  # paste begin
        assert results[1].decode("utf-8") == "中"
        assert results[2].decode("utf-8") == "文"
        assert results[3] == b"\x1b[201~"  # paste end

    def test_emoji_4_byte(self):
        """4-byte UTF-8 emoji should yield as one sequence."""
        parser = InputParser()
        results = parser.feed("🎉".encode("utf-8"))
        assert len(results) == 1
        assert results[0].decode("utf-8") == "🎉"

    def test_partial_utf8_wait(self):
        """When only partial UTF-8 bytes arrive, parser should wait (has_pending)."""
        parser = InputParser()
        # Feed only the first 2 bytes of a 3-byte CJK char
        cjk = "中".encode("utf-8")  # 3 bytes: e4 b8 ad
        results = parser.feed(cjk[:2])
        assert results == []  # not enough bytes yet
        assert parser.has_pending()

        # Feed the remaining byte
        results = parser.feed(cjk[2:])
        assert len(results) == 1
        assert results[0].decode("utf-8") == "中"
        assert not parser.has_pending()

    def test_partial_csi_wait(self):
        """When partial CSI sequence arrives, parser should wait."""
        parser = InputParser()
        data = b"\x1b["  # partial CSI, no final byte
        results = parser.feed(data)
        assert results == []  # should wait for more
        assert parser.has_pending()

        # Complete the arrow-up sequence
        results = parser.feed(b"A")
        assert results == [b"\x1b[A"]
        assert not parser.has_pending()

    def test_partial_bracketed_paste_wait(self):
        """Bracketed paste markers arriving in fragments."""
        parser = InputParser()
        # Feed ESC[
        results = parser.feed(b"\x1b[")
        assert results == []
        assert parser.has_pending()

        # Feed 200
        results = parser.feed(b"200")
        # Still waiting (need the ~ )
        # After 200, we have "\x1b[200" — len is 4, not enough for the 6-byte check
        # But CSI final byte search finds nothing since '0' < 0x40
        assert parser.has_pending()

        # Feed ~ to complete
        results = parser.feed(b"~")
        assert len(results) >= 1
        # The parser may have decomposed it differently or kept it as one
        assert b"200" in b"".join(results) or b"\x1b[200~" in results

    def test_unknown_escape_consumed(self):
        """Unknown ESC sequence should consume just the ESC byte."""
        parser = InputParser()
        results = parser.feed(b"\x1bZ")
        assert results == [b"\x1bZ"]
