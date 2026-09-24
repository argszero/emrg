"""`tar` writes its archive, or the directory it extracts into, and the walk names it.

`tar` was the first row of `test_bash_tool_option_destinations.py`'s
`UNCOVERED_WRITERS` table: a run whose target lay outside every allowed root was
**ALLOW at both tiers with an empty target list**, which is allowed by construction —
the loop that judges targets never runs. `tar -cf <outside>/a.tar f` really creates
that archive, so the run was a silent allow of a write outside every allowed root, the
same fail-open `gzip` had (#1418) and `zip` had (#1529).

Why it took a rule rather than a table row: `-f`'s value is a **write** under
`-c`/`-r`/`-u` and a **read** under `-x`/`-t`, and `-C` is where members land when
extracting but only where members are collected from when creating. The missing fact is
the **operation letter**, and the line always spells it — so the two meanings are
decidable and this is the departure `rsync`, `split`, `csplit` and `zip` each took.

Ground truth, taken 2026-09-22 in a scratch directory on this host (bsdtar 3.5.3,
libarchive 3.7.4) with the tree read back off disk after every row — the table lives
above `_TAR_LONG_OPERATIONS` in `emrg/tools/bash_tool.py`, and the rows that decide the
rule are these:

* `tar -cf out/a.tar src/f.txt` created the archive, and so did the dashless
  `tar cf out/b.tar src/f.txt`, the clustered `tar -vcf out/i.tar src/f.txt`, the
  split `tar c -f out/n.tar src/f.txt` and both long spellings;
* `tar -f out/l.tar src/f.txt` created **nothing** (`Must specify one of -c, -r, -t,
  -u, -x`) — no operation letter, no write. That row is why the rule names nothing on a
  line whose operation it cannot read, and it is the difference between a rule and a
  false block;
* `tar -fc out/j.tar src/f.txt` created nothing either, for the getopt reason: `f` took
  `c` as its value, so the line has no operation at all;
* `tar -tf ref.tar`, `tar --list -f ref.tar` and `tar -tf ref.tar -C out` created
  nothing: list mode is a read whatever `-C` says;
* `tar -xf ref.tar -C out`, `-Cout`, `--directory out`, `--directory=out`, `--cd out`
  and `tar -C out -xf ref.tar` all really put the members under `out`;
* `tar -xOf ref.tar -C out` and `tar -xO --directory out -f ref.tar` created **nothing**
  — `-O` sends the members to stdout — while `tar -xf ref.tar -C out/nope` failed with
  `could not chdir to 'out/nope'`.

The ground-truth test at the end of this file re-runs the four rows the rule rests on
and skips where the binary is not this one (a GNU tar reads `--get` as extract and takes
`-r` to create a missing archive, so its verdicts are not this host's verdicts).

This file is separate from `test_bash_tool_sandbox.py` for the reason its siblings are:
it covers one family, and a self-contained fixture set cannot disagree with a table it
does not share.
"""

import os
import shutil
import subprocess
import sys

import pytest

from emrg.tools.bash_tool import (
    _TAR_PROGRAM_WORDS,
    _check_sandbox,
    _extract_write_targets,
    _positional_args,
    _tar_operation_and_values,
)

# Outside every allowed root (workspace, OS temp root, the evolution data dir) and used
# only as an argument to the pure predicate — never executed and never opened.
OUTSIDE = "/outside/emrg"
WORKSPACE = "/workspace"


def test_the_predicate_is_the_one_the_tiers_read():
    """The two functions this file judges are the ones the tool layer calls.

    A test that reached a private re-reading would keep passing while the shipped guard
    changed, which is the failure this whole class of test exists to prevent.
    """
    allowed, _reason, enforcement = _check_sandbox(
        "tar -cf a.tar f", "workspace-write", WORKSPACE
    )
    assert allowed is True
    assert enforcement in ("partial", "full")


# ── the creating operations: the archive is the write ────────────────────────────────
#
# (row, command) — the target is `{out}` in every row, so the assertion is about *which
# token* the rule places rather than about whether it found something.
CREATING_FORMS = (
    ("clustered -cf", "tar -cf {out} f"),
    ("dashless old style", "tar cf {out} f"),
    ("operation letter not first", "tar -vcf {out} f"),
    ("dashless word, -f later", "tar c -f {out} f"),
    ("append -rf", "tar -rf {out} f"),
    ("update -uf", "tar -uf {out} f"),
    ("long --create --file=", "tar --create --file={out} f"),
    ("long --create --file spaced", "tar --create --file {out} f"),
    ("long --append --file", "tar --append --file {out} f"),
    ("long --update -f", "tar --update -f {out} f"),
    ("archive and no member", "tar -cf {out}"),
)


