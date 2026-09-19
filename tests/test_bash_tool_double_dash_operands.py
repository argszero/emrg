"""A `--` ends option parsing, so a `-`-led operand after it is a path and is named.

`_positional_args` has carried the sentence "a lone ``--`` ends option parsing" in its
docstring from the start, while the loop under that sentence skipped the ``--`` and went
on dropping every dash-led token — so the claim was false in exactly the case it was
written for (issue #1433). A destructive command whose operand is a file whose *own name*
looks like an option, which is the spelling `--` exists to express, named **nothing**;
and an empty target list is allowed by construction, because the loop that judges targets
never runs.

Measured with the real predicate on master `910a307c`, this function byte-identical at
`26449c59` where the fix was written: `rm -- -s` reported ``[]``, i.e. ALLOW at **both**
tiers. Ground truth, taken in a scratch directory on this host and read back off disk:
`printf x > ./-s; rm -- -s` is rc=0 with `./-s` gone; `printf x > ./--; rm -- --` is rc=0
with that file gone; `gzip -- -f` is rc=0 and `-f` has become `-f.gz`; `touch -- -t` is
rc=0 with `-t` created. Every one of those names really deletes or really writes, and each
is only an option *shape*.

This file pins the rule in three directions, because a fix for one of them can be wrong in
the others:

* the forms that must now name their operand, including the ones already named before the
  fix (a rule that only moved the hole would pass a one-row test);
* the spellings that must stay exactly as they were — an option's own value, which is
  consumed *before* the terminator test, and the limit where no `--` was written at all;
* the tier verdict the hole was measured in, because an empty target list is a hole only
  in that it is allowed.

Nothing here executes a command except the two ground-truth arms at the bottom, whose
arguments live in a `tmp_path` the test creates.
"""

import subprocess
import sys

import pytest

import emrg.tools.bash_tool as bash_tool
from emrg.tools.bash_tool import (
    _check_sandbox,
    _extract_write_targets,
    _positional_args,
)

# Outside every allowed root (the workspace root is the workdir, the OS temp root and the
# evolution data dir are the other two), and used only as an argument to the pure
# predicate — `_check_sandbox` `realpath`s a target and opens nothing.
OUTSIDE = "/outside/emrg"
WORKSPACE = "/workspace"


def test_the_predicate_is_the_one_the_tiers_read():
    """The walk and the tier check this file judges are the ones the tool layer calls.

    A test that reached a private re-reading would keep passing while the shipped guard
    changed, which is the failure this class of test exists to prevent. The row is an
    ordinary in-workspace removal, so it is allowed where writes are on.
    """
    allowed, reason, enforcement = _check_sandbox(
        f"rm {WORKSPACE}/f", "workspace-write", WORKSPACE
    )
    assert allowed is True, reason
    assert enforcement in ("partial", "full")


# ── naming forms: the operand after the terminator is a path ────────────────────────
#
# (row, command, named) — the dash-led rows are the fix; the absolute-operand row was
# already named before it, and is here so a rule that only *moved* the hole is caught.
NAMING_FORMS = (
    ("remover, flag-shaped name", "rm -- -s", ("-s",)),
    ("remover, a file named --", "rm -- --", ("--",)),
    ("remover, absolute operand", f"rm -- {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("rmdir, flag-shaped name", "rmdir -- -d", ("-d",)),
    ("compressor, flag-shaped name", "gzip -- -f", ("-f",)),
    ("creator, flag-shaped name", "touch -- -t", ("-t",)),
    # The terminator ends option parsing once; a second `--` is a file named `--`.
    ("terminator mid-list", "rm -- x -s -- y", ("x", "-s", "--", "y")),
)


@pytest.mark.parametrize(
    "row,cmd,named", NAMING_FORMS, ids=[row for row, _c, _n in NAMING_FORMS]
)
def test_every_naming_form_names_its_operand(row, cmd, named):
    """The assertion is on the *reported target*, not on the verdict alone.

    A verdict-only reading would leave the defect open in the direction that matters
    most here: a block message that names no path is a guard nobody can act on.
    """
    assert _extract_write_targets(cmd) == list(named), row


@pytest.mark.parametrize(
    "row,cmd,named", NAMING_FORMS, ids=[row for row, _c, _n in NAMING_FORMS]
)
def test_a_named_operand_is_refused_where_writes_are_off(row, cmd, named):
    """The tier the hole was measured in, now reading the operand it dropped.

    `read-only` refuses every named target but `/dev/null`, which is why this row is the
    one that was ALLOW before the fix. The message is asserted too, because a refusal
    that named nothing would be the same defect one layer up.
    """
    allowed, reason, _ = _check_sandbox(cmd, "read-only", WORKSPACE)
    assert allowed is False, f"{row}: read-only allowed a write"
    assert named[0] in (reason or ""), f"{row}: the refusal named {reason!r}"


@pytest.mark.parametrize(
    "row,cmd,named", NAMING_FORMS, ids=[row for row, _c, _n in NAMING_FORMS]
)
def test_an_operand_inside_the_workspace_is_allowed_where_writes_are_on(row, cmd, named):
    """Naming the operand must not refuse how the tool is normally used.

    Every row is run with the workspace root as the workdir, so each relative name
    resolves inside it and `workspace-write` allows it; the one absolute operand is
    outside, and the row is rewritten to sit inside for this arm.
    """
    inside = f"{WORKSPACE}/-s"
    allowed, reason, _ = _check_sandbox(cmd.replace(f"{OUTSIDE}/f", inside), "workspace-write",
                                        WORKSPACE)
    assert allowed is True, f"{row}: workspace-write refused an in-workspace write ({reason})"


# ── forms that stay unnamed: dropping a path here would be a false block ─────────────
STAYS_UNNAMED = (
    # No `--` was written, so the token really is option-shaped and its reading is
    # unknowable without the per-verb grammar this walk refuses to grow.
    ("remover, no terminator", "rm -s"),
    ("rmdir, no terminator", "rmdir -d"),
    # `gzip -f` reads stdin and writes stdout: the flag shape is doing what it says.
    ("compressor's own flag", "gzip -f"),
)


@pytest.mark.parametrize("row,cmd", STAYS_UNNAMED, ids=[row for row, _ in STAYS_UNNAMED])
def test_a_form_with_no_terminator_names_nothing(row, cmd):
    """The limit this fix leaves in place, pinned so a reader finds it recorded.

    `rm -s` is the same *shape* as `rm -- -s` and is deliberately still dropped: without
    a `--` there is no way to tell a flag from a file whose name looks like one, and
    naming the flag would refuse a spelling people really type. The measured cost is nil
    on this host (`rm -s` is a usage error, so nothing is deleted), and the two-row
    contrast with `rm -- -s` is what shows the fix is about the terminator.
    """
    assert _extract_write_targets(cmd) == [], row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, WORKSPACE)
        assert allowed is True, f"{row}: {tier} refused ({reason})"


