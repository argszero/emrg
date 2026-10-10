"""Tests for the grep tool."""

import asyncio
import re
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


class TestTheHiddenDirectoryItKeepsIsDeclared:
    """`grep` drops every hidden dot-part except `.emrg`, and said only "hidden dirs".

    Measured 2026-10-07 (`cyc20261007-000240`) with the tool itself: a search from a root
    holding `.emrg/memory/MEMORY.md` and `.git/config` returns the `.emrg` file and never
    the `.git` one, while the description promised "automatic binary/hidden file skipping"
    and the class docstring "Skips binary files, hidden dirs, and files over 512KB". The
    exception is deliberate — `.emrg` is the agent's own state — so the claim is what is
    wrong, exactly as in the sibling `glob` tool (issue #1880).
    """

    def _tree(self, root: Path) -> None:
        (root / ".emrg" / "memory").mkdir(parents=True)
        (root / ".emrg" / "memory" / "MEMORY.md").write_text("# index\nNEEDLE here\n")
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("NEEDLE in git config\n")
        (root / "src.py").write_text("NEEDLE in source\n")

    def _search(self, path: Path):
        return _run(GrepTool().execute({
            "pattern": "NEEDLE", "path": str(path), "intent": "hidden-dir probe",
        }))

    def test_the_definition_names_the_one_hidden_directory_that_is_read(self):
        tool = GrepTool()
        assert ".emrg" in tool.definition().description, (
            "the description claims hidden-file skipping without its exception"
        )
        assert ".emrg" in (type(tool).__doc__ or ""), (
            "the class docstring claims it skips hidden dirs without its exception"
        )

    def test_the_emrg_directory_it_declares_as_read_really_is_read(self, tmp_path):
        self._tree(tmp_path)

        content = self._search(tmp_path).content

        # `.as_posix()`, because the tool now renders `path.relative_to(root).as_posix()`:
        # a report's path is `/`-separated on every platform. Both lines used to be
        # `str(Path(...) / ...)` — the platform's separator — which was portable only while
        # the tool was not, and both went red on `test-windows` the moment it became
        # deterministic (run 37990298947, 2026-10-10). The earlier history is in the note
        # they replace (run 37495221347, 2026-10-06, the POSIX literal failing the same leg).
        #
        # The negative one is the sharper half, and the rewrite makes it sharper still: it
        # exists to notice a `.git` that was searched when the description says it is
        # skipped, and while the tool rendered the platform's separator the comparison
        # against `.git\\config` on Windows could not fail whatever the tool did — a false
        # green about the very directory this test is for. Now both sides are `/`-separated,
        # so it fails exactly when `.git` was searched.
        assert (Path(".emrg") / "memory" / "MEMORY.md").as_posix() in content, (
            f"the .emrg file the description says is read was not searched:\n{content}"
        )
        assert (Path(".git") / "config").as_posix() not in content, (
            "the .git directory the description says is skipped was searched"
        )
        assert "src.py" in content



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


# ── the numeric parameters' domains (issue #1935) ─────────────────────────────
#
# Measured 2026-10-08 (`cyc20261008-175325`) on a one-match fixture. A negative
# context was not merely odd: `max(0, i - context_before)` starts *after* the
# match, so the block printed its `five.txt:3:` header and **no line at all** —
# the matching line was dropped — and the same value collapsed the stop budget
# `max_results * (2 + cb + ca)` below zero, so the search stopped at the first
# match and the summary read "the search stopped at its result budget
# (max_results=200)", with a remedy ("raise max_results") aimed at a cause that
# was not the cause. Both are refused now.


def test_grep_refuses_a_negative_context(tmp_path):
    """A negative context is refused, and no false budget claim is made."""
    f = tmp_path / "five.txt"
    f.write_text("alpha\nbeta\nGAMMA\ndelta\nepsilon\n")
    tool = GrepTool()
    for key in ("context_before", "context_after"):
        result = _run(tool.execute({"pattern": "GAMMA", "path": str(f), key: -2}))
        assert result.error, f"{key}=-2 was accepted"
        assert key in result.content and "-2" in result.content
        # The defect's second half: a cut attributed to a budget that was not the
        # cause. Nothing may be claimed about a search that did not run.
        assert "Found" not in result.content
        assert "result budget" not in result.content
        assert "GAMMA" not in result.content, "the block was not rendered at all or half-rendered"


def test_grep_refuses_a_context_that_is_not_a_number(tmp_path):
    """A value that cannot be read as a count is refused, not read as 0."""
    f = tmp_path / "five.txt"
    f.write_text("alpha\nGAMMA\n")
    result = _run(GrepTool().execute({
        "pattern": "GAMMA", "path": str(f), "context_before": "two",
    }))
    assert result.error
    assert "context_before" in result.content and "two" in result.content
    assert "Found" not in result.content


