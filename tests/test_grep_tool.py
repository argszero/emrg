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


class TestTheCountIsTheMatchesNotTheRenderedLines:
    """Issue #1805: asking for context used to *create* matches.

    The count was re-derived from the rendered output —
    `sum(1 for r in results if r.endswith(":"))` — where a header is `rel:line:` but a
    **context** line is emitted with its own text intact. Any context line whose text ends
    in a colon (a YAML block key, a C++/Pascal label, a Markdown `Term:`) therefore counted
    as a match, so the reported number rose with `context_before`/`context_after` alone.
    Measured 2026-10-02 on this host, on a file holding `first:` / `second:` / `TARGET line`
    / `third:` / `fourth:`:

        no context                          -> Found 1 matches
        context_before=1, context_after=2   -> Found 4 matches

    `grep` is the tool an agent reaches for to answer "how many places does this happen?",
    so the number is reasoned from — and the inflation is systematic, because context
    windows are exactly where labelled lines live.
    """

    #: One matching line, with labelled lines before and after it.
    LABELLED = "first:\nsecond:\nTARGET line\nthird:\nfourth:\n"

    def _file(self, tmp_path: Path) -> Path:
        f = tmp_path / "labelled.txt"
        f.write_text(self.LABELLED, encoding="utf-8")
        return f

    def _count(self, content: str) -> int:
        """The number the summary claims, read the way a caller reads it."""
        m = re.search(r"Found (?:at least )?(\d+) matches", content)
        assert m, f"no count in:\n{content}"
        return int(m.group(1))

    def test_context_lines_ending_in_a_colon_are_not_counted_as_matches(
        self, tmp_path
    ) -> None:
        """The reproduction itself: three of the four 'matches' were the context."""
        f = self._file(tmp_path)
        plain = _run(GrepTool().execute({"pattern": "TARGET", "path": str(f)}))
        with_ctx = _run(GrepTool().execute({
            "pattern": "TARGET", "path": str(f),
            "context_before": 1, "context_after": 2,
        }))
        assert self._count(plain.content) == 1, plain.content
        assert self._count(with_ctx.content) == 1, (
            "asking for context changed the number of matches, which is the defect:\n"
            + with_ctx.content
        )

    @pytest.mark.parametrize("before,after", [(0, 0), (1, 0), (0, 1), (2, 3)])
    def test_the_count_does_not_move_with_the_context_window(
        self, tmp_path, before, after
    ) -> None:
        """The invariant the acceptance item asks for, at four window shapes.

        Stated as a property rather than only on the one reproduction, because a fix that
        special-cased that file would satisfy the test above and nothing else.
        """
        f = self._file(tmp_path)
        result = _run(GrepTool().execute({
            "pattern": "TARGET", "path": str(f),
            "context_before": before, "context_after": after,
        }))
        assert self._count(result.content) == 1, result.content

    def test_the_context_is_still_shown(self, tmp_path) -> None:
        """The other direction: the fix must not be bought by dropping the context.

        A tool that returned no context lines would report a correct count and break every
        caller that asked for context.
        """
        f = self._file(tmp_path)
        result = _run(GrepTool().execute({
            "pattern": "TARGET", "path": str(f),
            "context_before": 1, "context_after": 1,
        }))
        assert "second:" in result.content, result.content
        assert "third:" in result.content, result.content

    def test_the_block_layout_a_reader_already_sees_is_unchanged(self, tmp_path) -> None:
        """Acceptance item 3: header, `" >"` for the matching line, `"  "` for context."""
        f = self._file(tmp_path)
        result = _run(GrepTool().execute({
            "pattern": "TARGET", "path": str(f),
            "context_before": 1, "context_after": 1,
        }))
        lines = result.content.split("\n")
        header = [i for i, l in enumerate(lines) if l.startswith("labelled.txt:")]
        assert len(header) == 1, result.content
        block = lines[header[0] + 1 : header[0] + 4]
        assert block[0] == "  second:", block
        assert block[1] == " >TARGET line", block
        assert block[2] == "  third:", block
        assert block[1].startswith(" >") and block[0].startswith("  "), block


