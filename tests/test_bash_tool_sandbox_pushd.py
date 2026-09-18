"""A relative write target after `pushd` / `popd` / a prefixed `cd` (issue #1362).

The `cd` rule (issue #1244, `test_bash_tool_sandbox_cwd.py`) asks where a command
writes from, not where it started. Three spellings moved the shell while the walk
did not follow, measured on master:

* `pushd <dir>` — the same move under the stack's name. The walk read `pushd` as
  an ordinary word, so `pushd /elsewhere && echo x > out.txt` created
  `/elsewhere/out.txt` while the guard read `out.txt` as in-workspace.
* `builtin cd <dir>` — `cd` reached through the prefix that `_COMMAND_WRAPPERS`
  did not list, so `cd` was not in command position (the same held for
  `builtin cd -P`, a grouped `(builtin cd …)`, and `sh -c 'builtin cd …; …'`).
* the stack forms themselves — `popd`, a bare `pushd`, `pushd ±N` — whose
  destination is a directory an earlier `pushd` pushed, i.e. a value this token
  stream does not carry. They are refused, like `cd -`, rather than placed.

The property under test is *parity* (as in the `cd` file): however the guard
judges the absolute spelling of a write, it must judge the relative-after-move
spelling the same way. A verdict list would pass on a guard that refused every
command mentioning `pushd`; parity fails such a guard on the allow side.

Paths are spelled with forward slashes for the reason the `cd` file gives
(issue #1261: a backslash reaches the token stream as an escape character, so a
Windows spelling is not read as absolute at all).
"""

import os
import shutil
import subprocess
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
WORKDIR = os.path.join(os.path.expanduser("~"), "Documents", "emrg-pushd-ws")
OUTSIDE = os.path.join(os.path.expanduser("~"), "Documents", "emrg-pushd-outside")
INSIDE_SUB = os.path.join(WORKDIR, "sub")
TEMP = tempfile.gettempdir()
WW = "workspace-write"
RO = "read-only"


def spelled(path: str) -> str:
    """The path as a command line spells it (forward slashes)."""
    return path.replace(os.sep, "/") if os.sep != "/" else path


def _verdict(cmd: str, mode: str = WW, workdir: str | None = WORKDIR) -> bool:
    allowed, _reason, _enforcement = _check_sandbox(cmd, mode, workdir)
    return allowed


def test_premise_the_fixture_is_judgeable():
    """The fixture itself must sit where the boundary can tell outside apart.

    Without this, a block below could be measuring the fixture — e.g. an
    ``OUTSIDE`` swallowed by a trusted or temp root would make every "refused"
    row true for the wrong reason.
    """
    assert _is_absolute_path(WORKDIR)
    assert _is_absolute_path(spelled(OUTSIDE))
    assert not _is_within(OUTSIDE, WORKDIR)
    roots = [_temp_write_roots(), _trusted_write_zones()]
    assert not any(_is_within(OUTSIDE, r) for group in roots for r in group)


@pytest.mark.parametrize(
    "directory",
    [WORKDIR, INSIDE_SUB, TEMP, os.path.join(TEMP, "emrg-child"), OUTSIDE,
     os.path.dirname(WORKDIR)],
)
def test_pushd_is_a_move_like_cd(directory):
    """The parity property, for the stack's spelling of a move.

    ``pushd <dir>`` moves the shell exactly as ``cd <dir>`` does, so the relative
    spelling of a write after it must get the absolute spelling's verdict. The
    rows cover both sides: a destination inside the workspace (allow) and one
    outside it or in its parent (refuse).
    """
    for separator in ("&&", ";"):
        relative = f"pushd {spelled(directory)} {separator} echo x > out.txt"
        absolute = f"echo x > {spelled(os.path.join(directory, 'out.txt'))}"
        assert _verdict(relative) == _verdict(absolute), (
            f"relative-after-pushd and absolute disagree for {directory!r}"
        )


def test_leaving_the_workspace_blocks_a_relative_write():
    """The live fail-open: it wrote outside while the guard read "in-workspace"."""
    assert _verdict(f"pushd {spelled(OUTSIDE)} && echo x > out.txt") is False
    assert _verdict(f"pushd {spelled(OUTSIDE)}; echo x > out.txt") is False
    assert _verdict(f"pushd {spelled(OUTSIDE)} && rm -rf build") is False
    assert _verdict(f"pushd {spelled(os.path.dirname(WORKDIR))}; echo x > out.txt") is False
    # The same move through a nested shell and behind `env`: read like every
    # other move the walk follows.
    assert _verdict(f"sh -c 'pushd {spelled(OUTSIDE)}; echo x > out.txt'") is False


