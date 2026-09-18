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
    _cwd_at_write_site,
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


# ---------------------------------------------------------------------------
# The other direction: a `cd` that stays *inside* moves the write site too
# (issue #1370).
#
# Everything above is about a move that *leaves* — the `..` climb, the
# `cd /elsewhere`, the nested shell. These rows are about the move that stays:
# the boundary joins a relative target onto the directory the child starts in,
# and that is the right reading only while the command writes from where it
# started. `cd sub && echo x > ../back.txt` creates `<workspace>/back.txt` —
# inside — while the join onto the start directory reads
# `<workspace>/../back.txt`, refuses it, and names a directory the file never
# appears in. `cd` into a subdirectory and climbing back is how a shell writes
# *beside* a subdirectory rather than in it, so the refusal was friction the
# caller could only avoid by spelling the target absolutely.
#
# Every row below was run for real in a scratch tree of the same shape
# (`ws/sub`, `ws/sub/sub2`) in `/bin/sh`, with the file's location read back off
# disk; the comment beside each says where it really lands. The verdicts are
# compared against that measured location, never against a rule — and the rows
# that must stay refused are there because each one is a way this reading can
# name a directory the shell is not in (the docstring of `_cwd_at_write_site`
# carries the same list).
# ---------------------------------------------------------------------------


def test_a_move_that_stays_inside_moves_the_write_site():
    """The three false blocks of issue #1370, and a chained move.

    Each really lands inside the workspace: `<ws>/back.txt`, `<ws>/sub/in.txt`,
    `<ws>/in2.txt` and `<ws>/deep.txt` respectively (measured, not inferred).
    """
    sub = spelled(os.path.join(WORKDIR, "sub"))
    assert _verdict("cd sub && echo x > ../back.txt") is True
    assert _verdict("cd sub && echo x > ../sub/in.txt") is True
    assert _verdict(f"cd {sub} && echo x > ../in2.txt") is True
    assert _verdict("cd sub && cd sub2 && echo x > ../../deep.txt") is True


def test_a_climb_that_leaves_is_still_refused():
    """The direction this change must not lose, now that `..` is measured from
    the write site: from *there*, a climb may still leave the workspace.

    `cd sub && echo x > ../../worse.txt` really writes the workspace's parent
    directory, and `cd sub && cd .. && echo x > ../outside.txt` really writes
    outside it — both measured. The last row is #1353's original case, which no
    move is involved in.
    """
    assert _verdict("cd sub && echo x > ../../worse.txt") is False
    assert _verdict("cd sub && cd .. && echo x > ../outside.txt") is False
    assert _verdict("echo x > ../escaped.txt") is False


def test_inside_and_outside_spellings_agree():
    """Parity for this family: the absolute spelling of one write gets the
    verdict the relative spelling gets. A verdict list would pass on a guard
    that simply allowed everything behind a `cd`; parity fails such a guard on
    the block side.

    The absolute paths are the *measured* landing places of the relative rows.
    """
    pairs = [
        ("cd sub && echo x > ../back.txt", os.path.join(WORKDIR, "back.txt"), True),
        (
            "cd sub && cd sub2 && echo x > ../../deep.txt",
            os.path.join(WORKDIR, "deep.txt"),
            True,
        ),
        (
            "cd sub && echo x > ../../worse.txt",
            os.path.join(os.path.dirname(WORKDIR), "worse.txt"),
            False,
        ),
    ]
    for relative, absolute, expected in pairs:
        assert _verdict(relative) is expected, relative
        assert _verdict(f"echo x > {spelled(absolute)}") is expected, absolute


