"""A task record's agent key: ``config.project`` → ``config.agent`` (issue #2531).

The migration moves a key in a file a host's daemon reads, so these tests hold two
separate things: that the move is faithful (nothing else travels, order included),
and that **only the daemon's process entry** performs it. The second matters more
than the first — a migration reachable from any path the suite drives would rewrite a
host's live ``tasks.yml`` under a daemon that is still reading the old key.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from emrg.server import scheduler as mod
from emrg.server.scheduler import migrate_task_config_keys, read_table

#: One table in the pre-rename shape, as a host's file carries it: the agent name
#: sits under `config.project` and nowhere else, and the surrounding fields are the
#: ones a real record has.
PRE_RENAME_SHAPE = [
    {"name": "emrg-task", "type": "evolution", "config": {"project": "emrg"},
     "interval": 600, "enabled": True},
    {"name": "journal-editor", "type": "journal", "config": {"project": "silicon-science-cs"},
     "interval": 1800, "enabled": True},
    {"name": "feishu-inbox", "type": "feishu", "config": {"project": "mem"},
     "interval": 300, "enabled": True},
]


def _write(path: Path, records: list[dict]) -> None:
    path.write_text(
        yaml.safe_dump(records, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def test_the_key_moves_and_nothing_else_travels(tmp_path):
    """The value arrives unchanged, at the key's own position, with the record intact."""
    path = tmp_path / "tasks.yml"
    _write(path, [{
        "name": "a", "type": "evolution",
        "config": {"project": "emrg", "repo": "acme/x", "role": "author"},
        "interval": 600, "enabled": True, "schedule": None,
    }])

    assert migrate_task_config_keys(path) == 1

    record = yaml.safe_load(path.read_text(encoding="utf-8"))[0]
    assert record["config"] == {"agent": "emrg", "repo": "acme/x", "role": "author"}
    assert list(record["config"]) == ["agent", "repo", "role"], "the key keeps its place"
    assert record["interval"] == 600
    assert record["enabled"] is True
    assert "project" not in record["config"]


def test_every_record_keeps_its_own_agent(tmp_path):
    """One record per task: no record may end up wearing another's name."""
    path = tmp_path / "tasks.yml"
    _write(path, PRE_RENAME_SHAPE)
    expected = [r["config"]["project"] for r in PRE_RENAME_SHAPE]

    assert migrate_task_config_keys(path) == len(PRE_RENAME_SHAPE)

    migrated = read_table(path)
    assert [r["config"]["agent"] for r in migrated] == expected
    assert [r["name"] for r in migrated] == [r["name"] for r in PRE_RENAME_SHAPE]
    assert [r["interval"] for r in migrated] == [r["interval"] for r in PRE_RENAME_SHAPE]
    assert all("project" not in r["config"] for r in migrated)


def test_a_second_call_is_a_no_op(tmp_path):
    """The migration is idempotent, so a restart does not rewrite the file again."""
    path = tmp_path / "tasks.yml"
    _write(path, PRE_RENAME_SHAPE)
    assert migrate_task_config_keys(path) == len(PRE_RENAME_SHAPE)
    before = path.read_bytes()

    assert migrate_task_config_keys(path) == 0
    assert path.read_bytes() == before


def test_a_record_already_carrying_agent_is_left_alone(tmp_path):
    path = tmp_path / "tasks.yml"
    _write(path, [{"name": "a", "type": "evolution", "config": {"agent": "emrg"},
                   "interval": 600, "enabled": True}])
    before = path.read_bytes()

    assert migrate_task_config_keys(path) == 0
    assert path.read_bytes() == before


def test_a_stale_project_key_is_dropped_rather_than_duplicated(tmp_path):
    """Both keys present: `agent` governs, and the old word does not survive as residue."""
    path = tmp_path / "tasks.yml"
    _write(path, [{"name": "a", "type": "evolution",
                   "config": {"agent": "correct", "project": "stale"},
                   "interval": 600, "enabled": True}])

    assert migrate_task_config_keys(path) == 1
    record = yaml.safe_load(path.read_text(encoding="utf-8"))[0]
    assert record["config"] == {"agent": "correct"}


def test_a_missing_file_is_not_created(tmp_path):
    """This moves an existing table; it never seeds one."""
    path = tmp_path / "tasks.yml"
    assert migrate_task_config_keys(path) == 0
    assert not path.exists()


def test_an_unreadable_table_is_left_exactly_where_it_lies(tmp_path):
    """A file this cannot parse is not a file to rewrite from a half-understood read."""
    path = tmp_path / "tasks.yml"
    path.write_text("- name: a\n  config: [unbalanced\n", encoding="utf-8")
    before = path.read_bytes()

    assert migrate_task_config_keys(path) == 0
    assert path.read_bytes() == before


def test_the_reader_never_writes(tmp_path):
    """`read_table` answers the record as written — old key and all — and touches nothing.

    This is the property that keeps a suite run off a host's live table: reading a
    record must never be the act that renames it.
    """
    path = tmp_path / "tasks.yml"
    _write(path, PRE_RENAME_SHAPE)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    records = read_table(path)

    assert records[0]["config"] == {"project": "emrg"}, "the reader reports what is written"
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_only_the_daemon_process_entry_calls_the_migration():
    """The migration has one reachable call site, and it is the one no test drives.

    A second call site — a scheduler method, a loader, a `read_table` — would make the
    rename happen wherever that path ran, which is the shape that rewrites a host's
    live file from inside a test run.
    """
    root = Path(mod.__file__).resolve().parents[2]
    hits = []
    for path in sorted((root / "emrg").rglob("*.py")):
        if "migrate_task_config_keys()" in path.read_text(encoding="utf-8"):
            hits.append(str(path.relative_to(root)))

    assert hits == ["emrg/server/__main__.py"]
