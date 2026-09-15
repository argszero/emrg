"""A ``#`` inside a word is not a comment, so the guard must read the whole line.

The defect these tests exist for (issue #1264, measured on master
`e9bd6d8ab003293e`, in a scratch repo holding one uncommitted edit): the guard
tokenises with ``shlex``, whose default ``commenters`` is ``'#'`` — and it drops
everything from the first ``#`` to the end of the line **wherever** that ``#``
appears. A shell does not: ``#`` starts a comment only where a **word** starts.

So for ``echo a#&& cd <dir> && git checkout .`` the shell runs *three* commands —
``echo a#``, ``cd <dir>``, ``git checkout .`` — while the lexer handed the guard
the single word ``echo a``. The guard answered ALLOW at ``read-only`` and the
hidden tail really ran: the edit was discarded. The same truncation reached every
other rule, because it decides what the rules get to read:

* the **path** rule — ``echo a#&& echo x > <outside>/out.txt`` was ALLOWED at
  ``workspace-write`` and created the file outside the workspace, and
  ``echo a#&& rm -rf <outside>/v.txt`` deleted one;
* the **wrapper** rule — ``echo a#&& $SHELL -c 'git checkout .'`` was ALLOWED.

The invariant is one-directional and that is the whole point: the guard must never
read **less** of the line than the shell executes, because reading less hides a
mutator and work is lost. Reading *more* cannot hide one — the over-read text is a
comment the shell ignores — so the worst case is refusing a command that would have
done nothing, which is the fail-closed side this guard already picks for input it
cannot parse.

Both directions are asserted, because a change that refused everything would
otherwise pass this file: an ordinary trailing comment stays allowed, a ``#``
inside quotes is data (it was already one token — quoting is resolved before the
comment rule either way), a hidden write *inside* the workspace stays allowed, and
the same chains **without** the ``#`` are asserted to block, so the difference the
tests measure is the ``#`` itself rather than a blanket change.
"""

from __future__ import annotations

import os

import pytest

from emrg.tools.bash_tool import (
    _check_sandbox,
    _split_command_tokens,
    _tokenize_command,
)

READ_ONLY = "read-only"
WW = "workspace-write"
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# An absolute path outside the workspace, spelled with **forward slashes on both
# platforms**. A `C:\…` spelling would make these verdicts depend on issue #1261
# rather than on the comment rule this file is about: a backslash is shlex's escape
# character in the guard's POSIX reading, so `C:\Users\x\f.txt` reaches the guard as
# `C:Usersxf.txt` — a *relative* name, therefore "inside the workspace", therefore
# allowed. `/emrg-hash-probe` is rooted and drive-less, which `_is_absolute_path`
# accepts on POSIX and Windows alike (that mis-spelling is what the Windows CI leg
# caught here: 4 failures, every one of them this path, including its own un-hidden
# control — the control failing is what identified the spelling, not the fix).
OUTSIDE = "/emrg-hash-probe"

# The hidden tails: in every one the `#` sits *inside* the word `a#`, so the shell
# runs what follows it and the truncated reading never saw it.
HIDDEN_MUTATORS = [
    "echo a#&& git checkout .",
    "echo a#; git checkout .",
    "echo a#&& git stash drop",
    "echo a#&& git reset --hard",
    "echo a#&& $SHELL -c 'git checkout .'",
]

# The same chains with the `#` removed. Derived, never hand-listed: a hand-written
# control set can drift from the cases it is supposed to control for.
UNHIDDEN_CONTROLS = [cmd.replace("a#", "a") for cmd in HIDDEN_MUTATORS]

HIDDEN_OUTSIDE_WRITES = [
    f"echo a#&& echo x > {OUTSIDE}/out.txt",
    f"echo a#&& rm -rf {OUTSIDE}/v.txt",
]


def verdict(cmd: str, mode: str) -> bool:
    """The guard's answer, through its own entry point in the tier asked about."""
    allowed, reason, _enforcement = _check_sandbox(cmd, mode, WORKDIR)
    return allowed, reason


def test_the_reader_no_longer_stops_at_a_mid_word_hash():
    """The reader, not the rule: the hidden tail must be in the token stream.

    Asserted on **both** tokenizers, because both feed rules — the write-target
    walk uses one and the git-mutator walk the other, and a fix applied to one of
    them leaves the other's rules reading a prefix.
    """
    for split in (_split_command_tokens, _tokenize_command):
        tokens = split("echo a#&& git checkout .")
        assert tokens[1] == "a#", tokens
        assert tokens[-3:] == ["git", "checkout", "."], tokens


def test_a_quoted_hash_is_still_data_in_one_token():
    """`echo "a # b"` is one argument: quoting is resolved before the comment rule."""
    for split in (_split_command_tokens, _tokenize_command):
        assert split('echo "a # b"') == ["echo", "a # b"]


@pytest.mark.parametrize("cmd", HIDDEN_MUTATORS)
def test_a_mutator_hidden_behind_a_mid_word_hash_is_blocked(cmd: str):
    allowed, reason = verdict(cmd, READ_ONLY)
    assert not allowed, f"{cmd!r} was allowed: {reason}"


@pytest.mark.parametrize("cmd", UNHIDDEN_CONTROLS)
def test_the_same_chain_without_the_hash_was_already_blocked(cmd: str):
    """The control: this is what makes the parametrised case above meaningful.

    These blocked on master too, so the flip is the ``#`` being read rather than a
    new blanket refusal — if the fix had been "block every command mentioning a
    separator", these tests would still pass while the ones above proved nothing.
    """
    allowed, reason = verdict(cmd, READ_ONLY)
    assert not allowed, f"{cmd!r} was allowed: {reason}"


@pytest.mark.parametrize("cmd", HIDDEN_OUTSIDE_WRITES)
def test_a_hidden_tail_cannot_write_outside_the_workspace(cmd: str):
    allowed, reason = verdict(cmd, WW)
    assert not allowed, f"{cmd!r} was allowed: {reason}"


@pytest.mark.parametrize("cmd", HIDDEN_OUTSIDE_WRITES)
def test_the_same_outside_write_without_the_hash_is_blocked_too(cmd: str):
    allowed, reason = verdict(cmd.replace("a#", "a"), WW)
    assert not allowed, f"{cmd!r} was allowed: {reason}"


def test_a_hidden_write_inside_the_workspace_is_still_allowed():
    """The boundary refuses the *escape*, not the verb — no blanket refusal."""
    allowed, reason = verdict("echo a#&& echo x > in.txt", WW)
    assert allowed, reason


@pytest.mark.parametrize(
    "cmd",
    [
        "git status # checking",
        "ls -la # trailing note",
        "echo hi # just a note",
    ],
)
def test_a_benign_trailing_comment_still_runs(cmd: str):
    allowed, reason = verdict(cmd, READ_ONLY)
    assert allowed, reason


def test_a_comment_whose_text_chains_is_refused_fail_closed():
    """The stated cost of reading the whole line, pinned so it cannot change silently.

    Telling this comment from code means re-deriving the shell's word-start rule —
    a second parser, which is exactly where a hole would come from. So the reader
    errs toward reading more, and this command is refused even though the shell
    would run only ``ls``.
    """
    allowed, reason = verdict("ls # ; git checkout .", READ_ONLY)
    assert not allowed, reason
