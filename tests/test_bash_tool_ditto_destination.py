"""`ditto SRC... DST` writes DST, so the walk names the last operand.

`ditto` is the macOS copier, and it was in **no** table of the write-target walk:
measured on master `35284a01`, in a geometry whose destination lay outside every
allowed root, `ditto <outside>/src <outside>/dst` reported an **empty target list**
and answered ALLOW at both tiers, while `cp`, `rsync` and `truncate` on the same
two paths were refused. An empty list is allowed by construction — the loop that
judges targets never runs — so this is the same fail-open the everyday writers
(#1398), the compressor family (#1418) and `rsync` had, one installed binary over.
`/usr/bin/ditto` is present on every macOS, which is this project's primary host.

The verb's own usage line (`ditto [ <options> ] src [ ... src ] dst`) already states
the rule, and ground truth was taken before it was trusted — a fresh scratch
directory per row on this host (`/usr/bin/ditto`, 2026-08-26), the listing read back
off disk afterwards:

* `ditto f g` → rc=0, `g` created (`f` is a read);
* `ditto -c -k f arc.zip` → rc=0, the archive named by the *last operand* is created;
* `ditto -x -k arc.zip outdir` → rc=0, `outdir/f` appears, so extracting writes the
  last operand as well;
* `ditto --bom nope.bom f g` → rc=1 with the bom absent, so `--bom`'s value is a
  **read** and naming it would be a false block;
* `ditto f` → rc=0 and nothing written ("No destination"), so a lone operand names
  nothing here;
* `ditto --help` → rc=1 and nothing written.

One option is a write of its own: `--keepBinariesList <path>` creates that file
**beside** the destination — `ditto --keepBinaries --keepBinariesList kept.txt src/
dst/`, `ditto --keepBinariesList kept_no.txt src/ dst/` (i.e. without
`--keepBinaries`) and the `--keepBinariesList=<path>` spelling each create it, rc=0,
read off disk. So the two readings are *additions* here, unlike `-t <dir>` for
`cp`/`mv` and unlike `patch -o`, where each displaces the other.

Like `test_bash_tool_rsync_destination.py`, this file is separate from
`test_bash_tool_sandbox.py` because it covers one family: keeping it self-contained
keeps files that share no table from having to agree.
"""

import pytest

from emrg.tools import bash_tool
from emrg.tools.bash_tool import _check_sandbox, _extract_write_targets

# Outside every allowed root (workspace, OS temp root, the evolution data dir) and
# used only as an argument to the pure predicate — never executed and never opened.
# The protected-daemon-file arm below passes a host path for the same reason:
# `_check_sandbox` and `_protected_paths` only `realpath` it.
OUTSIDE = "/outside/emrg"
WORKSPACE = "/workspace"


def test_the_predicate_is_the_one_the_tiers_read():
    """The two functions this file judges are the ones the tool layer calls.

    A test that reached a private re-reading would keep passing while the shipped
    guard changed, which is the failure this whole class of test exists to prevent.
    """
    allowed, _reason, enforcement = _check_sandbox(
        "ditto src dst", "workspace-write", WORKSPACE
    )
    assert allowed is True
    assert enforcement in ("partial", "full")