@pytest.mark.parametrize("row,cmd", CREATING_FORMS, ids=[row for row, _ in CREATING_FORMS])
def test_every_creating_form_names_the_archive(row, cmd):
    """The `-f` value is the file created, appended to or rewritten, and only it.

    `-C` is deliberately absent from every row here: under a creating operation it is
    where the members are *collected from*, so naming it would refuse
    `tar -cf out.tgz -C /etc .`, which writes nothing outside the workspace.
    """
    out = f"{OUTSIDE}/a.tar"
    assert _extract_write_targets(cmd.format(out=out)) == [out], row


@pytest.mark.parametrize("row,cmd", CREATING_FORMS, ids=[row for row, _ in CREATING_FORMS])
def test_an_archive_outside_every_root_is_refused_at_both_tiers(row, cmd):
    """The hole was in both tiers, so both are asserted — and the refusal names the path.

    A block that did not say which path it refused would leave a reader unable to tell
    an archive refusal from a member-name one, which is the failure #1398 already
    produced once.
    """
    out = f"{OUTSIDE}/a.tar"
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd.format(out=out), tier, WORKSPACE)
        assert allowed is False, f"{row}: {tier} allowed a write to {out}"
        assert out in (reason or ""), f"{row}: {tier} refused without naming {out}"


def test_the_creating_directory_is_a_read_and_is_not_named():
    """The objection the shared option table raises, answered by the rule.

    `_OPTION_DESTINATION_VERBS` refuses a row for tar because a rule that named `-C`
    unconditionally would refuse `tar -cf out.tgz -C /etc .` — and it would be a false
    block, because under `-c` the directory is only read from (measured: the archive was
    created and `src` untouched). This is the row that keeps that objection answered.
    """
    assert _extract_write_targets(
        f"tar -cf {WORKSPACE}/a.tar -C {OUTSIDE}/src f"
    ) == [f"{WORKSPACE}/a.tar"]


# ── the extracting operation: the directory is the write ─────────────────────────────
EXTRACTING_FORMS = (
    ("spaced value", "tar -xf a.tar -C {out}"),
    ("attached value", "tar -xf a.tar -C{out}"),
    ("directory leads", "tar -C {out} -xf a.tar"),
    ("dashless old style", "tar xf a.tar -C {out}"),
    ("long --directory", "tar -xf a.tar --directory {out}"),
    ("long --directory=", "tar -xf a.tar --directory={out}"),
    ("long --cd", "tar -xf a.tar --cd {out}"),
    ("long --extract", "tar --extract --directory {out} -f a.tar"),
    ("long --get", "tar --get --directory {out} -f a.tar"),
)


@pytest.mark.parametrize(
    "row,cmd", EXTRACTING_FORMS, ids=[row for row, _ in EXTRACTING_FORMS]
)
def test_every_extracting_form_names_the_directory(row, cmd):
    """The members land in `-C`, so `-C` is the write — and the archive is not named."""
    out = f"{OUTSIDE}/dest"
    assert _extract_write_targets(cmd.format(out=out)) == [out], row


@pytest.mark.parametrize(
    "row,cmd", EXTRACTING_FORMS, ids=[row for row, _ in EXTRACTING_FORMS]
)
def test_an_extract_directory_outside_every_root_is_refused_at_both_tiers(row, cmd):
    out = f"{OUTSIDE}/dest"
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd.format(out=out), tier, WORKSPACE)
        assert allowed is False, f"{row}: {tier} allowed members to land in {out}"
        assert out in (reason or ""), f"{row}: {tier} refused without naming {out}"


# ── the forms that write nothing: named by nothing, allowed everywhere ──────────────
#
# Each is a measurement, not a guess — the rc and the reason are in the table above
# `_TAR_LONG_OPERATIONS`. They are pinned separately because "names nothing" is what the
# rule must do here: an over-eager reader that took `-f` for a write whatever the
# operation would refuse every one of them.
NOTHING_WRITTEN = (
    ("list -tf", "tar -tf {out}"),
    ("list, long", "tar --list -f {out}"),
    ("list with -C", "tar -tf a.tar -C {out}"),
    ("no operation letter", "tar -f {out} f"),
    ("f consumed the operation letter", "tar -fc {out} f"),
    ("extract, archive is a read", "tar -xf {out}"),
    ("extract to stdout", "tar -xOf {out} -C /outside/emrg/dest"),
    ("extract to stdout, long", "tar -xO --directory /outside/emrg/dest -f {out}"),
    ("no arguments", "tar"),
)


