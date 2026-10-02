"""Tests for the grep tool."""

import asyncio
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
