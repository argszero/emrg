"""Tests for the edit tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from emrg.tools.edit_tool import EditTool


@pytest.fixture
def temp_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        f = root / "test.py"
        f.write_text("hello world\nfoo bar\nhello world\n")
        yield f


def _run(coro):
    return asyncio.run(coro)


def test_edit_basic_replacement(temp_file):
    tool = EditTool()
    result = _run(tool.execute({
        "file_path": str(temp_file),
        "old_string": "foo bar",
        "new_string": "baz qux",
    }))
    assert not result.error
    assert "Made 1 replacement" in result.content
    assert temp_file.read_text() == "hello world\nbaz qux\nhello world\n"


def test_edit_replace_all(temp_file):
    tool = EditTool()
    result = _run(tool.execute({
        "file_path": str(temp_file),
        "old_string": "hello world",
        "new_string": "hi",
        "replace_all": True,
    }))
    assert not result.error
    assert "Made 2 replacements" in result.content
    assert temp_file.read_text() == "hi\nfoo bar\nhi\n"


def test_edit_not_found(temp_file):
    tool = EditTool()
    result = _run(tool.execute({
        "file_path": str(temp_file),
        "old_string": "nonexistent",
        "new_string": "something",
    }))
    assert result.error
    assert "old_string not found" in result.content


def test_edit_multiple_without_replace_all(temp_file):
    tool = EditTool()
    result = _run(tool.execute({
        "file_path": str(temp_file),
        "old_string": "hello world",
        "new_string": "hi",
    }))
    assert result.error
    assert "found 2 times" in result.content


def test_edit_file_not_found():
    tool = EditTool()
    result = _run(tool.execute({
        "file_path": "/nonexistent/file.txt",
        "old_string": "foo",
        "new_string": "bar",
    }))
    assert result.error
    assert "file not found" in result.content


def test_edit_empty_old_string(temp_file):
    tool = EditTool()
    result = _run(tool.execute({
        "file_path": str(temp_file),
        "old_string": "",
        "new_string": "bar",
    }))
    assert result.error
    assert "old_string is empty" in result.content


def test_edit_missing_file_path():
    tool = EditTool()
    result = _run(tool.execute({
        "old_string": "foo",
        "new_string": "bar",
    }))
    assert result.error
    assert "no file_path" in result.content


def test_edit_is_directory(temp_file):
    tool = EditTool()
    result = _run(tool.execute({
        "file_path": str(temp_file.parent),
        "old_string": "foo",
        "new_string": "bar",
    }))
    assert result.error
    assert "is a directory" in result.content
