"""A relative write target after the command moved its own cwd (issue #1244).

The ``workspace-write`` boundary reads a relative target as "inside the
workspace, because the cwd is the workspace root". ``cd <dir>`` and
``env -C <dir>`` move the shell first, so the same text can name a file the
boundary cannot place — measured on master,
`cd /elsewhere; echo x > out.txt` truncated `/elsewhere/out.txt` while the
guard allowed it.

The property under test is *parity*, not a list of blocked strings: however the
guard judges the absolute path, it must judge the relative spelling of the same
write the same way. A verdict list would pass on a guard that simply refused
every command containing ``cd``; parity fails such a guard on the allow side.

Every path is spelled with forward slashes on purpose. The guard reads targets
and operands from a POSIX-style token stream, where a backslash is an escape
character, so a Windows spelling `C:\\Users\\x` reaches it as `C:Usersx` — a
name that is not absolute at all (issue #1261). Using forward slashes keeps
this file a test of the cwd rule instead of a test of that separate defect;
Windows resolves both spellings to the same path.
"""

import os
import tempfile

import pytest

from emrg.tools.bash_tool import (
    _check_sandbox,
    _cwd_left_workspace,
    _is_absolute_path,
    _is_within,
    _temp_write_roots,
    _trusted_write_zones,
)

# Synthetic paths: the checks are textual, so the directories need not exist.
WORKDIR = os.path.join(os.path.expanduser("~"), "Documents", "emrg-cwd-ws")
OUTSIDE = os.path.join(os.path.expanduser("~"), "Documents", "emrg-cwd-outside")
TEMP = tempfile.gettempdir()
WW = "workspace-write"
RO = "read-only"


def spelled(path: str) -> str:
    """The path as a command line spells it (forward slashes)."""
    return path.replace(os.sep, "/") if os.sep != "/" else path


def _verdict(cmd: str, mode: str = WW, workdir: str | None = WORKDIR) -> bool:
    allowed, _reason, _enforcement = _check_sandbox(cmd, mode, workdir)
    return allowed


def test_premise_workdir_is_judgeable():
    """The fixture itself must sit where the boundary can tell outside apart."""
    assert _is_absolute_path(WORKDIR)
    assert _is_within(os.path.join(WORKDIR, "x"), WORKDIR)
    assert not _is_within(OUTSIDE, WORKDIR)
    assert not _is_within(os.path.dirname(WORKDIR), WORKDIR)
    # And no trusted/temp root may swallow the "outside" directory, or every
    # block below would be measuring the fixture rather than the guard.
    roots = [_temp_write_roots(), _trusted_write_zones()]
    assert not any(_is_within(OUTSIDE, r) for group in roots for r in group)
    # The spelling used in every command below must be one the guard reads as
    # absolute — the premise the blocks rest on (issue #1261 is why this file
    # does not use backslashes).
    assert _is_absolute_path(spelled(OUTSIDE))


@pytest.mark.parametrize(
    "directory",
    [
        WORKDIR,
        os.path.join(WORKDIR, "sub"),
        TEMP,
        os.path.join(TEMP, "emrg-child"),
        OUTSIDE,
        os.path.dirname(WORKDIR),
    ],
)
def test_relative_after_cd_gets_the_absolute_verdict(directory):
    """The parity property: both spellings of one write get one verdict."""
    relative = f"cd {spelled(directory)}; echo x > out.txt"
    absolute = f"echo x > {spelled(os.path.join(directory, 'out.txt'))}"
    assert _verdict(relative) == _verdict(absolute), (
        f"relative-after-cd and absolute disagree for {directory!r}"
    )


def test_leaving_the_workspace_blocks_a_relative_write():
    """The live fail-open: it wrote outside while the guard read "in-workspace"."""
    assert _verdict(f"cd {spelled(OUTSIDE)}; echo x > out.txt") is False
    assert _verdict(f"cd {spelled(OUTSIDE)} && echo x > out.txt") is False
    assert _verdict(f"cd {spelled(os.path.dirname(WORKDIR))}; echo x > out.txt") is False
    assert _verdict(f"cd {spelled(OUTSIDE)}; rm -rf build") is False


def test_the_block_names_the_directory_that_moved_the_shell():
    allowed, reason, _ = _check_sandbox(
        f"cd {spelled(OUTSIDE)}; echo x > out.txt", WW, WORKDIR
    )
    assert allowed is False
    # The message must name both the target and the directory that moved the
    # shell — a block that does not say where the write would land is not
    # actionable. Compared by basename so the check does not depend on how the
    # platform spells the path back.
    assert "out.txt" in reason and os.path.basename(OUTSIDE) in reason, reason


