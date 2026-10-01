"""The intra-line offset of a read: which unit it is in, and what an impossible one does.

Two defects in one parameter, both measured 2026-10-02 on master `6b417c4c` with the real
tool, and both about the same thing — a number the caller has to guess the meaning of:

* **the unit was not the one the tool said.** The parameter was `start_line_byte_offset`,
  described as "Byte offset within the first line to begin reading", and the code sliced a
  Python `str` — **characters**. On a 10-character CJK line (30 bytes) offset 6 returned
  `'七八九十'` (characters 6..), where a byte offset of 6 is `'三四五六七八九十'`. Nothing in
  the system ever hands the caller a byte position: the read tool's whole view of a file is
  text, and its continuation hint always says offset 0. So the description named a unit the
  caller could not count and the code used a different one — a model following the
  description passed a number the tool read as something else.
* **an offset the tool could not honour was answered with something else.** The guard was
  `if off < len(first_line)`, so any offset at or past the end of the line — 29, 30, 999 on
  that 10-char line — returned the **entire line**, unchanged and unremarked. The caller
  could not tell that its request had been dropped; the tool's own convention is the
  opposite (an empty range is *named*: `(empty range: lines 3-3 of 2)`).

The unit is settled as **characters** — the one the code has always used and the only one a
caller has — and the name now says so, with `start_line_byte_offset` kept as an accepted
legacy alias. An offset past the end shows none of the line and a note names its length.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from emrg.tools.read_tool import ReadTool

CJK_LINE = "一二三四五六七八九十"  # 10 chars, 30 bytes


def _read(path: Path, **kwargs):
    return asyncio.run(
        ReadTool().execute({"file_path": str(path), "intent": "test", **kwargs})
    )


def _line(res, index: int = 0) -> str:
    return res.content.split("\n")[index]


@pytest.fixture
def cjk_file(tmp_path):
    path = tmp_path / "cjk.txt"
    path.write_text(CJK_LINE + "\nsecond\n", encoding="utf-8")
    return path


@pytest.fixture
def ascii_file(tmp_path):
    path = tmp_path / "ascii.txt"
    path.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
    return path


# ── the unit ───────────────────────────────────────────────────────


def test_the_offset_counts_characters_not_bytes(cjk_file):
    """The claim the old name made, as the reading that contradicts it.

    A byte offset of 6 into this line is `'三四五六七八九十'` (bytes are 3 per character
    here); a character offset of 6 is `'七八九十'`. The tool answers with the second, so
    the unit is characters — and the parameter now says so, which is the whole fix: the
    number's meaning is stated where the caller reads it.
    """
    assert len(CJK_LINE) == 10 and len(CJK_LINE.encode()) == 30

    res = _read(cjk_file, start_line=1, line_limit=1, start_line_char_offset=6)

    assert not res.error
    assert _line(res).endswith("七八九十"), (
        f"the offset is not counting characters: {_line(res)!r}"
    )
    # The byte reading is the discriminating one: bytes 6.. start two characters earlier.
    assert not _line(res).endswith("三四五六七八九十"), "the offset counted bytes"


def test_the_unit_does_not_depend_on_the_spelling(cjk_file):
    """The legacy alias reads the same number in the same unit — one parameter, two names."""
    by_new_name = _read(cjk_file, start_line=1, line_limit=1, start_line_char_offset=6)
    by_legacy_name = _read(cjk_file, start_line=1, line_limit=1, start_line_byte_offset=6)

    assert _line(by_legacy_name) == _line(by_new_name), (
        "the alias and the real name disagree: "
        f"{_line(by_legacy_name)!r} vs {_line(by_new_name)!r}"
    )


def test_the_new_name_is_the_one_the_tool_offers(cjk_file):
    """What the model is told: the property is named for the unit it uses."""
    props = ReadTool().definition().parameters["properties"]

    assert "start_line_char_offset" in props, sorted(props)
    description = props["start_line_char_offset"]["description"]
    assert "Characters" in description
    assert "start_line_byte_offset" in description, "the alias is not documented"


# ── an offset the tool cannot honour ───────────────────────────────


def test_an_offset_past_the_line_shows_none_of_it(cjk_file):
    """Not the whole line: the request is honoured as far as it can be, and named."""
    res = _read(cjk_file, start_line=1, line_limit=1, start_line_char_offset=999)

    assert not res.error
    assert _line(res).strip() == "1", f"the line came back whole: {_line(res)!r}"
    assert CJK_LINE not in res.content


def test_an_offset_exactly_at_the_end_is_past_it(ascii_file):
    """The boundary: the last character is at `len - 1`, so `len` is already past the end."""
    res = _read(ascii_file, start_line=1, line_limit=1, start_line_char_offset=6)

    assert _line(res).strip() == "1", f"offset == len should yield nothing: {_line(res)!r}"
    assert "line 1" not in _line(res)


def test_the_note_names_the_length_and_the_offset(ascii_file):
    """Why nothing of the line is shown — the fact the caller cannot otherwise recover."""
    res = _read(ascii_file, start_line=1, line_limit=1, start_line_char_offset=999)

    assert "(line 1 is 6 char(s): char offset 999 is past its end" in res.content, (
        f"the reason is not stated: {res.content!r}"
    )


def test_the_lines_after_it_are_still_shown(ascii_file):
    """Only the first selected line is affected — the rest of the range is untouched."""
    res = _read(ascii_file, start_line=1, line_limit=3, start_line_char_offset=999)

    assert "line 2" in res.content and "line 3" in res.content
    assert _line(res).strip() == "1"


def test_an_ordinary_offset_still_cuts_the_line(ascii_file):
    """The behaviour that must not change: a usable offset truncates the first line."""
    res = _read(ascii_file, start_line=1, line_limit=1, start_line_char_offset=2)

    assert _line(res).endswith("ne 1"), _line(res)


def test_a_zero_offset_changes_nothing(ascii_file):
    """The default path, byte for byte what it was."""
    res = _read(ascii_file, start_line=1, line_limit=4)

    assert res.content == "     1\tline 1\n     2\tline 2\n     3\tline 3\n     4\t", (
        f"a read with no offset changed: {res.content!r}"
    )


def test_the_continuation_hint_offers_the_real_parameter_name(ascii_file):
    """The hint is a recipe the model copies, so it must spell a parameter that exists."""
    res = _read(ascii_file, start_line=1, line_limit=2)

    assert "truncated at start_line=3" in res.content
    assert "start_line_char_offset=0" in res.content, (
        f"the hint names a parameter the schema does not offer: {res.content!r}"
    )
