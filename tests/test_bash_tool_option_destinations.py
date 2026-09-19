"""A destination named by an **option**, not by an operand (issue #1398's class).

`curl -o <f>`, `wget -O <f>`, `sort -o <f>` and `unzip -d <d>` put their
destination in an option's value, so no operand rule can reach it. Measured on the
landing tree of #1399 (master + the everyday-writer fix), in one geometry whose
target lay outside every allowed root, all four were **ALLOW at both tiers with an
empty target list** while `truncate`, `tee` and `cp` were refused — an empty target
list is allowed by construction, because the loop that judges targets never runs.

Ground truth is GNU's (`debian:bookworm-slim`, colima), because that is where these
flags can be measured: the sources below are read back off disk in a directory
outside every allowed root. Measured there:

* the destination is written by the spaced form (`sort -o <d>/f x`), the attached
  form (`curl -o<d>/f <url>`, `sort -o<d>/f x`, `unzip -d<d>`), and the long forms
  (`wget --output-document=<d>/f`, `sort --output=<d>/f`);
* a value of exactly `-` writes **nothing** (`wget -O - <url>` and `sort -o - x`
  both leave the directory empty), which is why the reader drops that value;
* two long spellings are rejected by their own tools — `curl --output=<f>` exits 2
  and `unzip --directory=<d>` exits 11, nothing written — while `sort` and `wget`
  accept theirs. The reader takes them anyway, and the asymmetry is recorded in
  `_option_destination_values`' docstring rather than left to be re-derived: the two
  ways of being wrong are not equally costly, and refusing a command that was going
  to fail is the cheap one.

This file is deliberately separate from `test_bash_tool_sandbox.py`: it covers a
different family, and keeping it self-contained keeps the two files' fixtures from
having to agree about a table neither owns.
"""

import asyncio
import os
import shutil
import stat
import sys
import tempfile
import zipfile

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import (
    BashTool,
    _check_sandbox,
    _extract_write_targets,
)


def _run(coro):
    return asyncio.run(coro)


# Outside every allowed root (workspace, OS temp root, the evolution data dir) and
# used only as an argument to the pure predicate — never executed and never opened.
OUTSIDE = "/outside/emrg"

# (row, command, every path the walk must name for it)
OPTION_DESTINATIONS = (
    ("curl -o spaced", f"curl -o {OUTSIDE}/f https://example.invalid/x",
     (f"{OUTSIDE}/f",)),
    ("curl -o attached", f"curl -o{OUTSIDE}/f https://example.invalid/x",
     (f"{OUTSIDE}/f",)),
    ("curl --output spaced", f"curl --output {OUTSIDE}/f https://example.invalid/x",
     (f"{OUTSIDE}/f",)),
    ("curl --output=", f"curl --output={OUTSIDE}/f https://example.invalid/x",
     (f"{OUTSIDE}/f",)),
    ("wget -O spaced", f"wget -O {OUTSIDE}/f https://example.invalid/x",
     (f"{OUTSIDE}/f",)),
    ("wget -O attached", f"wget -O{OUTSIDE}/f https://example.invalid/x",
     (f"{OUTSIDE}/f",)),
    ("wget --output-document",
     f"wget --output-document {OUTSIDE}/f https://example.invalid/x",
     (f"{OUTSIDE}/f",)),
    ("wget --output-document=",
     f"wget --output-document={OUTSIDE}/f https://example.invalid/x",
     (f"{OUTSIDE}/f",)),
    ("sort -o spaced", f"sort -o {OUTSIDE}/f x", (f"{OUTSIDE}/f",)),
    ("sort -o attached", f"sort -o{OUTSIDE}/f x", (f"{OUTSIDE}/f",)),
    ("sort --output spaced", f"sort --output {OUTSIDE}/f x", (f"{OUTSIDE}/f",)),
    ("sort --output=", f"sort --output={OUTSIDE}/f x", (f"{OUTSIDE}/f",)),
    # `unzip -d` names a *directory*, and it may lead or trail the archive operand.
    ("unzip -d trailing", f"unzip a.zip -d {OUTSIDE}", (OUTSIDE,)),
    ("unzip -d leading", f"unzip -d {OUTSIDE} a.zip", (OUTSIDE,)),
    ("unzip -d attached", f"unzip a.zip -d{OUTSIDE}", (OUTSIDE,)),
    ("unzip --directory=", f"unzip a.zip --directory={OUTSIDE}", (OUTSIDE,)),
    # The destination letter carried **inside a cluster**, which needs the verb's own
    # value-taking letters (issue #1448): the first letter in the token that takes a
    # value owns the rest, so `-so` is only `-s` then `-o` when `s` takes none.
    ("curl -so cluster", f"curl -so {OUTSIDE}/f https://example.invalid/x",
     (f"{OUTSIDE}/f",)),
    ("curl -so cluster attached", f"curl -so{OUTSIDE}/f https://example.invalid/x",
     (f"{OUTSIDE}/f",)),
    ("sort -bo cluster", f"sort -bo {OUTSIDE}/f x", (f"{OUTSIDE}/f",)),
    ("sort -bo cluster attached", f"sort -bo{OUTSIDE}/f x", (f"{OUTSIDE}/f",)),
    ("unzip -qd cluster", f"unzip -qd {OUTSIDE} a.zip", (OUTSIDE,)),
    ("unzip -qd cluster attached", f"unzip -qd{OUTSIDE} a.zip", (OUTSIDE,)),
)

_DESTINATION_ROW_IDS = [row for row, _c, _n in OPTION_DESTINATIONS]


@pytest.mark.parametrize("row,cmd,named", OPTION_DESTINATIONS, ids=_DESTINATION_ROW_IDS)
def test_the_walk_names_the_path_an_option_destination_writes(row, cmd, named):
    """The target list, so the refusal can name the file and not just say no.

    An exact tuple rather than a membership check: a row that also named the
    *source* (`x`, the archive operand, the URL) is a different defect — the
    #1398 family has produced one of those already, where `cp -s x <target>`
    reported nothing and the neighbouring `-t` reading reported the source.
    """
    assert tuple(_extract_write_targets(cmd)) == named


