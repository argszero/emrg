"""The rant ledger's target key moves from `project` to `agent`, once, at startup.

The move is code and not a reader-side fallback because of what the reader does with a
row it does not recognise: `_normalize_rant` builds every row as
`{k: raw.get(k) for k in _RANT_FIELDS}`, so a row still carrying `project` comes back
with `agent: None` — and `_write_rants` is the only writer while `append_rant` is a
read-modify-write, so the next submission rewrites the whole ledger and drops every
pre-rename target. A fallback in the reader would keep both spellings alive instead,
which is the ambiguity the rename exists to remove.

So the arms below are ordered by what they cost if they regress: the first proves a
pre-rename ledger keeps its targets **across a read-and-write cycle** (the data loss),
the second is its control (a row that already carries `agent`), and the rest pin the
properties the move promises — position preserved, a no-op that touches nothing, an
absent ledger that is not created, a stale second spelling that is dropped rather than
kept, and a legacy array row that is left to `_normalize_rant`'s positional mapping.

Every arm drives the real store over a `tmp_path` ledger; none reaches the host's own
`~/.emrg/rants.jsonl`.
"""

from __future__ import annotations

import json

import pytest

from emrg.server import rants as rants_module
from emrg.server.rants import (
    append_rant,
    list_rants,
    migrate_rant_agent_field,
    update_rant,
)


