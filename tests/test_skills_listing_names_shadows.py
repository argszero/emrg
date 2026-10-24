"""The host's `/skills` screen names a declaration a name collision displaced.

Issue #2569. The list called ``load_skills(...)``, which is
``load_skills_with_shadows(...)[0]`` — the wrapper drops the second list — so a host whose
user-root ``foo`` was displaced by an agent-root ``foo`` was shown one ``foo`` labelled
``agent`` and nothing on that screen said the user copy existed and was ignored. The
prompt had told the *model* which declaration lost; the *host's* own screen had not.

Four properties below, one per test: the listing names both declarations of a displaced
name, a tree without a collision lists exactly what it listed before, an empty tree returns
nothing so the caller keeps its own advice, and every carrier of this rule (the listing,
``build_skills_context``, ``system.j2``) names the same two files. A fifth test asks the
client module which loader it can reach, which is the one regression the behaviour tests
cannot see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emrg.client.app import skills_listing
from emrg.config import LlmConfig
from emrg.server.daemon import EmrgServer
from emrg.session import Session
from emrg.skills import loader as loader_mod
from emrg.skills import registry as registry_mod
from emrg.skills.loader import build_skills_context, load_skills_with_shadows

SKILL_TEXT = "---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nBody.\n"


def _skill(name: str, description: str = "a skill") -> str:
    return SKILL_TEXT.format(name=name, description=description)


def _server(tmp_path: Path) -> EmrgServer:
    """A daemon built the way the daemon tests build one."""
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = tmp_path / "agents.yml"
    return server


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """A user root and an agent root, both off the real home (and both empty)."""
    data = tmp_path / "home" / ".emrg"
    (data / "skills").mkdir(parents=True)
    monkeypatch.setattr(loader_mod, "config_dir", lambda: data)
    monkeypatch.setattr(registry_mod, "config_dir", lambda: data)

    agent = tmp_path / "agent"
    (agent / ".git").mkdir(parents=True)
    (agent / ".emrg" / "skills").mkdir(parents=True)
    return data / "skills", agent


def test_the_listing_names_the_declaration_a_collision_displaced(roots):
    """Both declarations, each with its source and path — not one line for the winner."""
    user_root, agent = roots
    user_file = user_root / "foo.md"
    user_file.write_text(_skill("foo", "the host's own copy"), encoding="utf-8")
    agent_file = agent / ".emrg" / "skills" / "foo.md"
    agent_file.write_text(_skill("foo", "the agent's copy"), encoding="utf-8")

    text = "\n".join(skills_listing(agent))

    assert "Shadowed names" in text, "the screen says nothing about the displaced declaration"
    assert f"in effect - agent, `{agent_file}`" in text, "the winner is not named with its path"
    assert f"shadowed - user, `{user_file}`" in text, "the loser is not named with its path"


def test_a_tree_without_a_collision_lists_exactly_what_it_listed_before(roots):
    """The extra lines appear only where a declaration was actually displaced."""
    _, agent = roots
    (agent / ".emrg" / "skills" / "solo.md").write_text(
        _skill("solo", "only here"), encoding="utf-8"
    )

    assert skills_listing(agent) == [
        "**Loaded Skills:**",
        "",
        "- **solo** (agent) — only here",
    ], "a tree with no collision must read as it read before the shadow list was rendered"


def test_an_empty_tree_returns_nothing_so_the_caller_keeps_its_advice(roots):
    """An empty list is the caller's signal to print its own "no skills" line."""
    _, agent = roots
    assert skills_listing(agent) == []


def test_every_carrier_names_the_same_displaced_declaration(roots, tmp_path):
    """One rule, three renderers — a fourth wording is how the second implementation returns."""
    user_root, agent = roots
    user_file = user_root / "foo.md"
    user_file.write_text(_skill("foo", "the host's own copy"), encoding="utf-8")
    agent_file = agent / ".emrg" / "skills" / "foo.md"
    agent_file.write_text(_skill("foo", "the agent's copy"), encoding="utf-8")

    skills, shadows = load_skills_with_shadows(agent_dir=agent)
    carriers = {
        "the /skills listing": "\n".join(skills_listing(agent)),
        "build_skills_context": build_skills_context(skills, shadows),
        "system.j2": _server(tmp_path)._build_system_prompt(
            Session.create_with_id("skills-shadow-carriers", agent)
        ),
    }

    for carrier, rendered in carriers.items():
        assert "Shadowed names" in rendered, f"{carrier} does not name the collision"
        assert str(user_file) in rendered, f"{carrier} omits the declaration that lost the name"
        assert str(agent_file) in rendered, f"{carrier} omits the declaration in effect"


def test_the_client_has_no_route_to_the_list_with_the_shadows_dropped():
    """The dropping wrapper is not reachable from the client at all, and this is what says so.

    The behaviour tests above drive `skills_listing` directly, so on their own they stay
    green if the `/skills` branch went back to the dropping wrapper and a renderer of its
    own. A `hasattr` check is the reading that catches it — the same shape
    `test_skills_read_live.py` uses for the daemon's stale skills snapshot, and stronger
    than a source grep, which reads prose too: a docstring naming `load_skills(...)` while
    explaining why it is not called is exactly the false positive this replaces.
    """
    import emrg.client.app as app_mod

    assert hasattr(app_mod, "skills_listing"), (
        "the client no longer renders its skill list through `skills_listing`, so nothing "
        "here says what the host's screen does with a displaced declaration"
    )
    assert hasattr(app_mod, "load_skills_with_shadows"), (
        "the client does not reach the loader that reports displaced declarations"
    )
    assert not hasattr(app_mod, "load_skills"), (
        "the dropping wrapper is reachable from the client again: `load_skills()` is "
        "`load_skills_with_shadows()[0]`, so the /skills screen can go back to hiding the "
        "declaration a collision displaced (#2569)"
    )
