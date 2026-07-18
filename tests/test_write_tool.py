"""Tests for the write tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from emrg.tools.write_tool import WriteTool


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _run(coro):
    return asyncio.run(coro)


def test_write_creates_file(temp_dir):
    tool = WriteTool()
    filepath = temp_dir / "new_file.txt"
    result = _run(tool.execute({
        "file_path": str(filepath),
        "content": "hello world\n",
    }))
    assert not result.error
    assert "Created" in result.content
    assert filepath.read_text() == "hello world\n"


def test_write_overwrites_file(temp_dir):
    tool = WriteTool()
    filepath = temp_dir / "existing.txt"
    filepath.write_text("old content")

    result = _run(tool.execute({
        "file_path": str(filepath),
        "content": "new content",
    }))
    assert not result.error
    assert "Updated" in result.content
    assert filepath.read_text() == "new content"


def test_write_creates_parent_directories(temp_dir):
    tool = WriteTool()
    filepath = temp_dir / "deep" / "nested" / "file.txt"
    result = _run(tool.execute({
        "file_path": str(filepath),
        "content": "deep content",
    }))
    assert not result.error
    assert "Created" in result.content
    assert filepath.read_text() == "deep content"


def test_write_missing_file_path():
    tool = WriteTool()
    result = _run(tool.execute({
        "content": "bar",
    }))
    # Note: error=True will be added in PR #9
    assert "no file_path" in result.content


def test_write_empty_content(temp_dir):
    tool = WriteTool()
    filepath = temp_dir / "empty.txt"
    result = _run(tool.execute({
        "file_path": str(filepath),
        "content": "",
    }))
    assert not result.error
    assert filepath.exists()
    assert filepath.read_text() == ""


def test_write_large_content_ok(temp_dir):
    tool = WriteTool()
    filepath = temp_dir / "large.txt"
    content = "x" * 100_000  # well under 10MB limit
    result = _run(tool.execute({
        "file_path": str(filepath),
        "content": content,
    }))
    assert not result.error
    assert "Created" in result.content
    assert len(filepath.read_text()) == 100_000
