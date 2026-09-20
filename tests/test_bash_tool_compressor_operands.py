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
  named their operand would refuse a pure read;
* the family's own **stream operand** — a bare `-` — is not a path, so it is
  dropped from a write form one operand at a time, while the file beside it is
  still named (`gzip - f` really compresses `f`; see
  `test_the_stream_operand_is_what_spares_the_bare_dash`).

Nothing here executes a command. `_check_sandbox` is a pure predicate — it
`realpath`s a path and opens nothing — and `_extract_write_targets` only parses,
so the protected path below is an *input to a predicate* rather than something a
test can damage. That matters for this family more than for most: every assertion
here is a negative one ("the guard refuses X"), and a negative test is only
harmless while the guard works.

This file is deliberately separate from `test_bash_tool_sandbox.py`: it covers a
family of its own, and keeping it self-contained keeps the two files' fixtures
from having to agree about a table neither owns.

**`compress` / `uncompress` — the same hole, one installable name over**
(measured 2026-09-19, `cyc20260919-162257`). Issue #1420 says the family is
enumerated by name, so every compressor off the list keeps the hole #1418 closed;
`compress` is exactly that case, and unlike #1420's other rows it is not
hypothetical — `/usr/bin/compress` is installed on this host. Ground truth in a
scratch directory: `compress f` **removed `f` and wrote `f.Z`** at rc=0 (`f.Z`
present, `f` gone), and `uncompress f.Z` did the same in reverse. Through the real
predicate both answered **ALLOW** on the protected daemon file at **both** tiers
while `gzip` was refused on it. They are the same shape as the rest of this
family, so they joined `_COMPRESSOR_VERBS` rather than getting a branch of their
own — and the read gate needed one honest correction to take them: `compress`
accepts `-c` but **rejects `-t` and `-l` as illegal options** (its own usage line
is `compress [-cfv] [-b bits] [file ...]`), so those two letters write nothing
rather than being read forms the program supports.

**`zstdmt` — the same bytes under a second name** (measured 2026-09-19). This list
is a list of *names*, so it was blind to a second name for a verb it already
handled — even though the two names are one file. `/opt/homebrew/bin/zstdmt` is a
symlink to `/opt/homebrew/Cellar/zstd/1.5.7/bin/zstd`, both hash to
`15da463937cca60558fc7e7b281e09b071ea40ea3b80408328a87d4f83195be1`, and their
`--help` output differs in exactly one line: the usage line's program name. The
program dispatches on argv[0] and nothing else, so the family's rows hold verbatim
and were re-measured rather than assumed, one fresh directory per row with only the
input present: `zstdmt f` and `zstdmt -19 f` derive `f.zst` beside the operand at
rc=0 while `f` stays; `zstdmt -c f` and `zstdmt --stdout f` leave the directory
holding only `f`; `zstdmt -l f.zst` prints the frame table and `zstdmt -t f.zst`
tests the frame, neither creating a file. Through the real predicate the write
forms were ALLOW at both tiers on the protected daemon file before the name was
added, which is the hole this row closes. It joins on same-bytes evidence, not on
the resemblance of the name — the line the `*cat` rows draw from the other side.

**`pigz` / `unpigz` — the parallel twin of `gzip`, measured before it was named**
(2026-09-20). Issue #1420 lists it first among the verbs the enumeration is blind
to, and unlike the rest of that list it is not hypothetical *in shape*: `pigz` is
`gzip` with threads, so its default form rewrites the operand in place and it needs
no branch of its own. The measurement is still what earned the name, because the
same issue records the opposite outcome for a verb that looks like a sibling too
(`lz4` derives `f.lz4` beside an untouched `f`) and because no package of `pigz` is
installed here: the binary was built from its own release source (`madler/pigz`
v2.8) in a scratch directory, one **fresh** directory per row with only the input
present and the listing read back off disk afterwards — `pigz f`, `pigz -9 f` and
`pigz -k f` write `f.gz` (the third keeping `f`), `pigz -S .zz f` writes `f.zz`,
`pigz -d f.gz` and `unpigz f.gz` write `f`, `pigz - f` and `pigz f -` both write
`f.gz`, while `pigz -c f`, `pigz --stdout f`, `pigz -t f.gz`, `pigz --test f.gz`,
`pigz -l f.gz`, `pigz --list f.gz`, `pigz -dc f.gz`, `unpigz -c f.gz`,
`unpigz -t f.gz` and a bare `pigz -` create no file at all. Through the real
predicate, `pigz` and `unpigz` on the protected daemon file were **ALLOW at both
tiers** before the name was added — the hole #1418 closed for `gzip`, one argv[0]
over.

