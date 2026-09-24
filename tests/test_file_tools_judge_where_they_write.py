"""The write and the predicate that judges it must name the same file (issue #1558).

The class
---------
At ``workspace-write`` the in-process file tools were judged by a predicate that
returned early on every **relative** path — "assumed in-workspace (cwd = the
workspace root)" — while the write itself resolved that path against the
**daemon process's cwd**, a different tree. Each half is correct for its own
stated assumption, and jointly they are wrong: the gate answers about one file
and ``write_text`` touches another, with nothing between them (an in-process
write is not kernel-confined by the Seatbelt/bwrap profile the way a bash child
is, and ``emrg/sandbox/fence.py`` does not exist yet).

``check_read_only_file_write`` had the mirror-image slip: it realpath'd the
spelling as given — i.e. against the daemon's cwd — so a relative path could land
*inside* the workspace without that function ever having looked there, which is
the dirty-tree guard bypassed rather than an unrelated file written.

What is asserted here is therefore the **composition**, not either predicate
alone: for a relative input, the file the tool writes is the file the predicate
judged. Each test pins the process cwd to ``tmp_path`` (``monkeypatch.chdir``), so
the "daemon's cwd" of the measurement is a directory this test owns and a
regression lands inside it rather than in a host tree — the base the pre-fix code
used is then observable as a *different file inside the same tmp_path*, which is
what makes these tests discriminating instead of merely green.

The rule they pin is one function, ``resolve_file_target`` — the same reduction
#1353 installed for the command scan (join the target onto the base, then require
the realpath to stay under it), applied to the two in-process writers.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from emrg.tools.bash_tool import (
    check_read_only_file_write,
    check_workspace_write,
    resolve_file_target,
)
from emrg.tools.edit_tool import EditTool
from emrg.tools.write_tool import WriteTool


def _run(coro):
    return asyncio.run(coro)


def _workspace(tmp_path: Path) -> Path:
    """The session cwd for these tests: a directory the test owns."""
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_a_relative_target_is_joined_onto_the_workspace_not_the_cwd(tmp_path, monkeypatch):
    """The one decision, stated: the base is the declared workspace.

    The comparison is ``Path``-to-``Path``, not string-to-string, and that is a
    measured requirement rather than tidiness: ``os.path.join`` appends the
    second part **as spelled**, so a relative target written with a forward slash
    comes back as ``…\\ws\\sub/f.txt`` on Windows — the mixed-separator spelling
    of the file ``tmp_path/"sub"/"f.txt"`` names. String equality would call that
    a regression on Windows and only on Windows (this test failed the Windows CI
    leg on exactly that line, 2026-09-24); path equality compares the file.
    """
    monkeypatch.chdir(tmp_path)
    ws = _workspace(tmp_path)
    assert Path(resolve_file_target("sub/f.txt", str(ws))) == ws / "sub" / "f.txt"
    # An absolute target is judged as spelled; a caller that declared no boundary
    # has no base to join onto and keeps the old reading.
    assert resolve_file_target(str(tmp_path / "abs.txt"), str(ws)) == str(tmp_path / "abs.txt")
    assert resolve_file_target("sub/f.txt", None) == "sub/f.txt"


def test_a_relative_write_lands_inside_the_workspace(tmp_path, monkeypatch):
    """The composition half that was never asserted: judge and write agree.

    Under the pre-fix reading this file appears at ``<cwd>/sub/f.txt`` — i.e.
    beside the workspace, not in it — so both assertions below flip together.
    """
    monkeypatch.chdir(tmp_path)
    ws = _workspace(tmp_path)
    result = _run(WriteTool().execute({
        "file_path": "sub/f.txt",
        "content": "x\n",
        "sandbox": "workspace-write",
        "workspace": str(ws),
    }))
    assert not result.error, result.content
    assert (ws / "sub" / "f.txt").read_text() == "x\n", (
        "the write did not land inside the workspace it was judged against"
    )
    assert not (tmp_path / "sub" / "f.txt").exists(), (
        "the write was resolved against the process cwd — the base the predicate "
        "does not judge (issue #1558)"
    )


def test_a_relative_escape_is_judged_like_its_absolute_spelling(tmp_path, monkeypatch):
    """A relative path that leaves the workspace is no longer exempt.

    Measured before the fix (pure predicate, issue #1558): `../outside.txt` and
    `../../../tmp/escape.txt` both answered ``None`` — allowed — because the
    relative branch returned before any realpath ran.

    The target is placed outside the workspace **and** outside the two zones a
    ``workspace-write`` session may always write (the OS temp area and the
    evolution module's own data root): with a target under the temp root both
    spellings are legitimately allowed, so a test built on ``tmp_path`` alone
    would agree with the defect. ``$HOME`` appears here **only** as an input to a
    pure predicate — it is realpath'd and nothing is opened, and the write that
    follows this judgement is asserted never to happen.
    """
    monkeypatch.chdir(tmp_path)
    ws = _workspace(tmp_path)
    outside = Path.home() / "emrg-1558-does-not-exist" / "f.txt"
    spelling = os.path.relpath(outside, ws)
    assert ".." in spelling, f"the fixture must actually leave the workspace: {spelling!r}"

    absolute_verdict = check_workspace_write(str(outside), str(ws))
    assert absolute_verdict, "the fixture is not outside the boundary to begin with"
    relative_verdict = check_workspace_write(spelling, str(ws))
    assert relative_verdict, (
        f"{spelling!r} is still exempt from the workspace boundary — the relative "
        "branch returned before any realpath ran (issue #1558)"
    )
    # Not a string comparison: the message quotes the spelling it was handed, while
    # the *judgement* is about the file, and the same file spelled two ways is the
    # property under test. The refusal is additionally required to name where the
    # spelling resolved — a relative target that is refused without saying where it
    # pointed is the same "which file?" gap one layer down.
    #
    # Compared as ``repr`` because that is how the message renders it (`{real!r}`):
    # on Windows a raw backslash path is *doubled* inside the string, so the
    # un-repr'd spelling is not a substring of it and this line was red on the
    # Windows CI leg alone (measured 2026-09-24). ``repr`` is the same on both
    # sides of that comparison, so one assertion holds on either platform.
    assert repr(os.path.realpath(str(outside))) in relative_verdict, relative_verdict
    # The control: inside the workspace both spellings are allowed.
    inside = ws / "f.txt"
    assert check_workspace_write(str(inside), str(ws)) is None
    assert check_workspace_write(os.path.relpath(inside, ws), str(ws)) is None


def test_a_relative_escape_never_reaches_the_disk(tmp_path, monkeypatch):
    """The write half of the same row: a refused escape leaves nothing behind.

    ``../outside.txt`` from a workspace under ``tmp_path`` is *allowed* by the
    zones above (it lands in the OS temp area), so this row uses an outside
    directory that exists and is owned by this test: the workspace is
    ``tmp_path/ws`` and the spelling climbs to ``tmp_path/outside.txt``, which is
    refused by nothing — hence the assertion is about where the bytes landed, not
    about a refusal. The refused shape is judged in the test above.
    """
    monkeypatch.chdir(tmp_path)
    ws = _workspace(tmp_path)
    result = _run(WriteTool().execute({
        "file_path": "../outside.txt",
        "content": "x\n",
        "sandbox": "workspace-write",
        "workspace": str(ws),
    }))
    assert not result.error, result.content
    assert (tmp_path / "outside.txt").read_text() == "x\n", (
        "the joined target is the file the tool writes"
    )
    assert not (ws / "outside.txt").exists()


def test_a_relative_edit_inside_the_workspace_is_allowed_and_a_relative_escape_is_not(
    tmp_path, monkeypatch
):
    """The edit side of the same pair — the two tools must not drift apart."""
    monkeypatch.chdir(tmp_path)
    ws = _workspace(tmp_path)
    (ws / "in.txt").write_text("old\n")
    result = _run(EditTool().execute({
        "file_path": "in.txt",
        "old_string": "old",
        "new_string": "new",
        "sandbox": "workspace-write",
        "workspace": str(ws),
    }))
    assert not result.error, result.content
    assert (ws / "in.txt").read_text() == "new\n"
    assert not (tmp_path / "in.txt").exists()

    escaped = _run(EditTool().execute({
        "file_path": os.path.relpath(
            Path.home() / "emrg-1558-does-not-exist" / "f.txt", ws
        ),
        "old_string": "old",
        "new_string": "new",
        "sandbox": "workspace-write",
        "workspace": str(ws),
    }))
    assert escaped.error and "outside workspace" in escaped.content, escaped.content


def test_a_relative_path_is_not_a_way_past_the_read_only_fence(tmp_path, monkeypatch):
    """The read-only half: relative no longer means "somewhere I did not look".

    Before the fix this row was *allowed* — the predicate realpath'd the spelling
    against the process cwd, a base outside the workspace the caller declared — so
    its containment answer was about a path the caller never named, while the
    absolute spelling of that same file was blocked at this tier. The write used
    the same base, so it landed outside the declared workspace rather than inside
    it: the hole is *which* file the predicate was asked about, not one it never
    saw. Re-measured on both trees: pre-fix the relative spelling was allowed here
    while the absolute spelling of that same file was blocked.
    """
    monkeypatch.chdir(tmp_path)
    ws = _workspace(tmp_path)
    reason = check_read_only_file_write("rel.txt", str(ws))
    assert reason and "read-only sandbox" in reason, reason
    result = _run(WriteTool().execute({
        "file_path": "rel.txt",
        "content": "x\n",
        "sandbox": "read-only",
        "workspace": str(ws),
    }))
    assert result.error and "read-only sandbox" in result.content, result.content
    assert not (ws / "rel.txt").exists()
    assert not (tmp_path / "rel.txt").exists()


def test_the_no_workspace_reading_is_unchanged(tmp_path, monkeypatch):
    """The control: with no declared boundary, neither predicate invents a base.

    The daemon always injects ``workspace`` for ``write``/``edit``, so this is the
    non-daemon path — and it is the one the fix must not silently tighten, since
    there is no boundary the caller asked for.
    """
    monkeypatch.chdir(tmp_path)
    assert check_workspace_write("rel.txt", None) is None
    assert check_read_only_file_write("rel.txt", None) is None
    result = _run(WriteTool().execute({"file_path": "rel.txt", "content": "x\n"}))
    assert not result.error, result.content
    assert (tmp_path / "rel.txt").read_text() == "x\n"
