"""`rsync SRC... DEST` writes DEST, so the walk names the last operand.

`rsync` was pinned as a measured hole in `test_bash_tool_option_destinations.py`'s
`UNCOVERED_WRITERS` table, on the grounds that its destination cannot be read without
the verb's own flag grammar. That grounds holds for the *option* destinations the table
is about; for rsync it never applied, because rsync's destination is an **operand** —
the last one, exactly as `cp`'s is. Measured on master `f4e7328d` with the real
predicate, one protected daemon file and one workspace file, both tiers: every writing
form answered **ALLOW with an empty target list** while `cp`, `truncate` and `tee` on
the same two paths were refused, which is the fail-open this file's rule closes.

Ground truth for what rsync really does, taken in a scratch tree on this host with the
destination's content read back off disk (`openrsync`, "rsync version 2.6.9
compatible", 2026-09-19):

* `rsync -a src/a.txt dst/victim.txt` → `victim.txt` holds `SOURCE` afterwards, so the
  destination operand really is rewritten;
* `rsync -an src/a.txt dst/victim.txt` and `rsync --list-only src/a.txt dst/victim.txt`
  → unchanged (`KEEP-ME`), so both are read forms and naming their operand would be a
  false block;
* `rsync -a src/ dst/` copies into the destination directory.

This file is separate from `test_bash_tool_sandbox.py` for the same reason
`test_bash_tool_option_destinations.py` is: it covers one family, and keeping it
self-contained keeps the fixtures of files that share no table from having to agree.
"""

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
        "rsync -a src dst", "workspace-write", WORKSPACE
    )
    assert allowed is True
    assert enforcement in ("partial", "full")


# ── writing forms: the destination operand must be named ────────────────────────────
#
# (row, command) — every path the walk must name is the destination, and the source is
# deliberately *inside* the workspace so a rule that named the wrong operand would be
# caught by the target it reports rather than by the verdict it gives.
WRITING_FORMS = (
    ("plain", f"rsync -a {WORKSPACE}/src.txt {{dest}}"),
    ("archive slash", f"rsync -a {WORKSPACE}/src/ {{dest}}"),
    ("verbose", f"rsync -avz {WORKSPACE}/src/ {{dest}}"),
    ("delete", f"rsync -a --delete {WORKSPACE}/src/ {{dest}}"),
    ("preserve times", f"rsync -t {WORKSPACE}/src.txt {{dest}}"),
    ("rsh, spaced value", f"rsync -e ssh {WORKSPACE}/src.txt {{dest}}"),
    ("exclude, long", f"rsync -a --exclude=foo {WORKSPACE}/src.txt {{dest}}"),
    ("two sources", f"rsync -a {WORKSPACE}/a.txt {WORKSPACE}/b.txt {{dest}}"),
    ("after --", f"rsync -a -- {WORKSPACE}/src.txt {{dest}}"),
)


@pytest.mark.parametrize("row,cmd", WRITING_FORMS, ids=[row for row, _ in WRITING_FORMS])
def test_every_writing_form_names_the_destination(row, cmd):
    """The last operand is the destination, and the walk must report exactly it."""
    dest = f"{OUTSIDE}/dst"
    assert _extract_write_targets(cmd.format(dest=dest)) == [dest], row


@pytest.mark.parametrize("row,cmd", WRITING_FORMS, ids=[row for row, _ in WRITING_FORMS])
def test_a_named_destination_is_refused_at_both_tiers(row, cmd):
    """A resource outside every allowed root is refused whether or not writes are on.

    Both tiers, because the hole was in both and the untested tier is the one a cycle
    actually runs in: `workspace-write` still refuses writes outside the workspace, and
    `read-only` refuses them anywhere.
    """
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(
            cmd.format(dest=f"{OUTSIDE}/dst"), tier, WORKSPACE
        )
        assert allowed is False, f"{row}: {tier} allowed a write to {OUTSIDE}"
        assert reason, f"{row}: {tier} refused without a reason"


def test_the_protected_daemon_file_is_refused_at_both_tiers():
    """The strongest case: the destination is a file the sandbox exists to protect.

    The path is an argument to the pure predicate — `_check_sandbox` `realpath`s it and
    opens nothing — so the host's real store is never touched by this test.

    The reason differs by tier and both are asserted as they are rather than loosely:
    `read-only` refuses the write on its own account, so its message names the target;
    `workspace-write` refuses it *because* the file is protected, which is the reading
    that says this rule reached the protected-path check rather than a generic one.
    """
    protected = "~/.emrg/rants.jsonl"
    allowed, ro_reason, _ = _check_sandbox(f"rsync -a /etc/hosts {protected}",
                                           "read-only", WORKSPACE)
    assert allowed is False, "read-only allowed a write to the protected store"
    assert protected in (ro_reason or ""), ro_reason

    allowed, ww_reason, _ = _check_sandbox(f"rsync -a /etc/hosts {protected}",
                                           "workspace-write", WORKSPACE)
    assert allowed is False, "workspace-write allowed a write to the protected store"
    assert "protected" in (ww_reason or ""), ww_reason


