"""Tests for the grep tool."""

import asyncio
import re
import sys
import tempfile
from pathlib import Path

import pytest

from emrg.tools.grep_tool import MAX_FILE_SIZE, GrepTool


@pytest.fixture
def temp_cwd():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text(
            "import os\n\ndef main():\n    print('hello')"
        )
        (root / "src" / "utils.py").write_text(
            "import sys\nimport os\n\ndef helper():\n    return True"
        )
        (root / "tests").mkdir()
        (root / "tests" / "test_main.py").write_text(
            "import pytest\nfrom src.main import main\n\ndef test_main():\n    main()\n"
        )
        (root / "README.md").write_text("# Project\n\nA test project.\n")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "compiled.pyc").write_text("binary")
        yield root


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.skipif(sys.platform == "win32", reason="path separator differs (\\ vs /)")
def test_grep_simple(temp_cwd):
    tool = GrepTool()
    result = _run(tool.execute({"pattern": "import", "path": str(temp_cwd)}))
    assert not result.error
    assert "src/main.py" in result.content
    assert "src/utils.py" in result.content
    assert "test_main.py" in result.content


def test_grep_glob_filter(temp_cwd):
    tool = GrepTool()
    # Non-recursive glob: only root-level .py files (__pycache__ excluded)
    result = _run(tool.execute({
        "pattern": "import", "path": str(temp_cwd), "glob": "*.py"
    }))
    # With rglob("*.py"), it'll match nested files too — that's correct behavior.
    # Just verify it found matches.
    assert "Found" in result.content


def test_grep_case_insensitive(temp_cwd):
    tool = GrepTool()
    result = _run(tool.execute({
        "pattern": "PROJECT", "path": str(temp_cwd), "ignore_case": True,
    }))
    assert "README.md" in result.content


def test_grep_no_match(temp_cwd):
    tool = GrepTool()
    result = _run(tool.execute({
        "pattern": "XYZ_NOT_FOUND", "path": str(temp_cwd),
    }))
    assert "No matches" in result.content


def test_grep_invalid_regex():
    tool = GrepTool()
    result = _run(tool.execute({"pattern": "[unclosed"}))
    assert result.error
    assert "invalid regex" in result.content.lower()


def test_grep_no_pattern():
    tool = GrepTool()
    result = _run(tool.execute({"pattern": ""}))
    assert result.error


def test_grep_context_lines(temp_cwd):
    tool = GrepTool()
    result = _run(tool.execute({
        "pattern": "def main", "path": str(temp_cwd / "src" / "main.py"),
        "context_before": 2, "context_after": 1,
    }))
    # With context_before=2, "import os" (2 lines before match) should appear
    assert "import os" in result.content
    assert "print" in result.content


def test_the_last_newline_is_not_a_searchable_line(temp_cwd):
    """A file's terminating newline terminates its last line; it does not add one.

    `"a\\nb\\n".split("\\n")` yields `['a', 'b', '']`, and the trailing `''` used to be
    searched — so any pattern that can match an empty line reported a match **past the
    end of every terminated file**. Measured on master `bc114ab`, 2026-10-02: a
    two-line file searched for `^$` came back as "Found 1 matches ... t.txt:3:".
    """
    tool = GrepTool()
    f = temp_cwd / "two.txt"
    f.write_text("a\nb\n")          # two lines, both terminated

    result = _run(tool.execute({"pattern": "^$", "path": str(f)}))
    assert "No matches" in result.content, result.content

    # The same file's real lines are all still found (the opposite direction: the
    # last line is not dropped along with the phantom one).
    result = _run(tool.execute({"pattern": "^", "path": str(f)}))
    assert "Found 2 matches" in result.content, result.content
    assert "two.txt:2:" in result.content
    assert "two.txt:3:" not in result.content

    # An unterminated file is unchanged: its last line is a line.
    g = temp_cwd / "two_no_eol.txt"
    g.write_text("a\nb")
    result = _run(tool.execute({"pattern": "^", "path": str(g)}))
    assert "Found 2 matches" in result.content, result.content

    # A file with no lines has nothing to match — before, `^$` on a zero-byte file
    # reported "Found 1 matches ... zero.txt:1:".
    z = temp_cwd / "zero.txt"
    z.write_bytes(b"")
    result = _run(tool.execute({"pattern": "^$", "path": str(z)}))
    assert "No matches" in result.content, result.content


def test_definition():
    tool = GrepTool()
    d = tool.definition()
    assert d.name == "grep"
    assert "pattern" in d.parameters.get("properties", {})
    assert d.parameters.get("required") == ["pattern", "intent"]


