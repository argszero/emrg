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
    assert result.error
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


def test_write_content_too_large(temp_dir, monkeypatch):
    """Content exceeding MAX_WRITE_SIZE must return an error."""
    monkeypatch.setattr("emrg.tools.write_tool.MAX_WRITE_SIZE", 100)
    tool = WriteTool()
    filepath = temp_dir / "test.txt"
    result = _run(tool.execute({
        "file_path": str(filepath),
        "content": "x" * 200,
    }))
    assert result.error
    assert "too large" in result.content
    assert not filepath.exists()


# ── read-only sandbox (community issue #979) ──────────────────────────────


def test_write_read_only_blocks_inside_workspace(temp_dir):
    """Issue #979 follow-up: the write tool must not clobber the host's tree
    when the dirty-tree guard forced read-only — writes inside the workspace
    (task source dir) are blocked."""
    tool = WriteTool()
    workspace = temp_dir / "ws"
    workspace.mkdir()
    target = workspace / "host-file.txt"
    result = _run(tool.execute({
        "file_path": str(target),
        "content": "should not be written",
        "sandbox": "read-only",
        "workspace": str(workspace),
    }))
    assert result.error
    assert "read-only sandbox" in result.content
    assert not target.exists()


def test_write_read_only_allows_outside_workspace(temp_dir):
    """Writes outside the workspace (memory dir, logs) stay allowed — a
    read-only cycle still records state and writes its own artifacts."""
    tool = WriteTool()
    workspace = temp_dir / "ws"
    workspace.mkdir()
    target = temp_dir / "outside" / "record.md"
    result = _run(tool.execute({
        "file_path": str(target),
        "content": "# record",
        "sandbox": "read-only",
        "workspace": str(workspace),
    }))
    assert not result.error
    assert target.exists()


def test_write_no_sandbox_unchanged(temp_dir):
    """Normal use (no sandbox) keeps writing anywhere — no behavior change."""
    tool = WriteTool()
    target = temp_dir / "plain.txt"
    result = _run(tool.execute({
        "file_path": str(target),
        "content": "hi",
    }))
    assert not result.error
    assert target.read_text() == "hi"
