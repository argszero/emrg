"""Tests for the edit tool."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from emrg.tools.edit_tool import EditTool
from emrg.tools.read_tool import ReadTool


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


def test_edit_read_only_refuses_outside_the_workspace_too(temp_file):
    """``read-only`` grants nothing — the edit twin of the write tool's row.

    Inverted with the host ruling of 2026-09-28T21:50 (issue #1553): the file
    tools share the kernel fence's policy, so a tier that lets a spawned command
    write nowhere lets these tools write nowhere either.
    """
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
        assert result.error, result.content
        assert "read-only sandbox" in result.content
        assert "keep this line" in target.read_text(), "the tool edited after refusing"


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
    absolute path outside the session cwd — the same block the shell tool
    applies, and now the same *policy*: the boundary is
    ``emrg/sandbox/roots.writable_roots``, read through the fence.

    Both ambient temp sources are withheld, not just the ``gettempdir()`` probe:
    the host ``/tmp`` is a grant of its own and pytest's temp base is ``/tmp/...``
    on a Linux runner, which is how this test's sibling stopped being "outside"
    there (CI run 36432808100, red on ubuntu and green locally). See the write
    tool's twin for the measurement; the two are one rule with two callers.
    """
    import tempfile as _tf

    from emrg.sandbox import roots as sandbox_roots

    # Build the sibling scratch dir first (TemporaryDirectory needs the real
    # OS temp), then withhold the temp grants so the sibling is judged on the
    # workspace boundary alone.
    with _tf.TemporaryDirectory() as d:
        sibling = Path(d) / "outside.txt"
        sibling.write_text("dangerous line\n", encoding="utf-8")
        monkeypatch.setattr(_tf, "gettempdir", lambda: "/fake-os-temp")
        monkeypatch.setattr(sandbox_roots, "_host_temp_spellings", lambda: [])
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


def test_edit_workspace_write_blocks_a_protected_daemon_file(tmp_path, monkeypatch):
    """The protected-file branch reaches THIS tool's error path — not only the
    predicate (rant 2026-09-17T11:38:16).

    The deleted variant targeted the host's real ``~/.emrg/config.toml``, so its
    safety rested on the guard it was testing: the mutation arm that breaks that
    guard rewrote the host's file. Here ``~`` is pinned to scratch, so the target
    is built by the test and the same arm can only reach this test's own sentinel.
    The boundary does not block it (the OS temp root is a trusted write zone,
    measured 2026-09-17), so a red run means the PROTECTED branch let it through.
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
    tool = EditTool()
    result = _run(tool.execute({
        "file_path": str(target),
        "old_string": "sentinel",
        "new_string": "tamper",
        "sandbox": "workspace-write",
        "workspace": str(workspace),
    }))
    assert result.error
    assert "protected daemon file" in result.content
    assert target.read_text() == "sentinel = true\n"


class TestTheFileKeepsItsOwnLineEndings:
    """An edit changes what ``old_string`` named and nothing else — issue #1803.

    The read tool shows a file with newline translation, so the caller's
    ``old_string`` carries ``\\n`` whatever the file holds on disk. Reading with the
    default and writing it back applied the same translation in reverse: a CRLF file
    came back LF-only, i.e. *every* line of it changed. Measured on master `6b417c4`,
    2026-10-02, with these two tools: ``b'first\\r\\nsecond\\r\\nthird\\r\\n'`` in,
    ``b'first\\nSECOND\\nthird\\n'`` out.

    What that costs is not hypothetical: `.gitattributes` pins ``*.cmd``/``*.bat``/
    ``*.ps1`` to CRLF on every platform, and an LF-only ``.cmd`` is the
    v0.2.25–v0.2.27 installer failure `tests/test_cmd_crlf.py` exists for.
    """

    def _crlf(self, tmp_path: Path, name: str = "install.cmd") -> Path:
        f = tmp_path / name
        f.write_bytes(b"first\r\nsecond\r\nthird\r\n")
        return f

    def test_a_crlf_file_stays_crlf(self, tmp_path):
        f = self._crlf(tmp_path)
        result = _run(EditTool().execute({
            "file_path": str(f),
            "old_string": "second",
            "new_string": "SECOND",
        }))
        assert not result.error
        assert f.read_bytes() == b"first\r\nSECOND\r\nthird\r\n", (
            "every line's terminator has to survive an edit that named one line"
        )

    def test_the_edit_still_matches_what_the_read_tool_showed(self, tmp_path):
        """The caller's view is the read tool's, so its old_string carries LF."""
        f = self._crlf(tmp_path)
        shown = _run(ReadTool().execute({"file_path": str(f), "intent": "read it"}))

        result = _run(EditTool().execute({
            "file_path": str(f),
            "old_string": "first\nsecond",  # exactly the two lines as shown
            "new_string": "first\nSECOND",
        }))

        assert not result.error
        assert "second" in shown.content and "\n" in shown.content
        assert f.read_bytes() == b"first\r\nSECOND\r\nthird\r\n"

    def test_replace_all_keeps_the_endings_too(self, tmp_path):
        f = self._crlf(tmp_path)
        result = _run(EditTool().execute({
            "file_path": str(f),
            "old_string": "r",
            "new_string": "R",
            "replace_all": True,
        }))
        assert not result.error
        assert f.read_bytes() == b"fiRst\r\nsecond\r\nthiRd\r\n"

    def test_an_lf_file_is_byte_identical_to_before(self, tmp_path):
        f = tmp_path / "notes.md"
        f.write_bytes(b"first\nsecond\nthird\n")
        result = _run(EditTool().execute({
            "file_path": str(f),
            "old_string": "second",
            "new_string": "SECOND",
        }))
        assert not result.error
        assert f.read_bytes() == b"first\nSECOND\nthird\n"

    def test_a_file_with_no_newline_at_all_stays_one_line(self, tmp_path):
        f = tmp_path / "one.txt"
        f.write_bytes(b"only line")
        result = _run(EditTool().execute({
            "file_path": str(f),
            "old_string": "line",
            "new_string": "LINE",
        }))
        assert not result.error
        assert f.read_bytes() == b"only LINE"

    def test_a_multi_line_insertion_takes_the_files_endings(self, tmp_path):
        """A replacement that adds lines must not leave the file half-CRLF."""
        f = self._crlf(tmp_path)
        result = _run(EditTool().execute({
            "file_path": str(f),
            "old_string": "second",
            "new_string": "second\ninserted",
        }))
        assert not result.error
        assert f.read_bytes() == b"first\r\nsecond\r\ninserted\r\nthird\r\n"
        assert b"\n" not in f.read_bytes().replace(b"\r\n", b""), (
            "a bare LF left in a CRLF file is the mixed shape this must not produce"
        )

    def test_the_error_paths_still_read_the_file_the_way_they_did(self, tmp_path):
        """Not-found and not-unique are decided on the caller's view, unchanged."""
        f = self._crlf(tmp_path)
        tool = EditTool()
        missing = _run(tool.execute({
            "file_path": str(f),
            "old_string": "nothing like this",
            "new_string": "x",
        }))
        assert missing.error and "old_string not found" in missing.content

        ambiguous = _run(tool.execute({
            "file_path": str(f),
            "old_string": "ir",
            "new_string": "x",
        }))
        assert ambiguous.error and "found 2 times" in ambiguous.content

        assert f.read_bytes() == b"first\r\nsecond\r\nthird\r\n", (
            "a refused edit must not rewrite the file"
        )


# ── the subject has to be a regular file (see the read tool's twin) ──


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
def test_edit_refuses_a_named_pipe_instead_of_blocking(tmp_path):
    """The check used to name a directory alone; a FIFO reached `open` and never returned."""
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    result = _run(EditTool().execute(
        {"file_path": str(fifo), "old_string": "a", "new_string": "b"}
    ))
    assert result.error, "a FIFO cannot be edited and must not be opened"
    assert "a FIFO (named pipe)" in result.content, result.content


def test_edit_still_refuses_a_directory(tmp_path):
    """The directory refusal survives, with its own wording, now stated as a kind."""
    result = _run(EditTool().execute(
        {"file_path": str(tmp_path), "old_string": "a", "new_string": "b"}
    ))
    assert result.error
    assert "is a directory" in result.content, result.content
