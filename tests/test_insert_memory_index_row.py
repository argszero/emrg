"""Tests for scripts/insert-memory-index-row.py.

The tool exists because inserting a row into a memory index by *position* failed
three times in one session: the session index is newest-first while the evolution
index is oldest-first, and a trim that dropped "the first rows" of a newest-first
index deleted the ten newest — every row it was supposed to keep. These tests pin
the properties that make the failure impossible rather than unlikely: the order is
read from the index, the position is counted from ids, the trim is the archiver's
(which picks the oldest by id), and a failure leaves the file untouched.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "insert-memory-index-row.py"


def _load():
    spec = importlib.util.spec_from_file_location("insert_row", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


mod = _load()

HEADER = "# Index\n\n> notes about this index\n"


def row(ts: str, title: str = "cap") -> str:
    return f"- [cycle-{ts}](cycle-{ts}.md) — {title} row"


def write_index(tmp_path: Path, rows: list[str], name: str = "MEMORY.md") -> Path:
    p = tmp_path / name
    p.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_the_parser_reads_an_id_from_the_linked_filename():
    assert mod.row_id(row("20260914-180702")) == "20260914-180702"
    assert mod.row_id("- [x](../memory/cycle-20260914-180702.md) — t") == "20260914-180702"
    # a line that merely mentions a cycle id is not a row
    assert mod.row_id("Prose mentioning cycle-20260914-180702 without a link") is None
    assert mod.row_id("| a | 20260914-180702 |") is None


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([row("20260914-100000"), row("20260914-110000")], "ascending"),
        ([row("20260914-110000"), row("20260914-100000")], "descending"),
        ([row("20260914-100000")], "ascending"),
    ],
)
def test_the_order_is_read_from_the_index(rows, expected):
    assert mod.detect_order([(i, mod.row_id(r)) for i, r in enumerate(rows)]) == expected


def test_an_inconsistently_ordered_index_is_unanswerable():
    rows = [row("20260914-100000"), row("20260914-120000"), row("20260914-110000")]
    with pytest.raises(mod.Unanswerable):
        mod.detect_order([(i, mod.row_id(r)) for i, r in enumerate(rows)])


def test_a_duplicate_row_is_unanswerable_rather_than_inserted_twice():
    rows = [row("20260914-100000"), row("20260914-100000")]
    with pytest.raises(mod.Unanswerable):
        mod.detect_order([(i, mod.row_id(r)) for i, r in enumerate(rows)])


def test_insert_lands_among_the_rows_when_it_is_not_the_newest():
    rows = [(0, "20260914-100000"), (1, "20260914-120000")]
    assert mod.insert_position(rows, "ascending", "20260914-110000") == 1
    assert mod.insert_position(rows, "ascending", "20260914-130000") == 2
    desc = [(0, "20260914-120000"), (1, "20260914-100000")]
    assert mod.insert_position(desc, "descending", "20260914-110000") == 1
    assert mod.insert_position(desc, "descending", "20260914-130000") == 0


def test_newest_first_index_keeps_the_new_row_at_the_top(tmp_path):
    rows = [row("20260914-120000"), row("20260914-110000"), row("20260914-100000")]
    index = write_index(tmp_path, rows)
    new = tmp_path / "row.md"
    new.write_text(row("20260914-130000") + "\n", encoding="utf-8")

    proc = run(str(index), "--row-file", str(new), "--no-trim")
    assert proc.returncode == 0, proc.stderr
    ids = [mod.row_id(l) for l in index.read_text(encoding="utf-8").splitlines() if mod.row_id(l)]
    assert ids == ["20260914-130000", "20260914-120000", "20260914-110000", "20260914-100000"]
    assert index.read_text(encoding="utf-8").startswith(HEADER)


def test_oldest_first_index_appends_the_new_row(tmp_path):
    rows = [row("20260914-100000"), row("20260914-110000")]
    index = write_index(tmp_path, rows)
    new = tmp_path / "row.md"
    new.write_text(row("20260914-130000") + "\n", encoding="utf-8")

    proc = run(str(index), "--row-file", str(new), "--no-trim")
    assert proc.returncode == 0, proc.stderr
    ids = [mod.row_id(l) for l in index.read_text(encoding="utf-8").splitlines() if mod.row_id(l)]
    assert ids == ["20260914-100000", "20260914-110000", "20260914-130000"]


def test_reinserting_an_older_cycle_lands_where_that_cycle_belongs(tmp_path):
    """The point of counting by id: an old row is not a new top row."""
    rows = [row("20260914-130000"), row("20260914-110000")]
    index = write_index(tmp_path, rows)
    new = tmp_path / "row.md"
    new.write_text(row("20260914-120000") + "\n", encoding="utf-8")

    assert run(str(index), "--row-file", str(new), "--no-trim").returncode == 0
    ids = [mod.row_id(l) for l in index.read_text(encoding="utf-8").splitlines() if mod.row_id(l)]
    assert ids == ["20260914-130000", "20260914-120000", "20260914-110000"]


def test_a_duplicate_is_a_rule_violation_and_the_file_is_untouched(tmp_path):
    index = write_index(tmp_path, [row("20260914-120000"), row("20260914-110000")])
    before = index.read_bytes()
    new = tmp_path / "row.md"
    new.write_text(row("20260914-120000") + "\n", encoding="utf-8")

    proc = run(str(index), "--row-file", str(new), "--no-trim")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert index.read_bytes() == before
    assert not list(tmp_path.glob("*.bak-*")), "a refused insert must not leave a backup"


def test_dry_run_writes_nothing(tmp_path):
    index = write_index(tmp_path, [row("20260914-100000")])
    before = index.read_bytes()
    new = tmp_path / "row.md"
    new.write_text(row("20260914-110000") + "\n", encoding="utf-8")

    proc = run(str(index), "--row-file", str(new), "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert index.read_bytes() == before


def test_the_trim_drops_the_oldest_row_not_the_new_one(tmp_path):
    """The archiver is the one that trims, and it picks the oldest by cycle id."""
    rows = [row("20260914-130000"), row("20260914-120000"), row("20260914-110000")]
    index = write_index(tmp_path, rows)
    archive = tmp_path / "cycle-archive-20260914.md"
    new = tmp_path / "row.md"
    new.write_text(row("20260914-140000") + "\n", encoding="utf-8")

    proc = run(
        str(index), "--row-file", str(new), "--cap", "3", "--archive", str(archive)
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    ids = [mod.row_id(l) for l in index.read_text(encoding="utf-8").splitlines() if mod.row_id(l)]
    assert ids == ["20260914-140000", "20260914-130000", "20260914-120000"]
    assert "20260914-110000" in archive.read_text(encoding="utf-8")


def test_a_backup_of_the_index_is_written_before_it_is_changed(tmp_path):
    index = write_index(tmp_path, [row("20260914-100000")])
    new = tmp_path / "row.md"
    new.write_text(row("20260914-110000") + "\n", encoding="utf-8")

    assert run(str(index), "--row-file", str(new), "--no-trim").returncode == 0
    backups = list(tmp_path.glob("MEMORY.md.bak-*"))
    assert len(backups) == 1, backups
    assert backups[0].read_text(encoding="utf-8") == HEADER + "\n" + row("20260914-100000") + "\n"


def test_a_row_file_without_a_cycle_id_is_unanswerable(tmp_path):
    index = write_index(tmp_path, [row("20260914-100000")])
    new = tmp_path / "row.md"
    new.write_text("- [no id here](notes.md)\n", encoding="utf-8")

    proc = run(str(index), "--row-file", str(new), "--no-trim")
    assert proc.returncode == 2
    assert "no cycle id" in proc.stderr