# ── read forms: the operand is not written, so naming it would be a false block ─────
READING_FORMS = (
    ("-an cluster", f"rsync -an {WORKSPACE}/src.txt {{dest}}"),
    ("-n alone", f"rsync -n {WORKSPACE}/src.txt {{dest}}"),
    ("--dry-run", f"rsync -a --dry-run {WORKSPACE}/src.txt {{dest}}"),
    ("--list-only", f"rsync --list-only {WORKSPACE}/src.txt {{dest}}"),
    ("single operand = listing", f"rsync -a {WORKSPACE}/src.txt"),
)


@pytest.mark.parametrize("row,cmd", READING_FORMS, ids=[row for row, _ in READING_FORMS])
def test_a_read_form_names_nothing(row, cmd):
    """Each of these writes nothing, so the walk must report no target at all."""
    assert _extract_write_targets(cmd.replace("{dest}", f"{OUTSIDE}/dst")) == [], row


@pytest.mark.parametrize("row,cmd", READING_FORMS, ids=[row for row, _ in READING_FORMS])
def test_a_read_form_stays_allowed(row, cmd):
    """The false block is the direction this walk treats as worse than the hole."""
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(
            cmd.replace("{dest}", f"{OUTSIDE}/dst"), tier, WORKSPACE
        )
        assert allowed is True, f"{row}: {tier} refused a read ({reason})"


def test_the_read_gate_is_not_blind():
    """The instrument's own control: the same command with and without the gate.

    Without this, a reader that answered "read" to everything — or a fixture that
    never reached the rule — would leave the tests above green while the defect was
    fully open. The pair differs by one letter and by nothing else.
    """
    dest = f"{OUTSIDE}/dst"
    with_gate = f"rsync -an {WORKSPACE}/src.txt {dest}"
    without_gate = f"rsync -a {WORKSPACE}/src.txt {dest}"
    assert _extract_write_targets(with_gate) == []
    assert _extract_write_targets(without_gate) == [dest]


def test_a_dry_run_does_not_name_a_second_destination_either():
    """The gate is about the whole invocation, not about one operand's position.

    `rsync -an --delete src/ dst/` is the spelling somebody types before a mirror, and
    it deletes nothing: naming either operand would refuse a command that writes
    nothing.
    """
    cmd = f"rsync -an --delete {WORKSPACE}/src/ {OUTSIDE}/dst/"
    assert _extract_write_targets(cmd) == []
    allowed, _reason, _ = _check_sandbox(cmd, "read-only", WORKSPACE)
    assert allowed is True


# ── the named limits, pinned so a later reader does not re-derive them ──────────────
def test_an_attached_value_carrying_n_is_the_known_limit():
    """`-T/tmp/n` is a temp directory, not a dry run — and it is missed, on purpose.

    The rule reads short clusters for the dry-run letter, so an `n` inside an attached
    *value* reads as a read form. Distinguishing them needs the per-option grammar this
    walk refuses to grow, and the two ways of guessing are not equally costly: guessing
    "write" would refuse `rsync -an`, a spelling people actually type. The spaced
    spelling is unaffected, because a value in its own token is never scanned — which is
    the half of the limit that keeps the ordinary spelling correct.
    """
    attached = f"rsync -a -T/tmp/n {WORKSPACE}/src.txt {OUTSIDE}/dst"
    spaced = f"rsync -a -T /tmp/n {WORKSPACE}/src.txt {OUTSIDE}/dst"
    assert _extract_write_targets(attached) == [], "the documented miss"
    assert _extract_write_targets(spaced) == [f"{OUTSIDE}/dst"], "the spaced form is safe"


def test_a_write_batch_file_is_the_documented_residual():
    """`--write-batch=<file>` writes a second path; it is left unnamed, and named here.

    A batch file is a debugging artefact of a transfer rather than the transfer, and
    the destination operand this rule exists for is still reported. Pinned so the
    coverage claim in `bash_tool.py` stays as wide as the reader it describes.
    """
    cmd = f"rsync -a --write-batch={OUTSIDE}/batch {WORKSPACE}/src.txt {OUTSIDE}/dst"
    assert _extract_write_targets(cmd) == [f"{OUTSIDE}/dst"]
