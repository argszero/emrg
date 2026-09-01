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


# ── workspace-write sandbox (rant 2026-09-01T15:10:23) ────────────────────


def test_write_workspace_write_blocks_outside_workspace(temp_dir, monkeypatch):
    """Rant 2026-09-01T15:10:23: a workspace-write session must not write to
    an absolute path outside the session cwd — mirror the bash tool's block
    so write/edit are symmetric with bash (the asymmetric hole)."""
    import tempfile as _tf

    # The test workspace + target live in the OS temp dir, which is always
    # allowed — patch gettempdir to a distinct sentinel so the target is not
    # treated as an OS-temp write root.
    monkeypatch.setattr(_tf, "gettempdir", lambda: "/fake-os-temp")
    tool = WriteTool()
    workspace = temp_dir / "ws"
    workspace.mkdir()
    target = temp_dir / "sibling" / "poem.txt"  # outside workspace
    result = _run(tool.execute({
        "file_path": str(target),
        "content": "should be blocked",
        "sandbox": "workspace-write",
        "workspace": str(workspace),
    }))
    assert result.error
    assert "workspace-write sandbox" in result.content
    assert not target.exists()


def test_write_workspace_write_allows_inside_workspace(temp_dir):
    """A workspace-write session may still write inside the session cwd."""
    tool = WriteTool()
    workspace = temp_dir / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    result = _run(tool.execute({
        "file_path": str(target),
        "content": "ok",
        "sandbox": "workspace-write",
        "workspace": str(workspace),
    }))
    assert not result.error
    assert target.exists()
    assert target.read_text() == "ok"


def test_write_workspace_write_allows_os_temp(temp_dir):
    """OS temp is an allowed write root (mirrors dsh's workspace + temp area)."""
    import tempfile
    tool = WriteTool()
    workspace = temp_dir / "ws"
    workspace.mkdir()
    target = Path(tempfile.gettempdir()) / "emrg_sandbox_test.txt"
    result = _run(tool.execute({
        "file_path": str(target),
        "content": "temp",
        "sandbox": "workspace-write",
        "workspace": str(workspace),
    }))
    assert not result.error
    target.unlink(missing_ok=True)
    assert not target.exists()


def test_write_workspace_write_blocks_protected_config(temp_dir):
    """Daemon state files (~/.emrg/config.toml) are always blocked from a
    workspace-write session."""
    tool = WriteTool()
    workspace = temp_dir / "ws"
    workspace.mkdir()
    result = _run(tool.execute({
        "file_path": "~/.emrg/config.toml",
        "content": "tamper",
        "sandbox": "workspace-write",
        "workspace": str(workspace),
    }))
    assert result.error
    assert "protected daemon file" in result.content


def test_write_workspace_write_allows_evolution_memory(tmp_path, monkeypatch):
    """Issue #1093 self-regression: the evolution module writes its cycle records
    to ~/.emrg/evolution/.emrg/memory/, which is OUTSIDE the repo checkout
    workspace. The workspace-write boundary must trust that data root so the
    evolution module can still record its own history (positive state)."""
    import os
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    evo_data = Path(os.path.realpath(os.path.expanduser("~/.emrg/evolution/.emrg")))
    evo_data.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    target = evo_data / "memory" / "cycle-20260901-000000.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    tool = WriteTool()
    result = _run(tool.execute({
        "file_path": str(target),
        "content": "cycle record",
        "sandbox": "workspace-write",
        "workspace": str(ws),
    }))
    assert not result.error
    assert target.exists()
    assert target.read_text() == "cycle record"
