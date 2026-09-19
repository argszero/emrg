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


def test_the_cluster_spelling_is_a_measured_residual_not_a_guess():
    """`-so<dir>` is left unnamed, and this is the measurement that says so.

    A token that puts another letter before the destination letter has to be split
    by the verb's own option grammar. Both guesses are wrong in one direction — the
    remainder is the value when the earlier letter takes none (`curl -so<dir>`), and
    the *next* token is the value when `-o` is not a flag at all (`sort -ko out.txt`
    means `-k o` plus an operand to read) — and naming a read is a false block, the
    direction this guard's record treats as worse. So this row is pinned as a
    known hole with its ground truth, the way the #1391 residuals are: a future
    change that closes it must flip this assertion deliberately.

    Measured on GNU: `curl -so <dir>/f <url>` really does write into `<dir>`.
    """
    for cmd in (f"curl -so {OUTSIDE}/f https://example.invalid/x",
                f"curl -so{OUTSIDE}/f https://example.invalid/x"):
        assert _extract_write_targets(cmd) == [], cmd
        # The cost is bounded to this spelling: the same destination spread across
        # two tokens is named, which is the ordinary way it is written.
        assert _extract_write_targets(cmd.replace("-so", "-s -o")) == [f"{OUTSIDE}/f"]


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
UNCOVERED_WRITERS = (
    # (row, command, allowed under read-only, allowed under workspace-write)
    ("tar -cf", "tar -cf OUT/a.tgz x", True, True),
    ("tar -xf -C spaced", "tar -xf a.tgz -C OUT", True, True),
    ("tar -xf -C attached", "tar -xf a.tgz -COUT", True, True),
    ("split", "split -l 100 x OUT/pre", True, True),
    ("curl -so cluster", "curl -soOUT/f https://example.invalid/x", True, True),
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


def test_the_attached_short_spellings_need_the_reader_of_an_attached_token():
    """Arm the `_leading_short_option_value` reader off, and these rows must go.

    Dropping the verb cannot kill these rows — they survive on the table plus a
    second piece of code, so the arm is aimed at that piece. The spaced form must
    *survive* the same arm, which is what makes it discriminate: it kills the
    attached rows without killing the spaced ones.
    """
    for cmd in ATTACHED_SHORT_SPELLINGS:
        assert _check_sandbox(cmd, "read-only", workdir="/workspace")[0] is False, cmd
    saved = bash_tool._leading_short_option_value
    bash_tool._leading_short_option_value = lambda *_a, **_k: None
    try:
        for cmd in ATTACHED_SHORT_SPELLINGS:
            allowed = _check_sandbox(cmd, "read-only", workdir="/workspace")[0]
            assert allowed is True, (
                f"{cmd!r} is still refused with the attached-token reader disabled "
                "- it does not depend on the spelling it claims to test"
            )
        # Not a blanket off-switch: the spaced form still names its path.
        assert _check_sandbox(SPACED_CURL, "read-only", workdir="/workspace")[0] is False
    finally:
        bash_tool._leading_short_option_value = saved


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
