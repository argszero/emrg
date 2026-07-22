"""Tests for session module: Session class and _validate_tool_messages."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from emrg.session import Session, _validate_tool_messages, generate_session_id


class TestValidateToolMessages:
    """Tests for the _validate_tool_messages safety-net function."""

    def test_empty(self):
        """Empty list returns empty list."""
        assert _validate_tool_messages([]) == []

    def test_plain_messages_pass_through(self):
        """Plain user/assistant messages pass through unchanged."""
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        assert _validate_tool_messages(msgs) == msgs

    def test_summary_messages_pass_through(self):
        """Summary-type messages pass through unchanged."""
        msgs = [
            {"role": "user", "content": "[Previous conversation summary]\n..."},
        ]
        assert _validate_tool_messages(msgs) == msgs

    def test_assistant_with_tool_calls_and_matching_tools(self):
        """Assistant with tool_calls + matching tool messages: both preserved."""
        msgs = [
            {"role": "user", "content": "read foo.txt"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
        ]
        result = _validate_tool_messages(msgs)
        assert len(result) == 3
        assert "tool_calls" in result[1]
        assert result[1]["tool_calls"][0]["id"] == "call_1"
        assert result[2]["role"] == "tool"

    def test_assistant_tool_calls_no_matching_tools(self):
        """Assistant with tool_calls but no matching tool messages: stripped."""
        msgs = [
            {"role": "user", "content": "read foo.txt"},
            {
                "role": "assistant",
                "content": "Let me check that.",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                ],
            },
        ]
        result = _validate_tool_messages(msgs)
        assert len(result) == 2
        assert "tool_calls" not in result[1]
        assert result[1]["content"] == "Let me check that."

    def test_assistant_tool_calls_no_matching_tools_null_content(self):
        """Assistant with tool_calls, no match, null content → content becomes ''."""
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                ],
            },
        ]
        result = _validate_tool_messages(msgs)
        assert len(result) == 1
        assert "tool_calls" not in result[0]
        assert result[0]["content"] == ""

    def test_orphaned_tool_messages_removed(self):
        """Tool messages without preceding assistant tool_calls are stripped."""
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "tool", "tool_call_id": "orphan_1", "content": "orphan result"},
            {"role": "assistant", "content": "done"},
        ]
        result = _validate_tool_messages(msgs)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"

    def test_partial_match_valid_kept_invalid_stripped(self):
        """Multiple tool_calls, only some have matching tool messages."""
        msgs = [
            {"role": "user", "content": "do things"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                    {"id": "call_2", "type": "function", "function": {"name": "write", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result 1"},
        ]
        result = _validate_tool_messages(msgs)
        assert len(result) == 3
        assert len(result[1]["tool_calls"]) == 1
        assert result[1]["tool_calls"][0]["id"] == "call_1"
        assert result[2]["tool_call_id"] == "call_1"

    def test_multiple_assistant_tool_blocks(self):
        """Two separate assistant+tool blocks, both preserved."""
        msgs = [
            {"role": "user", "content": "read a and b"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_a", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "content a"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_b", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_b", "content": "content b"},
            {"role": "assistant", "content": "here you go"},
        ]
        result = _validate_tool_messages(msgs)
        assert len(result) == 6
        assert result[1]["tool_calls"][0]["id"] == "call_a"
        assert result[3]["tool_calls"][0]["id"] == "call_b"

    def test_mixed_block_one_valid_one_orphaned(self):
        """First assistant+tool valid, second assistant has orphaned tools."""
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_ok", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_ok", "content": "done"},
            # Orphaned tool message — no preceding assistant tool_calls
            {"role": "tool", "tool_call_id": "bad_call", "content": "stale"},
            {"role": "assistant", "content": "all done"},
        ]
        result = _validate_tool_messages(msgs)
        assert len(result) == 4  # user + assistant+tool + assistant
        assert result[2]["tool_call_id"] == "call_ok"
        assert result[3]["content"] == "all done"


# ── Session core operations ────────────────────────────────────


class TestSessionCreate:
    """Tests for Session.create() and Session.create_with_id()."""

    def test_create_returns_session_with_valid_id(self, tmp_path):
        """create() returns a Session with a generated ID matching the pattern."""
        session = Session.create(tmp_path)
        assert session.session_id.startswith("s_")
        assert len(session.session_id) >= 14
        assert session._dir.exists()
        assert session._meta_path.exists()

    def test_create_sets_timestamps(self, tmp_path):
        """create() sets created_at and updated_at to ISO-format timestamps."""
        session = Session.create(tmp_path)
        assert session._created_at
        assert session._updated_at
        # Verify ISO 8601 format (contains 'T')
        assert "T" in session._created_at
        assert "T" in session._updated_at

    def test_create_initializes_counts(self, tmp_path):
        """create() initializes message_count and compact_count to 0."""
        session = Session.create(tmp_path)
        assert session.message_count == 0
        assert session.compact_count == 0

    def test_create_writes_meta_disk(self, tmp_path):
        """create() persists meta.json with correct fields."""
        session = Session.create(tmp_path)
        meta = json.loads(session._meta_path.read_text())
        assert meta["session_id"] == session.session_id
        assert meta["message_count"] == 0
        assert meta["compact_count"] == 0
        assert meta["cwd"] == str(tmp_path)

    def test_create_with_id_uses_given_id(self, tmp_path):
        """create_with_id() uses the exact ID provided."""
        session = Session.create_with_id("s_test_0001", tmp_path)
        assert session.session_id == "s_test_0001"
        assert session._dir.name == "s_test_0001"

    def test_create_with_id_persists_meta(self, tmp_path):
        """create_with_id() writes meta.json with the given session ID."""
        session = Session.create_with_id("s_custom_42", tmp_path)
        meta = json.loads(session._meta_path.read_text())
        assert meta["session_id"] == "s_custom_42"

    def test_create_unique_ids(self, tmp_path):
        """Multiple create() calls produce unique session IDs."""
        s1 = Session.create(tmp_path)
        s2 = Session.create(tmp_path)
        assert s1.session_id != s2.session_id


class TestSessionLoad:
    """Tests for Session.load()."""

    def test_load_restores_from_disk(self, tmp_path):
        """load() restores a previously created session."""
        created = Session.create(tmp_path)
        sid = created.session_id

        loaded = Session.load(sid, tmp_path)
        assert loaded.session_id == sid
        assert loaded._created_at == created._created_at
        assert loaded.message_count == 0
        assert loaded.compact_count == 0

    def test_load_restores_message_count(self, tmp_path):
        """load() correctly restores message_count after messages were appended."""
        session = Session.create(tmp_path)
        session.append_message({"role": "user", "content": "hello"})
        session.append_message({"role": "assistant", "content": "hi"})

        loaded = Session.load(session.session_id, tmp_path)
        assert loaded.message_count == 2

    def test_load_restores_compact_count(self, tmp_path):
        """load() correctly restores compact_count after compact()."""
        session = Session.create(tmp_path)
        for i in range(10):
            session.append_message({"role": "user", "content": f"msg {i}"})
        session.compact("summary of first 5", keep_recent=5)

        loaded = Session.load(session.session_id, tmp_path)
        assert loaded.compact_count == 1

    def test_load_nonexistent_session_does_not_crash(self, tmp_path):
        """load() on a non-existent session returns a Session (without meta)."""
        session = Session.load("s_nonexistent", tmp_path)
        assert session.session_id == "s_nonexistent"
        assert session.message_count == 0


class TestSessionAppendMessage:
    """Tests for Session.append_message()."""

    def test_append_message_increments_count(self, tmp_path):
        """append_message() increments message_count by 1 each call."""
        session = Session.create(tmp_path)
        assert session.message_count == 0
        session.append_message({"role": "user", "content": "hello"})
        assert session.message_count == 1
        session.append_message({"role": "assistant", "content": "hi"})
        assert session.message_count == 2

    def test_append_message_writes_to_history(self, tmp_path):
        """append_message() writes to history.jsonl."""
        session = Session.create(tmp_path)
        session.append_message({"role": "user", "content": "hello"})
        session.append_message({"role": "assistant", "content": "world"})

        records = session._read_history()
        assert len(records) == 2
        assert records[0]["role"] == "user"
        assert records[0]["content"] == "hello"
        assert records[1]["role"] == "assistant"
        assert records[1]["content"] == "world"

    def test_append_message_writes_to_daily_history(self, tmp_path):
        """append_message() also writes to daily history file."""
        session = Session.create(tmp_path)
        session.append_message({"role": "user", "content": "daily test"})

        daily_path = session._daily_history_path()
        assert daily_path.exists()
        lines = daily_path.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["role"] == "user"
        assert record["content"] == "daily test"

    def test_append_message_adds_timestamp(self, tmp_path):
        """append_message() adds a timestamp if not provided."""
        session = Session.create(tmp_path)
        session.append_message({"role": "user", "content": "no timestamp"})

        records = session._read_history()
        assert "timestamp" in records[0]
        # timestamp is first key
        first_key = list(records[0].keys())[0]
        assert first_key == "timestamp"

    def test_append_message_preserves_existing_timestamp(self, tmp_path):
        """append_message() preserves an explicitly provided timestamp."""
        session = Session.create(tmp_path)
        ts = "2025-01-15T10:30:00"
        session.append_message({"timestamp": ts, "role": "user", "content": "custom"})

        records = session._read_history()
        assert records[0]["timestamp"] == ts

    def test_append_llm_writes_to_llm_file(self, tmp_path):
        """append_llm() writes records to llm.jsonl."""
        session = Session.create(tmp_path)
        session.append_llm({"type": "request", "model": "deepseek-chat"})
        session.append_llm({"type": "response", "tokens": 150})

        assert session._llm_path.exists()
        lines = session._llm_path.read_text().strip().split("\n")
        assert len(lines) == 2
        r1 = json.loads(lines[0])
        assert r1["type"] == "request"
        r2 = json.loads(lines[1])
        assert r2["type"] == "response"


class TestSessionCompact:
    """Tests for Session.compact()."""

    def test_compact_noop_when_few_messages(self, tmp_path):
        """compact() returns 0 when records <= keep_recent."""
        session = Session.create(tmp_path)
        session.append_message({"role": "user", "content": "only one"})

        result = session.compact("summary", keep_recent=5)
        assert result == 0
        assert session.compact_count == 0

    def test_compact_compresses_older_messages(self, tmp_path):
        """compact() replaces older messages with a summary record."""
        session = Session.create(tmp_path)
        for i in range(10):
            session.append_message({"role": "user", "content": f"msg {i}"})

        result = session.compact("summary of first 5", keep_recent=5)
        assert result == 5
        assert session.compact_count == 1

    def test_compact_keeps_recent_messages(self, tmp_path):
        """compact() preserves the keep_recent most recent messages."""
        session = Session.create(tmp_path)
        for i in range(10):
            session.append_message({"role": "user", "content": f"msg {i}"})

        session.compact("summary", keep_recent=3)
        records = session._read_history()

        # First record should be the summary
        assert records[0]["type"] == "summary"
        assert records[0]["content"] == "summary"
        assert "compact_id" in records[0]

        # Remaining 3 records should be the most recent messages
        assert len(records) == 4  # summary + 3 recent
        assert records[1]["content"] == "msg 7"
        assert records[2]["content"] == "msg 8"
        assert records[3]["content"] == "msg 9"

    def test_compact_increments_compact_count(self, tmp_path):
        """compact() increments compact_count on each call."""
        session = Session.create(tmp_path)
        for i in range(10):
            session.append_message({"role": "user", "content": f"msg {i}"})

        assert session.compact_count == 0
        session.compact("first", keep_recent=5)
        assert session.compact_count == 1

        # Add more and compact again
        for i in range(5):
            session.append_message({"role": "user", "content": f"more {i}"})
        session.compact("second", keep_recent=3)
        assert session.compact_count == 2

    def test_compact_updates_meta(self, tmp_path):
        """compact() updates meta.json with new compact_count."""
        session = Session.create(tmp_path)
        for i in range(10):
            session.append_message({"role": "user", "content": f"msg {i}"})

        session.compact("summary", keep_recent=5)
        meta = json.loads(session._meta_path.read_text())
        assert meta["compact_count"] == 1
        assert meta["last_compact_at"] is not None

    def test_compact_keeps_exact_keep_recent(self, tmp_path):
        """compact() with keep_recent=1 replaces all but the last message."""
        session = Session.create(tmp_path)
        for i in range(5):
            session.append_message({"role": "user", "content": f"msg {i}"})

        result = session.compact("all summarized", keep_recent=1)
        assert result == 4
        records = session._read_history()
        assert len(records) == 2  # summary + 1 recent
        assert records[0]["type"] == "summary"
        assert records[1]["content"] == "msg 4"


class TestSessionRename:
    """Tests for Session.rename()."""

    def test_rename_sets_title(self, tmp_path):
        """rename() sets a custom title and persists it."""
        session = Session.create(tmp_path)
        session.rename("My Custom Session")

        assert session.title == "My Custom Session"

    def test_rename_persists_in_meta(self, tmp_path):
        """rename() persists the title in meta.json."""
        session = Session.create(tmp_path)
        session.rename("Persisted Title")

        meta = json.loads(session._meta_path.read_text())
        assert meta["title"] == "Persisted Title"

    def test_title_defaults_to_session_id(self, tmp_path):
        """title property returns session_id when no custom title is set."""
        session = Session.create(tmp_path)
        assert session.title == session.session_id

    def test_rename_survives_reload(self, tmp_path):
        """rename() title is preserved when session is reloaded."""
        session = Session.create(tmp_path)
        sid = session.session_id
        session.rename("Will Survive")

        loaded = Session.load(sid, tmp_path)
        assert loaded.title == "Will Survive"

    def test_rename_updates_updated_at(self, tmp_path):
        """rename() updates the updated_at timestamp."""
        session = Session.create(tmp_path)
        original_updated = session._updated_at
        session.rename("New Title")
        assert session._updated_at != original_updated


class TestSessionClear:
    """Tests for Session.clear()."""

    def test_clear_resets_message_count(self, tmp_path):
        """clear() resets message_count to 0."""
        session = Session.create(tmp_path)
        session.append_message({"role": "user", "content": "hello"})
        session.append_message({"role": "assistant", "content": "hi"})
        assert session.message_count == 2

        session.clear()
        assert session.message_count == 0

    def test_clear_preserves_session_id(self, tmp_path):
        """clear() does not change the session_id."""
        session = Session.create(tmp_path)
        sid = session.session_id
        session.append_message({"role": "user", "content": "something"})
        session.clear()
        assert session.session_id == sid

    def test_clear_writes_reset_record(self, tmp_path):
        """clear() writes a single system reset record to history."""
        session = Session.create(tmp_path)
        session.append_message({"role": "user", "content": "hello"})
        session.clear()

        records = session._read_history()
        assert len(records) == 1
        assert records[0]["type"] == "message"
        assert records[0]["role"] == "system"
        assert "[Session cleared]" in records[0]["content"]

    def test_clear_persists_to_disk(self, tmp_path):
        """clear() persists the reset to meta.json."""
        session = Session.create(tmp_path)
        session.append_message({"role": "user", "content": "hello"})
        session.clear()

        meta = json.loads(session._meta_path.read_text())
        assert meta["message_count"] == 0


class TestSessionGetMessagesForLLM:
    """Tests for Session.get_messages_for_llm()."""

    def test_empty_history_returns_empty(self, tmp_path):
        """get_messages_for_llm() returns empty list for new session."""
        session = Session.create(tmp_path)
        result = session.get_messages_for_llm()
        assert result == []

    def test_plain_messages_converted(self, tmp_path):
        """get_messages_for_llm() converts message records to role/content dicts."""
        session = Session.create(tmp_path)
        session.append_message({"type": "message", "role": "user", "content": "hello"})
        session.append_message({"type": "message", "role": "assistant", "content": "hi"})

        result = session.get_messages_for_llm()
        assert len(result) == 2
        assert result[0] == {"role": "user", "content": "hello"}
        assert result[1] == {"role": "assistant", "content": "hi"}

    def test_summary_converted_to_user_message(self, tmp_path):
        """get_messages_for_llm() converts summary records to user messages with prefix."""
        session = Session.create(tmp_path)
        for i in range(10):
            session.append_message({"role": "user", "content": f"msg {i}"})
        session.compact("summary of earlier conversation", keep_recent=0)

        result = session.get_messages_for_llm()
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert "[Previous conversation summary]" in result[0]["content"]
        assert "summary of earlier conversation" in result[0]["content"]

    def test_messages_with_embedded_tool_calls(self, tmp_path):
        """get_messages_for_llm() handles embedded tool_calls in assistant messages."""
        session = Session.create(tmp_path)
        session.append_message({"type": "message", "role": "user", "content": "read file"})
        session.append_message({
            "type": "message",
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ],
        })
        session.append_message({
            "type": "tool_result",
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "file content",
        })

        result = session.get_messages_for_llm()
        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert "tool_calls" in result[1]
        assert result[1]["tool_calls"][0]["id"] == "call_1"
        assert result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "call_1"


class TestSessionListSessions:
    """Tests for Session.list_sessions()."""

    def test_list_empty_returns_empty(self, tmp_path):
        """list_sessions() returns empty list when no sessions exist."""
        result = Session.list_sessions(tmp_path)
        assert result == []

    def test_list_returns_created_sessions(self, tmp_path):
        """list_sessions() returns metadata for all sessions in the directory."""
        s1 = Session.create(tmp_path)
        s2 = Session.create_with_id("s_manual_001", tmp_path)

        result = Session.list_sessions(tmp_path)
        assert len(result) >= 2

        ids = [r["session_id"] for r in result]
        assert s1.session_id in ids
        assert "s_manual_001" in ids

    def test_list_sorted_by_created_at_desc(self, tmp_path):
        """list_sessions() returns sessions sorted by created_at descending."""
        s1 = Session.create(tmp_path)
        s2 = Session.create(tmp_path)

        result = Session.list_sessions(tmp_path)
        # s2 was created after s1, so it should appear first
        s1_idx = next(i for i, r in enumerate(result) if r["session_id"] == s1.session_id)
        s2_idx = next(i for i, r in enumerate(result) if r["session_id"] == s2.session_id)
        assert s2_idx < s1_idx

    def test_list_includes_title(self, tmp_path):
        """list_sessions() includes the title field if set."""
        session = Session.create(tmp_path)
        session.rename("My Listed Session")

        result = Session.list_sessions(tmp_path)
        found = next(r for r in result if r["session_id"] == session.session_id)
        assert found["title"] == "My Listed Session"


class TestSessionProperties:
    """Tests for Session properties."""

    def test_dir_path(self, tmp_path):
        """dir_path returns the session directory."""
        session = Session.create(tmp_path)
        assert session.dir_path == session._dir
        assert session.dir_path.exists()

    def test_memory_dir(self, tmp_path):
        """memory_dir exists and is inside the session directory."""
        session = Session.create(tmp_path)
        assert session.memory_dir.exists()
        assert session.memory_dir.is_dir()
        assert session.memory_dir.parent == session._dir

    def test_memory_store_lazy_init(self, tmp_path):
        """memory_store is lazily initialized and returns a SessionMemoryStore."""
        session = Session.create(tmp_path)
        store = session.memory_store
        assert store is not None
        # Second access returns the same instance
        assert session.memory_store is store

    def test_message_count_property(self, tmp_path):
        """message_count property reflects the current count."""
        session = Session.create(tmp_path)
        assert session.message_count == 0
        session.append_message({"role": "user", "content": "hello"})
        assert session.message_count == 1

    def test_compact_count_property(self, tmp_path):
        """compact_count property reflects the current count."""
        session = Session.create(tmp_path)
        assert session.compact_count == 0
        for i in range(10):
            session.append_message({"role": "user", "content": f"msg {i}"})
        session.compact("summary", keep_recent=5)
        assert session.compact_count == 1


class TestGenerateSessionId:
    """Tests for generate_session_id()."""

    def test_generates_valid_format(self, tmp_path):
        """generate_session_id() produces IDs matching s_YYMMDD_HHMM_xxxx."""
        sid = generate_session_id(tmp_path)
        assert sid.startswith("s_")
        assert len(sid) >= 14
        # Format: s_YYMMDD_HhMM_xxxx
        parts = sid.split("_")
        assert len(parts) >= 4
        assert len(parts[1]) == 6  # YYMMDD
        assert len(parts[2]) == 4  # HHMM

    def test_generates_unique_ids(self, tmp_path):
        """generate_session_id() returns unique IDs."""
        ids = {generate_session_id(tmp_path) for _ in range(10)}
        assert len(ids) == 10

    def test_avoids_existing_dirs(self, tmp_path):
        """generate_session_id() does not return an ID that already exists."""
        # Create a session directory manually
        sid = generate_session_id(tmp_path)
        (tmp_path / ".emrg" / "sessions" / sid).mkdir(parents=True, exist_ok=True)

        # Next call should produce a different ID
        new_sid = generate_session_id(tmp_path)
        assert new_sid != sid
