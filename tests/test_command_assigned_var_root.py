"""A write target rooted in a variable *the command itself* assigns (issue #1316).

The refusal these tests surround reasons from the **environment**: a target that
still carries a variable root after ``os.path.expandvars`` is one the guard cannot
place, so ``workspace-write`` fails closed. That reasoning is sound about the
environment and wrong about the command, which is a second resolution scope:

    T=.emrg/tmp && cat > "$T/c.md"     the shell resolves this — the write lands in the workspace
    T=.emrg/tmp cat > "$T/c.md"        the shell does not — `$T` is empty, the write lands at /c.md

One character apart, opposite outcomes. The first is the ordinary idiom for a
scratch path (it is how a comment or PR body file gets written), so refusing it is
a false block; the second must keep its refusal. Both are asserted here, and so is
the whole matrix the issue lists between them.

**Ground truth, not self-consistency.** Each verdict was measured against
``/bin/sh`` in a scratch cwd before it was asserted — the shell's own answer for
these shapes, with ``printf '%s\\n' "$T/f"`` standing in for the redirect so that
the answer is printed instead of written:

    ====================  ==========================  ==========
    shape                 the shell expands it to     the guard
    ====================  ==========================  ==========
    T=./inner && …        ./inner/f                    ALLOW, same path
    T=./inner            ⏎ ./inner/f                   ALLOW, same path
    T=./inner; …          ./inner/f                    ALLOW, same path
    T=./inner cat > …     /f (the prefix is not        BLOCK
                          visible to the redirect)
    (no assignment)       /f                          BLOCK
    T=./inner; T=./other  ./other/f                    BLOCK (over-blocks)
    T=$(basename …)       inner/f                      BLOCK (over-blocks)
    T=../outside          ../outside/f                 BLOCK (over-blocks)
    … > "$T/f"; T=./inner /f                          BLOCK
    T=./inner | …         /f (a pipeline element is    BLOCK
                          a shell of its own)
    ====================  ==========================  ==========

The three rows marked *over-blocks* are deliberate and documented in
``_resolve_target_from_command_assignment``: the value is decidable only by
reading execution order (`./other/f`) or by building it (`inner/f`), and the
``..`` value is exactly the write the relative branch's "relative therefore
inside the workspace" assumption cannot survive. A guard may refuse a command it
cannot place; it may never allow one it places elsewhere — that is why the
divergences are all in one direction, and why they are written down here rather
than discovered later.

Every case is driven through ``_check_sandbox`` — the guard's own entry point, in
the tier where the question is asked. Nothing here is executed, and no case names
a file that will be written: a test whose safety depended on the guard working
would stop being a test the moment the guard broke.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emrg.tools.bash_tool import (
    _assigned_value_is_decidable,
    _check_sandbox,
    _resolve_target_from_command_assignment,
    _split_command_statements,
    _split_command_tokens,
)

WW = "workspace-write"
# The workspace is the checkout the tests run in, derived rather than hardcoded:
# the assertion is about *membership of the workspace*, so a path that names
# another machine's tree would make the test measure nothing.
WORKSPACE = str(Path(__file__).resolve().parent.parent)
# A scratch path inside the workspace. It need not exist: no case writes, and the
# membership rule is a name comparison.
SCRATCH = ".emrg/sessions/emrg-evolution-emrg-task/tmp"

MAY_RESOLVE = [
    f"T={SCRATCH} && cat > \"$T/c.md\"",
    f"T={WORKSPACE}/{SCRATCH} && cat > \"$T/c.md\"",
    f"T={SCRATCH}\necho x > \"$T/c.md\"",
    f"T={SCRATCH}; echo x > \"$T/c.md\"",
    f"T={SCRATCH} && cat > \"${{T}}/c.md\"",
]

# (command, why the shell does not put the write where the path spells it)
MUST_STAY_REFUSED = [
    ('T=tmp cat > "$T/c.md"',
     "the inline prefix is not visible to the redirect"),
    ('T=tmp cat > /dev/null && cat > "$T/f"',
     "an inline prefix does not persist into the next statement either"),
    ('T=tmp cat > /dev/null; cat > "$T/f"',
     "the same, with `;` as the separator"),
    ('cat > "$T/c.md"',
     "no assignment in the command at all"),
    ('T=.emrg/tmp; T=/etc; cat > "$T/f"',
     "assigned twice: the value at the site is not the one assignment"),
    ('T=$(mktemp -d) && cat > "$T/f"',
     "the value is built, not literal"),
    ('T=`mktemp -d` && cat > "$T/f"',
     "the value is built, not literal — the other spelling of it"),
    ('T=../outside && cat > "$T/f"',
     "a literal that would move a relative target out of the workspace"),
    ('T=/etc && cat > "$T/passwd"',
     "a literal absolute outside the workspace"),
    ('T=.emrg/tmp && sh -c \'cat > "$T/f"\'',
     "unexported: the nested shell expands it to nothing"),
    ('cd /tmp && T=.emrg/tmp && cat > "$T/f"',
     "the moved-out check must still run"),
    ('cat > "$T/f"; T=.emrg/tmp',
     "the assignment comes after the write site"),
    ('T=.emrg/tmp | cat > "$T/f"',
     "a pipeline element is a shell of its own"),
    ('T=.emrg/tmp & cat > "$T/f"',
     "a background job is a shell of its own"),
    ('T= && cat > "$T/f"',
     "an empty value puts the write at /f"),
    ('echo x > out$T/y.txt',
     "the root is not the leading word"),
    ('echo x > ${T:?}/y.txt',
     "an expansion operator decides the value, not an assignment"),
    ('A=.emrg B=tmp && cat > "$A/$B/c.md"',
     "a second root this rule cannot reach in the same path"),
    ("cat <<'EOF' > notes.txt\nmyprog\nEOF\necho x > \"$T/f\"",
     "a heredoc body nothing blanked is text, not statements"),
    ("cat <<'EOF' > notes.txt\nbody\nEOF\nmyprog <<EOF\nT=.emrg/tmp\nEOF\ncat > \"$T/f\"",
     "a *half*-masked command: one body blank, the other still text"),
]

CONTROLS_ALLOWED = [
    ("echo x > out.txt", "a literal relative target"),
    ("cp $SRC $DST", "a bare `$VAR` operand is not a variable *root*"),
    ("echo $SHELL", "a read"),
    (f'T={SCRATCH} && cat > "$T~f"', "no `/` after the variable: not this rule"),
]


@pytest.mark.parametrize("cmd", MAY_RESOLVE)
def test_a_preceding_statements_assignment_resolves_the_target(cmd: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, WW, WORKSPACE)
    assert allowed, (cmd, reason)


@pytest.mark.parametrize("cmd,why", MUST_STAY_REFUSED)
def test_every_other_shape_keeps_its_refusal(cmd: str, why: str) -> None:
    """Fail closed on the whole matrix: this rule may never open a new path."""
    allowed, reason, _enforcement = _check_sandbox(cmd, WW, WORKSPACE)
    assert not allowed, f"{cmd} was allowed ({why})"
    assert "sandbox" in (reason or ""), (cmd, reason)


@pytest.mark.parametrize("cmd,why", CONTROLS_ALLOWED)
def test_the_rules_own_boundaries_are_unchanged(cmd: str, why: str) -> None:
    allowed, reason, _enforcement = _check_sandbox(cmd, WW, WORKSPACE)
    assert allowed, (cmd, why, reason)


def test_the_inline_prefix_and_the_earlier_statement_differ() -> None:
    """The pair the issue is about, asserted as a pair.

    These two commands differ by one character and the shell treats them
    differently, so a rule that answers them the same way is wrong whichever way
    it answers: allowing both spends the safety margin the rule exists for
    (measured — `$T` is empty at the inline redirect, so the write leaves the
    workspace), and refusing both is the false block the issue reports.
    """
    inline = f'T={SCRATCH} cat > "$T/c.md"'
    earlier = f'T={SCRATCH} && cat > "$T/c.md"'
    assert _split_command_tokens(inline) != _split_command_tokens(earlier)
    inline_allowed, _r1, _e1 = _check_sandbox(inline, WW, WORKSPACE)
    earlier_allowed, _r2, _e2 = _check_sandbox(earlier, WW, WORKSPACE)
    assert inline_allowed is False
    assert earlier_allowed is True


def test_the_resolved_path_is_the_one_the_shell_would_use() -> None:
    """The verdict is not enough: the path itself is what the checks are made of.

    Both spellings of this command are relative, so a resolver that dropped the
    separator (`$T/f` → `./innerf`) still reached the same *verdict* — the value it
    handed the workspace / protected-file checks was a different file than the
    shell writes. Measured while writing this, and the reason this test asserts
    the string rather than the boolean.
    """
    assert _resolve_target_from_command_assignment(
        'T=./inner && cat > "$T/f"', "$T/f") == "./inner/f"
    # The braced spelling is the same root, and must resolve to the same path.
    assert _resolve_target_from_command_assignment(
        'T=./inner && cat > "${T}/f"', "${T}/f") == "./inner/f"


def test_a_resolved_absolute_value_is_still_judged_by_the_tier() -> None:
    """Resolution must hand the value to the existing rules, not replace them.

    `T=/etc` is decidable, so the target *is* resolved — and then refused for
    being outside the workspace, which is a different reason from "unresolvable".
    Asserting the reason is what distinguishes the two: a guard that resolved the
    value and then refused it for the variable's sake would pass a boolean test
    while still being the defect this issue is about.
    """
    allowed, reason, _enforcement = _check_sandbox(
        'T=/etc && cat > "$T/passwd"', WW, WORKSPACE)
    assert allowed is False
    assert "outside workspace" in (reason or ""), reason
    assert "shell variable" not in (reason or ""), reason


def test_a_nested_shell_gets_no_resolution_from_its_parent() -> None:
    """The fail-open the issue warns about, held by its own test.

    An unexported variable is invisible to a nested shell, so `sh -c 'cat >
    "$T/f"'` writes wherever an empty `$T` points. Resolving the parent's value
    into that payload would turn the fix into the hole, and the two are one
    textual substitution apart.
    """
    cmd = f'T={SCRATCH} && sh -c \'cat > "$T/f"\''
    assert _resolve_target_from_command_assignment(cmd, "$T/f") is None
    allowed, reason, _enforcement = _check_sandbox(cmd, WW, WORKSPACE)
    assert allowed is False
    assert "shell variable" in (reason or ""), reason


def test_a_body_that_cannot_be_blanked_is_not_read_as_statements() -> None:
    """A heredoc body is input, and this rule must not build values out of it.

    When every body line is blanked (`_mask_data_heredoc_bodies`) the lines that
    remain are the statements; when any body is left as text there is no such
    guarantee, and an assignment written in a body is a line of somebody's data
    that no later `$T` ever sees. The command then keeps its refusal rather than
    gaining an allowance built from text.

    The second case is the one that got in: masking can come back **half**-applied
    — `cat <<EOF` is a data reader and gets blanked, `myprog <<EOF` is not and does
    not — and the first version of this rule only noticed a command where *nothing*
    had been masked. Driven end to end it answered ALLOW and resolved the write
    from the body's own `T=.emrg/tmp`, while `/bin/sh` put it at `/f`.
    """
    for body in (
        'myprog <<EOF\nT=.emrg/tmp\nEOF\ncat > "$T/f"',
        "cat <<'EOF' > notes.txt\nbody\nEOF\nmyprog <<EOF\nT=.emrg/tmp\nEOF\ncat > \"$T/f\"",
    ):
        assert _resolve_target_from_command_assignment(body, "$T/f") is None
        allowed, _reason, _enforcement = _check_sandbox(body, WW, WORKSPACE)
        assert allowed is False, body


# The values the rule is willing to use, split by the boundary the docstring
# states rather than by what was easy to write down.
DECIDABLE_VALUES = [".emrg/tmp", "./inner", "/tmp/x", "/etc", "inner/sub"]
UNDECIDABLE_VALUES = [
    "",                     # an empty value puts an absolute target at `/f`
    "../outside",           # the relative branch's assumption cannot survive this
    "a/../b",               # the same, in the middle of the value
    "$HOME",                # the value the *shell* would need to expand first
    "~/x",                  # …and the tilde spelling of the same
    "$(mktemp -d)",         # a value built by running something
    "`mktemp -d`",          # …in the other substitution spelling
    "a b",                  # whitespace needs quoting to survive the tokenizer
    "*",                    # a glob is not a path fragment this rule can place
    "a\\b",                 # a backslash is a separator on Windows, poison on POSIX
]


@pytest.mark.parametrize("value", DECIDABLE_VALUES)
def test_a_literal_path_fragment_is_a_decidable_value(value: str) -> None:
    assert _assigned_value_is_decidable(value)


@pytest.mark.parametrize("value", UNDECIDABLE_VALUES)
def test_every_other_value_is_refused_rather_than_guessed(value: str) -> None:
    """The rule's own boundary, stated as a test.

    The `..` half of this boundary is also held end to end (the must-stay-refused
    rows above), and it is the half that matters: a relative target is assumed to
    be inside the workspace, so a value with a `..` segment is exactly the write
    that assumption cannot survive.

    The **charset** half is held here only, and the reason is a measurement: the
    mutation arm that drops it (`if not _ASSIGNED_LITERAL_VALUE_RE.match(value)`
    → `if False`) is *survived* by the end-to-end corpus, because the shapes it
    would let through are refused before or after it — `_resolve_target_…` bails
    out on a `(` or a backtick before the value is ever read, and the caller
    re-checks the substituted string for a variable root that is still there, so
    `$HOME` cannot slip past either. It is kept anyway, and this is where that
    decision is written down: without it the rule's own claim — "a literal path
    fragment and nothing else" — would be wider than what it can place, and a
    future cycle loosening it should have to change an assertion on purpose
    rather than by editing one line of the guard.
    """
    assert not _assigned_value_is_decidable(value)


def test_the_statement_lexer_reports_what_the_tokenizer_loses() -> None:
    """The premise of the second lexer, asserted rather than assumed.

    A newline is a statement separator to the shell and whitespace to `shlex`, so
    the ordinary tokenizer hands the guard one word-list where the shell runs two
    statements — which is why the newline row of the matrix could not be answered
    from it. If a future tokenizer keeps the newline, this fails and the second
    lexer can be reconsidered instead of kept by habit.
    """
    cmd = f"T={SCRATCH}\necho x > \"$T/c.md\""
    assert "\n" not in _split_command_tokens(cmd)
    assert "\n" in _split_command_statements(cmd)
