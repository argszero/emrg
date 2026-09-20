"""The reading operators (`<<`, `<<-`, `<<<`, `<&`) keep their operand out of the walk.

`_args_after_command` steps over a redirect *and the word behind it*, because neither is
a command operand: the shell removes them from the word list and hands the command the
rest. The step-over asked `tok == "<" or _is_redirect_operator(tok)` — the **naming**
question plus one spelling of the reading family — and the naming predicate keeps a
bare `<` out of its set on purpose (naming a `<`'s operand would turn
`cat < /etc/passwd` into a refusal). So every *other* reading spelling was believed to
be a command word, and for a destination-last verb the last word is the destination:
the leftover word became the named target and the real destination went unjudged.

Measured on master `347f023e`, predicate calls only, destination outside every allowed
root (`/outside/emrg/out`), the same geometry `split`'s file describes:

    cp <ws>/a <out> <<EOF      named ['EOF']   — ALLOW at workspace-write
    cp <ws>/a <out> <<-EOF     named ['<<']    — ALLOW at workspace-write
    cp <ws>/a <out> <<< here   named ['here']  — ALLOW at workspace-write
    cp <ws>/a <out> <&0        named ['0']     — ALLOW at workspace-write

Ground truth for what the shell does with those lines, taken in a scratch directory on
this host with `/bin/sh`, one file per row, read back off disk afterwards: each row
**created the destination**, so the walk's verdict was about a write that really
happened and to a path it never named.

The attached-operand spellings are the reason the step-over is asked of every operator
rather than of a list: in `2>&1` the descriptor is its own token (`2` `>&` `1`), so the
word the operator consumes is the `1`, and the word after *that* is still an operand.
`cp <ws>/a 2>&1 <out>` and `cp <ws>/a >&1 <out>` both name the destination — before and
after this change — and they are pinned here so a step-over that widened into consuming
a real operand cannot pass.

This file is separate from `test_bash_tool_sandbox.py` for the reason the other family
files are: it covers one family, and a self-contained fixture set cannot disagree with a
table it does not share.
"""

import os
import subprocess

import pytest

from emrg.tools.bash_tool import (
    _check_sandbox,
    _extract_write_targets,
    _is_redirect_operator,
    _redirect_consumes_the_next_word,
)

# Outside every allowed root (workspace, OS temp root, the evolution data dir) and used
# only as an argument to the pure predicate — never executed and never opened.
OUTSIDE = "/outside/emrg"
WORKSPACE = "/workspace"


def test_the_predicate_is_the_one_the_tiers_read():
    """The two functions this file judges are the ones the tool layer calls.

    A test that reached a private re-reading would keep passing while the shipped guard
    changed, which is the failure this whole class of test exists to prevent.
    """
    allowed, _reason, enforcement = _check_sandbox(
        "cat <<EOF", "workspace-write", WORKSPACE
    )
    assert allowed is True
    assert enforcement in ("partial", "full")


# ── the two questions, and the one spelling they deliberately disagree about ────────
#
# Both readers are asked of every token the walk meets, so the difference has to be a
# *named* one: the naming set excludes a bare `<` (an over-block in exchange for nothing)
# and the step-over set includes it, plus the reading operators it stands for.

AGREEMENT = (">", ">>", ">|", ">&", "&>", "&>>", "<>", "2>&1")
ONLY_THE_STEP_OVER = ("<", "<<", "<<-", "<<<", "<&", "<&-")
NEITHER = ("out", "-", "1", "EOF", "here", "a<b", "--", "||", ";")


@pytest.mark.parametrize("tok", AGREEMENT)
def test_both_readers_agree_on_the_operators_they_share(tok):
    assert _is_redirect_operator(tok) is True, tok
    assert _redirect_consumes_the_next_word(tok) is True, tok


@pytest.mark.parametrize("tok", ONLY_THE_STEP_OVER)
def test_the_step_over_takes_the_reading_operators_too(tok):
    """The set `_is_redirect_operator` cannot hold — and the whole reason for the split.

    A narrow answer here is not a conservative one: the word left in the operand list
    becomes a destination-last verb's destination, so the write it names is the wrong
    path and the right one is never judged.
    """
    assert _is_redirect_operator(tok) is False, tok
    assert _redirect_consumes_the_next_word(tok) is True, tok


