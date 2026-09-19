"""`split` writes a family of derived paths, so the walk names its last operand.

`split` was pinned as a measured hole in
`test_bash_tool_option_destinations.py`'s `UNCOVERED_WRITERS` table. It left that
table the way `rsync` did, and for the operand-shaped half of the same reason: the
paths it writes are **derived from an operand** rather than named by an option, so
covering it is a rule about which operand is written, not the per-verb flag grammar
that table is about.

Measured on master `e24ff6ea` with the real predicate, the target outside every
allowed root, at both tiers: `split -b 3 <outside>/in <outside>/pre` reported an
**empty target list**, and an empty list is allowed by construction — the loop that
judges targets never runs. `cp` on the same two paths was refused in the same
geometry, which is what makes this a hole rather than an opinion.

Ground truth for what split really does, taken in a scratch directory on this host
(BSD `split`, usage line `split [-cd] [-l line_count] [-a suffix_length] [file
[prefix]]`, 2026-09-19) with the directory read back off disk afterwards:

* `split -b 3 in.txt pfx` created `pfxaa pfxab pfxac pfxad` beside `in.txt`, so the
  last operand really is where the writes are placed;
* `split -a 2 -b 3 in.txt pfx2` created `pfx2aa …`, so the suffix length is a flag
  (`-a`) and not part of the operand;
* `split -b 3 in.txt` (one operand) created `xaa xab xac xad` — the **default**
  prefix, in the cwd, spelled by no operand at all.

That third row is why the rule names nothing when there is one operand: only the
*input* is left to name there, and naming an input is the false block this walk
treats as worse than a hole.

This file is separate from `test_bash_tool_sandbox.py` for the reason the two files
above are: it covers one family, and a self-contained fixture set cannot disagree
with a table it does not share.
"""

import subprocess
import sys

import pytest

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
        "split -b 3 in.txt pre", "workspace-write", WORKSPACE
    )
    assert allowed is True
    assert enforcement in ("partial", "full")


# ── writing forms: the prefix operand must be named ─────────────────────────────────
#
# (row, command) — the input is deliberately *inside* the workspace so a rule that
# named the wrong operand would be caught by the target it reports rather than by the
# verdict it gives.
WRITING_FORMS = (
    ("plain", f"split -b 3 {WORKSPACE}/in.txt {{out}}"),
    ("suffix length, spaced", f"split -b 3 -a 2 {WORKSPACE}/in.txt {{out}}"),
    ("value attached to its letter", f"split -b3 {WORKSPACE}/in.txt {{out}}"),
    ("long, spaced value", f"split --bytes 3 {WORKSPACE}/in.txt {{out}}"),
    ("long, attached value", f"split --bytes=3 {WORKSPACE}/in.txt {{out}}"),
    ("line count", f"split -l 100 {WORKSPACE}/in.txt {{out}}"),
    ("chunk count", f"split -n 4 {WORKSPACE}/in.txt {{out}}"),
    ("after --", f"split -b 3 -- {WORKSPACE}/in.txt {{out}}"),
    ("stdin input", f"split -b 3 - {{out}}"),
)


@pytest.mark.parametrize("row,cmd", WRITING_FORMS, ids=[row for row, _ in WRITING_FORMS])
def test_every_writing_form_names_the_prefix(row, cmd):
    """The last operand is the prefix, and the walk must report exactly it."""
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


def test_the_protected_daemon_file_is_refused_at_both_tiers():
    """The strongest case: the prefix sits inside the store the sandbox protects.

    The path is an argument to the pure predicate — `_check_sandbox` `realpath`s it and
    opens nothing — so the host's real store is never touched by this test.

    The named path is the *prefix*, so what the two messages say is read as it is: both
    tiers refuse the write, and `workspace-write` refuses it *because* the path is a
    protected daemon file, which is the reading that says this rule reached the
    protected-path check rather than a generic one. The chunks themselves would land
    beside that prefix, inside the same protected directory, which is why refusing on
    the prefix is the right boundary and not merely a convenient one.
    """
    protected = "~/.emrg/rants.jsonl"
    allowed, ro_reason, _ = _check_sandbox(f"split -b 3 /etc/hosts {protected}",
                                           "read-only", WORKSPACE)
    assert allowed is False, "read-only allowed a write into the protected store"
    assert protected in (ro_reason or ""), ro_reason

    allowed, ww_reason, _ = _check_sandbox(f"split -b 3 /etc/hosts {protected}",
                                           "workspace-write", WORKSPACE)
    assert allowed is False, "workspace-write allowed a write into the protected store"
    assert "protected" in (ww_reason or ""), ww_reason


def test_a_prefix_inside_the_workspace_is_allowed_where_writes_are_on():
    """The rule places the write; it does not refuse how the tool is normally used.

    Named-and-inside is the ordinary case, and the two tiers read it as they read any
    other in-workspace write: `read-only` still refuses it on its own account (that tier
    refuses every named target but `/dev/null`), and `workspace-write` allows it.
    """
    cmd = f"split -b 3 {WORKSPACE}/in.txt {WORKSPACE}/pre"
    assert _extract_write_targets(cmd) == [f"{WORKSPACE}/pre"]
    allowed, _reason, _ = _check_sandbox(cmd, "read-only", WORKSPACE)
    assert allowed is False
    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", WORKSPACE)
    assert allowed is True, reason


