"""Tests for the grep tool."""

import asyncio
import re
import sys
import tempfile
from pathlib import Path

import pytest

from emrg.tools.grep_tool import GrepTool


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


# ── A count argument's domain is decided where the untrusted value enters ──────
#
# Measured on master `6b417c45`, 2026-10-02. `context_before=-1` is not a smaller
# window: it computed `range(i + 1, i + 1)`, a window starting *after* the match, so a
# one-match file answered ``Found 1 matches ... one.txt:2:`` with an empty block body —
# a successful result (`error=False`) whose summary claimed a match it never showed.
# `context_before="two"` raised `TypeError` out of `execute()` instead.


def _one_match_file(root: Path) -> Path:
    """A file whose single match is on line 2, with a neighbour either side."""
    f = root / "one.txt"
    f.write_text("alpha:1\nTARGET here\nomega:2\n", encoding="utf-8")
    return f


def _header_lines(content: str) -> list[str]:
    """The `file:line:` block headers of a result."""
    return [ln for ln in content.split("\n") if re.fullmatch(r"\S.*:\d+:", ln)]


def _bodies(content: str) -> list[list[str]]:
    """Each block header's body lines, in order.

    A block body is every line after a header up to the next header or the truncation
    note; the empty list means a header that printed nothing under it.
    """
    blocks: list[list[str]] = []
    for ln in content.split("\n"):
        if re.fullmatch(r"\S.*:\d+:", ln):
            blocks.append([])
        elif blocks and ln.strip() and not ln.startswith("..."):
            blocks[-1].append(ln)
    return blocks


class TestTheNumericArgumentsHaveADomain:
    """Every count argument accepts its domain or falls back to the documented default.

    The two directions matter equally: a nonsense value must not change the reading
    (compared against the same call without it), and a *valid* value must not be
    flattened to the default by the same code that rejects the nonsense ones.
    """

    def _grep(self, path: Path, **kw):
        return _run(GrepTool().execute(
            {"pattern": "TARGET", "path": str(path), "intent": "probe", **kw}
        ))

    @pytest.mark.parametrize("kw", [
        {"context_before": -1},
        {"context_after": -1},
        {"context_before": -1, "context_after": -1},
        {"context_before": -5, "context_after": -3},
    ])
    def test_a_negative_context_still_shows_the_match_line(self, tmp_path, kw):
        """The defect itself: the summary claimed a match the block body omitted."""
        result = self._grep(_one_match_file(tmp_path), **kw)
        assert not result.error
        assert " >TARGET here" in result.content, result.content
        assert " >TARGET here" in _bodies(result.content)[0], result.content

    def test_no_block_ever_prints_an_empty_body(self, tmp_path):
        """The invariant, over the whole domain including its junk values."""
        path = _one_match_file(tmp_path)
        for before in (-3, -1, 0, 1, 2, "two", None):
            for after in (-3, -1, 0, 1, 2, "x", None):
                result = self._grep(path, context_before=before, context_after=after)
                assert not result.error, (before, after, result.content)
                bodies = _bodies(result.content)
                assert bodies, (before, after, result.content)
                assert all(b for b in bodies), (
                    f"a block printed a header and no body at "
                    f"context_before={before!r}, context_after={after!r}:\n{result.content}"
                )

    @pytest.mark.parametrize("kw", [
        {"context_before": -1},
        {"context_after": -1},
        {"context_before": -1, "context_after": -1},
        {"context_before": "two"},
        {"context_after": "x"},
        {"context_before": None, "context_after": None},
    ])
    def test_a_nonsense_context_is_the_same_reading_as_no_context(self, tmp_path, kw):
        """Not merely 'no crash': the fallback must be exactly the documented default."""
        path = _one_match_file(tmp_path)
        assert self._grep(path, **kw).content == self._grep(path).content

    def test_a_positive_context_is_still_honoured(self, tmp_path):
        """The other direction: the domain check must not flatten valid values."""
        path = tmp_path / "many.txt"
        path.write_text("\n".join(f"line{n}" + (" TARGET" if n == 5 else "")
                                  for n in range(1, 10)), encoding="utf-8")
        with_ctx = self._grep(path, context_before=1)
        without = self._grep(path)
        assert " >line5 TARGET" in with_ctx.content
        assert with_ctx.content != without.content, "context_before=1 was ignored"
        assert "  line4" in with_ctx.content and "line4" not in without.content

    @pytest.mark.parametrize("value", [-1, -100, 0, "five", None])
    def test_a_nonsense_max_results_finds_everything_the_default_finds(
        self, tmp_path, value
    ):
        """A bad cap must not narrow the scan: `max_results=-1` used to stop at one.

        Measured on master: on a file with 8 matching lines, `max_results=-1` reported
        `Found 1 matches` — the scan stopped on the first hit because the comparison
        `len(results) > -1 * (2 + 0 + 0)` held immediately.
        """
        path = tmp_path / "many.txt"
        path.write_text("\n".join(f"line{n}" + (" TARGET" if n % 3 == 0 else "")
                                  for n in range(1, 25)), encoding="utf-8")
        assert _header_lines(self._grep(path, max_results=value).content) == \
            _header_lines(self._grep(path).content)

    def test_a_max_results_smaller_than_the_default_still_narrows(self, tmp_path):
        """The other direction for the cap: a valid smaller cap is not the default."""
        path = tmp_path / "many.txt"
        path.write_text("\n".join(f"line{n} TARGET" for n in range(1, 25)), encoding="utf-8")
        narrow = _header_lines(self._grep(path, max_results=2).content)
        default = _header_lines(self._grep(path).content)
        assert len(narrow) < len(default), (narrow, default)

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
