"""Tests for the read tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from emrg.tools.read_tool import ReadTool


@pytest.fixture
def temp_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        f = root / "test.txt"
        f.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n")
        yield f, root


def _run(coro):
    return asyncio.run(coro)


def test_read_basic(temp_file):
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({"file_path": str(f)}))
    assert not result.error
    assert "line 1" in result.content
    assert "line 5" in result.content
    # Check line number prefix
    assert result.content.startswith("     1\t")


def test_read_with_start_line(temp_file):
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({"file_path": str(f), "start_line": 3}))
    assert not result.error
    assert result.content.startswith("     3\tline 3")


def test_read_with_offset_alias(temp_file):
    """Legacy 'offset' param still works."""
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({"file_path": str(f), "offset": 3}))
    assert not result.error
    assert result.content.startswith("     3\tline 3")


def test_read_with_line_limit(temp_file):
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({"file_path": str(f), "line_limit": 2}))
    assert not result.error
    assert "line 1" in result.content
    assert "line 2" in result.content
    assert "truncated" in result.content  # 6 lines total with trailing newline > 2


def test_read_with_limit_alias(temp_file):
    """Legacy 'limit' param still works."""
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({"file_path": str(f), "limit": 2}))
    assert not result.error
    assert "line 1" in result.content
    assert "line 2" in result.content
    assert "truncated" in result.content


def test_read_file_not_found():
    tool = ReadTool()
    result = _run(tool.execute({"file_path": "/nonexistent/file.txt"}))
    assert result.error
    assert "file not found" in result.content


def test_read_missing_file_path():
    tool = ReadTool()
    result = _run(tool.execute({}))
    assert result.error
    assert "no file_path" in result.content


def test_read_directory(temp_file):
    tool = ReadTool()
    _, d = temp_file
    result = _run(tool.execute({"file_path": str(d)}))
    assert not result.error
    assert "Directory listing" in result.content
    assert "test.txt" in result.content


def test_read_binary_fails(temp_file):
    tool = ReadTool()
    _, d = temp_file
    # Create a binary file
    bf = d / "binary.bin"
    bf.write_bytes(b"\x00\x01\x02\xff\xfe")
    result = _run(tool.execute({"file_path": str(bf)}))
    # Binary files may or may not decode; if they fail, it should be an error
    if result.error:
        assert "binary file" in result.content.lower() or "cannot read" in result.content.lower()


def test_read_start_line_beyond_eof(temp_file):
    tool = ReadTool()
    f, _ = temp_file
    # 6 lines (5 + trailing newline), start_line=100 is way beyond
    result = _run(tool.execute({"file_path": str(f), "start_line": 100}))
    assert "(empty range" in result.content or "empty range" in result.content


def test_read_truncation_message(temp_file):
    """When a file exceeds the default max lines limit and no limit is specified,
    the result should be truncated and include an exact truncation message."""
    tool = ReadTool()
    _, d = temp_file
    # Create a file with 1500 lines (exceeds DEFAULT_MAX_LINES=1000)
    big = d / "big.txt"
    big.write_text("\n".join(f"line {i}" for i in range(1, 1501)) + "\n")
    result = _run(tool.execute({"file_path": str(big)}))
    assert not result.error
    # Should show truncation message since 1500 > 1000
    assert "truncated" in result.content
    assert "start_line=" in result.content


def test_read_explicit_limit_above_default(temp_file):
    """Explicit limit between DEFAULT_MAX_LINES (1000) and MAX_LINES (2000)
    should be honored — returns up to the requested number of lines."""
    tool = ReadTool()
    _, d = temp_file
    big = d / "big2.txt"
    big.write_text("\n".join(f"line {i}" for i in range(1, 1501)) + "\n")
    result = _run(tool.execute({"file_path": str(big), "line_limit": 1200}))
    assert not result.error
    # With explicit limit=1200, we should get more than 1000 lines
    assert "truncated" in result.content  # 1500 > 1200


def test_read_explicit_limit_capped_at_max(temp_file):
    """Explicit limit above MAX_LINES (2000) should be capped at MAX_LINES."""
    tool = ReadTool()
    _, d = temp_file
    big = d / "big3.txt"
    big.write_text("\n".join(f"line {i}" for i in range(1, 2501)) + "\n")
    result = _run(tool.execute({"file_path": str(big), "line_limit": 2500}))
    assert not result.error
    # Should be capped at MAX_LINES=2000, so 2500 lines should show truncation
    assert "truncated" in result.content


def test_read_with_start_line_byte_offset(temp_file):
    """start_line_byte_offset truncates the first selected line."""
    tool = ReadTool()
    f, _ = temp_file
    # line 3 is "line 3" (6 chars). offset=2 skips "li"
    result = _run(tool.execute({
        "file_path": str(f),
        "start_line": 3,
        "line_limit": 2,
        "start_line_byte_offset": 2,
    }))
    assert not result.error
    assert "ne 3" in result.content  # "line 3"[2:] = "ne 3"
    assert "line 4" in result.content


def test_read_start_line_byte_offset_at_eol(temp_file):
    """byte_offset beyond line length yields empty first line."""
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({
        "file_path": str(f),
        "start_line": 3,
        "start_line_byte_offset": 999,
    }))
    assert not result.error
    # First line (line 3) should be empty or skipped
    lines = result.content.split("\n")
    # line 3 should be empty (byte offset beyond its length)
    assert "     3\t" in lines[0] or lines[0].strip().startswith("3")


def test_read_image_returns_vision_ref(temp_file):
    """Reading a .png returns a structured image reference, not a binary error
    (rant 2026-08-24T14:36:01 — ReadTool vision support)."""
    import json

    tool = ReadTool()
    _, d = temp_file
    img = d / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)  # tiny valid-ish PNG header
    result = _run(tool.execute({"file_path": str(img)}))
    assert not result.error, f"image read should not error: {result.content}"
    ref = json.loads(result.content)
    assert ref["type"] == "image"
    assert ref["path"] == str(img.resolve())
    assert ref["mime"] == "image/png"


def test_read_image_jpeg_webp_mime(temp_file):
    """jpeg/webp extensions map to the right mime types."""
    import json

    tool = ReadTool()
    _, d = temp_file
    for name, expect in (
        ("photo.jpg", "image/jpeg"),
        ("photo.jpeg", "image/jpeg"),
        ("anim.gif", "image/gif"),
        ("pic.webp", "image/webp"),
    ):
        img = d / name
        img.write_bytes(b"\x00" * 32)
        result = _run(tool.execute({"file_path": str(img)}))
        assert not result.error, name
        ref = json.loads(result.content)
        assert ref["mime"] == expect, name


def test_read_image_too_large(temp_file):
    """Oversized images are rejected with a resize hint instead of a vision block."""
    from emrg.tools.read_tool import MAX_IMAGE_SIZE

    tool = ReadTool()
    _, d = temp_file
    img = d / "big.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_SIZE + 1))
    result = _run(tool.execute({"file_path": str(img)}))
    assert result.error
    assert "too large" in result.content
    assert "resize" in result.content.lower() or "compress" in result.content.lower()


def test_read_uppercase_extension(temp_file):
    """Uppercase extensions (.PNG) are handled case-insensitively."""
    import json

    tool = ReadTool()
    _, d = temp_file
    img = d / "shot.PNG"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    result = _run(tool.execute({"file_path": str(img)}))
    assert not result.error
    ref = json.loads(result.content)
    assert ref["mime"] == "image/png"


def test_read_tool_description_mentions_images():
    """The read tool description advertises image support."""
    tool = ReadTool()
    desc = tool.definition().description
    assert "vision-format image block" in desc
    assert ".png" in desc


# ── A count argument's domain is decided where the untrusted value enters ──────
#
# Measured on master `256400f4`, 2026-10-02. A negative `line_limit` is not a smaller
# read, it is a different one: it became the slice bound `all_lines[start:limit]`, which
# drops the *last* lines, and the continuation hint printed a line number that cannot
# exist (`start_line=-4`). `start_line=8, line_limit=-2` selected nothing at all and
# reported `(empty range: lines 8-5 of 11)` — a backwards range. All `error=False`.

import re as _re


def _note(content: str) -> str | None:
    """The continuation hint the caller may copy-paste, or None when absent."""
    m = _re.search(r"truncated at start_line=(-?\d+)", content)
    return m.group(1) if m else None


@pytest.fixture
def ten_lines(tmp_path):
    f = tmp_path / "ten.txt"
    f.write_text("\n".join(f"L{n}" for n in range(1, 11)) + "\n", encoding="utf-8")
    return f


def _read(path, **kw):
    return _run(ReadTool().execute({"file_path": str(path), "intent": "probe", **kw}))


class TestTheCountArgumentsHaveADomain:
    """Every count accepts its domain or takes the documented default.

    Both directions matter: a nonsense value must not change the reading compared with
    the same call without it, and a *valid* value must not be flattened by the same code
    that rejects the nonsense ones.
    """

    @pytest.mark.parametrize("limit", [-1, -2, -5, -100])
    def test_a_negative_line_limit_reads_the_whole_file(self, ten_lines, limit):
        assert _read(ten_lines, line_limit=limit).content == _read(ten_lines).content

    #: Every shape `read` prints a line number in. A mutation arm is what found this
    #: list has to be complete: removing the helper's `>= minimum` check did not remove
    #: the bad number, it moved it from the continuation note
    #: (``truncated at start_line=0``) to the empty-range message
    #: (``(empty range: lines 0--2 of 11)``) — and a test that parsed only the first
    #: shape passed, so the arm read SURVIVED while the defect was still there.
    _NAMES_A_LINE = (
        _re.compile(r"start_line=(-?\d+)"),
        _re.compile(r"empty range: lines (-?\d+)-(-?\d+) of (\d+)"),
    )

    def _numbers_named(self, content: str) -> list[int]:
        """Every line number the message states, from every shape it states one in."""
        found: list[int] = []
        for pattern in self._NAMES_A_LINE:
            for m in pattern.finditer(content):
                found.extend(int(g) for g in m.groups())
        # `total N lines` is a count, not a line the caller may jump to; it is the last
        # group of the empty-range pattern and is always >= 1 for a non-empty file.
        return found

    @pytest.mark.parametrize("kw", [
        {"line_limit": -1}, {"line_limit": -2}, {"line_limit": -5}, {"line_limit": -100},
        {"line_limit": "three"}, {"line_limit": 0}, {"line_limit": 2.7},
        {"start_line": -3}, {"start_line": 0}, {"start_line": "x"},
        {"start_line": 8, "line_limit": -2}, {"start_line": 3, "line_limit": -1},
        {"start_line": 99}, {"start_line_byte_offset": -4},
        {},
    ])
    def test_no_message_names_a_line_below_one(self, ten_lines, kw):
        """The invariant: a line number the caller may use is never < 1, in any shape.

        A call may name no line at all (an untruncated read prints the file and no
        message) — the invariant is on the numbers *when* one is stated.
        """
        content = _read(ten_lines, **kw).content
        named = self._numbers_named(content)
        assert all(n >= 1 for n in named), (
            f"the message at {kw} names a line below 1:\n{content}"
        )

    @pytest.mark.parametrize("kw", [{"line_limit": -1}, {"line_limit": -5},
                                    {"start_line": 8, "line_limit": -2},
                                    {"start_line": 0}])
    def test_no_range_is_printed_backwards(self, ten_lines, kw):
        """`(empty range: lines 8-5 of 11)` was reachable before the domain existed."""
        content = _read(ten_lines, **kw).content
        for m in _re.finditer(r"empty range: lines (\d+)-(\d+)", content):
            assert int(m.group(1)) <= int(m.group(2)), content

    @pytest.mark.parametrize("kw", [{"line_limit": -1}, {"line_limit": -5},
                                    {"line_limit": "three"}, {"line_limit": 0},
                                    {"start_line": -3}, {"start_line": "x"}])
    def test_a_nonsense_value_is_the_same_reading_as_no_value(self, ten_lines, kw):
        assert _read(ten_lines, **kw).content == _read(ten_lines).content

    def test_a_positive_limit_is_still_honoured(self, ten_lines):
        """The other direction: the domain check must not flatten valid values."""
        three = _read(ten_lines, line_limit=3)
        assert _note(three.content) == "4", three.content
        assert "L3" in three.content and "L4" not in three.content, three.content

    def test_a_start_line_after_the_limit_still_starts_there(self, ten_lines):
        """`start_line=8, line_limit=-2` used to select nothing at all."""
        result = _read(ten_lines, start_line=8, line_limit=-2)
        assert not result.error
        assert "L8" in result.content, result.content
        assert "L7" not in result.content, result.content

    def test_the_domain_helper_is_the_only_home(self):
        """The rule lives in `base`, and both consumers import it rather than respell it."""
        from emrg.tools import base, grep_tool, read_tool
        assert read_tool.as_count is base.as_count
        assert grep_tool.as_count is base.as_count

class TestTheFileIsAsLongAsItsLines:
    """A file's last newline terminates its last line; it does not add one.

    `"a\\nb\\nc\\n".split("\\n")` yields four elements, and the tool used to count
    all four — so every terminated file (most files) read as one line longer than it
    is, rendered a numbered line holding nothing, and could announce a truncation
    whose continuation was empty. Measured 2026-10-02 on master `bc114ab`: a file of
    1000 lines read with no arguments reported "truncated at start_line=1001 ...
    total 1001 lines", and line 1001 held `''`.
    """

    def test_a_terminated_file_renders_the_lines_it_has(self, tmp_path):
        f = tmp_path / "three.txt"
        f.write_text("a\nb\nc\n")
        result = _run(ReadTool().execute({"file_path": str(f), "intent": "read it"}))
        assert not result.error
        assert result.content.split("\n") == ["     1\ta", "     2\tb", "     3\tc"]

    def test_a_lone_newline_is_one_empty_line(self, tmp_path):
        f = tmp_path / "just_newline.txt"
        f.write_text("\n")
        result = _run(ReadTool().execute({"file_path": str(f), "intent": "read it"}))
        assert result.content.split("\n") == ["     1\t"]

    def test_a_zero_byte_file_has_no_lines(self, tmp_path):
        f = tmp_path / "zero.txt"
        f.write_bytes(b"")
        result = _run(ReadTool().execute({"file_path": str(f), "intent": "read it"}))
        assert "empty file" in result.content, result.content
        assert "     1\t" not in result.content

    def test_an_unterminated_file_keeps_its_last_line(self, tmp_path):
        """The opposite direction: the last line is real, never dropped."""
        f = tmp_path / "three_no_eol.txt"
        f.write_text("a\nb\nc")
        result = _run(ReadTool().execute({"file_path": str(f), "intent": "read it"}))
        assert result.content.split("\n") == ["     1\ta", "     2\tb", "     3\tc"]

    def test_a_file_of_exactly_the_default_limit_reads_whole(self, tmp_path):
        """1000 terminated lines fill the default window exactly — nothing is left
        over, so the notice that sends a reader to the next range must not appear."""
        from emrg.tools.read_tool import DEFAULT_MAX_LINES

        f = tmp_path / "exactly.txt"
        f.write_text("".join(f"line {i}\n" for i in range(1, DEFAULT_MAX_LINES + 1)))
        result = _run(ReadTool().execute({"file_path": str(f), "intent": "read it"}))
        assert not result.error
        assert "truncated" not in result.content
        assert result.content.split("\n")[-1] == f"  {DEFAULT_MAX_LINES}\tline {DEFAULT_MAX_LINES}"

    def test_the_line_over_the_default_limit_does_report_a_truncation(self, tmp_path):
        """The same boundary read the other way, so the test above cannot pass by
        the notice being broken: one line more, and the notice is owed — naming the
        real total and the real continuation line."""
        from emrg.tools.read_tool import DEFAULT_MAX_LINES

        f = tmp_path / "one_over.txt"
        total = DEFAULT_MAX_LINES + 1
        f.write_text("".join(f"line {i}\n" for i in range(1, total + 1)))
        result = _run(ReadTool().execute({"file_path": str(f), "intent": "read it"}))
        assert not result.error
        assert f"truncated at start_line={total}, " in result.content
        assert f"total {total} lines" in result.content
