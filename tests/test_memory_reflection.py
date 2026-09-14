"""Integration tests for memory auto-triggers (Phase 3).

Tests use asyncio.run() directly since pytest-asyncio is not installed.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from emrg.memory import (
    INDEX_COUNT_WARN,
    INDEX_SIZE_WARN,
    ProjectMemoryStore,
    SessionMemoryStore,
)
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

    def test_hygiene_self_review_always_present_below_threshold(self):
        """Rant 2026-08-28T22:12:16 — the digest-style hygiene self-review must be
        in EVERY reflection prompt, even when the index is small (below threshold).
        The old behavior gated it behind >100 entries / >50KB, so small indexes
        never got the consolidation instruction."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_test_hygiene", cwd)

                # Small index (1 memory) — below any soft threshold
                store = session.memory_store
                store.create("task", "One task", "body")

                server = _make_server({"content": "no new memories"})

                server._maybe_reflect_memory(
                    session,
                    "Any feedback?",
                    "No, everything looks good for now.",
                )

                await asyncio.sleep(0.15)

                server.llm.chat.assert_called_once()
                call_args = server.llm.chat.call_args[0][0]
                prompt_text = call_args[0]["content"]
                assert "Memory hygiene" in prompt_text
                assert "optimal organization" in prompt_text
                assert "no consolidation needed" in prompt_text  # forbidden skip
                assert "化零为整" in prompt_text
                assert "化整为零" in prompt_text

        asyncio.run(_test())


