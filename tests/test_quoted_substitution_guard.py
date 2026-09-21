"""A command substitution inside double quotes is read as the command it runs.

Issue #1516. Quote semantics in the tokenizer are deliberate (issue #1162: a `>` inside
a quoted argument must stay *inside* the token instead of becoming an operator), and what
keeps that `>` in also keeps a `$( … )` in: the substitution reaches the walk as one
token, so neither reader saw a command word and neither tier refused it. Measured before
the fix — the end-to-end rows are the issue's own (a scratch repository, one commit, one
uncommitted edit, `BashTool.execute`), the predicate rows are re-measured on master
`8861f1c3` with nothing executed:

    ALLOW | edit discarded | echo "$(git checkout .)"          (read-only)
    ALLOW | edit discarded | echo "`git checkout .`"           (read-only)
    ALLOW | edit discarded | echo "$(sh -c 'git checkout .')"  (read-only)
    BLOCK | edit survives  | git checkout .                    <- the control
    ALLOW | targets []     | echo "$(touch /outside/probe)"    (both tiers)
    BLOCK | targets named  | touch /outside/probe              (workspace-write)

The last two rows are the pair that matters at `workspace-write`: the bare spelling of
the write is refused with its target named, the quoted substitution of the same write is
refused at neither tier, because the target list was empty and an empty list is allowed by
construction — the tier question was answered about nothing.

**Which tier each row moves**: a `git` mutator is `read-only`'s business (that is the
tier whose whole purpose is to protect a dirty tree), while `workspace-write` deliberately
allows it; a write that leaves the workspace is both tiers' business. The assertions below
follow that split rather than "both tiers refuse everything", because a test that demanded
the second would be pinning a rule this guard does not have.

**The token stream cannot recover this**, which is why the fix reads text: shlex dequotes,
so the double-quoted form (which the shell runs) and its single-quoted twin (which it does
not) produce the *same* token — `test_the_token_stream_cannot_tell_the_two_apart` pins
that, because a "fix" in the tokenizer would undo #1162. Every row here is a pure predicate
on a literal path (`_check_sandbox` / `_extract_write_targets` / `_find_git_mutator`, which
only `realpath` the text); no row in this file is executed.
"""

from __future__ import annotations

import pytest

from emrg.tools.bash_tool import (
    _check_sandbox,
    _extract_write_targets,
    _find_git_mutator,
    _quoted_substitution_bodies,
    _split_command_tokens,
)

READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"

# The literal the sandbox suite already uses for this: a path outside any workspace, and
# one that exists on this host under neither reading. Nothing is executed, so it is only
# ever an input to a predicate.
WORKSPACE = "/workspace"
OUTSIDE = "/outside/probe.txt"


def _reads(cmd: str, tier: str) -> bool:
    allowed, _reason, _enforcement = _check_sandbox(cmd, tier, workdir=WORKSPACE)
    return allowed


# The quoted spellings the issue measured, every one of them running `git checkout .`.
QUOTED_GIT = [
    'echo "$(git checkout .)"',
    'echo "`git checkout .`"',
    'x="$(git checkout .)"',
    'echo "tail $(git checkout .)"',
    'echo "$(sh -c \'git checkout .\')"',
    'echo "$(echo $(git checkout .))"',
    'echo "`git stash`"',
]

# Their single-quoted twins: the shell substitutes nothing, so nothing may be refused.
LITERAL_TWINS = [
    "echo '$(git checkout .)'",
    "echo '`git checkout .`'",
    "x='$(git checkout .)'",
    "echo 'tail $(git checkout .)'",
    "echo '`git stash`'",
]


@pytest.mark.parametrize("cmd", QUOTED_GIT)
def test_a_quoted_git_substitution_is_refused_at_read_only(cmd):
    """The tier that exists to protect the uncommitted edit is the one this moves."""
    assert _find_git_mutator(cmd), cmd
    assert _reads(cmd, READ_ONLY) is False, cmd
    # ...and the tier that deliberately allows a mutator still allows it, so this is a
    # reading of the substitution and not a widening of workspace-write.
    assert _reads(cmd, WORKSPACE_WRITE) is True, cmd


@pytest.mark.parametrize("cmd", LITERAL_TWINS)
def test_a_single_quoted_substitution_stays_literal(cmd):
    assert _find_git_mutator(cmd) is None, cmd
    assert _extract_write_targets(cmd) == [], cmd
    assert _quoted_substitution_bodies(cmd) == [], cmd
    assert _reads(cmd, READ_ONLY) is True, cmd
    assert _reads(cmd, WORKSPACE_WRITE) is True, cmd