@pytest.mark.parametrize("row,cmd,named", OPTION_DESTINATIONS, ids=_DESTINATION_ROW_IDS)
def test_both_tiers_refuse_an_option_destination_that_leaves_the_workspace(row, cmd, named):
    """Both tiers, and `partial` stays the honest label.

    `read-only`'s promise is stronger than its tag ("blocks every destructive
    write") and it is the tier a dirty-tree downgrade drops a task into, so a task
    that could `curl -o` anywhere on the disk was not read-only in any useful sense.
    """
    for tier in ("read-only", "workspace-write"):
        allowed, reason, enforcement = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is False, f"{tier} allowed {cmd!r}"
        assert named[0] in reason, f"{tier} block for {cmd!r} does not name {named[0]!r}"
        assert enforcement == "partial", (
            "an option-destination writer is now read, but an interpreter still "
            "writes anywhere it likes - the label must stay `partial` (issue #1398)"
        )


# The other direction: forms that write inside, write only to stdout, or are reads.
# Naming any of these would be a false block, and the stdout half is the reason the
# reader drops a value of exactly `-`.
INSIDE_STDOUT_OR_READ = (
    "curl -o /workspace/f https://example.invalid/x",
    "curl -o/workspace/f https://example.invalid/x",
    "curl --output /workspace/f https://example.invalid/x",
    "curl -s -o /workspace/f https://example.invalid/x",
    "curl -o - https://example.invalid/x",           # `-` means stdout
    "curl -s https://example.invalid/x",             # no -o: stdout
    "curl -O https://example.invalid/x",             # remote-name in the cwd, unnamed
    "wget -O /workspace/f https://example.invalid/x",
    "wget -O - https://example.invalid/x",           # stdout
    "wget https://example.invalid/x",
    "sort -o /workspace/f x",
    "sort -o/workspace/f x",
    "sort -o - x",                                   # stdout
    "sort x",                                        # stdout only
    f"sort -k 2 -o /workspace/f x",                  # another option before it
    "sort -t : -o /workspace/f x",
    "unzip a.zip -d /workspace",
    "unzip -d /workspace a.zip",
    "unzip -l a.zip",                                # list: a read
    "unzip a.zip",                                   # no -d: extracts into the cwd
    # The cluster's *other* half: a value-taking letter before the destination letter
    # means the destination letter is a value, and the word after it belongs to that
    # option — naming it would be the false block (measured ground truth in
    # `test_a_cluster_that_does_not_end_on_the_destination_letter_names_nothing`).
    "sort -ko /workspace/out.txt x",                 # `-k` takes `o`
    "curl -do /workspace/out https://example.invalid/x",   # `-d` takes `o`
    "unzip -Pd secret a.zip",                        # `-P` takes `d`
    "curl -so - https://example.invalid/x",          # cluster, stdout
    "sort -bo - x",                                  # cluster, stdout
)


@pytest.mark.parametrize("cmd", INSIDE_STDOUT_OR_READ, ids=INSIDE_STDOUT_OR_READ)
def test_forms_that_write_inside_or_to_stdout_are_still_allowed(cmd):
    """The false-block half, on the tier whose documented flow is to keep working.

    `workspace-write` must allow a write inside its own workspace, and every
    stdout-only or read-only shape must name nothing at all — a guard that refuses
    the workspace it is guarding is worse than the hole it closed.
    """
    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir="/workspace")
    assert allowed is True, f"{cmd!r} is a false block: {reason}"


def test_read_only_refuses_an_option_destination_inside_the_workspace_too():
    """`read-only` refuses the write itself, wherever it lands — its contract.

    This is the assertion that moved `curl -s -o /tmp/out.json <url>` out of
    `test_containment_allows_legitimate_commands`'s list (it names a write, and
    that list asserts both tiers): read-only's documented promise is *no writes*,
    which is why `truncate -s 0 <file>` is refused there as well. The workspace
    path is `workdir`-relative in the same shape the allow cases below use.
    """
    for cmd in (f"curl -o /workspace/f https://example.invalid/x",
                "wget -O /workspace/f https://example.invalid/x",
                "sort -o /workspace/f x",
                f"unzip a.zip -d /workspace/out"):
        allowed, reason, _ = _check_sandbox(cmd, "read-only", workdir="/workspace")
        assert allowed is False, f"read-only allowed the write {cmd!r}"
        assert "read-only sandbox" in reason
    # ...while the tier that promises nothing of the sort still allows it, so the
    # refusal above is about the tier and not about the verb becoming unreadable.
    allowed, reason, _ = _check_sandbox(
        "curl -o /workspace/f https://example.invalid/x", "workspace-write",
        workdir="/workspace",
    )
    assert allowed is True, reason


def test_a_stdout_destination_names_nothing_at_all():
    """`-o -` is a *destination* option used to mean "no file" — pinned directly.

    It is the one value the reader drops, so it is asserted on the walk rather than
    only through a tier verdict: a reader that named `-` would put a bare dash in
    the target list and every block message would point at a token that is not a
    path (the rule `_positional_args`'s docstring already sets out).
    """
    assert _extract_write_targets("curl -o - https://example.invalid/x") == []
    assert _extract_write_targets("wget -O - https://example.invalid/x") == []
    assert _extract_write_targets("sort -o - x") == []
    # And the same spelling where the dash is *not* the value is still read.
    assert _extract_write_targets("sort -o -dash.txt x") == ["-dash.txt"]
    # The clustered spellings drop it the same way, through the same filter.
    assert _extract_write_targets("curl -so - https://example.invalid/x") == []
    assert _extract_write_targets("sort -bo - x") == []
    assert _extract_write_targets("sort -bo -dash.txt x") == ["-dash.txt"]