@pytest.mark.parametrize(
    "row,cmd", NOTHING_WRITTEN, ids=[row for row, _ in NOTHING_WRITTEN]
)
def test_forms_that_write_nothing_name_nothing(row, cmd):
    """An empty target list here is the *correct* answer, not a hole.

    The distinction matters because the same empty list was the defect for the creating
    rows: there it meant a real write went unjudged, here it means the run does not
    write. What separates them is the measurement in the module docstring, not the shape
    of the answer.
    """
    outside = f"{OUTSIDE}/a.tar"
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd.format(out=outside), tier, WORKSPACE)
        assert allowed is True, f"{row}: {tier} refused a run that writes nothing ({reason})"


def test_a_redirect_after_the_run_is_still_a_target():
    """The rule adds a target; it does not replace the walk's other readings.

    A `tar` line can carry an ordinary redirect, and the branch must not swallow it —
    the same non-regression `_zip_write_targets` is held to. Both targets survive, the
    relative archive `a.tar` and the redirect's outside path, so this fails if the
    branch either drops the redirect or stops naming the archive.
    """
    assert _extract_write_targets(
        f"tar -cf a.tar f > {OUTSIDE}/log"
    ) == ["a.tar", f"{OUTSIDE}/log"]
    assert _extract_write_targets(
        f"tar -cf {OUTSIDE}/a.tar f > log"
    ) == [f"{OUTSIDE}/a.tar", "log"]


# ── the rule is load-bearing, and here is what would happen without it ──────────────
def test_the_operand_reading_is_not_what_names_the_extract_directory():
    """Where the answer comes from, so a reader cannot mistake one rule for the other.

    Under `-x` an operand-shaped rule takes the **archive** — the first operand — as the
    write, and never sees `-C`, which is an option's value. This asserts both halves: the
    operand answer *leads* with the archive, and the shipped answer names the directory
    instead. A mutant that dropped the tar branch would name the archive the run only
    reads and miss the destination the members really land in, and this test says so in
    those words.
    """
    tokens = f"tar -xf {OUTSIDE}/a.tar -C {OUTSIDE}/dest".split()
    assert _positional_args(tokens, 0)[0] == f"{OUTSIDE}/a.tar"
    assert _extract_write_targets(" ".join(tokens)) == [f"{OUTSIDE}/dest"]


def test_the_operation_letter_is_what_the_rule_reads():
    """The one fact the whole rule rests on, read directly rather than through a verdict.

    `-fc` is the arm: tar takes `c` as `-f`'s value, so the line has **no** operation
    and writes nothing. A scanner that hunted the operation letter without honouring the
    value-taking letters would read that `c` as create and name `/outside/emrg/a.tar`,
    so this pair is what tells the two readers apart. What the helper reports for `-fc`
    is the truth about the *line* — one value, `c`, and no operation — and the caller
    names nothing for an operation it could not read, which is the row pinned in
    `test_forms_that_write_nothing_name_nothing`.
    """
    operation, archives, _directories = _tar_operation_and_values(
        f"-cf {OUTSIDE}/a.tar f".split()
    )
    assert (operation, archives) == ("c", [f"{OUTSIDE}/a.tar"])

    operation, archives, _directories = _tar_operation_and_values(
        f"-fc {OUTSIDE}/a.tar f".split()
    )
    assert (operation, archives) == (None, ["c"])
    assert _extract_write_targets(f"tar -fc {OUTSIDE}/a.tar f") == []


def test_a_member_named_like_an_option_word_is_not_read_as_one():
    """The dashless spelling is read in the first word only, which is where tar reads it.

    Measured: `tar -cf out/u.tar src/f.txt cf` created its archive and then reported `cf`
    as an unstattable **member**. A reader that scanned every word for a cluster would
    read that member as `c` + `f` and name `src/f.txt` as a second archive.
    """
    assert _extract_write_targets(
        f"tar -cf {WORKSPACE}/u.tar src/f.txt cf"
    ) == [f"{WORKSPACE}/u.tar"]