def test_skips_hidden_dirs(temp_cwd):
    """__pycache__ should not be searched."""
    tool = GrepTool()
    result = _run(tool.execute({"pattern": "binary", "path": str(temp_cwd)}))
    assert "No matches" in result.content


def test_grep_nonexistent_path():
    """Searching a non-existent path should return an error."""
    tool = GrepTool()
    result = _run(tool.execute({
        "pattern": "import",
        "path": "/nonexistent/path/xyzzy",
    }))
    assert result.error
    assert "not found" in result.content.lower()


# ── The reported count, and the truncation note ──────────────────────


def _payload(content: str) -> list[str]:
    """The printed match blocks: summary, blank lines and the notice removed."""
    return [ln for ln in content.splitlines()[2:] if ln and not ln.startswith("...")]


def _headers(payload: list[str]) -> list[str]:
    """Block header lines — the ones a match produced, not a context line."""
    return [ln for ln in payload if not ln.startswith(" ") and ln.endswith(":")]


def _reported(content: str) -> int:
    return int(re.search(r"Found (\d+) matches", content).group(1))


class TestTheCountIsTheNumberOfMatches:
    """Issue #1805 — asking for context used to *create* matches.

    The count was re-derived from the rendered lines with "ends with a colon", and a
    context line is emitted with its own text intact — so every YAML block key, code
    label or `Term:` inside the window was counted as a match. Measured on master
    `6b417c4`, 2026-10-02: one matching line with `first:` / `second:` / `third:`
    around it printed **Found 4 matches** at `context_before=1, context_after=2` and
    **Found 1 matches** for the same search without context.
    """

    def test_a_context_line_ending_in_a_colon_is_not_a_match(self, tmp_path):
        f = tmp_path / "conf.yaml"
        f.write_text("first:\nTARGET here\nsecond:\nthird:\n", encoding="utf-8")

        result = _run(GrepTool().execute({
            "pattern": "TARGET", "path": str(f),
            "context_before": 1, "context_after": 2,
        }))

        assert not result.error
        assert _reported(result.content) == 1, "a context line was counted as a match"
        assert len(_headers(_payload(result.content))) == 1

    def test_the_same_search_reports_the_same_count_with_and_without_context(self, tmp_path):
        f = tmp_path / "conf.yaml"
        f.write_text("first:\nTARGET here\nsecond:\nthird:\n", encoding="utf-8")

        tool = GrepTool()
        plain = _run(tool.execute({"pattern": "TARGET", "path": str(f)}))
        with_ctx = _run(tool.execute({
            "pattern": "TARGET", "path": str(f), "context_before": 1, "context_after": 2,
        }))

        assert _reported(plain.content) == _reported(with_ctx.content) == 1

    def test_the_count_is_the_number_of_matching_lines(self, tmp_path):
        (tmp_path / "a.yaml").write_text("k:\nTARGET one\nv:\n", encoding="utf-8")
        (tmp_path / "b.yaml").write_text("w:\nTARGET two\nTARGET three\nx:\n", encoding="utf-8")

        for context in (0, 1, 3):
            result = _run(GrepTool().execute({
                "pattern": "TARGET", "path": str(tmp_path),
                "context_before": context, "context_after": context,
            }))
            assert _reported(result.content) == 3, f"context={context} changed the count"

    def test_a_context_line_that_ends_in_a_colon_is_still_printed(self, tmp_path):
        """The fix is the number, not the output — the context is what was asked for."""
        f = tmp_path / "conf.yaml"
        f.write_text("first:\nTARGET here\nsecond:\n", encoding="utf-8")

        result = _run(GrepTool().execute({
            "pattern": "TARGET", "path": str(f),
            "context_before": 1, "context_after": 1,
        }))

        assert "  first:" in result.content
        assert "  second:" in result.content
        assert " >TARGET here" in result.content


