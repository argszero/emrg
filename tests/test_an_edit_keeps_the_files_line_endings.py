"""An edit changes the text it was given and nothing else — line endings included.

Issue #1803, measured 2026-10-02 on master `6b417c4c` with the real tools: a one-word
edit to a three-line CRLF file rewrote **every** terminator in it
(`b'first\\r\\nsecond\\r\\nthird\\r\\n'` → `b'first\\nSECOND\\nthird\\n'`), because
`Path.read_text` normalises the newlines and `Path.write_text` re-emits them. The tool
reported one replacement while the file's diff was every line.

Not a theoretical file class: `.gitattributes` pins `*.cmd`/`*.bat`/`*.ps1` to
`text eol=crlf`, and an LF-only `.cmd` is misparsed by `cmd.exe` — the v0.2.25–v0.2.27
installer "exit code 1" series (rant 2026-08-12T12:30:41, guarded by
`tests/test_cmd_crlf.py`, which only runs after the commit). So an agent that adds one
line to `bin/emrgd.cmd` used to convert the whole launcher.

Both directions are tested: the endings that must survive, and the behaviour that must
**not** change (an LF file stays byte-for-byte what it was, a refused edit writes
nothing). `emrg/tools/newlines.py` carries the rule and the reading.

What #1804 left behind, measured 2026-10-02 on `bc114ab9` (its merge) with the same
probes — the version above fixes the uniform case and only that one:

| probe | `256400f4` | `bc114ab9` (#1804) |
|---|---|---|
| uniform CRLF, edit one line | `CRLF=0` — every terminator gone | `CRLF=3` — fixed |
| **mixed** file, edit the LF line | `CRLF=0` | **`CRLF=0`** — still every terminator gone |
| `new_string` carrying `\\r\\n` | no lone CR | **`b'…b1\\r\\rb2…'` — a lone CR, newly introduced** |

So the endings were still decided per *file* (a boolean) rather than carried per byte,
and the caller's own text was never read the way the file is. Both are fixed by the
splice in `emrg/tools/newlines.py`; the two probes above are pinned as tests here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from emrg.tools.edit_tool import EditTool
from emrg.tools.newlines import CR, CRLF, LF, replace, terminator, view
from emrg.tools.read_tool import ReadTool

REPO_ROOT = Path(__file__).resolve().parent.parent


def _edit(path: Path, old: str, new: str, **extra):
    return asyncio.run(
        EditTool().execute(
            {
                "file_path": str(path),
                "old_string": old,
                "new_string": new,
                "intent": "test",
                **extra,
            }
        )
    )


def _lines(path: Path) -> str:
    return asyncio.run(ReadTool().execute({"file_path": str(path), "intent": "test"})).content


@pytest.fixture
def crlf_file(tmp_path):
    path = tmp_path / "install.cmd"
    path.write_bytes(b"first\r\nsecond\r\nthird\r\n")
    return path


# ── the endings the edit must not touch ────────────────────────────


def test_an_edit_to_one_word_leaves_every_other_terminator_alone(crlf_file):
    """The measurement in issue #1803, as the assertion it should have been."""
    result = _edit(crlf_file, "second", "SECOND")

    assert not result.error
    assert crlf_file.read_bytes() == b"first\r\nSECOND\r\nthird\r\n", (
        "the edit rewrote terminators it was not asked to change: "
        f"{crlf_file.read_bytes()!r}"
    )


def test_a_uniformly_crlf_file_is_still_crlf(crlf_file):
    """The property `tests/test_cmd_crlf.py` checks the working tree for, at the edit.

    The shape assertion alone is satisfied by a tool that writes the file back
    untouched — measured 2026-10-02 with a mutation arm that did exactly that
    (`new_content, count = raw, raw.count(old)`): ``\\r\\n`` was still 3 and the test
    passed. So the replacement is asserted first, and the shape second; a property test
    that a no-op satisfies is not holding the line it names.
    """
    result = _edit(crlf_file, "second", "SECOND")

    assert not result.error
    data = crlf_file.read_bytes()
    assert b"SECOND" in data, f"the edit did not apply: {data!r}"
    assert data.count(b"\r\n") == data.count(b"\n") == 3, (
        f"a bare LF appeared in a CRLF file: {data!r}"
    )


def test_a_mixed_file_keeps_the_endings_it_had_outside_the_match(tmp_path):
    """A file with no single convention is still carried byte for byte."""
    path = tmp_path / "mixed.txt"
    path.write_bytes(b"a\r\nb\nc\r\n")

    _edit(path, "b", "B")

    assert path.read_bytes() == b"a\r\nB\nc\r\n"