Two limits travel with the name, and both are asserted below rather than implied:
the five options of `pigz` that take a spaced value which the family's table does
not carry (`-b`, `-p`, `-A`, `-I`, `-J` — its own source lists exactly those six
with `-S`), and `pigz -h`/`--version`, which print and write nothing. In both the
walk *adds* a name rather than losing one, which is the direction this guard's
record accepts.
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
    # `compress`/`uncompress`: the same in-place shape, measured on the host's
    # own binary (2026-09-19) — see the module docstring.
    ("compress", f"compress {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("compress force", f"compress -f {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("compress verbose", f"compress -v {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("uncompress", f"uncompress {OUTSIDE}/f.Z", (f"{OUTSIDE}/f.Z",)),
    # `zstdmt`: the same binary as `zstd` under a second name, so the name list was
    # one argv[0] short of the verb it already handled — see the module docstring.
    ("zstdmt", f"zstdmt {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("zstdmt level", f"zstdmt -19 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("zstdmt decompress", f"zstdmt -d {OUTSIDE}/f.zst", (f"{OUTSIDE}/f.zst",)),
    # `pigz`/`unpigz`: the parallel twin of `gzip`, measured before it was named —
    # see the module docstring. Every row here rewrites its operand in place, and
    # every one of them was ALLOW at both tiers on the master this fixes.
    ("pigz default", f"pigz {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("pigz level", f"pigz -9 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("pigz keep", f"pigz -k {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # The family's one spaced value works for this verb unchanged (`pigz -S .zz f`
    # writes `f.zz`), so the shared table is what keeps the suffix from being named.
    ("pigz suffix", f"pigz -S .zz {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("pigz decompress", f"pigz -d {OUTSIDE}/f.gz", (f"{OUTSIDE}/f.gz",)),
    ("unpigz", f"unpigz {OUTSIDE}/f.gz", (f"{OUTSIDE}/f.gz",)),
    # Two operands: `pigz a b` rewrites both, so both are named.
    ("pigz two operands", f"pigz {OUTSIDE}/a {OUTSIDE}/b",
     (f"{OUTSIDE}/a", f"{OUTSIDE}/b")),
    # The stream operand is the family's, and so is the drop: measured, `pigz - f`
    # and `pigz f -` both write `f.gz`, so the file is named and the bare `-` is not.
    ("pigz dash beside a file", f"pigz - {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("pigz dash as the last operand", f"pigz {OUTSIDE}/f -", (f"{OUTSIDE}/f",)),
    # Two operands: both are rewritten, so both are named.
    ("two operands", f"gzip {OUTSIDE}/a {OUTSIDE}/b", (f"{OUTSIDE}/a", f"{OUTSIDE}/b")),
    # …and the drop below is **per operand**, not per run: measured on the host
    # 2026-09-19 in a fresh directory holding only `f`, `gzip - f` is rc=0 and
    # writes `f.gz` while removing `f`, so the file beside the stream must still
    # be named. A run-level "this is a read form" answer for the bare `-` would
    # have named nothing here, i.e. a hole in exchange for the false block. The
    # row carries the measured shape with an outside path, so that both tiers have
    # the file — rather than the stream token — to refuse.
    ("dash beside a file", f"gzip - {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
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
    # `compress` takes `-c` and only `-c` of the three letters.
    ("compress -c", f"compress -c {OUTSIDE}/f"),
    ("uncompress -c", f"uncompress -c {OUTSIDE}/f.Z"),
    # `zstdmt` is the same parser as `zstd`, so its read spellings are the family's.
    ("zstdmt -c", f"zstdmt -c {OUTSIDE}/f"),
    ("zstdmt --stdout", f"zstdmt --stdout {OUTSIDE}/f"),
    ("zstdmt -t", f"zstdmt -t {OUTSIDE}/f.zst"),
    ("zstdmt -l", f"zstdmt -l {OUTSIDE}/f.zst"),
    ("zstdmt -dc cluster", f"zstdmt -dc {OUTSIDE}/f.zst"),
    # `pigz` takes the family's three read letters and its long forms, measured —
    # see the module docstring. Nothing here creates a file.
    ("pigz -c", f"pigz -c {OUTSIDE}/f"),
    ("pigz --stdout", f"pigz --stdout {OUTSIDE}/f"),
    ("pigz -t", f"pigz -t {OUTSIDE}/f.gz"),
    ("pigz --test", f"pigz --test {OUTSIDE}/f.gz"),
    ("pigz -l", f"pigz -l {OUTSIDE}/f.gz"),
    ("pigz --list", f"pigz --list {OUTSIDE}/f.gz"),
    ("pigz -dc cluster", f"pigz -dc {OUTSIDE}/f.gz"),
    ("unpigz -c", f"unpigz -c {OUTSIDE}/f.gz"),
    ("unpigz -t", f"unpigz -t {OUTSIDE}/f.gz"),
    ("pigz bare dash", "pigz -"),
    # A bare `-` is this family's own stdin/stdout spelling: the program reads the
    # stream and writes the stream, so no file is opened under that name. Measured
    # on the host 2026-09-19, one **fresh** directory per row with the input present
    # and the listing read back off disk: every row below creates no file, and
    # `gzip -- -` / `gzip -9 -` leave a file literally named `-` untouched as well.
    # Before the drop each of them was refused — a false block, the direction this
    # file's own record treats as the costlier one.
    ("bare dash", "gzip -"),
    ("bare dash with a level", "gzip -9 -"),
    ("bare dash after --", "gzip -- -"),
    ("bare dash, bzip2", "bzip2 -"),
    ("bare dash, xz", "xz -"),
    ("bare dash, zstd", "zstd -"),
    ("bare dash, compress", "compress -"),
    ("bare dash, uncompress", "uncompress -"),
    ("bare dash, decompress", "gzip -d -"),
)

# The wrappers, which are reads by construction and must not be in the family.
CAT_WRAPPERS = (
    ("zcat", f"zcat {OUTSIDE}/f.gz"),
    ("bzcat", f"bzcat {OUTSIDE}/f.bz2"),
    ("xzcat", f"xzcat {OUTSIDE}/f.xz"),
    ("zstdcat", f"zstdcat {OUTSIDE}/f.zst"),
    # `lz4cat` is `lz4` under its cat name: same bytes as the lz4 above (measured
    # 2026-09-19), same `-dc` meaning, and the row that must stay allowed when
    # `unlz4`/`lz4c` join the lz4 rule.
    ("lz4cat", f"lz4cat {OUTSIDE}/f.lz4"),
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


def test_the_read_gate_answers_for_compresses_own_letters() -> None:
    """`compress` takes one of the three read letters, and that is enough.

    The gate spares an operand when a `c`/`t`/`l` appears, and `compress`'s own
    usage line is `compress [-cfv] [-b bits] [file ...]`: measured on the host's
    binary (2026-09-19), `-c` is a real read form (the bytes go to stdout and the
    file is left alone) while **`-t` and `-l` are rejected as illegal options**,
    so under those spellings the program writes nothing at all.

    Both directions matter and only one of them is a claim about `compress`: the
    `-c` row is the read form this family must not refuse, and the `-t`/`-l` rows
    are the *harmless* side of an over-approximation — reading an unsupported
    letter as a read cannot hide a write, because the program refuses the run
    first. If a future `compress` ever gave `-t` or `-l` a writing meaning, this
    is the test that would have to change, which is why it is stated here rather
    than left implicit in a comment.
    """
    assert _check_sandbox(f"compress -c {OUTSIDE}/f", "read-only",
                          workdir="/workspace")[0] is True
    for unsupported in ("-t", "-l"):
        cmd = f"compress {unsupported} {OUTSIDE}/f"
        assert _extract_write_targets(cmd) == [], unsupported
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is True, (
            f"{unsupported}: the gate spares this spelling and the program rejects "
            f"it, so nothing can be written under it"
        )


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

    # `pigz` on that same file: the row this name was added for. Before it the verb
    # was ALLOW at both tiers here, because a name the set does not carry produces an
    # empty target list and both tiers allow that by construction.
    allowed, reason, _ = _check_sandbox(f"pigz {PROTECTED}", "workspace-write",
                                        workdir="/workspace")
    assert allowed is False
    assert "protected daemon file" in reason, reason
    assert _check_sandbox(f"pigz -c {PROTECTED}", "workspace-write",
                          workdir="/workspace")[0] is True


def test_pigz_value_letters_are_a_stated_limit() -> None:
    """Five spaced values `pigz` takes are read as operands — the harmless direction.

    `pigz -b 65536 f`, `pigz -p 2 f`, `pigz -A nm f`, `pigz -I 5 f` and `pigz -J 4 f`
    all write `f.gz` (measured — see the module docstring), so those tokens are the
    option's value rather than a path. The family's table carries `-S` only, and the
    read gate refuses to grow a per-compressor value table on purpose, so the walk
    names the value as an extra operand.

    Pinned because the limit has to stay the direction this file's record accepts: it
    **adds** a name rather than losing one. The operand is still named, so both tiers
    still refuse the rewrite, and the extra token is one the program cannot write —
    a number, and `pigz -p /outside/x f` is rc=22 with nothing written.
    """
    for spelling in ("-b 65536", "-p 2", "-A nm", "-I 5", "-J 4"):
        cmd = f"pigz {spelling} {OUTSIDE}/f"
        assert _extract_write_targets(cmd) == [spelling.split()[-1], f"{OUTSIDE}/f"], (
            f"{spelling}: the operand must still be named beside the value"
        )
        for tier in ("read-only", "workspace-write"):
            allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
            assert allowed is False, f"{spelling}: {tier} allowed the write it names"


# ── mutation arms: a row that cannot be flipped is not a claim ──────────────

# (verb, a command whose refusal depends on that verb being in the family)
FAMILY_ARMS = (
    ("gzip", f"gzip {OUTSIDE}/f"),
    ("gunzip", f"gunzip {OUTSIDE}/f.gz"),
    ("bzip2", f"bzip2 {OUTSIDE}/f"),
    ("xz", f"xz {OUTSIDE}/f"),
    ("zstd", f"zstd {OUTSIDE}/f"),
    ("compress", f"compress {OUTSIDE}/f"),
    ("uncompress", f"uncompress {OUTSIDE}/f.Z"),
    ("zstdmt", f"zstdmt {OUTSIDE}/f"),
    ("pigz", f"pigz {OUTSIDE}/f"),
    ("unpigz", f"unpigz {OUTSIDE}/f.gz"),
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


def test_the_stream_operand_is_what_spares_the_bare_dash() -> None:
    """The drop is a second piece of code, so it gets its own arm.

    Without it a bare `-` goes back to being named, and every row in the dash
    block above returns to the refusal master gave. That is the whole claim: the
    rows are refused *because* the token is dropped, not because something else
    in the walk happens to spare them.
    """
    drop = bash_tool._without_the_stream_operand
    row = "gzip -"
    beside = "gzip - f"

    assert _extract_write_targets(row) == []
    assert _check_sandbox(row, "read-only", workdir="/workspace")[0] is True
    assert _check_sandbox(beside, "read-only", workdir="/workspace")[0] is False

    try:
        bash_tool._without_the_stream_operand = lambda targets: list(targets)
        assert _check_sandbox(row, "read-only", workdir="/workspace")[0] is False, (
            "with the drop removed the bare dash must return to the ALLOW master "
            "gave — otherwise the drop is not what spares it"
        )
        # …and the row beside it must *not* move: it is refused for naming `f`,
        # which is what makes the drop a per-operand rule rather than a per-run one.
        assert _extract_write_targets(beside) == ["-", "f"]
    finally:
        bash_tool._without_the_stream_operand = drop

    assert _extract_write_targets(row) == []
    assert _extract_write_targets(beside) == ["f"]


def test_the_drop_is_the_compressors_alone() -> None:
    """The boundary the drop must not cross, measured in the same geometry.

    A bare `-` is the compressors' stream operand. To the everyday writers it is
    a **path**, and a file named ``-`` is exactly what they act on: measured on
    this host 2026-09-19, one fresh directory per row with only `src.txt` present
    (no dash file), `touch -`, `truncate -s0 -`, `mv src.txt -` and `cp src.txt -`
    each *create* the file named ``-`` at rc=0, while `rm -` and `chmod 777 -` look
    one up (rc=1, `-: No such file or directory`). Dropping the token inside
    `_positional_args`, where every verb would inherit it, would therefore open
    the hole those rows are named to close — this test pins the drop to the family
    that measured it.
    """
    for cmd in ("touch -", "truncate -s0 -", "mv src.txt -", "cp src.txt -"):
        assert _extract_write_targets(cmd) == ["-"], cmd
        assert _extract_write_targets(cmd) != [], (
            f"{cmd}: an empty target list here is the everyday writers' hole"
        )

