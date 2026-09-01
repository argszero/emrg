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


# ── read-only sandbox (community issue #979) ──────────────────────────────


def test_edit_read_only_blocks_inside_workspace(temp_file):
    """Issue #979 follow-up: the edit tool must not modify the host's tree
    when the dirty-tree guard forced read-only."""
    tool = EditTool()
    workspace = temp_file.parent
    original = temp_file.read_text()
    result = _run(tool.execute({
        "file_path": str(temp_file),
        "old_string": "hello world",
        "new_string": "changed",
        "sandbox": "read-only",
        "workspace": str(workspace),
    }))
    assert result.error
    assert "read-only sandbox" in result.content
    assert temp_file.read_text() == original  # untouched


def test_edit_read_only_allows_outside_workspace(temp_file):
    """Writes outside the workspace (memory dir, logs) stay allowed — a
    read-only cycle still records state and writes its own artifacts."""
    import tempfile as _tf

    with _tf.TemporaryDirectory() as d:
        target = Path(d) / "doc.md"
        target.write_text("keep this line\n", encoding="utf-8")
        tool = EditTool()
        result = _run(tool.execute({
            "file_path": str(target),
            "old_string": "keep",
            "new_string": "edited",
            "sandbox": "read-only",
            "workspace": str(temp_file.parent),  # different boundary
        }))
        assert not result.error
        assert "edited" in target.read_text()


def test_edit_no_sandbox_unchanged(temp_file):
    """Normal use (no sandbox) keeps editing — no behavior change."""
    tool = EditTool()
    result = _run(tool.execute({
        "file_path": str(temp_file),
        "old_string": "foo bar",
        "new_string": "baz qux",
    }))
    assert not result.error
    assert "baz qux" in temp_file.read_text()


# ── workspace-write sandbox (rant 2026-09-01T15:10:23) ────────────────────


def test_edit_workspace_write_blocks_outside_workspace(temp_file, monkeypatch):
    """Rant 2026-09-01T15:10:23: a workspace-write session must not edit an
    absolute path outside the session cwd — mirror the bash tool's block so
    write/edit are symmetric with bash (the asymmetric hole)."""
    import tempfile as _tf

    # Build the sibling scratch dir first (TemporaryDirectory needs the real
    # OS temp), then patch gettempdir so the tool's check doesn't treat the
    # sibling as an OS-temp write root.
    with _tf.TemporaryDirectory() as d:
        sibling = Path(d) / "outside.txt"
        sibling.write_text("dangerous line\n", encoding="utf-8")
        monkeypatch.setattr(_tf, "gettempdir", lambda: "/fake-os-temp")
        tool = EditTool()
        result = _run(tool.execute({
            "file_path": str(sibling),
            "old_string": "dangerous",
            "new_string": "tampered",
            "sandbox": "workspace-write",
            "workspace": str(temp_file.parent),  # different boundary (session cwd)
        }))
        assert result.error
        assert "workspace-write sandbox" in result.content
        assert sibling.read_text() == "dangerous line\n"  # untouched


def test_edit_workspace_write_allows_inside_workspace(temp_file):
    """A workspace-write session may still edit inside the session cwd."""
    tool = EditTool()
    result = _run(tool.execute({
        "file_path": str(temp_file),
        "old_string": "foo bar",
        "new_string": "baz qux",
        "sandbox": "workspace-write",
        "workspace": str(temp_file.parent),
    }))
    assert not result.error
    assert "baz qux" in temp_file.read_text()


def test_edit_workspace_write_blocks_protected_config(temp_file):
    """Daemon state files are always blocked from a workspace-write session."""
    tool = EditTool()
    result = _run(tool.execute({
        "file_path": "~/.emrg/config.toml",
        "old_string": "foo",
        "new_string": "bar",
        "sandbox": "workspace-write",
        "workspace": str(temp_file.parent),
    }))
    assert result.error
    assert "protected daemon file" in result.content