class TestTheCapIsOnMatchesAndTheNoteNamesMeasuredNumbers:
    """The second half of #1805: the truncation note described a cut that was not made.

    The old code cut at `max_results * 3` **lines** and said
    `[output truncated at ~{max_results} match blocks]` — two different units, so with
    context the note overstated how much was shown (`3 x max_results` lines is *fewer*
    than `max_results` blocks), and the cut could fall inside a block, leaving context
    lines whose header was gone: output that reads as belonging to no match.
    """

    def _many(self, tmp_path: Path, matches: int = 12) -> Path:
        body = []
        for n in range(1, matches + 1):
            body += [f"label{n}:", f"TARGET {n}", f"after{n}:"]
        f = tmp_path / "many.txt"
        f.write_text("\n".join(body) + "\n", encoding="utf-8")
        return f

    @staticmethod
    def _blocks(content: str) -> list[list[str]]:
        """The returned output split into blocks, by its own header rule."""
        blocks: list[list[str]] = []
        for line in content.split("\n"):
            if re.match(r"^[^ ].*:\d+:$", line):
                blocks.append([line])
            elif line.startswith("  ") or line.startswith(" >"):
                blocks[-1].append(line)
        return blocks

    def test_a_completed_search_reports_an_exact_count(self, tmp_path) -> None:
        """Both numbers are the same fact here, so the sentence is a claim, not a bound."""
        f = self._many(tmp_path)
        result = _run(GrepTool().execute({
            "pattern": "TARGET", "path": str(f), "max_results": 100,
            "context_before": 1, "context_after": 1,
        }))
        assert "Found 12 matches" in result.content, result.content
        assert "at least" not in result.content, result.content
        assert len(self._blocks(result.content)) == 12, result.content

    def test_a_capped_search_says_which_number_is_a_lower_bound(self, tmp_path) -> None:
        """Acceptance item 2: the note names two measured numbers, and the bound is named.

        A capped scan has read only part of the tree, so the total is *not known* — the
        sentence has to say that rather than print a number the scan never measured.
        """
        f = self._many(tmp_path)
        result = _run(GrepTool().execute({
            "pattern": "TARGET", "path": str(f), "max_results": 3,
            "context_before": 1, "context_after": 1,
        }))
        assert "Found at least 4 matches" in result.content, result.content
        assert "returning the first 3" in result.content, result.content
        assert "not known" in result.content, result.content

    def test_the_output_never_ends_inside_a_block(self, tmp_path) -> None:
        """Every context line keeps its header — the half-cut block the old note allowed.

        The old cut was mid-list: `results[: max_results * 3]` sliced *lines*, so a block
        could lose its header and leave context that reads as belonging to no match. The
        property is structural, and it is checked without reference to a block's *size*
        (which legitimately varies at a file's edges): every line after the first header is
        a header or a context line, no context line precedes a header, and each block still
        carries the matching line it belongs to.
        """
        header_re = re.compile(r"^[^ ].*:\d+:$")
        f = self._many(tmp_path)
        for cap, ctx in [(3, 1), (5, 1), (7, 2), (5, 0)]:
            result = _run(GrepTool().execute({
                "pattern": "TARGET", "path": str(f), "max_results": cap,
                "context_before": ctx, "context_after": ctx,
            }))
            lines = result.content.split("\n")
            starts = [i for i, l in enumerate(lines) if header_re.match(l)]
            assert starts, result.content
            # Nothing but a header may open the body: an orphaned context line would come
            # first, since it has no header above it to belong to.
            first_context = next(
                i for i, l in enumerate(lines) if l.startswith("  ") or l.startswith(" >")
            )
            assert starts[0] < first_context, (
                "the body opens with a context line, not a header — the orphaned shape "
                f"the old line-based cut could produce:\n{result.content}"
            )

            body = lines[starts[0]:]
            for line in body:
                assert (
                    header_re.match(line) or line.startswith("  ") or line.startswith(" >")
                ), f"a body line is neither a header nor a context line: {line!r}"

            blocks = self._blocks(result.content)
            assert len(blocks) == min(cap, 12), (
                f"cap={cap} ctx={ctx}: {len(blocks)} blocks returned\n{result.content}"
            )
            for block in blocks:
                assert sum(1 for l in block[1:] if l.startswith(" >")) == 1, (
                    f"a block does not carry exactly one matching line: {block}"
                )

    def test_the_number_of_blocks_returned_is_the_cap_not_a_line_budget(
        self, tmp_path
    ) -> None:
        """`max_results` is documented as "Maximum matches to return" — so it is matches.

        The old stop condition was a *line* budget (`max_results * (2 + ctx)`), which with
        context returned fewer blocks than the caller asked for.
        """
        f = self._many(tmp_path)
        for cap in (1, 3, 6):
            result = _run(GrepTool().execute({
                "pattern": "TARGET", "path": str(f), "max_results": cap,
                "context_before": 2, "context_after": 2,
            }))
            assert len(self._blocks(result.content)) == cap, (
                f"cap={cap} asked for {cap} matches, got "
                f"{len(self._blocks(result.content))}\n{result.content}"
            )

    def test_no_matches_still_says_so(self, tmp_path) -> None:
        """The control: the rewritten path must keep the empty case a plain absence."""
        f = self._many(tmp_path)
        result = _run(GrepTool().execute({"pattern": "NOTHING_HERE", "path": str(f)}))
        assert "No matches" in result.content, result.content
        assert "Found" not in result.content, result.content
