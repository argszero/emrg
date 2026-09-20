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
  (the failure mode is "nothing happened", never "half a move");
* the move is a **compare-and-swap on content**: the index has other writers (every
  task's cycles append to the same file), so a row appended while this run is
  planning is planned *for*, and a file that keeps moving is refused rather than
  overwritten - the conservation check above cannot see that row, because it is in
  neither snapshot this process took;
* both files are written by **rename**, so an index is never a truncated prefix of
  itself when a write dies.

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
        "a row exactly at the cap is compliant"
    )

    over = padded + "z"
    _write_index(index, [over])
    assert mod.main([str(index), "--cap", "50", "--check"]) == 1
    assert f"over {mod.ROW_MAX_CHARS} chars" in capsys.readouterr().out

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


def test_a_decode_failure_is_a_measurement_error(tmp_path, mod, capsys):
    """A read that fails must not arrive as a rule violation.

    An escaping traceback exits the process with code 1, which this tool's contract
    reads as "the index violates the row rules" - a verdict about a file it never
    read. Bytes that are not UTF-8 fail the read the same way a denied read does, so
    this arm is the cross-platform spelling; the denied read has its own test below.
    """
    index = tmp_path / "MEMORY.md"
    index.write_bytes(b"- [cyc20260901-100000](cycle-20260901-100000.md)\n\xff\xfe\n")
    assert mod.main([str(index), "--check"]) == 2
    assert "could not read" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 000 does not deny reading on Windows")
def test_a_denied_read_is_a_measurement_error(tmp_path, mod, capsys):
    """The other spelling of a failed read, end to end through the real filesystem.

    Windows is skipped rather than asserted loosely: there `chmod(0o000)` only sets
    the read-only attribute, which does not stop a read, so this arm can only mean
    what it says on POSIX. (The Windows CI job is where a test that assumed POSIX
    semantics was caught - `cyc20260914-042726`.)
    """
    locked = tmp_path / "locked.md"
    locked.write_text(_row(OLD), encoding="utf-8")
    locked.chmod(0o000)
    try:
        assert mod.main([str(locked), "--check"]) == 2
        assert "could not read" in capsys.readouterr().err
    finally:
        locked.chmod(0o600)


# --- a row written in another shape must be reported, never silently skipped ----
#
# The counting path is where a false healthy verdict is invisible: the tool prints
# `N cycle row(s)`, and N = 0 on an index of 52 cycle rows written as a table reads
# as a clean index. Measured before these rules (`cyc20260914-042726`):
#
#     shape                                       --check              trim
#     - [cyc<ts>](cycle-<ts>.md)                  rc 1, over cap       moves 2 rows
#     - [cyc<ts>](memory/cycle-<ts>.md)           rc 0, "0 of 52" OK   nothing to move
#     | cyc<ts> | cycle-<ts>.md |                 rc 0, "0 of 0" OK    nothing to move
#     - cyc<ts> - cycle-<ts>.md -                 rc 0, "0 of 0" OK    nothing to move
#
# The path-prefixed link is a *readable* row once the id comes from the basename;
# the two non-link shapes are unreadable rows, and the tool now says so instead of
# answering `OK`. The last two tests are the other direction: a note or an
# ordinary non-cycle row that merely mentions a cycle id is not an unreadable row,
# so a guard that fires on everything cannot pass.


def test_a_path_prefixed_link_is_still_a_cycle_row(tmp_path, mod, capsys):
    """`memory/cycle-<ts>.md` names the same detail file: order it by its id."""
    index = tmp_path / "MEMORY.md"
    stamps = [f"2026090{i}-100000" for i in range(1, 5)]
    _write_index(index, [_row(s, target=f"memory/cycle-{s}.md") for s in stamps])

    assert mod.main([str(index), "--cap", "3", "--check"]) == 1, (
        "a prefixed link is a cycle row, so four of them are over a cap of three"
    )
    assert "4 cycle row(s) of 4 row(s)" in capsys.readouterr().out

    archive = tmp_path / "cycle-archive-X.md"
    assert mod.main([str(index), "--cap", "3", "--archive", str(archive)]) == 0
    kept = _targets(index)
    assert f"memory/cycle-{stamps[0]}.md" not in kept, "the oldest id must move"
    assert _targets(archive) == [f"memory/cycle-{stamps[0]}.md"]


