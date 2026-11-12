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
* **the append's own premise is read** -- a state block already in the archive is a record
  filed twice, which is what a half-done cycle leaves;
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


def header(subject: str, date: str = "2026-11-12") -> str:
    return (
        f"## Current state (snapshot {subject}, {date}) - re-derive, never quote\n"
    )


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
    """The half-done cycle: the append landed, the replacement did not."""
    block = header(CYCLE) + "\n- a fact\n"
    state, archive = build(tmp_path, block, header("cyc20261112-214138") + "\n- older\n" + block)
    assert run(state, archive) == 1


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


@pytest.mark.skipif(
    not (SCRIPT.parent.parent / ".emrg" / "memory" / "evolution-nte-steady-state-since-v0.2.73.md").is_file(),
    reason="this checkout carries no cycle record, so the real files are not here to read",
)
def test_the_files_as_they_stand_read_clean() -> None:
    """The guard must pass on the record it ships beside, or it is a rule nothing obeys."""
    assert mod.main([]) == 0