@pytest.mark.parametrize("tok", NEITHER)
def test_neither_reader_calls_a_word_an_operator(tok):
    assert _is_redirect_operator(tok) is False, tok
    assert _redirect_consumes_the_next_word(tok) is False, tok


# ── the corpus: every reading spelling must name the destination ────────────────────
#
# `{ws}` and `{out}` are the workspace and the outside destination, so a rule that named
# the wrong word is caught by the target it reports rather than by the verdict alone.
WRITING_FORMS = (
    ("heredoc", "cp {ws}/a {out} <<EOF"),
    ("tab-stripping heredoc", "cp {ws}/a {out} <<-EOF"),
    ("here-string", "cp {ws}/a {out} <<< here"),
    ("heredoc, spaced delimiter", "cp {ws}/a {out} << EOF"),
    ("fd-prefixed heredoc", "cp {ws}/a {out} 0<<EOF"),
    ("input descriptor duplication", "cp {ws}/a {out} <&0"),
    ("closing descriptor", "cp {ws}/a {out} 0<&-"),
    ("bare input redirect", "cp {ws}/a {out} < /dev/null"),
    ("reading operator before the destination", "cp {ws}/a <<EOF {out}"),
    ("reading operator between two operands", "cp {ws}/a <<< here {out}"),
)


@pytest.mark.parametrize("row,cmd", WRITING_FORMS, ids=[row for row, _ in WRITING_FORMS])
def test_every_reading_form_names_the_destination(row, cmd):
    out = f"{OUTSIDE}/out"
    assert _extract_write_targets(cmd.format(ws=WORKSPACE, out=out)) == [out], row


@pytest.mark.parametrize("row,cmd", WRITING_FORMS, ids=[row for row, _ in WRITING_FORMS])
def test_a_named_destination_is_refused_at_both_tiers(row, cmd):
    """Both tiers, because the hole was in both and the untested one is the one a cycle
    runs in: `workspace-write` refuses writes outside the workspace, `read-only` refuses
    them anywhere.
    """
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(
            cmd.format(ws=WORKSPACE, out=f"{OUTSIDE}/out"), tier, WORKSPACE
        )
        assert allowed is False, f"{row}: {tier} allowed a write to {OUTSIDE}"
        assert reason, f"{row}: {tier} refused without a reason"


# ── the controls: what must not change ──────────────────────────────────────────────
ALLOWED_FORMS = (
    ("read verb, heredoc", "cat <<EOF", []),
    ("read verb, here-string", "cat <<< here", []),
    ("read verb, input redirect", "cat < /etc/passwd", []),
    ("read verb, fd duplication", "cat 0<&0", []),
    ("grep with a heredoc", "grep -n x f.txt <<EOF", []),
)


@pytest.mark.parametrize("row,cmd,want", ALLOWED_FORMS, ids=[r for r, _, _ in ALLOWED_FORMS])
def test_the_reading_forms_stay_reads(row, cmd, want):
    """Over-block control: stepping over more of the line must not refuse a read."""
    assert _extract_write_targets(cmd) == want, row
    for tier in ("read-only", "workspace-write"):
        allowed, _reason, _ = _check_sandbox(cmd, tier, WORKSPACE)
        assert allowed is True, f"{row}: {tier} refused a line that writes nothing"


def test_a_creating_verb_keeps_its_operand_beside_a_heredoc():
    """A writer still names what it writes — the reading operator only takes *its* word.

    The row is not in the both-tiers-allowed set above because `mkdir` really writes:
    `read-only` refuses it for that reason. What is pinned is the *target*, which must
    be the directory and not the delimiter.
    """
    assert _extract_write_targets(f"mkdir {WORKSPACE}/new <<EOF") == [f"{WORKSPACE}/new"]
    allowed, _reason, _ = _check_sandbox(
        f"mkdir {WORKSPACE}/new <<EOF", "workspace-write", WORKSPACE
    )
    assert allowed is True


