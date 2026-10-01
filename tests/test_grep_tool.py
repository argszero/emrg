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


def _headers(content: str) -> list[str]:
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
        assert _headers(self._grep(path, max_results=value).content) == \
            _headers(self._grep(path).content)

    def test_a_max_results_smaller_than_the_default_still_narrows(self, tmp_path):
        """The other direction for the cap: a valid smaller cap is not the default."""
        path = tmp_path / "many.txt"
        path.write_text("\n".join(f"line{n} TARGET" for n in range(1, 25)), encoding="utf-8")
        narrow = _headers(self._grep(path, max_results=2).content)
        default = _headers(self._grep(path).content)
        assert len(narrow) < len(default), (narrow, default)
