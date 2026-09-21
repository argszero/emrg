"""`curl`'s destination options: `--output-dir` relocates what `-o` and `-O` write, and
the program's other file-writing options are named (issue #1504).

A destination option's value is not always the path the run writes. `curl --output-dir
<dir> -o f <url>` creates `<dir>/f` and **nothing** at `f`, so a reader that names the value
describes a write that does not happen: the command is then allowed at workspace-write (a
relative `f` resolves inside the workdir) while read-only refuses it for a word the run
never touches. Both verdicts are about the wrong path, and the second is the one the
maintainer's reproduction led with.

Measured 2026-09-21 on this host (curl 8.9.0, win64), one scratch directory per row, a
`file:///` source, the tree read back off disk after each run:

    curl --output-dir D -o f URL               rc=0   D/f created, nothing at `f`
    curl -o f --output-dir D URL               rc=0   D/f created — the directory applies
                                                      to an `-o` *before* it
    curl --output-dir D1 --output-dir D2 -o f  rc=0   D2/f created — the last one wins
    curl --output-dir D -O URL                 rc=0   D/<basename of URL> created
    curl --output-dir D -o sub/in.txt URL      rc=0   D/sub/in.txt created
    curl --output-dir D -o ../esc.txt URL      rc=0   D/../esc.txt created — literal join
    curl --output-dir D URL                    rc=0   nothing created (body to stdout)
    curl --output-dir D -o - URL               rc=0   nothing created (body to stdout)
    curl --output-dir=D -o f URL               rc=2   ``--output=…: is unknown`` here, and
                                                      read anyway (see the table's note)
    curl --output-dir D -o /rooted/f URL       rc=23  nothing created — pinned below

The second half of the same issue lives here too, because it is the same reader and the
same table: `curl` writes a file with more than its destination option, and the walk
enumerated none of the others. `--dump-header`/`-D`, `--cookie-jar`/`-c`, `--etag-save`,
`--hsts`, `--alt-svc`, `--trace`, `--trace-ascii`, `--stderr` and `--libcurl` each name a
file the run creates (measured through `curl`, `-D f` leaving a 91-byte header file and
`-c f` a 131-byte jar), yet the target list came back `()` — and an empty list is allowed
by construction, so both tiers allowed the write. Those spellings are rows of the named
block below now, with the two shapes that must *stay* unnamed (a `-` value, which is
stdout, and a path an option only ever reads) pinned beside them so a later widening of
the table cannot pass by naming everything. `-D` and `--trace` are also the spellings whose
*long* form eats the word in issue #1461, so a fix there must not read one as the other.

Separate from `test_bash_tool_option_destinations.py` for the reason that file gives for
itself: one family per file, so two fixtures need not agree about a table neither owns.
"""

import asyncio
import os
import shutil
import sys
import tempfile

import pytest

from emrg.tools.bash_tool import (
    BashTool,
    _check_sandbox,
    _extract_write_targets,
)


def _run(coro):
    return asyncio.run(coro)


# Outside every allowed root (workspace, OS temp root, the evolution data dir); used as an
# argument to the pure predicates and never executed.
OUTSIDE = "/outside/emrg"

# Inside the workspace the tier below is judged against.
INSIDE = "/workspace/sub"

URL = "file:///workspace/x"

# (row, command, every path the walk must name for it)
RELOCATED_ROWS = (
    ("spaced, dir first", f"curl --output-dir {OUTSIDE} -o f {URL}",
     (f"{OUTSIDE}/f",)),
    ("spaced, dir after the -o", f"curl -o f --output-dir {OUTSIDE} {URL}",
     (f"{OUTSIDE}/f",)),
    ("long --output", f"curl --output-dir {OUTSIDE} --output f {URL}",
     (f"{OUTSIDE}/f",)),
    ("attached -of", f"curl --output-dir {OUTSIDE} -of {URL}",
     (f"{OUTSIDE}/f",)),
    ("--output-dir=D, the form this curl rejects", f"curl --output-dir={OUTSIDE} -o f {URL}",
     (f"{OUTSIDE}/f",)),
    ("two directories, the last wins", f"curl --output-dir /elsewhere --output-dir {OUTSIDE} -o f {URL}",
     (f"{OUTSIDE}/f",)),
    ("a nested relative value stays under the directory",
     f"curl --output-dir {OUTSIDE} -o sub/in.txt {URL}", (f"{OUTSIDE}/sub/in.txt",)),
    ("-O is relocated too, and the directory is what is named",
     f"curl --output-dir {OUTSIDE} -O {URL}", (OUTSIDE,)),
    ("-O long spelling", f"curl --output-dir {OUTSIDE} --remote-name {URL}", (OUTSIDE,)),
)


