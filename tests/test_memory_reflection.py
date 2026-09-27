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

    def test_hygiene_self_review_always_present_below_threshold(self):
        """Rant 2026-08-28T22:12:16 — the digest-style hygiene self-review must be
        in EVERY reflection prompt, even when the index is small (below threshold).
        The old behavior gated it behind the store's soft caps, so small indexes
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


def _prompt_text(server) -> str:
    """The reflection prompt the server actually sent."""
    return server.llm.chat.call_args[0][0][0]["content"]


async def _reflect(server, session) -> str:
    """Run one reflection and return the prompt it sent, or "" if none was sent."""
    server._maybe_reflect_memory(session, "Any feedback?", "Nothing to report yet.")
    await asyncio.sleep(0.15)
    if not server.llm.chat.call_args:
        return ""
    return _prompt_text(server)


class TestMemoryIndexCompactionPrompt:
    """An index over `MEMORY_INDEX_ROW_CAP` lines asks the agent to compact it.

    Host design 2026-09-25 (`~/.emrg/designs/memory-index-compaction-design.md`): the
    trigger is a per-round line count of each index the prompt carries, the condition
    is `lines > cap`, and what fires is a *prompt* — the agent has read/edit/write, so
    merging, shortening and dropping rows is prose, not a mechanism. These tests pin
    the trigger and the reading; they do not pin the prompt's wording beyond the parts
    that carry a number the agent acts on.
    """

    def _index(self, path, lines: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"- row {i}" for i in range(lines)), encoding="utf-8")

    def test_an_index_over_the_cap_is_named_with_its_own_line_count(self):
        from emrg.server.daemon import MEMORY_INDEX_ROW_CAP

        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_test_cap", cwd)
                index = session.memory_dir / "MEMORY.md"
                self._index(index, MEMORY_INDEX_ROW_CAP + 1)
                server = _make_server({"content": "no new memories"})

                prompt = await _reflect(server, session)

                assert "Memory index compaction" in prompt
                assert str(index) in prompt, "the section must name the file it is about"
                assert f"has {MEMORY_INDEX_ROW_CAP + 1} lines" in prompt, (
                    "the number printed must be the one counted — a caller passing a "
                    "reading and the text printing another teaches distrust of both"
                )

        asyncio.run(_test())

    def test_an_index_at_the_cap_says_nothing(self):
        """The boundary: the condition is `> cap`, so exactly cap lines is silent.

        Both directions matter — an off-by-one here either never fires or fires on
        every index the prompt can already hold.
        """
        from emrg.server.daemon import MEMORY_INDEX_ROW_CAP

        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_test_cap_edge", cwd)
                self._index(session.memory_dir / "MEMORY.md", MEMORY_INDEX_ROW_CAP)
                server = _make_server({"content": "no new memories"})

                prompt = await _reflect(server, session)

                assert "Memory index compaction" not in prompt

        asyncio.run(_test())

    def test_the_project_index_is_counted_too(self):
        """Two indexes reach the prompt; the project one is the other half.

        A rule that only watched the session index would miss every long-lived
        project index — the ones a project's own cycles keep appending to.
        """
        from emrg.server.daemon import MEMORY_INDEX_ROW_CAP

        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_test_cap_proj", cwd)
                project_index = cwd / ".emrg" / "memory" / "MEMORY.md"
                self._index(project_index, MEMORY_INDEX_ROW_CAP + 7)
                server = _make_server({"content": "no new memories"})

                prompt = await _reflect(server, session)

                assert "Memory index compaction" in prompt
                assert str(project_index) in prompt
                assert f"has {MEMORY_INDEX_ROW_CAP + 7} lines" in prompt

        asyncio.run(_test())

    def test_both_indexes_over_the_cap_get_one_section_each(self):
        from emrg.server.daemon import MEMORY_INDEX_ROW_CAP

        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_test_cap_two", cwd)
                self._index(session.memory_dir / "MEMORY.md", MEMORY_INDEX_ROW_CAP + 1)
                self._index(cwd / ".emrg" / "memory" / "MEMORY.md", MEMORY_INDEX_ROW_CAP + 2)
                server = _make_server({"content": "no new memories"})

                prompt = await _reflect(server, session)

                assert prompt.count("## Memory index compaction") == 2, (
                    "one index, one section — an instruction naming the wrong file "
                    "sends the agent to compact something that is not over the cap"
                )

        asyncio.run(_test())

    def test_the_instance_root_index_is_counted_for_a_session_inside_the_root(
        self, monkeypatch
    ):
        """Issue #1606: the index the cycles write is the root's, not the cwd's.

        `~/.emrg/evolution/` is this instance's workspace and `<root>/emrg` is the
        checkout a task session runs in, so a cycle's records are indexed at
        `<root>/.emrg/memory/MEMORY.md` — one directory above the session's cwd, and a
        file neither of the two subjects above can name. Measured on this host before
        the subject existed: 195 lines, with no count that could fire on it.

        The root is monkeypatched rather than described by the host's own constant, so
        the test measures the rule instead of this machine's workspace.
        """
        from emrg.server import daemon
        from emrg.server.daemon import MEMORY_INDEX_ROW_CAP

        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                monkeypatch.setattr(daemon, "EVOLUTION_CWD", root)
                cwd = root / "emrg"
                cwd.mkdir()
                session = Session.create_with_id("s_test_cap_root", cwd)
                root_index = root / ".emrg" / "memory" / "MEMORY.md"
                self._index(root_index, MEMORY_INDEX_ROW_CAP + 3)
                server = _make_server({"content": "no new memories"})

                prompt = await _reflect(server, session)

                assert str(root_index) in prompt, (
                    "the section must name the root's index; without it the index the "
                    "cycles grow is the one file nothing counts"
                )
                assert f"has {MEMORY_INDEX_ROW_CAP + 3} lines" in prompt

        asyncio.run(_test())

    def test_a_session_outside_the_root_does_not_get_the_root_index(self, monkeypatch):
        """The subject is a property of the session, not of the machine.

        A root index over the cap is not this session's to compact when its cwd lies
        outside that root: the note would name a file whose writer is some other
        session, and the two indexes this one does carry are the ones its own writer
        appends to. Without this half, "count the root too" would be "count every
        index you can reach".
        """
        from emrg.server import daemon
        from emrg.server.daemon import MEMORY_INDEX_ROW_CAP

        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve() / "instance"
                root.mkdir()
                monkeypatch.setattr(daemon, "EVOLUTION_CWD", root)
                cwd = Path(tmp).resolve() / "elsewhere"
                cwd.mkdir()
                session = Session.create_with_id("s_test_cap_outside", cwd)
                self._index(root / ".emrg" / "memory" / "MEMORY.md", MEMORY_INDEX_ROW_CAP + 3)
                server = _make_server({"content": "no new memories"})

                prompt = await _reflect(server, session)

                assert str(root) not in prompt, (
                    "a session outside the root must not be asked to compact the "
                    "root's index — it does not write that file"
                )
                assert "Memory index compaction" not in prompt

        asyncio.run(_test())

    def test_a_session_started_in_the_root_counts_that_index_once(self, monkeypatch):
        """When the cwd *is* the root, the third subject is the first one.

        A session started in the instance root carries `<cwd>/.emrg/memory/MEMORY.md`
        as its project index, and that is the very file `_instance_root_index` names —
        so an unde-duplicated list would render the same index's section twice, and
        the note's contract is one section per index ("an instruction naming the wrong
        file sends the agent to compact something that is not over the cap"): two
        sections naming one path read as two files.
        """
        from emrg.server import daemon
        from emrg.server.daemon import MEMORY_INDEX_ROW_CAP

        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                monkeypatch.setattr(daemon, "EVOLUTION_CWD", root)
                session = Session.create_with_id("s_test_cap_inroot", root)
                self._index(root / ".emrg" / "memory" / "MEMORY.md", MEMORY_INDEX_ROW_CAP + 1)
                server = _make_server({"content": "no new memories"})

                prompt = await _reflect(server, session)

                assert prompt.count("## Memory index compaction") == 1, (
                    "one index, one section — a subject named twice asks for the same "
                    "compaction twice"
                )

        asyncio.run(_test())

    def test_the_targets_in_the_text_come_from_the_constants(self):
        """The numbers the agent is told to reach are the rulers, not a second spelling."""
        from emrg.memory import INDEX_TITLE_MAX_CHARS
        from emrg.server.daemon import MEMORY_INDEX_ROW_CAP

        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_test_cap_nums", cwd)
                self._index(session.memory_dir / "MEMORY.md", MEMORY_INDEX_ROW_CAP + 1)
                server = _make_server({"content": "no new memories"})

                prompt = await _reflect(server, session)

                assert f"≤ {MEMORY_INDEX_ROW_CAP} lines" in prompt
                assert f"longer than {INDEX_TITLE_MAX_CHARS} chars" in prompt

        asyncio.run(_test())

    def test_an_unreadable_index_shows_no_section_instead_of_breaking_the_round(self):
        """A background task must not die on a torn read — and this prompt is not the
        carrier that reports one.

        The condition is reported, by the session's system prompt, which embeds an
        index and so has something for a notice to replace
        (`tests/test_daemon.py::test_an_unreadable_index_costs_the_section_not_the_turn`).
        This prompt carries no index text at all, so neither the section nor the
        notice belongs in it; the skip is safe for the other reason — the count is
        per-round and stateless, so the next round asks again.
        """
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                session = Session.create_with_id("s_test_cap_bad", cwd)
                index = session.memory_dir / "MEMORY.md"
                index.parent.mkdir(parents=True, exist_ok=True)
                index.write_bytes(b"\xff\xfe\x00 not utf-8")
                server = _make_server({"content": "no new memories"})

                prompt = await _reflect(server, session)

                assert prompt, "the reflection must still run"
                assert "Memory index compaction" not in prompt
                assert "could not be read" not in prompt, (
                    "this prompt carries no index text, so a notice here would "
                    "replace nothing — the file it names is still on disk and the "
                    "reading that failed is the next round's"
                )

        asyncio.run(_test())

    def test_the_wording_is_a_template_the_host_can_edit(self):
        """The instruction is a file, not a Python string (host 2026-09-25T15:10).

        The whole point of moving it is that editing the wording needs no Python —
        and each half of that can regress while the other still holds. Rendering
        alone is satisfied by a string constant: `_COMPACTION_NOTE`, which this
        replaced, was exactly that, and its text was invisible to anyone who wanted
        to change it. A template file existing alone is satisfied by a file nothing
        renders. So the fingerprint is asserted on the **rendered** text and then
        searched for across the package's Python: the template carries the wording,
        and no code does.

        The numbers are excluded from the fingerprint on purpose — they are
        placeholders the daemon fills from the rulers, and a template spelling them
        would be the second copy `test_the_targets_in_the_text_come_from_the_constants`
        exists to refuse.
        """
        from emrg.server import daemon

        package = Path(daemon.__file__).parent
        template_path = package / "prompts" / daemon.COMPACTION_TEMPLATE
        assert template_path.is_file(), (
            f"{daemon.COMPACTION_TEMPLATE} is what the daemon renders; a name that "
            "resolves to nothing is an undefined render, not a fallback"
        )
        template_text = template_path.read_text(encoding="utf-8")
        assert "{{ cap }}" in template_text and "{{ row_max }}" in template_text, (
            "the targets reach the template as placeholders, so it cannot drift "
            "into carrying its own reading of either number"
        )

        fingerprints = [
            "An index row is not a backup",
            "Never reference an archive file from the index.",
        ]
        for fingerprint in fingerprints:
            assert fingerprint in template_text, (
                f"{fingerprint!r} is the pinned wording of step 3/5; if it is gone "
                "from the template, this guard is measuring nothing"
            )

        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "MEMORY.md"
            index.write_text("\n" * (daemon.MEMORY_INDEX_ROW_CAP + 1), encoding="utf-8")
            rendered = daemon._memory_index_compaction_note([index])

        for fingerprint in fingerprints:
            assert fingerprint in rendered, (
                f"{fingerprint!r} is in the template but not in what the daemon "
                "sends — the render is not the file"
            )

        in_python = [
            str(py.relative_to(package))
            for py in sorted(package.rglob("*.py"))
            if any(f in py.read_text(encoding="utf-8") for f in fingerprints)
        ]
        assert in_python == [], (
            "the instruction's wording must live only in "
            f"{daemon.COMPACTION_TEMPLATE} — a copy in {in_python} can silently "
            "shadow it, and a host editing the template would then be editing "
            "nothing that is sent"
        )


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

