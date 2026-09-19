"""`unlink` removes a file, and the walk named nothing for it.

`rm` and `rmdir` have been in the write-target walk from the start; `unlink` — the
POSIX way to remove one file, at `/usr/bin/unlink` on macOS and on Linux alike — was
not, so its operand was never named. An empty target list is allowed by construction
(the loop that judges targets never runs), which made `unlink` a completely
unclassified destructive command: measured on master `910a307c`, `unlink <outside>/f`,
`unlink -- <outside>/f`, `/usr/bin/unlink <outside>/f` and `unlink <protected daemon
file>` were each ALLOW at **both** tiers, while `rm` and `rmdir` on the same two paths
were refused in the same geometry. This file pins the closed hole in three directions,
because a fix for one of them can be wrong in the others:

* the **removing forms** name their operand, in every spelling the walk can see;
* the **no-op forms** (no operand at all) name nothing and stay allowed — refusing a
  command that removes nothing would be the false block this walk treats as the worse
  error;
* the **shared limit** — an option-shaped operand (`unlink -x`, which really does
  delete the file of that name) is dropped by `_positional_args` exactly as `rm -- -s`
  drops its own, so this fix inherits one general limit rather than adding a new one.

Ground truth, taken in a scratch directory on this host and read back off disk (BSD
`unlink`, usage line `unlink [--] file`, 2026-09-19): `unlink f.txt` really deletes it
(rc=0, gone); a missing operand reports an error and exits 0 having touched nothing;
`unlink -x` and `unlink --help` deleted files of those very names — this program takes
no options beyond `--`; and `unlink two1 two2` printed its usage line and deleted
**neither** file, so the single-operand form is the one that deletes and the
two-operand spelling is an error rather than a second deletion.

Nothing here executes a command: `_check_sandbox` is a pure predicate that `realpath`s
a path and opens nothing, so the protected path below is an *input* to a predicate
rather than something a test can damage. That matters here because most assertions are
negative ("the guard refuses X"), and a negative test is only harmless while the guard
works.
"""

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import _check_sandbox, _extract_write_targets

# Outside every allowed root (the workspace, the OS temp root, the evolution data
# dir), and only ever an argument to the pure predicate — never executed.
OUTSIDE = "/outside/emrg"

WORKSPACE = "/workspace"

# The daemon's own rant store: a protected file, and also only ever an input.
PROTECTED = "~/.emrg/rants.jsonl"

