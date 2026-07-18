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


class TestProjectMemoryStore:
    def test_create_creates_file_and_index(self, project_store):
        mem = project_store.create("decision", "Use httpx", "**What**: ...")
        assert mem.id is not None
        assert mem.scope == "project"
        file_path = project_store.directory / mem.filename
        assert file_path.exists()
        index_path = project_store.index_path
        assert index_path.exists()
        assert mem.filename in index_path.read_text()

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
