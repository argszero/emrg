"""`csplit` writes a family under a prefix, so the walk names it in every spelling.

`csplit` was pinned as a measured hole in
`test_bash_tool_option_destinations.py`'s `UNCOVERED_WRITERS` table. It left through
the door that table is actually about — a destination named by an **option** — which
is the opposite of the departures before it: `rsync` and `split` write to operands, so
their reason for being pinned was that the table could not describe them at all, while
`csplit`'s prefix is `-f`'s value and the table could describe it. What needed a rule
of its own is the other half: a run with no `-f` still writes, on the **default**
prefix `xx`, in the working directory, and no option spells that.

Measured on master `910a307c` with the real predicate, the prefix outside every
allowed root, at both tiers: `-f` leading, `-f` attached, `-f` trailing, no `-f` at
all, an in-workspace prefix and the `--prefix` spelling BSD does not accept each
reported an **empty target list**, and an empty list is allowed by construction — the
loop that judges targets never runs. `cp` on the same two paths was refused in the
same geometry, which is what makes this a hole rather than an opinion.

Ground truth for what csplit really does, taken in a scratch directory on this host
(BSD `csplit`, usage line `csplit [-ks] [-f prefix] [-n number] file args ...`,
2026-09-19) with the directory read back off disk afterwards:

* `csplit -f pfx in.txt 4 8` created `pfx00 pfx01 pfx02` beside `in.txt`, so the
  prefix option really is where the writes are placed;
* `csplit in.txt 4` created `xx00 xx01` — the default prefix, in the cwd, spelled by
  no option at all;
* `csplit -n 3 -f n3 in.txt 4` created `n3000 n3001`, so the digit count is a flag
  (`-n`) and not part of the prefix;
* `csplit -f - in.txt 3` created `-00 -01`, the one spelling whose prefix this rule
  does not read (below); `csplit -kf cl in.txt 3` also created `cl00 cl01` and was a
  second such spelling until the cluster reader learned this verb's letters — a
  cluster's value belongs to the first letter that takes one, so `-kf` is `-f`'s;
* `csplit` with no operand, `csplit -f pfxonly`, `csplit --help`, `csplit --version`
  and `csplit missing.txt 3` each created **nothing**.

This file is separate from `test_bash_tool_sandbox.py` for the reason the rsync and
split files are: it covers one family, and a self-contained fixture set cannot
disagree with a table it does not share.
"""

import subprocess
import sys

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import _check_sandbox, _extract_write_targets

# Outside every allowed root (workspace, OS temp root, the evolution data dir) and used
# only as an argument to the pure predicate — never executed and never opened. The
# protected-daemon-file arm below passes a host path for the same reason: `_check_sandbox`
# and `_protected_paths` only `realpath` it.
OUTSIDE = "/outside/emrg"
WORKSPACE = "/workspace"


def test_the_predicate_is_the_one_the_tiers_read():
    """The two functions this file judges are the ones the tool layer calls.

    A test that reached a private re-reading would keep passing while the shipped
    guard changed, which is the failure this whole class of test exists to prevent.
    """
    allowed, _reason, enforcement = _check_sandbox(
        "csplit in.txt 4", "workspace-write", WORKSPACE
    )
    assert allowed is True
    assert enforcement in ("partial", "full")