def test_a_cluster_is_split_by_the_verbs_own_value_taking_letters():
    """`curl -so<dir>` is read, and `sort -ko out.txt` is not — the two directions.

    A token that puts another letter before the destination letter can only be split
    by the verb's own grammar, and this is the measurement that says so. Both readings
    were run in a scratch directory on this host and the directory read back off disk
    (BSD `sort 2.3-Apple (199)`, `UnZip 6.00`, `curl 8.7.1`, 2026-09-20):

      ``sort -bo o/out.txt in.txt``   rc=0  `o/out.txt` created
      ``sort -ko o/out.txt in.txt``   rc=2  ``-k o: Invalid argument``, **nothing created**
      ``unzip -qd o/zd a.zip``        rc=0  `m.txt` extracted into `o/zd`
      ``unzip -xd foo a.zip``         rc=0  extracted into `foo/` — so `d` is the option
                                            and `foo` its value, not `x` taking `d`
      ``curl -so o/f file://…``       rc=0  `o/f` holds the file
      ``curl -do o/f <url>``          rc=6  ``Could not resolve host: o/f``, nothing created

    So a cluster is read **from the verb's own letters**, and the second and last rows
    are why those letters have to be the right ones: `-ko` and `-do` put the *next*
    word after an option that already took its value, and naming that word as a
    destination would refuse a command that writes nothing. (`sort -ko` here is the
    same row in both directions — unnamed is correct, and it is asserted in
    `INSIDE_STDOUT_OR_READ` as well, because a false block is the error this walk
    weighs most heavily.)
    """
    for cmd, named in ((f"curl -so {OUTSIDE}/f https://example.invalid/x", f"{OUTSIDE}/f"),
                       (f"curl -so{OUTSIDE}/f https://example.invalid/x", f"{OUTSIDE}/f"),
                       (f"sort -bo {OUTSIDE}/f x", f"{OUTSIDE}/f"),
                       (f"unzip -qd {OUTSIDE} a.zip", OUTSIDE)):
        assert tuple(_extract_write_targets(cmd)) == (named,), cmd
        for tier in ("read-only", "workspace-write"):
            allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
            assert allowed is False, f"{tier} allowed {cmd!r}"
            assert named in reason, reason
    # The residual a previous change pinned here deliberately is gone, and the controls
    # it was pinned against — the same destination spelled as its own token, with and
    # without a flag before it — still name their path: `-s -o <f>` and `-so <f>` now
    # read alike, and `-b -o <f>` and `-bo <f>` do too.
    for cmd in (f"curl -s -o {OUTSIDE}/f https://example.invalid/x",
                f"sort -b -o {OUTSIDE}/f x"):
        assert tuple(_extract_write_targets(cmd)) == (f"{OUTSIDE}/f",), cmd
        assert _check_sandbox(cmd, "workspace-write", workdir="/workspace")[0] is False, cmd


def test_a_cluster_that_does_not_end_on_the_destination_letter_names_nothing():
    """The word after a cluster belongs to whichever letter took a value.

    This is the false-block half of the same reader, asserted on the walk: with the
    value-taking letters in hand `sort -ko out.txt x` resolves to `k` = `o` and an
    operand to **read**, so naming `out.txt` would refuse a run that writes nothing.
    The ground truth is the line above — `sort` exits 2 there with ``-k o: Invalid
    argument`` and creates no file, which is what makes "unnamed" the true reading and
    not merely the timid one.
    """
    for cmd in (f"sort -ko {OUTSIDE}/f x",
                f"curl -do {OUTSIDE}/f https://example.invalid/x",
                f"unzip -Pd secret a.zip",
                f"sort -So {OUTSIDE}/f x"):
        assert _extract_write_targets(cmd) == [], cmd
        assert _check_sandbox(cmd, "workspace-write", workdir="/workspace")[0] is True, cmd


# ── the writers this table deliberately does not cover ─────────────────────
#
# The same fail-open this change closed for `curl`/`wget`/`sort`/`unzip`, left open
# where the destination cannot be read without the verb's own flag grammar. Measured
# this cycle on this branch, predicate only, nothing executed, with the target outside
# every allowed root — the third and fourth columns are the *measured* verdicts, which
# are not uniform: `git clone` reaches `read-only` through the git-mutator rule (issue
# #979), and that block says nothing about its destination.
#
# `rsync` was the fifth row and is gone: this table is for destinations named by an
# **option**, and rsync's is an *operand* (`rsync SRC... DEST`), so the reason the
# table exists never applied to it. It now has its own rule and its own file —
# `tests/test_bash_tool_rsync_destination.py` — measured the same way (predicate only,
# target outside every allowed root) plus ground truth from a real scratch transfer.
#
# `split` was the sixth and has left by the same door: the paths it writes are derived
# from its last *operand* (the prefix), which is the same operand-shaped exception
# rsync made. It now has its own rule (`_SPLIT_OPTIONS_WITH_VALUE`) and its own file —
# `tests/test_bash_tool_split_prefix.py`.
#
# `zip` was pinned here and has left it the same way `rsync` and `split` did: its
# archive is readable from the first *operand* — no flag grammar needed to place it —
# and the read half is a spelling (`-sf`/`--show-files` for show-files, `-T` only
# while it has no list) rather than the "value of an option". Its own rule is
# `_zip_write_targets`, its measured table is the comment above
# `_ZIP_OPTIONS_WITH_VALUE`, and its rows live in
# `tests/test_bash_tool_zip_archive.py`. The departure was measured while moving it:
# with the rule in place this row's own assertion reds (`zip OUT/a.zip x` now names
# `/outside/emrg/a.zip`), which is the signal this table is written to give.
#
# `csplit` has left through the door this table is actually about, which is the
# opposite of rsync's departure: its prefix *is* an option's value, so the production
# table lists it (`_CSPLIT_PREFIX_OPTIONS` in `bash_tool.py`), and what needed a
# separate arm is the half no option spells — a run with no `-f` still writes the
# `xx…` family, in the cwd. Measured on master `910a307c` before the change, predicate
# only, target outside every allowed root: `csplit x /re/ -f OUT/pre` reported an
# empty target list at both tiers; after it, the prefix is the reported target and is
# refused at both. Ground truth from a scratch directory on this host (BSD `csplit`):
# `csplit -f pfx in.txt 4 8` created `pfx00 pfx01 pfx02` beside `in.txt`, and
# `csplit in.txt 4` created `xx00 xx01`. Its file is
# `tests/test_bash_tool_csplit_prefix.py`.
#
# `curl -so cluster` was the seventh row and has left the same way: the destination in
# a *cluster* is readable once the verb's own value-taking letters are known — that is
# `_OPTION_DESTINATION_VALUE_TAKING`, added for issue #1448 — so the row redded and
# moved into `OPTION_DESTINATIONS` with its siblings rather than being relabelled here.
# Its two neighbour rows are the reason the letters must be the verb's own, and both
# are pinned as *allowed*: `sort -ko` and `curl -do` in that table's false-block list.
UNCOVERED_WRITERS = (
    # (row, command, allowed under read-only, allowed under workspace-write)
    ("tar -cf", "tar -cf OUT/a.tgz x", True, True),
    ("tar -xf -C spaced", "tar -xf a.tgz -C OUT", True, True),
    ("tar -xf -C attached", "tar -xf a.tgz -COUT", True, True),
    ("git clone", "git clone https://example.invalid/r.git OUT/clone", False, True),
)


