"""Integration tests for memory auto-triggers (Phase 3).

Tests use asyncio.run() directly since pytest-asyncio is not installed.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from emrg.memory import ProjectMemoryStore, SessionMemoryStore
from emrg.session import Session


def _make_server(llm_chat_return=None):
    """Create a minimal EmrgServer for testing with a mocked LLM."""
    from emrg.server.daemon import EmrgServer
    server = EmrgServer.__new__(EmrgServer)
    server.tools = MagicMock()
    server.skills = []
    server._max_tool_rounds = 10
    server.llm = AsyncMock()
    if llm_chat_return:
        server.llm.chat.return_value = llm_chat_return
    return server


class TestMemoryReflection:
    """Tests for _maybe_reflect_memory (post-response memory reflection)."""

    def test_skips_trivial_content(self):
        """Should NOT trigger reflection for very short assistant responses."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_test_reflect", cwd)
                server = _make_server()
                server.llm = MagicMock()  # not async — to check .chat was NOT called

                # Trivial content (< 20 chars) → should NOT call LLM
                server._maybe_reflect_memory(session, "hi", "ok")

                # Give background task a tick
                await asyncio.sleep(0.05)

                # LLM should NOT have been called
                server.llm.chat.assert_not_called()

        asyncio.run(_test())

    def test_triggers_for_substantive_content(self):
        """Should trigger reflection for meaningful exchanges."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_test_subst", cwd)
                server = _make_server({"content": "no new memories"})

                # Substantive content (>20 chars) → should trigger
                server._maybe_reflect_memory(
                    session,
                    "Help me implement a user authentication system",
                    "I'll help you implement authentication. Let's start by creating "
                    "the auth module with JWT token support.",
                )

                # Wait for background task
                await asyncio.sleep(0.15)

                # LLM should have been called
                server.llm.chat.assert_called_once()
                call_args = server.llm.chat.call_args[0][0]
                prompt_text = call_args[0]["content"]
                assert "memory reflection" in prompt_text
                assert "user authentication" in prompt_text
                assert "auth module" in prompt_text

        asyncio.run(_test())

    def test_includes_existing_memories_in_prompt(self):
        """Reflection prompt should mention existing memories."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_test_exist", cwd)

                # Pre-create some session memories
                store = session.memory_store
                store.create("decision", "Use httpx", "body")
                store.create("task", "Fix scroll bug", "body")

                server = _make_server({"content": "no new memories"})

                server._maybe_reflect_memory(
                    session,
                    "Should we change the HTTP library?",
                    "Yes, let's switch to aiohttp instead of httpx.",
                )

                await asyncio.sleep(0.15)

                server.llm.chat.assert_called_once()
                call_args = server.llm.chat.call_args[0][0]
                prompt_text = call_args[0]["content"]
                assert "Use httpx" in prompt_text
                assert "Fix scroll bug" in prompt_text

        asyncio.run(_test())


class TestSessionConsolidation:
    """Tests for _consolidate_session_memories (on-disconnect consolidation)."""

    def test_skips_when_few_memories(self):
        """Should NOT consolidate if less than 3 memories."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_consol_skip", cwd)

                store = session.memory_store
                store.create("task", "Task 1", "body")
                store.create("task", "Task 2", "body")

                server = _make_server()

                await server._consolidate_session_memories(session.session_id, cwd)

                server.llm.chat.assert_not_called()

        asyncio.run(_test())

    def test_consolidates_when_enough_memories(self):
        """Should consolidate when session has ≥3 memories."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_consol_do", cwd)

                store = session.memory_store
                store.create("decision", "Decision A", "body A")
                store.create("task", "Task B", "body B")
                store.create("reference", "Ref C", "body C")

                server = _make_server({"content": "no consolidation needed"})

                await server._consolidate_session_memories(session.session_id, cwd)

                server.llm.chat.assert_called_once()
                call_args = server.llm.chat.call_args[0][0]
                prompt_text = call_args[0]["content"]
                assert "memory consolidation" in prompt_text
                assert "Decision A" in prompt_text
                assert "Task B" in prompt_text

        asyncio.run(_test())

    def test_handles_missing_session(self):
        """Should gracefully handle non-existent session on disconnect."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server = _make_server()

                await server._consolidate_session_memories("nonexistent_session", cwd)
                server.llm.chat.assert_not_called()

        asyncio.run(_test())

    def test_client_disconnect_triggers_consolidation(self):
        """When client disconnects, consolidation should be attempted."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_client_cleanup", cwd)

                store = session.memory_store
                store.create("task", "Task 1", "body")
                store.create("task", "Task 2", "body")
                store.create("task", "Task 3", "body")

                server = _make_server({"content": "consolidation done"})

                await server._consolidate_session_memories(session.session_id, cwd)

                server.llm.chat.assert_called_once()
                call_args = server.llm.chat.call_args[0][0]
                prompt_text = call_args[0]["content"]
                assert "memory consolidation" in prompt_text

        asyncio.run(_test())
