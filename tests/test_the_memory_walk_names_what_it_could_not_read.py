"""The memory directory has one walk, and it names what it could not read.

`MemoryStore` reads the memory directory in three places, and they disagreed.
`_scan` (which serves `get_with_reason`) has long collected the files it could not
read and returned them as data; `list()` — what the daemon's `memories_list` frame
and the `/memory` panel show — and `_rebuild_index()` (whose result is then
*persisted*) dropped them with a silent `logger.debug`.

So a memory that was on disk could be missing from the panel, and a rebuild could
delete its row from the index: a shorter list is indistinguishable from a shorter
directory. The walk's own `except (OSError, ValueError)` is what a concurrent writer
trips — `MemoryIndex.save` truncates before it writes, and a cut inside a multi-byte
character is a `UnicodeDecodeError` (a `ValueError`) — and the same race measured
through `_scan` on 2026-09-24 answered "not found" for an on-disk memory on 68 of
120 requests.

The reading is `list_with_reason()`, which splits the pair the way `get_with_reason`
does for one id; `list()` stays the bare list; and `rebuild_index()` **fails closed**
rather than persisting an index it knows is incomplete.
"""

import pytest

from emrg.memory import ProjectMemoryStore


# Bytes that are not valid UTF-8, so `Path.read_text(encoding="utf-8")` raises
# `UnicodeDecodeError` — the shape a reader sees mid-write, and the one `from_file`
# turns into the tolerant-catch case.
_NOT_UTF8 = b"\xff\xfe\xfd not utf-8 \xff"


@pytest.fixture
def store(tmp_path):
    return ProjectMemoryStore(tmp_path)


def _break_one_file(store, name="broken.md"):
    path = store.directory / name
    path.write_bytes(_NOT_UTF8)
    return path


class TestTheListingNamesWhatItCouldNotRead:
    def test_list_still_returns_the_readable_memories(self, store):
        """The bare list is unchanged: what parses is what it returns."""
        keep = store.create("project", "Keep me", "body")
        _break_one_file(store)

        assert [m.id for m in store.list()] == [keep.id]

    def test_list_with_reason_names_the_file_it_could_not_read(self, store):
        store.create("project", "Keep me", "body")
        _break_one_file(store)

        memories, note = store.list_with_reason()

        assert [m.title for m in memories] == ["Keep me"]
        assert "broken.md" in note, "the walk did not name the file it skipped"
        assert "UnicodeDecodeError" in note, (
            "the note names the file but not what went wrong reading it"
        )

    def test_the_note_is_empty_when_every_file_was_read(self, store):
        """Control leg: an empty note means "this is all of them"."""
        store.create("project", "Keep me", "body")

        memories, note = store.list_with_reason()

        assert [m.title for m in memories] == ["Keep me"]
        assert note == ""

    def test_list_is_the_parsing_half_of_list_with_reason(self, store):
        """The two may not disagree about what is here."""
        store.create("project", "One", "a")
        store.create("project", "Two", "b")
        _break_one_file(store)

        assert store.list() == store.list_with_reason()[0]

    def test_a_filter_does_not_swallow_the_note(self, store):
        """A filter narrows the rows, never the answer about what was read."""
        store.create("decision", "A decision", "body")
        store.create("reference", "A note", "body")
        _break_one_file(store)

        memories, note = store.list_with_reason(type_filter="decision")

        assert [m.title for m in memories] == ["A decision"]
        assert "broken.md" in note


class TestTheRebuildFailsClosed:
    def test_the_rebuild_does_not_drop_a_row_for_a_file_it_could_not_read(self, store):
        """The loss itself, measured 2026-10-04 on master: the row outlives it.

        `Fragile` is indexed, then its file becomes unreadable (the torn-read shape a
        concurrent `MemoryIndex.save` produces). On master the rebuild rewrote the
        index without it — the file stays on disk and nothing indexes it, so the
        memory vanishes from the prompt every session carries, and `rebuild_index()`
        returned `None` rather than saying so.
        """
        store.create("project", "Keep me", "body")
        frail = store.create("project", "Fragile", "body")
        assert "Fragile" in store.index_path.read_text(encoding="utf-8")
        (store.directory / frail.filename).write_bytes(_NOT_UTF8)

        store.rebuild_index()

        assert "Fragile" in store.index_path.read_text(encoding="utf-8"), (
            "the rebuild rewrote the index without the memory it could not read"
        )

    def test_rebuild_refuses_to_persist_an_incomplete_index(self, store):
        """A rebuild that could not read a file may not answer for the index."""
        store.create("project", "Keep me", "body")
        assert store.rebuild_index() == ""
        before = store.index_path.read_bytes()
        _break_one_file(store)

        note = store.rebuild_index()

        assert "broken.md" in note
        assert store.index_path.read_bytes() == before, (
            "the rebuild persisted an index that dropped the file it could not read "
            "— the memory stays on disk and nothing indexes it"
        )

    def test_rebuild_writes_and_return_empty_when_it_read_every_file(self, store):
        """Control leg: the rebuild still does its job on a clean directory."""
        store.create("project", "Keep me", "body")
        store.index_path.write_text("stale index\n", encoding="utf-8")

        assert store.rebuild_index() == ""

        assert "Keep me" in store.index_path.read_text(encoding="utf-8")

    def test_the_rebuild_keeps_every_row_it_can_read(self, store):
        store.create("project", "One", "a")
        store.create("project", "Two", "b")
        store.index_path.write_text("stale index\n", encoding="utf-8")

        store.rebuild_index()

        text = store.index_path.read_text(encoding="utf-8")
        assert "One" in text and "Two" in text


class TestThePerIdReaderIsUnchanged:
    def test_get_with_reason_still_names_the_unreadable_file(self, store):
        """The reading this pair was modelled on: it must not regress."""
        _break_one_file(store)

        mem, reason = store.get_with_reason("nonexistent-id")

        assert mem is None
        assert "broken.md" in reason

    def test_get_with_reason_is_silent_for_a_measured_absence(self, store):
        """Control leg: nothing unreadable means no reason to give."""
        store.create("project", "Keep me", "body")

        mem, reason = store.get_with_reason("nonexistent-id")

        assert mem is None
        assert reason == ""