def test_grep_refuses_a_budget_below_one(tmp_path):
    """0 and negative budgets are refused — 0 used to be read as the default."""
    f = tmp_path / "five.txt"
    f.write_text("alpha\nbeta\nGAMMA\ndelta\nepsilon\n")
    tool = GrepTool()
    for value in (0, -1):
        result = _run(tool.execute({
            "pattern": "GAMMA", "path": str(f), "max_results": value,
        }))
        assert result.error, f"max_results={value} was accepted"
        assert "max_results" in result.content and str(value) in result.content
        assert "Found" not in result.content


def test_grep_context_inside_its_domain_still_renders_the_block(tmp_path):
    """The control: a legal context still shows the match and its neighbours.

    Without it, "refused" could pass for "handled" — the three refusals above only
    prove the tool says no, never that it still says yes where it should.
    """
    f = tmp_path / "five.txt"
    f.write_text("alpha\nbeta\nGAMMA\ndelta\nepsilon\n")
    result = _run(GrepTool().execute({
        "pattern": "GAMMA", "path": str(f), "context_before": 1, "context_after": 1,
    }))
    assert not result.error
    assert "Found 1 matches" in result.content
    for line in ("beta", ">GAMMA", "delta"):
        assert line in result.content
    assert "result budget" not in result.content, "a complete search is not a cut one"


# --- the glob filter: applied to every subject, and never to a pattern it cannot read ---


def test_the_glob_filter_applies_to_a_named_file_too(tmp_path):
    """`glob` was read by the directory branch and **ignored** by the file branch.

    Measured 2026-10-10 (`cyc20261010-220909`) on `emrg/tools/read_tool.py`: with
    `glob='*.nomatch'` the call returned the same one match as no filter at all, and the
    summary still printed `matching '*.nomatch'` — the output claimed a filter that had
    never run. The description says "Only search files matching this glob pattern", and a
    named file is still a file the filter can exclude.

    Both directions, because a filter that excludes everything would pass the
    exclusion half alone.
    """
    tool = GrepTool()
    named = tmp_path / "main.py"
    named.write_text("import os\n\ndef main():\n    return 1\n")

    unfiltered = _run(tool.execute({"pattern": "def main", "path": str(named)}))
    assert not unfiltered.error
    assert "Found 1 matches" in unfiltered.content, unfiltered.content
    assert "searched 1 files" in unfiltered.content

    matching = _run(tool.execute({
        "pattern": "def main", "path": str(named), "glob": "*.py",
    }))
    assert not matching.error
    assert "Found 1 matches" in matching.content, matching.content
    assert "searched 1 files" in matching.content

    excluded = _run(tool.execute({
        "pattern": "def main", "path": str(named), "glob": "*.nomatch",
    }))
    assert not excluded.error
    # The discriminating assertion first: `Found` below is also satisfied by a *wrong*
    # match, so an arm that drops the filter has to die on the line that says nothing was
    # searched (the arm runner reports a kill on a later line as UNJUDGEABLE).
    assert "searched 0 files" in excluded.content, (
        "a file path plus a non-matching glob must search nothing — the filter was "
        f"ignored here before this cycle: {excluded.content}"
    )
    assert "Found" not in excluded.content, excluded.content
    # The reading has to say *why*, or `searched 0 files matching '*.nomatch'` reads as
    # "that tree holds none" rather than "the filter you passed excluded the one file you
    # pointed at". The two have different remedies.
    assert "does not match the glob filter" in excluded.content, excluded.content
    assert "main.py" in excluded.content, excluded.content


def test_a_brace_glob_is_refused_rather_than_searched_to_an_empty_answer(tmp_path):
    """`*.{py,rs}` is the pattern the description advertised, and this walk cannot read it.

    `Path.rglob` has no brace expansion, so the whole pattern is one literal string that
    matches no file whose name contains a brace. Measured 2026-10-10 (`cyc20261010-220909`)
    over `emrg/tools/`: `glob='*.py'` -> `Found 9 matches ... (searched 15 files)`,
    `glob='*.{py,rs}'` -> `No matches ... (searched 0 files)`. The second sentence is the
    one a real absence produces, so a model following the description's own example was
    told "nothing here" about a question the tool never asked.

    Refusing is the reading this repository uses for a request it cannot answer (the
    `count_argument` family): the alternative is an empty answer that is indistinguishable
    from a true one.
    """
    tool = GrepTool()
    f = tmp_path / "main.py"
    f.write_text("import os\n")

    brace = _run(tool.execute({
        "pattern": "import", "path": str(tmp_path), "glob": "*.{py,rs}",
    }))
    assert brace.error, brace.content
    assert brace.content.startswith("Error:"), (
        "the refusal has to be an error, not an empty reading with an explanation "
        f"beside it: {brace.content}"
    )
    assert "brace" in brace.content, brace.content
    assert "does not expand" in brace.content, brace.content

    # The control: the same search, spelled the way the filter can read.
    control = _run(tool.execute({
        "pattern": "import", "path": str(tmp_path), "glob": "*.py",
    }))
    assert not control.error, control.content
    assert "Found 1 matches" in control.content, (
        "the pattern the brace example meant has to keep working, or 'refused' is "
        f"indistinguishable from 'handled': {control.content}"
    )