def test_env_chdir_is_a_move_too():
    """`env -C <dir>` starts its child there, exactly as `cd` would."""
    assert _verdict(f"env -C {spelled(OUTSIDE)} sh -c 'echo x > out.txt'") is False
    assert _verdict(f"env --chdir={spelled(OUTSIDE)} sh -c 'echo x > out.txt'") is False
    inside = spelled(os.path.join(WORKDIR, "sub"))
    assert _verdict(f"env -C {inside} sh -c 'echo x > out.txt'") is True


def test_a_nested_shell_is_read_the_same_way():
    assert _verdict(f"sh -c 'cd {spelled(OUTSIDE)}; echo x > out.txt'") is False
    inside = spelled(os.path.join(WORKDIR, "sub"))
    assert _verdict(f"sh -c 'cd {inside}; echo x > out.txt'") is True


def test_the_detector_resolves_chained_moves():
    """`cd a; cd b` resolves b against a, not against the workspace root.

    The discriminating case is a first move that stays inside: only a walk that
    carries the cwd forward lands on the workspace's parent — one that resolved
    every move against the workspace root would report the parent of that
    parent. The reported directory is the first one outside, which is the one
    that made the relative reading possible.
    """
    parent = os.path.realpath(os.path.dirname(WORKDIR))
    sub = spelled(os.path.join(WORKDIR, "sub"))
    assert _cwd_left_workspace(f"cd {sub}; cd ..", WORKDIR) is None
    assert _cwd_left_workspace(f"cd {sub}; cd ../..", WORKDIR) == parent
    assert _cwd_left_workspace("cd ..; cd ..; ls", WORKDIR) == parent
    # A bare `cd` goes $HOME; `cd -` names $OLDPWD, which nothing can resolve.
    assert _cwd_left_workspace("cd; ls", WORKDIR) == os.path.realpath(
        os.path.expanduser("~")
    )
    assert _cwd_left_workspace("cd -; ls", WORKDIR) == "-"


@pytest.mark.parametrize(
    "cmd",
    [
        "echo x > out.txt",
        "rm -rf build",
        "cp -r src dst",
        "cd sub && echo x > out.txt",
        f"cd {spelled(WORKDIR)}/sub; echo x > out.txt",
        f"cd {spelled(TEMP)}; echo x > out.txt",
        'echo "cd /elsewhere; echo x > out.txt" > out.txt',
        'grep -rn "cd /elsewhere" . > out.txt',
        f"cd {spelled(OUTSIDE)}",  # a move with no write in the same command
        "env -C . echo hi > out.txt",
    ],
)
def test_staying_inside_is_unchanged(cmd):
    """The guard must not become a refusal of every command that names `cd`."""
    assert _verdict(cmd) is True, cmd


def test_read_only_is_untouched_by_this_rule():
    """read-only blocks every non-/dev/null target already; the new rule adds
    nothing there and must not change those verdicts."""
    for cmd in (
        f"cd {spelled(OUTSIDE)}; echo x > out.txt",
        f"cd {spelled(WORKDIR)}; echo x > out.txt",
    ):
        assert _verdict(cmd, RO) is False
    assert _verdict(f"cd {spelled(OUTSIDE)}; echo x > /dev/null", RO) is True


def test_without_a_workspace_the_moved_cwd_is_still_read():
    """An omitted workdir skips the *workspace*, not the question (issue #1359).

    This asserted ``is True`` for the first command until #1359 measured what
    that meant: with no ``workdir`` the move-out question was never asked, so
    the row above was refused with a workspace declared and allowed without one
    — issue #1244's defect, keyed on the caller instead of on the command. The
    question is now asked about the cwd the child actually inherits (see
    ``execute()``: ``cwd=workdir``, and a None cwd means "inherit"), which is
    the only directory this guard can place the relative target in.

    The boundary itself does not move: with no declared workspace the workspace
    is still not an allowed root, so a command that stays put keeps its verdict
    (``rm -rf build`` below) — one hole closed, not a blanket refusal of every
    command that omits an argument. #1359's acceptance was exactly this pair.
    """
    assert _verdict(f"cd {spelled(OUTSIDE)}; echo x > out.txt", WW, None) is False
    assert _verdict(f"cd {spelled(OUTSIDE)}; echo x > out.txt", WW, WORKDIR) is False
    # Controls: no move, so the relative target keeps the old assumption.
    assert _check_sandbox("rm -rf build", WW, None)[0] is True
    assert _check_sandbox("echo x > out.txt", WW, None)[0] is True
