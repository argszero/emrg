"""The prefixes that exec the word after them, and the payload they hand it.

`_runs_as_a_command` answers "will the shell run this word?" by walking left to
the nearest token that opens a command context, and `_COMMAND_WRAPPERS` is the
list of prefixes whose *argument* is that command (`env`, `sudo`, `xargs`,
`builtin`, …). Four prefixes that exec the word after them were missing, so the
word was read as an argument and neither payload reader saw a mutator at all.
Measured on master `9a7bfe65` through `_check_sandbox` at `read-only`, one
`workdir`, nothing executed:

    unshare -r git checkout .      ALLOW  targets=[]
    nsenter -t 1 git checkout .    ALLOW  targets=[]
    chroot / git checkout .        ALLOW  targets=[]
    unshare -r rm -rf /tmp/x       ALLOW  targets=[]
    nsenter -t 1 rm -rf /tmp/x     ALLOW  targets=[]
    chroot / rm -rf /tmp/x         ALLOW  targets=[]
    busybox rm -rf /tmp/x          ALLOW  targets=[]

`unshare PROGRAM`, `nsenter … PROGRAM`, `chroot NEWROOT COMMAND` and
`busybox APPLET` all run the word after the prefix, so those verdicts are the
`builtin cd <outside>` hole (#1362) one prefix further out, and the fix is the
one that hole got: name the prefix. (The semantics are util-linux's and
busybox's documented behaviour, read rather than executed — no `unshare`,
`nsenter`, `chroot` or `busybox` binary exists on the host this was measured
on, and none of these rows is executed here either.)

The second half is a fence for issue #1513. The named-wrapper branch of
`_nested_command_texts` takes `tokens[i + 1:]` with no position test, which is
what reads these payloads today; a position test landing there — the fix #1513
proposes, and the right one for the *mention* shapes it enumerates — would stop
reading every payload behind a prefix that is not in `_COMMAND_WRAPPERS`, these
four among them. So the rows are asserted refused here, where the change that
opens them has to look.

Both directions are asserted, as every guard test in this repo is: a change that
refused everything must not pass either, so the reads behind the same prefixes
and the quoted mentions stay allowed.
"""

import pytest

from emrg.tools.bash_tool import (
    _check_sandbox,
    _extract_write_targets,
    _find_git_mutator,
)

# A git mutator the prefix really runs: the word after the prefix is `argv[0]`.
GIT_MUTATORS = [
    "unshare -r git checkout .",
    "nsenter -t 1 git checkout .",
    "chroot / git checkout .",
    "busybox git stash drop",
]

# A write verb the prefix really runs.
WRITE_MUTATORS = [
    "unshare -r rm -rf /tmp/x",
    "nsenter -t 1 rm -rf /tmp/x",
    "chroot / rm -rf /tmp/x",
    "busybox rm -rf /tmp/x",
    "unshare -r touch /tmp/x",
    "busybox patch /tmp/x",
]

# The same prefixes carrying a shell wrapper whose payload is the mutator.
SHELL_PAYLOADS = [
    'busybox sh -c "rm -rf /tmp/x"',
    'unshare -r sh -c "rm -rf /tmp/x"',
    'nsenter -t 1 sh -c "rm -rf /tmp/x"',
    'chroot / sh -c "rm -rf /tmp/x"',
    'chroot / /bin/sh -c "rm -rf /tmp/x"',
    'busybox bash -c "git checkout ."',
]

# The complement: the same prefixes with a read, or naming the word as data.
READS = [
    "busybox ls -l",
    "chroot / ls",
    "unshare -r git status",
    "nsenter -t 1 cat f.txt",
    "grep -n unshare f.txt",
    "echo unshare",
    'echo "unshare -r rm -rf /tmp/x"',
    # A flag's value is skipped, so this one is still the wrapper's argument.
    "unshare --map-root-user git status",
]


def _reads(cmd: str) -> bool:
    allowed, _reason, _enforcement = _check_sandbox(cmd, "read-only")
    return allowed


@pytest.mark.parametrize("cmd", GIT_MUTATORS)
def test_a_git_mutator_behind_an_exec_prefix_is_seen(cmd):
    assert _reads(cmd) is False, f"{cmd!r} runs the mutator"
    assert _find_git_mutator(cmd), cmd


@pytest.mark.parametrize("cmd", WRITE_MUTATORS)
def test_a_write_behind_an_exec_prefix_names_its_target(cmd):
    assert _reads(cmd) is False, f"{cmd!r} runs the writer"
    assert _extract_write_targets(cmd), cmd


@pytest.mark.parametrize("cmd", SHELL_PAYLOADS)
def test_a_shell_payload_behind_an_exec_prefix_is_read(cmd):
    assert _reads(cmd) is False, f"{cmd!r} hands the payload to the shell"
    assert _extract_write_targets(cmd) or _find_git_mutator(cmd), cmd


@pytest.mark.parametrize("cmd", READS)
def test_a_read_behind_an_exec_prefix_stays_allowed(cmd):
    assert _reads(cmd) is True, f"{cmd!r} reads and writes nothing"
