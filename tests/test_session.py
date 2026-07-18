"""Tests for session._validate_tool_messages."""

import pytest

from emrg.session import _validate_tool_messages


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