# ── writing forms: the prefix must be named ─────────────────────────────────────────
#
# (row, command) — the input is deliberately *inside* the workspace so a rule that
# named the wrong operands would be caught by the target it reports rather than by the
# verdict it gives.
WRITING_FORMS = (
    ("prefix, spaced", f"csplit -f {{out}} {WORKSPACE}/in.txt 4 8"),
    ("prefix, attached to its letter", f"csplit -f{{out}} {WORKSPACE}/in.txt 4 8"),
    ("long, attached value", f"csplit --prefix={{out}} {WORKSPACE}/in.txt 4 8"),
    ("long, spaced value", f"csplit --prefix {{out}} {WORKSPACE}/in.txt 4 8"),
    ("digits flag before the prefix", f"csplit -n 3 -f {{out}} {WORKSPACE}/in.txt 4"),
    ("quiet and keep", f"csplit -ks -f {{out}} {WORKSPACE}/in.txt 4 8"),
    ("one line number", f"csplit -f {{out}} {WORKSPACE}/in.txt 4"),
    ("a pattern operand", f"csplit -f {{out}} {WORKSPACE}/in.txt /two/"),
    ("after --", f"csplit -f {{out}} -- {WORKSPACE}/in.txt 4"),
    ("trailing prefix (GNU order)", f"csplit {WORKSPACE}/in.txt /two/ -f {{out}}"),
)


@pytest.mark.parametrize("row,cmd", WRITING_FORMS, ids=[row for row, _ in WRITING_FORMS])
def test_every_writing_form_names_the_prefix(row, cmd):
    """The prefix option's value is the family's common prefix, and it is reported."""
    out = f"{OUTSIDE}/pre"
    assert _extract_write_targets(cmd.format(out=out)) == [out], row


@pytest.mark.parametrize("row,cmd", WRITING_FORMS, ids=[row for row, _ in WRITING_FORMS])
def test_a_named_prefix_is_refused_at_both_tiers(row, cmd):
    """A prefix outside every allowed root is refused whether or not writes are on.

    Both tiers, because the hole was in both and the untested tier is the one a cycle
    actually runs in: `workspace-write` still refuses writes outside the workspace, and
    `read-only` refuses them anywhere.
    """
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(
            cmd.format(out=f"{OUTSIDE}/pre"), tier, WORKSPACE
        )
        assert allowed is False, f"{row}: {tier} allowed a write to {OUTSIDE}"
        assert reason, f"{row}: {tier} refused without a reason"


# ── the default prefix: the half no option spells ───────────────────────────────────
#
# (row, command) — no `-f` anywhere, so the family lands on `xx…` in the cwd. The
# walk cannot know where the shell's cwd is, and a *relative* target is the reading
# that says "in the workspace unless the command moved out of it", which is exactly
# what these runs do.
DEFAULT_PREFIX_FORMS = (
    ("line number", f"csplit {WORKSPACE}/in.txt 4"),
    ("pattern", f"csplit {WORKSPACE}/in.txt /two/"),
    ("quiet and keep", f"csplit -ks {WORKSPACE}/in.txt 4 8"),
    ("after --", f"csplit -- {WORKSPACE}/in.txt 4"),
)


@pytest.mark.parametrize(
    "row,cmd", DEFAULT_PREFIX_FORMS, ids=[row for row, _ in DEFAULT_PREFIX_FORMS]
)
def test_a_run_with_no_prefix_option_names_the_default(row, cmd):
    """`xx` is the documented default, and it is named rather than left unnamed.

    This is the assertion that closes the hole for the shortest spelling of all: the
    default is a constant (`xx`), not a derivation — the digits that follow it are
    what `-n` moves, and the prefix is what the tier verdict is read from.
    """
    assert _extract_write_targets(cmd) == ["xx"], row


@pytest.mark.parametrize(
    "row,cmd", DEFAULT_PREFIX_FORMS, ids=[row for row, _ in DEFAULT_PREFIX_FORMS]
)
def test_the_default_prefix_follows_the_tiers(row, cmd):
    """A csplit run always writes, so `read-only` refuses it and writes-on allows it.

    The two verdicts are the point: the first is the one the hole was hiding (a write
    allowed under a read-only tier), and the second is the one that says this rule
    places a write instead of refusing how the tool is normally used.
    """
    allowed, reason, _ = _check_sandbox(cmd, "read-only", WORKSPACE)
    assert allowed is False, f"{row}: read-only allowed a csplit write ({reason})"
    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", WORKSPACE)
    assert allowed is True, f"{row}: {reason}"


