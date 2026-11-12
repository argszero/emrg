"""The cycle record's reading: `scripts/check-cycle-record.py`.

What this file holds
--------------------
A cycle's record lives in two files that no other guard opens: the state file carries the
current snapshot, the archive carries every block that was displaced from it, and a
block's **header** is what names it. `grep -rn 'Current state' scripts/ tests/` answered
0 hits before this tool existed, so a header reading `snapshot cyc cyc20261112-220426` --
the prefix and the id run together -- passed every guard on 2026-11-12.

The assertions are the halves of "does it answer that question":

* **the defect is read in the state file and in the archive** -- the same doubled prefix is
  a finding wherever a block header sits, because both files are where a header is written;
* **the host-turn form is admitted** -- the archive really carries one
  (`snapshot 2026-10-26T20:45+08:00, host turn - no scheduler cycle`), so a rule that only
  admitted cycle ids would red on a legitimate block, and that arm is the other half of
  the refusals;
* **a header naming two ids is not admitted** -- an id that leaks into the date cell still
  names two cycles;
* **the append's own premise is read by identity** -- a state block already in the archive
  is a record filed twice, which is what a half-done cycle leaves; the archive's copy
  carries a rewritten header, so the comparison is the subject cell and the arm builds both
  the verbatim shape and the displaced one the workflow really produces;
* **a quoted header is not a block** -- both files are Markdown, so a fenced code block's
  contents are an illustration of the shape and not the shape. Measured 2026-11-13 against
  the guard as it first stood: a quoted *other* subject inflated the archive's count and
  passed (a block that does not exist), and a quoted copy of the state file's own subject
  read as "the record is filed twice" -- a fault naming a half-done cycle that was not
  there; a fenced example inside the state file read as `opens 2 snapshot headers`;
* **a header is found only where the reader says so** -- matched on the exact literal, a
  drifted `## Current State (snapshot ...` counted nothing, and "each of the zero headers
  names exactly one subject" is true, so a drifted archive printed `0 block header(s)` and
  exited 0. The near-miss reading refuses it, and the arm that keeps it honest is the
  opposite direction: `## Displaced snapshot cyc... (archived by cyc...)` is a heading the
  archive really carries and must not be refused;
* **unmeasurable is never a pass** -- an absent file and a file that is not UTF-8 text both
  answer exit 2, and the message says the question could not be asked.

Loading
-------
By path (`spec_from_file_location`) with the module registered in `sys.modules` **before**
`exec_module`: the filename is not importable by name (the dash), and a module that
dataclasses touch without being registered raises `AttributeError: 'NoneType' object has no
attribute '__dict__'` from inside `dataclasses.py` -- a red whose deepest frame is a
library has not reached the code it accuses.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-cycle-record.py"

CYCLE = "cyc20261112-220426"


def load_module(path: Path, name: str):
    """Load a script by path, registered before it executes."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = load_module(SCRIPT, "check_cycle_record")


def header(
    subject: str, date: str = "2026-11-12", tail: str = "- re-derive, never quote"
) -> str:
    """A block header as this record writes it; `tail` is the part displacement rewrites."""
    return f"## Current state (snapshot {subject}, {date}) {tail}\n"


def build(root: Path, state_body: str, archive_body: str = "") -> tuple[Path, Path]:
    state = root / mod.STATE_NAME
    archive = root / mod.ARCHIVE_NAME
    state.write_text(state_body, encoding="utf-8")
    archive.write_text(archive_body, encoding="utf-8")
    return state, archive


def run(state: Path, archive: Path) -> int:
    return mod.main(["--state", str(state), "--archive", str(archive)])


def test_a_well_formed_record_reads_clean(tmp_path: Path) -> None:
    """The state file's block is not in the archive, and both headers name one cycle."""
    state, archive = build(
        tmp_path,
        header(CYCLE) + "\n- a fact this cycle established\n",
        header("cyc20261112-214138") + "\n- an older block\n",
    )
    assert run(state, archive) == 0


