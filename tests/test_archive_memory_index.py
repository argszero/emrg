"""Tests for scripts/archive-memory-index.py - the index move, verified.

Background
----------
The index-hygiene protocol is a *move*: keep at most 50 cycle rows, append the
ones that fall off to `cycle-archive-<YYYYMMDD>.md`, never delete a detail file.
It has been executed by hand-written scripts in every evolution cycle, and twice
the script was wrong in a way that damaged the thing the protocol protects:

* an empty shell variable turned ``sed -i '' "${n}d"`` into the sed script ``d``
  and deleted every line of the index;
* an off-by-one archived the **newest** row, so the index lost the row it had
  just gained.

Both are one edit away from a correct one-liner, so the tests below pin the
properties that make the operation safe and that a positional implementation
cannot satisfy:

* the rows that move are the ones with the **smallest cycle id**, wherever they
  sit in the file - an index written newest-first and one written oldest-first
  must give the same answer;
* the row lines of (index + archive) are **conserved**: the multiset before
  equals the multiset after, so a row is neither lost nor invented;
* non-row lines and existing archive rows are untouched, and the archive is
  append-only;
* detail files are never opened for writing;
* the check runs against the **files on disk**, and a failure restores both files
  (the failure mode is "nothing happened", never "half a move").

Every violated property is pinned in both directions: the same call on a
compliant index reports OK, so a guard that fires on everything cannot pass.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "archive-memory-index.py"

NOTE = "> notes live here, above the rows\n"

OLD = "20260901-100000"
MID = "20260902-100000"
NEW = "20260903-100000"
NEWEST = "20260904-100000"


def _load_module():
    spec = importlib.util.spec_from_file_location("archive_memory_index", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: the tool uses dataclasses, and with
    # `from __future__ import annotations` every annotation is a string, so the
    # dataclass machinery has to find the module in sys.modules to resolve it.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _row(stamp: str, target: str | None = None) -> str:
    return f"- [cyc{stamp}]({target or f'cycle-{stamp}.md'}) - row {stamp}\n"


def _write_index(path: Path, rows: list[str], heading: str = "# Index\n") -> str:
    text = heading + NOTE + "".join(rows)
    path.write_text(text, encoding="utf-8")
    return text


def _rows_in(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- [")]


TARGET_RE = re.compile(r"\]\(([^)]+)\)")


def _targets(path: Path) -> list[str]:
    """The link targets of `path`'s rows, in file order."""
    return [TARGET_RE.search(line).group(1) for line in _rows_in(path)]


def test_the_oldest_rows_move_not_the_newest(tmp_path, mod):
    """The incident: an off-by-one that archived the newest row instead."""
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    _write_index(index, [_row(NEWEST), _row(NEW), _row(MID), _row(OLD)])

    assert mod.main([str(index), "--cap", "2", "--archive", str(archive)]) == 0

    assert _targets(index) == [f"cycle-{NEWEST}.md", f"cycle-{NEW}.md"]
    assert _targets(archive) == [f"cycle-{OLD}.md", f"cycle-{MID}.md"], (
        "the two OLDEST rows must be the ones that move, appended in cycle order"
    )


def test_rows_are_chosen_by_the_cycle_id_not_by_position(tmp_path, mod):
    """Position and cycle id disagree here: NEWS sits first, OLD second.

    A positional implementation drops the tail (MID) and keeps OLD; the tool must
    drop the row whose cycle id is smallest, wherever it sits.
    """
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    _write_index(index, [_row(NEWEST), _row(OLD), _row(MID)])

    assert mod.main([str(index), "--cap", "2", "--archive", str(archive)]) == 0

    assert _targets(index) == [f"cycle-{NEWEST}.md", f"cycle-{MID}.md"]
    assert _targets(archive) == [f"cycle-{OLD}.md"]