# ── forms that write nothing: naming them would be a false block ────────────────────
#
# Measured above: none of these creates a file. csplit needs its file operand before it
# can write anything, which is why the rule asks for one rather than reading the flags
# alone — every other verb in this walk names nothing when its operand list is empty.
NAMING_NOTHING = (
    ("no operand at all", "csplit"),
    ("help", "csplit --help"),
    ("version", "csplit --version"),
    ("prefix but no file", f"csplit -f {OUTSIDE}/pre"),
    ("flags but no file", "csplit -n 3 -ks"),
)


@pytest.mark.parametrize("row,cmd", NAMING_NOTHING, ids=[row for row, _ in NAMING_NOTHING])
def test_a_run_with_no_operand_is_named_nowhere(row, cmd):
    """Without a file operand csplit writes nothing, so nothing may be named."""
    assert _extract_write_targets(cmd) == [], row


@pytest.mark.parametrize("row,cmd", NAMING_NOTHING, ids=[row for row, _ in NAMING_NOTHING])
def test_those_forms_stay_allowed(row, cmd):
    """The false block is the direction this walk treats as worse than the hole."""
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, WORKSPACE)
        assert allowed is True, f"{row}: {tier} refused a no-write run ({reason})"


def test_the_gate_is_not_blind():
    """The instrument's own control: the same command with and without the prefix.

    Without this, a rule that named nothing at all — or a fixture that never reached
    the rule — would leave every test above green while the defect was fully open. The
    pair differs by the presence of the prefix option and by nothing else, so the
    control asserts the *reported target*, not just the verdict.
    """
    with_prefix = f"csplit -f {OUTSIDE}/pre {WORKSPACE}/in.txt 4"
    without_prefix = f"csplit {WORKSPACE}/in.txt 4"
    assert _extract_write_targets(with_prefix) == [f"{OUTSIDE}/pre"]
    assert _extract_write_targets(without_prefix) == ["xx"]
    # ...and neither operand is ever named, which is the assertion that would fail if a
    # reading without an option table mistook a *pattern* for a path: `/two/` looks
    # absolute and is a regular expression, and naming it would point the block at a
    # path that does not exist.
    assert f"{WORKSPACE}/in.txt" not in _extract_write_targets(with_prefix)
    assert "/two/" not in _extract_write_targets(
        f"csplit -f {OUTSIDE}/pre {WORKSPACE}/in.txt /two/"
    )


def test_a_prefix_inside_the_workspace_is_allowed_where_writes_are_on():
    """The rule places the write; it does not refuse how the tool is normally used.

    Named-and-inside is the ordinary case, and the two tiers read it as they read any
    other in-workspace write: `read-only` still refuses it on its own account (that tier
    refuses every named target but `/dev/null`), and `workspace-write` allows it.
    """
    cmd = f"csplit -f {WORKSPACE}/pre {WORKSPACE}/in.txt 4"
    assert _extract_write_targets(cmd) == [f"{WORKSPACE}/pre"]
    allowed, _reason, _ = _check_sandbox(cmd, "read-only", WORKSPACE)
    assert allowed is False
    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", WORKSPACE)
    assert allowed is True, reason


def test_the_protected_daemon_file_is_refused_at_both_tiers():
    """The strongest case: the prefix sits inside the store the sandbox protects.

    The path is an argument to the pure predicate — `_check_sandbox` `realpath`s it and
    opens nothing — so the host's real store is never touched by this test.

    The named path is the *prefix*, so the two messages are read as they are: both tiers
    refuse the write, and `workspace-write` refuses it *because* the path is a protected
    daemon file, which is the reading that says this rule reached the protected-path
    check rather than a generic one. The chunks themselves would land beside that
    prefix, inside the same protected directory, which is why refusing on the prefix is
    the right boundary and not merely a convenient one.
    """
    protected = "~/.emrg/rants.jsonl"
    allowed, ro_reason, _ = _check_sandbox(
        f"csplit -f {protected} /etc/hosts 4", "read-only", WORKSPACE
    )
    assert allowed is False, "read-only allowed a write into the protected store"
    assert protected in (ro_reason or ""), ro_reason

    allowed, ww_reason, _ = _check_sandbox(
        f"csplit -f {protected} /etc/hosts 4", "workspace-write", WORKSPACE
    )
    assert allowed is False, "workspace-write allowed a write into the protected store"
    assert "protected" in (ww_reason or ""), ww_reason