# ── writing forms: the destination operand must be named ────────────────────────────
#
# (row, command) — the source is deliberately *inside* the workspace so that a rule
# naming the wrong operand is caught by the target it reports rather than by the
# verdict it gives, and every row's own ground-truth run is listed in the docstring.
WRITING_FORMS = (
    ("plain copy", f"ditto {WORKSPACE}/src.txt {{dest}}"),
    ("verbose", f"ditto -v {WORKSPACE}/src.txt {{dest}}"),
    ("directory copy", f"ditto {WORKSPACE}/src/ {{dest}}"),
    ("create archive", f"ditto -c -k {WORKSPACE}/src.txt {{dest}}"),
    ("create gzipped cpio", f"ditto -c -z {WORKSPACE}/src.txt {{dest}}"),
    ("extract archive", f"ditto -x -k {WORKSPACE}/src.zip {{dest}}"),
    ("extract from stdin", f"ditto -x -k - {{dest}}"),
    ("--arch value is consumed", f"ditto --arch arm64 {WORKSPACE}/src.txt {{dest}}"),
    ("--lang value is consumed", f"ditto --lang en {WORKSPACE}/src.txt {{dest}}"),
    (
        "compression level is not an operand",
        f"ditto -c -z --zlibCompressionLevel 9 {WORKSPACE}/src.txt {{dest}}",
    ),
    ("--bom is a read, not the target", f"ditto --bom {OUTSIDE}/b.bom {WORKSPACE}/src.txt {{dest}}"),
    ("several sources", f"ditto {WORKSPACE}/a.txt {WORKSPACE}/b.txt {{dest}}"),
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


def test_a_destination_inside_the_workspace_stays_allowed():
    """The rule names a path; it is not a prohibition of `ditto`.

    `workspace-write` permits a write inside the workspace, so a rule that refused
    every `ditto` would be as wrong as the hole it closes.
    """
    cmd = f"ditto {WORKSPACE}/src.txt {WORKSPACE}/dst.txt"
    assert _extract_write_targets(cmd) == [f"{WORKSPACE}/dst.txt"]
    allowed, reason, _ = _check_sandbox(cmd, "workspace-write", WORKSPACE)
    assert allowed is True, reason


def test_the_protected_daemon_file_is_refused_at_both_tiers():
    """The strongest case: the destination is a file the sandbox exists to protect.

    The path is an argument to the pure predicate — `_check_sandbox` `realpath`s it and
    opens nothing — so the host's real store is never touched by this test.
    """
    protected = "~/.emrg/rants.jsonl"
    allowed, ro_reason, _ = _check_sandbox(
        f"ditto /etc/hosts {protected}", "read-only", WORKSPACE
    )
    assert allowed is False, "read-only allowed a write to the protected store"
    assert protected in (ro_reason or ""), ro_reason

    allowed, ww_reason, _ = _check_sandbox(
        f"ditto /etc/hosts {protected}", "workspace-write", WORKSPACE
    )
    assert allowed is False, "workspace-write allowed a write to the protected store"
    assert "protected" in (ww_reason or ""), ww_reason


# ── no-write forms: naming them would be a false block ──────────────────────────────
NO_WRITE_FORMS = (
    ("help", "ditto --help"),
    ("short help", "ditto -h"),
    ("a lone operand has no destination", f"ditto {WORKSPACE}/src.txt"),
)


@pytest.mark.parametrize(
    "row,cmd", NO_WRITE_FORMS, ids=[row for row, _ in NO_WRITE_FORMS]
)
def test_a_no_write_form_names_nothing(row, cmd):
    """Each of these writes nothing (`ditto f` exits 0 with "No destination")."""
    assert _extract_write_targets(cmd) == [], row


@pytest.mark.parametrize(
    "row,cmd", NO_WRITE_FORMS, ids=[row for row, _ in NO_WRITE_FORMS]
)
def test_a_no_write_form_stays_allowed(row, cmd):
    """The false block is the direction this walk treats as worse than the hole."""
    for tier in ("read-only", "workspace-write"):
        allowed, reason, _ = _check_sandbox(cmd, tier, WORKSPACE)
        assert allowed is True, f"{row}: {tier} refused a command ({reason})"


def test_the_rule_is_not_blind():
    """The instrument's own control: the same verb with and without a destination.

    Without this, a reader that named the last operand unconditionally — or a
    fixture that never reached the rule — would leave the tests above green.
    """
    assert _extract_write_targets(f"ditto {WORKSPACE}/src.txt") == []
    assert _extract_write_targets(
        f"ditto {WORKSPACE}/src.txt {OUTSIDE}/dst"
    ) == [f"{OUTSIDE}/dst"]


# ── the option that writes a file of its own ────────────────────────────────────────
KEPT_FORMS = (
    (
        "spaced, beside --keepBinaries",
        "ditto --keepBinaries --keepBinariesList {kept} src/ dst/",
    ),
    ("spaced, on its own", "ditto --keepBinariesList {kept} src/ dst/"),
    ("=-joined", "ditto --keepBinariesList={kept} src/ dst/"),
)


@pytest.mark.parametrize(
    "row,cmd", KEPT_FORMS, ids=[row for row, _ in KEPT_FORMS]
)
def test_keep_binaries_list_names_that_file_as_well_as_the_destination(row, cmd):
    """Both paths are created, so both must be named — this pair is an addition.

    Measured: `ditto --keepBinariesList kept.txt src/ dst/` creates `kept.txt` even
    without `--keepBinaries`, which is why the option is read unconditionally rather
    than only beside the flag that names it.
    """
    kept = f"{OUTSIDE}/kept.txt"
    assert _extract_write_targets(cmd.format(kept=kept)) == ["dst/", kept], row


@pytest.mark.parametrize(
    "row,cmd", KEPT_FORMS, ids=[row for row, _ in KEPT_FORMS]
)
def test_a_kept_list_outside_every_root_is_refused_at_both_tiers(row, cmd):
    """A second write path is a second way out, so it is judged like the first."""
    cmd = cmd.format(kept=f"{OUTSIDE}/kept.txt")
    for tier in ("read-only", "workspace-write"):
        allowed, _reason, _ = _check_sandbox(cmd, tier, WORKSPACE)
        assert allowed is False, f"{row}: {tier} allowed a write to {OUTSIDE}"


def test_dropping_the_kept_list_table_unnames_the_file():
    """A mutation arm: the table this rule reads is the one that does the work.

    An assertion that cannot be killed by breaking its subject is not a claim about
    the subject. Here the subject is `_DESTINATION_LAST_OPTION_TARGETS`: with the row
    removed the destination is still named — which is what makes the pair an
    addition — and the created file is not.
    """
    cmd = f"ditto --keepBinariesList {OUTSIDE}/kept.txt src/ dst/"
    assert _extract_write_targets(cmd) == ["dst/", f"{OUTSIDE}/kept.txt"]
    saved = bash_tool._DESTINATION_LAST_OPTION_TARGETS["ditto"]
    try:
        bash_tool._DESTINATION_LAST_OPTION_TARGETS["ditto"] = frozenset()
        after = _extract_write_targets(cmd)
    finally:
        bash_tool._DESTINATION_LAST_OPTION_TARGETS["ditto"] = saved
    assert after == ["dst/"], "the kept-list row has no job: the file is unnamed anyway"


# ── the option table, and why it exists ─────────────────────────────────────────────
def test_an_option_value_is_not_read_as_an_operand():
    """`--outBom <file>` consumes its value, so the value is not a destination.

    The command below has no destination at all (ditto exits with "No destination"),
    and without the table the option's value would *become* an operand: the walk would
    name the last one, `{WORKSPACE}/src.txt` — a path the run only reads.
    """
    cmd = f"ditto -c -k --outBom {OUTSIDE}/o.bom {WORKSPACE}/src.txt"
    assert _extract_write_targets(cmd) == []

    saved = bash_tool._VERB_OPTIONS_WITH_VALUE["ditto"]
    try:
        bash_tool._VERB_OPTIONS_WITH_VALUE["ditto"] = saved - {"--outBom"}
        unnamed = _extract_write_targets(cmd)
    finally:
        bash_tool._VERB_OPTIONS_WITH_VALUE["ditto"] = saved
    assert unnamed == [f"{WORKSPACE}/src.txt"], (
        "the ditto option table has no job: the value is read as an operand either way"
    )


def test_the_option_table_carries_every_value_taking_option_of_the_tool():
    """The table is derived from ditto's own usage output, not from what looks odd.

    A missing entry is not a crash — it silently turns an option's value into an
    operand, which is the direction that misnames a path. The list below is the set
    `ditto --help` prints with a value beside it (2026-08-26), so a reader comparing
    the two sees the table has to move with the tool.
    """
    expected = {
        "--arch", "--bom", "--keepBinariesList", "--keepBinariesPattern",
        "--lang", "--outBom", "--zlibCompressionLevel",
    }
    assert set(bash_tool._VERB_OPTIONS_WITH_VALUE["ditto"]) == expected


def test_ditto_is_in_the_destination_last_set_it_was_missing_from():
    """The membership itself is the fix, so it is asserted rather than implied."""
    assert "ditto" in bash_tool._DESTINATION_LAST_VERBS