def test_a_subject_that_runs_the_prefix_into_the_id_is_a_fault(tmp_path: Path) -> None:
    """The measured defect: `snapshot cyc cyc20261112-220426` names no cycle at all."""
    state, archive = build(tmp_path, header(f"cyc {CYCLE}") + "\n- a fact\n")
    assert run(state, archive) == 1
    faults = mod.header_faults(header(f"cyc {CYCLE}"))
    assert len(faults) == 1
    assert "which is not the cycle id it carries" in faults[0][1]
    assert f"cyc {CYCLE}" in faults[0][1]


def test_the_same_defect_in_the_archive_is_a_fault(tmp_path: Path) -> None:
    """A header is written into both files, so both are read."""
    state, archive = build(
        tmp_path,
        header(CYCLE) + "\n- a fact\n",
        header(f"cyc {CYCLE}") + "\n- an older block\n",
    )
    assert run(state, archive) == 1


def test_a_host_turn_header_is_admitted(tmp_path: Path) -> None:
    """The archive carries one real host-turn block, so refusing it would red on truth."""
    state, archive = build(
        tmp_path,
        header(CYCLE) + "\n- a fact\n",
        header("2026-10-26T20:45+08:00", "host turn - no scheduler cycle")
        + "\n- a block written in a host conversation\n",
    )
    assert run(state, archive) == 0


def test_a_header_naming_the_displacing_cycle_is_admitted(tmp_path: Path) -> None:
    """The archive really carries this: seven blocks name who displaced them, after the cell.

    A rule reading the whole line instead of the subject cell red on the first run against
    the real archive, on seven legitimate blocks. Their identity is the subject cell; the
    suffix is provenance.
    """
    displaced = (
        f"## Current state (snapshot {CYCLE}, 2026-11-12) - displaced by cyc20261112-214138\n"
        "- a block that a later cycle moved here\n"
    )
    state, archive = build(
        tmp_path,
        header("cyc20261112-221305") + "- the current fact\n",
        displaced,
    )
    assert run(state, archive) == 0
    assert mod.header_faults(displaced) == []


def test_a_subject_cell_naming_two_cycles_is_a_fault(tmp_path: Path) -> None:
    """Two ids in the cell that names the block leave it naming no single cycle."""
    state, archive = build(
        tmp_path,
        header(f"{CYCLE} cyc20261112-214138") + "\n- a fact\n",
    )
    assert run(state, archive) == 1
    faults = mod.header_faults(header(f"{CYCLE} cyc20261112-214138"))
    assert len(faults) == 1
    assert "names 2 cycle ids" in faults[0][1]


def test_a_state_block_already_archived_is_a_fault(tmp_path: Path) -> None:
    """The half-done cycle: the append landed, the replacement did not.

    **Both shapes are read, and the second one is the real one.** A displaced block's header
    is rewritten on the way into the archive -- `- re-derive, never quote` becomes
    `- displaced by cyc<id>` -- so the archive's copy is never the state file's text. A
    reading that compared the block's text answered clean on the realistic shape (measured
    2026-11-12: verbatim rc 1, rewritten rc 0), which is why the halves below are separate
    trees rather than one: the first is what a hand-written append leaves, the second is what
    this project does every cycle.
    """
    verbatim = tmp_path / "verbatim"
    verbatim.mkdir()
    block = header(CYCLE) + "\n- a fact\n"
    state, archive = build(
        verbatim, block, header("cyc20261112-214138") + "\n- older\n" + block
    )
    assert run(state, archive) == 1

    displaced = tmp_path / "displaced"
    displaced.mkdir()
    filed = header(CYCLE, tail="- displaced by cyc20261112-214138") + "- a fact\n"
    state, archive = build(
        displaced,
        header(CYCLE) + "- a fact\n",
        header("cyc20261112-221305", tail="- displaced by cyc20261112-222511") + "- older\n" + filed,
    )
    assert run(state, archive) == 1, (
        "the archive holds this block under its displaced header, which is the shape the "
        "workflow produces; comparing block text instead of the subject cell reads this clean"
    )
    faults = mod.archived_subjects(filed)
    assert faults == [(1, CYCLE)], (faults, "the subject cell is what identity is read from")