@pytest.mark.parametrize(
    "row,cmd,read_only_allowed,workspace_write_allowed",
    UNCOVERED_WRITERS, ids=[row for row, *_rest in UNCOVERED_WRITERS],
)
def test_the_uncovered_writers_are_pinned_as_a_measured_hole(
    row, cmd, read_only_allowed, workspace_write_allowed,
):
    """A hole pinned as a hole, with the verdict it really gets.

    Two things are asserted, and they are different claims: an **empty target list**
    (which is why the tier below it allows — the loop that judges targets never runs),
    and the tier verdicts themselves. Pinning only the second would let a `git clone`
    row read as "the walk places the destination", when the block comes from the
    git-mutator rule and the destination is still unnamed.

    An empty list is also what makes these a *hole* rather than a design: when one of
    these families is read, the row reds and must be moved into `OPTION_DESTINATIONS`
    deliberately.
    """
    resolved = cmd.replace("OUT", OUTSIDE)
    assert _extract_write_targets(resolved) == [], (
        f"{row}: the walk now names a target for {resolved!r} - this row is no longer "
        "a residual, move it into OPTION_DESTINATIONS"
    )
    for tier, expected in (("read-only", read_only_allowed),
                           ("workspace-write", workspace_write_allowed)):
        allowed, reason, _ = _check_sandbox(resolved, tier, workdir="/workspace")
        assert allowed is expected, f"{row}: {tier} gave {allowed}, not {expected}"
        if expected:
            # `git clone` under workspace-write is the one row that is allowed while
            # its destination is unnamed, which is the residual itself.
            assert reason is None


def test_the_only_read_only_block_in_that_family_is_the_git_mutator_rule():
    """Why `git clone` is not evidence that the family is handled.

    The row above says BLOCK under read-only; this says *where the block comes from*,
    so a reader cannot mistake it for the walk naming the clone directory. It is the
    mutator rule that catches it — the same rule that catches a bare `git clone` with
    no directory at all.
    """
    allowed, reason, _ = _check_sandbox(
        "git clone https://example.invalid/r.git " + OUTSIDE + "/clone",
        "read-only", workdir="/workspace",
    )
    assert allowed is False
    assert "git mutating" in reason, reason
    # ...and the same rule blocks a clone that names no directory whatsoever, so the
    # block is not a statement about the destination.
    assert _check_sandbox("git clone https://example.invalid/r.git", "read-only")[0] is False
    # Under workspace-write the destination is unnamed and the clone is allowed.
    assert _check_sandbox(
        "git clone https://example.invalid/r.git " + OUTSIDE + "/clone",
        "workspace-write", workdir="/workspace",
    )[0] is True


# ── mutation arms: a row that cannot be flipped is not a claim ──────────────

# (verb, a command whose refusal depends on that verb being in the table)
MUTATION_ARMS = (
    ("curl", f"curl -o {OUTSIDE}/f https://example.invalid/x"),
    ("wget", f"wget -O {OUTSIDE}/f https://example.invalid/x"),
    ("sort", f"sort -o {OUTSIDE}/f x"),
    ("unzip", f"unzip a.zip -d {OUTSIDE}"),
)


def test_each_verb_row_is_killed_by_dropping_its_verb_from_the_table():
    """Drop the verb, and its row must go back to the ALLOW master gave.

    The arm patches the table the branch tests, then reverts it. The row must be
    refused before the arm, or the arm proves nothing about the row.
    """
    original = dict(bash_tool._OPTION_DESTINATION_VERBS)
    for verb, cmd in MUTATION_ARMS:
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, verb
        try:
            bash_tool._OPTION_DESTINATION_VERBS.pop(verb)
            allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
        finally:
            bash_tool._OPTION_DESTINATION_VERBS.clear()
            bash_tool._OPTION_DESTINATION_VERBS.update(original)
        assert allowed is True, (
            f"the {verb} row survives dropping {verb} from _OPTION_DESTINATION_VERBS "
            "- it does not depend on the branch it claims to test"
        )


# The spellings that do NOT sit in the token after the option, split by which
# piece of the reader they need — a spelling is only evidence if something can
# remove the thing it depends on and flip it.
ATTACHED_SHORT_SPELLINGS = (
    f"curl -o{OUTSIDE}/f https://example.invalid/x",
    f"wget -O{OUTSIDE}/f https://example.invalid/x",
    f"sort -o{OUTSIDE}/f x",
    f"unzip a.zip -d{OUTSIDE}",
)

LONG_EQUALS_SPELLINGS = (
    f"curl --output={OUTSIDE}/f https://example.invalid/x",
    f"wget --output-document={OUTSIDE}/f https://example.invalid/x",
    f"sort --output={OUTSIDE}/f x",
    f"unzip a.zip --directory={OUTSIDE}",
)

SPACED_CURL = f"curl -o {OUTSIDE}/f https://example.invalid/x"

