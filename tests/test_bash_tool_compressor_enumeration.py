"""The boundary of the compressor enumeration (issue #1420).

`_COMPRESSOR_VERBS` is a list of **names**, so it covers exactly the verbs it
names and the hole it closes stays open for every compressor whose name is not on
it: an empty target list, and the loop that judges targets never runs. Issue
#1420 is that boundary. This file is the two halves of it —

* the verb whose *shape* is not the family's, now named and read by its own rule
  (`lz4`), and
* the verbs not named at all, pinned as measured holes so the next reader finds
  them stated rather than inferred.

The `lz4` rule's own read test was then narrowed once, from the other direction: a
value written **attached** (`-Ddata.txt`) put the value's letters into the flag set,
so an ordinary dictionary name read as `-c`/`-t` and the write was unnamed again
(issue #1426, rows and measurement below).

**Why `lz4` is not a name in `_COMPRESSOR_VERBS`.** Its default form derives a
sibling (`lz4 f` leaves `f` and creates `f.lz4`) instead of rewriting the operand
in place, its `-l` is *legacy format* rather than `--list`, and under `-m`/`-r`
every operand is an input. All three are measured on the host's own binary
(`lz4 v1.10.0`, `/opt/homebrew/bin/lz4`, 2026-09-19), one fresh directory per row
with only the input present and the listing read back off disk; the full table is
in the comment above `_LZ4_VERBS`, and each of the three is what a row below
asserts.

**Why the twins are pinned here and not in `UNCOVERED_WRITERS`.** That list's
question is a destination named by an **option** rather than by an operand
(`tar -cf`, `split -f`, `curl -so`, `git clone`); none of these has one — the hole
is that the verb itself is absent from the enumeration, which is this file's
question. The rows below are measured the same way that list's are: through the
real predicate, asserting an empty target list *and* the two tier verdicts, so a
future cycle that names one of these verbs reds the row instead of silently
changing the answer.

Nothing here executes a command. `_check_sandbox` is a pure predicate (it
`realpath`s a path and opens nothing) and `_extract_write_targets` only parses, so
`OUTSIDE` and the protected path below are arguments to a predicate rather than
things a test can damage — which matters most for the rows that assert a refusal.

**The twins of a verb that is already handled** (measured 2026-09-19). A list of
names is blind to a second name for a binary it covers, even when the two names are
one file. `unlz4`, `lz4c` and `lz4cat` are three symlinks to `lz4`: all four names
under `/opt/homebrew/Cellar/lz4/1.10.0/bin/` hash to
`b08405ac45dc1be5615bca7681c8d8d802a62ee9d5e1c1b4392a1e2cc7f68169`, so the program
is one parser dispatching on argv[0]. Two of the three write in `lz4`'s own shape —
`unlz4 f.lz4` writes `f` beside the operand (stderr `Decoding file f`), `lz4c f`
writes `f.lz4` (stderr `Compressed filename will be : f.lz4`) — and they are now in
`_LZ4_VERBS` with rows in both directions. `lz4cat` is `lz4 -dc`: it writes nothing,
so it stays out, and the row for it is the false block this fix could most easily
have caused. The same reading caught `zstdmt`, the second name of `zstd`; its rows
live in `test_bash_tool_compressor_operands.py`, which owns the family's table.
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

# (row, command, every path the walk must name — a tuple, because a multi-input
# run really derives a sibling for each operand).
WRITE_FORMS = (
    ("default", f"lz4 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("force", f"lz4 -f {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("force compress", f"lz4 -z {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # `-l` is legacy format and writes — the one letter this verb spells
    # differently from the family it superficially belongs to.
    ("legacy format", f"lz4 -l {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("decompress", f"lz4 -d {OUTSIDE}/f.lz4", (f"{OUTSIDE}/f.lz4",)),
    # `--rm` removes the operand; the sibling it derives is still the write.
    ("remove source", f"lz4 --rm {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # Two operands, no `-m`: the last one is the explicit destination.
    ("explicit destination", f"lz4 {OUTSIDE}/f {OUTSIDE}/out.lz4",
     (f"{OUTSIDE}/out.lz4",)),
    # `-m` makes every operand an input, each deriving its own sibling.
    ("multiple inputs", f"lz4 -m {OUTSIDE}/f {OUTSIDE}/g",
     (f"{OUTSIDE}/f", f"{OUTSIDE}/g")),
    # …with one of the inputs a stream: measured on the host 2026-09-19 in a fresh
    # directory holding only `f`, `lz4 -m f -` is rc=0 and **does** derive `f.lz4`,
    # so the file must still be named while the bare `-` is not. Naming the token
    # was the false block; dropping the whole run would have been the hole. The row
    # carries that measured shape with an outside path, so both tiers have the file
    # — rather than the stream token — to refuse.
    ("multiple inputs with a stream", f"lz4 -m - {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("recursive", f"lz4 -r {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("block size", f"lz4 -B4 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("threads", f"lz4 -T4 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    # `-D`'s value is the dictionary — a read, and not a target.
    ("dictionary", f"lz4 -D {OUTSIDE}/dict {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("dictionary with -m", f"lz4 -m -D {OUTSIDE}/dict {OUTSIDE}/f {OUTSIDE}/g",
     (f"{OUTSIDE}/f", f"{OUTSIDE}/g")),
    # The same option written **attached**, which is the spelling issue #1426 is
    # about: here the value's own letters must not be read as flags, or a
    # dictionary named `data.txt` reads as `-t` and the run as a read. Measured on
    # the host's binary with the dictionary present, one fresh directory per row:
    # `lz4 -Ddata.txt f` and `lz4 -fDdata.txt f` are rc=0 and create `f.lz4`;
    # under `-m` both operands derive a sibling (`f.lz4` and `g.lz4`).
    ("attached dictionary", f"lz4 -D{OUTSIDE}/data.txt {OUTSIDE}/f",
     (f"{OUTSIDE}/f",)),
    ("attached dictionary after a flag", f"lz4 -fD{OUTSIDE}/data.txt {OUTSIDE}/f",
     (f"{OUTSIDE}/f",)),
    ("attached dictionary with -m", f"lz4 -m -D{OUTSIDE}/cats {OUTSIDE}/f {OUTSIDE}/g",
     (f"{OUTSIDE}/f", f"{OUTSIDE}/g")),
    # The verb's other two writing argv[0] spellings: same file as `lz4` (measured
    # 2026-09-19, all four names one sha256) — see the module docstring.
    ("unlz4 decompress", f"unlz4 {OUTSIDE}/f.lz4", (f"{OUTSIDE}/f.lz4",)),
    ("legacy cli name", f"lz4c {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
)

# (row, command) — the measured spellings that create no file. Each must name
# nothing and stay allowed, or the rule would be a false block.
READ_FORMS = (
    ("-c", f"lz4 -c {OUTSIDE}/f"),
    ("--stdout", f"lz4 --stdout {OUTSIDE}/f"),
    ("-t", f"lz4 -t {OUTSIDE}/f.lz4"),
    ("--test", f"lz4 --test {OUTSIDE}/f.lz4"),
    ("-b benchmark", f"lz4 -b {OUTSIDE}/f"),
    ("--list", f"lz4 --list {OUTSIDE}/f.lz4"),
    ("-dc cluster", f"lz4 -dc {OUTSIDE}/f.lz4"),
    ("-m -c cluster", f"lz4 -m -c {OUTSIDE}/f {OUTSIDE}/g"),
    # The stop is *inside* the token: a read flag in a later token must still be
    # read, or "a value follows" would swallow real flags. Measured — `lz4 -Dcats
    # -c f` is rc=0, 34 bytes on stdout and no file created.
    ("attached dictionary then -c", f"lz4 -D{OUTSIDE}/cats -c {OUTSIDE}/f"),
    # The same parser under the twins' names, so the same reads — measured: each
    # leaves the directory exactly as it found it.
    ("unlz4 -c", f"unlz4 -c {OUTSIDE}/f.lz4"),
    ("unlz4 -t", f"unlz4 -t {OUTSIDE}/f.lz4"),
    ("lz4c -c", f"lz4c -c {OUTSIDE}/f"),
    # `lz4cat` is the twin that writes nothing at all (`lz4 -dc` under a name), so it
    # must name nothing and stay allowed — asserted here because it is the same file
    # as the three writers above, i.e. the false block this fix could most easily
    # cause. It is also a `CAT_WRAPPERS` row in the family's own test file.
    ("lz4cat writes nothing", f"lz4cat {OUTSIDE}/f.lz4"),
    # A bare `-` is the stream, in either position. Measured on the host
    # 2026-09-19, one fresh directory per row with only `f` present and the
    # listing read back off disk: `lz4 -`, `lz4 -9 -` and `lz4 - -` are rc=0 and
    # create no file, and `lz4 f -` is rc=0 with **nothing beside `f`** either —
    # its last operand names stdout as the destination rather than deriving a
    # sibling, so the last-operand rule has to answer with nothing instead of
    # falling back on the operand it only reads. (`lz4 -t -` is rc=44 and creates
    # nothing too, though it is spared by the read letters before this rule.)
    ("bare dash", "lz4 -"),
    ("bare dash with a level", "lz4 -9 -"),
    ("both operands the stream", "lz4 - -"),
    ("dash as the destination", "lz4 f -"),
)

# (row, command) — the compressors *not* on `_COMPRESSOR_VERBS` and not read by a
# rule of their own. None of them is installed on this host, so no name is added on
# documentation alone; they are pinned as the holes they are. A row reds if a later
# cycle names one, which is the point of pinning it.
#
# `pigz`/`unpigz` were the first two rows here and left when they were **measured**
# rather than argued: built from pigz's own release source (`madler/pigz` v2.8),
# every write form rewrote its operand in place and every read form created no file,
# so they are names in the family now, with rows in
# `test_bash_tool_compressor_operands.py` — the same departure `compress` made, and
# the reason this list is a list of measured holes rather than of guesses.
UNLISTED_TWINS = (
    ("pbzip2", f"pbzip2 {OUTSIDE}/f"),
    ("lbzip2", f"lbzip2 {OUTSIDE}/f"),
    ("pixz", f"pixz {OUTSIDE}/f"),
    ("plzip", f"plzip {OUTSIDE}/f"),
    ("lzip", f"lzip {OUTSIDE}/f"),
    ("lzop", f"lzop {OUTSIDE}/f"),
    ("brotli", f"brotli {OUTSIDE}/f"),
)


@pytest.mark.parametrize("row,cmd,target", WRITE_FORMS, ids=[r for r, *_ in WRITE_FORMS])
def test_an_lz4_write_form_names_the_path_it_creates(row, cmd, target) -> None:
    """The named path is the one the run really creates."""
    assert _extract_write_targets(cmd) == list(target), row


@pytest.mark.parametrize("row,cmd,target", WRITE_FORMS, ids=[r for r, *_ in WRITE_FORMS])
def test_an_lz4_write_form_is_refused_at_both_tiers(row, cmd, target) -> None:
    """…and naming it is what makes both tiers refuse, which is the point."""
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is False, f"{row}: {tier} allowed a write to {target}"
        assert reason, row


@pytest.mark.parametrize("row,cmd", READ_FORMS, ids=[r for r, _ in READ_FORMS])
def test_an_lz4_read_form_names_nothing_and_stays_allowed(row, cmd) -> None:
    """The measured read spellings, which a name-only fix would refuse."""
    assert _extract_write_targets(cmd) == [], row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is True, f"{row}: {tier} refused a read ({reason})"


def test_the_sibling_claim_is_what_keeps_the_inside_case_allowed() -> None:
    """Naming the operand is sound *because* the sibling never leaves its dir.

    A rule that blocked `lz4 f` outright would pass both write tests above and be
    wrong: the write lands beside the operand, so an operand inside the workspace
    is an ordinary write. Asserted against the operand form and the explicit
    destination form, since the second names a different path.
    """
    for cmd in (f"lz4 {OUTSIDE}/f".replace(OUTSIDE, "/workspace"),
                f"lz4 {OUTSIDE}/f {OUTSIDE}/out.lz4".replace(OUTSIDE, "/workspace")):
        assert _extract_write_targets(cmd), cmd
        allowed, reason, _ = _check_sandbox(cmd, "workspace-write",
                                            workdir="/workspace")
        assert allowed is True, f"{cmd}: refused an inside write ({reason})"


def test_the_default_form_cannot_touch_a_protected_daemon_file() -> None:
    """The failure this rule exists for, stated as the file it protects.

    Before the rule `lz4` on that path was ALLOW at both tiers with an empty
    target list. The two tiers now refuse it for their own reasons, which is why
    both are asserted rather than just "not allowed".
    """
    allowed, reason, _ = _check_sandbox(f"lz4 {PROTECTED}", "workspace-write",
                                        workdir="/workspace")
    assert allowed is False
    assert "protected daemon file" in reason, reason

    allowed, reason, _ = _check_sandbox(f"lz4 -f {PROTECTED}", "read-only",
                                        workdir="/workspace")
    assert allowed is False
    assert "destructive write" in reason, reason

    # …while the test form of the same command stays a read of the same file.
    assert _check_sandbox(f"lz4 -t {PROTECTED}", "workspace-write",
                          workdir="/workspace")[0] is True


@pytest.mark.parametrize("row,cmd", UNLISTED_TWINS, ids=[r for r, _ in UNLISTED_TWINS])
def test_an_unlisted_compressor_is_pinned_as_a_measured_hole(row, cmd) -> None:
    """A hole pinned as a hole, with the verdict it really gets.

    Two claims, and they are different: the walk names **no** target (which is why
    the tiers below allow — the loop that judges targets never runs), and the tier
    verdicts themselves. Pinning only the second would let the row read as "the
    walk handles this verb" when the block, if any, came from somewhere else.
    """
    assert _extract_write_targets(cmd) == [], (
        f"{row}: the walk now names a target for {cmd!r} — this row is no longer a "
        "residual, so state the measured rule that names it and move the row"
    )
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is True, f"{row}: {tier} gave {allowed} ({reason})"
        assert reason is None, row


# ── mutation arms: a row that cannot be flipped is not a claim ──────────────


def test_each_write_row_dies_when_the_verb_leaves_the_rule() -> None:
    """Drop a spelling, and its own rows must go back to the ALLOW master gave.

    One arm per name, because three argv[0] spellings share this rule and each is a
    separate entry in the set: dropping the one a row rides on must flip that row,
    and a row that survived would not depend on the branch it claims to test.
    """
    rows = (
        ("lz4", f"lz4 {OUTSIDE}/f"),
        ("unlz4", f"unlz4 {OUTSIDE}/f.lz4"),
        ("lz4c", f"lz4c {OUTSIDE}/f"),
    )
    original = bash_tool._LZ4_VERBS
    for verb, cmd in rows:
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, verb
        try:
            bash_tool._LZ4_VERBS = original - {verb}
            allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
        finally:
            bash_tool._LZ4_VERBS = original
        assert allowed is True, (
            f"the {verb} row survives dropping {verb} from _LZ4_VERBS — it does not "
            "depend on the branch it claims to test"
        )


def test_the_read_gate_is_what_spares_the_read_forms() -> None:
    """The gate is a second piece of code, so it gets its own arm, both ways.

    One direction widens the gate (every letter becomes a read) and the flagged
    write row must return to ALLOW; the other empties it and the read rows must
    become refusals. A rule that named the operands without the gate would pass
    neither.
    """
    write_row = f"lz4 -f {OUTSIDE}/f"
    read_row = f"lz4 -c {OUTSIDE}/f"
    assert _check_sandbox(write_row, "read-only", workdir="/workspace")[0] is False
    assert _check_sandbox(read_row, "read-only", workdir="/workspace")[0] is True

    letters, longs = bash_tool._LZ4_READ_LETTERS, bash_tool._LZ4_READ_LONG
    try:
        bash_tool._LZ4_READ_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyz")
        assert _check_sandbox(write_row, "read-only", workdir="/workspace")[0] is True, (
            "with the gate forced open the write row must return to the ALLOW master "
            "gave — otherwise the gate is not what refuses it"
        )
        bash_tool._LZ4_READ_LETTERS, bash_tool._LZ4_READ_LONG = frozenset(), frozenset()
        for row in (read_row, f"lz4 -t {OUTSIDE}/f.lz4", f"lz4 --list {OUTSIDE}/f.lz4"):
            assert _check_sandbox(row, "read-only", workdir="/workspace")[0] is False, (
                f"{row}: with the gate forced shut the read form must be refused — "
                "otherwise nothing about it depends on the gate"
            )
    finally:
        bash_tool._LZ4_READ_LETTERS, bash_tool._LZ4_READ_LONG = letters, longs


def test_the_multi_input_rule_is_what_spares_the_second_operand() -> None:
    """`-m` is a third piece of the rule, so it is flipped on its own.

    Without it the row would fall to the last-operand rule and name `g` only,
    leaving `f` — whose sibling is really written — unnoticed.
    """
    cmd = f"lz4 -m {OUTSIDE}/f {OUTSIDE}/g"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/f", f"{OUTSIDE}/g"]
    letters, longs = bash_tool._LZ4_MULTI_LETTERS, bash_tool._LZ4_MULTI_LONG
    try:
        bash_tool._LZ4_MULTI_LETTERS = frozenset()
        bash_tool._LZ4_MULTI_LONG = frozenset()
        assert _extract_write_targets(cmd) == [f"{OUTSIDE}/g"], (
            "with the multi-input reading gone the row still names both operands — "
            "it does not depend on that rule"
        )
    finally:
        bash_tool._LZ4_MULTI_LETTERS, bash_tool._LZ4_MULTI_LONG = letters, longs


def test_the_stream_operand_is_what_spares_the_bare_dash_here_too() -> None:
    """The same drop as the family's, in this verb's multi-input branch.

    With it removed that row names the bare `-` again and goes back to the
    refusal master gave, i.e. the row is refused *because* the token is dropped.
    The destination row (`lz4 f -`) is guarded by the last-operand branch's own
    test rather than by the drop — the drop would leave `f` there, which is the
    operand it only reads — so it is asserted in both states instead of flipped.
    """
    drop = bash_tool._without_the_stream_operand
    multi = "lz4 -m f -"
    destination = "lz4 f -"

    assert _extract_write_targets(multi) == ["f"]
    assert _extract_write_targets(destination) == []
    assert _check_sandbox(destination, "read-only", workdir="/workspace")[0] is True

    try:
        bash_tool._without_the_stream_operand = lambda targets: list(targets)
        assert _check_sandbox(multi, "read-only", workdir="/workspace")[0] is False, (
            "with the drop removed the stream operand must be named again — "
            "otherwise the drop is not what spares it"
        )
        assert _extract_write_targets(destination) == [], (
            "the destination row is not the drop's to spare: it must stay empty "
            "with the drop removed too, or `f` would be named for a run that "
            "writes to stdout"
        )
    finally:
        bash_tool._without_the_stream_operand = drop

    assert _extract_write_targets(multi) == ["f"]
    assert _extract_write_targets(destination) == []


def test_the_attached_value_is_what_stops_the_letter_scan() -> None:
    """The stop is its own piece of the rule, so it is flipped on its own.

    With the value-taking letters gone, the letters of the dictionary's *name* are
    read as flags again — the state issue #1426 was filed in, where the target list
    came back empty and both tiers allowed a write the binary really performs
    (`/outside/emrg/cats` carries a `t`, so the run reads as a read).
    """
    cmd = f"lz4 -D{OUTSIDE}/cats {OUTSIDE}/f"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/f"]
    original = bash_tool._LZ4_VALUE_TAKING_SHORT
    try:
        bash_tool._LZ4_VALUE_TAKING_SHORT = frozenset()
        assert _extract_write_targets(cmd) == [], (
            "with the value-taking letters gone the attached value is still read as "
            "flags — the row does not depend on the stop it claims to test"
        )
    finally:
        bash_tool._LZ4_VALUE_TAKING_SHORT = original


def test_the_dictionary_value_is_not_named() -> None:
    """The value table is the fourth piece, and this is the row that needs it.

    Under `-m` every operand is named, so a dictionary read as an operand would be
    refused — a false block on a file the run only reads.
    """
    cmd = f"lz4 -m -D {OUTSIDE}/dict {OUTSIDE}/f {OUTSIDE}/g"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/f", f"{OUTSIDE}/g"]
    original = bash_tool._LZ4_OPTIONS_WITH_VALUE
    try:
        bash_tool._LZ4_OPTIONS_WITH_VALUE = frozenset()
        assert _extract_write_targets(cmd) == [
            f"{OUTSIDE}/dict", f"{OUTSIDE}/f", f"{OUTSIDE}/g",
        ], (
            "without the value table the dictionary is not named — the row does not "
            "depend on it"
        )
    finally:
        bash_tool._LZ4_OPTIONS_WITH_VALUE = original
