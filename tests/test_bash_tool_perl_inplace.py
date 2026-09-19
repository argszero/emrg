"""`perl -i` rewrites its operands in place — the member the family was missing.

`sed -i` has been in the walk since #1162 and `truncate` / `tee` / `shred` since
too, but `perl -i` is the same destructive write in a different program: it
replaces each file operand with the rewritten text and, with a suffix, leaves the
original beside it. The walk named nothing for it, and an empty target list is
allowed by construction — the loop that judges targets never runs.

Measured on this host (perl 5.34.1, 2026-09-19), in a scratch tree with every
file's bytes read back off disk:

* `perl -i -pe 's/a/b/' f`   → rc=0, `f` is `baa`, no backup;
* `perl -pi -e 's/a/b/' h`   → rc=0, `h` is `baa`;
* `perl -i.bak -pe 's/a/b/' g` → `g` is `baa` **and** `g.bak` holds the original;
* `perl -i -ne 'print' f3`   → rc=0, `f3` is `qaa`;
* `perl -pe 's/a/b/' f4`     → **unchanged** (no `-i`: a filter writes to stdout);
* `perl -i -p script.pl f`   → `f` is `baa`, `script.pl` keeps its bytes — with no
  `-e` the first operand is the *program*;
* `perl -i -p script.pl a b` → both `a` and `b` are rewritten;
* `perl -i -pe 's/a/b/' -`   → `Can't open -: No such file or directory`, nothing
  written, which is why a lone `-` names no target.

Against a protected daemon path the predicate answered ALLOW/ALLOW at both tiers
with an empty target list before the fix (the same fail-open #1398 and the
compressor family #1418 had).

Three directions this file pins, because a fix for one can be wrong in the other
two:

* the **write forms** name their operand, in every cluster and suffix spelling;
* the **read forms** (a bare `perl`, `-e`, `-n`/`-p` without `-i`) name nothing and
  stay allowed — refusing a filter would be the false block this walk treats as
  the worse error;
* the **program text** is never named, in either spelling (`-e PROG` / `-pe PROG`
  in the next token, `-ePROG` attached), and neither is the program *file* of
  `perl -i -p script.pl f`.

Nothing here executes a command. `_check_sandbox` is a pure predicate — it
`realpath`s a path and opens nothing — and `_extract_write_targets` only parses,
so the protected path below is an *input to a predicate* rather than something a
test can damage. That matters more than usual here: most assertions are negative
("the guard refuses X"), and a negative test is only harmless while the guard
works.
"""

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import (
    _check_sandbox,
    _extract_write_targets,
)

# Outside every allowed root (the workspace, the OS temp root, the evolution data
# dir) and used only as an argument to the pure predicate — never executed.
OUTSIDE = "/outside/emrg"

# The daemon's own rant store: a protected file, and also only ever an input.
PROTECTED = "~/.emrg/rants.jsonl"

