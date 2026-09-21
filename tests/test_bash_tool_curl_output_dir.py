"""`curl --output-dir` relocates what `-o` and `-O` write (issue #1504).

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

What this file does **not** claim is the family the same reader still cannot name:
`--dump-header`/`-D`, `--cookie-jar`/`-c`, `--trace`, `--trace-ascii`, `--etag-save`,
`--stderr` and `--libcurl` are destinations the walk enumerates nowhere, so it names `()`
for them and both tiers allow a command that really creates the file (measured through
`curl`, `-D f` leaving a 91-byte file and `-c f` a 131-byte jar). Those rows are pinned here
as a measured hole rather than left silent, which is the shape the maintainer's reply asks
for; `-D` and `--trace` are also the spellings whose *long* form eats the word in issue
#1461, so a fix there must not read one as the other.

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


# ── the hole this file does *not* close, pinned as a hole ──────────────────────
#
# Every row below is a real write the walk cannot see: the options are enumerated nowhere,
# so the target list is empty and an empty list is allowed by construction. Measured
# through `curl` on this host in a scratch directory (issue #1504's table), each of
# `--dump-header`, `-D`, `--cookie-jar`, `-c`, `--etag-save`, `--trace` really creates the
# file it names. They are pinned here so a later reader finds them named as a hole instead
# of finding an empty list and assuming coverage — and so that a fix has a row to turn.
UNLISTED_WRITER_ROWS = (
    ("--dump-header", f"curl --dump-header {OUTSIDE}/h {URL}"),
    ("-D", f"curl -D {OUTSIDE}/h {URL}"),
    ("--cookie-jar", f"curl --cookie-jar {OUTSIDE}/c {URL}"),
    ("-c", f"curl -c {OUTSIDE}/c {URL}"),
    ("--etag-save", f"curl --etag-save {OUTSIDE}/e {URL}"),
    ("--trace", f"curl --trace {OUTSIDE}/t {URL}"),
    ("--trace-ascii", f"curl --trace-ascii {OUTSIDE}/t {URL}"),
    ("--stderr", f"curl --stderr {OUTSIDE}/s {URL}"),
    ("--libcurl", f"curl --libcurl {OUTSIDE}/l.c {URL}"),
    ("--output-dir only, no -o/-O to relocate", f"curl --output-dir {OUTSIDE} -o f {URL}".replace(" -o f", "")),
)


@pytest.mark.parametrize(
    "row,cmd", UNLISTED_WRITER_ROWS, ids=[r for r, _ in UNLISTED_WRITER_ROWS],
)
def test_the_unlisted_writers_are_pinned_as_a_measured_hole(row, cmd):
    """A hole, named: the walk names nothing and both tiers allow the write.

    Pinned in the shape `test_bash_tool_option_destinations.py` uses for its own limits —
    asserting today's answer so it cannot be read as coverage, and failing loudly when a
    fix lands, at which point this row moves up into the block that asserts the named path.
    """
    assert tuple(_extract_write_targets(cmd)) == (), row
    for tier in ("read-only", "workspace-write"):
        allowed, _reason, _ = _check_sandbox(cmd, tier, workdir="/workspace")
        assert allowed is True, f"{tier} now refuses {cmd!r} — move this row out of the hole"


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
