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
``_resolve_from_command_assignment``: the value is decidable only by
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

**Every case must be answered the same way on every host, and the first version of
this file was not.** Green locally, it failed both CI legs, each for its own
reason — the two host spellings a verdict can accidentally be made of:

  - ``/tmp`` was the moved-out case's destination. It is outside the workspace on
    macOS, where the case was written, and on Linux it *is*
    ``tempfile.gettempdir()`` — an allowed write root — so the case never reached
    the moved-out check and measured the temp-area allowance instead. Replaced by
    ``OUTSIDE``, derived, with its membership in no allowed root asserted as a
    premise (``test_the_outside_directory_is_taken_as_moved_out``); the same
    condition replays locally under ``TMPDIR=/tmp``.
  - the absolute row interpolated ``WORKSPACE`` as the host spells it, so on
    Windows it carried a drive letter and a backslash: ``:`` is outside the value
    charset, and a backslash is shlex's escape character (issue #1261), which is
    why a whole row can be decidable on one platform and refused on another.
    Every path this file composes now goes through ``spelled``, and the absolute
    case asserts itself where the charset admits its spelling — skipping, with a
    measured reason, where it does not.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from emrg.tools.bash_tool import (
    _assigned_value_is_decidable,
    _check_sandbox,
    _cwd_left_workspace,
    _is_absolute_path,
    _is_within,
    _resolve_from_command_assignment,
    _split_command_statements,
    _split_command_tokens,
    _temp_write_roots,
    _trusted_write_zones,
)

WW = "workspace-write"
# The workspace is the checkout the tests run in, derived rather than hardcoded:
# the assertion is about *membership of the workspace*, so a path that names
# another machine's tree would make the test measure nothing.
WORKSPACE = str(Path(__file__).resolve().parent.parent)
# A scratch path inside the workspace. It need not exist: no case writes, and the
# membership rule is a name comparison.
SCRATCH = ".emrg/sessions/emrg-evolution-emrg-task/tmp"
# A directory outside the workspace that is also outside every *allowed write
# root* (the temp area and the trusted data roots) — the premise the moved-out
# case rests on, asserted in `test_the_outside_directory_is_taken_as_moved_out`.
#
# `/tmp` is not that directory, and reading it as one is how this file shipped a
# platform-dependent verdict (issue #1316's PR): it is outside the workspace on
# macOS, where the case was written and measured, but on Linux it *is*
# `tempfile.gettempdir()` — an allowed write root — so `cd /tmp && …` never
# reached the moved-out check at all and the case measured the temp-area
# allowance instead. Green locally, red on the ubuntu leg of CI.
OUTSIDE = os.path.join(
    os.path.expanduser("~"), "Documents", "emrg-var-root-outside")


def spelled(path: str) -> str:
    """The path as a command line spells it (forward slashes).

    The guard reads commands from a POSIX-style token stream, where a backslash
    is an escape character, so a Windows spelling `C:\\Users\\x` reaches it as
    `C:Usersx` — a name that is not absolute at all (issue #1261). Every path this
    file interpolates into a command goes through here, or the case stops being a
    test of the rule and becomes a test of that separate defect: the absolute row
    of the resolvable matrix was written with the raw workspace path and failed on
    the windows leg for exactly that reason.
    """
    return path.replace(os.sep, "/") if os.sep != "/" else path

