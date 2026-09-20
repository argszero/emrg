"""`git`'s `--output[=]<file>` is read where git reads it (issue #1464).

`git diff --output=<f>` truncates and writes `<f>` exactly as `> <f>` does, so the
write-target walk names it. The reader that does that was a **second copy** of the
walk that finds git's subcommand: it scanned every token after `git`, so a word a
*global* option had already eaten was still read as a flag and the word after it was
named — a path the run never writes, and therefore a false block whenever it lies
outside the allowed roots (the guard refuses on a name, not on a verdict about what
the command does).

Ground truth first, one fresh repository per row on this host (git 2.50.1, Apple
Git-155, `a.txt` modified after a commit and the listing read back off disk):

* `git diff --output=x`          rc=0   `x` written (105 B) — the write this reader exists for;
* `git -C . diff --output=x`     rc=0   `x` written — a global option **with a value**
                                        before the flag does not hide it, which is what makes
                                        the step below safe to take rather than merely tidy;
* `git diff --output x`          rc=0   `x` written — the spaced form is real, so the reader
                                        must keep reading it;
* `git -c --output=x diff`       rc=128 `error: key does not contain a section: --output`,
                                        nothing written — `-c` took the whole token as its
                                        config string, so no output flag was in force. This is
                                        the row the issue measured;
* `git --output=x diff`          rc=129 `unknown option`, nothing written — git has **no**
                                        global `--output` (`git -h` lists its global options and
                                        no such flag is among them), so the issue's "control"
                                        writes nothing either;
* `git status --output=x`        rc=129 `unknown option`, nothing written — `--output` is not
                                        every verb's option, it is the diff/log family's.

Every token the flag can really occupy is inside the subcommand's argument list, and
every token outside it is one git refuses — so the reader asks the shared walk
(`_git_invocation_at`) for that list instead of writing its own. What that walk cannot
answer is *which* verbs have the flag at all: `git status --output=<f>` is named as if
`status` wrote it, while git exits 129 without writing. That residual is pinned below
with its reason rather than left to be re-derived. Nothing here executes a command:
`_check_sandbox` is a pure predicate, and the paths below are inputs to it.
"""

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import _check_sandbox, _extract_write_targets

# Outside every allowed root (the workspace, the OS temp root, the evolution data dir),
# and only ever an argument to the pure predicate — never executed, never opened.
OUTSIDE = "/outside/emrg"

WORKSPACE = "/workspace"


# (row, command, every path the walk must name). The flag really writes here, so these
# are the rows a reader that stops reading the flag would lose — the fail-open side of
# the same change.
FLAG_IN_FORCE = (
    ("long equals", f"git diff --output={OUTSIDE}/out", (f"{OUTSIDE}/out",)),
    ("spaced", f"git diff --output {OUTSIDE}/out", (f"{OUTSIDE}/out",)),
    ("global option with a value before it", f"git -C . diff --output={OUTSIDE}/out",
     (f"{OUTSIDE}/out",)),
    ("consumed config option before it", f"git -c x=1 diff --output={OUTSIDE}/out",
     (f"{OUTSIDE}/out",)),
)


# (row, command) — a word another option ate, or a flag no option of the invocation can be
# given. The run writes nothing, so the walk must name nothing: naming one is a refusal of a
# command that changes no byte (measured above).
FLAG_NOT_IN_FORCE = (
    ("config string ate the flag", f"git -c --output={OUTSIDE}/out status"),
    ("no global --output", f"git --output={OUTSIDE}/out status"),
    ("bare git, flag only", f"git --output={OUTSIDE}/out"),
)


# (row, command, what the walk names) — the measured limit of the same change: `--output`
# is the diff/log family's flag, and whether a *given* verb accepts it is a per-verb table
# this walk does not have (the enumeration it refuses to grow, #461). Measured rc=129
# `unknown option` with nothing written, so the name is a false one — but the command is
# one git refuses in the same breath, which is why this direction is left unexplained
# rather than guessed at. Pinned so no later cycle has to re-derive it.
FLAG_ON_A_VERB_THAT_HAS_NONE = (
    ("status has no --output", f"git status --output={OUTSIDE}/out", (f"{OUTSIDE}/out",)),
)


@pytest.mark.parametrize("row,cmd,named", FLAG_IN_FORCE, ids=[r for r, *_ in FLAG_IN_FORCE])
def test_the_walk_names_the_file_the_output_flag_writes(row, cmd, named) -> None:
    """The flag's value, in both spellings getopt takes, and behind a global option.

    The third and fourth rows are the control for the step: `-C .` and `-c x=1` are
    global options that take a separate value, so a reader that stepped over the wrong
    token — or over the flag itself — would lose the write here while the rows below
    stayed green.
    """
    assert tuple(_extract_write_targets(cmd)) == named, row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir=WORKSPACE)
        assert allowed is False, f"{row}: {tier} allowed a write to {OUTSIDE}/out"
        assert f"{OUTSIDE}/out" in reason, f"{row}: {tier} block names no such path: {reason}"


