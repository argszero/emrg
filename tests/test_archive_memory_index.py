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
from datetime import datetime, timedelta
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


def _over_cap_row(stamp: str, mod) -> str:
    """A valid row whose line is one char over the per-row cap, newline included.

    Built from ``_row`` so the padding cannot accidentally become a second line (or
    swallow the next row) if the row shape changes: the padding goes on the row's own
    line, before its newline.
    """
    line = _row(stamp).rstrip("\n")
    return line + "y" * (mod.ROW_MAX_CHARS + 1 - len(line)) + "\n"


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


def test_the_move_reports_the_row_length_rule_it_does_not_enforce(tmp_path, mod, capsys):
    """The one rule with a checker and no runner (#1551), read where it can be acted on.

    Both directions on the same fixture, and the last block is the sharper one: the
    reading is of the **post-move** index, so a row that was over the cap and just
    moved out is not reported as still sitting there. A report taken of the index as
    it was *before* the move would name a violation the run it is reporting on has
    already removed.
    """
    index = tmp_path / "MEMORY.md"
    archive = tmp_path / "cycle-archive-X.md"

    # A compliant index: the move says nothing about a rule that is not broken.
    _write_index(index, [_row(NEWEST), _row(NEW)])
    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 0
    out = capsys.readouterr()
    assert "over the per-row cap" not in out.err, "no violation, no note"

    # The over-cap row stays behind: the note names it, and the exit code does not move.
    _write_index(index, [_over_cap_row(NEW, mod), _row(MID)])
    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 0, (
        "the length rule is a report, not a refusal: refusing would strand the row cap"
    )
    out = capsys.readouterr()
    assert "1 row(s) of the index are over the per-row cap" in out.err
    assert str(mod.ROW_MAX_CHARS) in out.err
    assert "moved 1 row(s)" in out.out, "the move still happened"
    assert _targets(index) == [f"cycle-{NEW}.md"], "the oldest row (MID) moved out"

    # The over-cap row is the one that moves out: nothing is left to report.
    _write_index(index, [_over_cap_row(OLD, mod), _row(NEWEST)])
    assert mod.main([str(index), "--cap", "1", "--archive", str(archive)]) == 0
    out = capsys.readouterr()
    assert "over the per-row cap" not in out.err, (
        "the reading is of the text the move left behind — the plan's post-move "
        "index, which is what `apply_plan` wrote and `measure_on_disk` verified — "
        "so the row that was over the cap has already left: a reading taken of the "
        "index as it stood *before* the move would name a violation this very run "
        "has removed"
    )
    assert "moved 1 row(s)" in out.out


def test_nothing_to_move_still_reports_the_row_length_rule(tmp_path, mod, capsys):
    """The other exit a cycle can reach: no move, but the drift is still there."""
    index = tmp_path / "MEMORY.md"
    _write_index(index, [_over_cap_row(NEW, mod)])

    assert mod.main([str(index), "--cap", "50"]) == 0
    assert "over the per-row cap" in capsys.readouterr().err

def test_check_mode_answers_from_one_snapshot(tmp_path, mod, monkeypatch, capsys):
    """The count and the rule list must describe the same file.

    The index has other writers — every task's cycles append to the same one — which is
    why the move is a compare-and-swap and why `changed_since_planned` exists. `--check`
    read the file **twice**: the printed count came from the first read and the rules
    from the second, so a row appended in between put two snapshots in one answer, and
    the two lines of that answer contradicted each other (`3 cycle row(s) …` beside
    `VIOLATION: 4 cycle rows, over the cap 2`).

    The file here is never written between the reads: the second read is made to return
    a different text, which is what a parallel writer's append looks like from inside a
    process whose other read already happened.
    """
    index = tmp_path / "MEMORY.md"
    before = _write_index(index, [_row(NEW), _row(MID), _row(OLD)])
    after = _write_index(index, [_row(NEW), _row(MID), _row(OLD), _row(NEWEST)])

    real_read = Path.read_text
    seen: list[str] = []

    def racing_read(self, *args, **kwargs):
        text = real_read(self, *args, **kwargs)
        if self != index:
            return text
        seen.append(text)
        return before if len(seen) == 1 else after

    monkeypatch.setattr(Path, "read_text", racing_read)
    assert mod.main([str(index), "--cap", "2", "--check"]) == 1
    out = capsys.readouterr().out

    counted = int(re.search(r"^(\d+) cycle row\(s\)", out, re.M).group(1))
    violated = int(re.search(r"^VIOLATION: (\d+) cycle rows", out, re.M).group(1))
    assert counted == violated, (
        f"the answer describes two snapshots: it counts {counted} cycle row(s) and then "
        f"reports {violated} over the cap\n{out}"
    )
    assert len(seen) == 1, (
        f"the index was read {len(seen)} times; one answer must come from one snapshot"
    )