def _advertised_globs() -> list[str]:
    """Every glob example the `glob` parameter's description holds.

    Read from the schema rather than restated: the description is the contract the model
    reads, so an example in it is a promise, and a promise nothing measures is how
    `*.{py,rs}` survived in that list while selecting nothing.
    """
    param = GrepTool().definition().parameters["properties"]["glob"]["description"]
    assert "Examples:" in param, (
        f"the glob parameter states no examples, so this reader has no subject: {param}"
    )
    # Split on the two markers rather than on a period: the examples themselves contain
    # periods (`'*.py'`), and a non-greedy match up to the first one parses an unterminated
    # quote and returns nothing — the shape this reader had before it was fixed to read the
    # same description its author wrote.
    section = param.split("Examples:", 1)[1].split("Default:", 1)[0]
    return re.findall(r"'([^']+)'", section)


def test_every_glob_example_the_description_advertises_selects_files(tmp_path):
    """Each advertised example is run, against a tree holding the file it names.

    An example that selects nothing is worse than a missing one: the caller reads
    `No matches` and concludes the pattern is absent from the tree. That is the defect
    this cycle fixed for one entry of this very list — so the list is held to it, and a
    later edit that reintroduces an unreadable form goes red here rather than in a
    model's answer.
    """
    globs = _advertised_globs()
    assert globs, "no examples parsed out of the description"

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("import os\n")
    (tmp_path / "src" / "app.ts").write_text("import x\n")
    (tmp_path / "README.md").write_text("# Project\n")

    for glob_example in globs:
        result = _run(GrepTool().execute({
            "pattern": ".", "path": str(tmp_path), "glob": glob_example,
        }))
        assert not result.error, (
            f"the description advertises {glob_example!r}, but the tool refuses it: "
            f"{result.content}"
        )
        assert "searched 0 files" not in result.content, (
            f"the description advertises {glob_example!r}, which selects no file at all — "
            f"the caller would read 'No matches' as a fact about the tree: {result.content}"
        )


def test_a_literal_brace_name_in_the_filter_selects_its_file(tmp_path):
    """The filter's brace refusal must not fire on a name the walk can read literally.

    The refusal added for `*.{py,rs}` first fired on `"{" in filter or "}" in filter`, and
    that refused filters `Path.rglob` reads correctly: on a tree holding a file named
    `a{b}.py`, master's directory branch selected it and the branch refused the call, with a
    message asserting `{a,b}` alternation the input does not carry. Measured 2026-10-10
    (`cyc20261011-001130`) in this checkout. This repository named that class once already
    (`emrg/tools/command_scan.py`, the #1513 lesson: `echo sh "patch …"` was a bug, not a
    safe over-block).

    Alternation needs two alternatives, so the trigger is a comma inside the braces — and
    the tool additionally requires that the filter selected nothing, which is what makes the
    refusal a reading rather than a prediction. Both halves are asserted, because each alone
    is satisfied by a wrong rule: dropping the refusal passes the literal half, and the
    over-block passed the alternation half.
    """
    tool = GrepTool()
    literal = tmp_path / "a{b}.py"
    literal.write_text("needle here\n")
    comma = tmp_path / "a{b,c}.py"
    comma.write_text("needle here\n")

    through_the_directory = _run(tool.execute({
        "pattern": "needle", "path": str(tmp_path), "glob": "a{b}.py",
    }))
    assert not through_the_directory.error, (
        "the directory branch reads `a{b}.py` literally and selects the file that holds "
        f"it; refusing it blocks a filter that works: {through_the_directory.content}"
    )
    assert "Found 1 matches" in through_the_directory.content, through_the_directory.content
    assert "searched 1 files" in through_the_directory.content, through_the_directory.content

    through_the_named_file = _run(tool.execute({
        "pattern": "needle", "path": str(literal), "glob": "a{b}.py",
    }))
    assert not through_the_named_file.error, through_the_named_file.content
    assert "Found 1 matches" in through_the_named_file.content, through_the_named_file.content

    # The other half of the discriminator: a comma between the braces is alternation, it
    # selected nothing, and it is still refused rather than searched to `No matches`.
    alternation = _run(tool.execute({
        "pattern": "needle", "path": str(tmp_path), "glob": "*.{py,rs}",
    }))
    assert alternation.error, (
        "`*.{py,rs}` is the pattern the description used to advertise, and it selects "
        f"nothing — the answer has to be the refusal: {alternation.content}"
    )
    assert "brace" in alternation.content, alternation.content

    comma_but_selecting = _run(tool.execute({
        "pattern": "needle", "path": str(tmp_path), "glob": "a{b,c}.py",
    }))
    assert not comma_but_selecting.error, (
        "a comma-bearing filter that selects the named file is answered, not refused: the "
        f"refusal is gated on the filter having selected nothing: {comma_but_selecting.content}"
    )
    assert "Found 1 matches" in comma_but_selecting.content, comma_but_selecting.content

    # And a literal brace name that is simply *absent* gets the empty answer, not the
    # refusal: `{b}` alternates between nothing, so refusing it asserts alternation the
    # pattern does not carry. This is the reading that tells the comma predicate apart from
    # "any brace" — with the wide predicate the gate alone still refuses this call whenever
    # no file matches, and the files above cannot separate the two.
    missing = _run(tool.execute({
        "pattern": "needle", "path": str(tmp_path), "glob": "zz{b}.py",
    }))
    assert not missing.error, (
        "no file matches `zz{b}.py`, and that is the true answer — `{b}` alternates "
        f"between nothing, so this is not alternation: {missing.content}"
    )
    assert "No matches" in missing.content, missing.content

    # A named file with an unreadable filter is refused for the filter's reason, not told
    # that it merely failed to match: `_selects` matches with `fnmatch`, which does not
    # expand braces either, so the exclusion and the empty walk have one cause — the pattern
    # — and the remedy is to change it, not to look at the file.
    named_unreadable = _run(tool.execute({
        "pattern": "needle", "path": str(literal), "glob": "*.{py,rs}",
    }))
    assert named_unreadable.error, (
        "the filter cannot be read at all, so this is the refusal and not a match "
        f"failure the caller would fix by looking at the file: {named_unreadable.content}"
    )
    assert "brace" in named_unreadable.content, named_unreadable.content


