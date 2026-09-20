"""`patch` rewrites the files it is pointed at, and the walk named none of them.

`patch` is the same class of everyday writer as `sed -i` / `perl -i` / `truncate` /
the compressors: a program whose *purpose* is to modify a file in place. Measured on
master `15733088` with the real predicate (nothing executed — the paths below are
arguments to a pure predicate), the target outside every allowed root:

* `patch <outside>/f`, `patch -o <outside>/out <workspace>/in` and
  `patch -d <outside> <workspace>/f` each reported an **empty target list** at both
  tiers, and an empty list is allowed by construction — the loop that judges targets
  never runs;
* `rm`, `truncate -s 0` and `cp` on the same paths were refused in the same geometry,
  which is what makes this a hole rather than an opinion.

Ground truth for what the program really does, taken in a scratch directory on this
host and read back off disk (BSD `patch 2.0-12u11-Apple`, 2026-09-19):

* `patch v.txt < d.patch` → rc=0, `v.txt` rewritten (`two` → `TWO`);
* `patch -o out.txt v2.txt < d.patch` → rc=0, `out.txt` holds the patched text and
  **`v2.txt` is untouched** — with `-o` the operand is a source;
* `patch -i d.patch v.txt` → rc=0, `v.txt` rewritten (`-i` names the patch to read);
* `patch -d sub v.txt < d.patch` → rc=0, **`sub/v.txt`** is rewritten and the cwd's
  copy is not; `patch -dsub v.txt` behaves the same;
* `patch --dry-run v.txt < d.patch` → rc=0, prints "patching file v.txt", `v.txt`
  **unchanged** — the one read spelling;
* `patch -s v.txt < d.patch` → rc=0, rewritten (quiet is not a read);
* `patch -o only.txt < d.patch` with no operand → rc=1, no output file, and
  `only.txt.rej` lands beside the `-o` path;
* `patch < d.patch` with no operand and no `-o` → rc=0, the file named by the diff
  **header** is rewritten.

Four directions this file pins, because a rule for one can be wrong in the others:

* the **write forms** name the operand;
* `-o`/`--output` **displaces** the operand list — the two readings are alternatives,
  not additions;
* `-d`/`--directory` is where the write **lands**, so its value is named as well;
* `--dry-run` writes nothing and must stay allowed, or the fix would refuse a run that
  changes no byte.

A fifth was added with #1450: both of those path-taking options are read in their
**clustered** spelling (`-sd<dir>`, `-so <file>`) as well, since a cluster is where
getopt puts a value that the spaced-spelling reader never sees. Measured, `patch -sd out f`
rc=0 leaves the cwd's copy alone and rewrites `out/f` — the same write as `-d out`, which
was refused while the cluster was not. The letters a cluster is split with are this
verb's own (`_PATCH_OPTIONS_WITH_VALUE`), and the two spellings that are **not** clusters
(`-d<dir>` attached, `-d <dir>` spaced) are read from the option's own set, so a cluster
arm cannot make them disappear. One row of that grammar cuts the other way and is pinned
beside the rest: `-b`'s argument is **optional**, so `patch -bsd <outside>/f` really
rewrites `<outside>/f` while the reader — taking the `d` for a value-taking letter — ate
that word and named nothing at all; `tests/…patch_targets` names it, and
`_positional_args`' `cluster_optional_arg_letters` is what reads it.

Nothing here executes a command: `_check_sandbox` is a pure predicate that `realpath`s
a path and opens nothing, so the protected path below is an *input* to a predicate
rather than something a test can damage. That matters because most assertions are
negative ("the guard refuses X"), and a negative test is only harmless while the guard
works.
"""

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import _check_sandbox, _extract_write_targets

# Outside every allowed root (the workspace, the OS temp root, the evolution data dir),
# and only ever an argument to the pure predicate — never executed, never opened.
OUTSIDE = "/outside/emrg"

WORKSPACE = "/workspace"

# The daemon's own rant store: a protected file, and also only ever an input.
PROTECTED = "~/.emrg/rants.jsonl"