# The cluster spellings, and the one thing they have that the rows above do not: the
# verb's own value-taking letters (issue #1448).
CLUSTER_SPELLINGS = (
    f"curl -so {OUTSIDE}/f https://example.invalid/x",
    f"sort -bo {OUTSIDE}/f x",
    f"unzip -qd {OUTSIDE} a.zip",
)

# The same three verbs' *attached* form: `_short_cluster_option` reads it when the verb
# has letters, `_leading_short_option_value` when it does not, so a clustered verb's
# attached row names its path either way — which is what the arms below turn on.
CLUSTERED_ATTACHED = (
    f"curl -o{OUTSIDE}/f https://example.invalid/x",
    f"sort -o{OUTSIDE}/f x",
    f"unzip a.zip -d{OUTSIDE}",
)


def test_the_attached_short_spellings_need_the_reader_that_can_split_a_token():
    """Which reader an attached token needs depends on whether the verb has letters.

    `-o<f>` is read by `_short_cluster_option` for the three verbs in
    `_OPTION_DESTINATION_VALUE_TAKING` (issue #1448) and by
    `_leading_short_option_value` for one that is not in it — `wget`, whose letters
    this change did not measure, because the host has no `wget` to measure them on.
    Arming each reader off in turn is what shows the two are separate code: the
    cluster arm must flip the clustered verbs' attached rows and leave `wget`'s alone,
    and the leading arm must do the reverse. The spaced form has to *survive* both,
    which is what makes the arms discriminate.
    """
    for cmd in ATTACHED_SHORT_SPELLINGS:
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, cmd
    clustered = list(CLUSTERED_ATTACHED)
    leading = [cmd for cmd in ATTACHED_SHORT_SPELLINGS if cmd.startswith("wget")]

    saved_cluster = bash_tool._short_cluster_option
    bash_tool._short_cluster_option = lambda *_a, **_k: None
    try:
        for cmd in clustered:
            allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
            assert allowed is True, (
                f"{cmd!r} is still refused with the cluster reader disabled - it does "
                "not depend on the spelling it claims to test"
            )
        # Not a blanket off-switch, and not the other reader's rows either.
        assert _check_sandbox(SPACED_CURL, "read-only", workdir="/workspace")[0] is False
        for cmd in leading:
            assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, cmd
    finally:
        bash_tool._short_cluster_option = saved_cluster

    saved_leading = bash_tool._leading_short_option_value
    bash_tool._leading_short_option_value = lambda *_a, **_k: None
    try:
        for cmd in leading:
            allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
            assert allowed is True, (
                f"{cmd!r} is still refused with the attached-token reader disabled - it "
                "does not depend on the spelling it claims to test"
            )
        # The clustered verbs' attached rows no longer ride on this reader…
        for cmd in clustered:
            assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, cmd
        # …and the spaced form still names its path, as it never used either reader.
        assert _check_sandbox(SPACED_CURL, "read-only", workdir="/workspace")[0] is False
    finally:
        bash_tool._leading_short_option_value = saved_leading


def test_the_cluster_rows_need_the_verbs_value_taking_letters():
    """Empty the letters table and the cluster rows must go back to master's ALLOW.

    The arm is aimed at the one thing a cluster row has that a spaced row does not, so
    the spaced **and** the attached forms have to survive it: with the table empty the
    attached form is read by `_leading_short_option_value` again, which is what makes
    this arm evidence that the letters — not the token's shape — are what reads a
    cluster. A target-list assertion would not do: what changes when the table is
    emptied is the *verdict*, because an empty target list is allowed by construction.
    """
    for cmd in CLUSTER_SPELLINGS:
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, cmd
    original = dict(bash_tool._OPTION_DESTINATION_VALUE_TAKING)
    bash_tool._OPTION_DESTINATION_VALUE_TAKING.clear()
    try:
        for cmd in CLUSTER_SPELLINGS:
            allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
            assert allowed is True, (
                f"{cmd!r} is still refused with the letters table empty - it does not "
                "depend on the table it claims to test"
            )
        assert _check_sandbox(SPACED_CURL, "read-only", workdir="/workspace")[0] is False
        for cmd in CLUSTERED_ATTACHED:
            assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, cmd
    finally:
        bash_tool._OPTION_DESTINATION_VALUE_TAKING.clear()
        bash_tool._OPTION_DESTINATION_VALUE_TAKING.update(original)


def test_the_long_equals_spellings_need_the_long_option_in_the_table():
    """A third dependency, armed separately: the *long* name in the table.

    Pruning every `--long` from the table must flip the `--opt=value` rows while
    leaving the short ones refused — the short letter alone still reads both the
    spaced and the attached form, so this arm proves the long rows are evidence
    about the long name rather than about the verb.
    """
    for cmd in LONG_EQUALS_SPELLINGS:
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, cmd
    original = dict(bash_tool._OPTION_DESTINATION_VERBS)
    short_only = {
        verb: frozenset(opt for opt in options if not opt.startswith("--"))
        for verb, options in original.items()
    }
    bash_tool._OPTION_DESTINATION_VERBS.clear()
    bash_tool._OPTION_DESTINATION_VERBS.update(short_only)
    try:
        for cmd in LONG_EQUALS_SPELLINGS:
            allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
            assert allowed is True, (
                f"{cmd!r} is still refused with the long option pruned from the table "
                "- it does not depend on the long name it claims to test"
            )
        assert bash_tool._extract_write_targets(SPACED_CURL) == [f"{OUTSIDE}/f"], (
            "the short-only table must still read the short spelling"
        )
        assert bash_tool._extract_write_targets(f"sort -o{OUTSIDE}/f x") == [f"{OUTSIDE}/f"]
    finally:
        bash_tool._OPTION_DESTINATION_VERBS.clear()
        bash_tool._OPTION_DESTINATION_VERBS.update(original)


