"""Tests for the EMRG memory module."""

import tempfile
from pathlib import Path

import pytest

from emrg.memory import (
    MemoryFile,
    MemoryIndex,
    ProjectMemoryStore,
    SessionMemoryStore,
    generate_id,
)


@pytest.fixture
def temp_cwd():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def project_store(temp_cwd):
    return ProjectMemoryStore(temp_cwd)


@pytest.fixture
def session_store(temp_cwd):
    session_dir = temp_cwd / ".emrg" / "sessions" / "s_test"
    session_dir.mkdir(parents=True)
    return SessionMemoryStore(session_dir)


class TestMemoryFile:
    def test_create_defaults(self):
        mem = MemoryFile()
        assert len(mem.id) == 8  # 4 bytes = 8 hex chars
        assert mem.type == "reference"
        assert mem.scope == "session"
        assert mem.status == "active"
        assert mem.title == ""

    def test_filename_derivation(self):
        mem = MemoryFile(type="decision", title="Use httpx")
        assert mem.filename == "decision-use-httpx.md"

    def test_filename_type_prefix_skipped_for_reference(self):
        mem = MemoryFile(type="reference", title="API docs")
        assert mem.filename == "api-docs.md"

    def test_from_text_parses_frontmatter(self):
        raw = """---
id: "a1b2c3d4"
event_at: "2026-07-10T09:00:00Z"
created_at: "2026-07-14T15:30:42Z"
updated_at: "2026-07-14T15:30:42Z"
type: "decision"
scope: "project"
status: "active"
---

# Use httpx

**What**: HTTP client."""
        mem = MemoryFile.from_text(raw)
        assert mem.id == "a1b2c3d4"
        assert mem.event_at == "2026-07-10T09:00:00Z"
        assert mem.type == "decision"
        assert mem.scope == "project"
        assert mem.title == "Use httpx"

    def test_from_text_no_frontmatter(self):
        mem = MemoryFile.from_text("Just some markdown body")
        assert mem.body == "Just some markdown body"

    def test_to_markdown_roundtrip(self):
        mem = MemoryFile(
            id="deadbeef",
            type="feedback",
            scope="project",
            title="Test title",
            body="Test body",
        )
        md = mem.to_markdown()
        assert 'id: "deadbeef"' in md
        assert 'type: "feedback"' in md
        assert "Test title" in md
        assert "Test body" in md

        # Roundtrip
        mem2 = MemoryFile.from_text(md)
        assert mem2.id == mem.id
        assert mem2.type == mem.type

    def test_to_markdown_adds_title_heading(self):
        mem = MemoryFile(title="API Key Config", body="Use env vars", type="project")
        md = mem.to_markdown()
        assert "# API Key Config" in md


class TestMemoryIndex:
    def test_empty_index(self):
        idx = MemoryIndex()
        md = idx.to_markdown()
        assert md.strip() == "# Memory Index"

    def test_add_entry(self):
        idx = MemoryIndex()
        mem = MemoryFile(
            id="abc123",
            type="decision",
            title="Use httpx",
            created_at="2026-07-14T15:30:42Z",
            event_at="2026-07-10T09:00:00Z",
        )
        idx.add_entry(mem)
        md = idx.to_markdown()
        assert "Use httpx" in md
        assert "decision-use-httpx.md" in md
        assert "2026-07-14" in md
        assert "2026-07-10" in md

    def test_remove_entry(self):
        idx = MemoryIndex()
        mem = MemoryFile(type="task", title="Fix bug", created_at="2026-07-14T00:00:00Z")
        idx.add_entry(mem)
        assert len(idx.entries) == 1
        idx.remove_entry(mem.filename)
        assert len(idx.entries) == 0

    def test_add_duplicate_replaces(self):
        idx = MemoryIndex()
        mem = MemoryFile(
            id="1111", type="task", title="First",
            created_at="2026-07-14T00:00:00Z",
        )
        idx.add_entry(mem)
        mem2 = MemoryFile(
            id="2222", type="task", title="Updated",
            created_at="2026-07-14T01:00:00Z",
        )
        # Check what filename mem2 would have (same type+title derived name)
        idx.add_entry(mem2)
        # Filenames differ because titles differ
        assert len(idx.entries) == 2

    def test_from_text_parses_entries(self):
        text = """# Memory Index

## user
- [User prefers Chinese](user-pref-language.md) — rec: 2026-07-14, evt: 2026-07-10

## decision
- [Use httpx](decision-use-httpx.md) [superseded] — rec: 2026-07-14, evt: 2026-07-10
"""
        idx = MemoryIndex.from_text(text)
        assert len(idx.entries) == 2
        assert idx.entries[0].type == "user"
        assert idx.entries[0].filename == "user-pref-language.md"
        assert idx.entries[1].type == "decision"
        assert idx.entries[1].status == "superseded"

    def test_add_entry_truncates_long_title(self):
        """Rant 2026-08-23T08:04:26 — write-time truncation keeps the stored
        index title bounded so MEMORY.md lines can't bloat the system prompt."""
        idx = MemoryIndex()
        long_title = "决策" + "x" * 600
        mem = MemoryFile(
            id="abc123",
            type="task",
            title=long_title,
            created_at="2026-07-14T15:30:42Z",
        )
        idx.add_entry(mem)
        entry = idx.entries[0]
        assert len(entry.title) <= 512
        assert entry.title.endswith("…")
        # Filename (reachable detail file) is preserved un-truncated
        assert entry.filename == mem.filename

    def test_to_markdown_truncates_legacy_long_line(self):
        """Rant 2026-08-23T08:04:26 — render-time fallback for legacy dirty
        index data written before write-time truncation existed."""
        long_title = "旧记录" + "y" * 700
        text = (
            "# Memory Index\n\n"
            f"## task\n"
            f"- [{long_title}](task-long.md) — rec: 2026-07-14, evt: 2026-07-10\n"
        )
        idx = MemoryIndex.from_text(text)
        md = idx.to_markdown()
        # Every rendered index line stays bounded…
        for line in md.splitlines():
            if line.startswith("- ["):
                assert len(line) <= 512, f"line too long ({len(line)}): {line[:80]}…"
        # …and the detail filename stays reachable.
        assert "task-long.md" in md


