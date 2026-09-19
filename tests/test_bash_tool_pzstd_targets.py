"""`pzstd` names the sibling it derives, and `-o` names its destination.

`pzstd` is zstd's parallel front-end and it is **not** in `_COMPRESSOR_VERBS`,
because it is not a second name for `zstd`: measured on this host (2026-09-19),
`/opt/homebrew/bin/pzstd` realpaths into the same Cellar but hashes
`0bad6c010cc29143f7c84808393943bf30b4ad5a11368d3c353e330d09e246f6` against zstd's
`15da463937cca60558fc7e7b281e09b071ea40ea3b80408328a87d4f83195be1`, and its own
usage line carries an option zstd's family rule cannot express
(`-o  file : result stored into 'file' (only if 1 input file)`). Issue #1420's
whole subject is that the family is enumerated by name, so an unlisted writer keeps
the hole; this file is that row, one installed binary over.

Measured on master `1935e19d` through the real predicate before the fix, with the
target outside every allowed root: `pzstd <out>/f`, `pzstd -o <out>/out.zst
<out>/f`, `pzstd -o<out>/out.zst <out>/f`, `pzstd -c <out>/f`, `pzstd -t
<out>/f.zst` and `pzstd -p 4 <out>/f` **all reported an empty target list**, and an
empty list is allowed by construction — the loop that judges targets never runs. At
the same tiers `gzip <out>/f`, `lz4 <out>/f` and `zip <out>/a.zip <out>/f` were
refused in the same geometry, which is what makes this a hole rather than an
opinion.

Ground truth, taken in one **fresh** directory per row with `f` present and the
listing read back off disk (pzstd 1.5.7, this host, 2026-09-19):

    pzstd f                    writes  f  f.zst          sibling derived; `f` STAYS
    pzstd -k f                 writes  f  f.zst
    pzstd -19 f / -vv f / -q f writes  f  f.zst
    pzstd --rm f               writes  f.zst             and removes the operand
    pzstd -p 4 f               writes  f  f.zst          `-p` eats `4`
    pzstd f g                  writes  f.zst  g.zst      every operand derives one
    pzstd -- f                 writes  f  f.zst
    pzstd -o out.zst f         writes  f  out.zst        a real destination
    pzstd -oout.zst f          writes  f  out.zst        …in the attached spelling too
    pzstd f -o out.zst         writes  f  out.zst        the destination wins wherever it stands
    pzstd -o out.zst -         writes  out.zst           the stream still gets a destination
    pzstd -c f / --stdout f    read    f                 [stdout]
    pzstd -t f.zst             read    f.zst             [test]
    pzstd -dc f.zst            read    f.zst             [decompress to stdout]
    pzstd -                    read    —                 nothing on disk
    pzstd -o - f               read    f                 [stdout] — `-` as the destination
    pzstd -l f / --list f      rc=1    f                 `Invalid argument: -l`
    pzstd --to-stdout f        rc=1    f                 `Invalid argument`
    pzstd --output=out.zst f   rc=1    f                 `Invalid argument` — no long form
    pzstd - f                  rc=1    f                 "Cannot specify standard input …"
    pzstd -o out.zst f g       rc=1    f  f.zst          "Cannot specify an output file …"

So the family's rule cannot simply be lent to this verb, in three separate ways: the
default form keeps its operand and derives a sibling (that is `lz4`'s claim, not
`gzip`'s); the destination lives in **option** position where no operand rule
reaches it; and the attached `-oout.zst` carries the letter `t`, which the family's
read gate would read as `--test` and answer "read, nothing named" — the hole
reopened one spelling over. Hence `_pzstd_write_targets`, and hence this file.

Nothing here executes a command: `_check_sandbox` is a pure predicate that
`realpath`s a path and opens nothing, and `_extract_write_targets` only parses, so
the protected path below is an *input to a predicate* rather than something a test
can damage. That matters here because most of these assertions are negative ones,
and a negative test is only harmless while the guard works.
"""

from __future__ import annotations

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

