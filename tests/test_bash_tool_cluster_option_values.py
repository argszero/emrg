"""A value-taking letter **inside a cluster** eats the next word it declares.

getopt lets several short options share one token, and the option that takes a
value may be the last letter of that token — so the word after it is its value,
not an operand. The operand reader treated every cluster as a bag of flags, which
does not merely over-name: for a verb whose destination is the *last operand*, it
displaces the destination by one word and names the option's value instead.

Measured against master `edba48ca`'s own code (predicate only, nothing executed,
`workspace-write`, destination outside every allowed root) — the four rows that
were ALLOW **and named the value**:

    cp x <outside>/dst -aS .bak               → ['.bak']  ALLOW
    mv -va x <outside>/m -vS .bak             → ['.bak']  ALLOW
    ln -s x <outside>/l -vS .bak              → ['.bak']  ALLOW
    install -m 644 x <outside>/i -vS .bak     → ['.bak']  ALLOW

The neighbours that leave no separate word to misread were refused in the same
geometry — `-aS.bak` (attached), `-rv` / `-avT` (no value letter in the cluster),
`ln -sS .bak x <outside>/l` and `install -Sm 644 x <outside>/i` (the value word is
not last). That pair is the discriminator this file is built on: the hole is the
**cluster spelling**, not the verb and not the option.

The GNU grammar the reader now follows is read from the project's own source
rather than remembered (fetched 2026-09-20, `raw.githubusercontent.com/coreutils/
coreutils/master/src/{cp,mv,ln,install}.c`): `suffix` and `target-directory` are
``required_argument`` in all four, `mode`/`owner`/`group` in `install`. That is why
the fixed reading is the *GNU* one — and why it cannot be measured on this host:
BSD `cp` has no `--suffix` and rejects the letter, measured here as `cp -aSb` →
``cp: illegal option -- b`` and `cp --suffix .bak` → ``cp: illegal option -- -``,
i.e. the command fails and writes nothing. Ubuntu is the CI leg that runs GNU, so
the rows below pin the reading that leg realises; on BSD they pin the refusal of a
command that would not have run either way, which is the cheap direction.

This file lives apart from `test_bash_tool_sandbox.py` for the same reason
`test_bash_tool_option_destinations.py` does: it owns one family, and its rows
assert the whole target tuple so a value can never stand where the destination
belongs unnoticed.
"""

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import _check_sandbox, _extract_write_targets

# Outside every allowed root (workspace, OS temp root, the evolution data dir) and
# used only as an argument to the pure predicate — never executed, never opened.
OUTSIDE = "/outside/emrg"

# (row, command, the target list it must name) — the hole rows and their controls
# in one table, because the controls are what make the hole rows evidence about
# the cluster spelling rather than about the verb.
CLUSTER_VALUES = (
    # The hole: a value-taking letter ends the cluster, so the word after it is
    # the option's value and the destination is the operand before it.
    ("cp -aS", f"cp x {OUTSIDE}/dst -aS .bak", (f"{OUTSIDE}/dst",)),
    ("cp -a -aS", f"cp -a x {OUTSIDE}/dst -aS .bak", (f"{OUTSIDE}/dst",)),
    ("mv -vS", f"mv -va x {OUTSIDE}/m -vS .bak", (f"{OUTSIDE}/m",)),
    ("ln -vS", f"ln -s x {OUTSIDE}/l -vS .bak", (f"{OUTSIDE}/l",)),
    ("install -vS", f"install -m 644 x {OUTSIDE}/i -vS .bak", (f"{OUTSIDE}/i",)),
    # The long spelling of the same option, which was already read.
    ("cp --suffix", f"cp --suffix .bak x {OUTSIDE}/dst", (f"{OUTSIDE}/dst",)),
    # Controls: the value rides in the token, the cluster holds no value letter,
    # or the value word is not last. None of these may lose the destination.
    ("cp -aS attached", f"cp -a x {OUTSIDE}/dst -aS.bak", (f"{OUTSIDE}/dst",)),
    ("cp -rv", f"cp -a x {OUTSIDE}/dst -rv", (f"{OUTSIDE}/dst",)),
    ("cp -avT", f"cp -avT x {OUTSIDE}/dst", (f"{OUTSIDE}/dst",)),
    ("cp -St suffix", f"cp -St x {OUTSIDE}/dst", (f"{OUTSIDE}/dst",)),
    ("cp -vt dir", f"cp -a x {OUTSIDE}/dst -vt {OUTSIDE}", (OUTSIDE,)),
    ("ln -sS value first", f"ln -sS .bak x {OUTSIDE}/l", (f"{OUTSIDE}/l",)),
    ("install -Sm", f"install -Sm 644 x {OUTSIDE}/i", (f"{OUTSIDE}/i",)),
    ("install -vS before", f"install -m 644 -vS .bak x {OUTSIDE}/i", (f"{OUTSIDE}/i",)),
    ("mv -at dir", f"mv -at {OUTSIDE} x", (OUTSIDE,)),
    # The same read on a verb that creates *every* operand: the mode must stop
    # being named as a directory to create (verdict unchanged, list corrected).
    ("mkdir -pm", f"mkdir -pm 755 {OUTSIDE}/d", (f"{OUTSIDE}/d",)),
)

_ROW_IDS = [row for row, _c, _n in CLUSTER_VALUES]