@pytest.mark.parametrize(
    "row,cmd", FLAG_NOT_IN_FORCE, ids=[r for r, _ in FLAG_NOT_IN_FORCE]
)
def test_a_flag_no_option_is_in_force_for_names_nothing_and_stays_allowed(row, cmd) -> None:
    """The false block itself: nothing is named, so nothing is refused.

    An exact empty tuple rather than a verdict alone, because the verdict is what the
    defect moves — a reader that names `<outside>/out` here makes both tiers refuse a
    command that writes no byte, which is the cost the issue records.
    """
    assert tuple(_extract_write_targets(cmd)) == (), row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir=WORKSPACE)
        assert allowed is True, f"{row}: {tier} refused a run that writes nothing: {reason}"


# (row, command) — the controls that keep the rows above from being satisfied by a reader
# that names nothing on principle: a git invocation that really writes, and reads that do
# not, are all read through the same walk.
SHARED_WALK_CONTROLS = (
    ("diff with no flag", "git diff", ()),
    ("status with no flag", "git status", ()),
    ("global option before the verb", "git -C . status", ()),
    ("consumed config option before the verb", "git -c x=1 status", ()),
)


@pytest.mark.parametrize(
    "row,cmd,named", SHARED_WALK_CONTROLS, ids=[r for r, *_ in SHARED_WALK_CONTROLS]
)
def test_the_walk_answers_for_the_invocations_that_have_no_flag(row, cmd, named) -> None:
    """A bare verb, and verbs behind global options that take a value, name nothing.

    These are also the rows that say the reader did not simply stop reading: `-C .` and
    `-c x=1` move the verb's position, and the row above still finds `--output` behind
    them.
    """
    assert tuple(_extract_write_targets(cmd)) == named, row
    allowed, reason, _ = _check_sandbox(cmd, "read-only", workdir=WORKSPACE)
    assert allowed is True, f"{row}: a read was refused: {reason}"


def test_the_verb_walk_is_what_keeps_the_eaten_flag_out_of_this_reader(monkeypatch) -> None:
    """Scan every token after `git` again and the false block comes back.

    The arm restores the reader's **own** walk — the second copy of the global-option
    skip that issue #1464 names as the defect — so what it kills is the sharing, not a
    table: with it, the eaten `--output=<outside>/out` is read as a flag again and the
    path after it is named. The unmutated reading is asserted first, so a later reader
    cannot make this test pass by breaking the shared walk instead.
    """
    cmd = f"git -c --output={OUTSIDE}/out status"
    assert tuple(_extract_write_targets(cmd)) == (), "not the unmutated reading"

    def the_second_copy_of_the_walk(tokens, i):
        # Master's reader, verbatim in shape: no verb is resolved, because every token
        # after `git` was a candidate flag.
        return i, tokens[i + 1] if i + 1 < len(tokens) else None, tokens[i + 1:]

    monkeypatch.setattr(bash_tool, "_git_invocation_at", the_second_copy_of_the_walk)
    assert tuple(_extract_write_targets(cmd)) == (f"{OUTSIDE}/out",), (
        "with the walk gone the eaten flag must be read again and its neighbour named — "
        "otherwise the shared walk is not what keeps it out"
    )
    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir=WORKSPACE)
    assert allowed is False and OUTSIDE in reason, (
        f"and the false block must come back with it: {reason}"
    )
    # The other half of the arm, so it cannot be read as "the reader is broken by it":
    # a flag git really honours is still found while the second copy is in place.
    assert tuple(_extract_write_targets(f"git diff --output={OUTSIDE}/out")) == (
        f"{OUTSIDE}/out",
    )


@pytest.mark.parametrize(
    "row,cmd,named",
    FLAG_ON_A_VERB_THAT_HAS_NONE,
    ids=[r for r, *_ in FLAG_ON_A_VERB_THAT_HAS_NONE],
)
def test_a_verb_that_has_no_such_flag_is_named_anyway(row, cmd, named) -> None:
    """The other direction, left measured rather than fixed.

    `--output` belongs to the diff/log family, not to every verb, so `git status
    --output=<f>` is named as if `status` wrote `<f>` — while git exits 129 without
    writing. Reading the difference would need the per-verb flag table this walk
    deliberately does not carry, and the two ways of being wrong are not equally costly:
    this one refuses a command git itself refuses, in the same breath.
    """
    assert tuple(_extract_write_targets(cmd)) == named, row