@pytest.mark.parametrize(
    "row,cmd,named", RELOCATED_ROWS, ids=[r for r, *_ in RELOCATED_ROWS],
)
def test_the_directory_relocates_the_value_the_walk_names(row, cmd, named):
    """The named path is the one the run writes, in every spelling of the pair.

    The `=` form is among the rows although this host's curl exits 2 on it: the reader
    reads it for the reason it reads `--output=<f>` for the destination itself — the two
    ways of being wrong are not equally costly, and a build that accepts the form really
    relocates.
    """
    assert tuple(_extract_write_targets(cmd)) == named, row


@pytest.mark.parametrize(
    "row,cmd,named", RELOCATED_ROWS, ids=[r for r, *_ in RELOCATED_ROWS],
)
def test_the_relocated_path_is_what_the_tiers_judge(row, cmd, named):
    """The pair is refused at both tiers, and the reason names the relocated path.

    Before this reading the same rows were refused at read-only with `f` in the message
    while workspace-write allowed them — an escape, since the write really lands in the
    outside directory. The control below is the other direction, so this cannot be
    satisfied by refusing every `--output-dir` row on principle.
    """
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is False, f"{tier} allowed {cmd!r}"
        assert named[0] in reason, f"{tier}: {reason!r} does not name {named[0]!r}"


CONTROL_ROWS = (
    ("a directory inside the workspace is a write inside it",
     f"curl --output-dir {INSIDE} -o f {URL}", (f"{INSIDE}/f",)),
    ("no directory: the value alone, unchanged", f"curl -o {OUTSIDE}/f {URL}",
     (f"{OUTSIDE}/f",)),
)


@pytest.mark.parametrize(
    "row,cmd,named", CONTROL_ROWS, ids=[r for r, *_ in CONTROL_ROWS],
)
def test_the_reading_is_unchanged_where_no_directory_is_in_force(row, cmd, named):
    """Workspace-write must still allow the inside row, or the fix over-refuses."""
    assert tuple(_extract_write_targets(cmd)) == named, row
    tier = "workspace-write" if named[0].startswith("/workspace") else "read-only"
    allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
    assert allowed is (tier == "workspace-write"), f"{tier} answered {allowed} for {cmd!r}: {reason}"


# ── rows that name nothing, and it is right that they name nothing ──────────────
#
# Each is a command that really writes nothing, measured on this host: the directory alone
# prints the body to stdout (`curl --output-dir D URL` rc=0, the directory empty
# afterwards), `-o -` is the documented stdout spelling, and an option after a bare `--` is
# an operand rather than an option. Naming any of them would refuse a run that changes no
# byte — the false block this walk treats as the worse error.
NO_DESTINATION_ROWS = (
    ("the directory alone", f"curl --output-dir {OUTSIDE} {URL}"),
    ("-o - is stdout", f"curl --output-dir {OUTSIDE} -o - {URL}"),
    ("after the terminator the pair is operands", f"curl -- -o f --output-dir {OUTSIDE} {URL}"),
)


@pytest.mark.parametrize("row,cmd", NO_DESTINATION_ROWS, ids=[r for r, _ in NO_DESTINATION_ROWS])
def test_a_directory_that_names_no_destination_names_nothing(row, cmd):
    """The modifier is not a destination, so it is not named as one."""
    assert tuple(_extract_write_targets(cmd)) == (), row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is True, f"{tier} refused {cmd!r} — a false block: {reason}"