class TestTheTruncationNoteNamesWhatItMeasured:
    """The old note named ``max_results`` "match blocks" while cutting at
    ``max_results * 3`` **lines** — fewer blocks than it claimed, and the cut could
    land inside a block, printing a header with nothing under it (measured on master
    `6b417c4`: last payload line was a header; note said 10 blocks, 7 were printed).
    """

    MATCHES = 80

    def _big_file(self, tmp_path) -> Path:
        body = []
        for i in range(self.MATCHES):
            body += [f"TARGET {i}:", "  a: 1", "  b: 2", "  c: 3"]
        f = tmp_path / "conf.yaml"
        f.write_text("\n".join(body) + "\n", encoding="utf-8")
        return f

    def _run_truncated(self, path: Path, max_results: int = 10):
        return _run(GrepTool().execute({
            "pattern": "^TARGET ", "path": str(path),
            "context_before": 1, "context_after": 2, "max_results": max_results,
        }))

    def test_the_output_ends_on_a_complete_block(self, tmp_path):
        result = self._run_truncated(self._big_file(tmp_path))
        payload = _payload(result.content)

        assert payload, "the truncation printed nothing"
        assert payload[0] in _headers(payload), "the payload no longer starts with a header"
        assert payload[-1] not in _headers(payload), (
            "the cut fell inside a block, so its header was printed with no lines under it"
        )

    def test_the_note_names_the_blocks_shown_and_the_blocks_found(self, tmp_path):
        result = self._run_truncated(self._big_file(tmp_path))
        payload = _payload(result.content)
        match = re.search(r"\[output truncated: (\d+) of (\d+) match blocks shown\]", result.content)

        assert match, result.content.splitlines()[-1]
        shown, total = int(match.group(1)), int(match.group(2))
        assert shown == len(_headers(payload)), "the note's 'shown' is not the number printed"
        assert total == _reported(result.content), (
            "the note's total is not the count the summary reported"
        )
        assert 0 < shown < total, "this fixture is meant to truncate"

    def test_an_untruncated_search_carries_no_note(self, tmp_path):
        f = tmp_path / "small.yaml"
        f.write_text("k:\nTARGET one\n", encoding="utf-8")

        result = _run(GrepTool().execute({
            "pattern": "TARGET", "path": str(f), "context_before": 1, "context_after": 1,
        }))

        assert "output truncated" not in result.content
        assert not _payload(result.content)[-1].startswith("...")

    def test_a_budget_smaller_than_one_block_still_prints_the_first(self, tmp_path):
        """The cut keeps at least one block: a note with no output under it is worse
        than exceeding a soft line budget."""
        result = self._run_truncated(self._big_file(tmp_path), max_results=1)
        payload = _payload(result.content)

        assert len(_headers(payload)) == 1, (
            "a budget smaller than one block printed the notice with no match under it"
        )
        assert payload[-1] not in _headers(payload)


class TestTheCountSaysWhenItIsAFloor:
    """Issue #1805, second cause — the search itself stops at a budget and said nothing.

    ``max_results`` bounds a **line** budget (``max_results * (2 + cb + ca)``, about
    ``max_results`` matches) and the summary printed the number it reached as if it were
    the number in the tree. Measured on master `6b417c4`, 2026-10-02: a file holding
    **4000** matching lines came back as ``Found 11 matches ... (searched 1 files)`` with
    no indication that the search had stopped — a 364x undercount that reads as a total.
    """

    LINES = 4000
    HITS = "HIT"

    def _big_file(self, tmp_path) -> Path:
        f = tmp_path / "big.txt"
        f.write_text(f"{self.HITS}\n" * self.LINES, encoding="utf-8")
        return f

    def _search(self, path: Path, max_results: int = 10, context: int = 0):
        return _run(GrepTool().execute({
            "pattern": self.HITS, "path": str(path), "max_results": max_results,
            "context_before": context, "context_after": context,
            "intent": "floor probe",
        }))

    def test_a_capped_search_says_the_count_is_a_floor(self, tmp_path):
        result = self._search(self._big_file(tmp_path))

        assert "floor" in result.content, (
            f"{self.LINES} matches in the file, and the summary reported the budget "
            f"as a total: {result.content.splitlines()[0]}"
        )
        assert "max_results=10" in result.content, "the note does not name the budget"

    def test_the_note_and_the_count_name_the_same_number(self, tmp_path):
        result = self._search(self._big_file(tmp_path))
        reported = _reported(result.content)
        named = int(re.search(r"max_results=(\d+)\)", result.content).group(1))

        assert reported == len(_headers(_payload(result.content))), (
            "the count is not the number of blocks printed"
        )
        assert named == 10, "the note names a budget the call did not use"
        assert reported <= 10 + 1, "the cap is meant to stop the search near max_results"

    def test_a_bigger_budget_finds_more_of_the_same_file(self, tmp_path):
        """The number was produced by the budget, so raising it must raise the count —
        this is what a reader tells apart from a real total only if it is said."""
        f = self._big_file(tmp_path)

        assert _reported(self._search(f, max_results=10).content) < _reported(
            self._search(f, max_results=50).content
        )

    def test_an_uncapped_search_keeps_the_plain_summary(self, tmp_path):
        f = tmp_path / "small.txt"
        f.write_text("one\nHIT here\ntwo\n", encoding="utf-8")

        result = self._search(f)

        assert "floor" not in result.content
        assert "(searched 1 files)" in result.content
        assert _reported(result.content) == 1

    def test_the_floor_note_survives_the_print_truncation(self, tmp_path):
        """Both notes can be true at once: the search stopped, and the print was cut."""
        result = self._search(self._big_file(tmp_path), max_results=3, context=2)

        assert "floor" in result.content, "the floor note was lost"
        assert "output truncated" in result.content, "the print note was lost"
        assert result.content.index("floor") < result.content.index("output truncated"), (
            "the summary has to carry its own caveat, not depend on the note at the end"
        )