def _write_lines(path, rows):
    """Write rows as raw JSON lines, so a test controls the key order exactly."""
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _rows(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


PRE_RENAME_ROW = {
    "timestamp": "2026-09-01T10:00:00+08:00",
    "project": "emrg",
    "status": "pending",
    "progress": "",
    "completed": None,
    "message": "a pre-rename row",
}


# ── the data loss the migration exists to prevent ─────────────────────────────


def test_a_pre_rename_row_keeps_its_target_across_a_read_and_write_cycle(tmp_path):
    """The defect: read-modify-write drops `project`'s value and never moves it."""
    log = tmp_path / "rants.jsonl"
    _write_lines(log, [PRE_RENAME_ROW])

    # Unmigrated, the reader answers `agent: None` and the writer then persists that
    # loss — this is the arm the migration has to make unreachable, measured first.
    assert list_rants(log)[0]["agent"] is None

    assert migrate_rant_agent_field(log) == 1

    # And the same read-modify-write now carries the target through.
    append_rant(log, "a post-rename row", agent="emrg")
    entries = list_rants(log)
    assert [e["agent"] for e in entries] == ["emrg", "emrg"]
    assert [e["message"] for e in entries] == ["a pre-rename row", "a post-rename row"]

    # The old key is gone from the file, not merely ignored by the reader.
    assert all("project" not in row for row in _rows(log))


def test_a_row_that_already_carries_agent_is_the_control_arm(tmp_path):
    """The reading discriminates: a row the rename already reached is untouched."""
    log = tmp_path / "rants.jsonl"
    post_rename = dict(PRE_RENAME_ROW)
    post_rename.pop("project")
    post_rename["agent"] = "emrg"
    _write_lines(log, [post_rename])

    before = log.read_text(encoding="utf-8")
    assert migrate_rant_agent_field(log) == 0
    assert log.read_text(encoding="utf-8") == before
    assert list_rants(log)[0]["agent"] == "emrg"


# ── the properties the move promises ─────────────────────────────────────────


def test_the_key_moves_at_its_own_position_and_other_keys_are_verbatim(tmp_path):
    """Every other key, its order and its value travel across unchanged."""
    log = tmp_path / "rants.jsonl"
    _write_lines(log, [PRE_RENAME_ROW])
    assert migrate_rant_agent_field(log) == 1

    moved = _rows(log)[0]
    assert list(moved) == ["timestamp", "agent", "status", "progress", "completed", "message"]
    expected = dict(PRE_RENAME_ROW)
    expected.pop("project")
    expected["agent"] = "emrg"
    assert moved == {k: expected[k] for k in moved}


def test_a_ledger_with_nothing_to_move_is_not_opened_for_writing(tmp_path, monkeypatch):
    """A no-op writes nothing at all — not a re-serialization of the same rows."""
    log = tmp_path / "rants.jsonl"
    log.write_text('{"timestamp": "t", "agent": "emrg"}\n', encoding="utf-8")
    mtime_before = log.stat().st_mtime_ns

    def _explode(*args, **kwargs):
        raise AssertionError("the migration opened the ledger for writing with nothing to move")

    monkeypatch.setattr(rants_module.os, "replace", _explode)
    assert migrate_rant_agent_field(log) == 0
    assert log.stat().st_mtime_ns == mtime_before


def test_an_absent_ledger_is_not_created(tmp_path):
    """This moves a ledger; it never seeds one."""
    log = tmp_path / "rants.jsonl"
    assert migrate_rant_agent_field(log) == 0
    assert not log.exists()


def test_a_stale_second_spelling_is_dropped_rather_than_kept(tmp_path):
    """A row carrying both keeps `agent` and loses `project`."""
    log = tmp_path / "rants.jsonl"
    _write_lines(log, [dict(PRE_RENAME_ROW, agent="the-new-value")])
    assert migrate_rant_agent_field(log) == 1

    moved = _rows(log)[0]
    assert moved["agent"] == "the-new-value"
    assert "project" not in moved


def test_a_legacy_array_row_and_an_unparsable_line_are_left_alone(tmp_path):
    """The array form already maps by position; a corrupt line is not this call's job."""
    log = tmp_path / "rants.jsonl"
    array_row = [
        "2026-08-18T10:00:00+08:00",
        "emrg",
        "pending",
        "",
        None,
        "an array row",
    ]
    log.write_text(
        json.dumps(array_row, ensure_ascii=False) + "\n" + "not json at all\n",
        encoding="utf-8",
    )
    before = log.read_text(encoding="utf-8")

    assert migrate_rant_agent_field(log) == 0
    assert log.read_text(encoding="utf-8") == before
    # The array row still reads as a row with its target in the second slot.
    assert list_rants(log)[0]["agent"] == "emrg"


def test_a_ledger_that_cannot_be_read_is_reported_and_left_where_it_lies(tmp_path, caplog):
    """The caller is starting up: a refusal here must not be a crash."""
    log = tmp_path / "rants.jsonl"
    _write_lines(log, [PRE_RENAME_ROW])
    log.chmod(0o000)
    try:
        with caplog.at_level("WARNING"):
            assert migrate_rant_agent_field(log) == 0
        assert any("not migrated" in record.message for record in caplog.records)
    finally:
        log.chmod(0o644)


def test_the_store_still_works_on_a_migrated_ledger(tmp_path):
    """The migration leaves the store's own verbs working — update included."""
    log = tmp_path / "rants.jsonl"
    _write_lines(log, [PRE_RENAME_ROW])
    migrate_rant_agent_field(log)

    ok, why = update_rant(log, PRE_RENAME_ROW["timestamp"], progress="worked on")
    assert ok, why
    entries = list_rants(log)
    assert entries[0]["progress"] == "worked on"
    assert entries[0]["agent"] == "emrg"


def test_only_the_daemon_process_entry_calls_the_migration():
    """The migration has one reachable call site, and it is the one no test drives.

    A second call site — a store method, a reader, the submit tool — would make the
    move happen wherever that path ran, which is the shape that rewrites a host's live
    feedback channel from inside a test run. The same guard
    `tests/test_task_config_key_migration.py` puts on the task table's migration.
    """
    from pathlib import Path

    import emrg.server.rants as mod

    root = Path(mod.__file__).resolve().parents[2]
    hits = []
    for path in sorted((root / "emrg").rglob("*.py")):
        if "migrate_rant_agent_field()" in path.read_text(encoding="utf-8"):
            hits.append(path.relative_to(root).as_posix())

    assert hits == ["emrg/server/__main__.py"]
