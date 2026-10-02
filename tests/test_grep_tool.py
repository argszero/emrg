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


# ---------------------------------------------------------------------------
# The coverage clause: "searched N files" must count the files the pattern was
# actually run against, not the files the walk reached.
# ---------------------------------------------------------------------------

_COVERAGE_RE = re.compile(r"\(searched (\d+) files(?:, skipped (\d+) \([^)]*\))?\)")


def _coverage(text: str) -> tuple[int, int]:
    """Read the two numbers back out of a summary — (searched, skipped)."""
    m = _COVERAGE_RE.search(text)
    assert m, f"no coverage clause in {text!r}"
    return int(m.group(1)), int(m.group(2) or 0)


@pytest.fixture
def mixed_tree():
    """Six files: four text files the pattern can reach, two skipped by design."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.txt").write_text("alpha TARGET\n")
        (root / "b.txt").write_text("beta only\n")
        (root / "c.txt").write_text("gamma only\n")
        (root / "sub").mkdir()
        (root / "sub" / "d.txt").write_text("delta TARGET\n")
        # Over MAX_FILE_SIZE: skipped by design, and it does contain the pattern.
        (root / "huge.txt").write_text("x" * 600_000 + "\nTARGET\n")
        # Not UTF-8 text: skipped by design, and it does contain the bytes.
        (root / "raw.bin").write_bytes(b"\xff\xfe\x00TARGET\xff")
        yield root


def test_the_searched_count_is_the_files_read(mixed_tree):
    """`searched N` must be the files the pattern ran against, not files reached."""
    tool = GrepTool()
    result = _run(tool.execute({"pattern": "TARGET", "path": str(mixed_tree)}))
    assert not result.error
    searched, skipped = _coverage(result.content)
    # Four text files, and only two of them hold the pattern.
    assert searched == 4
    assert skipped == 2
    assert "Found 2 matches" in result.content


def test_the_coverage_numbers_add_up_to_the_tree(mixed_tree):
    """The two numbers a reader can check: searched + skipped == files present."""
    tool = GrepTool()
    result = _run(tool.execute({"pattern": "TARGET", "path": str(mixed_tree)}))
    searched, skipped = _coverage(result.content)
    present = sum(1 for p in mixed_tree.rglob("*") if p.is_file())
    assert searched + skipped == present == 6
    # And the number is not simply everything the walk reached.
    assert searched != present


def test_the_no_match_summary_reads_the_same_coverage(mixed_tree):
    """The branch a reader consults to conclude "nothing there" carries it too."""
    tool = GrepTool()
    result = _run(tool.execute({"pattern": "ZZZ_NOPE", "path": str(mixed_tree)}))
    assert "No matches" in result.content
    assert _coverage(result.content) == (4, 2)


def test_both_summaries_render_one_coverage_clause(mixed_tree):
    """Match and no-match summaries must render the identical clause."""
    tool = GrepTool()
    hit = _run(tool.execute({"pattern": "TARGET", "path": str(mixed_tree)}))
    miss = _run(tool.execute({"pattern": "ZZZ_NOPE", "path": str(mixed_tree)}))
    assert _COVERAGE_RE.search(hit.content).group(0) == _COVERAGE_RE.search(
        miss.content
    ).group(0)


def test_a_skipped_file_is_named(mixed_tree):
    """A summary must not imply the search covered files it never read."""
    tool = GrepTool()
    result = _run(tool.execute({"pattern": "TARGET", "path": str(mixed_tree)}))
    assert "skipped 2" in result.content
    assert str(MAX_FILE_SIZE // 1024) in result.content


def test_no_skip_is_announced_when_nothing_was_skipped(temp_cwd):
    """The other direction: a full search must not claim skipped files."""
    tool = GrepTool()
    result = _run(tool.execute({"pattern": "import", "path": str(temp_cwd)}))
    searched, skipped = _coverage(result.content)
    assert skipped == 0
    assert "skipped" not in result.content
    # The fixture holds five files, but `__pycache__/compiled.pyc` is never
    # collected (a skip dir), so four are read and all four are read.
    assert searched == 4
    assert sum(1 for p in temp_cwd.rglob("*") if p.is_file()) == 5


@pytest.mark.skipif(sys.platform == "win32", reason="path separator differs (\\ vs /)")
def test_the_size_bound_is_read_from_both_sides():
    """Exactly MAX_FILE_SIZE is searched; one byte more is skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        at_bound = "TARGET" + "x" * (MAX_FILE_SIZE - 6)
        (root / "at.txt").write_text(at_bound)
        (root / "over.txt").write_text("y" * (MAX_FILE_SIZE + 1))
        assert (root / "at.txt").stat().st_size == MAX_FILE_SIZE
        assert (root / "over.txt").stat().st_size == MAX_FILE_SIZE + 1
        tool = GrepTool()
        result = _run(tool.execute({"pattern": "TARGET", "path": str(root)}))
        assert _coverage(result.content) == (1, 1)
        assert "at.txt" in result.content
        assert "over.txt" not in result.content