def test_the_glob_description_states_the_refusal_the_tool_gives(tmp_path):
    """The description is the behaviour's second home, so a refusal it does not mention
    is a surprise the caller could have been spared.

    Both halves in one test, because either alone is satisfied by a wrong rule: a
    description that promises a refusal an unread filter never gives, and a refusal whose
    description says nothing about it, are the two ways this pair drifts apart. The
    repository's own shape for that is `test_read_never_cuts_a_line` — a promise in a tool
    description that no test holds is how a claim survives review.
    """
    param = GrepTool().definition().parameters["properties"]["glob"]["description"]
    assert "brace-alternation" in param and "refused with that reason" in param, (
        f"the description no longer says what an unreadable filter gets: {param}"
    )

    (tmp_path / "main.py").write_text("needle\n")
    refused = _run(GrepTool().execute({
        "pattern": "needle", "path": str(tmp_path), "glob": "*.{py,rs}",
    }))
    assert refused.error, (
        f"the description says this is refused, and it was searched instead: {refused.content}"
    )
    # The description's second clause — "rather than searched to `No matches`" — is the
    # half that matters on its own: an empty reading beside an explanation is what this
    # whole PR exists to remove. The refusal *quotes* that sentence while refusing it, so
    # the reading is the answer's own opening, not a substring.
    assert refused.content.startswith("Error:"), refused.content
    assert not refused.content.startswith("No matches"), refused.content
    assert "brace" in refused.content, refused.content
def test_the_siblings_spelling_of_path_selects_the_same_tree(temp_cwd):
    """`glob` names this parameter `workdir`; `grep` reads that spelling too (issue #2071).

    Measured 2026-10-11 (`cyc20261011-015723`) on master `63ee3a54`: `grep
    workdir=<a two-file tempdir>` searched this checkout instead and answered
    `Found 79 matches … (searched 4796 files)` — a whole-repo scan shaped exactly like
    an answer to the caller's question.

    The fixture's own file is asserted **before** the comparison, because two readings
    that both fell back to the cwd would compare equal as well; an equality that holds
    for the wrong reason is not evidence that the alias works.
    """
    tool = GrepTool()
    declared = _run(tool.execute({"pattern": "import", "path": str(temp_cwd)}))
    sibling = _run(tool.execute({"pattern": "import", "workdir": str(temp_cwd)}))

    assert "src/main.py" in declared.content, declared.content
    assert not sibling.error
    assert sibling.content == declared.content


def test_the_path_description_names_the_spelling_the_reader_accepts():
    """A description is read as the domain its reader enforces, so it names the alias."""
    description = GrepTool().definition().parameters["properties"]["path"]["description"]
    assert "`workdir`" in description, description