# ── ground truth: the refused form writes nothing, its control really writes ──
#
# One row per verb, driven end to end through `BashTool.execute` in a tree THIS
# TEST creates (a negative test's safety must not rest on the guard it tests). The
# refusal is the half every platform can measure — the guard answers before the
# shell is reached — while the control half needs the tool to exist.
#
# `{t}` is the outside directory and `{w}` the workspace; each refused form must
# leave nothing behind, and each control must really write, or "the walk names the
# path" would be a claim about a helper that nothing measures against a real write.
GROUND_TRUTH = (
    # (row, tool it needs, refused form, refused witness, allowed form, its witness)
    ("curl -o", "curl",
     "curl -s -o {t}/refused.txt file://{w}/sub/source.txt", "{t}/refused.txt",
     "curl -s -o {w}/allowed.txt file://{w}/sub/source.txt", "{w}/allowed.txt"),
    ("wget -O", "wget",
     "wget -q -O {t}/refused.txt file://{w}/sub/source.txt", "{t}/refused.txt",
     "wget -q -O {w}/allowed.txt file://{w}/sub/source.txt", "{w}/allowed.txt"),
    ("sort -o", "sort",
     "sort -o {t}/refused.txt {w}/sub/source.txt", "{t}/refused.txt",
     "sort -o {w}/allowed.txt {w}/sub/source.txt", "{w}/allowed.txt"),
    ("unzip -d", "unzip",
     "unzip -q {w}/sub/a.zip -d {t}", "{t}/source.txt",
     "unzip -q {w}/sub/a.zip -d {w}/extracted", "{w}/extracted/source.txt"),
)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX shell ground truth: the daemon's shell on Windows is cmd.exe, "
           "where curl/wget/sort/unzip are not the tools these flags belong to",
)
@pytest.mark.parametrize(
    "row,tool,refused,refused_witness,allowed,allowed_witness",
    GROUND_TRUTH, ids=[row for row, *_rest in GROUND_TRUTH],
)
def test_the_refused_option_destination_writes_nothing_and_its_control_writes(
    monkeypatch, tmp_path, row, tool, refused, refused_witness, allowed, allowed_witness,
):
    """Issue #1398's acceptance, in the shape this family needs.

    `tmp_path` sits inside the OS temp root, which `workspace-write` legitimately
    allows — so `gettempdir` is patched to a name no directory here has, and the
    outside tree is a sibling of the workspace. Without that patch the refusal would
    be about the *temp root* and would pass for the wrong reason.
    """
    if shutil.which(tool) is None:
        pytest.skip(f"`{tool}` is not on PATH here, so this ground truth is unmeasurable")

    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/fake-os-temp")
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "sub" / "source.txt").write_text("hello\n", encoding="utf-8")
    # The `unzip` row's premise is built by the test, with the stdlib — the source
    # is a zip the row can extract, and its extraction happens INSIDE the workspace
    # so a tool that has no network still runs.
    with zipfile.ZipFile(workspace / "sub" / "a.zip", "w") as archive:
        archive.writestr("source.txt", "hello\n")
    os.chmod(outside, 0o755)

    def path(template: str) -> str:
        return template.format(t=outside.as_posix(), w=workspace.as_posix())

    tool_instance = BashTool()
    result = _run(tool_instance.execute({
        "command": path(refused),
        "sandbox": "workspace-write",
        "workdir": str(workspace),
    }))
    assert result.error is True, f"{path(refused)!r} was not refused"
    assert "not executed" in result.content
    assert not os.path.lexists(path(refused_witness)), (
        f"{path(refused)!r} really wrote {path(refused_witness)!r} outside the workspace"
    )

    control = _run(tool_instance.execute({
        "command": path(allowed),
        "sandbox": "workspace-write",
        "workdir": str(workspace),
    }))
    assert control.error is not True, f"the control {path(allowed)!r} was refused: {control.content}"
    assert os.path.lexists(path(allowed_witness)), (
        f"the control {path(allowed)!r} really wrote nothing, so the refusal above was "
        "not the difference: re-measure the row"
    )


def test_the_control_witness_is_not_satisfied_by_the_refusal_alone():
    """The instrument control for the rows above: an empty directory is not a pass.

    If the refused and allowed halves were swapped, the own-witness assertion above
    would be checking the wrong file. This pins the shape directly on files the test
    creates, so a reader can see what `lexists` is being asked about.
    """
    with tempfile.TemporaryDirectory() as scratch:
        witness = os.path.join(scratch, "witness.txt")
        assert not os.path.lexists(witness)
        with open(witness, "w", encoding="utf-8") as handle:
            handle.write("x")
        assert os.path.lexists(witness)
        mode = stat.S_IMODE(os.stat(witness).st_mode)
        assert mode & 0o400, "the witness must be readable for the assertion to mean anything"


# ---------------------------------------------------------------------------
# A `--` that no option consumed ends option parsing (measured 2026-09-19).
#
# `_positional_args` has always obeyed the terminator; the two option readers did
# not, so an option *after* it was still read and named a path no program writes.
# That is issue #1398's class from the other side — a **false block**, of a command
# that does nothing at all. Every control row below is the same command with the
# option *before* the terminator, which really does write that path and must stay
# refused; the two halves are what make the rows a measurement rather than a wish.
# ---------------------------------------------------------------------------

TERMINATOR_OPTION_ROWS = (
    # (row, option AFTER the terminator: names nothing, its control, what the control names)
    ("sort -o", f"sort -- -o {OUTSIDE}/f x", f"sort -o {OUTSIDE}/f -- x", (f"{OUTSIDE}/f",)),
    ("sort --output", f"sort -- --output {OUTSIDE}/f x",
     f"sort --output {OUTSIDE}/f -- x", (f"{OUTSIDE}/f",)),
    ("curl -o", f"curl -- -o {OUTSIDE}/f https://example.invalid/x",
     f"curl -o {OUTSIDE}/f -- https://example.invalid/x", (f"{OUTSIDE}/f",)),
    ("curl --output", f"curl -- --output {OUTSIDE}/f https://example.invalid/x",
     f"curl --output {OUTSIDE}/f -- https://example.invalid/x", (f"{OUTSIDE}/f",)),
    ("wget -O", f"wget -- -O {OUTSIDE}/f https://example.invalid/x",
     f"wget -O {OUTSIDE}/f -- https://example.invalid/x", (f"{OUTSIDE}/f",)),
    ("unzip -d (leading)", f"unzip -- -d {OUTSIDE} a.zip",
     f"unzip -d {OUTSIDE} -- a.zip", (OUTSIDE,)),
    ("unzip -d (trailing)", f"unzip a.zip -- -d {OUTSIDE}",
     f"unzip a.zip -d {OUTSIDE} --", (OUTSIDE,)),
)

