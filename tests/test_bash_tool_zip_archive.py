"""`zip` writes the archive named by its **first** operand, so the walk names it.

`zip` was the last row of `test_bash_tool_option_destinations.py`'s
`UNCOVERED_WRITERS` table, and it left that table the same way `rsync` and `split`
did: its destination is an **operand** rather than an option's value, so the
question is which operand is written, not the per-verb flag grammar that table is
about. It differs from both of those in one respect — the write is at the *first*
operand, and every operand rule the walk already had reads the last one or all of
them, so nothing reached it.

Measured on master `26449c59` with the real predicate, the archive outside every
allowed root: `zip <outside>/a.zip x` reported an **empty target list**, and an
empty list is allowed by construction — the loop that judges targets never runs.
`cp` on the same path was refused in the same geometry, which is what makes this a
hole rather than an opinion. The same file run against the fixed tree names 24 of
its 37 rows where master names 0.

Ground truth for what `zip` really does was taken before the rule was written, on
the host's own binary (`/usr/bin/zip`, Info-ZIP 3.0, 2026-09-19): one fresh
directory per row holding `f` and `g`, `a.zip` pre-built where the row needs one,
and the result read back off disk as `st_mtime_ns` **plus** a content hash. The hash
alone is not enough — `zip a.zip f` on an archive that already holds `f` writes
identical bytes, so only the mtime says the file was rewritten. The full table is
the comment above `_ZIP_OPTIONS_WITH_VALUE`; the four rows this file's arms rest on
are:

* a run with **no list** writes nothing (`zip a.zip`, `zip -d a.zip`, `zip -v a.zip`
  all exit 12 with "Nothing to do!"), so naming the archive there would be a block
  on a command that writes nothing;
* `-T` is *not* a read — `zip -T a.zip f` rewrote the archive (mtime moved) while
  `zip -T a.zip` tested it and left it alone;
* `-m`/`--move` **deletes** every listed file, so the operands after the archive are
  write targets too;
* the read spellings are whole tokens and case matters — `-sf` is show-files while
  `-f` is freshen, `-L` is the licence while `-l` is an LF→CRLF conversion that
  really creates the archive.

Nothing here executes a command through the walk: `_check_sandbox` and
`_extract_write_targets` are pure (they `realpath` a path and parse a string), so
the outside and protected paths below are arguments to a predicate. The one arm that
does execute a real `zip` runs it in `tmp_path` — a directory the test creates — and
is skipped on Windows, whose CI leg has no `zip`.
"""

import os
import shutil
import subprocess
import sys
import time

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import _check_sandbox, _extract_write_targets

# Outside every allowed root (the workspace, the OS temp root, the evolution data
# dir) and used only as an argument to the pure predicate — never executed.
OUTSIDE = "/outside/emrg"
WORKSPACE = "/workspace"

# The daemon's own rant store: a protected file, and the one a sandboxed task must
# not be able to destroy. Also only ever an input to the predicate.
PROTECTED = "~/.emrg/rants.jsonl"


def tiers(cmd):
    """Both tier verdicts for one command, as the toolbox would answer them."""
    return {
        tier: _check_sandbox(cmd, tier, WORKSPACE)[0]
        for tier in ("read-only", "workspace-write")
    }


def test_the_predicate_is_the_one_the_tiers_read():
    """The two functions this file judges are the ones the tool layer calls.

    A test that reached a private re-reading would keep passing while the shipped
    guard changed, which is the failure this whole class of test exists to prevent.
    """
    allowed, _reason, enforcement = _check_sandbox(
        "zip a.zip in.txt", "workspace-write", WORKSPACE
    )
    assert allowed is True
    assert enforcement in ("partial", "full")