@pytest.mark.parametrize(
    "cmd, lands",
    [
        # The write is *before* the move, so the move is not its write site:
        # measured, `<parent>/a.txt`.
        ("echo x > ../a.txt && cd sub", "the workspace's parent"),
        # `;` and `||` run the next statement whether or not the `cd` worked,
        # and a `cd` that failed leaves the shell in the start directory:
        # measured with `nosuchdir`, both land outside the workspace.
        ("cd nosuchdir; echo x > ../escape.txt", "the workspace's parent"),
        ("cd nosuchdir || echo x > ../escape2.txt", "the workspace's parent"),
        # A grouping boundary can put the `cd` in a shell of its own: measured,
        # `<parent>/back.txt` — the inner `cd ..` is the one that counts.
        ("cd sub && (cd .. && echo x > ../back.txt)", "the workspace's parent"),
        # The token is written by two statements, so the stream does not say
        # which one names the write: measured, `<parent>/back.txt`.
        (
            "cd sub && echo ../back.txt && cd .. && echo x > ../back.txt",
            "the workspace's parent",
        ),
        # A redirect attached to the `cd` itself is set up before the `cd` runs:
        # measured, `<parent>/f.txt`.
        ("cd sub > ../f.txt", "the workspace's parent"),
    ],
)
def test_the_write_site_is_refused_when_it_cannot_be_proven(cmd, lands):
    """Each row is a way the write site could be read as the wrong directory.

    They are not hypothetical: removing the rule each one exercises allows a
    command whose file really lands in ``lands`` (measured, one mutation arm per
    row). A guard that allowed them would be trading issue #1370's friction for
    an escape, which is exactly the direction a fail-closed boundary must not
    move.
    """
    assert _verdict(cmd) is False, cmd


def test_the_conditional_spellings_stay_refused_with_their_ground_truth():
    """The two shapes that stay refused although the shell really wrote inside.

    ``cd sub; echo x > ../back.txt`` lands inside only because ``sub`` existed;
    the same line with a directory that does not is an escape (measured), and
    the token stream cannot tell the two apart, so the reading keeps the refusal
    rather than assuming the ``cd`` worked. The second row is the price of the
    grouping bail-out, measured the same way. Both are pinned as residuals: the
    change that lifts them has to lift them deliberately.
    """
    assert _verdict("cd sub; echo x > ../back.txt") is False
    assert _verdict("cd sub && (echo x > ../back.txt)") is False


def test_the_walk_names_the_directory_the_file_lands_in():
    """The helper itself, apart from its caller.

    ``None`` is "no directory this walk can prove" — the caller keeps the start
    directory, which is the fail-closed reading — and it is what every uncertain
    shape answers, not only the ones with a `cd` in them.
    """
    real_workdir = os.path.realpath(WORKDIR)
    sub = os.path.realpath(os.path.join(WORKDIR, "sub"))
    assert (
        _cwd_at_write_site("cd sub && echo x > ../back.txt", WORKDIR, "../back.txt")
        == sub
    )
    # A non-move answers with the start directory, so the caller's join is
    # unchanged by this rule.
    assert (
        _cwd_at_write_site("echo x > ../back.txt", WORKDIR, "../back.txt")
        == real_workdir
    )
    # `env -C` moves its *child*; the shell that sets the redirect up does not
    # move, which is why the target really lands in the parent directory.
    assert (
        _cwd_at_write_site("env -C sub echo x > ../back.txt", WORKDIR, "../back.txt")
        == real_workdir
    )
    assert _cwd_at_write_site("cd sub; echo x > ../back.txt", WORKDIR, "../back.txt") is None
    assert _cwd_at_write_site("cd -; echo x > ../back.txt", WORKDIR, "../back.txt") is None
    assert _cwd_at_write_site("cd; echo x > ../back.txt", WORKDIR, "../back.txt") is None
    # A move the walk cannot place, and a target the token stream does not carry
    # as a word of its own (a git `--output=` value is a fragment of a token,
    # not a token) — "not proven" for both, which the caller reads as the start
    # directory.
    assert (
        _cwd_at_write_site("cd $UNSET && echo x > ../back.txt", WORKDIR, "../back.txt")
        is None
    )
    assert (
        _cwd_at_write_site(
            "cd sub && echo x > ../back.txt", WORKDIR, "--output=../back.txt"
        )
        is None
    )