# ── the named limits, pinned so a later reader does not re-derive them ──────────────
def test_the_cluster_spelling_is_read_through_the_verbs_own_letters():
    """`-kf PREFIX` carries the prefix behind another letter, and it is read.

    Telling `-kf cl` apart from `-nf cl` needs the verb's own grammar — `-n` takes a
    value, so in one of them the next token is a digit count and in the other it is the
    prefix — and the walk now has that grammar for this verb
    (`_OPTION_DESTINATION_VALUE_TAKING`, derived from `_CSPLIT_OPTIONS_WITH_VALUE`), so
    the cluster is split the same way the operand walk splits it. This row used to be
    pinned as a residual that named the **default** prefix instead; the ground truth
    below is what says which of the two readings is right: BSD really does create
    `cl00 cl01` for `csplit -kf cl in.txt 3`, so naming `xx` was a name the run never
    writes, not merely a missing one.
    """
    cmd = f"csplit -kf {OUTSIDE}/pre {WORKSPACE}/in.txt 4"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/pre"]
    assert _check_sandbox(cmd, "workspace-write", WORKSPACE)[0] is False
    # The neighbours, both directions: `-n` takes a value, so in `-nf cl` the `f` is
    # *that* value and the token names no prefix at all — the walk answers with the
    # default it documents, and the real run writes nothing (measured: rc=1, `csplit: f:
    # bad suffix length`). The spaced spelling still names its prefix as before.
    assert _extract_write_targets(f"csplit -nf {OUTSIDE}/pre {WORKSPACE}/in.txt") == ["xx"]
    assert _extract_write_targets(f"csplit -f {OUTSIDE}/pre {WORKSPACE}/in.txt") == [
        f"{OUTSIDE}/pre"
    ]


def test_the_cluster_row_needs_this_verbs_letters_row():
    """Empty this verb's letters and the cluster falls back to the default prefix.

    The arm is aimed at the one thing the cluster row has that the spaced row does not,
    so the spaced form has to survive it: `-f <prefix>` is read by the destination table
    itself, while `-kf <prefix>` is read only because this verb's value-taking letters
    were supplied to the cluster reader. Emptied, the walk answers what master answered
    — the default `xx`, a name the run never writes — and that difference is the whole
    claim the row makes. A target-list comparison alone would not say it either way; the
    value that changes is which name comes out.
    """
    original = bash_tool._OPTION_DESTINATION_VALUE_TAKING["csplit"]
    bash_tool._OPTION_DESTINATION_VALUE_TAKING["csplit"] = frozenset()
    try:
        assert _extract_write_targets(f"csplit -kf {OUTSIDE}/pre {WORKSPACE}/in.txt") == [
            "xx"
        ]
        # ...while the spaced spelling never used the letters at all.
        assert _extract_write_targets(f"csplit -f {OUTSIDE}/pre {WORKSPACE}/in.txt") == [
            f"{OUTSIDE}/pre"
        ]
    finally:
        bash_tool._OPTION_DESTINATION_VALUE_TAKING["csplit"] = original


def test_a_dash_prefix_is_read_as_the_default():
    """`-f -` is a prefix named `-`, and the walk names `xx` for it instead.

    `_option_destination_values` skips a value of exactly `-`, because for the verbs it
    was written for (`curl -o -`, `sort -o -`) that means **stdout**. For csplit it is
    an ordinary prefix and the run really creates `-00`/`-01` (executed below). The
    substitution is harmless in the only place it is read: both names are relative, so
    both tiers give the same verdict — what changes is the name in a message.
    """
    assert _extract_write_targets(f"csplit -f - {WORKSPACE}/in.txt 3") == ["xx"]