def test_an_archive_block_of_another_cycle_is_clean(tmp_path: Path) -> None:
    """The control: a different subject in the archive is the normal, correct state.

    Without it, an arm that faults on every archive block would pass the halves above while
    reporting a fault on a healthy record. The archive's own block here carries the same
    tail convention and a *different* cycle id, and the reading must not fire.
    """
    state, archive = build(
        tmp_path,
        header(CYCLE) + "- a fact\n",
        header("cyc20261112-221305", tail="- displaced by " + CYCLE) + "- older\n",
    )
    assert run(state, archive) == 0


def test_a_state_file_with_no_header_is_a_fault(tmp_path: Path) -> None:
    """A record that names no cycle anywhere is not a record this reading can find."""
    state, archive = build(tmp_path, "- a fact with no snapshot header\n")
    assert run(state, archive) == 1


def test_a_state_file_carrying_two_snapshots_is_a_fault(tmp_path: Path) -> None:
    """One header is clean and two is not, so the count is the boundary this arm reads."""
    one = tmp_path / "one"
    one.mkdir()
    state, archive = build(one, header(CYCLE) + "- a fact\n")
    assert run(state, archive) == 0

    two = tmp_path / "two"
    two.mkdir()
    state, archive = build(
        two,
        header(CYCLE) + "- a fact\n" + header("cyc20261112-214138") + "- an older fact\n",
    )
    assert run(state, archive) == 1


def test_an_absent_file_is_unmeasurable_not_clean(tmp_path: Path, capsys) -> None:
    """A file that is not there is a question this run could not ask."""
    archive = tmp_path / mod.ARCHIVE_NAME
    archive.write_text(header("cyc20261112-214138") + "- older\n", encoding="utf-8")
    assert run(tmp_path / "no-such-state.md", archive) == 2
    # The spelling is part of the answer: the guards in this family are read by matchers
    # that look for `could not measure` at the start of a line, and a truthful refusal in
    # another word is a refusal those readers report as a guard printing no reason at all.
    refused = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("could not measure:")]
    assert refused, "the refusal must carry the family's spelling, not only its meaning"
    assert "state" in refused[0]
    assert "this is not a pass" in refused[0]


def test_the_report_names_its_checkout_before_any_verdict(tmp_path: Path, capsys) -> None:
    """A guard that reads a tree names it first — and names *this* checkout, not the cwd.

    The line is derived from the script's own file, so the same report is never true of two
    trees: launched from anywhere, it still answers about the checkout it ships in. Without
    this arm the family's registration of this guard (a member of `RUN_HERE`) would be a
    claim nothing here checks.

    Both shapes are read, because "before any verdict" is only measured by a run that
    reaches one: an absent subject (rc 2, no verdict at all) and a file that is readable and
    is not a record (rc 1, a finding). One arm alone would leave the other untested.
    """
    expected = f"tree: {SCRIPT.parent.parent}"

    assert run(tmp_path / "no-such-state.md", tmp_path / "no-such-archive.md") == 2
    assert capsys.readouterr().out.splitlines()[0] == expected

    found, archive = build(tmp_path, "- a fact with no snapshot header\n")
    assert run(found, archive) == 1
    assert capsys.readouterr().out.splitlines()[0] == expected, (
        "a verdict must be preceded by the tree that gave it, or one report is true of two "
        "different trees"
    )


