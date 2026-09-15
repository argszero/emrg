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
"""

from __future__ import annotations

import os
import tempfile

import pytest

from emrg.tools.bash_tool import _check_sandbox

READ_ONLY = "read-only"
WW = "workspace-write"
WORKDIR = "/Users/argszero/.emrg/evolution/emrg"


WRAPPER_MUTATORS = [
    "$SHELL -c 'git checkout .'",
    "${SHELL} -c 'git checkout .'",
    "\"$SHELL\" -c 'git checkout .'",
    "env FOO=1 $SHELL -c 'git checkout .'",
    "sudo $SHELL -c 'git checkout .'",
    "$SHELL -c 'git reset --hard'",
    "$SHELL -c 'echo hi; git clean -fd'",
]

WRAPPER_WRITES = [
    "$SHELL -c 'echo hi > OUT.txt'",
    "${SHELL} -c 'echo hi > OUT.txt'",
    "$SHELL -c 'rm -rf .'",
]

WRAPPER_READS = [
    "$SHELL -c 'ls -la'",
    "$SHELL -c 'git status'",
    "${SHELL} -c 'git log -1'",
    "$SHELL -c 'echo hi > /dev/null'",
    "echo $SHELL",
    "printf '%s' $SHELL",
]

VAR_ROOT_TARGETS = [
    "echo x > $EMRG_UNSET_PROBE_VAR/repo/x.txt",
    "echo x > ${EMRG_UNSET_PROBE_VAR}/.emrg/config.toml",
    "echo x > out$EMRG_UNSET_PROBE_VAR/y.txt",
]

WW_STILL_ALLOWED = [
    "echo x > out.txt",
    "cp $SRC $DST",
    "rm $F",
    "git status",
    "$SHELL -c 'ls -la'",
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
    """A root the environment cannot supply means the guard cannot decide it."""
    allowed, reason, _enforcement = _check_sandbox(
        "echo x > $EMRG_UNSET_PROBE_VAR/x.txt", WW, WORKDIR
    )
    assert allowed is False
    assert "workspace-write sandbox" in (reason or "")
    # The same shape with a variable the environment *can* supply is decided on
    # the resolved path instead — here, the daemon's own config, which the tier
    # refuses as a protected file.
    protected, _r2, _e2 = _check_sandbox("echo x > $HOME/.emrg/config.toml", WW, WORKDIR)
    assert protected is False


def test_an_env_resolvable_variable_is_decided_like_its_literal() -> None:
    """Expansion is not a bypass: a variable and its value reach one verdict."""
    home_literal = os.path.join(os.path.expanduser("~"), "emrg-unresolved-probe.txt")
    via_var, _r1, _e1 = _check_sandbox(
        "echo x > $HOME/emrg-unresolved-probe.txt", WW, WORKDIR
    )
    via_literal, _r2, _e2 = _check_sandbox(f"echo x > {home_literal}", WW, WORKDIR)
    assert via_var == via_literal


@pytest.mark.skipif(not os.environ.get("TMPDIR"), reason="this environment sets no TMPDIR")
def test_the_temp_root_is_still_reachable_through_its_variable() -> None:
    """The temp area the tier promises stays reachable by its own spelling."""
    temp_literal = os.path.join(tempfile.gettempdir(), "emrg-probe.txt")
    via_var, _r1, _e1 = _check_sandbox("echo x > $TMPDIR/emrg-probe.txt", WW, WORKDIR)
    via_literal, _r2, _e2 = _check_sandbox(f"echo x > {temp_literal}", WW, WORKDIR)
    assert via_var == via_literal