# (row, command, every path the walk must name). A tuple, because a run with two
# operands really rewrites two files, and an exact tuple because naming the
# program text as well is a different defect.
WRITE_FORMS = (
    ("-i -pe", f"perl -i -pe 's/a/b/' {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("-pi -e", f"perl -pi -e 's/a/b/' {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("-i.bak -pe", f"perl -i.bak -pe 's/a/b/' {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("-i -ne", f"perl -i -ne 'print' {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("-i -e spaced program", f"perl -i -e 's/a/b/' {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("-i -w -pe", f"perl -i -w -pe 's/a/b/' {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("-0 -i -pe", f"perl -0 -i -pe 's/a/b/' {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("-Mstrict -i -pe", f"perl -Mstrict -i -pe 's/a/b/' {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # Two operands: both are rewritten, so both are named.
    ("two operands", f"perl -i -e 's/a/b/' {OUTSIDE}/a {OUTSIDE}/b",
     (f"{OUTSIDE}/a", f"{OUTSIDE}/b")),
    # `--` ends option parsing (`_positional_args`' rule), so what follows is an
    # operand even though it starts with a dash.
    ("after --", f"perl -i -pe 's/a/b/' -- {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
)

# (row, command) — a run that writes nothing and must stay allowed.
READ_FORMS = (
    ("no -i", f"perl -pe 's/a/b/' {OUTSIDE}/f"),
    ("-e only", f"perl -e 'print' {OUTSIDE}/f"),
    ("-ne only", f"perl -ne 'print' {OUTSIDE}/f"),
    ("-I dir, no -i", f"perl -I {OUTSIDE}/lib -e 'print' {OUTSIDE}/f"),
    ("-c syntax check", f"perl -c {OUTSIDE}/f"),
    ("script file", f"perl {OUTSIDE}/script.pl {OUTSIDE}/f"),
    ("lone dash", f"perl -i -pe 's/a/b/' -"),
)

# (row, command, named) — the program is not a path, and the two ways it can sit
# in an operand position are both pinned.
PROGRAM_ROWS = (
    # The program text *contains* an outside path, so a walk that mistook the
    # program for an operand would name it — the `sed` script defect.
    ("program text with a path",
     f"perl -i -pe 's|{OUTSIDE}/x|y|' {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # With no `-e`, the first operand is the program (measured): it is read, not
    # rewritten, so naming it would point the block at a file the command only
    # opens.
    ("program file", f"perl -i -p {OUTSIDE}/script.pl {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("program file, two args",
     f"perl -i -p {OUTSIDE}/script.pl {OUTSIDE}/a {OUTSIDE}/b",
     (f"{OUTSIDE}/a", f"{OUTSIDE}/b")),
    # The program file alone rewrites nothing.
    ("program file only", f"perl -i -p {OUTSIDE}/script.pl", ()),
)


@pytest.mark.parametrize("row,cmd,named", WRITE_FORMS, ids=[r for r, *_ in WRITE_FORMS])
def test_a_perl_inplace_run_names_every_operand_it_rewrites(row, cmd, named) -> None:
    """The operand of `perl -i` is the file it rewrites."""
    assert tuple(_extract_write_targets(cmd)) == named, row


@pytest.mark.parametrize("row,cmd,named", WRITE_FORMS, ids=[r for r, *_ in WRITE_FORMS])
def test_both_tiers_refuse_an_inplace_run_that_leaves_the_workspace(row, cmd, named) -> None:
    """…and naming it is what makes both tiers refuse, which is the point.

    The two tiers refuse for their own reasons (`read-only` names the destructive
    write, `workspace-write` the path outside), so both are asserted.
    """
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is False, f"{row}: {tier} allowed a write to {named[0]}"
        assert named[0] in reason, f"{row}: {tier} block does not name {named[0]}"


@pytest.mark.parametrize("row,cmd", READ_FORMS, ids=[r for r, _ in READ_FORMS])
def test_a_run_without_the_flag_names_nothing_and_stays_allowed(row, cmd) -> None:
    """Without `-i` a perl run is a filter: it writes to stdout, never to a file."""
    assert _extract_write_targets(cmd) == [], row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is True, f"{row}: {tier} refused a read ({reason})"


@pytest.mark.parametrize("row,cmd,named", PROGRAM_ROWS, ids=[r for r, *_ in PROGRAM_ROWS])
def test_the_program_is_never_named_as_a_path(row, cmd, named) -> None:
    """A block whose message names the program text is a block nobody can trust.

    `sed`'s branch already answers this question for its script; perl hides the
    program in two places (`-e PROG` / `-pe PROG` in the next token, `-ePROG`
    attached), and with no `-e` at all the first operand *is* the program.
    """
    assert tuple(_extract_write_targets(cmd)) == named, row


def test_the_inplace_form_cannot_touch_a_protected_daemon_file() -> None:
    """The failure this fix exists for, stated as the file it protects.

    `~/.emrg/rants.jsonl` is the host's rant store. Before the fix `perl -i` on
    that path was ALLOW at both tiers; the read form of the same command stays a
    read of the same file.
    """
    allowed, reason, _ = _check_sandbox(
        f"perl -i -pe 's/a/b/' {PROTECTED}", "workspace-write", workdir="/workspace"
    )
    assert allowed is False
    assert "protected daemon file" in reason, reason

    allowed, reason, _ = _check_sandbox(
        f"perl -i -pe 's/a/b/' {PROTECTED}", "read-only", workdir="/workspace"
    )
    assert allowed is False
    assert "destructive write" in reason, reason

    assert _check_sandbox(
        f"perl -pe 's/a/b/' {PROTECTED}", "workspace-write", workdir="/workspace"
    )[0] is True


# ── mutation arms: a row that cannot be flipped is not a claim ──────────────


def test_the_write_rows_die_when_the_flag_reader_stops_seeing_the_flag() -> None:
    """Blind the flag reader and every write row must go back to the ALLOW base.

    A row that stayed refused would be refused by something else, not by the
    branch this file is about.
    """
    original = bash_tool._perl_inplace_flag
    try:
        assert all(
            _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False
            for _row, cmd, _n in WRITE_FORMS
        ), "a write row is not refused before the arm"
        bash_tool._perl_inplace_flag = lambda _tok: False
        for row, cmd, _named in WRITE_FORMS:
            targets = _extract_write_targets(cmd)
            allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
            assert targets == [] and allowed is True, (
                f"{row} survives blinding the flag reader — it does not depend on "
                "the branch it claims to test"
            )
    finally:
        bash_tool._perl_inplace_flag = original


def test_the_flag_reader_is_what_spares_the_read_rows() -> None:
    """Force the flag reader open and a filter must become a refusal.

    The accepting half of the boundary: without this arm, a fix that treated
    every `perl` run as a write would still pass the write rows.
    """
    original = bash_tool._perl_inplace_flag
    filter_row = f"perl -pe 's/a/b/' {OUTSIDE}/f"
    try:
        assert _extract_write_targets(filter_row) == []
        assert _check_sandbox(filter_row, "read-only", workdir="/workspace")[0] is True
        bash_tool._perl_inplace_flag = lambda _tok: True
        assert _extract_write_targets(filter_row) == [f"{OUTSIDE}/f"], (
            "with the flag reader forced open the filter row must name its file"
        )
        assert _check_sandbox(filter_row, "read-only", workdir="/workspace")[0] is False, (
            "with the flag reader forced open the filter row must be refused — "
            "otherwise nothing about it depends on the flag reader"
        )
    finally:
        bash_tool._perl_inplace_flag = original


def test_the_program_handling_holds_both_spellings_of_the_program() -> None:
    """Both directions of the program rule, each with the flip it really causes.

    The two spellings are kept out of the target list by two different pieces of
    the same rule, so each is blinded in the direction that makes it visible:

    * an **attached** program (`-ePROG`) leaves the file operand alone in the
      token stream, so blinding the reader loses the operand entirely — it is the
      reader, not the `[1:]` fallback, that finds it;
    * the **program file** (`-i -p script.pl f`, no `-e` at all) is dropped by the
      fallback, so making the reader claim every token names `script.pl` as well —
      the `sed`-script defect, measured as a real flip rather than asserted.
    """
    original = bash_tool._perl_carries_the_program
    attached = f"perl -i -e's/a/b/' {OUTSIDE}/f"
    program_file = f"perl -i -p {OUTSIDE}/script.pl {OUTSIDE}/f"
    try:
        assert _extract_write_targets(attached) == [f"{OUTSIDE}/f"]
        bash_tool._perl_carries_the_program = lambda _tok: False
        assert _extract_write_targets(attached) == [], (
            "an attached program must be what the reader recognises, or the "
            "fallback drops the real operand with it"
        )

        assert _extract_write_targets(program_file) == [f"{OUTSIDE}/f"]
        bash_tool._perl_carries_the_program = lambda _tok: True
        assert _extract_write_targets(program_file) == [
            f"{OUTSIDE}/script.pl", f"{OUTSIDE}/f"
        ], (
            "with the reader claiming every token, the program file must be named "
            "too — otherwise the fallback is not what keeps it out"
        )
    finally:
        bash_tool._perl_carries_the_program = original