def test_a_table_row_naming_a_cycle_is_an_unreadable_row(tmp_path, mod, capsys):
    """The measured false `OK`: 52 table rows were counted as zero.

    Two ways to be wrong about this index, and the answer is neither: `OK` (the
    measured pre-fix verdict) and `VIOLATION: 52 cycle rows` (a count taken over
    the rows that happen to be readable). Which rows are cycle rows is not
    knowable here, so nothing is counted and nothing is claimed.
    """
    index = tmp_path / "MEMORY.md"
    body = "# Index\n\n" + "".join(
        f"| cyc2026090{i}-100000 | cycle-2026090{i}-100000.md | row |\n" for i in range(1, 5)
    )
    index.write_text(body, encoding="utf-8")

    assert mod.main([str(index), "--cap", "3", "--check"]) == 2
    captured = capsys.readouterr()
    assert "are not markdown link rows" in captured.err, captured.err
    assert "MEMORY.md:3" in captured.err, "the first blocking line must be named"
    for wrong in ("OK: the index respects the row rules", "cycle row(s)", "VIOLATION"):
        assert wrong not in captured.out, f"nothing may be claimed: {captured.out}"


def test_the_trim_refuses_rather_than_moving_the_rows_it_can_read(tmp_path, mod, capsys):
    """A subset of the rows is not a move: the cap would stay violated."""
    index = tmp_path / "MEMORY.md"
    rows = [_row(f"2026090{i}-100000") for i in range(1, 5)]
    offending = "| cyc20260905-100000 | cycle-20260905-100000.md | row |"
    rows.append(offending + "\n")
    before = _write_index(index, rows)
    line_no = before.splitlines().index(offending) + 1
    archive = tmp_path / "cycle-archive-X.md"

    assert mod.main([str(index), "--cap", "3", "--archive", str(archive)]) == 2

    assert index.read_text(encoding="utf-8") == before, "refusing means writing nothing"
    assert not archive.exists()
    err = capsys.readouterr().err
    assert f"MEMORY.md:{line_no}" in err, f"the offending line must be named: {err}"
    assert "nothing to move" not in err


def test_a_note_that_mentions_a_cycle_id_is_not_an_unreadable_row(tmp_path, mod, capsys):
    """The negative direction: prose, and a non-cycle row quoting an id."""
    index = tmp_path / "MEMORY.md"
    text = (
        "# Index\n"
        "\n"
        "> Rebuilt by cycle cyc20260913-180238 after the file was damaged.\n"
        "\n"
        f"- [state](state.md) - task - active - cyc20260914-040021: master unchanged\n"
        + _row("20260901-100000")
    )
    index.write_text(text, encoding="utf-8")

    assert mod.main([str(index), "--check"]) == 0, capsys.readouterr().out
    assert "OK: the index respects the row rules" in capsys.readouterr().out


def test_a_plain_index_is_still_clean_in_both_modes(tmp_path, mod, capsys):
    """No unreadable row anywhere: the rule must not turn a normal index into work."""
    index = tmp_path / "MEMORY.md"
    _write_index(index, [_row(NEW), _row(MID), _row(OLD)])

    assert mod.main([str(index), "--cap", "50", "--check"]) == 0
    assert "OK" in capsys.readouterr().out

    archive = tmp_path / "cycle-archive-X.md"
    assert mod.main([str(index), "--cap", "2", "--archive", str(archive)]) == 0
    assert len(_targets(index)) == 2 and _targets(archive) == [f"cycle-{OLD}.md"]


# --- the index is shared: a move must survive another writer ----------------
#
# `/Users/argszero/.emrg/evolution/.emrg/memory/MEMORY.md` is written by *every*
# task's cycles, not only this repository's, and the daemon embeds it verbatim in
# every system prompt - so a run of the archiver is never the only writer of it.
# The conservation check the tests above pin cannot see a row another writer
# appends between this run's read and its write: the row is in neither snapshot
# this process took, so the verdict is `OK` and the plan drops it. These tests
# pin what makes that window harmless - a compare-and-swap on content, and a
# rename instead of a truncating write.


def _appending_plan(mod, path: Path, line: str, *, times: int = 1):
    """`build_plan` that appends `line` to `path` after its first `times` calls.

    The append happens *after* the wrapped call read the file, which is exactly
    where another task's cycle lands: between this run's read and its write.
    """
    real = mod.build_plan
    calls: list[int] = []

    def planning(*args, **kwargs):
        plan = real(*args, **kwargs)
        calls.append(1)
        if len(calls) <= times:
            path.write_text(path.read_text(encoding="utf-8") + line, encoding="utf-8")
        return plan

    return planning, calls


def _no_temp_left_behind(tmp_path: Path) -> None:
    """A leftover `.tmp-` file would be dirt next to a tracked index."""
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name]
    assert not leftovers, f"the atomic write leaked {leftovers}"