class TestTheCountNamesTheFilesItReallySearched:
    """`searched N files` counted files the loop only *looked at* (issue #1876).

    Measured on master `38b85268`, 2026-10-06 (`cyc20261006-214703`): the counter was
    incremented before the size and decode guards, so a tree whose **only** copies of the
    pattern were a file over `MAX_FILE_SIZE` and a binary one answered

        No matches for 'NEEDLE' in <root> (searched 4 files)

    with the two files holding `NEEDLE` counted among the four "searched". The word is a
    claim about work done; the skips are named beside it now, the way `glob` names its
    own (`PR #1875`), and the count's subject is the files really read.
    """

    HITS = "NEEDLE"

    def _tree(self, root: Path) -> None:
        (root / "small.txt").write_text("hello world\n", encoding="utf-8")
        (root / "other.txt").write_text("nothing here\n", encoding="utf-8")

    def _search(self, path: Path):
        return _run(GrepTool().execute({
            "pattern": self.HITS, "path": str(path), "intent": "skip probe",
        }))

    def test_a_file_over_the_cap_is_not_counted_as_searched(self, tmp_path):
        self._tree(tmp_path)
        (tmp_path / "big.txt").write_text(
            f"{self.HITS}\n" + "x" * (MAX_FILE_SIZE + 10), encoding="utf-8"
        )

        content = self._search(tmp_path).content

        assert "No matches" in content
        assert "(searched 2 files" in content, (
            f"two readable files were searched, and files the size guard skipped were "
            f"counted as searched too: {content}"
        )
        assert "1 skipped: 1 over 524288 bytes" in content

    def test_a_file_that_is_not_utf8_is_not_counted_as_searched(self, tmp_path):
        self._tree(tmp_path)
        (tmp_path / "binary.bin").write_bytes(b"\xff\xfe" + self.HITS.encode() + b"\x00")

        content = self._search(tmp_path).content

        assert "(searched 2 files" in content
        assert "1 skipped: 1 not readable as UTF-8 text" in content

    def test_a_search_that_skipped_nothing_says_nothing_about_skipping(self, tmp_path):
        """The control: the clause reports a measurement, so it is absent when there is none.

        A clause printed unconditionally would pass both legs above while telling a reader
        about a skip that never happened — and this is the same tree that pins the plain
        summary in `TestTheCountSaysWhenItIsAFloor`.

        Asserted on the **message**, not on the whole result: the tmp_path pytest builds
        for this test is named after the test, so `"skipped" not in content` was reading
        its own directory name and failed on the very run that should have passed.
        """
        self._tree(tmp_path)

        summary = self._search(tmp_path).content.splitlines()[0]

        assert summary.endswith("(searched 2 files)"), summary

    def test_the_hit_line_carries_the_same_breakdown(self, tmp_path):
        """Both summaries answer the same question, so both have to name the same holes."""
        self._tree(tmp_path)
        (tmp_path / "big.txt").write_text(
            f"{self.HITS}\n" + "x" * (MAX_FILE_SIZE + 10), encoding="utf-8"
        )
        (tmp_path / "small.txt").write_text(f"a {self.HITS} here\n", encoding="utf-8")

        content = self._search(tmp_path).content

        assert "Found 1 matches" in content
        assert "(searched 2 files; 1 skipped: 1 over 524288 bytes)" in content