def test_a_bash_append_does_not_make_a_later_edit_lf_only(tmp_path):
    """How a mixed file actually arrives — and what the next edit must not do to it.

    `>>` from a shell appends a *bare LF* line to whatever the file uses, so appending to
    one of `.gitattributes`' CRLF files produces exactly the shape `tests/test_cmd_crlf.py`
    calls "LF-only or mixed endings". The edit that follows names one line; the failure this
    test forbids is the quiet one — rewriting the file's own CRLF lines as bare LF, which is
    the LF-only `.cmd` that `cmd.exe` misparses (the installer "exit code 1" series).

    The distinction is the whole point of reading the file's bytes instead of a normalised
    string: a mixed file is a file the tool has no business normalising, and after an edit
    naming one line, its CRLF lines are still CRLF.
    """
    path = tmp_path / "emrgd.cmd"
    path.write_bytes(b"@echo off\r\nset EMRG=1\r\n")
    with open(path, "ab") as handle:  # what `printf 'rem added\n' >> file` writes
        handle.write(b"rem added\n")

    mixed = path.read_bytes()
    assert mixed.count(b"\r\n") == 2 and mixed.count(b"\n") - mixed.count(b"\r\n") == 1, (
        f"the fixture is not the mixed shape this test is about: {mixed!r}"
    )

    _edit(path, "set EMRG=1", "set EMRG=2")

    after = path.read_bytes()
    assert after == b"@echo off\r\nset EMRG=2\r\nrem added\n", (
        f"the edit normalised a mixed file instead of carrying it: {after!r}"
    )
    assert after.count(b"\r\n") == 2, (
        "the edit turned the file's CRLF lines into bare LF — the LF-only .cmd shape "
        f"cmd.exe misparses: {after!r}"
    )


def test_the_named_region_is_the_only_thing_that_changed(tmp_path):
    """Prefix and suffix survive exactly — the contract the tool's name promises."""
    path = tmp_path / "f.txt"
    before = b"alpha\r\nbravo\r\ncharlie\r\ndelta\r\n"
    path.write_bytes(before)

    _edit(path, "bravo", "BRAVO", replace_all=True)
    after = path.read_bytes()

    head, tail = before.split(b"bravo")
    assert after.startswith(head) and after.endswith(tail)
    assert after == b"alpha\r\nBRAVO\r\ncharlie\r\ndelta\r\n"


# ── what must not change ───────────────────────────────────────────


def test_an_lf_file_is_byte_for_byte_what_it_was(tmp_path):
    """The behaviour before the fix, for the files that are the overwhelming majority."""
    path = tmp_path / "f.txt"
    path.write_bytes(b"hello world\nfoo bar\n")

    _edit(path, "foo bar", "baz qux")

    data = path.read_bytes()
    assert data == b"hello world\nbaz qux\n"
    assert b"\r" not in data, f"a carriage return was introduced: {data!r}"


def test_a_refused_edit_writes_nothing(crlf_file):
    """`not found` is a refusal, not a rewrite — the file is the same object on disk."""
    before = crlf_file.read_bytes()

    result = _edit(crlf_file, "nowhere in this file", "x")

    assert result.error
    assert crlf_file.read_bytes() == before


def test_a_repeated_old_string_writes_nothing(tmp_path):
    before = b"same\r\nother\r\nsame\r\n"
    path = tmp_path / "f.txt"
    path.write_bytes(before)

    result = _edit(path, "same", "SAME")

    assert result.error and "found 2 times" in result.content
    assert path.read_bytes() == before


def test_replace_all_keeps_the_terminators_too(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"same\r\nother\r\nsame\r\n")

    result = _edit(path, "same", "SAME", replace_all=True)

    assert not result.error and "2 replacements" in result.content
    assert path.read_bytes() == b"SAME\r\nother\r\nSAME\r\n"


# ── the model's view is the view the match is taken against ────────


def test_a_match_taken_from_the_read_tool_matches_a_crlf_file(crlf_file):
    """Criterion 3: the read tool shows `\\n`, so `old_string` copied from it must match.

    Taken as the read tool's own text — the tab-separated content of two of its numbered
    lines — rather than re-typed, so the test fails if the two readers stop agreeing.
    """
    shown = _lines(crlf_file)
    body = [line.split("\t", 1)[1] for line in shown.splitlines() if "\t" in line]
    old = "\n".join(body[1:3])  # "second\nthird", as displayed
    assert old == "second\nthird"

    result = _edit(crlf_file, old, "SECOND\nTHIRD")

    assert not result.error, f"the model's own view did not match: {result.content}"
    assert crlf_file.read_bytes() == b"first\r\nSECOND\r\nTHIRD\r\n"


def test_an_old_string_written_with_crlf_matches(tmp_path):
    """Both sides are read the way the reader reads them — a CRLF caller matches too."""
    path = tmp_path / "f.txt"
    path.write_bytes(b"one\r\ntwo\r\n")

    result = _edit(path, "one\r\ntwo", "1\n2")

    assert not result.error, f"CRLF old_string did not match: {result.content}"
    assert path.read_bytes() == b"1\r\n2\r\n"