# (row, command, every path the walk must name, in order). The two-operand row names
# both operands deliberately: see the module docstring's ground truth — that spelling
# is a usage error, and over-naming it is the cheap side of the trade the `rm` branch
# already makes.
WRITE_FORMS = (
    ("single operand", f"unlink {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("after --", f"unlink -- {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("absolute path to the verb", f"/usr/bin/unlink {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("quoted operand", f"unlink '{OUTSIDE}/f'", (f"{OUTSIDE}/f",)),
    ("second command in a chain", f"cd /tmp && unlink {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("inside sh -c", f"sh -c 'unlink {OUTSIDE}/f'", (f"{OUTSIDE}/f",)),
    ("two operands", f"unlink {OUTSIDE}/a {OUTSIDE}/b", (f"{OUTSIDE}/a", f"{OUTSIDE}/b")),
)

# The same claim with the operand *inside* the workspace, where the two tiers answer
# differently — so it is named here and judged by its own test below.
IN_WORKSPACE = ("in-workspace operand", f"unlink {WORKSPACE}/f", (f"{WORKSPACE}/f",))

# (row, command) — a run that removes nothing and must stay allowed at both tiers.
NO_OP_FORMS = (
    ("bare", "unlink"),
    ("a flag alone", "unlink -f"),
)


@pytest.mark.parametrize(
    "row,cmd,named", WRITE_FORMS + (IN_WORKSPACE,), ids=[r for r, *_ in WRITE_FORMS + (IN_WORKSPACE,)]
)
def test_an_unlink_run_names_the_operand_it_removes(row, cmd, named) -> None:
    """The operand of `unlink` is the file the command deletes."""
    assert tuple(_extract_write_targets(cmd)) == named, row


@pytest.mark.parametrize("row,cmd,named", WRITE_FORMS, ids=[r for r, *_ in WRITE_FORMS])
def test_the_target_outside_the_workspace_is_refused_at_both_tiers(row, cmd, named) -> None:
    """…and naming it is what makes both tiers refuse, which is the point.

    The tiers refuse for their own reasons, so both are asserted: outside the workspace
    each of them blocks, and the block must name the path the command would delete — a
    guard whose message points at something else is a guard nobody can trust.
    """
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir=WORKSPACE)
        assert allowed is False, f"{row}: {tier} allowed a delete of {named[0]}"
        assert named[0] in reason, f"{row}: {tier} block does not name {named[0]}"


def test_an_in_workspace_operand_is_removed_by_read_only_alone() -> None:
    """The two tiers differ here, and that difference is the whole reason to fix this.

    `workspace-write` allows the delete: it guards the workspace *boundary*, and this
    is inside it. `read-only` refuses it — that tier exists to protect uncommitted
    work, and a file inside the workspace is exactly the work at risk.
    """
    cmd = f"unlink {WORKSPACE}/f"
    assert _extract_write_targets(cmd) == [f"{WORKSPACE}/f"]

    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir=WORKSPACE)
    assert allowed is True and reason is None

    allowed, reason, _ = _check_sandbox(cmd, "read-only", workdir=WORKSPACE)
    assert allowed is False
    assert "destructive write" in reason, reason


def test_unlink_cannot_touch_a_protected_daemon_file() -> None:
    """The failure this fix exists for, stated as the file it protects.

    `~/.emrg/rants.jsonl` is the host's rant store. Before the fix `unlink` on that
    path was ALLOW at both tiers.
    """
    allowed, reason, _ = _check_sandbox(
        f"unlink {PROTECTED}", "workspace-write", workdir=WORKSPACE
    )
    assert allowed is False
    assert "protected daemon file" in reason, reason

    allowed, reason, _ = _check_sandbox(f"unlink {PROTECTED}", "read-only", workdir=WORKSPACE)
    assert allowed is False
    assert "destructive write" in reason, reason


@pytest.mark.parametrize("row,cmd", NO_OP_FORMS, ids=[r for r, _ in NO_OP_FORMS])
def test_a_run_that_removes_nothing_names_nothing_and_stays_allowed(row, cmd) -> None:
    """With no operand there is nothing to delete — verified, not assumed.

    Ground truth above: a bare `unlink` prints its usage line and exits 0 having deleted
    nothing, so the walk naming a path here would refuse a no-op.
    """
    assert _extract_write_targets(cmd) == [], row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir=WORKSPACE)
        assert allowed is True, f"{row}: {tier} refused a no-op ({reason})"


def test_an_option_shaped_operand_is_one_general_limit_shared_with_rm() -> None:
    """The residual this fix inherits rather than introduces, pinned as a limit.

    Measured: `unlink -x` really does delete the file named `-x` (this program takes no
    options beyond `--`), and `_positional_args` drops any `-`-leading token — so the
    walk names nothing there. That is not a property of the remover branch: `rm -- -s`
    is the same shape on a verb that has been classified all along, and it names nothing
    either. Stated here so the next reader finds the boundary recorded rather than
    discovering it.
    """
    for cmd in ("unlink -x", "rm -- -s"):
        assert _extract_write_targets(cmd) == [], cmd
        assert _check_sandbox(cmd, "read-only", workdir=WORKSPACE)[0] is True, cmd


# ── mutation arms: a row that cannot be flipped is not a claim ──────────────


def test_the_write_rows_die_when_the_remover_set_forgets_unlink() -> None:
    """Take `unlink` back out of the set and every write row must go to the ALLOW base.

    A row that stayed refused would be refused by something else, not by the branch this
    file is about — the whole point being that before this fix these rows *were* allowed.
    """
    original = bash_tool._REMOVER_VERBS
    try:
        assert all(
            _check_sandbox(cmd, "read-only", workdir=WORKSPACE)[0] is False
            for _row, cmd, _named in WRITE_FORMS
        ), "a write row is not refused before the arm"
        bash_tool._REMOVER_VERBS = frozenset({"rm", "rmdir"})
        for row, cmd, _named in WRITE_FORMS:
            targets = _extract_write_targets(cmd)
            allowed = _check_sandbox(cmd, "read-only", workdir=WORKSPACE)[0]
            assert targets == [] and allowed is True, (
                f"{row} survives taking unlink out of the remover set — it does not "
                "depend on the branch it claims to test"
            )
    finally:
        bash_tool._REMOVER_VERBS = original


def test_membership_of_the_set_is_what_classifies_a_verb() -> None:
    """Force a non-remover into the set and a read must become a refusal.

    The accepting half of the boundary: without this arm, a build that treated every
    command as a remover would still pass the write rows above, and so would one whose
    refusal came from somewhere other than this branch.
    """
    original = bash_tool._REMOVER_VERBS
    read_row = f"cat {OUTSIDE}/f"
    try:
        assert _extract_write_targets(read_row) == []
        assert _check_sandbox(read_row, "read-only", workdir=WORKSPACE)[0] is True
        bash_tool._REMOVER_VERBS = original | {"cat"}
        assert _extract_write_targets(read_row) == [f"{OUTSIDE}/f"], (
            "with cat in the remover set its operand must be named"
        )
        assert _check_sandbox(read_row, "read-only", workdir=WORKSPACE)[0] is False, (
            "with cat in the remover set the read must be refused — otherwise nothing "
            "about the write rows depends on this branch"
        )
    finally:
        bash_tool._REMOVER_VERBS = original


def test_the_no_op_rows_depend_on_the_operand_list() -> None:
    """Blind the operand reader and the no-op row must become a refusal.

    The bare spelling is allowed because the walk finds no operand, not because the
    branch is skipped — so a rule that invented an operand out of the verb alone would
    refuse a command that deletes nothing, and this arm makes that flip explicit.
    """
    original = bash_tool._positional_args
    try:
        assert _check_sandbox("unlink", "read-only", workdir=WORKSPACE)[0] is True
        bash_tool._positional_args = lambda *a, **k: [f"{OUTSIDE}/invented"]
        targets = _extract_write_targets("unlink")
        allowed = _check_sandbox("unlink", "read-only", workdir=WORKSPACE)[0]
        assert targets == [f"{OUTSIDE}/invented"] and allowed is False, (
            "the bare spelling must follow the operand list, not the verb alone"
        )
    finally:
        bash_tool._positional_args = original