class TestASkipNamesItsOwnReason:
    """A skip's reason is a reading, and one word held two of them (measured 2026-10-07).

    `undecodable` counted both "the bytes are not UTF-8" and "the file could not be opened
    at all" — a `stat()` failure and a `read_text()` `OSError` were routed to the same
    counter as a `UnicodeDecodeError`, and the clause named only the first:

        $ ls -l  binary.bin  locked.txt
        -rw-r--r--  17 binary.bin        # not UTF-8
        ----------  16 locked.txt        # valid UTF-8, mode 000

        Found 1 matches for 'NEEDLE' in <root> (searched 1 files; 2 skipped: 2 not
        readable as UTF-8 text)                                    # on master 65df80ac

    `locked.txt` holds `NEEDLE here too` — perfectly good UTF-8. The sentence was false
    about it, and the remedy it implies (a byte-reading search) is not the one that works
    (the path or its permissions). The two are named apart now, and the pair below is the
    test: the binary file keeps the encoding clause, the locked one gets its own.
    """

    HITS = "NEEDLE"

    def _tree(self, root: Path) -> None:
        (root / "good.txt").write_text(f"{self.HITS} in a readable file\n", encoding="utf-8")

    def _search(self, path: Path):
        return _run(GrepTool().execute({
            "pattern": self.HITS, "path": str(path), "intent": "skip reason probe",
        }))

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="mode 000 does not stop a read on Windows, so the file is searched there",
    )
    def test_an_unopenable_file_is_not_called_non_utf8(self, tmp_path):
        """The measured case, driven through the real file system."""
        self._tree(tmp_path)
        locked = tmp_path / "locked.txt"
        locked.write_text(f"{self.HITS} is right here\n", encoding="utf-8")
        (tmp_path / "binary.bin").write_bytes(b"\xff\xfe" + self.HITS.encode() + b"\x00")
        locked.chmod(0o000)
        try:
            content = self._search(tmp_path).content

            assert "2 skipped" in content, content
            assert "1 not readable as UTF-8 text" in content, (
                "the binary file is the one this clause belongs to: " + content
            )
            assert "1 could not be read" in content, (
                "a mode-000 file is valid UTF-8 that could not be opened - neither the "
                "encoding clause nor silence is the truth about it: " + content
            )
            assert "2 not readable as UTF-8 text" not in content, (
                "the locked file is being reported as non-UTF-8, which is the defect: "
                + content
            )
        finally:
            locked.chmod(0o600)  # so tmp_path can be cleaned up on every platform

    def test_a_read_that_fails_for_its_own_reason_is_classified_the_same(self, tmp_path, monkeypatch):
        """The same reading on a platform where mode bits mean nothing, so the leg is not skipped.

        Windows has no `chmod 000`, so the test above does not run there — but the branch it
        covers does exist on Windows (a file removed between the listing and the read, a
        path that cannot be opened). Driving `read_text` to raise is what keeps the
        classification measured on every platform rather than only where chmod bites.
        """
        self._tree(tmp_path)
        (tmp_path / "unopenable.txt").write_text(f"{self.HITS}\n", encoding="utf-8")

        real_read_text = Path.read_text

        def read_text(self: Path, *args, **kwargs):
            if self.name == "unopenable.txt":
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)

        content = self._search(tmp_path).content

        assert "1 skipped: 1 could not be read" in content, content
        assert "not readable as UTF-8 text" not in content, (
            "an OSError is not a statement about the file's encoding: " + content
        )
        assert "(searched 1 files" in content, (
            "a file that could not be read was not searched, so it is not in this count: "
            + content
        )

    def test_a_file_whose_size_cannot_be_measured_is_named_the_same_way(self, tmp_path, monkeypatch):
        """The `stat()` guard's skip is not an encoding claim either — and it has its own test.

        A mutation arm is what put this leg here: routing the `stat()` failure back into
        `undecodable` survived the other two tests, because a `chmod 000` file still
        `stat`s — the two guards are different code paths and only one of them was covered.
        The path is real: `_collect_files` calls `is_file()` and the loop calls `stat()`
        after it, so the two disagree exactly when a file is removed between them. Driven
        here rather than raced, and driven on every platform, because the branch exists
        wherever the tool runs.
        """
        self._tree(tmp_path)
        (tmp_path / "vanishing.txt").write_text(f"{self.HITS}\n", encoding="utf-8")

        real_stat = Path.stat
        real_is_file = Path.is_file

        def stat(self, *args, **kwargs):
            if self.name == "vanishing.txt":
                raise FileNotFoundError(2, "No such file or directory")
            return real_stat(self, *args, **kwargs)

        def is_file(self):
            # Collected first, gone by the time the loop measures it: the shape the two
            # calls disagree on, without the race that produces it in the wild.
            if self.name == "vanishing.txt":
                return True
            return real_is_file(self)

        monkeypatch.setattr(Path, "stat", stat)
        monkeypatch.setattr(Path, "is_file", is_file)

        content = self._search(tmp_path).content

        assert "1 skipped: 1 could not be read" in content, content
        assert "not readable as UTF-8 text" not in content, (
            "a size that could not be measured says nothing about the file's encoding: "
            + content
        )
