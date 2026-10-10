"""Tests for the read tool."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from emrg.tools.read_tool import DEFAULT_MAX_LINES, ReadTool


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


class TestAReadIsBoundedWhicheverSubjectItHas:
    """A listing is a read, so the limits bind it too (issue #2059).

    Measured 2026-10-10 (`cyc20261010-211559`): the directory branch returned every
    entry whatever the caller passed — `line_limit=1` on a 6,000-entry directory
    produced a byte-identical 108,112-character result to a read with no arguments,
    while `DEFAULT_MAX_LINES` stopped a 6,000-line *file* at 1,000 lines and named the
    continuation. A limit that binds one subject and is dropped for the other is the
    "identical output whether or not you bounded it" defect, and the caller could not
    tell that its bound had been ignored.
    """

    @staticmethod
    def _dir(tmp_path, count):
        d = tmp_path / "listing"
        d.mkdir()
        for i in range(count):
            (d / f"entry-{i:05d}.txt").write_text("")
        return d

    @staticmethod
    def _entries(content):
        return [line for line in content.splitlines() if line.startswith("  ")]

    def test_a_listing_honours_an_explicit_limit(self, tmp_path):
        d = self._dir(tmp_path, 40)
        tool = ReadTool()
        whole = _run(tool.execute({"file_path": str(d)}))
        limited = _run(tool.execute({"file_path": str(d), "line_limit": 2}))
        assert len(limited.content) < len(whole.content), (
            "the limit did not shrink the listing, so a caller who bounded the read "
            "cannot tell whether its bound was applied"
        )
        assert len(self._entries(limited.content)) == 2, limited.content

    def test_a_listing_is_capped_by_default_and_names_its_size(self, tmp_path):
        d = self._dir(tmp_path, DEFAULT_MAX_LINES + 10)
        tool = ReadTool()
        result = _run(tool.execute({"file_path": str(d)}))
        assert len(self._entries(result.content)) == DEFAULT_MAX_LINES, (
            "the default cap must bind a listing exactly as it binds a file"
        )
        assert f"total {DEFAULT_MAX_LINES + 10} entries" in result.content, result.content

    def test_a_listing_says_where_to_continue(self, tmp_path):
        d = self._dir(tmp_path, 30)
        tool = ReadTool()
        result = _run(tool.execute({"file_path": str(d), "line_limit": 10}))
        assert "truncated at start_line=11, start_line_byte_offset=0" in result.content, (
            result.content
        )
        assert "total 30 entries" in result.content, result.content

    def test_a_start_line_selects_the_entry_to_start_from(self, tmp_path):
        d = self._dir(tmp_path, 30)
        tool = ReadTool()
        result = _run(tool.execute({"file_path": str(d), "start_line": 29, "line_limit": 5}))
        entries = self._entries(result.content)
        assert entries == ["  entry-00028.txt", "  entry-00029.txt"], entries

    def test_the_offset_is_reported_as_not_applied_to_a_listing(self, tmp_path):
        """Reported, not accepted in silence — the defect this branch is fixing.

        `start_line_byte_offset` names a position inside a *line* and exists for resuming
        one very long line. A listing's lines hold one entry name each, so there is
        nothing here to resume; dropping it without a word is what the branch used to do
        to all three parameters.
        """
        d = self._dir(tmp_path, 3)
        tool = ReadTool()
        result = _run(tool.execute({
            "file_path": str(d), "line_limit": 1, "start_line_byte_offset": 2,
        }))
        assert "start_line_byte_offset=2 is not applied" in result.content, result.content

    def test_a_start_line_past_the_end_is_reported(self, tmp_path):
        d = self._dir(tmp_path, 3)
        tool = ReadTool()
        result = _run(tool.execute({"file_path": str(d), "start_line": 9}))
        assert "no entries" in result.content, result.content
        assert "which has 3 entries" in result.content, result.content

    def test_an_empty_directory_is_not_a_clean_reading(self, tmp_path):
        """The header alone read as a broken listing rather than an empty one."""
        d = tmp_path / "nothing"
        d.mkdir()
        tool = ReadTool()
        result = _run(tool.execute({"file_path": str(d)}))
        assert result.content == f"(empty directory: {d})", result.content

    def test_a_one_entry_listing_past_the_end_is_counted_in_the_singular(self, tmp_path):
        """The singular half of the message above, which no test reached.

        `f"entr{'y' if total_entries == 1 else 'ies'}"` is a branch, and a branch needs a
        reading: the plural side is covered by `test_a_start_line_past_the_end_is_reported`,
        and replacing the whole spelling with the bare plural left every test in this file
        green (measured 2026-10-10) — so the sentence a one-entry listing really produces
        was pinned by nothing, and a later edit could have made it say "1 entries" without
        a red anywhere.
        """
        d = self._dir(tmp_path, 1)
        result = _run(ReadTool().execute({"file_path": str(d), "start_line": 2}))
        assert "which has 1 entry" in result.content, result.content
        assert "1 entries" not in result.content, result.content

    def test_the_sentence_about_a_listing_is_the_one_the_tool_keeps(self, tmp_path):
        """The description sentence this class arrived with, read against its behaviour.

        A sentence in a tool description is a claim about behaviour, so it is pinned
        against the behaviour it claims rather than against its own words —
        `tests/test_glob_tool.py` states the lesson (*"a promise in a tool description
        that no test holds is how the universal claim above survived review"*) and
        `tests/test_read_tool.py::test_read_never_cuts_a_line` is this file's precedent
        for the same shape, added because a cap introduced later would quietly re-define
        what its sentence said.

        Measured 2026-10-10: the sentence was **new here and read by nothing** — dropping
        its promise ("and a cut listing says so") left all 42 tests green, and so did
        naming a number other than the constant, because the number is interpolated and
        nothing asked whether the interpolation was still there. The two description
        assertions below are the ones no other test makes; the two behaviour assertions
        are the sentence's own claim, in the same reading, so neither half can drift
        alone.
        """
        description = ReadTool().definition().description
        assert f"{DEFAULT_MAX_LINES} entries" in description, description
        assert "a cut listing says so" in description, description

        d = self._dir(tmp_path, DEFAULT_MAX_LINES + 5)
        content = _run(ReadTool().execute({"file_path": str(d)})).content
        assert len(self._entries(content)) == DEFAULT_MAX_LINES, (
            "the number the description names is not the number the tool caps at: "
            f"{content[-200:]!r}"
        )
        assert "truncated at" in content, (
            "'a cut listing says so' must be a sentence the renderer keeps: "
            f"{content[-200:]!r}"
        )


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
    """The message names the request and the subject's size — and is not a range.

    The branch is reached exactly when `start_line` is past the last line, so the
    `lines {start + 1}-{end}` it used to print was always descending (measured 2026-10-10,
    `cyc20261010-204628`: a 10-line file answered `start_line=50` with `lines 50-10 of
    10`). This test asserted the substring `empty range`, which that descending range
    satisfies — a wording pinned by the presence of its words rather than by what they
    say. Both numbers the message holds are asserted now, and the descending shape is
    refused, so a message naming the wrong start line or the wrong file size fails here.
    """
    tool = ReadTool()
    f, _ = temp_file
    # 5 lines (the fixture writes 5, and the trailing newline is not a line)
    result = _run(tool.execute({"file_path": str(f), "start_line": 100}))
    assert "start_line=100" in result.content, result.content
    assert "5 lines" in result.content, result.content
    assert "100-5" not in result.content, result.content
    assert "lines 100" not in result.content, result.content


def test_a_one_line_file_past_its_end_is_counted_in_the_singular(tmp_path):
    """The singular half of the message above, which no test reached.

    `plural = "" if total_lines == 1 else "s"` is a branch, and a branch needs a reading:
    measured 2026-10-10, replacing it with the bare `"s"` left every test in this file
    green, so a one-line file read past its end could have been made to say `1 lines`
    with no red anywhere. It is reachable — `start_line=2` on a one-line file goes
    through this branch — and the plural side above does not reach it.
    """
    f = tmp_path / "one.txt"
    f.write_text("only\n")
    result = _run(ReadTool().execute({"file_path": str(f), "start_line": 2}))
    assert "which has 1 line" in result.content, result.content
    assert "1 lines" not in result.content, result.content


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
    """An offset at or past the line's end shows that line empty, and says so.

    Rewritten 2026-10-08 (`cyc20261008-153534`) after this arm was measured **unable to
    fail**: it read `line 3` (6 chars) with offset 999 and asserted
    `"     3\\t" in lines[0] or lines[0].strip().startswith("3")`, and the tool returned
    the **whole line** — which satisfies both halves, because `"     3\\tline 3"` begins
    with `"     3\\t"`. So the docstring above said "yields empty first line" while the
    code did the opposite, and nothing here could tell the two apart. The assertions
    below are the two outcomes separated: the line is empty, the whole line is absent,
    and the notice names the offset and the line's real length.
    """
    tool = ReadTool()
    f, _ = temp_file
    assert len("line 3") == 6
    result = _run(tool.execute({
        "file_path": str(f),
        "start_line": 3,
        "start_line_byte_offset": 999,
    }))
    assert not result.error
    lines = result.content.split("\n")
    # The numbered first line holds nothing after the tab.
    assert lines[0] == "     3\t", f"line 3 should be shown empty, got {lines[0]!r}"
    # The subject is not silently handed back instead: that output is what offset 0
    # produces, and the caller cannot tell the two apart.
    assert "line 3" not in lines[0], (
        "the whole line came back for an offset past its end — byte-identical to offset 0"
    )
    # The fact is stated, with both numbers a reader needs.
    assert "999" in result.content and "6 chars" in result.content, (
        f"the ignored offset was not reported: {result.content!r}"
    )


def test_read_start_line_byte_offset_exactly_at_the_line_end(temp_file):
    """An offset *equal* to the line's length is at its end, not inside it.

    The `<` that dropped the offset is an off-by-one, and the arm above cannot see one:
    with `999` of a 6-character line, flipping the guard to `<=` leaves every assertion
    there satisfied, because `"line 3"[999:]` and `"line 3"[6:]` are both `""`. Measured
    2026-10-08 — the `<=` mutant survived the rest of this file, so the defect the issue
    reports had two spellings, "at" and "past", and only the second one was pinned.
    """
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({
        "file_path": str(f),
        "start_line": 3,
        "start_line_byte_offset": 6,  # "line 3" is exactly 6 characters
    }))
    assert not result.error
    lines = result.content.split("\n")
    assert lines[0] == "     3\t", (
        f"offset 6 of a 6-character line is at its end, so nothing of it is shown, "
        f"got {lines[0]!r}"
    )
    assert "at or past the end of line 3" in result.content, result.content
    assert "6 chars" in result.content
    assert "line 4" in result.content  # the rest of the range is untouched


def test_read_line_count_truncation_still_asks_for_offset_zero(temp_file):
    """The tool's own truncation is by *line count*, so its continuation hint keeps
    `start_line_byte_offset=0`: the offset that resumes a line is not the one that
    continues a page (issue #1928, acceptance item 4)."""
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({"file_path": str(f), "line_limit": 2}))
    assert not result.error
    assert "truncated at start_line=3" in result.content
    assert "start_line_byte_offset=0" in result.content


def test_read_start_line_byte_offset_states_the_remainder(temp_file):
    """An offset inside the line reports the line's length and how much was shown.

    The other half of the same defect: with the offset applied, the output was a *suffix*
    printed with its line number and no way to tell it apart from the whole line — so a
    caller chunking a long line could not know whether more of it followed.
    """
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({
        "file_path": str(f),
        "start_line": 3,
        "start_line_byte_offset": 2,
    }))
    assert not result.error
    lines = result.content.split("\n")
    assert lines[0] == "     3\tne 3", f"expected the suffix from character 2, got {lines[0]!r}"
    assert "6 chars" in result.content and "character 2" in result.content, (
        f"the partial read was not reported as partial: {result.content!r}"
    )
    assert "4 of them" in result.content, (
        f"the notice does not say how much of the line was shown: {result.content!r}"
    )


def test_read_never_cuts_a_line(temp_file):
    """The premise `start_line_byte_offset`'s description states, measured.

    A line is returned whole however long it is, so no offset of the tool's own making
    exists: the offset belongs to a caller chunking a line itself. That is what the
    schema description now says, and this arm is what makes the sentence falsifiable —
    a line cap added later (the shape that would make the tool produce offsets) fails
    here rather than quietly re-defining the parameter.
    """
    tool = ReadTool()
    _, d = temp_file
    long_line = "x" * 5000 + "END-OF-LINE"
    big = d / "oneline.txt"
    big.write_text("short\n" + long_line + "\n")
    result = _run(tool.execute({"file_path": str(big), "start_line": 2}))
    assert not result.error
    shown = result.content.split("\n")[0]
    assert shown == f"     2\t{long_line}", (
        f"line 2 is {len(long_line)} chars and came back with {len(shown) - 8} of them — "
        "a line was cut, which is the offset-producing behaviour the description denies"
    )


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


# ── the numeric parameters' domains (issue #1935) ─────────────────────────────
#
# `{"type": "integer"}` is the whole of the declaration, so the lower bound lived
# only in the consuming code — and each of these three was read as a *different
# number* rather than as a caller error. Measured 2026-10-08 (`cyc20261008-175325`):
# `line_limit=-3` returned lines 1-7 of a 10-line file (a slice from the end) and
# named a continuation at `start_line=-2`; `line_limit=0` was falsy and read as
# *absent*, returning the default 1000 lines; a negative byte offset was clamped to
# 0 (the whole line) with no note. A reading of a range nobody asked for must not
# happen silently, so the value is refused and the refusal names it.


def test_read_refuses_a_line_limit_below_one(temp_file):
    """0 and a negative are refused — and nothing is read in their place."""
    tool = ReadTool()
    f, _ = temp_file
    for value in (0, -3):
        result = _run(tool.execute({"file_path": str(f), "line_limit": value}))
        assert result.error, f"line_limit={value} was accepted"
        assert "line_limit" in result.content and str(value) in result.content
        # The defect this replaces: `-3` answered with lines 1-7 of a 10-line file.
        assert "line 1" not in result.content, (
            f"line_limit={value} still returned lines of the file: {result.content!r}"
        )
        assert "truncated" not in result.content, "a refused call announces no range"


def test_read_refuses_a_line_limit_that_is_not_a_number(temp_file):
    """A value that cannot be read as a count is refused, not silently defaulted.

    Measured before the fix: `int("abc")` raised, the handler set `line_limit = None`,
    and the call came back as the default 1000-line window — the caller's parameter
    dropped without a word.
    """
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({"file_path": str(f), "line_limit": "abc"}))
    assert result.error
    assert "line_limit" in result.content and "abc" in result.content
    assert "line 1" not in result.content