# (row, command, every path the walk must name). The sibling the run derives lands
# in the operand's own directory, so the operand is what is named for the
# operand-derived forms — exactly the claim `_lz4_write_targets` states.
WRITE_FORMS = (
    ("default", f"pzstd {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("keep", f"pzstd -k {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("level", f"pzstd -19 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("verbose", f"pzstd -vv {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("quiet", f"pzstd -q {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("remove source", f"pzstd --rm {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # `-p` takes a *count*, not a path: naming it would point the guard's message at
    # a token that is not a file, the defect `_positional_args` exists to avoid.
    ("processes spaced", f"pzstd -p 4 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("processes attached", f"pzstd -p4 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("processes long", f"pzstd --processes 4 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("two operands", f"pzstd {OUTSIDE}/a {OUTSIDE}/b", (f"{OUTSIDE}/a", f"{OUTSIDE}/b")),
    ("after --", f"pzstd -- {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
)

# (row, command, the destination — and only the destination). While `-o` is
# present the operands are **read**: measured, `pzstd -o out.zst f` leaves `f`
# untouched, so naming `f` as well would refuse a pure read.
DESTINATION_FORMS = (
    ("-o spaced", f"pzstd -o {OUTSIDE}/out.zst {OUTSIDE}/f", (f"{OUTSIDE}/out.zst",)),
    ("-o attached", f"pzstd -o{OUTSIDE}/out.zst {OUTSIDE}/f", (f"{OUTSIDE}/out.zst",)),
    ("-o after the operand", f"pzstd {OUTSIDE}/f -o {OUTSIDE}/out.zst", (f"{OUTSIDE}/out.zst",)),
    ("-o with the stream", f"pzstd -o {OUTSIDE}/out.zst -", (f"{OUTSIDE}/out.zst",)),
)

# (row, command) — spellings that write nothing and must stay allowed, or the fix
# would be a false block. `-l`/`--list` is here although pzstd **rejects** it
# (`Invalid argument: -l`, measured, rc=1): a rejected run writes nothing, so
# reading the letter as a read cannot hide a write, while not reading it would
# refuse a command that was going to fail anyway — the same reading the family
# takes of `compress`'s illegal `-t`.
READ_FORMS = (
    ("-c", f"pzstd -c {OUTSIDE}/f"),
    ("--stdout", f"pzstd --stdout {OUTSIDE}/f"),
    ("-t", f"pzstd -t {OUTSIDE}/f.zst"),
    ("--test", f"pzstd --test {OUTSIDE}/f.zst"),
    ("-l", f"pzstd -l {OUTSIDE}/f.zst"),
    ("--list", f"pzstd --list {OUTSIDE}/f.zst"),
    ("-dc cluster", f"pzstd -dc {OUTSIDE}/f.zst"),
    ("-dt cluster", f"pzstd -dt {OUTSIDE}/f.zst"),
    ("-tv cluster", f"pzstd -tv {OUTSIDE}/f.zst"),
    # A bare `-` is the stream: the program reads stdin and writes stdout, so no
    # file is opened under that name.
    ("bare dash", "pzstd -"),
    # `-o -` names stdout as the destination — measured rc=0 with the directory
    # unchanged, the bytes on stdout. The destination option *is* spelled, so the
    # operand rule must not be reached for it.
    ("dash destination", f"pzstd -o - {OUTSIDE}/f"),
)

# (row, command, the target list this rule produces) — two measured limits, pinned
# so that neither is a surprise later. Both are **over-names**: in both, the path
# the run really writes is still named, so no hidden write is left unnamed, which is
# the direction this walk prefers to err in.
RESIDUALS = (
    # A destination letter that does not *lead* its cluster is not read as the
    # option — the shape `_leading_short_option_value` leaves unread on purpose —
    # so the run falls through and the destination is named as an operand ... plus
    # the input, which is only read. Measured: rc=0, directory left holding
    # `f out.zst`.
    ("destination behind a flag", f"pzstd -qo {OUTSIDE}/out.zst {OUTSIDE}/f",
     (f"{OUTSIDE}/out.zst", f"{OUTSIDE}/f")),
    # A stream operand beside a file is dropped, as in the family, leaving `f`
    # named although the run aborts: measured rc=1, "Cannot specify standard input
    # when handling multiple files", nothing written.
    ("stream beside a file", f"pzstd - {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
)


@pytest.mark.parametrize("row,cmd,target", WRITE_FORMS, ids=[r for r, *_ in WRITE_FORMS])
def test_a_pzstd_write_form_names_the_operand_it_derives_from(row, cmd, target) -> None:
    """The sibling lands in the operand's own directory, so the operand is named."""
    assert _extract_write_targets(cmd) == list(target), row


@pytest.mark.parametrize("row,cmd,target", WRITE_FORMS, ids=[r for r, *_ in WRITE_FORMS])
def test_a_pzstd_write_form_is_refused_at_both_tiers(row, cmd, target) -> None:
    """…and naming it is what makes both tiers refuse, which is the point."""
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is False, f"{row}: {tier} allowed a write to {target}"
        assert reason, row


@pytest.mark.parametrize("row,cmd", READ_FORMS, ids=[r for r, _ in READ_FORMS])
def test_a_pzstd_read_form_names_nothing_and_stays_allowed(row, cmd) -> None:
    """Every row here writes nothing, so naming its operand would be a false block."""
    assert _extract_write_targets(cmd) == [], row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is True, f"{row}: {tier} refused a read ({reason})"


@pytest.mark.parametrize(
    "row,cmd,target", DESTINATION_FORMS, ids=[r for r, *_ in DESTINATION_FORMS]
)
def test_the_destination_option_names_the_write_and_not_the_input(row, cmd, target) -> None:
    """`-o` is the write; the operands beside it are reads and must not be named.

    This is the half a rule borrowed from the in-place family would get wrong: that
    rule names every operand, so it would refuse `pzstd -o <out> <in>` on the input
    it only reads — and the input is where a task legitimately points it.
    """
    assert _extract_write_targets(cmd) == list(target), row
    assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, row


@pytest.mark.parametrize("row,cmd,target", RESIDUALS, ids=[r for r, *_ in RESIDUALS])
def test_the_named_limits_still_name_the_path_that_is_written(row, cmd, target) -> None:
    """Both residuals are over-names: the real write is named in each of them."""
    assert _extract_write_targets(cmd) == list(target), row
    assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, row


def test_the_attached_destination_is_not_read_as_a_read_letter() -> None:
    """The trap this branch exists around, stated as its own claim.

    `-oout.zst` carries `t` inside the *file name*. The family's gate scans a short
    cluster letter by letter and would call that `--test` — "read, nothing named" —
    so a verb added to the family without its own scan keeps the hole for exactly
    the spelling that names a destination. Measured: `pzstd -oout.zst f` is rc=0
    and writes `out.zst`.
    """
    args = [f"-o{OUTSIDE}/out.zst", f"{OUTSIDE}/f"]
    assert bash_tool._pzstd_read_form(args) is False
    assert bash_tool._compressor_operand_is_a_read(["pzstd", *args], 0) is True, (
        "the family gate should still read this as a read — that is the trap, and "
        "this assertion is what makes it visible if the family gate changes"
    )
    assert _extract_write_targets(f"pzstd -o{OUTSIDE}/out.zst {OUTSIDE}/f") == [
        f"{OUTSIDE}/out.zst"
    ]


def test_the_default_form_cannot_touch_a_protected_daemon_file() -> None:
    """The failure this fix exists for, stated as the file it protects.

    `~/.emrg/rants.jsonl` is the host's rant store. Before the fix `pzstd` on that
    path was ALLOW at both tiers; now each tier refuses it for its own reason, so
    both are asserted rather than just "not allowed".
    """
    allowed, reason, _ = _check_sandbox(f"pzstd {PROTECTED}", "workspace-write",
                                        workdir="/workspace")
    assert allowed is False
    assert "protected daemon file" in reason, reason

    allowed, reason, _ = _check_sandbox(f"pzstd {PROTECTED}", "read-only",
                                        workdir="/workspace")
    assert allowed is False
    assert "destructive write" in reason, reason

    # …and the destination spelling is refused on the destination, not the input.
    allowed, reason, _ = _check_sandbox(f"pzstd -o {PROTECTED} /tmp/in", "read-only",
                                        workdir="/workspace")
    assert allowed is False
    assert PROTECTED in reason, reason
    assert "/tmp/in" not in reason, reason


# ── mutation arms: a row that cannot be flipped is not a claim ──────────────


def test_the_verb_is_what_refuses_the_default_form() -> None:
    """Drop the verb, and the row must go back to the ALLOW master gave."""
    original = set(bash_tool._PZSTD_VERBS)
    cmd = f"pzstd {OUTSIDE}/f"
    assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False
    try:
        bash_tool._PZSTD_VERBS = original - {"pzstd"}
        allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
    finally:
        bash_tool._PZSTD_VERBS = original
    assert allowed is True, (
        "the row survives dropping pzstd from _PZSTD_VERBS — it does not depend on "
        "the branch it claims to test"
    )


def test_the_read_scan_is_what_spares_the_read_forms() -> None:
    """The scan is a second piece of code, so it gets its own arm, both ways.

    One direction kills the write rows (force the scan open and they go back to
    ALLOW), the other kills the read rows (force it shut and every read form
    becomes a refusal).
    """
    scan = bash_tool._pzstd_read_form
    write_row = f"pzstd {OUTSIDE}/f"
    read_row = f"pzstd -c {OUTSIDE}/f"

    assert _check_sandbox(write_row, "read-only", workdir="/workspace")[0] is False
    assert _check_sandbox(read_row, "read-only", workdir="/workspace")[0] is True

    try:
        bash_tool._pzstd_read_form = lambda *_a, **_k: True
        assert _check_sandbox(write_row, "read-only", workdir="/workspace")[0] is True, (
            "with the scan forced open the write row must return to the ALLOW master "
            "gave — otherwise the scan is not what refuses it"
        )
        bash_tool._pzstd_read_form = lambda *_a, **_k: False
        assert _check_sandbox(read_row, "read-only", workdir="/workspace")[0] is False, (
            "with the scan forced shut the read row must be refused — otherwise "
            "nothing about it depends on the scan"
        )
    finally:
        bash_tool._pzstd_read_form = scan


def test_the_destination_table_is_what_names_the_option_write() -> None:
    """Empty the destination table and the `-o` row must go back to ALLOW.

    Without this arm the `-o` rows could be passing on some other route — the
    operand rule also happens to name `out.zst` when it is spelled as a bare word.
    """
    original = bash_tool._PZSTD_DESTINATION_OPTIONS
    cmd = f"pzstd -o {OUTSIDE}/out.zst {OUTSIDE}/f"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/out.zst"]
    try:
        bash_tool._PZSTD_DESTINATION_OPTIONS = frozenset()
        allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
    finally:
        bash_tool._PZSTD_DESTINATION_OPTIONS = original
    assert allowed is True, (
        "with the destination table emptied the `-o` row must return to the ALLOW "
        "master gave — otherwise the table is not what names that write"
    )


def test_the_seam_spares_the_stream_destination_and_nothing_else() -> None:
    """`-o -` is a read; the same option with a real value is a write.

    Both spellings reach the option table, and the table drops a value of exactly
    `-` (that is how these options mean *stdout*). The two must not be answered by
    the same branch: keeping `f` named for `-o -` would refuse a measured read
    (rc=0, nothing on disk), and dropping it for `-o out.zst` would reopen the hole.
    """
    seam = bash_tool._pzstd_names_a_destination
    read_row = f"pzstd -o - {OUTSIDE}/f"

    assert _extract_write_targets(read_row) == []
    assert _check_sandbox(read_row, "read-only", workdir="/workspace")[0] is True

    try:
        bash_tool._pzstd_names_a_destination = lambda *_a, **_k: False
        assert _check_sandbox(read_row, "read-only", workdir="/workspace")[0] is False, (
            "without the seam the `-o -` row falls through to the operand rule and "
            "names `f`, which the run only reads — otherwise the seam is not what "
            "spares it"
        )
    finally:
        bash_tool._pzstd_names_a_destination = seam

    assert bash_tool._pzstd_names_a_destination(["-o", "out.zst"]) is True
    assert bash_tool._pzstd_names_a_destination(["-oout.zst"]) is True
    assert bash_tool._pzstd_names_a_destination(["-qo", "out.zst"]) is False, (
        "a cluster that does not lead with the destination letter is exactly the "
        "residual pinned above; reading it here would answer `[]` for a run that "
        "really writes out.zst"
    )
