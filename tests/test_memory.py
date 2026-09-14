"""Tests for the EMRG memory module."""

import tempfile
from pathlib import Path

import pytest

from emrg.memory import (
    INDEX_TITLE_MAX_CHARS,
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
        assert len(entry.title) <= INDEX_TITLE_MAX_CHARS
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
                assert len(line) <= INDEX_TITLE_MAX_CHARS, f"line too long ({len(line)}): {line[:80]}…"
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


# A hand-written index, as agents actually keep them: a document title, `>`
# notes, a row with prose after the dates, a row listing several links, and a
# heading that is not a `## <type>` section.
HAND_WRITTEN_INDEX = """# 项目记忆索引

> 历史记录已于 2026-08-23 归档清理。
> 本项目记忆从零开始重建。

- [Steady state](evolution-state.md) — active · 最近更新 cyc20260914-131523：master `f15e1b88`。

### Session Memory

- [cycle-20260914-131523](cycle-20260914-131523.md) — 本周期：宿主令复述一律删除。
- [cycle-20260914-125119](cycle-20260914-125119.md) / [cycle-20260914-122910](cycle-20260914-122910.md) — 更早周期。
"""


class TestMemoryIndexRoundTripFidelity:
    """The index is hand-edited; a store write must not rewrite it.

    Before this: `to_markdown` rendered only from parsed `_IndexEntry`s, so a
    load → save dropped the document title, every `>` note and every row's
    prose, and filed surviving rows under an invented `## reference` — the
    evolution index (22389 chars, 50 rows) came back as 2628 chars.
    """

    def test_untouched_index_round_trips_byte_for_byte(self):
        idx = MemoryIndex.from_text(HAND_WRITTEN_INDEX)
        assert idx.to_markdown() == HAND_WRITTEN_INDEX

    def test_round_trip_keeps_what_the_model_cannot_represent(self):
        out = MemoryIndex.from_text(HAND_WRITTEN_INDEX).to_markdown()
        for kept in [
            "# 项目记忆索引",                      # document title
            "> 历史记录已于",                       # `>` note
            "### Session Memory",                  # unknown heading
            "最近更新 cyc20260914-131523",           # per-row prose
            "本周期：宿主令复述一律删除",              # per-row prose
            "cycle-20260914-122910.md",            # 2nd link in a multi-link row
        ]:
            assert kept in out, f"lost on round trip: {kept}"

    def test_loading_an_untouched_index_twice_is_stable(self):
        once = MemoryIndex.from_text(HAND_WRITTEN_INDEX).to_markdown()
        twice = MemoryIndex.from_text(once).to_markdown()
        assert twice == once

    def test_store_create_preserves_hand_written_rows(self, temp_cwd):
        """The store's write path: create → load index → add_entry → save."""
        store = SessionMemoryStore(temp_cwd)
        store.index_path.write_text(HAND_WRITTEN_INDEX, encoding="utf-8")

        mem = store.create("task", "A new memory", "body text")

        after = store.index_path.read_text(encoding="utf-8")
        assert mem.filename in after, "the new memory is not in the index"
        assert "### Session Memory" in after
        assert "> 历史记录已于" in after
        assert "最近更新 cyc20260914-131523" in after
        # The pre-existing hand-written rows are still there, verbatim.
        assert "cycle-20260914-131523.md" in after
        assert "cycle-20260914-125119.md" in after

    def test_removed_entry_drops_only_its_own_row(self):
        idx = MemoryIndex.from_text(HAND_WRITTEN_INDEX)
        idx.remove_entry("cycle-20260914-131523.md")
        out = idx.to_markdown()
        assert "cycle-20260914-131523.md" not in out
        assert "### Session Memory" in out
        assert "cycle-20260914-125119.md" in out

    def test_rewritten_entry_renders_once_from_its_fields(self):
        """An entry the store rewrote loses its stale line — once, not twice."""
        text = (
            "# Memory Index\n\n"
            "## task\n"
            "- [Fix bug](task-fix-bug.md) — rec: 2026-07-14, evt: 2026-07-10\n"
            "- [Other](task-other.md) — rec: 2026-07-14, evt: 2026-07-10\n"
        )
        idx = MemoryIndex.from_text(text)
        idx.add_entry(
            MemoryFile(
                id="1111",
                type="task",
                title="Fix bug",
                created_at="2026-07-20T00:00:00Z",
            )
        )
        out = idx.to_markdown()
        assert out.count("task-fix-bug.md") == 1
        assert "2026-07-20" in out, "the rewritten row kept its stale dates"
        # The untouched row is still its original line, verbatim.
        assert "- [Other](task-other.md) — rec: 2026-07-14, evt: 2026-07-10" in out