MAY_RESOLVE = [
    f"T={SCRATCH} && cat > \"$T/c.md\"",
    f"T={SCRATCH}\necho x > \"$T/c.md\"",
    f"T={SCRATCH}; echo x > \"$T/c.md\"",
    f"T={SCRATCH} && cat > \"${{T}}/c.md\"",
]
# An *absolute* resolvable value is asserted separately
# (`test_an_absolute_value_resolves_where_the_charset_admits_the_spelling`): the
# only absolute spelling the value charset admits is a POSIX one, so a row here
# would have made the matrix platform-dependent in the other direction — it is
# the row that failed on the windows leg.

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
    (f'cd {spelled(OUTSIDE)} && T={SCRATCH} && cat > "$T/f"',
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
    ('D=.emrg && cd "$D" && cat > f',
     "an assigned move that stays in the workspace is placed, not refused"),
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


def test_the_outside_directory_is_taken_as_moved_out() -> None:
    """The premise of the moved-out row, asserted instead of assumed.

    That row claims "a `cd` out of the workspace keeps the refusal", and it can
    only claim it from a destination no allowed write root covers: a destination
    inside one is allowed *by the boundary's own rule*, so the refusal never
    happens and the row measures nothing. That is not hypothetical — it is what
    `/tmp` did on Linux, where it is `tempfile.gettempdir()`. Asserting the
    structural facts here means a host whose layout makes this destination
    writable fails loudly in a test that names the reason, rather than in a
    parametrized row whose failure looks like a guard regression.
    """
    assert _is_absolute_path(spelled(OUTSIDE))
    assert not _is_within(OUTSIDE, WORKSPACE)
    roots = [r for group in (_temp_write_roots(), _trusted_write_zones()) for r in group]
    assert not any(_is_within(OUTSIDE, r) or os.path.realpath(r) == os.path.realpath(OUTSIDE)
                   for r in roots), f"{OUTSIDE!r} is inside an allowed write root: {roots}"
    cmd = f"cd {spelled(OUTSIDE)} && T={SCRATCH} && cat > \"$T/f\""
    assert _cwd_left_workspace(cmd, WORKSPACE) is not None, cmd


def test_a_move_spelled_by_an_assigned_variable_is_placed() -> None:
    """The move walk reads the same resolution scope as the write-target rule.

    One defect, two halves, and the second half was the dangerous one: the scope
    was added for write targets, and the walk that decides whether a *relative*
    target is still relative to the workspace kept expanding the environment
    alone. It then joined the literal `$D` onto the cwd, which reads as
    "`.`/`$D` — inside", so the move was invisible and the target behind it was
    read as in-workspace. Measured on the head this file is part of: with
    `D=<outside>` above it, `cd "$D" && T=<in-ws> && cat > "$T/f"` is BLOCK on
    master (the target-side scope absent, so the unresolvable root refused it),
    ALLOW with the target-side scope only, and BLOCK again once the walk reads
    the assignment. The introduced allowance is what this case exists for: the
    shell writes outside the workspace either way, and the guard said so before
    the rule that was supposed to widen legitimate use arrived.

    Both assertions are needed and they are different questions — that the move
    is *placed* (the walk's answer) and that the write is *refused* (the tier's).
    A walk that returned the directory while the loop below ignored it would
    satisfy the second alone for the wrong reason.

    The deciding value is an absolute spelling, which the value charset admits on
    POSIX only (issue #1354), so this skips where the value cannot be decided —
    with the cause held on every host by `test_why_the_absolute_case_is_posix_only`.
    """
    cmd = f'D={spelled(OUTSIDE)} && cd "$D" && T={SCRATCH} && cat > "$T/f"'
    if not _assigned_value_is_decidable(spelled(OUTSIDE)):
        pytest.skip(f"the value charset does not admit this spelling: {cmd}")
    assert _cwd_left_workspace(cmd, WORKSPACE) is not None, cmd
    allowed, reason, _enforcement = _check_sandbox(cmd, WW, WORKSPACE)
    assert not allowed, (cmd, reason)
    assert "sandbox" in (reason or ""), (cmd, reason)


def test_the_move_and_the_target_are_resolved_by_one_rule() -> None:
    """The two call sites answer from the same function, asserted as a pair.

    The asymmetry this pins is invisible in either half alone: a resolver that
    read assignments for write targets and a walk that read only the environment
    are each defensible, and together they turned a refusal into an allowance.
    Asserting the shared entry point's verdict on both kinds of token is what
    makes the two halves one rule rather than two that happen to agree.
    """
    assert _resolve_from_command_assignment(
        'D=/outside && cd "$D"', "$D") == "/outside"
    assert _resolve_from_command_assignment(
        'T=/outside && cat > "$T/f"', "$T/f") == "/outside/f"
    # A move whose value is decided by the shell, not by an assignment, is not
    # placed — and the walk then reads it as inside (the limit named in
    # `_cwd_left_workspace`, not a promise of this rule).
    assert _resolve_from_command_assignment(
        'cd "$D" && D=/outside', "$D") is None
    assert _resolve_from_command_assignment(
        'D=../outside && cd "$D"', "$D") is None


def test_an_absolute_value_resolves_where_the_charset_admits_the_spelling() -> None:
    """The absolute half of the matrix, on the platform whose spelling is decidable.

    The value charset (`_ASSIGNED_LITERAL_VALUE_RE`) is POSIX-shaped: it has no
    `:` in it, so `T=D:/a/ws && …` is a value the rule will not use however
    absolute it plainly is, and a backslash spelling never reaches the guard as
    one word at all (issue #1261). On Windows, #1316's false block therefore
    survives for absolute values — a limitation of this rule, not of this test,
    and recorded as an issue rather than papered over.

    The skip is tied to its cause, not to the platform: it disappears by itself
    if the charset ever admits a drive letter, and the case then asserts the
    Windows verdict too. `test_why_the_absolute_case_is_posix_only` holds the
    cause on every platform, so the skip cannot outlive it silently.
    """
    cmd = f'T={spelled(WORKSPACE)}/{SCRATCH} && cat > "$T/c.md"'
    if not _assigned_value_is_decidable(f"{spelled(WORKSPACE)}/{SCRATCH}"):
        pytest.skip(f"the value charset does not admit this spelling: {cmd}")
    allowed, reason, _enforcement = _check_sandbox(cmd, WW, WORKSPACE)
    assert allowed, (cmd, reason)


def test_why_the_absolute_case_is_posix_only() -> None:
    """The skip's cause, as a predicate that measures the same on every host."""
    assert _assigned_value_is_decidable("/tmp/emrg-ws") is True
    assert _assigned_value_is_decidable("D:/emrg-ws") is False


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
    assert _resolve_from_command_assignment(
        'T=./inner && cat > "$T/f"', "$T/f") == "./inner/f"
    # The braced spelling is the same root, and must resolve to the same path.
    assert _resolve_from_command_assignment(
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
    assert _resolve_from_command_assignment(cmd, "$T/f") is None
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
        assert _resolve_from_command_assignment(body, "$T/f") is None
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
    would let through are refused before or after it — `_resolve_from_command_assignment_…` bails
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
