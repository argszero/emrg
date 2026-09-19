"""The compressors rewrite their operand in place (one verb family shorter).

`gzip f` replaces `f` with `f.gz` and removes `f` — the sentence
`_INPLACE_WRITER_VERBS`' own comment already carries about `truncate`, `tee` and
`shred`, whose job is to stop a read-only cycle from destroying uncommitted work.
The compressors belong to that family and were not in it, so their operand was
never named as a write target and the loop that judges targets never ran.

Measured on master `9a8bc960` (2026-09-19), one geometry, both checked tiers, the
same protected daemon file: `gzip`, `gzip -f`, `gzip -9`, `gzip -k`, `gzip -d`,
`gunzip`, `bzip2`, `xz` and `zstd` all answered **ALLOW** — at `workspace-write`,
where `truncate -s 0`, `tee` and `shred -u` on that path were refused with
"blocked write to protected daemon file", and at `read-only`, whose whole job is
that refusal, where the same three were refused and `gzip` was the one that got
through. An empty target list is allowed by construction, so every one of those
rows was a hole rather than an opinion, and nothing pinned it.

What this file asserts, in the three directions the fix can be wrong:

* the **write forms** name their operand (the default form, `-d`, `-f`, `-k`, a
  level, and the decompressing twins of the same programs);
* the **read forms** name nothing and stay allowed — `gzip -c f` sends the bytes
  to stdout, `-t` and `-l` only inspect the file, and `gzip -dc f.gz` is the
  `zcat` idiom, so the letter is read inside a short cluster as well;
* the **`*cat` wrappers** (`zcat`, `bzcat`, `xzcat`, `zstdcat`) are not in the
  family at all: they are `-dc` wrappers that write nothing, and a rule that
  named their operand would refuse a pure read.

Nothing here executes a command. `_check_sandbox` is a pure predicate — it
`realpath`s a path and opens nothing — and `_extract_write_targets` only parses,
so the protected path below is an *input to a predicate* rather than something a
test can damage. That matters for this family more than for most: every assertion
here is a negative one ("the guard refuses X"), and a negative test is only
harmless while the guard works.

This file is deliberately separate from `test_bash_tool_sandbox.py`: it covers a
family of its own, and keeping it self-contained keeps the two files' fixtures
from having to agree about a table neither owns.
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

# The daemon's own rant store: a protected file, and the one a sandboxed task
# must not be able to destroy. Also only ever an input to the predicate.
PROTECTED = "~/.emrg/rants.jsonl"

# (row, command, every path the walk must name for it — a tuple, because a
# command with two operands really rewrites two files). The two tiers are
# asserted separately below, because they refuse for different reasons: read-only
# names the destructive write, workspace-write the protected file.
WRITE_FORMS = (
    ("gzip default", f"gzip {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("gzip level", f"gzip -9 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("gzip force", f"gzip -f {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("gzip keep", f"gzip -k {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("gzip decompress", f"gzip -d {OUTSIDE}/f.gz", (f"{OUTSIDE}/f.gz",)),
    ("gzip suffix", f"gzip -S .gz {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("gunzip", f"gunzip {OUTSIDE}/f.gz", (f"{OUTSIDE}/f.gz",)),
    ("bzip2", f"bzip2 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("bunzip2", f"bunzip2 {OUTSIDE}/f.bz2", (f"{OUTSIDE}/f.bz2",)),
    ("xz", f"xz {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("unxz", f"unxz {OUTSIDE}/f.xz", (f"{OUTSIDE}/f.xz",)),
    ("lzma", f"lzma {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("zstd", f"zstd {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("unzstd", f"unzstd {OUTSIDE}/f.zst", (f"{OUTSIDE}/f.zst",)),
    # Two operands: both are rewritten, so both are named.
    ("two operands", f"gzip {OUTSIDE}/a {OUTSIDE}/b", (f"{OUTSIDE}/a", f"{OUTSIDE}/b")),
)

# (row, command) — the spellings whose operand is a *read*. Each must name
# nothing and stay allowed, or the fix would be a false block.
READ_FORMS = (
    ("-c spaced", f"gzip -c {OUTSIDE}/f"),
    ("--stdout", f"gzip --stdout {OUTSIDE}/f"),
    ("--to-stdout", f"gzip --to-stdout {OUTSIDE}/f"),
    ("-t", f"gzip -t {OUTSIDE}/f.gz"),
    ("--test", f"gzip --test {OUTSIDE}/f.gz"),
    ("-l", f"gzip -l {OUTSIDE}/f.gz"),
    ("--list", f"gzip --list {OUTSIDE}/f.gz"),
    ("-dc cluster", f"gzip -dc {OUTSIDE}/f.gz"),
    ("-cd cluster", f"gzip -cd {OUTSIDE}/f.gz"),
    ("-9c cluster", f"gzip -9c {OUTSIDE}/f"),
    ("-tv cluster", f"gzip -tv {OUTSIDE}/f.gz"),
    ("bzip2 -dc", f"bzip2 -dc {OUTSIDE}/f.bz2"),
    ("xz -dc", f"xz -dc {OUTSIDE}/f.xz"),
    ("zstd -dc", f"zstd -dc {OUTSIDE}/f.zst"),
)

# The wrappers, which are reads by construction and must not be in the family.
CAT_WRAPPERS = (
    ("zcat", f"zcat {OUTSIDE}/f.gz"),
    ("bzcat", f"bzcat {OUTSIDE}/f.bz2"),
    ("xzcat", f"xzcat {OUTSIDE}/f.xz"),
    ("zstdcat", f"zstdcat {OUTSIDE}/f.zst"),
)


@pytest.mark.parametrize("row,cmd,target", WRITE_FORMS, ids=[r for r, *_ in WRITE_FORMS])
def test_a_compressor_write_form_names_its_operand(row, cmd, target) -> None:
    """The operand of a writing compressor run is the file it rewrites."""
    assert _extract_write_targets(cmd) == list(target), row


@pytest.mark.parametrize("row,cmd,target", WRITE_FORMS, ids=[r for r, *_ in WRITE_FORMS])
def test_a_compressor_write_form_is_refused_at_both_tiers(row, cmd, target) -> None:
    """…and naming it is what makes both tiers refuse, which is the point."""
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is False, f"{row}: {tier} allowed a write to {target}"
        assert reason, row


@pytest.mark.parametrize("row,cmd", READ_FORMS, ids=[r for r, _ in READ_FORMS])
def test_a_compressor_read_form_names_nothing_and_stays_allowed(row, cmd) -> None:
    """The gate that keeps this family out of `_INPLACE_WRITER_VERBS`.

    Every row here writes nothing, so naming its operand would be a false block —
    the direction this guard's own record treats as worse than the hole.
    """
    assert _extract_write_targets(cmd) == [], row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is True, f"{row}: {tier} refused a read ({reason})"


@pytest.mark.parametrize("row,cmd", CAT_WRAPPERS, ids=[r for r, _ in CAT_WRAPPERS])
def test_the_cat_wrappers_stay_out_of_the_family(row, cmd) -> None:
    """`zcat` is `gzip -dc` and writes nothing, so it must not be swept in.

    This is the accepting half of the family boundary: a fix that covered the
    compressors by listing every tool whose name starts with one of these would
    refuse a pure read.
    """
    assert _extract_write_targets(cmd) == [], row
    assert _check_sandbox(cmd, "workspace-write", workdir="/workspace")[0] is True, row


def test_the_default_form_cannot_touch_a_protected_daemon_file() -> None:
    """The failure this fix exists for, stated as the file it protects.

    `~/.emrg/rants.jsonl` is the host's rant store. Before the fix `gzip` on that
    path was ALLOW at both tiers; the two tiers now refuse it for their own two
    reasons, which is why both are asserted rather than just "not allowed".
    """
    allowed, reason, _ = _check_sandbox(f"gzip {PROTECTED}", "workspace-write",
                                        workdir="/workspace")
    assert allowed is False
    assert "protected daemon file" in reason, reason

    allowed, reason, _ = _check_sandbox(f"gzip -f {PROTECTED}", "read-only",
                                        workdir="/workspace")
    assert allowed is False
    assert "destructive write" in reason, reason

    # …while the read form of the same command stays a read of the same file.
    assert _check_sandbox(f"gzip -c {PROTECTED}", "workspace-write",
                          workdir="/workspace")[0] is True


# ── mutation arms: a row that cannot be flipped is not a claim ──────────────

# (verb, a command whose refusal depends on that verb being in the family)
FAMILY_ARMS = (
    ("gzip", f"gzip {OUTSIDE}/f"),
    ("gunzip", f"gunzip {OUTSIDE}/f.gz"),
    ("bzip2", f"bzip2 {OUTSIDE}/f"),
    ("xz", f"xz {OUTSIDE}/f"),
    ("zstd", f"zstd {OUTSIDE}/f"),
)


def test_each_verb_row_dies_when_its_verb_leaves_the_family() -> None:
    """Drop the verb, and its row must go back to the ALLOW master gave.

    The arm patches the family the branch tests, then restores it. The row must be
    refused before the arm, or the arm proves nothing about the row.
    """
    original = set(bash_tool._COMPRESSOR_VERBS)
    for verb, cmd in FAMILY_ARMS:
        assert _extract_write_targets(cmd), f"{verb}: the row names no target before the arm"
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, verb
        try:
            bash_tool._COMPRESSOR_VERBS = original - {verb}
            allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
        finally:
            bash_tool._COMPRESSOR_VERBS = original
        assert allowed is True, (
            f"the {verb} row survives dropping {verb} from _COMPRESSOR_VERBS — it "
            "does not depend on the branch it claims to test"
        )


def test_the_read_gate_is_what_spares_the_read_forms() -> None:
    """The gate is a second piece of code, so it gets its own arm, both ways.

    One direction kills the write rows (force the gate open and they go back to
    ALLOW), the other kills the read rows (force it shut and every read form
    becomes a refusal). A fix that listed the verbs without the gate would pass
    neither.
    """
    gate = bash_tool._compressor_operand_is_a_read
    write_row = f"gzip {OUTSIDE}/f"
    read_row = f"gzip -c {OUTSIDE}/f"

    assert _check_sandbox(write_row, "read-only", workdir="/workspace")[0] is False
    assert _check_sandbox(read_row, "read-only", workdir="/workspace")[0] is True

    try:
        bash_tool._compressor_operand_is_a_read = lambda *_a, **_k: True
        assert _check_sandbox(write_row, "read-only", workdir="/workspace")[0] is True, (
            "with the gate forced open the write row must return to the ALLOW "
            "master gave — otherwise the gate is not what refuses it"
        )
        assert _check_sandbox(read_row, "read-only", workdir="/workspace")[0] is True

        bash_tool._compressor_operand_is_a_read = lambda *_a, **_k: False
        assert _check_sandbox(read_row, "read-only", workdir="/workspace")[0] is False, (
            "with the gate forced shut the read row must be refused — otherwise "
            "nothing about it depends on the gate"
        )
    finally:
        bash_tool._compressor_operand_is_a_read = gate
