"""A program word the guard cannot resolve is judged as code, not as data.

The defect these tests exist for (issue #1244, measured on master `7e7cd598`,
read-only tier): ``$SHELL -c 'git checkout .'`` answered ALLOW and the shell ran
it, discarding a tracked file's uncommitted edit. Five spellings did so —
``$SHELL``, ``${SHELL}``, ``"$SHELL"``, ``env FOO=1 $SHELL``, ``sudo $SHELL`` —
while their named twins (``sh -c 'git checkout .'``) blocked. The same hole
swallowed a redirect: ``$SHELL -c 'echo hi > OUT.txt'`` created the file.

The mechanism was that the wrapper walk recursed only into a token whose
*basename* is a shell's name, and the basename of ``$SHELL`` is ``$SHELL``. The
fix reads a token whose command word is a variable reference as a possible
wrapper, and — for an un-resolvable wrapper only — scans its payload for write
targets too, because a redirect inside a quoted payload is a character rather
than an operator.

Both directions are asserted, because a change that blocked everything would
otherwise pass half of this file: the same wrapper around a read stays allowed,
``/dev/null`` keeps its exemption, and the workspace-write tier's boundaries
(relative names, the temp root, a bare ``$VAR`` operand) are unchanged. Every
case is driven through ``_check_sandbox`` — the guard's own entry point, in the
tier where the question is asked.

**The class, not the spelling** (measured 2026-09-16 on master `cca0b8dc`). The
first fix named two spellings of the word — ``$SHELL`` and ``${SHELL}`` — and a
parameter expansion is a *class* with more than two:

    ``$0``  ``${SHELL:?}``  ``${SHELL:-sh}``  ``${SHELL//x/y}``  ``${A}${B}``

Four of those, each driven end to end through ``BashTool.execute`` against a
scratch repo holding one uncommitted edit, answered ALLOW at ``read-only`` and
**discarded the edit**, while ``$SHELL -c 'git checkout .'`` blocked on the same
mutator. The class reached the *path* rule too: ``echo x > ${VAR:?}/escaped.txt``
answered ALLOW at ``workspace-write`` and created the file outside the workspace.
That is the #461 defect again — matching one spelling of a class while the others
pass — so the corpus below is built from the class rather than hand-listed.

``${SHELL//x/y}`` is also why the rule matches the token as written and not only
its basename: a ``/`` inside ``${…}`` is not a directory separator, so
``_basename("${SHELL//x/y}")`` is ``y}`` and the expansion is cut in half.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from emrg.tools.bash_tool import (
    _basename,
    _check_sandbox,
    _is_absolute_path,
    _is_within,
    _split_command_tokens,
    _temp_write_roots,
    _tokenize_command,
    _trusted_write_zones,
    _unresolved_wrapper_payloads,
)

READ_ONLY = "read-only"
WW = "workspace-write"
WORKDIR = "/Users/argszero/.emrg/evolution/emrg"

# The class, listed once so the corpus below is built *from it* rather than from a
# hand-written set of whole commands — a hand-enumerated axis is a hidden
# assumption, and assuming this axis is exactly the defect these tests exist for.
# Every entry is a shell word that expands to the interpreter on some host, and
# `os.path.expandvars` resolves none of them, so each is a wrapper the guard
# cannot place.
PARAMETER_EXPANSION_SPELLINGS = [
    "$SHELL",
    "${SHELL}",
    '"$SHELL"',
    "$0",
    "${SHELL:?}",
    "${SHELL:-sh}",
    "${SHELL//x/y}",
    "${SHELL}${SUFFIX}",
]


WRAPPER_MUTATORS = [
    "$SHELL -c 'git checkout .'",
    "${SHELL} -c 'git checkout .'",
    "\"$SHELL\" -c 'git checkout .'",
    "env FOO=1 $SHELL -c 'git checkout .'",
    "sudo $SHELL -c 'git checkout .'",
    "$SHELL -c 'git reset --hard'",
    "$SHELL -c 'echo hi; git clean -fd'",
    # The rest of the class (issue #1244 residual, measured 2026-09-16 on master
    # `cca0b8dc`): each of these answered ALLOW and discarded the edit, driven end
    # to end, while the `$SHELL` arms above blocked on the same payload.
    "$0 -c 'git checkout .'",
    "${SHELL:?} -c 'git checkout .'",
    "${SHELL:-sh} -c 'git checkout .'",
    "${SHELL//x/y} -c 'git checkout .'",
    "${SHELL}${SUFFIX} -c 'git checkout .'",
]

WRAPPER_WRITES = [
    "$SHELL -c 'echo hi > OUT.txt'",
    "${SHELL} -c 'echo hi > OUT.txt'",
    "$SHELL -c 'rm -rf .'",
    "$0 -c 'echo hi > OUT.txt'",
    "${SHELL:?} -c 'rm -rf .'",
]

WRAPPER_READS = [
    "$SHELL -c 'ls -la'",
    "$SHELL -c 'git status'",
    "${SHELL} -c 'git log -1'",
    "$SHELL -c 'echo hi > /dev/null'",
    "echo $SHELL",
    "printf '%s' $SHELL",
    # A read behind every spelling of the class must stay readable.
    "$0 -c 'ls -la'",
    "${SHELL:?} -c 'git status'",
    "${SHELL:-sh} -c 'git log -1'",
    "${SHELL//x/y} -c 'ls -la'",
    "echo $0",
    "echo ${SHELL:-sh}",
]

VAR_ROOT_TARGETS = [
    "echo x > $EMRG_UNSET_PROBE_VAR/repo/x.txt",
    "echo x > ${EMRG_UNSET_PROBE_VAR}/.emrg/config.toml",
    "echo x > out$EMRG_UNSET_PROBE_VAR/y.txt",
    # The rest of the class: an operator inside the braces (`expandvars` leaves
    # these alone, so the guard sees no resolvable root), and a substitution whose
    # `/` must not be read as a separator.
    "echo x > ${EMRG_UNSET_PROBE_VAR:?}/repo/x.txt",
    "echo x > ${EMRG_UNSET_PROBE_VAR:-/nowhere}/repo/x.txt",
    "echo x > ${EMRG_UNSET_PROBE_VAR//x/y}/repo/x.txt",
]

WW_STILL_ALLOWED = [
    "echo x > out.txt",
    "cp $SRC $DST",
    "cp ${SRC} ${DST}",
    "rm $F",
    "git status",
    "$SHELL -c 'ls -la'",
    "${SHELL//x/y} -c 'ls -la'",
    "echo $0",
    "echo ${SHELL:-sh}",
]


@pytest.mark.parametrize("cmd", WRAPPER_MUTATORS)
def test_a_mutator_behind_an_unresolved_wrapper_is_blocked(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert not allowed
    # The reason names the mutator, not the wrapper: the point is that the guard
    # read the payload, and a block for some unrelated reason would be a
    # different (and wrong) explanation for the same verdict.
    assert "git" in (reason or "")


@pytest.mark.parametrize("cmd", WRAPPER_WRITES)
def test_a_write_behind_an_unresolved_wrapper_is_blocked(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert not allowed
    assert "read-only sandbox" in (reason or "")


@pytest.mark.parametrize("cmd", WRAPPER_READS)
def test_the_same_wrapper_around_a_read_stays_allowed(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert allowed, reason


@pytest.mark.parametrize("cmd", VAR_ROOT_TARGETS)
def test_a_target_whose_root_is_unresolved_is_blocked(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, WW, WORKDIR)
    assert not allowed
    assert "workspace-write sandbox" in (reason or "")


@pytest.mark.parametrize("cmd", WW_STILL_ALLOWED)
def test_workspace_write_keeps_its_boundaries(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, WW, WORKDIR)
    assert allowed, reason


# Issue #1467: the same variable reference standing in **operand** position. The
# words behind it are that command's arguments, not a wrapper's payload — and
# reading them as one turned data into a command. Measured on master `c1a70c94`,
# every row below answered BLOCK while writing nothing, naming `rc=$?`: the
# argument token `patch rc=$?` was re-tokenized into the words `patch` and
# `rc=$?`, and `patch` is a write verb. A refusal aborts the whole compound
# command, so the reads sharing the call are lost with it.
OPERAND_POSITION_VARIABLES = [
    'wc -c "$F" && echo "patch rc=$?"',
    'F=/tmp/x && wc -c "$F" && echo "patch rc=$?"',
    'ls "$HOME" && echo "patch rc=$?"',
    'echo $SHELL && echo "patch rc=$?"',
    'printf %s $0 && echo "patch rc=$?"',
    'cat "$F" | echo "patch rc=$?"',
    'stat "$F"; echo "patch rc=$?"',
]

# The narrowing must not touch the class it was written for: a wrapper that
# shares a command with an operand-position variable is still read.
WRAPPER_WITH_AN_OPERAND_POSITION_VARIABLE = [
    'wc -c "$F" && $SHELL -c \'echo x > OUT.txt\'',
    'ls "$HOME" && ${SHELL//x/y} -c \'git checkout .\'',
]


@pytest.mark.parametrize("cmd", OPERAND_POSITION_VARIABLES)
def test_a_variable_in_operand_position_is_not_a_wrapper(cmd: str) -> None:
    """Both tiers: the false block was in the read-only arm, the rule is one rule.

    The assertion is the verdict *and* its reason — a command that writes nothing
    must not be refused for naming a target it never writes.
    """
    for tier in (READ_ONLY, WW):
        allowed, reason, _enforcement = _check_sandbox(cmd, tier, WORKDIR)
        assert allowed, (cmd, tier, reason)


@pytest.mark.parametrize("cmd", WRAPPER_WITH_AN_OPERAND_POSITION_VARIABLE)
def test_a_wrapper_beside_an_operand_position_variable_is_still_read(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert not allowed, (cmd, reason)


# Issue #1492: the wrapper's own **arguments**. `_runs_as_a_command` answers the
# position question for the wrapper *word*; the same question is owed to the words
# behind it. A word that follows a flag is that flag's value — text the program
# reads — and a word anywhere else is one argument the wrapper consumes. Measured
# on master `a38fd0d`, through `_check_sandbox` at the read-only tier, every row
# below answered BLOCK while writing nothing, naming `rc=$?`: the argument token
# `patch rc=$?` was re-tokenized into the words `patch` and `rc=$?`, and `patch`
# is a write verb.
WRAPPER_ARGUMENTS_ARE_DATA = [
    '$SHELL "patch rc=$?"',
    "$SHELL -c 'ls' \"patch rc=$?\"",
    '${SHELL} "patch rc=$?"',
    '$SHELL x "echo hi > OUT.txt"',
    "$SHELL -c 'ls' \"rm -rf /tmp/x\"",
]

# The rule matches the *flag*, not `-c`: naming the flag that takes code is the
# #461 enumeration `_nested_command_texts` refuses (its docstring carries the
# measurement — a long option or an option value ended the walk before `-c` was
# reached, and 9 of 14 named-wrapper shapes went ALLOW). These are the shapes that
# killed that version, driven with an un-resolvable wrapper.
STILL_CODE_AFTER_A_LONG_OPTION = [
    "$SHELL --command 'git checkout .'",
    "$SHELL --login -c 'git checkout .'",
    "$SHELL -o pipefail -c 'git checkout .'",
]


@pytest.mark.parametrize("cmd", WRAPPER_ARGUMENTS_ARE_DATA)
def test_an_argument_of_an_unresolved_wrapper_is_data(cmd: str) -> None:
    """Both tiers: a refusal here aborts the reads sharing the compound command."""
    for tier in (READ_ONLY, WW):
        allowed, reason, _enforcement = _check_sandbox(cmd, tier, WORKDIR)
        assert allowed, (cmd, tier, reason)


@pytest.mark.parametrize("cmd", STILL_CODE_AFTER_A_LONG_OPTION)
def test_the_flag_rule_does_not_lose_the_shapes_the_walk_to_c_lost(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert not allowed, (cmd, reason)
    assert "git" in (reason or ""), (cmd, reason)


# Issue #1523: the argument rule described one position that carries text and
# there are two. A redirection operator is not a flag, and a shell with no `-c`
# reads its **program** from stdin — so `sh <<< 'git checkout .'` runs the
# mutator. The flag-only test read these as operands and so came back with an
# empty payload, which released exactly the rows master `5ff1db1` blocked:
# measured on this host, every row below answered BLOCK on master and ALLOW at
# the head before this repair.
STDIN_FEEDING_MUTATORS = [
    "$SHELL <<< 'git checkout .'",
    '$SHELL <<< "git checkout ."',
    "$SHELL 0<<< 'git checkout .'",
    "$SHELL <<< 'git stash drop'",
]

# The same position carrying a write: the operand rule would have read it as a
# word the wrapper consumes, so the target rule is the only reader that sees it.
# The destination must be outside *every* allowed root or the row measures
# nothing, and `/tmp` is not: on the Linux runner it **is** the OS temp root, so
# the workspace-write tier allowed that write for the temp-root reason and the
# row was red there while it passed here (measured 2026-09-21, run 35599007744).
# ``OUTSIDE`` is a name no platform's ``tempfile.gettempdir()`` returns, and the
# premise below asserts that rather than assuming it.
OUTSIDE = "/var/tmp/emrg-1523-outside"

STDIN_FEEDING_A_WRITE = [
    f"$SHELL <<< 'rm -rf {OUTSIDE}'",
]

# The control that keeps the widening honest: the same position, a program that
# reads. A rule that blocked these would be refusing the read to reach the write.
STDIN_CARRYING_A_READ = [
    "$SHELL <<< 'git status'",
    "$SHELL 0<<< 'ls -la'",
]


@pytest.mark.parametrize("cmd", STDIN_FEEDING_MUTATORS)
def test_a_redirection_that_feeds_the_wrapper_a_program_is_read(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert not allowed, (cmd, reason)
    # The reason names the payload, not the wrapper: a block for some unrelated
    # reason would be a different explanation for the same verdict.
    assert "git" in (reason or ""), (cmd, reason)


def test_premise_the_write_destination_is_outside_every_allowed_root() -> None:
    """The write row only measures the payload reader if ``OUTSIDE`` is really out.

    Without this, the row silently degrades to a measurement of the host's temp
    root instead of of the payload reader — which is exactly how it went red on
    the Linux runner and green here.
    """
    assert _is_absolute_path(OUTSIDE)
    assert not _is_within(OUTSIDE, WORKDIR)
    for root in (*_temp_write_roots(), *_trusted_write_zones()):
        assert not _is_within(OUTSIDE, root), root


@pytest.mark.parametrize("cmd", STDIN_FEEDING_A_WRITE)
def test_a_here_string_can_carry_the_write_too(cmd: str) -> None:
    """Blocked at both tiers: at read-only as a write, at workspace-write as one
    landing outside the workspace — the destination is ``OUTSIDE``, which the
    premise above pins as outside every allowed root."""
    for tier in (READ_ONLY, WW):
        allowed, reason, _enforcement = _check_sandbox(cmd, tier, WORKDIR)
        assert not allowed, (cmd, tier, reason)
        # The reason names the payload's destination: a block for some unrelated
        # reason would be a different explanation for the same verdict.
        assert "emrg-1523" in (reason or ""), (cmd, tier, reason)


@pytest.mark.parametrize("cmd", STDIN_CARRYING_A_READ)
def test_a_redirection_that_feeds_the_wrapper_a_read_is_allowed(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert allowed, (cmd, reason)


# The residual of #1523, taken one spelling in — and the reason the rule reads the
# substitution rather than the word after the operator. `<<<$(echo …)` tokenizes to
# `['<<<', '$', '(', 'echo', …]`: the operator and its operand are separate tokens
# when the operand opens a substitution, so a rule that hands over the word after
# the operator hands over `$` and leaves the program text behind. Master's blanket
# collection covered these incidentally, so this is the fail-open direction — the
# one this walk must never move in. Measured on master `1f2feef` and on this
# branch's head `8b160d82` before the repair, both tiers, through `_check_sandbox`:
# every mutator row below answered BLOCK on master and ALLOW at the head.
SUBSTITUTION_FEEDING_MUTATORS = [
    "$SHELL <<<$(echo 'git checkout .')",
    "$SHELL <<<$(printf %s 'git checkout .')",
    "$SHELL < <(echo 'git checkout .')",
    "$SHELL < <(printf %s 'git checkout .')",
    "$SHELL <<$(echo 'git checkout .')",
    "$SHELL 2< <(echo 'git checkout .')",
    "$SHELL <<<$(echo 'git checkout .') x",
    "echo hi && $SHELL <<<$(echo 'git checkout .')",
]

# The write, through the same position: named by the target rule, refused at both
# tiers because `OUTSIDE` is outside every allowed root (the premise below pins
# that for the here-string rows, and this is the same destination).
SUBSTITUTION_FEEDING_A_WRITE = [
    f"$SHELL <<<$(echo 'rm -rf {OUTSIDE}')",
    f"$SHELL < <(printf %s 'rm -rf {OUTSIDE}')",
]

# The control the hand-over is admitted against: the same position, substitutions
# that read. A rule that blocked these would be refusing the read to reach the write.
SUBSTITUTION_CARRYING_A_READ = [
    "$SHELL <<<$(echo hi)",
    "$SHELL <<<$(date)",
    "$SHELL < <(echo hi)",
]


@pytest.mark.parametrize("cmd", SUBSTITUTION_FEEDING_MUTATORS)
def test_a_redirection_whose_operand_is_a_substitution_is_read(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert not allowed, (cmd, reason)
    assert "git" in (reason or ""), (cmd, reason)


@pytest.mark.parametrize("cmd", SUBSTITUTION_FEEDING_A_WRITE)
def test_a_substituted_here_string_can_carry_the_write_too(cmd: str) -> None:
    for tier in (READ_ONLY, WW):
        allowed, reason, _enforcement = _check_sandbox(cmd, tier, WORKDIR)
        assert not allowed, (cmd, tier, reason)
        assert "emrg-1523" in (reason or ""), (cmd, tier, reason)


@pytest.mark.parametrize("cmd", SUBSTITUTION_CARRYING_A_READ)
def test_a_redirection_whose_operand_substitutes_a_read_is_allowed(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert allowed, (cmd, reason)


def test_the_hand_over_is_the_substitution_not_the_token_at_the_operator() -> None:
    """The mechanism, not the verdicts above: the words handed to the reader.

    A verdict test cannot see the difference between handing over `$` and handing
    over the substitution — both can produce a block for the right reason on some
    row — so this pins the words the collector hands the readers
    (`_unresolved_wrapper_payloads`, the caller of `_payload_code_words`). It is
    also the shape a tokenizer change would move while the verdicts above stayed
    green.
    """
    for cmd, program in (
        ("$SHELL <<<$(echo 'git checkout .')", "git checkout ."),
        ("$SHELL < <(printf %s 'rm -rf /x')", "rm -rf /x"),
        ("$SHELL <<<$(echo hi)", "hi"),
    ):
        words = _unresolved_wrapper_payloads(_tokenize_command(cmd))
        assert program in words, (cmd, words)


def test_the_named_wrapper_is_the_control() -> None:
    """The hole was the spelling, so the named twin must reach the same verdict."""
    named, _r1, _e1 = _check_sandbox("sh -c 'git checkout .'", READ_ONLY, WORKDIR)
    unresolved, _r2, _e2 = _check_sandbox("$SHELL -c 'git checkout .'", READ_ONLY, WORKDIR)
    assert named is False
    assert unresolved is False


def test_a_wrapper_chain_terminates() -> None:
    """Nesting is bounded by the shell and by the guard: it must not spin."""
    cmd = "$A -c " * 12 + "'git checkout .'"
    allowed, _reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert not allowed


def test_a_wrapper_chain_of_writes_terminates() -> None:
    cmd = "$A -c " * 12 + "'echo hi > OUT.txt'"
    allowed, _reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert not allowed


def test_an_unresolvable_variable_root_is_blocked() -> None:
    """A root the environment cannot supply means the guard cannot decide it.

    The premise is asserted rather than assumed: the variable must really be
    absent from the guard's environment, or this test would be measuring a
    different rule.
    """
    assert "EMRG_UNSET_PROBE_VAR" not in os.environ
    allowed, reason, _enforcement = _check_sandbox(
        "echo x > $EMRG_UNSET_PROBE_VAR/x.txt", WW, WORKDIR
    )
    assert allowed is False
    assert "workspace-write sandbox" in (reason or "")


def test_a_resolvable_variable_root_is_decided_after_expansion(monkeypatch, tmp_path) -> None:
    """Expansion happens first, so the variable spelling is not a bypass.

    The literal arm is built from the guard's *own* expansion of the same string
    rather than from `~`, because the two are only interchangeable where the
    environment happens to define both — an earlier version of this test compared
    `$HOME/...` with `os.path.expanduser("~")/...` and failed on a Windows runner
    that sets neither, which was a defect in the test, not in the guard.
    """
    value = str(tmp_path)
    assert _is_absolute_path(value), "the premise of this comparison is an absolute path"
    monkeypatch.setenv("EMRG_PROBE_ROOT", value)
    raw = "echo x > $EMRG_PROBE_ROOT/out.txt"
    literal = f"echo x > {os.path.expandvars('$EMRG_PROBE_ROOT/out.txt')}"
    via_var, _r1, _e1 = _check_sandbox(raw, WW, WORKDIR)
    via_literal, _r2, _e2 = _check_sandbox(literal, WW, WORKDIR)
    assert via_var == via_literal


def test_a_variable_can_reach_the_daemons_own_config(monkeypatch) -> None:
    """The resolved path — not the spelling — is what the tier decides on."""
    home = os.path.expanduser("~")
    if not _is_absolute_path(home):
        pytest.skip("this environment has no resolvable home directory")
    monkeypatch.setenv("EMRG_PROBE_HOME", home)
    allowed, reason, _enforcement = _check_sandbox(
        "echo x > $EMRG_PROBE_HOME/.emrg/config.toml", WW, WORKDIR
    )
    assert allowed is False
    assert "workspace-write sandbox" in (reason or "")


def test_the_temp_root_is_still_reachable_through_its_variable(monkeypatch) -> None:
    """The temp area the tier promises stays reachable by its own spelling.

    Same construction as the test above — the literal arm is the guard's own
    expansion — and the one positive claim is made only after checking that the
    temp directory really is a permitted root on this host.
    """
    monkeypatch.setenv("EMRG_PROBE_TMP", tempfile.gettempdir())
    raw = "echo x > $EMRG_PROBE_TMP/emrg-probe.txt"
    literal = f"echo x > {os.path.expandvars('$EMRG_PROBE_TMP/emrg-probe.txt')}"
    via_var, _r1, _e1 = _check_sandbox(raw, WW, WORKDIR)
    via_literal, _r2, _e2 = _check_sandbox(literal, WW, WORKDIR)
    assert via_var == via_literal
    real = os.path.realpath(tempfile.gettempdir())
    if any(_is_within(real, root) or real == root for root in _temp_write_roots()):
        assert via_var is True


@pytest.mark.parametrize("spelling", PARAMETER_EXPANSION_SPELLINGS)
@pytest.mark.parametrize(
    "payload", ["git checkout .", "git reset --hard", "git clean -fd", "git stash drop"]
)
def test_every_spelling_of_the_class_reads_its_payload(spelling: str, payload: str) -> None:
    """The class is the corpus: no spelling may slip a mutator past the tier.

    Each pair is composed here rather than written out, so adding a spelling to
    ``PARAMETER_EXPANSION_SPELLINGS`` extends the coverage instead of leaving a
    forgotten combination — the failure mode of a hand-listed table.
    """
    cmd = f"{spelling} -c '{payload}'"
    allowed, reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert not allowed, f"{cmd} was allowed at {READ_ONLY}"
    assert "git" in (reason or ""), (cmd, reason)


@pytest.mark.parametrize("spelling", PARAMETER_EXPANSION_SPELLINGS)
def test_no_spelling_of_the_class_blocks_a_read(spelling: str) -> None:
    """The other direction, driven over the same axis."""
    cmd = f"{spelling} -c 'ls -la'"
    allowed, reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert allowed, (cmd, reason)


def test_a_slash_inside_an_expansion_is_not_a_directory_separator() -> None:
    """The premise of matching the token as written, asserted rather than assumed.

    ``/`` is a separator inside a *path*, but inside ``${…}`` it is part of the
    expansion's operator — so a basename test cuts the word in half and reads
    neither half as a wrapper. The measurement is the assertion: if a future
    tokenizer change makes the basename equal to the token, this fails and the
    second arm of the rule can be reconsidered rather than kept by habit.
    """
    tok = _split_command_tokens("${SHELL//x/y} -c 'git checkout .'")[0]
    assert tok == "${SHELL//x/y}"
    assert _basename(tok) == "y}", "the basename truncated the expansion at its first /"
    allowed, reason, _enforcement = _check_sandbox(
        "${SHELL//x/y} -c 'git checkout .'", READ_ONLY, WORKDIR
    )
    assert not allowed, reason


@pytest.mark.parametrize("cmd", ["sh -c'git checkout .'", "$SHELL -c'git checkout .'"])
def test_a_fused_flag_is_latent_not_live(cmd: str) -> None:
    """ALLOW is the correct verdict here, and this records the measurement.

    `sh -c'git checkout .'` looks like the same hole as `sh -c 'git checkout .'`
    and is not one: the quoting makes ``-cgit checkout .`` **a single word**, which
    a shell reads as an option cluster rather than as `-c` plus a command string.
    Measured 2026-09-16 with `sh` (bash as sh), `bash`, `zsh`, `dash` and `ksh`:
    ``sh -c'echo ALIVE'`` exits 1-2 with an invalid-option message and never prints
    ALIVE, and the payload does not run end to end. So the guard's ALLOW agrees
    with the shell. A future cycle that wants to over-approximate it belongs here
    *with its own measurement* — not with a claim that master loses data this way.
    """
    tokens = _split_command_tokens(cmd)
    assert len(tokens) == 2, f"the flag and its text must be one token: {tokens}"
    assert tokens[1].startswith("-c"), tokens
    allowed, _reason, _enforcement = _check_sandbox(cmd, READ_ONLY, WORKDIR)
    assert allowed