# ---------------------------------------------------------------------------
# The shell's other move verb (issue #1381)
#
# `_cwd_left_workspace` follows `pushd <dir>` as it follows `cd <dir>` (issue
# #1362); its mirror read only `cd`, so the same command got two answers
# depending on which verb spelled the move. Measured in `/bin/sh`, `ws/sub`
# present, declared workspace `ws`, with the file's placement read back off
# disk: `cd <ws>/sub && echo x > ../gt-out.txt` is allowed and lands
# `<ws>/gt-out.txt`; `pushd <ws>/sub && echo x > ../gt-out.txt` was refused as
# resolving to `<ws>/../gt-out.txt` — a directory the file never appears in —
# while the same file landed inside.
#
# The rows below are the pair that makes the change discriminating in both
# directions, plus the forms that must keep the start directory: `pushd`'s
# options all mean there is no placeable destination (`-n` pushes without
# moving, `±N` indexes the stack), so reading the token after the flag would
# name a directory the shell never entered.
# ---------------------------------------------------------------------------


def test_both_move_verbs_place_the_write_site():
    """One command, one answer, whichever verb spells the move.

    Each row's landing place is the one measured in the shell: `../back.txt`
    after a move into `sub` is `<ws>/back.txt`, and after climbing twice it is
    `<ws>/../worse.txt`. The absolute spellings are those same paths, so the
    verdict cannot be a list of blocked or allowed strings — a guard that
    answered by verb rather than by placement would fail one of the two.
    """
    sub = os.path.realpath(os.path.join(WORKDIR, "sub"))
    for verb in ("cd", "pushd"):
        # Inside: refused before this change, and the file really lands inside.
        assert _verdict(f"{verb} sub && echo x > ../back.txt") is True, verb
        assert (
            _cwd_at_write_site(
                f"{verb} sub && echo x > ../back.txt", WORKDIR, "../back.txt"
            )
            == sub
        ), verb
        assert _verdict(f"{verb} {spelled(sub)} && echo x > ../in2.txt") is True, verb
        assert _verdict(f"echo x > {spelled(os.path.join(WORKDIR, 'in2.txt'))}") is True
        # `--` ends option parsing for both verbs — measured, `pushd -- <dir>`
        # moves in sh, bash and zsh — so it must not be read as the flag that
        # names no destination.
        assert _verdict(f"{verb} -- sub && echo x > ../back.txt") is True, verb
        # Outside: the reading that must survive the change. Measured, the file
        # lands in the workspace's parent.
        assert _verdict(f"{verb} sub && echo x > ../../worse.txt") is False, verb
        assert (
            _verdict(f"echo x > {spelled(os.path.join(os.path.dirname(WORKDIR), 'worse.txt'))}")
            is False
        )


def test_a_pushd_form_with_no_placeable_destination_keeps_the_start_directory():
    """`pushd`'s flags and the stack forms answer "not proven", never a guess.

    `-n` pushes the directory and does **not** move, so the shell is in the
    start directory and `../back.txt` really does land outside the workspace
    (measured) — the refusal on that row is the correct reading, not friction.
    Reading the token after `-n` as the destination would join the write onto a
    directory the shell never entered, which is the one direction this walk must
    not move.
    """
    assert _cwd_at_write_site(
        "pushd -n sub && echo x > ../back.txt", WORKDIR, "../back.txt"
    ) is None
    assert _verdict("pushd -n sub && echo x > ../back.txt") is False
    assert (
        _cwd_at_write_site("pushd +1 && echo x > ../back.txt", WORKDIR, "../back.txt")
        is None
    )
    assert (
        _cwd_at_write_site("pushd && echo x > ../back.txt", WORKDIR, "../back.txt")
        is None
    )
    # `popd` is not read as a move at all: its destination is an entry the stack
    # pushed earlier, and a non-move answers with the start directory — the same
    # fail-closed base `None` gives the caller.
    assert _cwd_at_write_site(
        "popd && echo x > ../back.txt", WORKDIR, "../back.txt"
    ) == os.path.realpath(WORKDIR)