_TERMINATOR_ROW_IDS = [row for row, *_rest in TERMINATOR_OPTION_ROWS]


@pytest.mark.parametrize(
    "row,terminator,control,named", TERMINATOR_OPTION_ROWS, ids=_TERMINATOR_ROW_IDS,
)
def test_the_walk_names_nothing_after_a_terminator(row, terminator, control, named):
    """The pair, on the walk: the terminator row names nothing, its control names.

    Asserting only the first half would be satisfied by a reader that names nothing
    anywhere — which is the hole this family of tests exists to prevent. The control
    is the *same* command with one token moved, so the difference between the two
    assertions can only be the terminator's position.
    """
    assert _extract_write_targets(terminator) == [], (
        f"{terminator!r}: an option after `--` is an operand and names no destination"
    )
    assert tuple(_extract_write_targets(control)) == named, (
        f"{control!r}: with the option before the terminator the write is real and "
        f"must still be named"
    )


@pytest.mark.parametrize(
    "row,terminator,control,named", TERMINATOR_OPTION_ROWS, ids=_TERMINATOR_ROW_IDS,
)
def test_a_terminator_row_is_allowed_at_both_tiers_and_its_control_is_refused(
    row, terminator, control, named,
):
    """Both tiers, because the false block was in both: the write was named, so the
    refusal was about a path the command never touches.

    `read-only` allows the terminator row for the same reason `workspace-write`
    does — the row names no write at all, and read-only's contract is about writes.
    """
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(terminator, tier, workdir="/workspace")
        assert allowed is True, f"{tier} still refuses {terminator!r}: {reason}"
        allowed, reason, _ = _check_sandbox(control, tier, workdir="/workspace")
        assert allowed is False, f"{tier} allowed {control!r}"
        assert named[0] in reason, f"{tier} block for {control!r} does not name {named[0]!r}"


# `-t`/`--target-directory` is `_target_directory_values`' reader, not the option
# table's, so it gets its own rows: the same sentence has to hold in both readers or
# the walk has a terminator-shaped hole again the moment a cycle works in one of them.
TERMINATOR_TARGET_DIRECTORY_ROWS = (
    ("cp -t", f"cp -- -t {OUTSIDE} src.txt", f"cp -t {OUTSIDE} -- src.txt"),
    ("cp --target-directory", f"cp -- --target-directory {OUTSIDE} src.txt",
     f"cp --target-directory {OUTSIDE} -- src.txt"),
    ("mv -t", f"mv -- -t {OUTSIDE} src.txt", f"mv -t {OUTSIDE} -- src.txt"),
    ("ln -t", f"ln -- -t {OUTSIDE} src.txt", f"ln -t {OUTSIDE} -- src.txt"),
    ("install -t", f"install -- -t {OUTSIDE} src.txt", f"install -t {OUTSIDE} -- src.txt"),
)


@pytest.mark.parametrize(
    "row,terminator,control", TERMINATOR_TARGET_DIRECTORY_ROWS,
    ids=[row for row, *_rest in TERMINATOR_TARGET_DIRECTORY_ROWS],
)
def test_the_target_directory_reader_obeys_the_terminator_too(row, terminator, control):
    """The same sentence in the second reader, as a predicate and never executed.

    This host's `cp`/`mv`/`ln` implement no `-t` at all — `cp -t OUT/f -- src.txt`
    exits 64 with the usage line, measured — so there is no executed row to write
    here. What the row pins is what the second reader owes: an option *after* `--`
    is an operand, so it is not the option that moves the destination and its value
    is never named. `OUTSIDE` must be absent from the terminator reading in both
    tiers, while the control (`-t OUTSIDE -- …`, options still in force) names it
    and is refused.

    What the terminator row *does* name is not nothing, and that is the conversation
    with the operand reader rather than a second hole. The terminator makes `-t` an
    operand — the same sentence the operand reader learned for `rm -- -s`, which
    names `-s` — so the operands are `-t`, `OUTSIDE`, `src.txt`, and a copy is judged
    by the last of them, its destination. Real `cp` really writes there: measured on
    this host, `cp -- -t x dest` exits 0 and puts both operands inside `dest`, and
    `cp -- -t OUT src.txt` exits 1 with "src.txt: Not a directory" — the last operand
    is the path the run is aimed at whether or not it turns out to be a directory.
    So the row is read as what the command is, and it is allowed at workspace-write
    because that operand is inside the workspace.
    """
    assert OUTSIDE not in _extract_write_targets(terminator), terminator
    assert _extract_write_targets(terminator) == ["src.txt"], terminator
    assert _extract_write_targets(control) == [OUTSIDE], control
    allowed, reason, _ = _check_sandbox(terminator, "workspace-write", workdir="/workspace")
    assert allowed is True, reason


def test_an_options_own_value_may_be_the_terminator():
    """The guard is "a `--` that no option **consumed**", and the clause is load bearing.

    `sort -o -- x` writes a file literally named `--`: measured on this host, it
    exits 0 and creates `./--` in the cwd. So a reader that broke at every `--`
    would name nothing here and allow a write that really happens — a false allow,
    the worse direction. The consumed-value clause is what keeps that row named.
    """
    assert _extract_write_targets("sort -o -- x") == ["--"]
    assert _extract_write_targets("sort --output=-- x") == ["--"]
    # Both readers, since both had to learn the sentence.
    assert _extract_write_targets("cp -t -- src.txt") == ["--"]
    # ...and a terminator that no option consumed still ends parsing: `-o` after it
    # is an operand, so no option names a destination and the sort row names nothing
    # (a copy is the other case, below, and it names its last operand).
    assert _extract_write_targets("sort -- -o x y") == []
    # The destination-last family names the last operand, which is what real `cp`
    # writes into — measured, `cp -- -t x dest` exits 0 and puts both operands inside
    # `dest`. So the same `--` reads as "nothing" for an option-destination verb and
    # as "the last operand" for a copy, which is each family's own rule and not a
    # contradiction.
    assert _extract_write_targets("cp -- -t x y") == ["y"]


