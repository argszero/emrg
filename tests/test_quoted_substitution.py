"""A command substitution between quotes is a command (issue #1516).

`"$(git checkout .)"` reaches the guard as **one token**, because quote semantics
are deliberate here (issue #1162: a `>` inside a quoted argument must stay inside
the token, not become an operator). So no payload reader had a command word to
recurse into and the command the substitution really runs was invisible: measured
through `_check_sandbox`, `echo "$(git checkout .)"`, `x="$(git checkout .)"` and
`echo "$(rm -rf /tmp/x)"` all answered ALLOW with an empty target list, while
`git checkout .` on its own was refused. The quoted spelling *executes* — the
reporter drove it end to end at `read-only` and the uncommitted edit was
discarded.

The property under test is **parity**, as in `test_bash_tool_sandbox_cwd.py` and
`test_bash_tool_sandbox_pushd.py`: however the guard judges the bare spelling of
a command, it must judge the quoted-substitution spelling of the same command the
same way. A verdict list would pass on a guard that refused every line mentioning
`$(`, which would be a false block of a pure read — parity fails such a guard on
the allow side, and the allow rows below are that side.

Three arms in each direction, all through the predicates the tool itself calls
(`BashTool.execute` refuses on `_check_sandbox` before it spawns anything):

* the mutating spellings, refused at `read-only` exactly as their bare twin is;
* the harmless ones — single quotes, an escaped `$(`, a body that runs nothing —
  untouched;
* the **reviewer's mutation arm**: with the body-reader neutered the new
  refusals go away, so they are not a verdict this guard already had.
"""

import os

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import (
    _WINDOWS_SHELL,
    _check_sandbox,
    _extract_write_targets,
    _find_git_mutator,
    _nested_command_texts,
    _split_command_tokens,
    _substitution_payloads,
)

# Synthetic paths: the checks are textual, so the directories need not exist.
WORKDIR = os.path.join(os.path.expanduser("~"), "Documents", "emrg-1516-ws")
OUTSIDE = os.path.join(os.path.expanduser("~"), "Documents", "emrg-1516-outside")
RO = "read-only"
WW = "workspace-write"

# The bare control every quoted spelling below is asked to match.
BARE = "git checkout ."

# The same command, every spelling the reporter measured as allowed.
QUOTED = [
    'echo "$(git checkout .)"',
    'echo "`git checkout .`"',
    'x="$(git checkout .)"',
    'echo "tail $(git checkout .)"',
    'echo "$(sh -c \'git checkout .\')"',
    'echo "$(echo "$(git checkout .)")"',
    'echo "$(git checkout .)" > /dev/null',
]

# The rows the token stream cannot show *at all*: before the fix each of these
# answered ALLOW at `read-only` with an empty target list (measured). The nested
# double-quote spelling above is deliberately not one of them — there the inner
# quote closes the outer one, so `git checkout` reaches the token stream on its
# own — which is why the mutation arm below is pinned on this list: the refusal it
# removes must be the line reader's and nobody else's.
INVISIBLE_TO_TOKENS = [
    'echo "$(git checkout .)"',
    'echo "`git checkout .`"',
    'x="$(git checkout .)"',
    'echo "tail $(git checkout .)"',
    'echo "$(sh -c \'git checkout .\')"',
    'echo "$(git checkout .)" > /dev/null',
]

# A quoted body that runs nothing, or runs something harmless.
HARMLESS = [
    "echo '$(git checkout .)'",          # single quotes are literal
    "echo '`git checkout .`'",
    'echo "not a substitution"',
    'echo "$(date)"',                     # substitutes, mutates nothing
    'echo "$(cat f.txt)"',
    'echo "$(git status --short)"',       # a read-only git verb
    'echo "a > b"',                       # the #1162 shape, a quoted operator
    'echo "a | b"',
]


def spelled(path: str) -> str:
    """The path as a command line spells it (forward slashes)."""
    return path.replace(os.sep, "/") if os.sep != "/" else path


def verdict(cmd: str, mode: str = RO, workdir: str | None = WORKDIR) -> bool:
    allowed, _reason, _enforcement = _check_sandbox(cmd, mode, workdir)
    return allowed


def reason(cmd: str, mode: str = RO) -> str:
    return _check_sandbox(cmd, mode, WORKDIR)[1] or ""


def test_premise_the_fixture_sits_where_the_boundary_can_place_it():
    """Without this, an "outside" fixture inside a trusted root would make every
    refusal below true for the wrong reason."""
    assert verdict(BARE, RO) is False, "the bare control must be refused"
    assert verdict(BARE, WW) is True, "workspace-write allows an in-workspace mutator"
    assert verdict(f"touch {spelled(OUTSIDE)}/x", WW) is False


@pytest.mark.parametrize("cmd", QUOTED)
def test_a_quoted_substitution_is_read_as_the_command_it_runs(cmd):
    """The hole: the payload is what the shell runs, so the tier must judge it."""
    assert _find_git_mutator(cmd) == "git checkout"
    assert verdict(cmd, RO) is False
    assert "git checkout" in reason(cmd, RO)