def test_a_trailing_prefix_is_read_though_this_host_writes_nothing_for_it():
    """The walk reads `-f` wherever it stands; BSD's getopt stops before it.

    Permuting options past operands is GNU behaviour, and the CI platform's `csplit` is
    the GNU one — no longer an assumption: the runner's own run of this file created the
    `tr…` family for exactly this command while this host created nothing. So the
    spelling is read, which is right where the option is permuted and only strict here:
    not reading it would miss the write on the platform that counts. This host's BSD
    `getopt` stops at the file operand, reads `-f` as a *pattern*, fails with
    `unrecognised pattern` and leaves nothing behind (executed below).
    """
    cmd = f"csplit {WORKSPACE}/in.txt 4 -f {OUTSIDE}/pre"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/pre"]


def test_a_file_operand_that_does_not_exist_is_still_placed():
    """This walk never `stat`s anything, and a missing file is not an exemption.

    `csplit missing.txt 3` writes nothing (executed below), and it is still refused
    under `read-only` for the same reason `rm` of a missing file is: placing a target is
    a fact about the command's *text*, not about the disk. The alternative — asking the
    filesystem — would make the same command line mean two different things on two
    machines, which is the opposite of what a guard is for.
    """
    cmd = f"csplit {WORKSPACE}/missing.txt 3"
    assert _extract_write_targets(cmd) == ["xx"]
    assert _check_sandbox(cmd, "read-only", WORKSPACE)[0] is False
    assert _check_sandbox(cmd, "workspace-write", WORKSPACE)[0] is True


# ── ground truth, executed: the derivation the rule is built on ─────────────────────
@pytest.mark.skipif(sys.platform == "win32", reason="no csplit on Windows CI")
def test_csplit_really_writes_under_the_named_prefix(tmp_path):
    """Drive the tool itself, so the rule rests on what it does rather than on a manual.

    `tmp_path` is a directory the test creates, so nothing here can reach a host path;
    the chunks are read back off disk instead of assumed. Skipped on Windows, whose CI
    leg has no `csplit` — the *product* claim above is platform-free, and this arm is
    the one that needs the tool.
    """
    source = tmp_path / "in.txt"
    source.write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n")
    prefix = tmp_path / "pfx"
    result = subprocess.run(
        ["csplit", "-f", str(prefix), str(source), "4", "8"],
        cwd=str(tmp_path), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    created = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("pfx"))
    assert created, "the named prefix produced nothing, so the rule names a read"
    assert created[0] == "pfx00", created
    # ...and the rule names exactly the prefix those files sit under.
    assert _extract_write_targets(
        f"csplit -f {prefix} {source} 4 8"
    ) == [str(prefix)]