def test_a_row_that_arrives_while_planning_is_not_overwritten(tmp_path, mod, monkeypatch):
    """The defect: a first-write-wins move silently drops the other writer's row."""
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    _write_index(index, [_row(NEWEST), _row(NEW), _row(MID), _row(OLD)])
    sibling = _row("20260905-100000")
    planning, calls = _appending_plan(mod, index, sibling)
    monkeypatch.setattr(mod, "build_plan", planning)

    assert mod.main([str(index), "--cap", "2", "--archive", str(archive)]) == 0

    assert _targets(index) == [f"cycle-{NEWEST}.md", "cycle-20260905-100000.md"], (
        "the row that arrived mid-run must still be in the index"
    )
    assert _targets(archive) == [f"cycle-{OLD}.md", f"cycle-{MID}.md", f"cycle-{NEW}.md"], (
        "the second plan moves the three oldest of the five rows"
    )
    assert len(calls) == 2, "the run must re-plan on the text on disk, not write the stale one"
    _no_temp_left_behind(tmp_path)


def test_a_row_another_archiver_appends_is_not_overwritten(tmp_path, mod, monkeypatch):
    """The archive half: it is shared too, and it is append-only."""
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    _write_index(index, [_row(NEW), _row(OLD)])
    archive.write_text(
        "# cycle index archive (2026-09-01)\n\n" + _row("20260801-000000"), encoding="utf-8"
    )
    other = _row("20260802-000000")
    planning, calls = _appending_plan(mod, archive, other)
    monkeypatch.setattr(mod, "build_plan", planning)

    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 0

    text = archive.read_text(encoding="utf-8")
    assert text.count(other.strip()) == 1, "the other archiver's row survives, exactly once"
    assert other.strip() in text.splitlines()
    assert len(calls) == 2


def test_a_file_that_keeps_moving_is_refused_rather_than_overwritten(tmp_path, mod, monkeypatch, capsys):
    """Bounded retries: a file rewritten on every attempt is not this run's to write."""
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    _write_index(index, [_row(NEW), _row(OLD)])
    real = mod.build_plan
    stamps = iter(["20260801", "20260802", "20260803", "20260804"])

    def planning(*args, **kwargs):
        plan = real(*args, **kwargs)
        index.write_text(
            index.read_text(encoding="utf-8") + _row(f"{next(stamps)}-000000"), encoding="utf-8"
        )
        return plan

    monkeypatch.setattr(mod, "build_plan", planning)

    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 2

    err = capsys.readouterr().err
    assert "refuses to write" in err and str(index) in err
    assert not archive.exists(), "a refused run writes nothing at all"
    assert f"cycle-{NEW}.md" in _targets(index) and f"cycle-{OLD}.md" in _targets(index)
    _no_temp_left_behind(tmp_path)


def test_a_refusal_names_the_file_that_could_not_be_read_back(tmp_path, mod, monkeypatch, capsys):
    """The refusal names the file it is about: an unreadable archive, not the index.

    The compare-and-swap reads two files. One `try` around both used to append the
    *index* whichever read raised, so an archive this run could not read back was
    reported as "the index changed" and the operator was sent to a file nothing had
    written, while the unreadable one went unnamed (issue #1486). Here the index is
    readable and byte-identical to what the plan read, throughout.
    """
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    _write_index(index, [_row(NEW), _row(OLD)])
    before_index = index.read_text(encoding="utf-8")

    # A plan that was built while the archive was still readable; every attempt
    # re-plans from it, exactly as the retry loop does, while the file on disk
    # can no longer be read back.
    plan = mod.build_plan(index, archive, 1, "2026-09-21")
    archive.unlink(missing_ok=True)
    archive.mkdir()
    monkeypatch.setattr(mod, "build_plan", lambda *_a, **_k: plan)

    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 2

    err = capsys.readouterr().err
    assert str(archive) in err, "the refusal must name the archive it could not read"
    assert str(index) not in err, "the index was readable and unchanged throughout"
    assert index.read_text(encoding="utf-8") == before_index, "a refused run writes nothing"
    assert archive.is_dir(), "the unreadable archive is not this run's to replace"


def test_a_failed_replace_leaves_the_index_exactly_as_it_was(tmp_path, mod, monkeypatch, capsys):
    """The write is a rename, so a denial cannot leave a prefix of the new index.

    With a truncating write there is no replace to fail: the file is overwritten
    first and the failure arrives afterwards, which is why this arm asserts both
    the exit code and the bytes.
    """
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"
    before = _write_index(index, [_row(NEW), _row(OLD)])

    def denied(*_args, **_kwargs):
        raise OSError("injected: replace denied")

    monkeypatch.setattr(mod.os, "replace", denied)

    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 2

    assert index.read_text(encoding="utf-8") == before, "the index must be the old file"
    assert not archive.exists()
    assert "writing failed" in capsys.readouterr().err
    _no_temp_left_behind(tmp_path)