def _topic_row(i: int) -> str:
    """A readable row that is not a cycle row - bulk for an over-cap index."""
    return f"- [topic {i}](topic-{i}.md) - {'x' * 90}\n"


def _capped(index: Path) -> list[str]:
    """The rows the daemon's embed cap would keep - the oracle, not a re-derivation.

    Asked of the production function, because the reading's whole claim is that it
    reports *that* cut: a test that re-implemented the cap would agree with a second
    implementation while both drifted from the daemon.
    """
    from emrg.server.daemon import EmrgServer

    capped = EmrgServer._cap_memory_index(None, index)
    return [line for line in capped.splitlines() if line.startswith("- [")]


def test_the_reading_names_the_rows_the_cap_does_not_embed(tmp_path, mod, capsys):
    """An index past the embed cap: the rows of its tail are not what a reader sees."""
    index = tmp_path / "MEMORY.md"
    rows = [_row(OLD)] + [_topic_row(i) for i in range(600)] + [_row(NEWEST)]
    _write_index(index, rows)
    assert len(index.read_text(encoding="utf-8")) > mod.INDEX_SIZE_WARN, (
        "the fixture must be past the cap, or this test measures nothing"
    )

    assert mod.main([str(index), "--cap", "50", "--check"]) == 0, (
        "an index past the embed cap is not a row-rule violation"
    )
    out = capsys.readouterr().out

    capped = _capped(index)
    dropped = len(rows) - len(capped)
    assert dropped > 0, "the oracle must find rows the cap drops"
    assert f"{dropped} of {len(rows)} row(s) are past the cut" in out
    assert f"last row embedded: {capped[-1][:120]}" in out
    assert f"newest cycle row {NEWEST}: not embedded" in out
    assert "(a reading, not a rule" in out


def test_the_reading_reports_an_embedded_newest_row_when_it_is_one(tmp_path, mod, capsys):
    """The other direction: a reading that always says 'not embedded' proves nothing."""
    index = tmp_path / "MEMORY.md"
    rows = [_row(NEWEST)] + [_topic_row(i) for i in range(600)] + [_row(OLD)]
    _write_index(index, rows)

    assert mod.main([str(index), "--cap", "50", "--check"]) == 0
    out = capsys.readouterr().out

    capped = _capped(index)
    assert len(capped) < len(rows), "the fixture must still drop rows"
    assert _row(NEWEST).strip()[:120] in capped, "the oracle must keep the newest row"
    assert f"newest cycle row {NEWEST}: embedded" in out


def test_an_index_within_the_cap_says_so(tmp_path, mod, capsys):
    index = tmp_path / "MEMORY.md"
    _write_index(index, [_row(NEWEST), _row(OLD)])

    assert mod.main([str(index), "--cap", "50", "--check"]) == 0
    out = capsys.readouterr().out

    assert "the whole index is embedded" in out
    assert "past the cut" not in out


def test_the_reading_asks_the_cap_rather_than_cutting_the_text_itself(
    tmp_path, mod, capsys, monkeypatch
):
    """The reading's claim is that it reports *that* cut, so it has to ask it.

    A local copy of the three-line rule would pass every assertion above - the two
    agree until the cap's line-boundary handling changes - and then diverge in
    silence, with the copy being what `--check` reports. Spying on the production
    method is what separates "asked" from "agrees today".
    """
    from emrg.server import daemon

    index = tmp_path / "MEMORY.md"
    _write_index(index, [_topic_row(i) for i in range(600)] + [_row(NEWEST)])

    asked: list[Path] = []
    real = daemon.EmrgServer._cap_memory_index

    def spy(self, path):  # noqa: ANN001 - mirrors the method it wraps
        asked.append(path)
        return real(self, path)

    monkeypatch.setattr(daemon.EmrgServer, "_cap_memory_index", spy)

    assert mod.main([str(index), "--cap", "50", "--check"]) == 0
    capsys.readouterr()
    assert asked == [index], (
        "the reading must ask the cap what it keeps, not cut the text itself"
    )


