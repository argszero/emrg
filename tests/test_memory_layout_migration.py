"""A store written before the `ref/` layout existed must still answer for its files.

The readers were moved to `ref/` and the files were not moved with them, so a directory
created before that change lists nothing: `list()` empty, `count` zero, and every update,
delete, merge or promotion answering "not found" for an id that is on disk. These tests
hold the migration that closes that gap, and each one is a property the fix has to keep
rather than a restatement of the code:

- it happens on construction, so no caller can forget it (the daemon builds a store per
  command, against a working directory no startup hook could enumerate);
- a retired file goes to `archive/` and an active one to `ref/`, because that split is the
  whole reason the layout exists;
- it is idempotent and resumable, because a move of hundreds of files can stop part-way;
- it never overwrites a destination, and it keeps an unparseable file in use rather than
  filing it away;
- and the index survives: `rebuild_index()` on a store that was flat until a moment ago
  must still name every memory it named before, which is the data-loss shape this closes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emrg.memory import (
    MEMORY_ARCHIVE_DIRNAME,
    MEMORY_INDEX_NAME,
    MEMORY_REF_DIRNAME,
    MemoryStore,
)

FLAT_MEMORY = """---
id: {mem_id}
event_at: 2026-08-27T11:30:45Z
created_at: 2026-08-27T11:48:00Z
updated_at: 2026-08-27T11:48:00Z
type: task
scope: agent
status: {status}
---

# {title}