def test_a_tar_word_in_data_position_is_not_an_invocation():
    """A verb *spelling* is not a call — the guard `_runs_as_a_command` is what says so.

    Without it, quoting a tar command line as an argument would be refused for a write
    nobody makes. The two rows are the same tokens in both positions, so the test fails
    if the guard is dropped rather than if the rule is wrong.
    """
    assert _extract_write_targets(f'echo "tar -cf {OUTSIDE}/a.tar f"') == []
    assert _extract_write_targets(f"tar -cf {OUTSIDE}/a.tar f") == [f"{OUTSIDE}/a.tar"]


# ── the ordinary use is not refused ────────────────────────────────────────────────
def test_an_archive_inside_the_workspace_is_allowed_where_writes_are_on():
    """The rule places the write; it does not refuse how the tool is normally used.

    Both spellings the everyday user reaches for — a create into the workspace and an
    extract into it — plus the read-only tier's own answer, which refuses a named target
    on its own account.
    """
    create = f"tar -cf {WORKSPACE}/a.tar f"
    extract = f"tar -xf a.tar -C {WORKSPACE}"
    assert _extract_write_targets(create) == [f"{WORKSPACE}/a.tar"]
    assert _extract_write_targets(extract) == [WORKSPACE]
    for cmd in (create, extract):
        allowed, _reason, _ = _check_sandbox(cmd, "workspace-write", WORKSPACE)
        assert allowed is True, cmd
        allowed, _reason, _ = _check_sandbox(cmd, "read-only", WORKSPACE)
        assert allowed is False, cmd


def test_an_extract_into_a_directory_it_cannot_enter_is_still_named():
    """Named limit, stated rather than implied: tar's own failure is not this walk's.

    `tar -xf ref.tar -C <missing>` exits 1 with `could not chdir to …` and creates
    nothing, while this rule names the directory and refuses it. It is an over-block on
    a run that does nothing — the direction this walk treats as the cheap one — and it is
    pinned so the choice is visible rather than accidental.
    """
    out = f"{OUTSIDE}/nope"
    assert _extract_write_targets(f"tar -xf a.tar -C {out}") == [out]
    allowed, _reason, _ = _check_sandbox(f"tar -xf a.tar -C {out}", "workspace-write",
                                         WORKSPACE)
    assert allowed is False


# ── ground truth, re-run rather than quoted ────────────────────────────────────────
def _bsdtar() -> str | None:
    """This host's tar, and only when it is the binary the measured table came from.

    A GNU tar is a different subject: it reads `--get` as a synonym for `--extract`
    (where this one rejects it) and takes `-r` to create a missing archive, so its
    verdicts would not be evidence about the table this file pins.
    """
    path = shutil.which("tar")
    if path is None:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return path if "bsdtar" in out.stdout else None


@pytest.mark.skipif(sys.platform == "win32", reason="the measured table is this host's tar")
def test_tar_really_writes_where_the_rule_says(tmp_path):
    """The rule's four load-bearing rows, executed rather than believed.

    Executed in a directory the *test* creates, with relative paths only, so nothing
    outside `tmp_path` can be touched: `-f -` writes to stdout and every other row names
    a file under `tmp_path`. The point is the pair `-cf` (creates) and `-f` without an
    operation (creates nothing) — the two rows the rule's central decision rests on.
    """
    tar = _bsdtar()
    if tar is None:
        pytest.skip("this host's tar is not the bsdtar the measured table came from")
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")

    def run(*argv: str) -> int:
        return subprocess.run([tar, *argv], cwd=tmp_path, capture_output=True,
                              timeout=30).returncode

    assert run("-cf", "a.tar", "f.txt") == 0
    assert (tmp_path / "a.tar").exists()

    assert run("cf", "b.tar", "f.txt") == 0
    assert (tmp_path / "b.tar").exists()

    # No operation letter: tar refuses the line and writes nothing — the row that makes
    # an unreadable operation a "name nothing" rather than an over-block.
    assert run("-f", "c.tar", "f.txt") != 0
    assert not (tmp_path / "c.tar").exists()

    # `f` takes `c` as its value, so the line has no operation either.
    assert run("-fc", "d.tar", "f.txt") != 0
    assert not (tmp_path / "d.tar").exists()

    # `-t` is a read whatever the archive is.
    assert run("-tf", "a.tar") == 0