def test_a_file_that_is_not_text_is_unmeasurable_too(tmp_path: Path) -> None:
    """The third shape of an unreadable file is a `UnicodeDecodeError`, not an `OSError`."""
    state = tmp_path / mod.STATE_NAME
    archive = tmp_path / mod.ARCHIVE_NAME
    state.write_bytes(b"\xff\xfe\x00\x00 not text")
    archive.write_text(header("cyc20261112-214138") + "- older\n", encoding="utf-8")
    assert run(state, archive) == 2


def test_the_default_root_is_derived_from_this_script_not_written_as_a_home_path() -> None:
    """A copy of the tree measures its own memory instead of naming one installation."""
    root = mod.default_memory_root()
    assert root == SCRIPT.parent.parent / ".emrg" / "memory"


def test_a_header_quoted_in_a_fenced_block_is_not_a_block(tmp_path: Path) -> None:
    """A record that illustrates its own header shape is not carrying a second block.

    Both shapes are read, because they failed differently before the reader knew about
    fences: a quoted *other* subject was counted as a real block (a block that does not
    exist, reported as `2 block header(s)`), and a quoted copy of the state file's own
    subject was reported as `the record is filed twice` -- a fault naming a half-done cycle
    that was not there. Measured 2026-11-13 against the guard as it first stood.
    """
    quoted_other = (
        header("cyc20261112-214138", tail="- displaced by " + CYCLE)
        + "- an older block\n"
        + "```markdown\n"
        + header("cyc20260101-000000")
        + "- an example, not a block\n"
        + "```\n"
    )
    state, archive = build(tmp_path, header(CYCLE) + "- a fact\n", quoted_other)
    assert run(state, archive) == 0
    assert [subject for _, subject in mod.archived_subjects(quoted_other)] == [
        "cyc20261112-214138"
    ], "the quoted subject is content only where the fence does not reach"

    same = tmp_path / "same"
    same.mkdir()
    quoted_same = (
        header("cyc20261112-214138", tail="- displaced by " + CYCLE)
        + "- an older block\n"
        + "```\n"
        + header(CYCLE)
        + "```\n"
    )
    state, archive = build(same, header(CYCLE) + "- a fact\n", quoted_same)
    assert run(state, archive) == 0, (
        "the quoted copy names the state file's own subject; read as a block it reports a "
        "record filed twice that is not there"
    )


def test_a_fence_nests_by_length_so_a_quoted_marker_is_content() -> None:
    """A block opened with four markers is closed only by four; the inner three are text."""
    nested = "````\n```\n" + header("cyc20260101-000000") + "```\n````\n"
    assert mod.archived_subjects(nested) == [], (
        "a shorter marker inside a longer block is content, as a reader sees it"
    )


def test_a_fenced_example_in_the_state_file_is_not_a_second_snapshot(tmp_path: Path) -> None:
    """The state file documents its own header shape, and one snapshot is still one."""
    body = (
        header(CYCLE)
        + "- a fact\n"
        + "\nExample of the shape:\n\n"
        + "```markdown\n"
        + header("cyc20260101-000000")
        + "```\n"
    )
    state, archive = build(tmp_path, body, header("cyc20261112-214138") + "- older\n")
    assert run(state, archive) == 0
    assert mod.snapshot_subject(body) == CYCLE


def test_a_drifted_literal_prefix_is_a_fault_in_both_files(tmp_path: Path) -> None:
    """A header nothing recognises is a finding, not a count of zero.

    `## Current State (snapshot ...` with a capital `S` matched no header, so the archive
    half counted nothing and printed `0 block header(s) ... each name exactly one subject`
    -- true of the empty set, and therefore a pass the run never earned.
    """
    drifted = header("cyc20261112-214138").replace("## Current state", "## Current State")
    archive_tree = tmp_path / "archive"
    archive_tree.mkdir()
    state, archive = build(archive_tree, header(CYCLE) + "- a fact\n", drifted + "- older\n")
    assert run(state, archive) == 1
    faults = mod.unrecognised_headers(drifted)
    assert len(faults) == 1 and faults[0][1] == drifted.rstrip("\n")

    state_tree = tmp_path / "state"
    state_tree.mkdir()
    drifted_state = header(CYCLE).replace("## Current state", "## Current State")
    state, archive = build(state_tree, drifted_state + "- a fact\n")
    assert run(state, archive) == 1


