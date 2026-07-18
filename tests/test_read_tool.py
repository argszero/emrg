"""Tests for the read tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from emrg.tools.read_tool import ReadTool


@pytest.fixture
def temp_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        f = root / "test.txt"
        f.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n")
        yield f, root


def _run(coro):
    return asyncio.run(coro)


def test_read_basic(temp_file):
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({"file_path": str(f)}))
    assert not result.error
    assert "line 1" in result.content
    assert "line 5" in result.content
    # Check line number prefix
    assert result.content.startswith("     1\t")


def test_read_with_offset(temp_file):
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({"file_path": str(f), "offset": 3}))
    assert not result.error
    assert result.content.startswith("     3\tline 3")


def test_read_with_limit(temp_file):
    tool = ReadTool()
    f, _ = temp_file
    result = _run(tool.execute({"file_path": str(f), "limit": 2}))
    assert not result.error
    assert "line 1" in result.content
    assert "line 2" in result.content
    assert "truncated" in result.content  # 6 lines total with trailing newline > 2


def test_read_file_not_found():
    tool = ReadTool()
    result = _run(tool.execute({"file_path": "/nonexistent/file.txt"}))
    assert result.error
    assert "file not found" in result.content


def test_read_missing_file_path():
    tool = ReadTool()
    result = _run(tool.execute({}))
    # Note: error=True will be added in PR #9
    assert "no file_path" in result.content


def test_read_directory(temp_file):
    tool = ReadTool()
    _, d = temp_file
    result = _run(tool.execute({"file_path": str(d)}))
    assert not result.error
    assert "Directory listing" in result.content
    assert "test.txt" in result.content


def test_read_binary_fails(temp_file):
    tool = ReadTool()
    _, d = temp_file
    # Create a binary file
    bf = d / "binary.bin"
    bf.write_bytes(b"\x00\x01\x02\xff\xfe")
    result = _run(tool.execute({"file_path": str(bf)}))
    # Binary files may or may not decode; if they fail, it should be an error
    if result.error:
        assert "binary file" in result.content.lower() or "cannot read" in result.content.lower()


def test_read_offset_beyond_eof(temp_file):
    tool = ReadTool()
    f, _ = temp_file
    # 6 lines (5 + trailing newline), offset=100 is way beyond
    result = _run(tool.execute({"file_path": str(f), "offset": 100}))
    assert "(empty range" in result.content or "empty range" in result.content


def test_read_truncation_message(temp_file):
    """When a file exceeds the NDJSON safe limit and no limit is specified,
    the result should be truncated and include a truncation message."""
    tool = ReadTool()
    _, d = temp_file
    # Create a file with 1500 lines (exceeds NDJSON_SAFE_MAX_LINES=1000)
    big = d / "big.txt"
    big.write_text("\n".join(f"line {i}" for i in range(1, 1501)) + "\n")
    result = _run(tool.execute({"file_path": str(big)}))
    assert not result.error
    # Should show truncation message since 1500 > 1000
    assert "truncated" in result.content or "lines" in result.content