# (row, command, every path the walk must name, in order). The patch file (`-i`) is
# deliberately inside the workspace: a walk that mistook it for a write would be caught
# by the *named* tuple rather than by a verdict, because the verdict would not move.
WRITE_FORMS = (
    ("bare operand", f"patch {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("after --", f"patch -- {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("strip attached", f"patch -p1 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("strip spaced", f"patch -p 1 {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("quiet", f"patch -s {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("backup", f"patch -b {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("reverse", f"patch -R {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("patch file, spaced", f"patch -i {WORKSPACE}/d.patch {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("patch file, attached", f"patch -i{WORKSPACE}/d.patch {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
    ("patch file, long", f"patch --input={WORKSPACE}/d.patch {OUTSIDE}/f", (f"{OUTSIDE}/f",)),
)

# `-o`/`--output` sends the result elsewhere: the operand becomes a *source*, so it must
# not be named and the option's value must be.
OUTPUT_FORMS = (
    ("output, spaced", f"patch -o {OUTSIDE}/out {WORKSPACE}/in", (f"{OUTSIDE}/out",)),
    ("output, attached", f"patch -o{OUTSIDE}/out {WORKSPACE}/in", (f"{OUTSIDE}/out",)),
    ("output, long", f"patch --output={OUTSIDE}/out {WORKSPACE}/in", (f"{OUTSIDE}/out",)),
    ("output, no operand", f"patch -o {OUTSIDE}/out", (f"{OUTSIDE}/out",)),
    ("output, cluster attached", f"patch -so{OUTSIDE}/out {WORKSPACE}/in",
     (f"{OUTSIDE}/out",)),
    ("output, cluster spaced", f"patch -so {OUTSIDE}/out {WORKSPACE}/in",
     (f"{OUTSIDE}/out",)),
)

# `-d` changes directory first, so the operand's write lands *there*. The operand here
# is deliberately inside the workspace: naming only it is the miss measured on master.
# The two clustered rows are the ones #1450 measured as allowed at both tiers.
DIRECTORY_FORMS = (
    ("chdir, spaced", f"patch -d {OUTSIDE} {WORKSPACE}/f", (f"{WORKSPACE}/f", OUTSIDE)),
    ("chdir, attached", f"patch -d{OUTSIDE} {WORKSPACE}/f", (f"{WORKSPACE}/f", OUTSIDE)),
    ("chdir, long", f"patch --directory={OUTSIDE} {WORKSPACE}/f",
     (f"{WORKSPACE}/f", OUTSIDE)),
    ("chdir, cluster attached", f"patch -sd{OUTSIDE} {WORKSPACE}/f",
     (f"{WORKSPACE}/f", OUTSIDE)),
    ("chdir, cluster spaced", f"patch -sd {OUTSIDE} {WORKSPACE}/f",
     (f"{WORKSPACE}/f", OUTSIDE)),
)

# (row, command) — a run that changes no byte and must stay allowed at both tiers.
READ_FORMS = (
    ("dry run", f"patch --dry-run {OUTSIDE}/f"),
    ("dry run with an output", f"patch --dry-run -o {OUTSIDE}/out {WORKSPACE}/in"),
)

# A word a cluster's letter ate, so the scan must step over it. `patch -d -sd <dir> f` is
# `-d` taking the *word* `-sd` as its directory: the eaten `-sd` is not a cluster, and a
# reader that scans it again reports the `d` inside it and names `<dir>` a second time.
# The name itself is not this row's claim — the operand reader eats `-sd` as `-d`'s spaced
# value and reads `<dir>` as an operand either way — so the *duplicate* is the
# discriminator, and `test_the_step_is_what_keeps_the_eaten_word_out_of_the_cluster_read`
# is the arm that flips it. Measured on the landing tree of #1451 (issue #1454).
EATEN_WORD_FORMS = (
    ("chdir eats the cluster", f"patch -d -sd {OUTSIDE}/dir {WORKSPACE}/f",
     (f"{OUTSIDE}/dir", f"{WORKSPACE}/f", "-sd")),
)

# (row, command) — the residual: with no operand and no `-o`, the paths come from the
# diff's own content, which no static scan can read. Pinned as a measured hole with the
# verdict it really gets, the shape `tests/test_bash_tool_option_destinations.py` uses
# for the `tar` family, rather than guessed at or left undocumented.
CONTENT_DECIDES_FORMS = (
    ("no operand at all", "patch"),
    ("patch file only", f"patch -i {OUTSIDE}/d.patch"),
    ("strip only", "patch -p1"),
    ("redirect only", f"patch < {OUTSIDE}/d.patch"),
)


@pytest.mark.parametrize(
    "row,cmd,named", WRITE_FORMS + OUTPUT_FORMS, ids=[r for r, *_ in WRITE_FORMS + OUTPUT_FORMS]
)
def test_a_patch_run_names_the_file_it_writes(row, cmd, named) -> None:
    """The operand is the file rewritten, or `-o`'s value is when it displaces that."""
    assert tuple(_extract_write_targets(cmd)) == named, row


@pytest.mark.parametrize(
    "row,cmd,named", WRITE_FORMS + OUTPUT_FORMS, ids=[r for r, *_ in WRITE_FORMS + OUTPUT_FORMS]
)
def test_both_tiers_refuse_a_patch_run_that_leaves_the_workspace(row, cmd, named) -> None:
    """…and naming the path is what makes both tiers refuse, which is the point.

    The tiers refuse for their own reasons (`read-only` names the destructive write,
    `workspace-write` the path outside), so both are asserted, and each block must name
    the path the command would write — a guard whose message points elsewhere is a guard
    nobody can trust.
    """
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir=WORKSPACE)
        assert allowed is False, f"{row}: {tier} allowed a write to {named[0]}"
        assert named[0] in reason, f"{row}: {tier} block does not name {named[0]}"


@pytest.mark.parametrize(
    "row,cmd,named", DIRECTORY_FORMS, ids=[r for r, *_ in DIRECTORY_FORMS]
)
def test_the_chdir_value_is_named_beside_the_operand(row, cmd, named) -> None:
    """`-d <dir>` is where the write lands, so `patch -d <outside> <inside>/f` must block.

    Naming only the operand would read this as an in-workspace write — the miss measured
    against master, where the run was allowed at `workspace-write` while the file really
    rewritten was the one under `-d`'s directory.
    """
    assert tuple(_extract_write_targets(cmd)) == named, row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir=WORKSPACE)
        assert allowed is False, f"{row}: {tier} allowed a write under {OUTSIDE}"
        assert named[0] in reason or OUTSIDE in reason, (
            f"{row}: {tier} block names neither path the command would write: {reason}"
        )
    # The discriminating half: `workspace-write` allows the workspace's own copy, so the
    # block it reports can only be the outside directory — which is where the write lands.
    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir=WORKSPACE)
    assert OUTSIDE in reason, f"{row}: workspace-write block does not name {OUTSIDE}"


@pytest.mark.parametrize(
    "row,cmd,named", EATEN_WORD_FORMS, ids=[r for r, *_ in EATEN_WORD_FORMS]
)
def test_a_word_a_cluster_ate_is_stepped_over_not_read_twice(row, cmd, named) -> None:
    """Exactly the list a run names — the duplicate is the whole assertion.

    `patch -d -sd <dir> f` is `-d` taking the word `-sd` as its directory, and the run
    writes `<dir>/f`. Both readings name that path; what separates them is that a reader
    which scans the eaten `-sd` again finds the `d` inside it and reports a cluster too,
    so the same directory lands in the list twice. Asserting the *tuple* rather than a
    verdict is therefore the only way to pin it: the verdict is identical either way, which
    is why the step went unpinned until issue #1454 — and why a row that only says "BLOCK"
    here would pass on the mutated tree as well.
    """
    assert tuple(_extract_write_targets(cmd)) == named, row


@pytest.mark.parametrize("row,cmd", READ_FORMS, ids=[r for r, _ in READ_FORMS])
def test_a_dry_run_names_nothing_and_stays_allowed(row, cmd) -> None:
    """`--dry-run` prints what it would do and writes nothing (measured above)."""
    assert _extract_write_targets(cmd) == [], row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir=WORKSPACE)
        assert allowed is True, f"{row}: {tier} refused a dry run ({reason})"


@pytest.mark.parametrize(
    "row,cmd", CONTENT_DECIDES_FORMS, ids=[r for r, _ in CONTENT_DECIDES_FORMS]
)
def test_the_content_decides_residual_is_named_as_one(row, cmd) -> None:
    """A run with no operand and no `-o` writes the paths **inside the diff**.

    That is content no static scan can read, so the walk names nothing and both tiers
    allow — pinned here so the residual is a recorded decision rather than an
    undiscovered hole, and so a later reader who teaches this walk to read patch bodies
    finds the row that must move with it.
    """
    assert _extract_write_targets(cmd) == [], row
    for tier in ("read-only", "workspace-write"):
        allowed, _reason, _ = _check_sandbox(cmd, tier, workdir=WORKSPACE)
        assert allowed is True, row


def test_patch_cannot_touch_a_protected_daemon_file() -> None:
    """The failure this fix exists for, stated as the file it protects.

    `~/.emrg/rants.jsonl` is the host's rant store. Before the fix `patch` on that path
    was ALLOW at both tiers with an empty target list.
    """
    allowed, reason, _ = _check_sandbox(
        f"patch {PROTECTED}", "workspace-write", workdir=WORKSPACE
    )
    assert allowed is False
    assert "protected daemon file" in reason, reason

    allowed, reason, _ = _check_sandbox(f"patch {PROTECTED}", "read-only", workdir=WORKSPACE)
    assert allowed is False
    assert "destructive write" in reason, reason


def test_an_in_workspace_operand_is_refused_by_read_only_alone() -> None:
    """The two tiers differ here, and that difference is why this is worth fixing.

    `workspace-write` guards the workspace *boundary* and this write is inside it;
    `read-only` exists to protect uncommitted work, and a file in the workspace is
    exactly the work at risk.
    """
    cmd = f"patch {WORKSPACE}/f"
    assert _extract_write_targets(cmd) == [f"{WORKSPACE}/f"]

    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir=WORKSPACE)
    assert allowed is True and reason is None

    allowed, reason, _ = _check_sandbox(cmd, "read-only", workdir=WORKSPACE)
    assert allowed is False
    assert "destructive write" in reason, reason


def test_the_option_value_table_keeps_a_count_out_of_the_target_list() -> None:
    """`patch -p 1 f` must name the file, never the strip count.

    A block whose message points at `'1'` is the wrong-name defect `_positional_args`
    exists to avoid, and the spaced spelling is the one that reaches it: the attached
    `-p1` is dropped as an option-shaped token either way.
    """
    cmd = f"patch -p 1 {OUTSIDE}/f"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/f"], "the table did not consume -p's value"


def test_the_geometry_is_what_makes_this_a_fix() -> None:
    """A control in the same geometry: `cp` of the same two paths is refused.

    Without it, "the walk now names a target" would be indistinguishable from "this
    geometry blocks everything".
    """
    allowed, _reason, _ = _check_sandbox(
        f"cp {WORKSPACE}/in {OUTSIDE}/out", "workspace-write", workdir=WORKSPACE
    )
    assert allowed is False


def test_the_scan_stops_at_the_first_value_taking_letter() -> None:
    """`-i`'s value is the rest of the token, so `-isd…` has no `-d` in force.

    This is the control that makes the cluster rows a grammar rather than a search for a
    `d`: measured, `patch -i <abs> -isd<dir> f` is rc=2 `too many file arguments` with
    nothing written, because `-i` took `sd<dir>` as its value. Naming `<dir>` would be a
    false block — and the *word* `-i` would have eaten must not be named either, which is
    what an unnamed value buys.
    """
    attached = f"patch -isd{OUTSIDE} {WORKSPACE}/f"
    assert _extract_write_targets(attached) == [f"{WORKSPACE}/f"], (
        "the tail belongs to -i, so no directory is named"
    )
    allowed, _reason, _ = _check_sandbox(attached, "workspace-write", workdir=WORKSPACE)
    assert allowed is True, "and the run, which writes nothing, must not be refused"

    # The spaced spelling is the other side of the same grammar: `-i` took `sd` from its
    # own token, so `<dir>` is a genuine **operand** and the operand rule names it (the
    # run then fails on two file arguments — measured rc=2). Naming it is the operand
    # rule's business, not this reading's: nothing here calls it a destination.
    spaced = f"patch -isd {OUTSIDE} {WORKSPACE}/f"
    assert _extract_write_targets(spaced) == [OUTSIDE, f"{WORKSPACE}/f"], (
        "an operand the run is given is named, whatever it spells"
    )


def test_an_option_with_an_optional_argument_owns_its_tokens_tail() -> None:
    """`-b`'s argument is optional, so `-bsdout` is `-b sdout` — no chdir, no output.

    `strings /usr/bin/patch` gives this host's optstring as
    `b::B:cCd:D:eEfF:g:i:lnNo:p:r:RstTuvV:x:Y:z:Z`; an optional argument takes the rest
    of its own token and never the next word. Measured: `patch -i <abs> -bsdout f` rc=0
    rewrites the cwd's `f` and never `cd`s to `out`, and `patch -i <abs> -bsd out f`
    rc=2 `too many file arguments`, so `-d` did not take `out`. Splitting such a token
    with the required-argument letters alone would name `out` — a directory the run
    never enters, and a false block the moment it lies outside the workspace.
    """
    swallowed = f"patch -bsdout {WORKSPACE}/f"
    assert _extract_write_targets(swallowed) == [f"{WORKSPACE}/f"], (
        "-b swallowed the tail, so neither `out` nor a directory is named"
    )
    allowed, _reason, _ = _check_sandbox(swallowed, "workspace-write", workdir=WORKSPACE)
    assert allowed is True, "and the in-workspace write it really does stays allowed"

    spaced = f"patch -bsd {OUTSIDE} {WORKSPACE}/f"
    assert _extract_write_targets(spaced) == [OUTSIDE, f"{WORKSPACE}/f"], (
        "-b took no word, so both tokens are operands — which is why the run fails"
    )


def test_a_swallowed_tail_keeps_the_operand_the_run_really_writes() -> None:
    """The same fact in the direction that was a real hole, not a false name.

    With `-b` swallowing the tail there is no `-d` in force, so the word after the token
    is an **operand** — and for the single-operand form that word is the file rewritten.
    Measured: `patch -i <abs> -bsd outside/f` rc=0 rewrites `outside/f`. Before the
    operand reader knew about optional arguments it ate that word (taking the `d` for a
    value-taking letter) and reported **no target at all**, which is ALLOW at both tiers.
    """
    hole = f"patch -bsd {OUTSIDE}/f"
    assert _extract_write_targets(hole) == [f"{OUTSIDE}/f"], (
        "the word after the token is the operand, not -d's value"
    )
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(hole, tier, workdir=WORKSPACE)
        assert allowed is False, f"{tier} allowed a rewrite of {OUTSIDE}/f"
        assert f"{OUTSIDE}/f" in reason, reason

    # The same row without the tail: `-b` and `-d` are separate options there, so `-d`
    # takes the word and the run names the directory instead — the row the cluster fix
    # is about, and it must keep refusing.
    together = f"patch -b -d {OUTSIDE} {WORKSPACE}/f"
    assert _extract_write_targets(together) == [f"{WORKSPACE}/f", OUTSIDE]


def test_blinding_the_optional_argument_letters_names_the_swallowed_tail(monkeypatch) -> None:
    """Empty the optional-argument table and the swallowed tail is read as a destination.

    `-bdout`/`-bsdout` really rewrite the cwd's file (`-b` took `dout`/`sdout`), and
    `-bso<out>` really rewrites its operand — so both readings the arm produces are names
    of a path the run never writes. That is what the table is for, and it is asserted on
    the *names*, since the verdict would not move for a relative name.
    """
    swallowed = f"patch -bdout {WORKSPACE}/f"
    # And the clustered value the same token *is* not: `-bso<out> in` rewrites `in` in
    # place (measured), so naming `<out>` would be a false block, not a miss.
    output = f"patch -bso{OUTSIDE}/out {WORKSPACE}/in"
    assert _extract_write_targets(swallowed) == [f"{WORKSPACE}/f"], "not the unmutated read"
    assert _extract_write_targets(output) == [f"{WORKSPACE}/in"], "not the unmutated read"

    monkeypatch.setattr(bash_tool, "_PATCH_OPTIONAL_ARG_LETTERS", frozenset())
    assert _extract_write_targets(swallowed) == [f"{WORKSPACE}/f", "out"], (
        "with the table empty the swallowed tail is named as a directory"
    )
    assert _extract_write_targets(output) == [f"{OUTSIDE}/out"], (
        "with the table empty the swallowed tail is read as the clustered -o's value"
    )


# ── mutation arms: a row that cannot be flipped is not a claim ──────────────


def test_blinding_the_value_table_names_the_strip_count(monkeypatch) -> None:
    """Empty the per-verb option table and the operand stops being the only target.

    The flip is a *wrong name*, not a verdict change, which is why it is asserted on the
    target list: `1` is a count, not a path.
    """
    cmd = f"patch -p 1 {OUTSIDE}/f"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/f"], "not the unmutated reading"
    monkeypatch.setattr(bash_tool, "_PATCH_OPTIONS_WITH_VALUE", frozenset())
    assert _extract_write_targets(cmd) == ["1", f"{OUTSIDE}/f"], (
        "with the table empty the count must be named too — otherwise the table is not "
        "what keeps it out"
    )


def test_blinding_the_output_set_leaves_the_destination_unnamed(monkeypatch) -> None:
    """Empty the `-o` set and the write it names becomes invisible again.

    This is the pre-fix reading of every `-o` row, so it is the direction that proves the
    output half has a job of its own rather than riding on the operand rule.
    """
    cmd = f"patch -o {OUTSIDE}/out {WORKSPACE}/in"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/out"], "not the unmutated reading"
    monkeypatch.setattr(bash_tool, "_PATCH_OUTPUT_OPTIONS", frozenset())
    assert _extract_write_targets(cmd) == [f"{WORKSPACE}/in"], (
        "with the output set empty the operand must be named instead — the source, which "
        "is the reading this branch exists to displace"
    )
    allowed, _reason, _ = _check_sandbox(cmd, "workspace-write", workdir=WORKSPACE)
    assert allowed is True, "and the outside destination must go unnamed again"


def test_blinding_the_read_table_refuses_a_dry_run(monkeypatch) -> None:
    """Empty `--dry-run` and a run that writes nothing becomes a refusal.

    The false block is the error this walk weighs most heavily, and only this arm shows
    that the read spelling is what spares it.
    """
    cmd = f"patch --dry-run {OUTSIDE}/f"
    assert _extract_write_targets(cmd) == [] and _check_sandbox(
        cmd, "read-only", workdir=WORKSPACE
    )[0] is True, "not the unmutated reading"
    monkeypatch.setattr(bash_tool, "_PATCH_READ_LONG", frozenset())
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/f"], (
        "with the read table empty the dry run must name its operand"
    )
    assert _check_sandbox(cmd, "read-only", workdir=WORKSPACE)[0] is False, (
        "…and be refused, or nothing about the dry-run row depends on that table"
    )


def test_blinding_the_chdir_set_reads_an_outside_write_as_an_inside_one(monkeypatch) -> None:
    """Empty the `-d` set and the miss measured on master comes back, row for row."""
    cmd = f"patch -d {OUTSIDE} {WORKSPACE}/f"
    assert tuple(_extract_write_targets(cmd)) == (f"{WORKSPACE}/f", OUTSIDE)
    monkeypatch.setattr(bash_tool, "_PATCH_DIRECTORY_OPTIONS", frozenset())
    assert _extract_write_targets(cmd) == [f"{WORKSPACE}/f"], (
        "with the chdir set empty only the in-workspace operand is named"
    )
    allowed, _reason, _ = _check_sandbox(cmd, "workspace-write", workdir=WORKSPACE)
    assert allowed is True, (
        "and the outside write is allowed again — which is exactly the hole this branch "
        "was written for"
    )


# The arm below empties the letters a cluster is split with, and asserts both halves of
# the claim: the clustered spellings fall back to master's reading, and the two spellings
# that are not clusters survive — those read the option's **own** set, so a cluster arm
# must not reach them. (row, command, master's reading of that command)
CLUSTER_DEPENDS_ROWS = (
    ("chdir, cluster attached", f"patch -sd{OUTSIDE} {WORKSPACE}/f", [f"{WORKSPACE}/f"]),
    ("chdir, cluster spaced", f"patch -sd {OUTSIDE} {WORKSPACE}/f", [f"{WORKSPACE}/f"]),
    ("output, cluster attached", f"patch -so{OUTSIDE}/out {WORKSPACE}/in",
     [f"{WORKSPACE}/in"]),
    ("output, cluster spaced", f"patch -so {OUTSIDE}/out {WORKSPACE}/in",
     [f"{WORKSPACE}/in"]),
)

CLUSTER_INDEPENDENT_ROWS = (
    ("chdir, attached", f"patch -d{OUTSIDE} {WORKSPACE}/f", [f"{WORKSPACE}/f", OUTSIDE]),
    ("chdir, spaced", f"patch -d {OUTSIDE} {WORKSPACE}/f", [f"{WORKSPACE}/f", OUTSIDE]),
    ("output, attached", f"patch -o{OUTSIDE}/out {WORKSPACE}/in", [f"{OUTSIDE}/out"]),
    ("output, spaced", f"patch -o {OUTSIDE}/out {WORKSPACE}/in", [f"{OUTSIDE}/out"]),
)


@pytest.mark.parametrize(
    "row,cmd,masters_reading",
    CLUSTER_DEPENDS_ROWS,
    ids=[r for r, *_ in CLUSTER_DEPENDS_ROWS],
)
def test_blinding_the_cluster_letters_returns_the_clustered_rows_to_masters_reading(
    row, cmd, masters_reading, monkeypatch
) -> None:
    """Empty the letters a cluster is split with: the destination is unnamed again.

    Both halves are asserted, because either alone proves nothing. The reading *before*
    the arm must differ from master's — otherwise the rows would not be evidence that the
    cluster is what names the destination — and the reading *after* must be master's, with
    the tier allowing the outside write again.
    """
    assert _extract_write_targets(cmd) != masters_reading, (
        "not the reading this arm is about: the clustered destination is already unnamed"
    )
    assert _check_sandbox(cmd, "workspace-write", workdir=WORKSPACE)[0] is False, (
        "and it must be refused before the arm, or there is no hole to reopen"
    )
    monkeypatch.setattr(bash_tool, "_PATCH_CLUSTER_LETTERS", frozenset())
    assert _extract_write_targets(cmd) == masters_reading, (
        f"{row}: master's reading of this command is {masters_reading}"
    )
    allowed, _reason, _ = _check_sandbox(cmd, "workspace-write", workdir=WORKSPACE)
    assert allowed is True, f"{row}: with the letters gone the hole must reopen"


@pytest.mark.parametrize(
    "row,cmd,expected",
    CLUSTER_INDEPENDENT_ROWS,
    ids=[r for r, *_ in CLUSTER_INDEPENDENT_ROWS],
)
def test_the_spellings_that_are_not_clusters_survive_the_cluster_arm(
    row, cmd, expected, monkeypatch
) -> None:
    """The attached and spaced spellings read the option's **own** set, not the letters.

    That is the difference the issue's acceptance turns on: a fix that read `-d<dir>` and
    `-d <dir>` through the cluster letters would lose them the moment those letters were
    wrong or missing, so the arm above must not reach them.
    """
    monkeypatch.setattr(bash_tool, "_PATCH_CLUSTER_LETTERS", frozenset())
    assert _extract_write_targets(cmd) == expected, row
    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir=WORKSPACE)
    assert allowed is False and OUTSIDE in reason, (
        f"{row}: the plain spelling is still read, so this write is still refused"
    )


def test_the_step_is_what_keeps_the_eaten_word_out_of_the_cluster_read(monkeypatch) -> None:
    """Stop stepping over the eaten word and the same path is named twice.

    The arm is the shared advance itself (`_words_eaten`), not the letters and not a
    verdict: with it at ``1`` every token is looked at, so the `-sd` that `-d` ate is
    scanned as a cluster and its `d` reports `<dir>` a second time. The verdict does not
    move at either tier, which is exactly why the row above asserts the *list* — and the
    proof that it is a genuine arm is that the unmutated reading is asserted first, so a
    future reader cannot make this test pass by breaking the step in the source.
    """
    cmd = f"patch -d -sd {OUTSIDE}/dir {WORKSPACE}/f"
    unmutated = (f"{OUTSIDE}/dir", f"{WORKSPACE}/f", "-sd")
    assert tuple(_extract_write_targets(cmd)) == unmutated, "not the unmutated reading"
    monkeypatch.setattr(bash_tool, "_words_eaten", lambda attached: 1)
    assert tuple(_extract_write_targets(cmd)) == unmutated + (f"{OUTSIDE}/dir",), (
        "with the step gone the eaten `-sd` must be read as a cluster and name the same "
        "directory again — otherwise the step is not what keeps it out"
    )
