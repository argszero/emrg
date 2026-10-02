"""Tests for the glob tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from emrg.tools.glob_tool import GlobTool


@pytest.fixture
def temp_cwd():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("")
        (root / "src" / "utils.py").write_text("")
        (root / "tests").mkdir()
        (root / "tests" / "test_main.py").write_text("")
        (root / "__init__.py").write_text("")
        (root / "README.md").write_text("")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "compiled.pyc").write_text("")
        (root / ".hidden.py").write_text("")
        yield root


def _run(coro):
    return asyncio.run(coro)


def test_glob_all_python(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "**/*.py", "workdir": str(temp_cwd)}))
    assert not result.error
    # A hidden file must not be *listed as a match*. The assertion reads the listing
    # rather than the whole answer because the answer now also *names* what it skipped
    # — a skip that is not reported is a claim about the tree this search never made
    # (`tests/test_glob_names_what_it_skipped.py` carries that measurement).
    listed = [line.strip() for line in result.content.split("\n") if line.startswith("  ")]
    assert "src/main.py" in listed
    assert "src/utils.py" in listed
    assert "tests/test_main.py" in listed
    assert "__init__.py" in listed
    assert ".hidden.py" not in listed
    assert "compiled.pyc" not in listed


def test_glob_markdown(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "*.md", "workdir": str(temp_cwd)}))
    assert "README.md" in result.content
    assert "main.py" not in result.content


def test_glob_test_files(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "**/test*", "workdir": str(temp_cwd)}))
    assert "test_main.py" in result.content


def test_glob_no_match(temp_cwd):
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "*.rs", "workdir": str(temp_cwd)}))
    assert "No files matched" in result.content


def test_glob_no_pattern():
    tool = GlobTool()
    result = _run(tool.execute({"pattern": ""}))
    assert result.error
    assert "no pattern" in result.content.lower()


def test_definition():
    tool = GlobTool()
    d = tool.definition()
    assert d.name == "glob"
    assert "pattern" in d.parameters.get("properties", {})
    assert d.parameters.get("required") == ["pattern", "intent"]


def test_glob_invalid_workdir():
    """Non-existent workdir should return error."""
    tool = GlobTool()
    result = _run(tool.execute({"pattern": "**/*.py", "workdir": "/nonexistent"}))
    assert result.error
    assert "not found" in result.content.lower()