class TestProjectMemoryStore:
    def test_create_creates_file_and_index(self, project_store):
        mem = project_store.create("decision", "Use httpx", "**What**: ...")
        assert mem.id is not None
        assert mem.scope == "project"
        file_path = project_store.directory / mem.filename
        assert file_path.exists()
        index_path = project_store.index_path
        assert index_path.exists()
        assert mem.filename in index_path.read_text(encoding="utf-8")

    def test_list_returns_all_active(self, project_store):
        project_store.create("task", "Task 1", "body1")
        project_store.create("task", "Task 2", "body2")
        all_mems = project_store.list()
        assert len(all_mems) == 2

    def test_list_with_type_filter(self, project_store):
        project_store.create("decision", "D1", "body")
        project_store.create("task", "T1", "body")
        decisions = project_store.list(type_filter="decision")
        assert len(decisions) == 1
        assert decisions[0].type == "decision"

    def test_get_by_id(self, project_store):
        mem = project_store.create("reference", "API docs", "url")
        retrieved = project_store.get(mem.id)
        assert retrieved is not None
        assert retrieved.id == mem.id

    def test_get_nonexistent(self, project_store):
        assert project_store.get("nonexistent") is None

    def test_update_modifies_file(self, project_store):
        mem = project_store.create("project", "Original", "old body")
        updated = project_store.update(mem.id, body="new body", title="Updated title")
        assert updated is not None
        assert updated.body != mem.body
        assert "new body" in updated.body
        assert updated.title == "Updated title"

    def test_soft_delete(self, project_store):
        mem = project_store.create("task", "To delete", "body")
        assert project_store.delete(mem.id)
        deleted = project_store.get(mem.id)
        assert deleted.status == "superseded"

    def test_merge(self, project_store):
        m1 = project_store.create("reference", "Source A", "body")
        m2 = project_store.create("reference", "Source B", "body")
        assert project_store.merge(m1.id, m2.id)
        merged = project_store.get(m1.id)
        assert merged.status == "merged"


class TestSessionMemoryStore:
    def test_create_session_memory(self, session_store):
        mem = session_store.create("task", "Fix bug", "Working on it")
        assert mem.scope == "session"
        assert (session_store.directory / mem.filename).exists()

    def test_promote_to_project(self, session_store, temp_cwd):
        pstore = ProjectMemoryStore(temp_cwd)
        mem = session_store.create("decision", "Important decision", "Use X over Y")
        promoted = session_store.promote_to_project(mem.id, pstore)
        assert promoted is not None
        assert promoted.scope == "project"
        # Original is merged
        original = session_store.get(mem.id)
        assert original.status == "merged"

    def test_session_index_soft_guard_silent_below_threshold(
        self, session_store, caplog
    ):
        """Rant 2026-08-23T08:04:26 — no warning below the soft thresholds."""
        import logging

        with caplog.at_level(logging.WARNING, logger="emrg.memory"):
            for i in range(5):
                session_store.create("task", f"T{i}", "body")
        assert not any(
            "consolidation recommended" in r.message for r in caplog.records
        )

    def test_session_index_soft_guard_warns_at_threshold(
        self, session_store, caplog
    ):
        """Rant 2026-08-23T08:04:26 — soft guard warns (never deletes) when the
        session index crosses the count threshold."""
        import logging

        with caplog.at_level(logging.WARNING, logger="emrg.memory"):
            for i in range(101):
                session_store.create("task", f"T{i}", "body")
        assert any(
            "consolidation recommended" in r.message for r in caplog.records
        )
        # Guard is non-destructive: no memory file was removed.
        assert len(session_store.list()) == 101