def test_a_consumed_terminator_does_not_end_parsing():
    """The clause's other half: parsing continues *after* a consumed `--`, so a
    later option is still read and its path is a real write.

    Measured on this host — `sort -o -- -o OUT_f in.txt` exits 0 and writes `OUT_f`,
    the last `-o` winning (`--` is that option's value, not a terminator). A reader
    that ended parsing at every `--` would name only `--` and allow the OUT_f write,
    which is why the row below is asserted at both tiers and not just on the walk:
    the walk half alone cannot tell the two clauses apart.

    The naming of `--` as well is the over-approximation the reader already
    documents for a repeated option — it names every value, not the winning one.
    """
    for cmd, later in ((f"sort -o -- -o {OUTSIDE}/f x", f"{OUTSIDE}/f"),
                       (f"sort --output=-- --output={OUTSIDE}/f x", f"{OUTSIDE}/f"),
                       (f"cp -t -- -t {OUTSIDE} src.txt", OUTSIDE)):
        assert later in _extract_write_targets(cmd), (
            f"{cmd!r}: a later option after a consumed `--` is still an option"
        )
        for tier in ("read-only", "workspace-write"):
            allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
            # The block names one target and this row has two (`--` and the later
            # option's path), so the tier half asserts the refusal itself — which is
            # exactly what an end-parsing-at-every-`--` reader gets wrong, since it
            # would name only `--` and let the outside write through.
            assert allowed is False, f"{tier} allowed {cmd!r}"
            assert "--" in reason or later in reason, (
                f"{tier} block for {cmd!r} names neither of its targets: {reason}"
            )


# The executed half: the terminator rows are no-ops, so allowing them is safe — and
# the control rows, whose option sits before the terminator, still write.
TERMINATOR_GROUND_TRUTH = (
    # (row, tool, allowed terminator form, the outside witness it must NOT create,
    #  refused control form, inside form that really writes, its witness)
    ("sort -o", "sort",
     "sort -- -o {t}/refused.txt {w}/sub/source.txt", "{t}/refused.txt",
     "sort -o {t}/refused.txt -- {w}/sub/source.txt",
     "sort -o {w}/allowed.txt -- {w}/sub/source.txt", "{w}/allowed.txt"),
    ("unzip -d", "unzip",
     "unzip -q -- -d {t} {w}/sub/a.zip", "{t}/source.txt",
     "unzip -q -d {t} -- {w}/sub/a.zip",
     "unzip -q -d {w}/extracted -- {w}/sub/a.zip", "{w}/extracted/source.txt"),
    ("curl -o", "curl",
     "curl -s -- -o {t}/refused.txt file://{w}/sub/source.txt", "{t}/refused.txt",
     "curl -s -o {t}/refused.txt -- file://{w}/sub/source.txt",
     "curl -s -o {w}/allowed.txt -- file://{w}/sub/source.txt", "{w}/allowed.txt"),
)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX shell ground truth: the daemon's shell on Windows is cmd.exe, "
           "where curl/sort/unzip are not the tools these flags belong to",
)
@pytest.mark.parametrize(
    "row,tool,allowed,allowed_witness,refused,inside,inside_witness",
    TERMINATOR_GROUND_TRUTH, ids=[row for row, *_rest in TERMINATOR_GROUND_TRUTH],
)
def test_the_allowed_terminator_form_really_writes_nothing(
    monkeypatch, tmp_path, row, tool, allowed, allowed_witness, refused, inside, inside_witness,
):
    """Allowing a row is only correct if the row is a no-op — so run it and look.

    The `tmp_path` patch is the one the sibling ground-truth test needs: without it
    the outside tree would sit inside the OS temp root, which `workspace-write`
    allows, and the control's refusal would pass for the wrong reason.

    Three assertions, and the last two are the instrument's control: the allowed
    terminator row leaves the outside path absent, the control is still refused, and
    a *third* form that keeps the option inside the workspace does write — proving
    the tool really ran, so "absent" is a fact about the command and not about a
    BashTool that never executed anything.
    """
    if shutil.which(tool) is None:
        pytest.skip(f"`{tool}` is not on PATH here, so this ground truth is unmeasurable")

    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/fake-os-temp")
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "sub" / "source.txt").write_text("hello\n", encoding="utf-8")
    with zipfile.ZipFile(workspace / "sub" / "a.zip", "w") as archive:
        archive.writestr("source.txt", "hello\n")
    os.chmod(outside, 0o755)

    def path(template: str) -> str:
        return template.format(t=outside.as_posix(), w=workspace.as_posix())

    tool_instance = BashTool()

    def execute(command: str):
        return _run(tool_instance.execute({
            "command": command, "sandbox": "workspace-write", "workdir": str(workspace),
        }))

    result = execute(path(allowed))
    assert "not executed" not in result.content, (
        f"{path(allowed)!r} was refused at the sandbox — the terminator row is a no-op "
        f"and naming its path is the false block this fixes: {result.content}"
    )
    assert not os.path.lexists(path(allowed_witness)), (
        f"{path(allowed)!r} really wrote {path(allowed_witness)!r}, so the row is not a "
        "no-op and allowing it is a hole — re-measure it"
    )

    control = execute(path(refused))
    assert control.error is True, f"the control {path(refused)!r} was not refused"
    assert "not executed" in control.content
    assert not os.path.lexists(path(allowed_witness)), path(refused)

    witness = execute(path(inside))
    assert witness.error is not True, f"{path(inside)!r} was refused: {witness.content}"
    assert os.path.lexists(path(inside_witness)), (
        f"{path(inside)!r} wrote nothing, so the absence asserted above says nothing "
        "about the tool — re-measure the row"
    )