class TestTheReminderNamesTheCapThatFired:
    """The soft-cap reminder must name the cap that actually fired, from the constants.

    Measured 2026-09-14: the reminder said `(past the ~50-entry soft cap)` for every
    trigger, while `INDEX_COUNT_WARN` — the constant that fires the entry branch —
    has been **100** since it was introduced (`e67a0a2`, #1057), and the reminder was
    written after that (`33d5700`, #1067). So the number never matched its constant,
    and a size-only trigger still blamed the entry count. These arms measure the
    three states: below both caps (silent), size only, count only — plus the title
    limit, which is stated from the constant that truncates it.
    """

    def test_below_both_caps_the_reminder_is_silent(self):
        """The hygiene block is always there (rant 2026-08-28T22:12:16); the ⚠️ line is not."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                session = Session.create_with_id("s_cap_none", Path(tmp))
                session.memory_store.create("task", "One task", "body")
                server = _make_server({"content": "no new memories"})

                server._maybe_reflect_memory(
                    session, "Any feedback?", "No, everything looks good for now."
                )
                await asyncio.sleep(0.15)

                prompt_text = server.llm.chat.call_args[0][0][0]["content"]
                assert "Memory hygiene" in prompt_text
                assert "Index currently" not in prompt_text, (
                    "the caps were not passed, so no ⚠️ line may appear"
                )

        asyncio.run(_test())

    def test_size_only_names_the_size_and_not_the_count(self):
        """A size-only trigger names the size cap — and must not blame the entry count.

        The size cap is tuned down to 1KB rather than writing 50KB, which also makes
        this arm a *derivation* check: a reminder that re-spelled a number could not
        follow the constant. The state is measured, not assumed — the first draft of
        this arm set the cap to 100 bytes over an index of 92 and asserted a ⚠️ line
        that was correctly absent, which is the arm doing its job.
        """
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                session = Session.create_with_id("s_cap_size", Path(tmp))
                store = session.memory_store
                for i in range(20):
                    store.create("task", f"Task number {i}", "body")
                assert store.count <= INDEX_COUNT_WARN, "the entry cap must not fire here"

                import emrg.server.daemon as daemon_mod
                original = daemon_mod.INDEX_SIZE_WARN
                daemon_mod.INDEX_SIZE_WARN = 1024  # 1KB: the index below is ~1.6KB
                try:
                    assert store.index_path.stat().st_size > daemon_mod.INDEX_SIZE_WARN, (
                        "the size cap must actually be passed, or this arm measures nothing"
                    )
                    server = _make_server({"content": "no new memories"})
                    server._maybe_reflect_memory(
                        session, "Any feedback?", "No, everything looks good for now."
                    )
                    await asyncio.sleep(0.15)
                finally:
                    daemon_mod.INDEX_SIZE_WARN = original

                prompt_text = server.llm.chat.call_args[0][0][0]["content"]
                assert "Index currently" in prompt_text, "the size cap was passed"
                assert f"{1024 // 1024}KB" in prompt_text, (
                    "the reminder must name the size cap it actually passed"
                )
                assert "50-entry" not in prompt_text
                assert f"{INDEX_COUNT_WARN}-entry" not in prompt_text, (
                    "only the size cap was passed, so the entry cap must not be blamed"
                )

        asyncio.run(_test())

    def test_count_only_names_the_count_from_its_constant(self):
        """The entry branch states `INDEX_COUNT_WARN` entries — measured, not typed."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                session = Session.create_with_id("s_cap_count", Path(tmp))
                store = session.memory_store
                for i in range(INDEX_COUNT_WARN + 1):
                    store.create("task", f"Task {i}", "body")
                assert store.count > INDEX_COUNT_WARN
                assert store.index_path.stat().st_size <= INDEX_SIZE_WARN  # size not passed

                server = _make_server({"content": "no new memories"})
                server._maybe_reflect_memory(
                    session, "Any feedback?", "No, everything looks good for now."
                )
                await asyncio.sleep(0.2)

                prompt_text = server.llm.chat.call_args[0][0][0]["content"]
                assert "Index currently" in prompt_text
                assert f"{INDEX_COUNT_WARN}-entry" in prompt_text
                assert "soft cap —" in prompt_text, "one cap passed => singular"

        asyncio.run(_test())

    def test_the_title_limit_is_stated_from_its_constant(self):
        """`≤512 chars` is `INDEX_TITLE_MAX_CHARS`; a tuned constant moves the text."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                session = Session.create_with_id("s_cap_title", Path(tmp))
                session.memory_store.create("task", "One task", "body")
                server = _make_server({"content": "no new memories"})

                import emrg.server.daemon as daemon_mod
                original = daemon_mod.INDEX_TITLE_MAX_CHARS
                daemon_mod.INDEX_TITLE_MAX_CHARS = 256
                try:
                    server._maybe_reflect_memory(
                        session, "Any feedback?", "No, everything looks good for now."
                    )
                    await asyncio.sleep(0.15)
                finally:
                    daemon_mod.INDEX_TITLE_MAX_CHARS = original

                prompt_text = server.llm.chat.call_args[0][0][0]["content"]
                assert "≤256 chars" in prompt_text, (
                    "the stated limit must follow INDEX_TITLE_MAX_CHARS"
                )
                assert "512 chars" not in prompt_text

        asyncio.run(_test())


    def test_both_caps_names_both(self):
        """When both were passed the reminder says so, and says it in the plural.

        Without this state the pluralisation and the join in the message are
        unreached code — a mutant that always writes the singular would survive.
        """
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                session = Session.create_with_id("s_cap_both", Path(tmp))
                store = session.memory_store
                for i in range(20):
                    store.create("task", f"Task number {i}", "body")

                import emrg.server.daemon as daemon_mod
                original_size = daemon_mod.INDEX_SIZE_WARN
                original_count = daemon_mod.INDEX_COUNT_WARN
                daemon_mod.INDEX_SIZE_WARN = 1024
                daemon_mod.INDEX_COUNT_WARN = 1
                try:
                    assert store.index_path.stat().st_size > daemon_mod.INDEX_SIZE_WARN
                    assert store.count > daemon_mod.INDEX_COUNT_WARN
                    server = _make_server({"content": "no new memories"})
                    server._maybe_reflect_memory(
                        session, "Any feedback?", "No, everything looks good for now."
                    )
                    await asyncio.sleep(0.15)
                finally:
                    daemon_mod.INDEX_SIZE_WARN = original_size
                    daemon_mod.INDEX_COUNT_WARN = original_count

                prompt_text = server.llm.chat.call_args[0][0][0]["content"]
                assert "past the 1-entry and 1KB soft caps" in prompt_text, (
                    "both caps passed => both are named, plural"
                )

        asyncio.run(_test())


class TestDisconnectConsolidationDisabled:
    """Rant 2026-08-28T22:12:16 — the on-disconnect consolidation entry
    (_consolidate_session_memories) is REMOVED. The single memory entry point is
    the reflection path (_maybe_reflect_memory)."""

    def test_method_removed(self):
        """The disconnect-consolidation method must no longer exist on EmrgServer."""
        from emrg.server.daemon import EmrgServer
        assert not hasattr(EmrgServer, "_consolidate_session_memories"), (
            "_consolidate_session_memories was removed (rant 2026-08-28T22:12:16); "
            "if it is back, the disconnect entry was re-introduced."
        )

