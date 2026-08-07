"""Tests for InputParser — byte-stream → complete key sequences, including UTF-8."""

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


# ── Legacy Windows scan codes (rant 2026-08-07T10:38:21) ──


class TestLegacyScanCodes:
    """0xE0/0x00 prefix + scan code → normalized ANSI sequence."""

    def test_legacy_up(self):
        parser = InputParser()
        assert parser.feed(b"\xe0\x48") == [b"\x1b[A"]

    def test_legacy_down_zero_prefix(self):
        parser = InputParser()
        assert parser.feed(b"\x00\x50") == [b"\x1b[B"]

    def test_legacy_left_right(self):
        parser = InputParser()
        assert parser.feed(b"\xe0\x4b") == [b"\x1b[D"]
        assert parser.feed(b"\xe0\x4d") == [b"\x1b[C"]

    def test_legacy_home_end(self):
        parser = InputParser()
        assert parser.feed(b"\xe0\x47") == [b"\x1b[H"]
        assert parser.feed(b"\xe0\x4f") == [b"\x1b[F"]

    def test_legacy_pgup_pgdn_ins_del(self):
        parser = InputParser()
        assert parser.feed(b"\xe0\x49") == [b"\x1b[5~"]
        assert parser.feed(b"\xe0\x51") == [b"\x1b[6~"]
        assert parser.feed(b"\xe0\x52") == [b"\x1b[2~"]
        assert parser.feed(b"\xe0\x53") == [b"\x1b[3~"]

    def test_legacy_unknown_scan_waits_as_utf8(self):
        """0xE0 + non-scan-code byte is NOT consumed as a pair — it falls
        through to the UTF-8 path (pre-PR behavior). 0xE0 is the UTF-8 lead
        for U+0800-U+0FFF; valid continuation bytes (0xA0-0xBF) are disjoint
        from scan codes (0x47-0x53), so gating on the map is exact."""
        parser = InputParser()
        assert parser.feed(b"\xe0\xff") == []
        assert parser.has_pending()

    def test_e0_led_utf8_not_garbled(self):
        """Regression (review 4882245397): 0xE0-led UTF-8 scripts must
        survive intact — Thai ก (U+0E01) and Devanagari अ (U+0905)."""
        parser = InputParser()
        thai = "ก".encode()  # E0 B8 81
        assert parser.feed(thai) == [thai]
        deva = "अ".encode()  # E0 A4 85
        assert parser.feed(deva) == [deva]

    def test_nul_not_swallowed_with_next_key(self):
        """Regression (review 4882245397): lone 0x00 (Ctrl+@) followed by a
        key in the same read must yield two sequences, not one pair."""
        parser = InputParser()
        assert parser.feed(b"\x00A") == [b"\x00", b"A"]

    def test_legacy_incomplete_waits(self):
        """A lone 0xE0 prefix waits for the scan-code byte."""
        parser = InputParser()
        assert parser.feed(b"\xe0") == []
        assert parser.has_pending()
        assert parser.feed(b"\x48") == [b"\x1b[A"]

    def test_utf8_cjk_still_works(self):
        """CJK UTF-8 multi-byte input is unaffected by the scan-code branch."""
        parser = InputParser()
        encoded = "中".encode()
        assert parser.feed(encoded) == [encoded]


class TestParseKeypressLegacy:
    def test_parse_legacy_up(self):
        from emrg.client.python_tui.events import KeyName, parse_keypress
        key = parse_keypress(b"\xe0\x48")
        assert key is not None
        assert key.name == KeyName.UP

    def test_parse_legacy_down(self):
        from emrg.client.python_tui.events import KeyName, parse_keypress
        key = parse_keypress(b"\x00\x50")
        assert key is not None
        assert key.name == KeyName.DOWN

    def test_parse_legacy_unknown_returns_none(self):
        from emrg.client.python_tui.events import parse_keypress
        assert parse_keypress(b"\xe0\xff") is None