# The rows that were ALLOW at `workspace-write` while naming the option's value.
# Listed separately so the arm below can name exactly what it must kill.
WAS_A_HOLE = (
    (f"cp x {OUTSIDE}/dst -aS .bak", ".bak", f"{OUTSIDE}/dst"),
    (f"mv -va x {OUTSIDE}/m -vS .bak", ".bak", f"{OUTSIDE}/m"),
    (f"ln -s x {OUTSIDE}/l -vS .bak", ".bak", f"{OUTSIDE}/l"),
    (f"install -m 644 x {OUTSIDE}/i -vS .bak", ".bak", f"{OUTSIDE}/i"),
)


def _allowed(cmd: str, tier: str = "workspace-write") -> bool:
    return _check_sandbox(cmd, tier, workdir="/workspace")[0]


@pytest.mark.parametrize("row,cmd,named", CLUSTER_VALUES, ids=_ROW_IDS)
def test_the_operand_reader_names_the_destination_a_cluster_hid(row, cmd, named):
    """The whole tuple, so a *value* cannot stand where the destination belongs.

    Naming `.bak` is worse than naming nothing: nothing is an ALLOW by
    construction (the loop that judges targets never runs), but a misnamed token
    is an ALLOW *and* a sentence pointing at a path the command never writes.
    """
    assert tuple(_extract_write_targets(cmd)) == named


@pytest.mark.parametrize("row,cmd,named", CLUSTER_VALUES, ids=_ROW_IDS)
def test_both_tiers_refuse_the_cluster_rows_that_leave_the_workspace(row, cmd, named):
    """Both tiers, and the refusal names the real destination."""
    for tier in ("read-only", "workspace-write"):
        allowed, reason, enforcement = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is False, f"{tier} allowed {cmd!r}"
        assert named[0] in reason, f"{tier} block for {cmd!r} does not name {named[0]!r}"
        assert enforcement == "partial", (
            "an interpreter still writes anywhere it likes - the label must stay "
            "`partial` (issue #1398)"
        )


def test_the_value_a_cluster_declares_is_never_named_as_the_destination():
    """The shape of the defect, stated without leaning on the tuple table.

    Each hole row named a token that is an option's *value* — the suffix, not a
    path. This asserts the destination is reported *and* the value is absent, so
    a later reader cannot satisfy the table above by naming both.
    """
    for cmd, value, destination in WAS_A_HOLE:
        targets = _extract_write_targets(cmd)
        assert targets == [destination], cmd
        assert value not in targets, f"{cmd!r} named its option's value {value!r}"


def test_the_hole_rows_need_the_cluster_reader():
    """Arm `_short_cluster_option` off: the four rows must go back to the hole.

    Without this, a passing tuple could come from anywhere — a table entry, a verb
    list, a different helper. The arm names the one piece of code the rows claim to
    be evidence about, and the controls are asserted to *survive* it, so it
    discriminates rather than blanket-disabling the walk.

    The tier is `workspace-write` on purpose: `read-only` refuses every write, so
    a hole is invisible there — which is exactly how this class hides.
    """
    for cmd, _value, _destination in WAS_A_HOLE:
        assert _allowed(cmd) is False, cmd
    saved = bash_tool._short_cluster_option
    bash_tool._short_cluster_option = lambda *_a, **_k: None
    try:
        for cmd, value, _destination in WAS_A_HOLE:
            targets = _extract_write_targets(cmd)
            assert _allowed(cmd) is True, (
                f"{cmd!r} is still refused with the cluster reader disabled - it "
                "does not depend on the spelling it claims to test"
            )
            assert value in targets, (
                f"{cmd!r} must fall back to naming its option's value {value!r}"
            )
        # Not a blanket off-switch: the attached form still names its destination.
        assert _allowed(f"cp -a x {OUTSIDE}/dst -aS.bak") is False
    finally:
        bash_tool._short_cluster_option = saved


def test_the_cluster_letters_come_from_the_verbs_own_table():
    """Prune the verb's table and its cluster row must go back to the hole.

    The walk reads the *per-verb* table (`_VERB_OPTIONS_WITH_VALUE`), so removing
    `-S`/`--suffix` from `cp`'s entry must lose the `cp -aS` row while leaving the
    attached control refused — the two facts the rule is built from, armed one at
    a time.
    """
    cmd = f"cp x {OUTSIDE}/dst -aS .bak"
    assert _allowed(cmd) is False
    original = bash_tool._VERB_OPTIONS_WITH_VALUE["cp"]
    bash_tool._VERB_OPTIONS_WITH_VALUE["cp"] = frozenset(
        opt for opt in original if opt not in ("-S", "--suffix")
    )
    try:
        assert _allowed(cmd) is True, (
            "the cluster row must depend on the verb's own table, not on a "
            "hard-coded letter"
        )
        assert _extract_write_targets(cmd) == [".bak"]
        # The attached form never depended on the cluster walk; it stays refused.
        assert _allowed(f"cp -a x {OUTSIDE}/dst -aS.bak") is False
    finally:
        bash_tool._VERB_OPTIONS_WITH_VALUE["cp"] = original


def test_the_flat_table_is_not_read_as_a_cluster_grammar():
    """`rm -is x` must not lose its operand to a table shared across verbs.

    The flat `_OPTIONS_WITH_VALUE` is a union — its letters are not every reading
    verb's options — so the cluster walk is deliberately scoped to the per-verb
    tables. This row is the reason: read as a grammar, `-is` would make `x` the
    value of `-s` and the removal would name nothing at all.
    """
    assert _extract_write_targets("rm -is /outside/emrg/x") == ["/outside/emrg/x"]
    assert _allowed("rm -is /outside/emrg/x", "read-only") is False