def test_a_digit_operand_is_still_an_operand():
    """The fd-prefix reader must not eat a file that is really called `0`.

    The step-over and the fd-prefix masking both look at digits beside an operator, and
    `cp a b 0` has an operator-free digit that is an ordinary destination.
    """
    assert _extract_write_targets(f"cp {WORKSPACE}/a {WORKSPACE}/b 0") == ["0"]


@pytest.mark.parametrize(
    "row,cmd",
    (
        ("descriptor after the destination", "cp {ws}/a {out} 2>&1"),
        ("descriptor before the destination", "cp {ws}/a 2>&1 {out}"),
        ("bare duplication operator", "cp {ws}/a >&1 {out}"),
        ("metadata before the destination", "cp {ws}/a 2> /dev/null {out}"),
    ),
    ids=["after", "before", "bare", "spaced"],
)
def test_an_attached_descriptor_is_not_the_next_word(row, cmd):
    """The union's other half: stepping over `>&` must consume its descriptor, not a path.

    The descriptor rides inside the operator's spelling in the shell's grammar, so the
    word *after* the operator is the command's own operand. A step-over that took the
    next word unconditionally would name this row's destination nowhere — the same hole
    one operator over.
    """
    out = f"{OUTSIDE}/out"
    targets = _extract_write_targets(cmd.format(ws=WORKSPACE, out=out))
    assert out in targets, row
    allowed, _reason, _ = _check_sandbox(
        cmd.format(ws=WORKSPACE, out=out), "workspace-write", WORKSPACE
    )
    assert allowed is False, row


# ── ground truth: the shell really writes where the walk now points ─────────────────
GROUND_TRUTH_ROWS = (
    ("heredoc", "cp ./a d1 <<EOF", "d1", "/bin/sh"),
    ("tab-stripping heredoc", "cp ./a d2 <<-EOF", "d2", "/bin/sh"),
    # A here-string is a bash extension, so this row names a shell that has it. The
    # Linux runners' `/bin/sh` is dash, which refuses the line outright — measured on
    # this repo's own macOS host against `/bin/dash`, whose message is byte-identical
    # to the one CI produced (`rc=2 /bin/dash: 1: Syntax error: redirection
    # unexpected`), while a probe of the `<<EOF`, `<<-EOF` and `< /dev/null` rows under
    # the same dash created their destinations at `rc=0`. The row's *subject* is the
    # operator's word grammar, which both shells share; only the ground-truth run needs
    # a shell that can spell it.
    ("here-string", "cp ./a d3 <<< here", "d3", "/bin/bash"),
    ("input descriptor duplication", "cp ./a d4 <&0", "d4", "/bin/sh"),
    ("bare input redirect", "cp ./a d5 < /dev/null", "d5", "/bin/sh"),
    ("descriptor duplication after", "cp ./a d6 2>&1", "d6", "/bin/sh"),
    ("descriptor duplication before", "cp ./a 2>&1 d7", "d7", "/bin/sh"),
)


@pytest.mark.parametrize("row,script,dst,shell", GROUND_TRUTH_ROWS,
                         ids=[r for r, _, _, _ in GROUND_TRUTH_ROWS])
def test_the_shell_creates_the_destination_these_lines_name(row, script, dst, shell, tmp_path):
    """One file per row, in a directory this test creates, read back off disk.

    The predicate rows above are only a defect if the shell really writes the path the
    walk failed to name — this is that half, measured rather than asserted. Nothing here
    touches a host path: the cwd is `tmp_path`, the source is written by the test, and
    the destination is the name the row spells.

    A row whose shell is absent is skipped for that reason, which is also what keeps
    this honest on Windows (where neither `/bin/sh` nor `/bin/bash` exists) without
    asserting a platform instead of a fact.
    """
    if not os.path.exists(shell):
        pytest.skip(f"{shell} is not on this host")
    (tmp_path / "a").write_text("src\n")
    proc = subprocess.run([shell, "-c", script], cwd=tmp_path,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert (tmp_path / dst).exists(), f"{row}: rc={proc.returncode} {proc.stderr!r}"
    assert (tmp_path / dst).read_text() == "src\n", row