# ── the measured limits, pinned so they cannot be mistaken for coverage ─────────
#
# Two kinds, both measured rather than inferred:
#
# * a **rooted** value is left un-relocated. `curl --output-dir D -o /abs/f URL` exits 23
#   on this host with nothing created, so the pair has no answer to read and the walk keeps
#   naming the value as it stands (the row is a false block, and it is named as one).
# * `-O` with no directory: the file lands in the workdir, which this reader does not name.
#   That is right while the workdir is the workspace and a hole if it is not, so the row is
#   pinned rather than claimed.
ROOTED_AND_WORKDIR_ROWS = (
    ("rooted -o with a directory", f"curl --output-dir {OUTSIDE} -o {OUTSIDE}/abs {URL}",
     (f"{OUTSIDE}/abs",), False),
    ("-O with no directory", f"curl -O {URL}", (), True),
)


@pytest.mark.parametrize(
    "row,cmd,named,allowed", ROOTED_AND_WORKDIR_ROWS,
    ids=[r for r, *_ in ROOTED_AND_WORKDIR_ROWS],
)
def test_the_rooted_value_and_the_workdir_row_are_pinned(row, cmd, named, allowed):
    """The two rows the relocation does not decide, with the measurement that fixes them."""
    assert tuple(_extract_write_targets(cmd)) == named, row
    got, reason, _ = _check_sandbox(cmd, "workspace-write", workdir="/workspace")
    assert got is allowed, f"{cmd!r} answered {got} ({reason})"


# ── the rest of what curl writes: every file-writing option it has ──────────────
#
# Each row is a real write, measured on this host (curl 8.9.0, win64) in a scratch
# directory and read back off disk: `-D f` leaves a header file, `-c f` a cookie jar, and
# `--hsts f`, `--alt-svc f`, `--libcurl f`, `--stderr f`, `--trace f` each create their
# file. Before the table carried these spellings the walk named nothing for them, which is
# the hole issue #1504 reports — an empty target list is allowed at both tiers.
NAMED_WRITER_ROWS = (
    ("--dump-header", f"curl --dump-header {OUTSIDE}/h {URL}", (f"{OUTSIDE}/h",)),
    ("-D", f"curl -D {OUTSIDE}/h {URL}", (f"{OUTSIDE}/h",)),
    ("-D inside a cluster", f"curl -sD {OUTSIDE}/h {URL}", (f"{OUTSIDE}/h",)),
    ("--cookie-jar", f"curl --cookie-jar {OUTSIDE}/c {URL}", (f"{OUTSIDE}/c",)),
    ("-c", f"curl -c {OUTSIDE}/c {URL}", (f"{OUTSIDE}/c",)),
    ("-c inside a cluster", f"curl -sc {OUTSIDE}/c {URL}", (f"{OUTSIDE}/c",)),
    ("--etag-save", f"curl --etag-save {OUTSIDE}/e {URL}", (f"{OUTSIDE}/e",)),
    ("--hsts", f"curl --hsts {OUTSIDE}/hs {URL}", (f"{OUTSIDE}/hs",)),
    ("--alt-svc", f"curl --alt-svc {OUTSIDE}/as {URL}", (f"{OUTSIDE}/as",)),
    ("--trace", f"curl --trace {OUTSIDE}/t {URL}", (f"{OUTSIDE}/t",)),
    ("--trace-ascii", f"curl --trace-ascii {OUTSIDE}/t {URL}", (f"{OUTSIDE}/t",)),
    ("--stderr", f"curl --stderr {OUTSIDE}/s {URL}", (f"{OUTSIDE}/s",)),
    ("--libcurl", f"curl --libcurl {OUTSIDE}/l.c {URL}", (f"{OUTSIDE}/l.c",)),
    # The `=` spelling is read although this host's curl rejects it, for the reason the
    # relocation rows give for `--output-dir=`: `--opt=value` is what a GNU getopt-style
    # parser accepts, so a build that takes it really creates the file.
    ("--dump-header=, the form this curl rejects",
     f"curl --dump-header={OUTSIDE}/h {URL}", (f"{OUTSIDE}/h",)),
)