@pytest.mark.skipif(sys.platform == "win32", reason="no csplit on Windows CI")
def test_no_prefix_option_really_writes_xx_in_the_cwd(tmp_path):
    """The default-prefix half, measured: `xx00` appears and the input survives.

    The second half is the assertion that matters — the *source* must survive, because a
    rule that named it would be refusing a read.
    """
    source = tmp_path / "in.txt"
    source.write_text("l1\nl2\nl3\nl4\nl5\nl6\n")
    result = subprocess.run(
        ["csplit", str(source), "4"], cwd=str(tmp_path),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert source.read_text() == "l1\nl2\nl3\nl4\nl5\nl6\n"
    assert (tmp_path / "xx00").exists()
    assert _extract_write_targets(f"csplit {source} 4") == ["xx"]


@pytest.mark.skipif(sys.platform == "win32", reason="no csplit on Windows CI")
def test_the_cluster_spelling_really_writes_where_the_walk_says(tmp_path):
    """The cluster this rule now reads is driven, and its family read off disk.

    A reading is only as good as the run behind it: BSD really does create `cl00 cl01`
    for `csplit -kf cl in.txt 4`, which is why `-kf` must be read and why naming the
    default `xx` was a name the run never writes. The dash spelling's residual is driven
    in the same directory (below).
    """
    source = tmp_path / "in.txt"
    source.write_text("l1\nl2\nl3\nl4\nl5\nl6\n")

    result = subprocess.run(
        ["csplit", "-kf", "cl", str(source), "4"], cwd=str(tmp_path),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("cl")) == [
        "cl00", "cl01",
    ]
    cmd = f"csplit -kf {tmp_path}/pre {source} 4"
    assert _extract_write_targets(cmd) == [f"{tmp_path}/pre"]

    dash = subprocess.run(
        ["csplit", "-f", "-", str(source), "4"], cwd=str(tmp_path),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert dash.returncode == 0, dash.stderr
    assert (tmp_path / "-00").exists()
    assert _extract_write_targets(f"csplit -f - {source} 4") == ["xx"]


@pytest.mark.skipif(sys.platform == "win32", reason="no csplit on Windows CI")
def test_the_forms_that_name_nothing_really_write_nothing(tmp_path):
    """The false-block half, driven: a run with no operand leaves the directory alone.

    This is the arm that keeps `csplit --help` allowed for a reason rather than by
    accident — the rule names the default prefix only when there is an operand, and the
    claim behind that is that csplit needs one before it can write.
    """
    source = tmp_path / "in.txt"
    source.write_text("l1\nl2\nl3\nl4\n")
    before = sorted(p.name for p in tmp_path.iterdir())

    for argv in (["csplit"], ["csplit", "--help"], ["csplit", "-f", "pfxonly"]):
        subprocess.run(
            argv, cwd=str(tmp_path), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        assert sorted(p.name for p in tmp_path.iterdir()) == before, argv

    # ...and a file operand that does not exist is the same story.
    subprocess.run(
        ["csplit", "missing.txt", "3"], cwd=str(tmp_path),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.skipif(sys.platform == "win32", reason="no csplit on Windows CI")
def test_a_trailing_prefix_is_where_getopt_permutes_and_only_there(tmp_path):
    """One command line, two implementations, two measured truths — both pinned.

    GNU's `getopt` permutes options past operands, so `csplit in.txt 4 -f tr` writes the
    `tr…` family; BSD's `getopt` stops at the first operand, reads `-f` as a *pattern*,
    fails with `unrecognised pattern` and leaves nothing. This arm is what makes the
    rule's choice honest rather than merely convenient: reading the trailing spelling is
    right on the platform that permutes it, and the price paid on the other is refusing
    a run that was going to fail anyway.

    The branch is on the platform's getopt, not on a wish — measured on this host's BSD
    (`unrecognised pattern`, nothing created), and measured on the CI runner's GNU
    coreutils by this arm failing there: 3814 passed with only *this* assertion failing,
    the tool printing two 9-byte sizes for the prefix `tr`, i.e. `tr00` and `tr01` under
    the default `-n 2`. A single assertion cannot hold in both, which is exactly the kind
    of claim a test must not make on one platform's evidence.
    """
    source = tmp_path / "in.txt"
    source.write_text("l1\nl2\nl3\nl4\nl5\nl6\n")
    result = subprocess.run(
        ["csplit", str(source), "4", "-f", "tr"], cwd=str(tmp_path),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    written = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("tr"))
    if sys.platform == "darwin":
        assert "unrecognised pattern" in (result.stderr + result.stdout)
        assert written == []
    else:
        assert written == ["tr00", "tr01"], written
    # Either way the walk names the prefix the option gives, wherever it stands — it
    # cannot know which getopt the platform ships, and the writing half is the one worth
    # naming.
    assert _extract_write_targets(
        f"csplit {source} 4 -f {tmp_path}/pre"
    ) == [f"{tmp_path}/pre"]