def test_the_builtin_prefix_is_a_command_position():
    """`builtin cd` is `cd`: the prefix consumes one word, like `command`."""
    outside = spelled(OUTSIDE)
    assert _verdict(f"builtin cd {outside} && echo x > out.txt") is False
    assert _verdict(f"builtin cd {outside}; echo x > out.txt") is False
    # A flag between the prefix and the directory must not hide the operand.
    assert _verdict(f"builtin cd -P {outside} && echo x > out.txt") is False
    # Grouped and nested: the same command, read the same way.
    assert _verdict(f"(builtin cd {outside} && echo x > out.txt)") is False
    assert _verdict(f"sh -c 'builtin cd {outside}; echo x > out.txt'") is False
    # And the allow side, so this is not a blanket refusal of the word `builtin`.
    inside = spelled(INSIDE_SUB)
    assert _verdict(f"builtin cd {inside} && echo x > out.txt") is True
    assert _verdict(f"builtin cd {spelled(WORKDIR)} && echo x > out.txt") is True


@pytest.mark.parametrize(
    "move",
    [
        "popd",
        "pushd",
        "pushd +1",
        "pushd -1",
        "popd +0",
    ],
)
def test_the_stack_forms_are_refused(move):
    """A destination that lives on the shell's directory stack is not placeable.

    Every row's destination is whatever an earlier `pushd` pushed — a value this
    token stream does not carry — so the walk answers with the verb, the way it
    answers `cd -`. Refusing is the fail-closed side; the caller's work-around is
    to spell the write target absolutely.
    """
    assert _verdict(f"{move} && echo x > out.txt") is False, move
    assert _verdict(f"{move}; echo x > out.txt") is False, move
    # The helper names the verb it could not place, so the block is actionable.
    assert _cwd_left_workspace(f"{move} && echo x > out.txt", WORKDIR) == move.split()[0]


def test_the_block_names_the_directory_that_moved_the_shell():
    allowed, reason, _ = _check_sandbox(
        f"pushd {spelled(OUTSIDE)} && echo x > out.txt", WW, WORKDIR
    )
    assert allowed is False
    assert "out.txt" in reason and os.path.basename(OUTSIDE) in reason, reason


def test_staying_inside_is_unchanged():
    """The guard must not become a refusal of every command that names pushd."""
    inside = spelled(INSIDE_SUB)
    for cmd in (
        f"pushd {inside} && echo x > out.txt",
        f"pushd {inside}; echo x > out.txt",
        f"builtin cd {inside} && echo x > out.txt",
        # A move with no write in the same command is not a write risk at all —
        # the same reading `cd <outside>` alone gets in the `cd` file.
        f"pushd {spelled(OUTSIDE)}",
        "popd",
        "pushd",
        "echo x > out.txt",
        # Quoted text is not a command: the walk must not read these as moves.
        'echo "pushd /elsewhere && echo x > out.txt" > out.txt',
        'grep -rn "pushd /elsewhere" . > out.txt',
        'echo "builtin cd /elsewhere" > out.txt',
    ):
        assert _verdict(cmd) is True, cmd


def test_read_only_is_untouched_by_this_rule():
    """read-only blocks every non-/dev/null target already."""
    for cmd in (
        f"pushd {spelled(OUTSIDE)} && echo x > out.txt",
        f"builtin cd {spelled(OUTSIDE)} && echo x > out.txt",
        f"pushd {spelled(WORKDIR)} && echo x > out.txt",
    ):
        assert _verdict(cmd, RO) is False
    assert _verdict(f"pushd {spelled(OUTSIDE)} && echo x > /dev/null", RO) is True


def test_the_new_prefix_carries_the_same_over_approximation_as_command():
    """`builtin` costs exactly what `command` already cost — no more.

    Both prefixes make the word after them a command position, so a directory
    merely *named* behind them reads as an invocation: `echo builtin cd <dir>`
    is refused, as `echo command cd <dir>` already was. Pinned as parity rather
    than as a new defect, and pinned at all because the direction is the loud
    one: the price of the fix is a false block, never an escape.
    """
    outside = spelled(OUTSIDE)
    for prefix in ("builtin", "command"):
        cmd = f"echo {prefix} cd {outside} > out.txt"
        assert _verdict(cmd) is False, cmd


