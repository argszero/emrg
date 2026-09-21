"""`brotli` names the path it writes (issue #1420's last installed member).

Issue #1420 is the boundary of a family enumerated **by name**: `_COMPRESSOR_VERBS`
covers exactly the verbs on it, and every compressor off the list keeps the hole it
closes — an empty target list, which both tiers allow by construction because the loop
that judges targets never runs. `brotli` was the last member of that list that is
actually installed on a host, so it is the last one whose ground truth could be
measured rather than argued; the comment above `_BROTLI_VERBS` carries the table, and
this file carries the rows.

**Why it is not a name in `_COMPRESSOR_VERBS`.** Measured on this host's own binary
(`brotli 1.1.0`, the copy the git installation puts on `PATH`), one fresh directory
per row with only the input present and the listing read back off disk: `brotli f`
writes `f.br` **beside** the operand and keeps `f`, where a family member rewrites the
operand in place. That is `lz4`'s and `pzstd`'s shape, and it is the reason a name in
the family would be a *wrong* reading rather than a thin one: `brotli -o out.br f`
leaves `f` untouched, so the family's operand rule would name a file the run only
reads.

The sibling claim is what makes naming the operand sound in both directions — the
derived path lands in the operand's own directory and in no other, so the operand
names the directory the write happens in. `brotli f g` derives one sibling per
operand (measured), which is why the rule names them all rather than the last.

Nothing here executes a command. `_check_sandbox` is a pure predicate (it `realpath`s a
path and opens nothing) and `_extract_write_targets` only parses, so `OUTSIDE` and the
protected path below are arguments to a predicate rather than things a test can damage.
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

# The daemon's own rant store: a protected file, and the one a sandboxed task must
# not be able to destroy. Also only ever an input to the predicate.
PROTECTED = "~/.emrg/rants.jsonl"

# (row, command, every path the walk must name — a tuple, because a multi-input run
# really derives a sibling for each operand).
WRITE_FORMS = (
    ("default", f"brotli {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("keep", f"brotli -k {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("force", f"brotli -f {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # `-s` is `--squash` (drop the destination when it is larger), not a read and not
    # a suffix: measured, it writes `f.br` exactly as the bare form does.
    ("squash", f"brotli -s {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # `--` ends option parsing, and the file after it is compressed like any other
    # operand — measured, `brotli -- f` writes `f.br`.
    ("terminator", f"brotli -- {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # The long values this binary takes **attached**: measured, `brotli --lgwin=24 f`
    # and `brotli --quality=11 f` write `f.br`, and the single token is not an operand.
    ("long window", f"brotli --lgwin=24 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("long quality", f"brotli --quality=11 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # The two spaced-number options. Their values are numbers, so naming the *value*
    # would point the guard at a token that is not a path — the assert here is that
    # the operand is what is named.
    ("quality", f"brotli -q 11 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("window", f"brotli -w 24 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("suffix", f"brotli -S .zz {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # `-j` removes the operand once the sibling is written, so the operand is the
    # path at risk as well as the directory the write lands in.
    ("remove source", f"brotli -j {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # Decompression writes too, and lands `f` beside `f.br`.
    ("decompress", f"brotli -d {OUTSIDE}/f.br", (f"{OUTSIDE}/f.br",)),
    # Every operand derives its own sibling — measured, `brotli f g` writes `f.br`
    # and `g.br` — so both are named.
    ("two operands", f"brotli {OUTSIDE}/f {OUTSIDE}/g",
     (f"{OUTSIDE}/f", f"{OUTSIDE}/g")),
    # `-D`'s value is the dictionary: a **read**, and not a target.
    ("dictionary", f"brotli -D {OUTSIDE}/dict.bin {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
)

# (row, command, every path the walk must name) — the destination option.
DESTINATION_FORMS = (
    ("spaced", f"brotli -o {OUTSIDE}/out.br {OUTSIDE}/f", (f"{OUTSIDE}/out.br",)),
    ("long equals", f"brotli --output={OUTSIDE}/out.br {OUTSIDE}/f",
     (f"{OUTSIDE}/out.br",)),
    # The destination wins wherever it stands — measured, `brotli f -o out.br`
    # creates `out.br` and does not derive `f.br`.
    ("after the operand", f"brotli {OUTSIDE}/f -o {OUTSIDE}/out.br",
     (f"{OUTSIDE}/out.br",)),
)

# (row, command) — the measured read spellings, which a name-only fix would refuse.
READ_FORMS = (
    ("stdout short", f"brotli -c {OUTSIDE}/f"),
    ("stdout long", f"brotli --stdout {OUTSIDE}/f"),
    ("test short", f"brotli -t {OUTSIDE}/f.br"),
    ("test long", f"brotli --test {OUTSIDE}/f.br"),
    # `-l` is not an option here at all — measured, rc=1 "invalid argument -l". It is
    # in the read letters for the reason the family gives for `compress -t` and
    # `pzstd -l`: a letter the program rejects cannot hide a write, while refusing it
    # would refuse a run that was going to fail anyway.
    ("rejected -l", f"brotli -l {OUTSIDE}/f"),
    # A bare `-` is the stream: measured, `brotli -` is rc=0 with no file created.
    ("stream alone", f"brotli -"),
)


@pytest.mark.parametrize("row,cmd,target", WRITE_FORMS, ids=[r for r, *_ in WRITE_FORMS])
def test_a_brotli_write_form_names_the_path_it_creates(row, cmd, target) -> None:
    """The named path is the one the run really creates."""
    assert _extract_write_targets(cmd) == list(target), row


@pytest.mark.parametrize("row,cmd,target", WRITE_FORMS, ids=[r for r, *_ in WRITE_FORMS])
def test_a_brotli_write_form_is_refused_at_both_tiers(row, cmd, target) -> None:
    """…and naming it is what makes both tiers refuse, which is the point."""
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is False, f"{row}: {tier} allowed a write to {target}"
        assert reason, row


@pytest.mark.parametrize("row,cmd,target", DESTINATION_FORMS,
                         ids=[r for r, *_ in DESTINATION_FORMS])
def test_the_destination_option_names_the_destination_and_not_the_operand(
        row, cmd, target) -> None:
    """`-o` is an option-position write no operand rule can reach.

    Asserted as the *whole* target list, because the second half of the claim is that
    the operand is **not** named: while `-o` is spelled the operands are reads, so
    naming them as well would refuse a run that writes only where it was told to.
    """
    assert _extract_write_targets(cmd) == list(target), row


@pytest.mark.parametrize("row,cmd", READ_FORMS, ids=[r for r, _ in READ_FORMS])
def test_a_brotli_read_form_names_nothing_and_stays_allowed(row, cmd) -> None:
    """The measured read spellings, which a name-only fix would refuse."""
    assert _extract_write_targets(cmd) == [], row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is True, f"{row}: {tier} refused a read ({reason})"


def test_an_outside_operand_beside_a_destination_inside_stays_allowed() -> None:
    """The row that separates this verb from the family it is not in.

    `brotli -o out.br <outside>/in` writes only `out.br`, so the outside path is a
    **read**. A rule that named the operands as well — what adding `brotli` to
    `_COMPRESSOR_VERBS` would have done — refuses it, and this is the measurement that
    keeps that reading out of the branch.
    """
    cmd = f"brotli -o out.br {OUTSIDE}/in"
    assert _extract_write_targets(cmd) == ["out.br"]
    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir="/workspace")
    assert allowed is True, f"refused a run that only reads outside ({reason})"


def test_only_the_stream_operand_is_dropped_not_the_file_beside_it() -> None:
    """`brotli - f` still derives `f.br`; `brotli -` alone creates nothing.

    Measured on this host, one fresh directory per row: `brotli -` is rc=0 and creates
    no file, while `brotli - f` is rc=0 and creates `f.br`. So the drop is per operand,
    as in the family — dropping the whole run would have been the hole, and naming the
    token would have refused a stream.
    """
    assert _extract_write_targets(f"brotli - {OUTSIDE}/f") == [f"{OUTSIDE}/f"]
    assert _extract_write_targets(f"brotli {OUTSIDE}/f -") == [f"{OUTSIDE}/f"]


def test_the_sibling_claim_is_what_keeps_the_inside_case_allowed() -> None:
    """Naming the operand is sound *because* the sibling never leaves its dir.

    A rule that blocked `brotli f` outright would pass every write test above and be
    wrong: the write lands beside the operand, so an operand inside the workspace is an
    ordinary write. Asserted against the default form and the destination form, since
    the second names a different path.
    """
    for cmd in ("brotli /workspace/f", "brotli -o out.br /workspace/f"):
        assert _extract_write_targets(cmd), cmd
        allowed, reason, _ = _check_sandbox(cmd, "workspace-write",
                                            workdir="/workspace")
        assert allowed is True, f"{cmd}: refused an inside write ({reason})"


def test_the_default_form_cannot_touch_a_protected_daemon_file() -> None:
    """The failure this rule exists for, stated as the file it protects.

    Before the rule `brotli` on that path was ALLOW at both tiers with an empty target
    list. The two tiers now refuse it for their own reasons, which is why both are
    asserted rather than just "not allowed".
    """
    allowed, reason, _ = _check_sandbox(f"brotli {PROTECTED}", "workspace-write",
                                        workdir="/workspace")
    assert allowed is False
    assert "protected daemon file" in reason, reason

    allowed, reason, _ = _check_sandbox(f"brotli {PROTECTED}", "read-only",
                                        workdir="/workspace")
    assert allowed is False
    assert "destructive write" in reason, reason

    # …while the read form of the same command stays a read of the same file.
    assert _check_sandbox(f"brotli -c {PROTECTED}", "workspace-write",
                          workdir="/workspace")[0] is True


def test_the_destination_spellings_this_binary_rejects_are_read_anyway() -> None:
    """Two over-names, in the direction this walk prefers, pinned rather than hidden.

    Measured, `brotli --output out.br f` and `brotli -oout.br f` are both rc=1 with
    nothing created ("must pass the parameter as --output=value", "expected parameter
    for argument -o"), so the destination is named for a run that was going to fail.
    That is the reading the shared option-destination extractor gives every verb — it
    reads both long forms and the attached short one because a getopt-style parser
    accepts them — and it is kept rather than special-cased: not reading a spelling
    would miss a real write on a build that takes it.
    """
    assert _extract_write_targets(f"brotli --output out.br {OUTSIDE}/f") == ["out.br"]
    assert _extract_write_targets(f"brotli -oout.br {OUTSIDE}/f") == ["out.br"]


def test_brotli_is_read_by_a_rule_of_its_own_not_by_a_name_in_the_family() -> None:
    """The two tables are disjoint, and that is the branch's whole argument.

    If `brotli` were also a name in `_COMPRESSOR_VERBS` the family's rule would run
    first for it and name the operand under `-o` — the false block
    `test_an_outside_operand_beside_a_destination_inside_stays_allowed` pins.
    """
    assert "brotli" in bash_tool._BROTLI_VERBS
    assert "brotli" not in bash_tool._COMPRESSOR_VERBS


def test_a_spaced_long_value_is_not_stepped_over_and_that_is_pinned() -> None:
    """The one measured limit of this branch, stated instead of left to be found.

    `brotli --lgwin 24 <path>` is rc=1 ("must pass the parameter as --lgwin=value")
    and writes nothing, but the walk does not step over the word a *long*
    value-taking option eats, so `24` is read as an operand as well — an over-name,
    the direction the rest of this walk errs in, and the same limit
    `_option_destination_values` records for `curl --user-agent`. The attached
    spellings the program really accepts are one token each and name nothing but the
    file operand.
    """
    assert _extract_write_targets(f"brotli --lgwin 24 {OUTSIDE}/f") == [
        "24", f"{OUTSIDE}/f"]
    assert _extract_write_targets(f"brotli --lgwin=24 {OUTSIDE}/f") == [f"{OUTSIDE}/f"]
    assert _extract_write_targets(f"brotli --quality=11 {OUTSIDE}/f") == [f"{OUTSIDE}/f"]


def test_a_dash_destination_is_the_one_write_this_rule_leaves_unnamed() -> None:
    """The measured residual, named here so a later reader finds it stated.

    `brotli -o - f` is rc=0 and creates a file **literally named `-`** (measured this
    host, one fresh directory: neither `f.br` nor stdout). The shared extractor drops a
    destination value of exactly `-` because that is how those options mean *stdout* on
    the verbs it was written for, so the path here is unnamed and both tiers allow the
    run. What keeps it a residual rather than a hole is that the name is relative: it
    resolves in the run's own directory, so the file lands in the cwd and no spelling of
    `-o -` can be aimed outside the workspace. Naming it would mean this verb holding
    its own `-` policy against the shared extractor.
    """
    assert _extract_write_targets(f"brotli -o - {OUTSIDE}/f") == []
    allowed, _, _ = _check_sandbox(f"brotli -o - {OUTSIDE}/f", "workspace-write",
                                   workdir="/workspace")
    assert allowed is True