def test_a_heading_the_record_really_carries_is_not_a_drifted_header(tmp_path: Path) -> None:
    """The archive holds `## Displaced snapshot cyc... (archived by cyc...)` blocks.

    A rule that refused every level-2 heading mentioning a snapshot would redden on them.
    Measured 2026-11-13 on this checkout's archive: of its 311 level-2 headings 57 are not
    admitted headers, and every one of them is this shape. The discriminator is the
    parenthetical, not the word.
    """
    legitimate = (
        "## Displaced snapshot cyc20261023-205806 (archived by cyc20261023-210604)\n"
        "- an older block\n"
        "## Moved from the state file at cyc20261025-060350 (verbatim)\n"
        "- another one\n"
    )
    state, archive = build(
        tmp_path,
        header(CYCLE) + "- a fact\n",
        header("cyc20261112-214138", tail="- displaced by " + CYCLE) + legitimate,
    )
    assert run(state, archive) == 0
    assert mod.unrecognised_headers(legitimate) == [], (
        "these are headings the record really carries, so refusing them is a rule nothing "
        "obeys"
    )


def test_an_archive_holding_text_but_no_block_header_is_a_fault(tmp_path: Path) -> None:
    """Zero headers out of a file with content is a shape renamed away, not a clean archive."""
    state, archive = build(
        tmp_path,
        header(CYCLE) + "- a fact\n",
        "## Snapshot (cyc20261112-214138)\n- a block in a shape this reader does not know\n",
    )
    assert run(state, archive) == 1


def test_an_empty_archive_is_clean_and_says_what_it_did_not_read(tmp_path: Path, capsys) -> None:
    """A record that has archived nothing is legal, and the verdict must not overclaim.

    `each of the zero headers names exactly one subject` is true and says nothing, so an
    empty archive answers rc 0 in words that name the block it did not decide.
    """
    state, archive = build(tmp_path, header(CYCLE) + "- a fact\n")
    assert run(state, archive) == 0
    out = capsys.readouterr().out
    assert "nothing archived yet" in out
    assert "decided nothing about any archived block" in out
    assert "each name exactly one subject" not in out, (
        "a universal claim about an empty set is the pass this arm exists to refuse"
    )


def test_an_unbalanced_fence_does_not_hide_the_headers_after_it(tmp_path: Path) -> None:
    """A stray marker is read as prose: masking the tail would hide every later header.

    The direction is the family's own -- the fence reader in `check-vote-count.py` takes it
    for the same reason -- and what a hidden line would cost here is a block header, which
    is exactly the silence this guard exists to report.
    """
    archive_body = (
        "```\n- a stray opener nothing closes\n"
        + header("cyc20261112-214138", tail="- displaced by " + CYCLE)
        + "- the block after it\n"
    )
    state, archive = build(tmp_path, header(CYCLE) + "- a fact\n", archive_body)
    assert run(state, archive) == 0
    assert [subject for _, subject in mod.archived_subjects(archive_body)] == [
        "cyc20261112-214138"
    ], "an unmatched opener must not hide the rest of the record"


@pytest.mark.skipif(
    not (SCRIPT.parent.parent / ".emrg" / "memory" / "evolution-nte-steady-state-since-v0.2.73.md").is_file(),
    reason="this checkout carries no cycle record, so the real files are not here to read",
)
def test_the_files_as_they_stand_read_clean() -> None:
    """The guard must pass on the record it ships beside, or it is a rule nothing obeys."""
    assert mod.main([]) == 0