def test_an_option_value_is_consumed_before_the_terminator_test():
    """`cp -t -- f` — the `--` here is *the value of `-t`*, not a terminator.

    The value is taken by the option before the terminator test is reached, so the
    operand list is `['f']` and the `--` is never seen. Asserted on the predicate rather
    than through a tier verdict, because the walk's `cp` branch reads the `-t` value
    separately and would report the target directory `--` — a true reading of that
    spelling, and not the question this row asks.
    """
    assert _positional_args(["-t", "--", "f"], 0) == ["f"]


def test_the_contrast_is_the_terminator_and_nothing_else():
    """The instrument's own control: one token differs between the two rows.

    Without this, a rule that named every dash-led token — or a fixture that never
    reached the rule — could leave the arms above green for the wrong reason. `rm -s`
    and `rm -- -s` differ by the `--` alone, and the walk must report the second.
    """
    assert _extract_write_targets("rm -s") == []
    assert _extract_write_targets("rm -- -s") == ["-s"]
    assert _check_sandbox("rm -s", "read-only", WORKSPACE)[0] is True
    assert _check_sandbox("rm -- -s", "read-only", WORKSPACE)[0] is False


# ── the mutation arm: a row that cannot be flipped is not a claim ────────────────────
def _positional_args_as_master_read_it(tokens, i, options_with_value=None):
    """The pre-fix loop, verbatim in shape: the `--` is skipped and drops nothing.

    Written out here rather than imported, because the arm must be able to *unfix* the
    walk it is testing: the value of the arm is that it restores the exact reading the
    hole was measured with, so a row that stayed refused after it would be refused by
    something else.
    """
    table = (
        bash_tool._OPTIONS_WITH_VALUE if options_with_value is None else options_with_value
    )
    out: list[str] = []
    skip_next = False
    for tok in bash_tool._args_after_command(tokens, i):
        if skip_next:
            skip_next = False
            continue
        if tok == "--":
            continue
        if tok.startswith("-") and tok != "-":
            if tok in table:
                skip_next = True
            continue
        out.append(tok)
    return out


def test_the_naming_rows_die_when_the_terminator_is_ignored_again():
    """Restore the pre-fix loop and every naming row must fall back to the ALLOW base.

    A row that stayed named would be named by something other than the terminator, and
    the fix would be claiming work it does not do.
    """
    original = bash_tool._positional_args
    try:
        for row, cmd, _named in NAMING_FORMS:
            assert _extract_write_targets(cmd), f"{row}: not named before the arm"
            assert _check_sandbox(cmd, "read-only", WORKSPACE)[0] is False, row
        bash_tool._positional_args = _positional_args_as_master_read_it
        named_after = {row: _extract_write_targets(cmd) for row, cmd, _n in NAMING_FORMS}
        gone = [row for row, _c, named in NAMING_FORMS
                if named_after[row] != [t for t in named if not t.startswith("-")]]
        assert not gone, f"still named with the terminator ignored: {gone}"
        assert _check_sandbox("rm -- -s", "read-only", WORKSPACE)[0] is True
    finally:
        bash_tool._positional_args = original


# ── ground truth, executed: the derivation the rule is built on ─────────────────────
@pytest.mark.skipif(sys.platform == "win32", reason="no rm/unlink semantics for `-s` names on Windows CI")
def test_rm_really_deletes_a_flag_shaped_name_after_the_terminator(tmp_path):
    """Drive the real `rm`, so the rule rests on what it does rather than on a manual.

    `tmp_path` is a directory the test creates, so nothing here can reach a host path,
    and the directory is read back off disk instead of assumed.
    """
    victim = tmp_path / "-s"
    victim.write_text("x")
    result = subprocess.run(
        ["rm", "--", "-s"], cwd=str(tmp_path),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert not victim.exists(), "rm -- -s did not delete the flag-shaped name"
    # ...and the walk names exactly that operand.
    assert _extract_write_targets("rm -- -s") == ["-s"]


@pytest.mark.skipif(sys.platform == "win32", reason="no rm semantics for `--` names on Windows CI")
def test_rm_really_deletes_a_file_named_double_dash(tmp_path):
    """The second `--` is a *file name*, which is why the walk keeps naming it.

    A loop that treated every `--` as a terminator would report nothing for this
    spelling and lose the same class of deletion the fix is about.
    """
    victim = tmp_path / "--"
    victim.write_text("x")
    result = subprocess.run(
        ["rm", "--", "--"], cwd=str(tmp_path),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert not victim.exists(), "rm -- -- did not delete the file named --"
    assert _extract_write_targets("rm -- --") == ["--"]