def test_every_row_is_conserved_across_the_two_files(tmp_path, mod):
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    rows = [_row(NEWEST), _row(NEW), _row(MID), _row(OLD)]
    index_before = _write_index(index, rows)
    archive.write_text("# cycle index archive (2026-09-01)\n\n" + _row("20260801-000000"), encoding="utf-8")
    archive_before = archive.read_text(encoding="utf-8")

    assert mod.main([str(index), "--cap", "2", "--archive", str(archive)]) == 0

    before = Counter(mod.row_texts(index_before) + mod.row_texts(archive_before))
    after = Counter(
        mod.row_texts(index.read_text(encoding="utf-8"))
        + mod.row_texts(archive.read_text(encoding="utf-8"))
    )
    assert before == after, "a move may not lose or invent a row"


def test_the_archive_is_append_only_and_the_index_keeps_its_notes(tmp_path, mod):
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    index_before = _write_index(index, [_row(NEW), _row(OLD)])
    old_row = _row("20260801-000000")
    archive.write_text("# cycle index archive (2026-09-01)\n\n" + old_row, encoding="utf-8")

    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 0

    archive_after = archive.read_text(encoding="utf-8")
    assert archive_after.startswith("# cycle index archive (2026-09-01)\n\n" + old_row)
    assert _rows_in(archive)[0] == old_row.strip()
    assert _rows_in(archive)[1:] == [_row(OLD).strip()], "new rows append after the old ones"
    assert mod.non_row_lines(index.read_text(encoding="utf-8")) == mod.non_row_lines(index_before)


def test_a_missing_archive_is_created_with_a_heading(tmp_path, mod):
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    _write_index(index, [_row(NEW), _row(OLD)])

    assert not archive.exists()
    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 0

    text = archive.read_text(encoding="utf-8")
    assert text.startswith("# cycle index archive ("), text[:40]
    assert "MEMORY.md" in text
    assert _rows_in(archive) == [_row(OLD).strip()]


def test_nothing_to_move_writes_nothing(tmp_path, mod):
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    before = _write_index(index, [_row(NEW), _row(MID), _row(OLD)])

    assert mod.main([str(index), "--cap", "3", "--archive", str(archive)]) == 0

    assert index.read_text(encoding="utf-8") == before
    assert not archive.exists(), "an untouched run must not create an archive"


def test_a_non_cycle_row_is_never_archived(tmp_path, mod):
    """Rows without a cycle id are not part of the capped set."""
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    _write_index(index, [_row(NEW), "- [project state](state.md) - not a cycle row\n", _row(OLD)])

    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 0

    kept = _rows_in(index)
    assert "- [project state](state.md) - not a cycle row" in kept
    assert kept == [_row(NEW).strip(), "- [project state](state.md) - not a cycle row"]


def test_detail_files_are_never_touched(tmp_path, mod):
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    _write_index(index, [_row(NEW), _row(OLD)])
    details = []
    for stamp in (NEW, OLD):
        target = tmp_path / f"cycle-{stamp}.md"
        target.write_text(f"# cycle {stamp}\n\nbody\n", encoding="utf-8")
        details.append((target, target.read_text(encoding="utf-8")))

    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 0

    for target, body in details:
        assert target.is_file(), f"{target.name} was removed"
        assert target.read_text(encoding="utf-8") == body


def test_check_mode_pins_a_violation_and_a_clean_index(tmp_path, mod, capsys):
    index = tmp_path / "MEMORY.md"
    _write_index(index, [_row(NEW), _row(MID), _row(OLD)])

    assert mod.main([str(index), "--cap", "2", "--check"]) == 1
    out = capsys.readouterr().out
    assert "VIOLATION" in out and "over the cap 2" in out
    assert index.read_text(encoding="utf-8").endswith(_row(OLD)), "check mode is read-only"

    assert mod.main([str(index), "--cap", "3", "--check"]) == 0
    assert "OK" in capsys.readouterr().out


