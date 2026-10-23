"""The Agent is one directory, and it stores that directory exactly as given.

Two properties these tests exist for:

* the value is **never normalised** — ``resolve()``, ``expanduser()`` and a trailing
  slash all change what a downstream reader compares against, so none of them may
  happen here;
* the ``.emrg`` layout is derived in **one place** — the assertions below pin the
  literal layout rather than comparing two things that both ask the Agent, because an
  equality between two derivations stays green under a mutated derivation. That is what
  makes an arm that changes the layout visible.
"""

from __future__ import annotations

from emrg.agent import Agent, as_agent
from emrg.memory import ProjectMemoryStore
from emrg.session import Session, generate_session_id, session_dir
from emrg.sessions_index import _iter_project_sessions


class TestBaseDirIsStoredAsGiven:
    """No resolve, no expanduser, no tidying — the value a caller gave survives."""

    def test_a_trailing_slash_survives(self):
        assert Agent("a/b/").base_dir == "a/b/"

    def test_a_dotdot_survives(self):
        """`resolve()` would collapse this to `b`; the string must not move."""
        assert Agent("a/../b").base_dir == "a/../b"

    def test_a_home_placeholder_is_not_expanded(self):
        """`expanduser()` would answer a real home directory; the `~` must survive."""
        assert Agent("~/project").base_dir == "~/project"

    def test_the_type_is_part_of_the_value(self, tmp_path):
        """A str stays a str and a Path stays a Path: two spellings are two values."""
        assert Agent(str(tmp_path)).base_dir == str(tmp_path)
        assert Agent(tmp_path).base_dir == tmp_path

    def test_wrapping_an_agent_answers_the_same_agent(self, tmp_path):
        """A second wrapper would be a second base_dir, which is the drift to avoid."""
        agent = Agent(tmp_path)
        assert as_agent(agent) is agent
        assert Agent(agent).base_dir == tmp_path
        assert Agent(agent) == agent

    def test_equality_does_not_normalise_either_side(self, tmp_path):
        assert Agent(tmp_path) == Agent(tmp_path)
        assert Agent(tmp_path) != Agent(tmp_path / "other")
        assert not Agent(tmp_path) == tmp_path


class TestTheLayoutIsDerivedInOnePlace:
    def test_the_three_directories(self, tmp_path):
        agent = Agent(tmp_path)
        assert agent.emrg_dir == tmp_path / ".emrg"
        assert agent.sessions_dir == tmp_path / ".emrg" / "sessions"
        assert agent.memory_dir == tmp_path / ".emrg" / "memory"

    def test_the_derivation_carries_the_value_it_was_handed(self):
        """A `~` and a `..` reach the filesystem as they were written."""
        sessions_dir = Agent("~/a/../b").sessions_dir
        assert str(sessions_dir).startswith("~")
        assert ".." in str(sessions_dir)


class TestASessionRunsInAnAgent:
    def test_a_path_and_an_agent_build_the_same_session(self, tmp_path):
        from_path = Session("s1", tmp_path)
        from_agent = Session("s1", Agent(tmp_path))

        assert from_path.agent == from_agent.agent
        assert from_path._dir == from_agent._dir == tmp_path / ".emrg" / "sessions" / "s1"
        assert from_path.cwd == from_agent.cwd == tmp_path

    def test_the_session_directory_is_the_agents_own_derivation(self, tmp_path):
        session = Session("s1", tmp_path)
        assert session._dir == Agent(tmp_path).sessions_dir / "s1"

    def test_cwd_is_read_only(self, tmp_path):
        """A second assignment could move the files and the cwd apart, silently."""
        session = Session("s1", tmp_path)
        try:
            session.cwd = tmp_path / "elsewhere"
        except AttributeError:
            pass
        else:
            raise AssertionError("cwd accepted an assignment")
        assert session.cwd == tmp_path

    def test_cwd_answers_the_value_it_was_given(self, tmp_path):
        """A caller that handed over a string reads that string back, not a Path."""
        session = Session("s1", str(tmp_path))
        assert session.cwd == str(tmp_path)
        assert session._dir == tmp_path / ".emrg" / "sessions" / "s1"


class TestTheCallersAskTheAgent:
    """The four sites that used to spell `<dir>/.emrg/...` themselves."""

    def test_session_dir_answers_the_literal_layout(self, tmp_path):
        assert session_dir(tmp_path, "s1") == tmp_path / ".emrg" / "sessions" / "s1"

    def test_session_dir_takes_an_agent_as_well_as_a_path(self, tmp_path):
        assert session_dir(Agent(tmp_path), "s1") == tmp_path / ".emrg" / "sessions" / "s1"

    def test_generate_session_id_reserves_an_id_it_does_not_create(self, tmp_path):
        sid = generate_session_id(tmp_path)
        assert sid.startswith("s_")
        assert not (Agent(tmp_path).sessions_dir / sid).exists()

    def test_list_sessions_answers_the_same_rows_for_either_spelling(self, tmp_path):
        Session.create_with_id("s1", tmp_path)
        from_agent = Session.list_sessions(Agent(tmp_path))
        assert from_agent == Session.list_sessions(tmp_path)
        assert [row["session_id"] for row in from_agent] == ["s1"]

    def test_the_project_memory_store_is_the_agents_memory_dir(self, tmp_path):
        store = ProjectMemoryStore(tmp_path)
        assert store.directory == Agent(tmp_path).memory_dir
        assert store.directory == tmp_path / ".emrg" / "memory"

    def test_the_session_index_walk_reads_the_agents_sessions_dir(self, tmp_path):
        session_dir_path = tmp_path / ".emrg" / "sessions" / "s1"
        session_dir_path.mkdir(parents=True)
        (session_dir_path / "meta.json").write_text(
            '{"session_id": "s1"}', encoding="utf-8"
        )

        assert list(_iter_project_sessions(tmp_path)) == [("s1", str(session_dir_path))]