def test_the_reading_is_printed_under_a_violation_too(tmp_path, mod, capsys):
    """`--check` reports both questions in one run, whichever way each of them goes."""
    index = tmp_path / "MEMORY.md"
    stamps = [
        (datetime(2026, 9, 1) + timedelta(hours=i)).strftime("%Y%m%d-%H%M%S")
        for i in range(60)
    ]
    rows = [_row(stamp) for stamp in stamps] + [_topic_row(i) for i in range(600)]
    _write_index(index, rows)

    assert mod.main([str(index), "--cap", "50", "--check"]) == 1, (
        "60 cycle rows are over the 50-row cap"
    )
    out = capsys.readouterr().out

    assert "VIOLATION" in out
    assert "embed cap:" in out, "the reading must not be hidden by the violation"


def test_the_embed_reading_keeps_describing_the_rows_the_count_used(
    tmp_path, mod, monkeypatch, capsys
):
    """The reading is one answer with the count, even though the cap reads the file.

    Over the cap the kept prefix comes from the cap's own read - it takes a path, which
    is what "asked, not copied" costs - so the two reads are one writer apart in the
    ordinary case: a cycle appending to the same index. The size it prints must be the
    size of the text the count above came from, not of a second snapshot, and an append
    cannot move the head (both reads cover the same first `INDEX_SIZE_WARN` characters),
    so the printed "N of M row(s) are past the cut" must still describe the rows that
    count used. Both reads are made to return different texts here, which is what a
    parallel writer's append looks like from inside the process.
    """
    index = tmp_path / "MEMORY.md"
    before = _write_index(index, [_topic_row(i) for i in range(600)] + [_row(NEWEST)])
    after = before + _topic_row(999)  # append to the text: a writer that appends
    index.write_text(after, encoding="utf-8")
    assert len(after) > len(before), "the appended row must change the size"

    real_read = Path.read_text
    seen: list[int] = []

    def racing_read(self, *args, **kwargs):
        text = real_read(self, *args, **kwargs)
        if self != index:
            return text
        seen.append(1)
        return before if len(seen) == 1 else after

    monkeypatch.setattr(Path, "read_text", racing_read)
    assert mod.main([str(index), "--cap", "50", "--check"]) == 0
    out = capsys.readouterr().out

    assert len(seen) > 1, "the fixture must exercise the cap's own read, or this is vacuous"
    assert f"embed cap: {len(before)} char(s)" in out, (
        "the reading reported a size from a snapshot the count above did not use - a "
        f"second read of the file rather than the one `--check` already made\n{out}"
    )
    counted_rows = int(re.search(r"cycle row\(s\) of (\d+) row\(s\)", out).group(1))
    reading = re.search(r"(\d+) of (\d+) row\(s\) are past the cut", out)
    assert reading, out
    dropped, described_rows = (int(reading.group(1)), int(reading.group(2)))
    assert dropped > 0, "the fixture must drop rows, or the numbers are both 0"
    assert described_rows == counted_rows, (
        f"the reading describes {described_rows} row(s) while the count above used "
        f"{counted_rows}: one answer, two snapshots\n{out}"
    )


def test_the_reading_names_a_snapshot_that_moved_under_it(tmp_path, mod, monkeypatch, capsys):
    """The one racing case that changes the answer, said rather than number-printed.

    An append leaves the head intact, so the reading still describes the rows the count
    used (the test above). A writer that removes rows from the **head** does not: the
    cut moves relative to those rows, and the kept prefix no longer lines up with them.
    The reading compares the two and refuses to state a number that describes neither
    file - which is the shape a move (this tool's own writer) leaves behind.
    """
    index = tmp_path / "MEMORY.md"
    before = _write_index(index, [_topic_row(i) for i in range(600)] + [_row(NEWEST)])
    # A move removes *old* rows: the same file, its head shortened.
    after = _write_index(index, [_topic_row(i) for i in range(200, 600)] + [_row(NEWEST)])

    real_read = Path.read_text
    seen: list[int] = []

    def racing_read(self, *args, **kwargs):
        text = real_read(self, *args, **kwargs)
        if self != index:
            return text
        seen.append(1)
        return before if len(seen) == 1 else after

    monkeypatch.setattr(Path, "read_text", racing_read)
    assert mod.main([str(index), "--cap", "50", "--check"]) == 0
    out = capsys.readouterr().out

    assert len(seen) > 1, "the fixture must exercise the cap's own read, or this is vacuous"
    assert "changed between the two reads" in out, (
        f"the reading stated a cut for a snapshot its own count did not use\n{out}"
    )
    assert "row(s) are past the cut" not in out, (
        f"the reading printed a drop count that describes neither snapshot\n{out}"
    )
    assert "embed cap:" in out, "the reading is still a reading: it must appear"


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