# ── writing forms: the archive operand must be named ────────────────────────────────
#
# (row, command, targets) — the operand files are deliberately *inside* the
# workspace so a rule that named the wrong operand would be caught by the target it
# reports rather than by the verdict it gives.
WRITING_FORMS = (
    ("add", "zip {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("quiet", "zip -q {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("recurse", "zip -r {out}/a.zip {ws}/dir", ("{out}/a.zip",)),
    ("delete an entry", "zip -d {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("update", "zip -u {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("freshen", "zip -f {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("stamp from entries", "zip -o {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    # `-T` with a list is the update path, not the test path (ground truth, mtime).
    ("test with a list", "zip -T {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("verbose with a list", "zip -v {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    # Lowercase `-l` is the LF→CRLF conversion, not `-L` the licence.
    ("line-ending conversion", "zip -l {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    # `-m` deletes what it archived, so those operands are write targets too.
    ("move", "zip -m {out}/a.zip {ws}/in.txt", ("{out}/a.zip", "{ws}/in.txt")),
    ("move, long spelling",
     "zip --move {out}/a.zip {ws}/in.txt", ("{out}/a.zip", "{ws}/in.txt")),
    ("move two operands",
     "zip -m {out}/a.zip {ws}/a {ws}/b",
     ("{out}/a.zip", "{ws}/a", "{ws}/b")),
    # Spaced option values must not be read as the archive (the wrong-name defect
    # `_positional_args` exists to avoid).
    ("spaced -b temporary dir",
     "zip -b {ws}/tmp {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("spaced -n suffix",
     "zip -n .jpg {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("spaced -s split size",
     "zip -s 64k {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("spaced -t date",
     "zip -t 20010101 {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("spaced -TT command",
     "zip -TT 'unzip -tqq' {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    # `-P` was the sixth spaced value and the one the table was short: the walk
    # named the *password* instead of the archive, and when that token resolved
    # inside the workspace the run was allowed at `workspace-write` while it
    # really rewrote the archive outside every allowed root. Measured on the
    # host's binary 2026-09-20: `zip -P secret a.zip f` is rc=0 and creates
    # `a.zip`, i.e. the archive is still the first operand of the run.
    ("spaced -P password",
     "zip -P secret {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("spaced -P password, attached",
     "zip -Psecret {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    # The hole shape, spelled out: the password is a path *inside* the workspace,
    # so a rule that named it left the real write to `{out}/a.zip` unnamed and
    # both tiers allowed the run. This row is the one that reds in that
    # direction; the two above red on the name.
    ("spaced -P password inside the workspace",
     "zip -P {ws}/pw {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    # `-tt`, `-Z` and `-lf` were the next three spaced values the same table was
    # short of, each measured 2026-09-20 on the same binary. `zip -tt 20200101
    # a.zip f` exits 12 with "invalid date entered for -tt option" — the option
    # **ate** the token, so the archive sits one position further right; `zip -Z
    # store a.zip f` is rc=0 and creates no file named `store`; `zip -lf ./log
    # a.zip f` is rc=0 and creates `log.log`. Absent from the table, the walk
    # named the date, the method and the log path respectively, so
    # `zip -tt 20010101 {out}/a.zip f` was **allowed** at `workspace-write`
    # while really rewriting the archive outside every allowed root.
    ("spaced -tt date",
     "zip -tt 20010101 {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("spaced -Z method",
     "zip -Z store {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    # `-lf`'s value is a **path** zip writes, so it is named *beside* the archive
    # rather than consumed and forgotten: the run writes both. Both spellings,
    # because the attached one was measured to work (`zip -lf./log2 a.zip f`
    # created `log2.log`).
    ("spaced -lf logfile",
     "zip -lf {ws}/log {out}/a.zip {ws}/in.txt", ("{out}/a.zip", "{ws}/log")),
    ("attached -lf logfile",
     "zip -lf{ws}/log {out}/a.zip {ws}/in.txt", ("{out}/a.zip", "{ws}/log")),
    # A resolved verb, a chain and a nested shell all reach the same rule.
    ("absolute path to the verb",
     "/usr/bin/zip {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("second in a chain",
     "true && zip {out}/a.zip {ws}/in.txt", ("{out}/a.zip",)),
    ("inside sh -c",
     "sh -c 'zip {out}/a.zip {ws}/in.txt'", ("{out}/a.zip",)),
)


def rendered(rows):
    """The rows with their two placeholders filled in, as (row, cmd, targets)."""
    return [
        (row, cmd.format(out=OUTSIDE, ws=WORKSPACE),
         tuple(t.format(out=OUTSIDE, ws=WORKSPACE) for t in targets))
        for row, cmd, targets in rows
    ]


@pytest.mark.parametrize("row,cmd,targets", rendered(WRITING_FORMS),
                         ids=[row for row, _c, _t in WRITING_FORMS])
def test_every_writing_form_names_the_archive(row, cmd, targets):
    """The first operand is the archive, and the walk must report exactly it.

    The whole list is asserted, not just the first entry, because the `-m` rows have
    more than one target and a rule that named the wrong end of the operand list
    would still pass a "something was named" test.
    """
    assert _extract_write_targets(cmd) == list(targets), row


@pytest.mark.parametrize("row,cmd,targets", rendered(WRITING_FORMS),
                         ids=[row for row, _c, _t in WRITING_FORMS])
def test_a_named_archive_is_refused_at_both_tiers(row, cmd, targets):
    """An archive outside every allowed root is refused whether or not writes are on.

    Both tiers, because the hole was in both and the untested tier is the one a cycle
    actually runs in: `workspace-write` still refuses writes outside the workspace,
    and `read-only` refuses them anywhere.
    """
    assert targets, row  # the row's named targets are the other test's assertion
    for tier, allowed in tiers(cmd).items():
        assert allowed is False, f"{row}: {tier} allowed a write to {OUTSIDE}"


def test_the_protected_daemon_file_is_refused_at_both_tiers():
    """The strongest case: the archive *is* the file the sandbox protects.

    `zip <protected> x` creates or rewrites its archive in place, so the archive here
    is the daemon's own rant store and not merely a file beside it — measured while
    writing this test, a sibling path (`rants.jsonl.zip`) is refused as "outside
    workspace", which says nothing about the protected check. The path is an argument
    to the pure predicate — `_check_sandbox` `realpath`s it and opens nothing — so the
    host's real store is never touched by this test, and the command is never run.
    """
    cmd = f"zip {PROTECTED} {WORKSPACE}/in.txt"
    assert _extract_write_targets(cmd) == [PROTECTED]

    allowed, reason, _ = _check_sandbox(cmd, "read-only", WORKSPACE)
    assert allowed is False, "read-only allowed a write into the protected store"
    assert PROTECTED in (reason or ""), reason

    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", WORKSPACE)
    assert allowed is False, "workspace-write allowed a write into the protected store"
    assert "protected" in (reason or ""), reason


def test_an_archive_inside_the_workspace_is_allowed_where_writes_are_on():
    """The rule places the write; it does not refuse how the tool is normally used.

    Named-and-inside is the ordinary case, and the two tiers read it as they read any
    other in-workspace write: `read-only` still refuses it on its own account (that
    tier refuses every named target but `/dev/null`), and `workspace-write` allows it.
    """
    cmd = f"zip {WORKSPACE}/a.zip {WORKSPACE}/in.txt"
    assert _extract_write_targets(cmd) == [f"{WORKSPACE}/a.zip"]
    assert tiers(cmd)["read-only"] is False
    assert tiers(cmd)["workspace-write"] is True


# ── forms that name nothing: naming them would be a false block ─────────────────────
#
# Each row is one of the measured "nothing written" shapes: the read spellings, and
# the runs whose only operand is the archive (zip exits 12 having created nothing).
NAMING_NOTHING = (
    ("-T alone", "zip -T {out}/a.zip"),
    ("-sf alone", "zip -sf {out}/a.zip"),
    ("-sf with a list", "zip -sf {out}/a.zip {ws}/in.txt"),
    ("--show-files with a list", "zip --show-files {out}/a.zip {ws}/in.txt"),
    ("-su alone", "zip -su {out}/a.zip"),
    ("-sU alone", "zip -sU {out}/a.zip"),
    ("-h with a list", "zip -h {out}/a.zip {ws}/in.txt"),
    ("-h2 with a list", "zip -h2 {out}/a.zip {ws}/in.txt"),
    ("-L with a list", "zip -L {out}/a.zip {ws}/in.txt"),
    ("--help with a list", "zip --help {out}/a.zip {ws}/in.txt"),
    ("--version with a list", "zip --version {out}/a.zip {ws}/in.txt"),
    ("archive and no list", "zip {out}/a.zip"),
    ("delete and no members", "zip -d {out}/a.zip"),
    ("verbose and no list", "zip -v {out}/a.zip"),
    # `-P`'s value is the *next* token, so here it eats the archive and the run
    # has only one operand left: measured rc=12, nothing written. Before `-P`
    # joined the value table the walk named that eaten token — a false block on a
    # run that writes nothing at all.
    ("password eats the archive", "zip -P {out}/a.zip {ws}/in.txt"),
    ("password and no list", "zip -P secret {out}/a.zip"),
)


@pytest.mark.parametrize("row,cmd", NAMING_NOTHING, ids=[row for row, _ in NAMING_NOTHING])
def test_a_form_that_writes_nothing_names_nothing(row, cmd):
    """Nothing in these rows creates or rewrites a file, so no target is reported."""
    assert _extract_write_targets(cmd.format(out=OUTSIDE, ws=WORKSPACE)) == [], row


@pytest.mark.parametrize("row,cmd", NAMING_NOTHING, ids=[row for row, _ in NAMING_NOTHING])
def test_those_forms_stay_allowed(row, cmd):
    """The false block is the direction this walk treats as worse than the hole."""
    for tier, allowed in tiers(cmd.format(out=OUTSIDE, ws=WORKSPACE)).items():
        assert allowed is True, f"{row}: {tier} refused a form that writes nothing"


# ── the discriminator, in both directions ───────────────────────────────────────────
def test_the_list_is_what_decides_and_the_two_spellings_are_read_apart():
    """The control: a pair that differs only by the list, and two token pairs.

    Without this, a rule that named nothing at all — or a fixture that never reached
    the rule — would leave every test above green while the defect was fully open.

    The token pairs are the case-sensitivity claim, and they are read in the
    direction that costs data if it is wrong: `-L` (the licence, a read) versus `-l`
    (the LF→CRLF conversion, which really creates the archive), and `-sf`
    (show-files) versus `-f` (freshen, a write). A letter scan — the shape the
    compressor family uses — would conflate both pairs, so each is asserted unnamed
    on the read side **and** named on the write side.
    """
    writes = f"zip {OUTSIDE}/a.zip {WORKSPACE}/in.txt"
    no_list = f"zip {OUTSIDE}/a.zip"
    assert _extract_write_targets(writes) == [f"{OUTSIDE}/a.zip"]
    assert _extract_write_targets(no_list) == []

    read_rows = (f"zip -L {OUTSIDE}/a.zip {WORKSPACE}/in.txt",
                 f"zip -sf {OUTSIDE}/a.zip {WORKSPACE}/in.txt")
    write_rows = (f"zip -l {OUTSIDE}/a.zip {WORKSPACE}/in.txt",
                  f"zip -f {OUTSIDE}/a.zip {WORKSPACE}/in.txt")
    for row in read_rows:
        assert _extract_write_targets(row) == [], row
    for row in write_rows:
        assert _extract_write_targets(row) == [f"{OUTSIDE}/a.zip"], row


# ── mutation arms: a row that cannot be flipped is not a claim ──────────────────────
def test_the_rule_is_what_names_the_archive() -> None:
    """Blind the rule, and its rows must go back to the ALLOW master gave."""
    cmd = f"zip {OUTSIDE}/a.zip {WORKSPACE}/in.txt"
    assert tiers(cmd)["read-only"] is False
    original = bash_tool._zip_write_targets
    try:
        bash_tool._zip_write_targets = lambda tokens, i: []
        allowed = tiers(cmd)["read-only"]
    finally:
        bash_tool._zip_write_targets = original
    assert allowed is True, (
        "the row survives blinding _zip_write_targets — it does not depend on the "
        "rule it claims to test"
    )


def test_the_read_gate_is_what_spares_the_read_forms() -> None:
    """The gate is a second piece of code, so it gets its own arm, both ways.

    One direction widens it (the write row's own token becomes a read) and the write
    row must return to ALLOW; the other empties it and the read rows must become
    refusals. A rule that named the archive without the gate would pass neither.

    Every row in the closed-gate half carries a **list** on purpose: a read spelling
    with no list after it is spared by the no-list rule as well, so it would stay
    allowed with the gate shut — measured while writing this arm, `zip -T <outside>/a.zip`
    does exactly that — and asserting otherwise would be testing the wrong line.
    """
    write_row = f"zip -l {OUTSIDE}/a.zip {WORKSPACE}/in.txt"
    read_row = f"zip -sf {OUTSIDE}/a.zip {WORKSPACE}/in.txt"
    assert tiers(write_row)["read-only"] is False
    assert tiers(read_row)["read-only"] is True

    original = bash_tool._ZIP_READ_TOKENS
    try:
        bash_tool._ZIP_READ_TOKENS = frozenset({"-l"})
        assert tiers(write_row)["read-only"] is True, (
            "with the gate forced wide the write row must return to the ALLOW master "
            "gave — otherwise the gate is not what refuses it"
        )
        bash_tool._ZIP_READ_TOKENS = frozenset()
        for row in (read_row,
                    f"zip --show-files {OUTSIDE}/a.zip {WORKSPACE}/in.txt",
                    f"zip -L {OUTSIDE}/a.zip {WORKSPACE}/in.txt",
                    f"zip -h {OUTSIDE}/a.zip {WORKSPACE}/in.txt"):
            assert tiers(row)["read-only"] is False, (
                f"{row}: with the gate forced shut the read form must be refused — "
                "otherwise nothing about it depends on the gate"
            )
    finally:
        bash_tool._ZIP_READ_TOKENS = original


def test_the_move_flag_is_what_names_the_listed_operands() -> None:
    """`-m` is a third piece of the rule: without it only the archive is a target.

    Read in both directions, because the two are different claims — that the flag
    adds the operands, and that *nothing else* adds them. The unflagged row is the
    `zip a.zip f` form, whose operand is only read.
    """
    moved = f"zip -m {OUTSIDE}/a.zip {WORKSPACE}/in.txt"
    archived = f"zip {OUTSIDE}/a.zip {WORKSPACE}/in.txt"
    assert f"{WORKSPACE}/in.txt" in _extract_write_targets(moved)
    assert f"{WORKSPACE}/in.txt" not in _extract_write_targets(archived)


# ── the named limits, pinned so a later reader does not re-derive them ──────────────
def test_the_exclusion_list_is_the_documented_residual():
    """An operand the `-x` list neutralises by name is still named.

    Measured: `zip -m a.zip f -x f` writes nothing (exit 12), so the over-block lands
    on a run that does nothing anyway. The alternative is matching operand names
    against exclusion patterns inside the walk — the grammar these rules refuse to
    grow. Pinned so the coverage claim in `bash_tool.py` stays as wide as the rule.
    """
    cmd = f"zip -m {OUTSIDE}/a.zip {WORKSPACE}/in.txt -x {WORKSPACE}/in.txt"
    assert _extract_write_targets(cmd) == [
        f"{OUTSIDE}/a.zip", f"{WORKSPACE}/in.txt", f"{WORKSPACE}/in.txt",
    ]


def test_names_read_from_stdin_are_the_documented_residual():
    """`-@` takes its names from stdin, which the walk cannot see.

    The archive is still named, so the run is judged by where its own write lands;
    the names the standard input supplies stay unnamed, which is the same limit every
    other stdin-driven form has.
    """
    cmd = f"zip -@ {OUTSIDE}/a.zip"
    assert _extract_write_targets(cmd) == []


# ── ground truth, executed: the derivation the rule is built on ─────────────────────
needs_zip = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("zip") is None,
    reason="no zip on the Windows CI leg",
)


def _archive_state(path):
    """(mtime_ns, size) of an archive, or None when it does not exist."""
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


@needs_zip
def test_zip_really_creates_the_archive_it_is_pointed_at(tmp_path):
    """Drive the tool itself, so the rule rests on what it does rather than a manual.

    `tmp_path` is a directory the test creates, so nothing here can reach a host
    path; the archive is read back off disk instead of assumed.
    """
    (tmp_path / "in.txt").write_text("hello\n")
    archive = tmp_path / "a.zip"
    result = subprocess.run(
        ["zip", "-q", str(archive), str(tmp_path / "in.txt")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert _archive_state(archive) is not None, "the named archive was not created"
    assert _extract_write_targets(
        f"zip -q {archive} {tmp_path / 'in.txt'}"
    ) == [str(archive)]


@needs_zip
def test_the_read_forms_really_leave_the_archive_alone(tmp_path):
    """The read half, executed: `-sf` and `-T`-without-a-list must not rewrite it.

    mtime is the discriminator rather than the content hash, because `zip -T a.zip f`
    on an unchanged operand rewrites *identical bytes* — measured, and the reason the
    product rule treats `-T` as a write whenever a list follows it.
    """
    (tmp_path / "in.txt").write_text("hello\n")
    archive = tmp_path / "a.zip"
    subprocess.run(["zip", "-q", str(archive), str(tmp_path / "in.txt")],
                   capture_output=True)
    before = _archive_state(archive)

    for argv in (["zip", "-sf", str(archive)],
                 ["zip", "-sf", str(archive), str(tmp_path / "in.txt")],
                 ["zip", "-T", str(archive)]):
        time.sleep(0.01)
        result = subprocess.run(argv, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        assert result.returncode == 0, (argv, result.stdout, result.stderr)
        assert _archive_state(archive) == before, (
            f"{' '.join(argv)} rewrote the archive, so the rule names a write "
            "as a read"
        )
        assert _extract_write_targets(" ".join(argv)) == []

    # …and the same letter with a list does rewrite it, which is the other direction.
    time.sleep(0.01)
    result = subprocess.run(["zip", "-T", str(archive), str(tmp_path / "in.txt")],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stderr
    assert _archive_state(archive) != before, (
        "`zip -T a.zip f` did not rewrite the archive on this host, so the rule's "
        "write side for it is a claim rather than a measurement"
    )
    assert _extract_write_targets(
        f"zip -T {archive} {tmp_path / 'in.txt'}"
    ) == [str(archive)]


@needs_zip
def test_move_really_removes_the_operand_it_archived(tmp_path):
    """`-m` is the row that makes the listed operands write targets; executed.

    The operand must be *gone* afterwards — that is the whole claim — and it is a file
    the test created inside its own directory, so nothing outside `tmp_path` is at
    stake.
    """
    operand = tmp_path / "in.txt"
    operand.write_text("hello\n")
    archive = tmp_path / "a.zip"
    result = subprocess.run(["zip", "-m", "-q", str(archive), str(operand)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stderr
    assert _archive_state(archive) is not None, "the archive was not created"
    assert not operand.exists(), "`zip -m` did not remove the operand it archived"
    assert _extract_write_targets(f"zip -m {archive} {operand}") == [
        str(archive), str(operand),
    ]


@needs_zip
def test_an_archive_only_run_really_writes_nothing(tmp_path):
    """The no-list row, executed: exit 12 and no archive appears.

    This is what keeps `zip a.zip` out of the target list — naming the archive there
    would refuse a run that creates nothing.
    """
    archive = tmp_path / "a.zip"
    before = _archive_state(archive)
    result = subprocess.run(["zip", str(archive)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode != 0, "an archive-only run unexpectedly succeeded"
    assert _archive_state(archive) == before, (
        "`zip a.zip` created the archive, so the no-list rule is wrong"
    )
    assert _extract_write_targets(f"zip {archive}") == []


@needs_zip
def test_the_lowercase_l_really_writes_and_the_uppercase_L_does_not(tmp_path):
    """The case pair, executed — the one a letter scan would get wrong."""
    operand = tmp_path / "in.txt"
    operand.write_text("hello\n")

    lower = tmp_path / "lower.zip"
    result = subprocess.run(["zip", "-l", "-q", str(lower), str(operand)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stderr
    assert os.path.exists(lower), "lowercase `-l` did not create the archive"
    assert _extract_write_targets(f"zip -l {lower} {operand}") == [str(lower)]

    upper = tmp_path / "upper.zip"
    result = subprocess.run(["zip", "-L", str(upper), str(operand)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stderr
    assert not os.path.exists(upper), "uppercase `-L` created the archive"
    assert _extract_write_targets(f"zip -L {upper} {operand}") == []


# ── the values that are consumed but are not paths: `-tt`, `-Z` ────────────────────
def test_the_new_table_rows_are_what_moves_the_naming_onto_the_archive() -> None:
    """Drop the rows the table was short of and the hole comes back, measured.

    Consequence 6 of `bash_tool.py`'s table claims `-tt`, `-Z` and `-lf` are what
    moves the naming onto the archive. Read in both directions: with the table as
    written the row is refused, and with `-tt`/`-Z` removed (the state before this
    cycle) the walk names the date again and the run is **allowed** while it really
    rewrites the archive outside every allowed root — the exact hole measured on
    the host.
    """
    cmd = f"zip -tt 20010101 {OUTSIDE}/a.zip {WORKSPACE}/in.txt"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/a.zip"]
    assert tiers(cmd)["workspace-write"] is False

    original = bash_tool._ZIP_OPTIONS_WITH_VALUE
    try:
        bash_tool._ZIP_OPTIONS_WITH_VALUE = original - {"-tt", "-Z"}
        assert _extract_write_targets(cmd) == ["20010101"], (
            "with `-tt` out of the table the walk must name the date — that wrong "
            "name is what the row exists to prevent"
        )
        assert tiers(cmd)["workspace-write"] is True, (
            "with `-tt` out of the table the run is allowed while it really "
            "rewrites the archive outside — the hole, restored"
        )
    finally:
        bash_tool._ZIP_OPTIONS_WITH_VALUE = original


@needs_zip
def test_tt_and_Z_really_consume_the_token_they_are_given(tmp_path):
    """Executed, because the table's whole meaning is "this option eats the next one".

    `-tt` gets a **future** date so the run succeeds rather than relying on an error
    message: every file is then "before" the bound, so the archive is created — and
    no file named after the date exists, which is what says the date was consumed
    rather than taken as the archive. `-Z` is the same shape: the method name is
    eaten, so the archive is the operand and no file named `store` appears.
    """
    operand = tmp_path / "in.txt"
    operand.write_text("hello\n")

    dated = tmp_path / "dated.zip"
    result = subprocess.run(["zip", "-tt", "2099-01-01", str(dated), str(operand)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert dated.exists(), "the archive named after the date was not created"
    assert not (tmp_path / "2099-01-01").exists(), (
        "zip treated the date as a file name, so `-tt` does not consume its value"
    )

    method = tmp_path / "method.zip"
    result = subprocess.run(["zip", "-Z", "store", str(method), str(operand)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert method.exists(), "the archive named after the method was not created"
    assert not (tmp_path / "store").exists(), (
        "zip treated the method as a file name, so `-Z` does not consume its value"
    )


# ── `-lf`: a spaced value that is itself a path zip writes ──────────────────────────
def test_the_logfile_is_named_where_no_archive_is() -> None:
    """`-lf` writes a path the archive rule cannot reach, so it is named on its own.

    Three shapes, all measured on the host (consequence 6): a read spelling still
    creates the logfile, the exit-12 "Nothing to do!" case still creates it, and it
    is written in addition to the archive when there is one. Both directions,
    because a rule that only added the value to the table would name the logfile
    nowhere and let it be written outside every allowed root.
    """
    read_row = f"zip -sf -lf {OUTSIDE}/log {OUTSIDE}/a.zip {WORKSPACE}/in.txt"
    no_list_row = f"zip -lf {OUTSIDE}/log {OUTSIDE}/a.zip"
    write_row = f"zip -lf {OUTSIDE}/log {OUTSIDE}/a.zip {WORKSPACE}/in.txt"
    attached_row = f"zip -lf{OUTSIDE}/log {OUTSIDE}/a.zip {WORKSPACE}/in.txt"

    assert _extract_write_targets(read_row) == [f"{OUTSIDE}/log"]
    assert _extract_write_targets(no_list_row) == [f"{OUTSIDE}/log"]
    assert _extract_write_targets(write_row) == [f"{OUTSIDE}/a.zip", f"{OUTSIDE}/log"]
    assert _extract_write_targets(attached_row) == [f"{OUTSIDE}/a.zip", f"{OUTSIDE}/log"]
    for row in (read_row, no_list_row, write_row):
        assert tiers(row)["workspace-write"] is False, row

    original = bash_tool._zip_logfile_targets
    try:
        bash_tool._zip_logfile_targets = lambda words: []
        assert _extract_write_targets(read_row) == [], (
            "the read shape must go back to naming nothing when the logfile rule is "
            "blinded — otherwise it is spared by something else"
        )
        assert _extract_write_targets(no_list_row) == [], (
            "the no-list shape names the logfile and nothing else, so blinding the "
            "rule must leave it naming nothing"
        )
        assert _extract_write_targets(write_row) == [f"{OUTSIDE}/a.zip"], (
            "with the logfile rule blinded only the archive should be named"
        )
        assert tiers(read_row)["workspace-write"] is True, (
            "with the logfile rule blinded the run is allowed while it really "
            "creates the logfile outside — the hole, restored"
        )
    finally:
        bash_tool._zip_logfile_targets = original


@needs_zip
def test_the_logfile_is_really_written_in_every_shape(tmp_path):
    """Executed: `-lf <path ending in .log>` creates exactly the file it names.

    The suffix matters to the *test* rather than to the rule: zip appends `.log` to a
    value that does not already end in it (measured), so naming a value with the
    suffix makes the file asserted here the same token the walk names.
    """
    operand = tmp_path / "in.txt"
    operand.write_text("hello\n")
    archive = tmp_path / "a.zip"
    subprocess.run(["zip", "-q", str(archive), str(operand)],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")

    # a read spelling still writes the logfile
    read_log = tmp_path / "read.log"
    result = subprocess.run(["zip", "-sf", "-lf", str(read_log), str(archive)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert read_log.exists(), (
        "`zip -sf -lf <log>` did not create the logfile, so the read short-circuit "
        "would be right after all"
    )
    assert _extract_write_targets(
        f"zip -sf -lf {read_log} {archive} {operand}"
    ) == [str(read_log)]

    # the exit-12 "Nothing to do!" case writes it too
    idle_log = tmp_path / "idle.log"
    result = subprocess.run(["zip", "-lf", str(idle_log), str(tmp_path / "none.zip")],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode != 0, "the no-list run unexpectedly succeeded"
    assert idle_log.exists(), "the no-list shape did not create the logfile"
    assert _extract_write_targets(
        f"zip -lf {idle_log} {tmp_path / 'none.zip'}"
    ) == [str(idle_log)]

    # and the ordinary shape writes both the archive and the logfile
    both_log = tmp_path / "both.log"
    both_archive = tmp_path / "both.zip"
    result = subprocess.run(["zip", "-lf", str(both_log), str(both_archive), str(operand)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert both_log.exists() and both_archive.exists(), (
        "the ordinary shape must write the archive and the logfile"
    )
    assert _extract_write_targets(
        f"zip -lf {both_log} {both_archive} {operand}"
    ) == [str(both_archive), str(both_log)]


# ── copy mode: the destination is an option's value that is a path (issue #1441) ────
#
# (row, command, targets) — the *source* operand is written outside every allowed root
# as well, so a rule that still named it (master's rule before this one) would be
# caught by the list as well as by the verdict. That is the shape the issue measures:
# a first operand which is only read, and a destination named by nothing.
COPY_MODE_FORMS = (
    ("--out spaced", "zip -U {out}/src.zip --out {out}/new.zip", ("{out}/new.zip",)),
    ("-O spaced", "zip -U {out}/src.zip -O {out}/new.zip", ("{out}/new.zip",)),
    ("--out= attached", "zip -U {out}/src.zip --out={out}/new.zip", ("{out}/new.zip",)),
    ("-O attached", "zip -U {out}/src.zip -O{out}/new.zip", ("{out}/new.zip",)),
    # `--out` implies copy mode on its own: measured, `zip src.zip --out new.zip` is
    # rc=0 and writes only `new.zip`, the same as with `-U`.
    ("without -U", "zip {out}/src.zip --out {out}/new.zip", ("{out}/new.zip",)),
    ("option before the operand", "zip -U --out {out}/new.zip {out}/src.zip", ("{out}/new.zip",)),
    # A mode that edits the archive edits the *copy* under `--out`: measured with `-d`
    # (the source byte-identical afterwards, member list unchanged) and with `-m` (the
    # member is still on disk, and zip warns "can't set method, move, recurse, or
    # comments with copy mode"), so neither adds an operand. `-d` is spelled *without*
    # `-U` on purpose: `zip -U -d ... --out ...` is rejected by zip with rc=16
    # ("specify just one action") having written nothing.
    ("with -d", "zip -d {out}/src.zip member.txt --out {out}/new.zip", ("{out}/new.zip",)),
    ("with -m", "zip -U {out}/src.zip --out {out}/new.zip -m", ("{out}/new.zip",)),
    ("with a member pattern", "zip -U {out}/src.zip member.txt --out {out}/new.zip",
     ("{out}/new.zip",)),
    ("with a logfile", "zip -U {out}/src.zip --out {out}/new.zip -lf {ws}/log",
     ("{out}/new.zip", "{ws}/log")),
)

# The rows that write nothing in copy mode, each measured: no operand at all (rc=9),
# an empty value (`--out=`, rc=0 and no file created anywhere), and a trailing `-O`
# with nothing after it (rc=16). Naming any of them would be the false block this walk
# treats as the worse direction — and an empty token is worse still, because
# `realpath("")` is the working directory.
COPY_MODE_WRITES_NOTHING = (
    ("no operand", "zip --out {out}/new.zip"),
    ("empty value", "zip -U {out}/src.zip --out="),
    ("trailing -O", "zip -U {out}/src.zip -O"),
)


@pytest.mark.parametrize("row,cmd,targets", rendered(COPY_MODE_FORMS),
                         ids=[row for row, _c, _t in COPY_MODE_FORMS])
def test_a_copy_mode_run_names_the_destination_and_not_the_source(row, cmd, targets):
    """Exactly the destination, in every measured spelling.

    The whole list is asserted: the source operand sits under the same placeholder, so
    a rule that named it instead (or as well) fails here even though something *was*
    named — which is what master's rule did.
    """
    assert _extract_write_targets(cmd) == list(targets), row


@pytest.mark.parametrize("row,cmd,targets", rendered(COPY_MODE_FORMS),
                         ids=[row for row, _c, _t in COPY_MODE_FORMS])
def test_the_destination_of_a_copy_run_is_refused_at_both_tiers(row, cmd, targets):
    """The destination is the write, so a destination outside every root is refused.

    Both tiers: `workspace-write` refuses writes outside the workspace, `read-only`
    refuses them anywhere. Before this rule the same command was ALLOW at
    `workspace-write` while really creating the archive there.
    """
    assert targets, row
    for tier, allowed in tiers(cmd).items():
        assert allowed is False, f"{row}: {tier} allowed a write to {OUTSIDE}"


def test_the_source_operand_of_a_copy_run_is_read_not_written():
    """The other half of the issue, as its own pair — the false block, and its removal.

    One command line, two verdicts that differ by where the *source* sits. The row the
    issue opens with is the second: a read outside the workspace used to be refused
    because the walk named it as the write, while the write it really makes (inside the
    workspace) was named by nothing.
    """
    writes_inside = f"zip -U {WORKSPACE}/src.zip --out {OUTSIDE}/new.zip"
    reads_outside = f"zip -U {OUTSIDE}/src.zip --out {WORKSPACE}/new.zip"

    assert _extract_write_targets(writes_inside) == [f"{OUTSIDE}/new.zip"]
    assert _extract_write_targets(reads_outside) == [f"{WORKSPACE}/new.zip"]
    assert tiers(writes_inside)["workspace-write"] is False, (
        "the archive really created outside every root must be refused"
    )
    assert tiers(reads_outside)["workspace-write"] is True, (
        "the source operand is only read, so a source outside the workspace is not a "
        "write to it — refusing this is the false block the issue names"
    )
    assert tiers(reads_outside)["read-only"] is False, (
        "read-only refuses the destination wherever it is"
    )


@pytest.mark.parametrize("row,cmd", COPY_MODE_WRITES_NOTHING,
                         ids=[row for row, _c in COPY_MODE_WRITES_NOTHING])
def test_the_copy_forms_that_write_nothing_name_nothing(row, cmd):
    """No target, and therefore no verdict — the direction this walk protects.

    `--out` with no operand really does exit 9 having written nothing (`Interrupted
    (aborting)`), and an empty value really does write nothing anywhere, so the
    "there is a source" question is what keeps both unnamed.
    """
    resolved = cmd.format(out=OUTSIDE, ws=WORKSPACE)
    assert _extract_write_targets(resolved) == [], row


def test_the_copy_rule_is_what_names_the_destination() -> None:
    """Blind the reader and the hole the issue measured must come back, both ways.

    This is the arm that makes the rows above claims rather than observations: with
    `_zip_out_values` returning nothing the walk falls back to the first-operand rule,
    so the destination goes unnamed and the run is ALLOW at `workspace-write` **while
    it really writes there** — the exact hole measured on the host at the time the
    issue was filed. The source, meanwhile, is named again and refused, which is the
    false block on the other side.
    """
    write_row = f"zip -U {WORKSPACE}/src.zip --out {OUTSIDE}/new.zip"
    read_row = f"zip -U {OUTSIDE}/src.zip --out {WORKSPACE}/new.zip"
    assert tiers(write_row)["workspace-write"] is False
    assert tiers(read_row)["workspace-write"] is True

    original = bash_tool._zip_out_values
    try:
        bash_tool._zip_out_values = lambda words: []
        assert _extract_write_targets(write_row) == [f"{WORKSPACE}/src.zip"], (
            "with the destination reader blinded the walk must name the source — that "
            "wrong name is what the issue reports"
        )
        assert tiers(write_row)["workspace-write"] is True, (
            "with the destination reader blinded the run is allowed while it really "
            "creates the archive outside every root — the hole, restored"
        )
        assert _extract_write_targets(read_row) != [f"{WORKSPACE}/new.zip"], (
            "the destination must stop being named once its reader is blinded, "
            "otherwise this arm is testing the fallback rather than the reader"
        )
    finally:
        bash_tool._zip_out_values = original


def test_the_read_gate_beats_copy_mode() -> None:
    """`-sf` still writes nothing, `--out` or not — measured, and it is a second claim.

    Copy mode looks like a write and the gate looks like a read; the measurement says
    the gate already returned before the copy was made (`zip -sf -U src.zip --out
    o.zip` printed its listing and created no file), so naming the destination there
    would be a block on a run that writes nothing. Ordered, not merged: if the copy
    branch were read first this row would be refused, which is the false block.
    """
    row = f"zip -sf -U {OUTSIDE}/src.zip --out {OUTSIDE}/new.zip"
    assert _extract_write_targets(row) == []
    assert tiers(row)["workspace-write"] is True


@needs_zip
def test_copy_mode_really_writes_the_new_archive_and_leaves_the_source_alone(tmp_path):
    """Executed, because every claim above is a claim about what zip does.

    `tmp_path` is a directory this test creates, so no host path is involved. Every
    command runs with `cwd=tmp_path` and **relative** names, because zip stores the
    member name it was given: built from an absolute path the member is the whole path
    (with the leading slash stripped), and then no relative pattern can select it —
    measured while writing this test, `zip -d <abs>/src.zip in.txt --out …` exits 12
    "Nothing to do!" for exactly that reason.

    The source is judged by `(mtime_ns, size)` **and** its member list, because "the
    source was not rewritten" is the load-bearing half: a run that edited it in place
    would make the destination rule the wrong rule.
    """
    (tmp_path / "in.txt").write_text("hello\n")
    source = tmp_path / "src.zip"

    def run(*argv):
        return subprocess.run(list(argv), cwd=tmp_path, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    result = run("zip", "-q", "src.zip", "in.txt")
    assert result.returncode == 0, result.stderr
    before = _archive_state(source)
    assert before is not None, "the source archive was not built"
    baseline_members = run("zip", "-sf", "src.zip").stdout
    assert "in.txt" in baseline_members, (
        "the member is not stored under the name this test selects it by"
    )

    # 1. the plain copy: the destination appears, the source is untouched
    new = tmp_path / "new.zip"
    result = run("zip", "-U", "src.zip", "--out", "new.zip")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert new.exists(), "copy mode did not create the archive it was pointed at"
    assert _archive_state(source) == before, (
        "copy mode rewrote the source archive, so the destination is not the only write"
    )
    assert _extract_write_targets(f"zip -U {source} --out {new}") == [str(new)]

    # 2. `-d` under `--out` edits the copy: the source keeps its member, byte for byte
    deleted = tmp_path / "deleted.zip"
    result = run("zip", "-d", "src.zip", "in.txt", "--out", "deleted.zip")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _archive_state(source) == before, "`-d` with `--out` edited the source"
    assert run("zip", "-sf", "src.zip").stdout == baseline_members, (
        "`-d` with `--out` changed the source's member list, so the walk cannot treat "
        "the operand as a read"
    )
    assert _extract_write_targets(f"zip -d {source} in.txt --out {deleted}") == [str(deleted)]

    # 3. `-m` under `--out` is inert: the member is still on disk afterwards
    moved = tmp_path / "moved.zip"
    result = run("zip", "-U", "src.zip", "--out", "moved.zip", "-m")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (tmp_path / "in.txt").exists(), (
        "`-m` removed the member under `--out`, so copy mode is not inert to it — the "
        "rule would have to name every operand again for that spelling"
    )
    assert _extract_write_targets(f"zip -U {source} --out {moved} -m") == [str(moved)]

    # 4. the read gate wins over the copy, executed: the listing prints, nothing is made
    gated = tmp_path / "gated.zip"
    result = run("zip", "-sf", "-U", "src.zip", "--out", "gated.zip")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not gated.exists(), (
        "`zip -sf -U src.zip --out out.zip` created the archive, so the read gate does "
        "not beat copy mode and this rule's ordering is wrong"
    )
    assert _extract_write_targets(f"zip -sf -U {source} --out {gated}") == []

    # 5. `-U` with an action flag is the contradictory line the docstring names as a
    #    limit: zip rejects it and writes nothing, so the destination is an over-block
    contradictory = tmp_path / "contradictory.zip"
    result = run("zip", "-U", "-d", "src.zip", "in.txt", "--out", "contradictory.zip")
    assert result.returncode == 16, (result.stdout, result.stderr)
    assert not contradictory.exists(), (
        "zip accepted `-U -d … --out …`, so the limit this rule records is wrong"
    )
    assert _extract_write_targets(
        f"zip -U -d {source} in.txt --out {contradictory}"
    ) == [str(contradictory)], (
        "the over-block on the contradictory line is a named limit, so it is pinned "
        "here rather than left to be rediscovered"
    )