def test_check_mode_flags_a_long_row_and_a_duplicate(tmp_path, mod, capsys):
    index = tmp_path / "MEMORY.md"
    row = _row(NEW)
    padded = row.rstrip("\n") + "y" * (mod.ROW_MAX_CHARS - len(row.rstrip("\n")))
    assert len(padded) == mod.ROW_MAX_CHARS, "the boundary row must sit exactly at the cap"
    _write_index(index, [padded])

    assert mod.main([str(index), "--cap", "50", "--check"]) == 0, (
        "a row exactly at the 512-char cap is compliant"
    )

    over = padded + "z"
    _write_index(index, [over])
    assert mod.main([str(index), "--cap", "50", "--check"]) == 1
    assert "over 512 chars" in capsys.readouterr().out

    _write_index(index, [_row(NEW), _row(NEW)])
    assert mod.main([str(index), "--cap", "50", "--check"]) == 1
    assert "duplicate" in capsys.readouterr().out


def test_the_verifiers_rules_fire_on_a_bad_plan(tmp_path, mod):
    """The guard's own arms: each rule must fire when the plan breaks it.

    A verifier that cannot report a broken plan is indistinguishable from a
    correct move, so every rule is driven in both states - the real plan passes
    it, and a deliberately damaged copy of that same plan does not.
    """
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    _write_index(index, [_row(NEW), _row(OLD)])
    archive.write_text("# cycle index archive (2026-09-01)\n\n" + _row("20260801-000000"), encoding="utf-8")

    plan = mod.build_plan(index, archive, 1, "2026-09-14")
    assert len(plan.moved) == 1, "the fixture must plan a real move"
    assert mod.verify_plan(plan, 1) == [], "the real plan must verify"

    lost = replace(plan, archive_after=plan.archive_before)
    assert any("not conserved" in p for p in mod.verify_plan(lost, 1))

    dropped_note = replace(plan, index_after=plan.index_after.replace(NOTE, ""))
    assert any("non-row lines" in p for p in mod.verify_plan(dropped_note, 1))

    rewritten = replace(plan, archive_after="# kept\n\n" + plan.archive_after)
    assert any("rewritten" in p for p in mod.verify_plan(rewritten, 1))

    over_cap = replace(plan, index_after=plan.index_before)
    assert any("over the cap" in p for p in mod.verify_plan(over_cap, 0))


def test_dry_run_writes_nothing(tmp_path, mod):
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    before = _write_index(index, [_row(NEW), _row(OLD)])

    assert mod.main([str(index), "--cap", "1", "--dry-run", "--archive", str(archive)]) == 0

    assert index.read_text(encoding="utf-8") == before
    assert not archive.exists()


def test_a_plan_that_does_not_verify_never_reaches_the_files(tmp_path, mod, monkeypatch):
    """The pre-write check: an unsound plan must leave both files untouched."""
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    before = _write_index(index, [_row(NEW), _row(OLD)])
    monkeypatch.setattr(mod, "verify_plan", lambda plan, cap: ["injected: not conserved"])

    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 2

    assert index.read_text(encoding="utf-8") == before
    assert not archive.exists()


def test_a_failed_post_write_check_restores_both_files(tmp_path, mod, monkeypatch, capsys):
    """The safety net: the write happened, the verification failed, both revert."""
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    before = _write_index(index, [_row(NEW), _row(OLD)])
    monkeypatch.setattr(mod, "measure_on_disk", lambda *a, **k: ["injected: not conserved"])

    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 2

    assert index.read_text(encoding="utf-8") == before, "the index must be restored"
    assert not archive.exists(), "a freshly created archive must be removed again"
    err = capsys.readouterr().err
    assert "restored" in err and "injected" in err


def test_a_failed_post_write_check_restores_an_existing_archive(tmp_path, mod, monkeypatch):
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    _write_index(index, [_row(NEW), _row(OLD)])
    archive.write_text("# cycle index archive (2026-09-01)\n\n" + _row("20260801-000000"), encoding="utf-8")
    archive_before = archive.read_text(encoding="utf-8")
    monkeypatch.setattr(mod, "measure_on_disk", lambda *a, **k: ["injected"])

    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 2

    assert archive.read_text(encoding="utf-8") == archive_before


def test_a_missing_index_is_a_measurement_error(tmp_path, mod, capsys):
    assert mod.main([str(tmp_path / "nope.md"), "--check"]) == 2
    assert "no index at" in capsys.readouterr().err