class TestMemoryIndexFileRoundtrip:
    def test_save_and_load(self, temp_cwd):
        path = temp_cwd / "MEMORY.md"
        idx = MemoryIndex()
        mem = MemoryFile(
            id="test1234",
            type="decision",
            title="Test Memory",
            created_at="2026-07-14T15:30:42Z",
            event_at="2026-07-10T09:00:00Z",
        )
        idx.add_entry(mem)
        idx.save(path)
        assert path.exists()

        loaded = MemoryIndex.from_file(path)
        assert len(loaded.entries) == 1
        assert loaded.entries[0].filename == mem.filename


# A memory file in the shape the system prompt's format spec gives agents:
# unquoted ISO timestamps, own key order, a `Z` offset, and a hand-added key
# this module has no field for.
SPEC_SHAPED_MEMORY = """---
id: a1b2c3d4
event_at: 2026-08-27T11:30:45Z
created_at: 2026-08-27T11:48:00Z
updated_at: 2026-08-27T11:48:00Z
type: task
scope: project
status: active
tags: merge-gate
---

# A hand-written memory

Body text that must survive.
"""


class TestMemoryFileRoundTripFidelity:
    """A save must reproduce the file it read — frontmatter included.

    Before this: `to_markdown` re-rendered the frontmatter from the eight
    fields this model holds, so a load → save of a file shaped like the
    documented format came back with a space where the ISO `T` was (`yaml`
    coerces an unquoted timestamp to a `datetime`, and `str()` renders that
    with a space), `Z` as `+00:00`, every value newly quoted, and any key the
    model does not know dropped. Measured 2026-09-14 over the 1216 `.md` files
    under `~/.emrg` memory directories: 1104 came back different.
    """

    def test_spec_shaped_file_round_trips_byte_for_byte(self):
        mem = MemoryFile.from_text(SPEC_SHAPED_MEMORY, _filename="task-a.md")
        assert mem.to_markdown() == SPEC_SHAPED_MEMORY

    def test_timestamps_stay_iso_8601(self):
        """The field docs (and the format spec) say ISO 8601, not `str()`."""
        mem = MemoryFile.from_text(SPEC_SHAPED_MEMORY, _filename="task-a.md")
        assert mem.event_at == "2026-08-27T11:30:45+00:00"
        assert " " not in mem.event_at.split("T")[0]
        assert mem.created_at.startswith("2026-08-27T")

    def test_unknown_frontmatter_key_is_kept(self):
        """`tags:` is not a field of this model — dropping it was data loss."""
        mem = MemoryFile.from_text(SPEC_SHAPED_MEMORY, _filename="task-a.md")
        assert "tags: merge-gate" in mem.to_markdown()

    def test_changed_field_is_rendered_and_siblings_stay_verbatim(self):
        mem = MemoryFile.from_text(SPEC_SHAPED_MEMORY, _filename="task-a.md")
        mem.status = "superseded"
        out = mem.to_markdown()
        assert 'status: "superseded"' in out
        # Everything the store did not touch is still the file's own text.
        assert "event_at: 2026-08-27T11:30:45Z" in out
        assert "tags: merge-gate" in out
        assert "id: a1b2c3d4" in out
        # The two shapes must not both be present.
        assert out.count("status:") == 1

    def test_store_update_rewrites_only_the_changed_line(self, temp_cwd):
        """The store's write path, on a real file: only `status` may move."""
        store = SessionMemoryStore(temp_cwd)
        path = store.directory / "task-a.md"
        path.write_text(SPEC_SHAPED_MEMORY, encoding="utf-8")

        mem = store.update("a1b2c3d4", status="superseded")

        assert mem is not None
        after = path.read_text(encoding="utf-8")
        before_lines = SPEC_SHAPED_MEMORY.split("\n")
        after_lines = after.split("\n")
        changed = [
            (a, b) for a, b in zip(before_lines, after_lines, strict=False) if a != b
        ]
        # `update` sets the field it was asked for and bumps `updated_at`;
        # nothing else in the frontmatter may move.
        assert sorted(a.split(":")[0] for a, _ in changed) == ["status", "updated_at"]
        assert [b for _, b in changed if b.startswith("status")] == ['status: "superseded"']
        # Same line count, so the body is untouched and no line was added.
        assert len(before_lines) == len(after_lines)

    def test_a_file_without_frontmatter_gains_the_required_fields(self):
        """No frontmatter is not a round-trip case: the format requires it."""
        mem = MemoryFile.from_text("# Just a body\n\nText.\n")
        out = mem.to_markdown()
        assert out.startswith("---\n")
        assert "event_at:" in out
        assert "Text." in out
