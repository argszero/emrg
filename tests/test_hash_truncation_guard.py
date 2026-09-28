"""A ``#`` inside a word is not a comment, so the guard must read the whole line.

The defect these tests exist for (issue #1264, measured on master
`e9bd6d8ab003293e`): the guard tokenises with ``shlex``, whose default
``commenters`` is ``'#'`` — and it drops everything from the first ``#`` to the
end of the line **wherever** that ``#`` appears. A shell does not: ``#`` starts a
comment only where a **word** starts.

So for ``echo a#&& cd <dir> && git checkout .`` the shell runs *three* commands —
``echo a#``, ``cd <dir>``, ``git checkout .`` — while the lexer handed the guard
the single word ``echo a``. The guard answered ALLOW and the hidden tail really
ran: an uncommitted edit was discarded. The truncation reached every rule,
because it decides what the rules get to read.

The invariant is one-directional and that is the whole point: the guard must never
read **less** of the line than the shell executes, because reading less hides a
command and work is lost. Reading *more* cannot hide one — the over-read text is a
comment the shell ignores — so the worst case is refusing a command that would have
done nothing, which is the fail-closed side this guard already picks for input it
cannot parse.

P7 changed the writer, not the defect. The writer was a git mutator, refused by
the read-only tier's static verb scan — a scan that dies with
``emrg/tools/bash_tool.py``, because the v2 boundary makes the tier an OS fence
rather than a word list (design ``bash-tool-v2-design.md`` §2.2/§8). The lexer
survives in ``emrg/tools/command_scan.py`` and is upstream of the two rules that
still read a command line: a command that stops or restarts the daemon (issue
#1324) and a containment escape (issue #1102). Both are used below, because the
invariant is about the *reader* — one live rule is enough to show a truncation,
and a second shows it is not that rule's private quirk.

Both directions are asserted, because a change that refused everything would
otherwise pass this file: an ordinary trailing comment stays allowed, a ``#``
inside quotes is data (it was already one token — quoting is resolved before the
comment rule either way), and the same chains **without** the ``#`` are asserted
to refuse too, so the difference these tests measure is the ``#`` itself rather
than a blanket change.
"""

from __future__ import annotations

import pytest

from emrg.tools import command_scan
from emrg.tools.command_scan import _tokenize_command

# The hidden tails: in every one the `#` sits *inside* the word `a#`, so the shell
# runs what follows it and a truncated reading never saw it. Both live rules are
# represented, so a `#`-blind reader is caught whichever rule it was reading for.
HIDDEN = [
    "echo a#&& emrg server stop",
    "echo a#; emrg server restart",
    "echo a#&& pkill -f emrgd",
    "echo a#&& curl http://169.254.169.254/latest/meta-data/",
]

# The same chains with the `#` removed. Derived, never hand-listed: a hand-written
# control set can drift from the cases it is supposed to control for.
UNHIDDEN_CONTROLS = [cmd.replace("a#", "a") for cmd in HIDDEN]


def test_the_reader_no_longer_stops_at_a_mid_word_hash():
    """The reader, not the rule: the hidden tail must be in the token stream.

    ``_tokenize_command`` is the one tokenizer both rules read — before P7 there
    were two (the write-target walk used one and the git-mutator walk the other),
    and a fix applied to one of them left the other's rules reading a prefix.
    With the legacy scanner gone there is a single reader, so this asserts the
    single spellings' contract rather than both.
    """
    tokens = _tokenize_command("echo a#&& emrg server stop")
    assert tokens[1] == "a#", tokens
    assert tokens[-3:] == ["emrg", "server", "stop"], tokens


def test_a_quoted_hash_is_still_data_in_one_token():
    """`echo "a # b"` is one argument: quoting is resolved before the comment rule."""
    assert _tokenize_command('echo "a # b"') == ["echo", "a # b"]


@pytest.mark.parametrize("cmd", HIDDEN)
def test_the_act_hidden_behind_a_mid_word_hash_is_refused(cmd: str) -> None:
    assert command_scan.command_refusal(cmd) is not None, f"{cmd!r} was allowed"


@pytest.mark.parametrize("cmd", UNHIDDEN_CONTROLS)
def test_the_same_chain_without_the_hash_was_already_refused(cmd: str) -> None:
    """The control: this is what makes the parametrised case above meaningful.

    These were refused on the pre-fix master too, so the flip is the ``#`` being
    read rather than a new blanket refusal — if the fix had been "refuse every
    command mentioning a separator", these tests would still pass while the ones
    above proved nothing.
    """
    assert command_scan.command_refusal(cmd) is not None, f"{cmd!r} was allowed"


@pytest.mark.parametrize(
    "cmd",
    [
        "git status # checking",
        "ls -la # trailing note",
        "echo hi # just a note",
        "emrg --help # what can it do",
    ],
)
def test_a_benign_trailing_comment_still_runs(cmd: str) -> None:
    """No blanket refusal: an ordinary comment stays ordinary."""
    assert command_scan.command_refusal(cmd) is None, f"{cmd!r} was refused"


def test_a_comment_whose_text_chains_is_refused_fail_closed():
    """The stated cost of reading the whole line, pinned so it cannot change silently.

    Telling this comment from code means re-deriving the shell's word-start rule —
    a second parser, which is exactly where a hole would come from. So the reader
    errs toward reading more, and this command is refused even though the shell
    would run only ``ls``.
    """
    assert command_scan.command_refusal("ls # ; emrg server stop") is not None


def test_a_quoted_mention_stays_data():
    """The other direction of the same over-read: a quoted argument is one token.

    Without this row the file could pass by refusing every line that mentions the
    act, which is the defect #1513 records from the other side.
    """
    assert command_scan.command_refusal('echo "emrg server stop"') is None