# --- a file whose rows are all in another shape is not a clean file ---------------
#
# The counted shape of the same false verdict: an index written as
# `| id | title | type | status | updated |`, which names no cycle and so is
# invisible to the cycle-id scan above. Measured 2026-09-24 on the `mem` project's
# index (161 row-like lines, the longest 3,141 chars, per-row cap 512):
#
#     --check  ->  "0 cycle row(s) of 0 row(s)"  "OK: the index respects the row rules"  rc 0
#     trim     ->  "nothing to move: the index is within its 50-row cap"                 rc 0
#
# Both are verdicts about rows that were never read, which is what exit 2 is for. The
# boundary is "no row parses at all", not "some line is not a row": the third test
# below pins the other direction, because the two indexes this host embeds both carry
# a line of another shape beside readable rows and must keep their verdicts.


def _table_index(path: Path) -> str:
    """A `mem`-shaped index: table rows, none of which names a cycle."""
    text = (
        "# Memory Index\n"
        "\n"
        "| ID | Title | Type | Status | Updated |\n"
        "|----|-------|------|--------|---------|\n"
        "| shenbi-workspace-001 | one | reference | active | 2026-07-22 |\n"
        "| e3a1b7c2 | two | reference | active | 2026-07-22 |\n"
        "| b4f2c8a1 | " + "x" * 600 + " | project | active | 2026-07-22 |\n"
    )
    path.write_text(text, encoding="utf-8")
    return text


def test_an_index_whose_rows_are_all_another_shape_is_not_called_clean(tmp_path, mod, capsys):
    """`0 cycle row(s) of 0 row(s)` plus `OK` is a healthy verdict on no question."""
    index = tmp_path / "MEMORY.md"
    _table_index(index)

    assert mod.main([str(index), "--check"]) == 2, (
        "the row rules were applied to no row, so nothing may be reported about them"
    )
    captured = capsys.readouterr()
    assert "not one row of the shape this tool reads" in captured.err, captured.err
    assert "MEMORY.md:3" in captured.err, "the first row-like line must be named"
    for wrong in ("OK: the index respects the row rules", "cycle row(s)", "VIOLATION"):
        assert wrong not in captured.out, f"nothing may be claimed: {captured.out}"


def test_the_trim_refuses_a_foreign_format_index_rather_than_moving_nothing(
    tmp_path, mod, capsys
):
    """`nothing to move` is the same claim from the other side: the cap was not read."""
    index = tmp_path / "MEMORY.md"
    before = _table_index(index)
    archive = tmp_path / "cycle-archive-X.md"

    assert mod.main([str(index), "--cap", "2", "--archive", str(archive)]) == 2

    assert index.read_text(encoding="utf-8") == before, "refusing means writing nothing"
    assert not archive.exists()
    captured = capsys.readouterr()
    assert "nothing to move" not in captured.out
    assert "not one row of the shape this tool reads" in captured.err, captured.err


def test_rows_of_another_shape_beside_readable_rows_keep_their_verdict(tmp_path, mod, capsys):
    """The boundary: the refusal is "no row parses", not "some line is not a row".

    Both indexes this host embeds carry such lines (a table row in one, a prose bullet
    in the other), and a rule that fired on them would refuse the files it exists to
    maintain. So a mixed index answers from what it can read - here the readable rows
    are over a cap of two, which is the verdict that must survive.
    """
    index = tmp_path / "MEMORY.md"
    text = _write_index(
        index,
        [
            "| not-a-row | of this tool |\n",
            _row(NEW),
            _row(MID),
            _row(OLD),
        ],
    )
    assert mod.parse_rows(text), "the readable rows are the ones that count"

    assert mod.main([str(index), "--cap", "2", "--check"]) == 1, (
        "three readable rows over a cap of two is a violation, not an unmeasurable file"
    )
    out = capsys.readouterr().out
    assert "3 cycle row(s) of 3 row(s)" in out, out


def test_an_index_with_no_rows_at_all_is_still_clean(tmp_path, mod, capsys):
    """The other control: having no rows is not the same as having unreadable ones."""
    index = tmp_path / "MEMORY.md"
    index.write_text("# Index\n\n> notes only, no rows yet\n", encoding="utf-8")

    assert mod.main([str(index), "--check"]) == 0
    assert "OK: the index respects the row rules" in capsys.readouterr().out


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
