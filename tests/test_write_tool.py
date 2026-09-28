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


def test_write_read_only_refuses_outside_the_workspace_too(temp_dir):
    """``read-only`` grants nothing, outside the workspace exactly as inside it.

    This row used to assert the opposite ("writes outside the workspace stay
    allowed — a read-only cycle still records state"), which was the file tools'
    own answer and the divergence from the kernel fence: a `read-only` bash
    command could write nowhere while this tool could write anywhere outside the
    workspace. Host ruling 2026-09-28T21:50 (issue #1553) chose one policy for
    both families, so the row is inverted here rather than deleted — the tier
    now means one thing, and the consequence (a read-only cycle writes no record)
    is written down in `emrg/sandbox/fence.py`.
    """
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
    assert result.error, result.content
    assert "read-only sandbox" in result.content
    assert not target.exists(), "the tool refused after writing"


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
    """Rant 2026-09-01T15:10:23: a workspace-write session must not write to an
    absolute path outside the session cwd — the same block the shell tool
    applies, and now the same *policy*: the boundary is
    ``emrg/sandbox/roots.writable_roots``, read through the fence.

    Both ambient temp sources are withheld, and the second one is the whole
    point: ``gettempdir()`` and the host ``/tmp`` are separate grants, pytest's
    temp base *is* ``/tmp/...`` on a Linux runner, and with only the probe
    patched this test wrote the sibling it calls "outside" — measured red on the
    ubuntu leg (CI run 36432808100) and reproduced here by putting the temp base
    under ``/tmp``. One reader pinned while another stays ambient is the defect
    class ``tests/test_windows_path_tokens.py`` recorded; the workspace root is
    still granted, so the target is outside the session cwd and outside every
    temp root on every platform.
    """
    import tempfile as _tf

    from emrg.sandbox import roots as sandbox_roots

    # The test workspace + target live in the OS temp dir, which is normally
    # allowed — withhold that grant so the target is judged on the boundary
    # alone.
    monkeypatch.setattr(_tf, "gettempdir", lambda: "/fake-os-temp")
    monkeypatch.setattr(sandbox_roots, "_host_temp_spellings", lambda: [])
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


def test_write_workspace_write_refuses_the_retired_deployer_root(tmp_path, monkeypatch):
    """Issue #1093's extra root is retired, so this target is now refused (D5, #1703).

    The file tools used to grant ``~/.emrg/evolution/.emrg/`` aside from the
    workspace, because the evolution module writes its cycle records there and
    that tree sits outside every checkout (the positive state issue #1093
    recorded). Host decision D5 deleted that root from the derivation, issue
    #1703 archived it, and the fence asks the derivation and nothing else — so
    the same target is refused. That is this test's discriminating half: a fence
    that still carried the exception would answer ``allow`` here.

    ``~`` is pinned to scratch and every ambient temp source is withheld, so the
    target is provably outside all of them on every platform and nothing of the
    host's is reachable. The previous version of this test asserted the retired
    behaviour; it passed on ubuntu only because the pinned home happened to sit
    under ``/tmp``, and on Windows ``expanduser`` reads ``USERPROFILE``, which it
    did not pin (CI run 36432808100: ``assert not True``).
    """
    import os
    import tempfile as _tf

    from emrg.sandbox import roots as sandbox_roots

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    # expanduser reads USERPROFILE first on Windows, so pin the one that decides.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(_tf, "gettempdir", lambda: "/fake-os-temp")
    monkeypatch.setattr(sandbox_roots, "_host_temp_spellings", lambda: [])
    evo_data = Path(os.path.realpath(os.path.expanduser("~/.emrg/evolution/.emrg")))
    ws = tmp_path / "ws"
    ws.mkdir()
    target = evo_data / "memory" / "cycle-20260901-000000.md"
    tool = WriteTool()
    result = _run(tool.execute({
        "file_path": str(target),
        "content": "cycle record",
        "sandbox": "workspace-write",
        "workspace": str(ws),
    }))
    assert result.error
    assert "workspace-write sandbox" in result.content
    assert not target.exists()


def test_write_workspace_write_blocks_a_protected_daemon_file(tmp_path, monkeypatch):
    """The protected-file branch reaches THIS tool's error path — not only the
    predicate (rant 2026-09-17T11:38:16).

    The deleted variant of this test targeted the host's real ``~/.emrg/config.toml``,
    so its safety rested on the guard it was testing: the mutation arm that breaks
    that guard overwrote the host's file. Here ``~`` is pinned to scratch, so the
    target is built by the test and the same arm can only reach this test's own
    sentinel. Note the boundary does NOT block it — the OS temp root is a trusted
    write zone, measured 2026-09-17 — so a red run here means the PROTECTED branch
    let the write through.
    """
    home = tmp_path / "home"
    home.mkdir()
    # expanduser("~") reads USERPROFILE on Windows, HOME elsewhere.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    target = home / ".emrg" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_text("sentinel = true\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = WriteTool()
    result = _run(tool.execute({
        "file_path": str(target),
        "content": "tamper",
        "sandbox": "workspace-write",
        "workspace": str(workspace),
    }))
    assert result.error
    assert "protected daemon file" in result.content
    assert target.read_text() == "sentinel = true\n"