@pytest.mark.parametrize(
    "row,cmd,named", NAMED_WRITER_ROWS, ids=[r for r, *_ in NAMED_WRITER_ROWS],
)
def test_the_other_writers_name_the_file_they_create(row, cmd, named):
    """The option's value is the write, so it is named and refused outside the workspace."""
    assert tuple(_extract_write_targets(cmd)) == named, row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is False, f"{tier} allowed {cmd!r}"
        assert named[0] in reason, f"{tier}: {reason!r} does not name {named[0]!r}"


INSIDE_WRITER_ROWS = tuple(
    (label, f"curl {opt} {INSIDE}/w {URL}", (f"{INSIDE}/w",))
    for label, opt in (
        ("-D", "-D"), ("-c", "-c"), ("--dump-header", "--dump-header"),
        ("--cookie-jar", "--cookie-jar"), ("--trace", "--trace"),
        ("--hsts", "--hsts"), ("--libcurl", "--libcurl"),
    )
)


@pytest.mark.parametrize(
    "row,cmd,named", INSIDE_WRITER_ROWS, ids=[r for r, *_ in INSIDE_WRITER_ROWS],
)
def test_the_other_writers_stay_allowed_inside_the_workspace(row, cmd, named):
    """The same rows inside the workspace are allowed, so naming them over-refuses nothing."""
    assert tuple(_extract_write_targets(cmd)) == named, row
    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", workdir="/workspace")
    assert allowed is True, f"workspace-write refused {cmd!r}: {reason}"


# ── the same options in the spelling that names no file ─────────────────────────
#
# `-` is the stdout spelling for these two, measured on this host (`curl -D - URL` and
# `curl -c - URL` are rc=0 with the directory empty afterwards, as is `--trace -`), and the
# reader drops a `-` value for **every** option it reads. Naming one would refuse a run that
# changes no byte, which is the direction this walk treats as the worse error.
STDOUT_WRITER_ROWS = (
    ("-D -", f"curl -D - {URL}"),
    ("--cookie-jar -", f"curl --cookie-jar - {URL}"),
    ("--trace -", f"curl --trace - {URL}"),
)


@pytest.mark.parametrize(
    "row,cmd", STDOUT_WRITER_ROWS, ids=[r for r, _ in STDOUT_WRITER_ROWS],
)
def test_a_dash_value_is_stdout_and_names_no_file(row, cmd):
    """The run writes nothing, so no path is judged and both tiers allow it."""
    assert tuple(_extract_write_targets(cmd)) == (), row
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is True, f"{tier} refused {cmd!r} — a false block: {reason}"


# ── the other direction: an option that only ever *reads* its value ─────────────
#
# The table names writers, so the value of a read-only option must stay unnamed — the shape
# a later widening of that table would break first. These rows are statements about the
# walk (it names nothing), not ground truth about curl: each option below is the program's
# documented way of *reading* the path it is given.
READER_OPTION_ROWS = (
    ("--netrc-file", f"curl --netrc-file {OUTSIDE}/n {URL}"),
    ("--cacert", f"curl --cacert {OUTSIDE}/ca.pem {URL}"),
    ("-b", f"curl -b {OUTSIDE}/c {URL}"),
    ("-T", f"curl -T {OUTSIDE}/up {URL}"),
    ("--data-binary @", f"curl --data-binary @{OUTSIDE}/d {URL}"),
)


@pytest.mark.parametrize(
    "row,cmd", READER_OPTION_ROWS, ids=[r for r, _ in READER_OPTION_ROWS],
)
def test_an_option_that_reads_its_value_names_no_write(row, cmd):
    """A read is not a write: widening the table past the writers reds this row."""
    assert tuple(_extract_write_targets(cmd)) == (), row