Body text that must survive the move.
"""


def flat_store(root: Path) -> Path:
    """A memory directory in the pre-`ref/` shape: `MEMORY.md` and its files side by side."""
    directory = root / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MEMORY_INDEX_NAME).write_text("# Memory Index\n", encoding="utf-8")
    return directory


def write_flat(
    directory: Path,
    name: str,
    mem_id: str = "a1b2c3d4",
    status: str = "active",
    title: str = "A memory",
) -> Path:
    path = directory / name
    path.write_text(
        FLAT_MEMORY.format(mem_id=mem_id, status=status, title=title), encoding="utf-8"
    )
    return path


def test_a_flat_store_is_migrated_on_construction_and_then_lists_its_files(tmp_path: Path):
    """The defect itself: before the migration these two files were invisible."""
    directory = flat_store(tmp_path)
    write_flat(directory, "task-a.md", "aaaaaaaa", title="Alpha")
    write_flat(directory, "task-b.md", "bbbbbbbb", title="Beta")

    store = MemoryStore(directory, "agent")

    assert [mem.id for mem in store.list()] == ["aaaaaaaa", "bbbbbbbb"]
    assert store.count == 2
    assert (directory / MEMORY_REF_DIRNAME / "task-a.md").is_file()
    # Nothing is left behind in the old place.
    assert not (directory / "task-a.md").exists()


def test_a_retired_file_goes_to_archive_and_is_not_listed(tmp_path: Path):
    """`archive/` is the other half of the reason for the layout, not a place to skip."""
    directory = flat_store(tmp_path)
    write_flat(directory, "task-live.md", "11111111", status="active")
    write_flat(directory, "task-old.md", "22222222", status="superseded")
    write_flat(directory, "task-merged.md", "33333333", status="merged")

    store = MemoryStore(directory, "agent")

    assert [mem.id for mem in store.list()] == ["11111111"]
    assert (directory / MEMORY_REF_DIRNAME / "task-live.md").is_file()
    assert (directory / MEMORY_ARCHIVE_DIRNAME / "task-old.md").is_file()
    assert (directory / MEMORY_ARCHIVE_DIRNAME / "task-merged.md").is_file()


def test_an_unreadable_file_stays_in_use_rather_than_being_filed_away(tmp_path: Path):
    """Unreadable is not retired: the file goes where a reader can still reach it.

    The bytes here do not decode, which is the failure the module already documents for a
    file caught mid-rewrite by another writer. That is a `ValueError`, so the migration's
    fallback is the branch under test — not a status it read and then overrode.
    """
    directory = flat_store(tmp_path)
    (directory / "task-broken.md").write_bytes(b"---\nid: \xff\xfe\xff\n---\n\n# broken\n")

    store = MemoryStore(directory, "agent")

    assert (directory / MEMORY_REF_DIRNAME / "task-broken.md").is_file()
    assert not (directory / MEMORY_ARCHIVE_DIRNAME).exists()
    # And it is not silently turned into a memory: the store lists what parses.
    assert store.list() == []


def test_the_index_never_moves(tmp_path: Path):
    """The index is the directory's map, so it stays at the directory's top level."""
    directory = flat_store(tmp_path)
    write_flat(directory, "task-a.md")

    MemoryStore(directory, "agent")

    assert (directory / MEMORY_INDEX_NAME).is_file()
    assert not (directory / MEMORY_REF_DIRNAME / MEMORY_INDEX_NAME).exists()


def test_a_second_pass_moves_nothing_and_finishes_a_half_migrated_store(tmp_path: Path):
    """Idempotence, and the resumable case a move of hundreds of files actually produces."""
    directory = flat_store(tmp_path)
    write_flat(directory, "task-already.md", "aaaaaaaa")
    write_flat(directory, "task-still-flat.md", "bbbbbbbb")

    # The state a pass that stopped part-way leaves: one file moved, one not.
    ref = directory / MEMORY_REF_DIRNAME
    ref.mkdir()
    (directory / "task-already.md").rename(ref / "task-already.md")

    store = MemoryStore(directory, "agent")

    assert (ref / "task-still-flat.md").is_file()
    assert [mem.id for mem in store.list()] == ["aaaaaaaa", "bbbbbbbb"]
    # Nothing left to move, and a store that has nothing to do says so.
    assert store.migrate_flat_layout() == 0


def test_a_store_nobody_has_written_to_is_left_alone(tmp_path: Path):
    """An empty store must not be given a `ref/` for visiting it."""
    directory = tmp_path / "memory"
    directory.mkdir()
    (directory / MEMORY_INDEX_NAME).write_text("# Memory Index\n", encoding="utf-8")

    store = MemoryStore(directory, "agent")

    assert store.list() == []
    assert not (directory / MEMORY_REF_DIRNAME).exists()


def test_an_existing_destination_is_named_and_not_overwritten(tmp_path: Path):
    """Two files with one name: the one already in use wins, and the other is left alone."""
    directory = flat_store(tmp_path)
    ref = directory / MEMORY_REF_DIRNAME
    ref.mkdir()
    write_flat(ref, "task-a.md", "aaaaaaaa", title="In use")
    write_flat(directory, "task-a.md", "bbbbbbbb", title="Flat")

    store = MemoryStore(directory, "agent")

    assert [mem.id for mem in store.list()] == ["aaaaaaaa"]
    assert (directory / "task-a.md").is_file(), "the loser must not be destroyed"
    assert "In use" in (ref / "task-a.md").read_text(encoding="utf-8")


def test_a_move_that_cannot_happen_leaves_the_file_where_it_was(tmp_path: Path, monkeypatch):
    """A read-only home answers "not migrated", never an exception out of a read."""
    directory = flat_store(tmp_path)
    write_flat(directory, "task-a.md")

    def refuse(self: Path, target):  # noqa: ANN001 - the signature Path.rename has
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "rename", refuse)

    store = MemoryStore(directory, "agent")

    assert (directory / "task-a.md").is_file()
    assert store.list() == []


def test_the_index_survives_a_rebuild_on_a_previously_flat_store(tmp_path: Path):
    """The data-loss shape: a rebuild must not rewrite the map from an empty `ref/`.

    `rebuild_index()` saves what it found on disk. On a store whose files are beside the
    index and whose `ref/` is empty, what it found is nothing — so before the migration
    this call replaced a map of every memory with an empty one, and the files that map
    named were still there, unread.
    """
    directory = tmp_path / "memory"
    store = MemoryStore(directory, "agent")
    alpha = store.create("task", "Alpha", "body of alpha")
    beta = store.create("task", "Beta", "body of beta")

    # Put the store back into the shape it had before the layout existed.
    for path in list(store.ref_dir.glob("*.md")):
        path.rename(directory / path.name)
    store.ref_dir.rmdir()
    assert (directory / alpha.filename).is_file()

    reopened = MemoryStore(directory, "agent")
    reopened.rebuild_index()

    text = (directory / MEMORY_INDEX_NAME).read_text(encoding="utf-8")
    # An index row links its file by name, so that is what a lost map loses.
    assert f"- [Alpha]({alpha.filename})" in text
    assert f"- [Beta]({beta.filename})" in text


def test_a_session_store_is_migrated_the_same_way(tmp_path: Path):
    """Both scopes go through the one constructor, so both are repaired by it."""
    from emrg.memory import SessionMemoryStore

    session_dir = tmp_path / "sessions" / "s_1"
    memory_dir = session_dir / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / MEMORY_INDEX_NAME).write_text("# Memory Index\n", encoding="utf-8")
    write_flat(memory_dir, "task-a.md", "aaaaaaaa")

    store = SessionMemoryStore(session_dir)

    assert [mem.id for mem in store.list()] == ["aaaaaaaa"]
    assert (memory_dir / MEMORY_REF_DIRNAME / "task-a.md").is_file()


@pytest.mark.parametrize("status", ["superseded", "merged"])
def test_every_retired_status_is_filed_away(tmp_path: Path, status: str):
    """The split is a set, so a status added to it later is covered without a new test."""
    directory = flat_store(tmp_path)
    write_flat(directory, "task-a.md", status=status)

    MemoryStore(directory, "agent")

    assert (directory / MEMORY_ARCHIVE_DIRNAME / "task-a.md").is_file()