# ── the program's other name is the same binary, so it is the same rule ─────────────
#
# `/usr/bin/tar` is a symlink to `/usr/bin/bsdtar` on this host (measured 2026-09-22:
# `tar -> bsdtar`), so the word `bsdtar` names the very program every row above was
# measured with. Reading only the word `tar` left the other name naming nothing at all:
# on the head this section was added to, `bsdtar -cf <outside>/a.tar f` and
# `bsdtar -xf a.tar -C <outside>` were ALLOW with an empty target list.
#
# The words are listed **here**, not read off `_TAR_PROGRAM_WORDS`: a parametrisation
# derived from the set under test shrinks with it, so dropping a name from the rule would
# have removed its own rows and left the file green (measured — the first version of this
# section did exactly that, 64 → 62 passed and no failure). The literal tuple plus the
# agreement test below make that mutation red instead.
COVERED_PROGRAM_WORDS = ("bsdtar", "tar")


def test_the_rule_reads_both_of_the_words_this_host_s_tar_answers_to():
    """The list above is the expectation; this is where it meets the module's set.

    A name dropped from `_TAR_PROGRAM_WORDS` — or one added without a row — fails here,
    which is what keeps the parametrised rows from being the only witness.
    """
    assert sorted(COVERED_PROGRAM_WORDS) == sorted(_TAR_PROGRAM_WORDS)


@pytest.mark.parametrize("prog", COVERED_PROGRAM_WORDS)
def test_each_program_word_reads_the_archive_and_the_extract_directory(prog):
    """One rule for both words: the create names the archive, the extract names `-C`.

    `_command_word` reduces a word to its bare spelling, so the path spelling is the
    same question and is asserted beside it — `/usr/bin/bsdtar` is how a script that
    wants this binary without a PATH lookup spells it.
    """
    archive = f"{OUTSIDE}/a.tar"
    dest = f"{OUTSIDE}/dest"
    assert _extract_write_targets(f"{prog} -cf {archive} f") == [archive]
    assert _extract_write_targets(f"/usr/bin/{prog} -cf {archive} f") == [archive]
    assert _extract_write_targets(f"{prog} -xf a.tar -C {dest}") == [dest]
    for cmd, target in ((f"{prog} -cf {archive} f", archive),
                        (f"{prog} -xf a.tar -C {dest}", dest)):
        for tier in ("read-only", "workspace-write"):
            allowed, reason, _ = _check_sandbox(cmd, tier, WORKSPACE)
            assert allowed is False, f"{prog}: {tier} allowed a write to {target}"
            assert target in (reason or ""), f"{prog}: {tier} refused without naming it"


@pytest.mark.parametrize("prog", COVERED_PROGRAM_WORDS)
def test_each_program_word_in_data_position_is_still_a_mention(prog):
    """The drift guard: a name the walk dispatches on must also be a *word* it distrusts.

    The two sites hold one fact — the dispatch reads `_TAR_PROGRAM_WORDS` and
    `_WRITE_VERB_WORDS` carries the same words so that a spelling in data position is not
    believed. A name added to the first without the second would refuse `echo bsdtar -cf
    out.tar f`, which is the #1513 over-block; this row fails in that direction, and the
    command-position row above fails if the dispatch does not know the name at all.
    """
    assert _extract_write_targets(f'echo "{prog} -cf {OUTSIDE}/a.tar f"') == []
    assert _extract_write_targets(f"printf %s {prog} -cf {OUTSIDE}/a.tar f") == []
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(
            f'echo "{prog} -cf {OUTSIDE}/a.tar f"', tier, WORKSPACE
        )
        assert allowed is True, f"{prog}: {tier} refused a mention ({reason})"


def test_the_two_program_words_really_name_one_binary_on_this_host():
    """Why one table covers both words, measured rather than argued.

    Skipped where the host has no `bsdtar` (a GNU-tar machine), because the claim being
    pinned is about *this* host: the table in `emrg/tools/bash_tool.py` was measured with
    bsdtar 3.5.3, and `tar` here is a link to it — so a name that resolved to a different
    program would be a different subject and would belong in issue #1538's list instead.
    """
    tar, bsdtar = shutil.which("tar"), shutil.which("bsdtar")
    if not tar or not bsdtar:
        pytest.skip("no bsdtar on this host: the two words are not one binary here")
    assert os.path.samefile(tar, bsdtar), f"{tar} is not {bsdtar} on this host"