@pytest.mark.parametrize("cmd", QUOTED)
def test_the_quoted_spelling_gets_the_bare_spelling_s_verdict(cmd):
    """Parity, both tiers: the quoted form is not a different rule."""
    assert verdict(cmd, RO) == verdict(BARE, RO)
    assert verdict(cmd, WW) == verdict(BARE, WW)


@pytest.mark.parametrize(
    "spelling",
    [
        '$(rm -rf {path}/doomed)',
        '`rm -rf {path}/doomed`',
        '$(touch {path}/written)',
        '$(echo x > {path}/written)',
    ],
)
def test_a_write_inside_a_quoted_substitution_names_its_target(spelling):
    """The third measured row: the destination is what the tier question needs.

    With the body unread the tier question was answered about nothing — so this
    row was allowed at `workspace-write` too, where it is not "a mutator the tier
    deliberately allows" but a write to a path the tier exists to keep out.
    """
    path = spelled(OUTSIDE)
    cmd = 'echo "' + spelling.format(path=path) + '"'
    targets = _extract_write_targets(cmd)
    assert targets, f"no target named in {cmd!r}"
    assert verdict(cmd, RO) is False
    assert verdict(cmd, WW) is False


@pytest.mark.parametrize("cmd", HARMLESS)
def test_a_quoted_body_that_runs_nothing_is_untouched(cmd):
    """The allow side: a false block of a pure read is the other way to fail."""
    assert _extract_write_targets(cmd) == []
    assert _find_git_mutator(cmd) is None
    assert verdict(cmd, RO) is True
    assert verdict(cmd, WW) is True


@pytest.mark.parametrize(
    "text,bodies",
    [
        ('echo "$(git checkout .)"', ["git checkout ."]),
        ('echo "`git checkout .`"', ["git checkout ."]),
        ('x="$(git checkout .)"', ["git checkout ."]),
        ('echo "tail $(git checkout .)"', ["git checkout ."]),
        ('echo "$(echo ")")"', ['echo ")"']),          # a quoted paren is not the close
        ('echo "$((1+2))"', []),                        # arithmetic is an expression
        ('echo "$(($(git checkout .)))"', ["git checkout ."]),  # … with a command in it
        (
            'echo "$(git checkout .)" ; echo "$(rm -rf /tmp/y)"',
            ["git checkout .", "rm -rf /tmp/y"],
        ),
        ("echo '$(git checkout .)'", []),               # single quotes run nothing
        ("echo '`git checkout .`'", []),
        ('echo "not a substitution"', []),
        ('echo "it\'s $(git checkout .)"', ["git checkout ."]),
        ('echo "$(git checkout ."', ['git checkout ."']),  # unterminated: read to the end
    ],
)
def test_the_reader_reads_the_line_and_the_quote_it_stands_in(text, bodies):
    """The one fact the token stream cannot supply, pinned spell by spell."""
    assert _substitution_payloads(text) == bodies


def test_a_backslash_escaped_substitution_follows_the_tokenizer_s_own_rule():
    """`"\\$(x)"` is literal where the shell treats a backslash as an escape.

    On Windows the tokenizer reads a backslash as a path character
    (`_protect_windows_backslashes`), so the same text is read and refused. The
    expectation is read off the module's own flag rather than written down twice:
    the two readings must not be allowed to drift apart.
    """
    cmd = 'echo "\\$(git checkout .)"'
    if _WINDOWS_SHELL:
        assert _substitution_payloads(cmd) == ["git checkout ."]
        assert verdict(cmd, RO) is False
    else:
        assert _substitution_payloads(cmd) == []
        assert verdict(cmd, RO) is True


def test_a_move_inside_a_quoted_substitution_carries_the_relative_write():
    """The same reader feeds the cwd walk, so the move is followed there too."""
    outside = spelled(OUTSIDE)
    quoted = f'echo "$(cd {outside})" && echo x > out.txt'
    bare = f"cd {outside} && echo x > out.txt"
    assert verdict(quoted, WW) == verdict(bare, WW) is False


@pytest.mark.parametrize("cmd", QUOTED + HARMLESS)
def test_the_line_only_ever_adds_payloads(cmd):
    """Monotone in the safe direction: the tokens' reading is still in there."""
    tokens = _split_command_tokens(cmd)
    token_only = _nested_command_texts(tokens)
    with_line = _nested_command_texts(tokens, cmd)
    for payload in token_only:
        assert payload in with_line
    assert len(with_line) >= len(token_only)


@pytest.mark.parametrize("cmd", INVISIBLE_TO_TOKENS)
def test_the_new_refusals_come_from_the_line_reader(cmd, monkeypatch):
    """The reviewer's mutation arm, as an assertion rather than a claim.

    Neutering the reader restores the measured hole exactly: every row above
    answers ALLOW at `read-only` again. If a row stayed refused, the assertion
    below would be pinning someone else's rule and the arm above would be
    measuring nothing.
    """
    monkeypatch.setattr(bash_tool, "_substitution_payloads", lambda text: [])
    assert verdict(cmd, RO) is True