# ── ground truth: the relocation is a fact about the tool, not about the reader ──
#
# Posix-only, for the reason `test_bash_tool_option_destinations.py` gives for its own
# executed arm: the daemon's shell on Windows is cmd.exe, where `curl` is not the tool
# these flags belong to. The directory is created by this test, and the fake OS temp root
# keeps `tmp_path` from being allowed for being the temp root instead of for the reading.
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX shell ground truth: the daemon's shell on Windows is cmd.exe",
)
@pytest.mark.parametrize(
    "row,args,expected_name",
    (
        ("-o is relocated", ["--output-dir", "{d}", "-o", "f", "{u}"], "f"),
        ("-O is relocated", ["--output-dir", "{d}", "-O", "{u}"], "source.txt"),
        ("the directory alone writes nothing", ["--output-dir", "{d}", "{u}"], None),
    ),
    ids=["-o", "-O", "dir-alone"],
)
def test_the_relocation_really_moves_the_file(monkeypatch, tmp_path, row, args, expected_name):
    """Run it: the file appears under the directory, under the name the walk named.

    The walk's answer is a reading, and this is the measurement it stands on. Without it
    the rows above would pin a spelling rather than a behaviour: the premise "the bytes
    land in `<dir>`" would be a claim about curl that no test here had run. The name is
    asserted too, so `-O`'s directory-level answer is measured against the file curl
    really creates under it (`source.txt`, the URL's last segment).
    """
    if shutil.which("curl") is None:
        pytest.skip("`curl` is not on PATH here, so this ground truth is unmeasurable")

    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/fake-os-temp")
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    source = workspace / "sub" / "source.txt"
    source.write_text("hello\n", encoding="utf-8")
    directory = workspace / "out"
    directory.mkdir()

    command = "curl -s " + " ".join(
        a.format(d=directory.as_posix(), u="file://" + source.as_posix()) for a in args
    )
    tool = BashTool()
    result = _run(tool.execute({
        "command": command, "sandbox": "workspace-write", "workdir": str(workspace),
    }))

    assert "not executed" not in result.content, (
        f"{command!r} was refused at the sandbox: {result.content}"
    )
    landed = sorted(p.name for p in directory.iterdir())
    if expected_name is None:
        assert landed == [], f"{command!r} wrote {landed!r} although no -o/-O was given"
    else:
        assert landed == [expected_name], (
            f"{command!r} left {landed!r} under the directory, not [{expected_name!r}] — the "
            "relocation this file asserts is not what curl does here; re-measure before "
            "changing the reader"
        )
        cwd_leftover = [p.name for p in workspace.iterdir() if p.is_file()]
        assert cwd_leftover == [], (
            f"{command!r} also wrote {cwd_leftover!r} in the workdir — not relocated"
        )


# ── ground truth for the writers above, not only for the relocation ─────────────
#
# `NAMED_WRITER_ROWS` says what the walk answers; this says why the answer is right — the
# file really appears where the option named it. Without it the rows above would pin a
# spelling rather than a behaviour: "this option writes a file" would be a claim about curl
# that nothing here had run. Posix-only for the reason the relocation arm above gives.
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX shell ground truth: the daemon's shell on Windows is cmd.exe",
)
@pytest.mark.parametrize(
    "row,args,expected_name",
    (
        ("-D", ["-s", "-D", "{d}/h.txt", "{u}"], "h.txt"),
        ("-c", ["-s", "-c", "{d}/c.txt", "{u}"], "c.txt"),
        ("--trace", ["-s", "--trace", "{d}/t.txt", "{u}"], "t.txt"),
        ("--etag-save", ["-s", "--etag-save", "{d}/e.txt", "{u}"], "e.txt"),
    ),
    ids=["-D", "-c", "--trace", "--etag-save"],
)
def test_the_other_writers_really_create_the_file_they_name(
    monkeypatch, tmp_path, row, args, expected_name
):
    """Run it: the option's value is the path that appears on disk afterwards."""
    if shutil.which("curl") is None:
        pytest.skip("`curl` is not on PATH here, so this ground truth is unmeasurable")

    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/fake-os-temp")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_text("hello\n", encoding="utf-8")
    directory = workspace / "out"
    directory.mkdir()

    command = "curl " + " ".join(
        a.format(d=directory.as_posix(), u="file://" + source.as_posix()) for a in args
    )
    tool = BashTool()
    result = _run(tool.execute({
        "command": command, "sandbox": "workspace-write", "workdir": str(workspace),
    }))

    assert "not executed" not in result.content, (
        f"{command!r} was refused at the sandbox: {result.content}"
    )
    assert sorted(p.name for p in directory.iterdir()) == [expected_name], (
        f"{command!r} did not create {expected_name!r} under the directory — the option is "
        "not a write here, so naming it would be an over-approximation; re-measure before "
        "changing the reader"
    )