# ── forms that name nothing: naming the input would be a false block ────────────────
NAMING_NOTHING = (
    # The single-operand row is the ground truth's third bullet: the chunks land on the
    # default prefix in the cwd, so the only thing left to name is the file being read.
    ("one operand, inside", f"split -b 3 {WORKSPACE}/in.txt"),
    ("one operand, outside", f"split -b 3 {OUTSIDE}/in.txt"),
    ("no operand at all", "split"),
    ("flags but no operand", "split -a 2"),
    ("stdin, default prefix", "split -b 3 -"),
    ("help", "split --help"),
)


@pytest.mark.parametrize("row,cmd", NAMING_NOTHING, ids=[row for row, _ in NAMING_NOTHING])
def test_no_operand_is_written_so_nothing_is_named(row, cmd):
    """Each of these writes only under the default prefix, which no operand spells."""
    assert _extract_write_targets(cmd) == [], row


@pytest.mark.parametrize("row,cmd", NAMING_NOTHING, ids=[row for row, _ in NAMING_NOTHING])
def test_those_forms_stay_allowed(row, cmd):
    """The false block is the direction this walk treats as worse than the hole."""
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, WORKSPACE)
        assert allowed is True, f"{row}: {tier} refused a read ({reason})"


def test_the_gate_is_not_blind():
    """The instrument's own control: the same command with and without the operand.

    Without this, a rule that named nothing at all — or a fixture that never reached
    the rule — would leave every test above green while the defect was fully open. The
    pair differs by the presence of the prefix operand and by nothing else, and the
    single-operand form is exactly what a blinded rule would have named instead: the
    input. So the control asserts the *reported target*, not just the verdict.
    """
    with_prefix = f"split -b 3 {WORKSPACE}/in.txt {OUTSIDE}/pre"
    without_prefix = f"split -b 3 {WORKSPACE}/in.txt"
    assert _extract_write_targets(with_prefix) == [f"{OUTSIDE}/pre"]
    assert _extract_write_targets(without_prefix) == []
    # ...and the input is named by neither, which is the assertion that would fail if a
    # last-operand reading without the option table mistook the size for an operand.
    assert f"{WORKSPACE}/in.txt" not in _extract_write_targets(with_prefix)
    assert _extract_write_targets(f"split -b 3 {OUTSIDE}/in.txt") == []


# ── the named limits, pinned so a later reader does not re-derive them ──────────────
def test_the_default_prefix_is_the_documented_residual():
    """One operand writes `xaa…` in the cwd, and that name is deliberately unnamed.

    `split -b 3 in.txt` really does write — the ground truth's third bullet — and the
    path it writes is fully determined (`xaa`, `xab`, …) yet spelled by no operand. It
    is left unnamed rather than derived, because deriving it means modelling the suffix
    alphabet and the count, and because naming the *input* instead would refuse a read.
    Pinned here so the coverage claim in `bash_tool.py` stays as wide as the rule.
    """
    assert _extract_write_targets(f"split -b 3 {WORKSPACE}/in.txt") == []
    assert _check_sandbox(
        f"split -b 3 {WORKSPACE}/in.txt", "workspace-write", WORKSPACE
    )[0] is True


def test_the_filter_option_is_the_documented_residual():
    """`--filter=COMMAND` hands the chunks to a command instead of writing them.

    That command may write anywhere this walk is not looking, and following it would
    mean interpreting another command's arguments. The prefix operand is still named,
    so the invocation is judged by where its own chunks would have landed.
    """
    cmd = f"split -b 3 --filter=cat {WORKSPACE}/in.txt {OUTSIDE}/pre"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/pre"]


# ── ground truth, executed: the derivation the rule is built on ─────────────────────
@pytest.mark.skipif(sys.platform == "win32", reason="no split on Windows CI")
def test_split_really_writes_under_the_named_prefix(tmp_path):
    """Drive the tool itself, so the rule rests on what it does rather than on a manual.

    `tmp_path` is a directory the test creates, so nothing here can reach a host path;
    the chunks are read back off disk instead of assumed. Skipped on Windows, whose CI
    leg has no `split` — the *product* claim above is platform-free, and this arm is
    the one that needs the tool.
    """
    source = tmp_path / "in.txt"
    source.write_text("abcdefghij")
    prefix = tmp_path / "pre"
    result = subprocess.run(
        ["split", "-b", "3", str(source), str(prefix)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    created = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("pre"))
    assert created, "the named prefix produced nothing, so the rule names a read"
    assert created[0] == "preaa", created
    # ...and the rule names exactly the prefix those files sit under.
    assert _extract_write_targets(
        f"split -b 3 {source} {prefix}"
    ) == [str(prefix)]


@pytest.mark.skipif(sys.platform == "win32", reason="no split on Windows CI")
def test_one_operand_really_writes_the_default_prefix_in_the_cwd(tmp_path):
    """The residual above, measured: the input is not rewritten, and `xaa` appears.

    The second half is the assertion that matters — the *source* must survive, because
    a rule that named it would be refusing a read.
    """
    source = tmp_path / "in.txt"
    source.write_text("abcdefghij")
    result = subprocess.run(
        ["split", "-b", "3", str(source)], cwd=str(tmp_path),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert source.read_text() == "abcdefghij"
    assert (tmp_path / "xaa").exists()
    assert _extract_write_targets(f"split -b 3 {source}") == []