def test_the_conservative_refusals_are_pinned_with_their_ground_truth():
    """Shapes the walk refuses even though the shell really wrote inside.

    Each row's destination is knowable only by running the shell, which is what
    the fix's docstring records. They are pinned as residuals: a later change may
    lift them, but it has to lift them deliberately.

    * `pushd <inside>/sub && popd && echo x > f.txt` returns to the directory the
      shell started in and lands `f.txt` inside the workspace — refused at the
      `popd`, because the stack entry is not in the token stream.
    * `pushd <outside> && popd && …` is refused one statement earlier, at the
      `pushd`, exactly as the `cd <outside>; cd <back>` spelling is.
    * `pushd -n <dir>` pushes without moving; the walk skips flags rather than
      interpreting them, so it reads the destination as a move (`-n` on the row
      above is what makes it a refusal rather than a miss).
    """
    assert _verdict(f"pushd {spelled(INSIDE_SUB)} && popd && echo x > f.txt") is False
    assert _verdict(f"pushd {spelled(OUTSIDE)} && popd && echo x > f.txt") is False
    assert _verdict(f"pushd -n {spelled(OUTSIDE)} && echo x > f.txt") is False


def test_the_prefix_reaches_the_read_only_git_block_too():
    """A second consumer the one-word addition reaches, pinned on purpose.

    The change is in `_COMMAND_WRAPPERS`, which every consumer of
    `_runs_as_a_command` reads, not only this walk. The one that matters is the
    ``read-only`` git-mutator check: measured on master (`bash_tool.py` sha16
    `46d3e0161f47d0c9`), `git push origin master` is BLOCK there while
    ``builtin git push origin master`` came back **ALLOW** — the prefix left the
    git verb outside command position, so a mutator reached the read-only tier
    as an argument. `builtin git reset --hard HEAD` and `builtin git commit`
    were allowed the same way.

    Found by an external contributor's independent run
    (how2how2how2-arch on #1379, "worth naming in the PR") and re-measured here
    on both arms before pinning: on _this_ arm all three are BLOCK, and the
    read verbs stay allowed. Pinned because the read-only half would otherwise
    be an implicit consequence of a fixture in a workspace-write test — a later
    change could drop it without any test noticing.

    Classification only: nothing here is executed.
    """
    for mutator in (
        "git push origin master",
        "git reset --hard HEAD",
        "git commit -m x",
    ):
        # The prefix must not widen the tier: the bare and prefixed spellings
        # get the same verdict, exactly as `command`/`env` already did.
        for prefix in ("", "builtin ", "command ", "env "):
            cmd = f"{prefix}{mutator}"
            assert _verdict(cmd, RO) is False, cmd
    # Read verbs are not collateral damage: the block is about mutators, and the
    # prefix must not turn a read into a refusal.
    for read in ("builtin git status", "builtin git log --oneline -1", "builtin cat f.txt"):
        assert _verdict(read, RO) is True, read


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the ground truth needs a POSIX shell: the bash on the Windows runner is Git Bash, "
        "which reads `pushd C:\\...` as an error (measured, exit 1 on run 35336751754), so it "
        "cannot witness the semantics this arm is about. The corpus above is asserted on BOTH "
        "platforms - that is where the Windows leg's value is - and this arm is the instrument "
        "for a POSIX-shell claim, so it is gated to the platform that has one."
    ),
)
def test_the_two_new_spellings_really_move_the_shell(tmp_path):
    """Ground truth for the two escapes, in a directory this test builds.

    A verdict mismatch alone is not a bug, so the claim "the shell moved and the
    file landed outside" is measured rather than asserted. ``builtin`` and
    ``pushd`` are bash builtins (POSIX sh has neither), so the arm needs bash;
    the temp directory is the test's own, never a host working directory.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available for the ground-truth run")
    work = tmp_path / "work"
    outside = tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    for name, cmd in (
        ("pushd", f"pushd {outside} && echo x > f_pushd.txt"),
        ("builtin", f"builtin cd {outside} && echo x > f_builtin.txt"),
    ):
        subprocess.run([bash, "-c", cmd], cwd=work, capture_output=True, check=True)
        assert (outside / f"f_{name}.txt").exists(), (
            f"{name}: the shell did not write outside, so this arm has no job"
        )
        assert not (work / f"f_{name}.txt").exists(), name