def test_read_refuses_a_start_line_below_one(temp_file):
    """`start_line=0` (or negative) is refused rather than read as line 1."""
    tool = ReadTool()
    f, _ = temp_file
    for value in (0, -5):
        result = _run(tool.execute({"file_path": str(f), "start_line": value}))
        assert result.error, f"start_line={value} was accepted"
        assert "start_line" in result.content and str(value) in result.content
        assert "line 1" not in result.content
    # The alias is refused under the spelling the caller used.
    alias = _run(tool.execute({"file_path": str(f), "offset": 0}))
    assert alias.error and "offset" in alias.content


def test_read_refuses_a_negative_byte_offset(temp_file):
    """A negative offset is refused — the whole line is not handed back instead.

    This is the third carrier of the same shape in this file: `max(0, offset)`
    turned `-5` into `0`, which returns the line entire, byte-identical to asking
    for no offset at all.
    """
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({
        "file_path": str(f), "start_line": 3, "start_line_byte_offset": -5,
    }))
    assert result.error
    assert "start_line_byte_offset" in result.content and "-5" in result.content
    assert "line 3" not in result.content, "the whole line came back for a negative offset"


def test_read_still_reads_every_value_inside_its_domain(temp_file):
    """The control: the refusals above must not have swallowed a legal call.

    Drives the whole domain's useful range in one call — a start line, a limit and a
    within-line offset together — and asserts the exact slice, so "refused" cannot
    pass for "handled".
    """
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({
        "file_path": str(f), "start_line": 3, "line_limit": 1, "start_line_byte_offset": 2,
    }))
    assert not result.error
    lines = result.content.split("\n")
    assert lines[0] == "     3\tne 3"
    assert "line 4" not in result.content, "line_limit=1 read more than one line"