@pytest.mark.parametrize(
    "template",
    [
        'echo "$(touch {p})"',
        'echo "$(rm -rf {p})"',
        'echo "$(mkdir {p})"',
        'echo "prefix $(touch {p}) suffix"',
    ],
)
def test_a_write_inside_a_quoted_substitution_names_its_target(template):
    """Both tiers judge a target list, so the write has to be *named* to be judged."""
    cmd = template.format(p=OUTSIDE)
    assert _extract_write_targets(cmd) == [OUTSIDE], cmd
    assert _reads(cmd, READ_ONLY) is False, cmd
    assert _reads(cmd, WORKSPACE_WRITE) is False, cmd
    # The bare spelling of the same write is the row it must agree with.
    assert _extract_write_targets(f"touch {OUTSIDE}") == [OUTSIDE]
    assert _reads(f"touch {OUTSIDE}", WORKSPACE_WRITE) is False


@pytest.mark.parametrize(
    "template",
    [
        "echo '$(touch {p})'",
        "echo '`touch {p}`'",
        "echo '$(rm -rf {p})'",
    ],
)
def test_a_write_in_a_single_quoted_substitution_is_not_read(template):
    cmd = template.format(p=OUTSIDE)
    assert _extract_write_targets(cmd) == [], cmd
    assert _reads(cmd, READ_ONLY) is True, cmd
    assert _reads(cmd, WORKSPACE_WRITE) is True, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        'echo "$(date)"',
        'echo "$(cat f.txt)"',
        'echo "$(ls -l | head -1)"',
        'echo "total $(wc -l < f.txt) lines"',
        'echo "$(git status)"',
        'echo "$(git rev-parse HEAD)"',
    ],
)
def test_a_read_inside_a_quoted_substitution_stays_allowed(cmd):
    assert _extract_write_targets(cmd) == [], cmd
    assert _find_git_mutator(cmd) is None, cmd
    assert _reads(cmd, READ_ONLY) is True, cmd
    assert _reads(cmd, WORKSPACE_WRITE) is True, cmd


def test_the_token_stream_cannot_tell_the_two_apart():
    """The reason the fix reads text: dequoting makes the two spellings one token."""
    running = 'echo "$(git checkout .)"'
    literal = "echo '$(git checkout .)'"
    assert _split_command_tokens(running) == _split_command_tokens(literal)
    assert _find_git_mutator(running) and _find_git_mutator(literal) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ('echo "$(git checkout .)"', ["git checkout ."]),
        ("echo \"`git checkout .`\"", ["git checkout ."]),
        ("echo '$(git checkout .)'", []),
        ("echo '`git checkout .`'", []),
        ('echo "`date` and $(whoami)"', ["date", "whoami"]),
        ('echo "$(echo $(git checkout .))"', ["echo $(git checkout .)"]),
        ("echo \"$(printf '%s' 'a ) b')\"", ["printf '%s' 'a ) b'"]),
        (r'echo "\$(git checkout .)"', []),
        ('echo "$(git checkout ."', []),
        ('echo "no substitution here"', []),
        ("echo $(date)", ["date"]),
    ],
)
def test_the_bodies_are_the_ones_the_shell_substitutes(text, expected):
    assert _quoted_substitution_bodies(text) == expected, text


def test_the_1162_shapes_are_unchanged():
    """A `>` inside a quoted argument is still a character, not an operator."""
    for cmd in [
        'echo "a > b"',
        'python3 -c "print(1 > 0)"',
        "grep -n '>' f.txt",
        'echo "$(date) > b"',
    ]:
        assert _extract_write_targets(cmd) == [], cmd
        assert _reads(cmd, READ_ONLY) is True, cmd
    refused = f"echo '>' > {OUTSIDE}"
    assert _extract_write_targets(refused) == [OUTSIDE], refused
    assert _reads(refused, WORKSPACE_WRITE) is False, refused


def test_a_masked_heredoc_body_is_not_read_as_a_substitution():
    """The masking every other reader applies wins over this one too."""
    cmd = f"cat <<EOF\ndocument $(touch {OUTSIDE})\nEOF"
    assert _extract_write_targets(cmd) == [], cmd
    assert _reads(cmd, WORKSPACE_WRITE) is True, cmd