@pytest.mark.parametrize(
    "written",
    ["second\ninserted", "second\r\ninserted"],
    ids=["caller-wrote-LF", "caller-wrote-CRLF"],
)
def test_a_new_line_inserted_into_a_crlf_file_is_crlf(crlf_file, written):
    """The hazard: an inserted LF is exactly what makes cmd.exe misparse a .cmd.

    Both spellings, because they are different branches and only one of them was covered
    at first: the model writes `\\n` (its view of every file is normalised), and a caller
    who saw `\\r\\n` writes that — neither may end up as a bare LF, and the second may not
    end up doubled. Measured 2026-10-02: with the first spelling only, the arm that
    skipped the retarget passed this test, so the branch was unheld.
    """
    _edit(crlf_file, "second", written)

    data = crlf_file.read_bytes()
    assert data == b"first\r\nsecond\r\ninserted\r\nthird\r\n", (
        f"the inserted line did not join the file's convention: {data!r}"
    )
    assert data.count(b"\r\n") == data.count(b"\n")


def test_a_new_string_written_with_crlf_is_not_doubled(tmp_path):
    """The regression #1804 landed, pinned to the bytes it wrote.

    Measured 2026-10-02 on `bc114ab9` (the merge of #1804) with the real tool: replacing
    `beta` in a uniformly CRLF file with `"b1\\r\\nb2"` produced
    `b'alpha\\r\\nb1\\r\\rb2\\r\\ngamma\\r\\n'` — the file's endings were restored by a
    blanket ``replace("\\n", "\\r\\n")`` applied to a `new_string` that had never been read
    the way the file is read, so the `\\r\\n` inside it came back out as `\\r\\r\\n`. Master
    `256400f4`, before that commit, produced no lone CR at all.

    The assertion is a byte string rather than a count because the defect *is* one byte,
    and it survives every check that only counts `\\r\\n` — this test's own file has four
    of them either way. It is the same class the module exists for: a shape the tool
    never promised, written on the success path where nothing reports it.
    """
    path = tmp_path / "f.txt"
    path.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    assert not _edit(path, "beta", "b1\r\nb2").error

    data = path.read_bytes()
    assert data == b"alpha\r\nb1\r\nb2\r\ngamma\r\n", data
    assert b"\r\r" not in data, f"a lone CR came through: {data!r}"


# ── the rule, read directly ────────────────────────────────────────


def test_the_terminator_is_the_files_own_spelling():
    """`terminator` is where the decision lives, so it is read here rather than inferred."""
    for text, expected in [
        ("a\r\nb\r\n", CRLF),          # uniform CRLF
        ("a\nb\n", LF),                # uniform LF
        ("a\r\nb\nc\r\n", CRLF),       # mixed: the commoner spelling
        ("a\nb\r\nc\n", LF),           # mixed: the commoner spelling
        ("a\r\nb\n", CRLF),            # a tie goes to the first break in the file
        ("a\nb\r\n", LF),              # ... in either order
        ("a\rb\r", CR),                # classic-Mac breaks are the file's too
        ("no break at all", LF),       # nothing to join: the platform-neutral default
    ]:
        spans = view(text)[1]
        assert terminator(spans, text) == expected, f"{text!r} answered {terminator(spans, text)!r}"


def test_a_view_is_the_readers_text_and_a_map_back_to_the_file():
    """The map is what lets a match found in the view be spliced into the raw text."""
    raw = "a\r\nb\rc\nd"
    seen, spans = view(raw)

    assert seen == "a\nb\nc\nd"
    assert len(spans) == len(seen)
    for (start, end), char in zip(spans, seen):
        spelled = raw[start:end]
        if char == "\n" and spelled != "\n":
            assert spelled in (CRLF, CR), f"{spelled!r} does not read as a line break"
        else:
            assert spelled == char
    # The spans tile the file exactly — no byte is dropped and none is re-rendered.
    assert "".join(raw[start:end] for start, end in spans) == raw


def test_the_real_windows_launcher_class_keeps_its_endings():
    """The file this defect was measured against, not a fixture shaped like it.

    `bin/emrgd.cmd` is pinned CRLF by `.gitattributes`; the same replacement the tool
    would make is applied to its text here (a copy — this test does not write the
    checkout), and the guard's own question (`crlf == lf`) is asked of the result.
    """
    path = REPO_ROOT / "bin" / "emrgd.cmd"
    before = path.read_bytes().decode("utf-8")
    assert before.count(CRLF) > 5 and CR not in before.replace(CRLF, ""), (
        "the fixture file is not the CRLF class this test is about"
    )

    first_line = before.split(CRLF)[0]
    after, count = replace(before, first_line, first_line, replace_all=False)

    assert count == 1
    assert after == before, "an edit that changes nothing still rewrote the file"
    assert after.count(CRLF) == after.count("\n"), "the launcher stopped being pure CRLF"
