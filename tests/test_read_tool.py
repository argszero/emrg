"""Tests for the read tool."""

import asyncio
import re
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
    """The message must state a fact, not just carry the right *prefix*.

    This assertion used to be `"(empty range" in result.content or "empty range" in
    result.content` — the second arm is implied by the first, so only the prefix was
    ever read, and the numbers after it were free. They were wrong every time: the
    branch is reachable **only** when `start_line` is past the file's last line, so
    `lines {start+1}-{end}` always named a range ending before it began. Measured on
    master `bc114ab9`, 2026-10-02: `(empty range: lines 100-6 of 6)`.
    """
    tool = ReadTool()
    f, _ = temp_file
    total = len(f.read_text(encoding="utf-8").split("\n"))
    result = _run(tool.execute({"file_path": str(f), "start_line": 100}))

    assert not result.error
    assert "start_line=100" in result.content, "the request must be named"
    assert f"the file has {total} line" in result.content, (
        f"the file's real length must be named: {result.content!r}"
    )
    assert not _RANGE_RE.search(result.content), (
        f"a range was printed for an empty answer: {result.content!r}"
    )


# Every fact the read tool prints about line numbers, read back out of its message.
# `_RANGE_RE` is the range shape, which must never run backwards; `_START_RE`/`_TOTAL_RE`
# are what an empty answer must carry *instead*. All three are needed: on the fixed tree
# no message prints a range any more, so a sweep that only looked for one would read no
# numbers at all and pass by not looking — a guard made of air.
_RANGE_RE = re.compile(r"lines (\d+)-(\d+) of (\d+)")
_START_RE = re.compile(r"start_line=(\d+)")
_TOTAL_RE = re.compile(r"the file has (\d+) line")


def test_a_read_never_names_a_range_that_ends_before_it_starts(temp_file):
    """The invariant, over the whole domain — not the one input that was reported.

    `start = start_line - 1`, `end = min(start + effective_limit, total_lines)` and
    `effective_limit >= 1`, so `all_lines[start:end]` is empty **only** when
    `start_line > total_lines`; that is what made the old message always wrong. The sweep
    covers the boundary from both sides (last line, last+1, far past) on four file shapes.

    Two readings of every message, so the test cannot pass by not looking: any range the
    tool prints must run forwards and stay inside the file, **and** an empty answer — only
    reachable past the end — must name the line the caller asked for and the file's real
    length. A reverted message fails both: it prints the backwards range *and* names no
    `start_line`.
    """
    tool = ReadTool()
    _, d = temp_file
    shapes = {
        "empty.txt": "",
        "one.txt": "only\n",
        "four.txt": "a\nb\nc\nd\n",
        "no_trailing.txt": "a\nb\nc",
    }
    for name, text in shapes.items():
        path = d / name
        path.write_text(text, encoding="utf-8")
        total = len(text.split("\n"))  # the tool's own reading of "how many lines"
        for start in range(1, total + 4):
            result = _run(tool.execute({"file_path": str(path), "start_line": start}))
            assert not result.error, (name, start, result.content)

            for lo, hi, size in _RANGE_RE.findall(result.content):
                assert int(lo) <= int(hi), (
                    f"{name} start_line={start}: named a backwards range {lo}-{hi}"
                )
                assert int(hi) <= int(size), (
                    f"{name} start_line={start}: range goes past the file's {size} lines"
                )

            if start > total:
                # The empty branch. An answer with no lines is still an answer, so it
                # has to say where the caller asked to start and how long the file is.
                named = _START_RE.search(result.content)
                assert named and int(named.group(1)) == start, (
                    f"{name} start_line={start}: the request is not named in "
                    f"{result.content!r}"
                )
                length = _TOTAL_RE.search(result.content)
                assert length and int(length.group(1)) == total, (
                    f"{name} start_line={start}: the file's real length ({total}) is "
                    f"not named in {result.content!r}"
                )


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