# ── the subject has to be a regular file ──
#
# Every file tool opens its subject, and an open is not a read: on a named pipe it blocks
# until the other end appears. Nothing here asked what it was opening, so a FIFO decided
# whether the call returned — and because `daemon._run_tool_loop` awaits `tool.execute`
# *on the event loop*, a call that never returns is the daemon never returning. The test
# cannot bound a hang it is meant to catch, so it is the guard: it fails the moment the
# refusal stops being made, by never finishing.


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
def test_read_refuses_a_named_pipe_instead_of_blocking(tmp_path):
    """`read` on a FIFO is refused, naming the kind — not entered and never returned."""
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    result = _run(ReadTool().execute({"file_path": str(fifo)}))
    assert result.error, "a FIFO is not readable and must not be read"
    assert "a FIFO (named pipe)" in result.content, result.content
    assert "not a regular file" in result.content, result.content


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
def test_read_still_reads_a_regular_file_beside_a_pipe(tmp_path):
    """The control: the refusal is about the kind, not about the directory holding it."""
    os.mkfifo(tmp_path / "pipe")
    regular = tmp_path / "notes.txt"
    regular.write_text("hello\n")
    result = _run(ReadTool().execute({"file_path": str(regular)}))
    assert not result.error, result.content
    assert "hello" in result.content
