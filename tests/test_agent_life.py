"""An agent's life value: the bands, the one writer, and where the reader sees it.

An agent that has worked all week and one nobody has opened in a fortnight read
identically in the agent register, and nothing an agent does changes anything it
owns. This file holds the three properties that make a life value real rather than
decorative:

1. the bands fall where the boundaries read — *below* ten is dying, so ten
   survives; *above* a hundred thrives, so a hundred is still healthy;
2. the write is the judge's alone, and that is a statement about the tool list
   every other agent is offered, not about a check inside the tool (a tool cannot
   see who called it);
3. the value reaches its agent in the per-round context message, **outside** the
   frozen snapshot, so a score written a second ago is readable in the next round
   instead of at the end of a refresh window.

The third is the one that needs the most care to hold, because the freeze in
`_build_context_message` is there for a good reason (prompt caching) and a life
section placed inside it would pass every other test in this file.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.server import daemon as daemon_module
from emrg.server.agents import AgentRegistry
from emrg.server.daemon import (
    EmrgServer,
    build_tool_registry,
    default_tool_names,
    judge_tool_names,
)
from emrg.server.life import JUDGE_AGENT_NAME, judge_dir, life_section, tier_of
from emrg.session import Session
from emrg.tools.life_tools import GetAgentLifeTool, SetAgentLifeTool


def _make_server(**llm) -> EmrgServer:
    """A minimal server whose agents log points away from the host's real register.

    The shape `tests/test_memory_block_leaves_the_prefix.py::_make_server` uses, and
    for the same reason: building a prompt must never write `~/.emrg/agents.yml`.
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test", **llm))
    server._agents_log = Path(tempfile.mkdtemp()) / "agents.yml"
    return server


def _register_with(tmp_path: Path, *names: str) -> tuple[AgentRegistry, Path]:
    """A throwaway register holding one row per name, each on its own directory."""
    path = tmp_path / "agents.yml"
    registry = AgentRegistry(path)
    for name in names:
        (tmp_path / name).mkdir(exist_ok=True)
        registry.upsert(name, str(tmp_path / name))
    return registry, path


def _items(*points: int) -> list[dict]:
    """A judgement's items: one entry per weight, each naming what it was for.

    The write takes a list rather than a total, so every caller here builds the list it
    means. A weight of zero is deliberately not reachable through this helper — the tests
    that assert its refusal write the entry out in full.
    """
    return [{"points": p, "what": f"work worth {p:+d}"} for p in points]


# ── 1. the bands ──────────────────────────────────────────────────────

def test_the_bands_fall_where_the_boundaries_read() -> None:
    """Every floor is asserted on both sides of its edge.

    The numbers are `emrg/server/life.py::_BANDS` and are not restated as a sentence
    here: a sentence can name the wrong boundary and stay readable, while these
    assertions redden. Both sides of every edge are asserted because an off-by-one
    is otherwise invisible — the section would print a band and nothing would
    disagree with it.
    """
    assert tier_of(9) == "dying"
    assert tier_of(10) == "surviving"
    assert tier_of(49) == "surviving"
    assert tier_of(50) == "healthy"
    assert tier_of(100) == "healthy"
    assert tier_of(101) == "thriving"
    # No upper bound: a number far above the top band is simply thriving.
    assert tier_of(10_000) == "thriving"


def test_an_unjudged_agent_reads_as_unjudged_rather_than_zero() -> None:
    """Zero and "nobody has looked" are different states, and only one is a judgement."""
    section = life_section({"name": "emrg"})
    assert "unjudged" in section
    assert "0 (" not in section


def test_the_section_carries_the_value_the_band_and_the_last_change() -> None:
    section = life_section({
        "name": "emrg", "life": 37,
        "life_at": "2026-11-20T15:02:11+08:00", "life_delta": -5,
        "life_reason": "nothing shipped for a week",
    })
    assert "37" in section and "surviving" in section
    assert "2026-11-20T15:02:11+08:00" in section
    assert "-5" in section
    assert "nothing shipped for a week" in section


# ── 2. the write is the judge's alone ────────────────────────────────

def test_the_write_tool_is_offered_to_the_judge_and_to_nobody_else() -> None:
    """The rule, read in both directions at the one place that decides it."""
    base = ["bash", "read", "grep"]
    assert "set_agent_life" not in judge_tool_names(base, "/tmp/some/agent")
    assert "set_agent_life" in judge_tool_names(base, judge_dir())


def test_no_default_turn_carries_the_write_tool() -> None:
    """The schema every ordinary turn is offered, read as a schema.

    A name in the registry is not a name in a turn: every agent's schema comes from
    `to_openai_tools(default_tool_names())`, so that is where "no other agent can
    write a life value" has to hold.
    """
    registry = build_tool_registry(None, None)
    offered = {
        tool["function"]["name"]
        for tool in registry.to_openai_tools(default_tool_names())
    }
    assert "set_agent_life" not in offered, (
        "the write tool reached every agent's tool schema — the judge's privilege is "
        "decided by `judge_tool_names`, and the default set must not carry the name"
    )
    # The read tool, by contrast, is ordinary: reading is not a privilege here.
    assert "get_agent_life" in offered


def test_the_judge_does_not_carry_the_write_tool_twice() -> None:
    """A duplicated schema is a payload a provider may reject."""
    names = judge_tool_names(["read", "set_agent_life"], judge_dir())
    assert names.count("set_agent_life") == 1


# ── 3. the value and its history ─────────────────────────────────────

def test_a_first_judgement_records_no_delta_and_a_later_one_derives_it(
    tmp_path: Path,
) -> None:
    """The size of a change is derived from the row, never supplied by the caller."""
    registry, _ = _register_with(tmp_path, "emrg")
    first = registry.set_life("emrg", _items(40), "kept shipping")
    assert first is not None
    assert first["life"] == 40
    assert "life_delta" not in first, (
        "a jump from nothing to something is not a change of nought, so a first "
        "judgement stores an absence rather than a zero"
    )

    second = registry.set_life("emrg", _items(-3), "nothing landed")
    assert second is not None and second["life"] == 37
    assert second["life_delta"] == -3
    assert second["life_reason"] == "nothing landed"


def test_an_unjudged_row_keeps_the_shape_it_always_had(tmp_path: Path) -> None:
    """The register is the host's file: a row nobody judged must not grow null keys."""
    registry, path = _register_with(tmp_path, "emrg", "seek")
    registry.set_life("emrg", _items(12), "still here")
    rows = registry.entries()
    seek = next(r for r in rows if r["name"] == "seek")
    assert set(seek) == {"path", "name", "last_active"}, (
        f"the life fields were projected onto an unjudged row: {sorted(seek)}"
    )
    written = AgentRegistry(path).entries()
    assert all(r.get("name") for r in written)


def test_writing_a_value_for_an_unknown_agent_writes_nothing(tmp_path: Path) -> None:
    registry, path = _register_with(tmp_path, "emrg")
    before = path.read_text(encoding="utf-8")
    assert registry.set_life("nobody", _items(5), "why not") is None
    assert path.read_text(encoding="utf-8") == before


# ── 4. the tools ─────────────────────────────────────────────────────

def test_the_read_tool_answers_a_value_and_a_refusal(tmp_path: Path) -> None:
    registry, path = _register_with(tmp_path, "emrg")
    registry.set_life("emrg", _items(55), "steady")
    reader = GetAgentLifeTool(path)
    answered = asyncio.run(reader.execute({"agent": "emrg"}))
    assert not answered.error and "55" in answered.content and "healthy" in answered.content

    refused = asyncio.run(reader.execute({"agent": "nobody"}))
    assert refused.error and "nobody" in refused.content


_ONE_ITEM = [{"points": 4, "what": "delivered the result"}]


@pytest.mark.parametrize(
    "arguments, why",
    [
        ({"agent": "emrg", "items": [], "reason": "x", "searched": "read it"},
         "an empty list is not a judgement"),
        ({"agent": "emrg", "items": [{"points": 0, "what": "nothing"}], "reason": "x",
          "searched": "read it"},
         "a zero-point item would be read as work that counted"),
        ({"agent": "emrg", "items": [{"points": True, "what": "x"}], "reason": "x",
          "searched": "read it"},
         "a boolean is not a weight"),
        ({"agent": "emrg", "items": [{"points": "4", "what": "x"}], "reason": "x",
          "searched": "read it"},
         "a string is not a number"),
        ({"agent": "emrg", "items": [{"points": 4}], "reason": "x", "searched": "read it"},
         "an item that names nothing says nothing"),
        ({"agent": "emrg", "items": _ONE_ITEM, "reason": "   ", "searched": "read it"},
         "an empty reason hides why"),
        ({"agent": "emrg", "items": _ONE_ITEM, "reason": "x"},
         "an all-positive judgement with no search report is a clean bill on faith"),
        ({"items": _ONE_ITEM, "reason": "x", "searched": "read it"}, "no agent named"),
    ],
)
def test_the_write_tool_refuses_a_call_it_cannot_honour(
    tmp_path: Path, arguments: dict, why: str
) -> None:
    registry, path = _register_with(tmp_path, "emrg")
    before = path.read_text(encoding="utf-8")
    result = asyncio.run(SetAgentLifeTool(path).execute(arguments))
    assert result.error, f"{why}: {result.content!r}"
    assert path.read_text(encoding="utf-8") == before


# ── 5. where the agent sees it ───────────────────────────────────────

def test_a_score_written_inside_the_refresh_window_is_readable_next_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The placement: the life section is outside the frozen snapshot.

    The clock is deliberately frozen with a long window, so the only thing that can
    move between the two renders is the life value. A section placed inside the
    freeze would answer the first value twice and this test would be the only thing
    that noticed.
    """
    server = _make_server(context_refresh_interval_ms=3_600_000)
    registry, path = _register_with(tmp_path, "emrg")
    monkeypatch.setattr(daemon_module, "register_for_reading", lambda base=None: path)
    session = Session.create_with_id("life-window", tmp_path / "emrg")

    before = server._build_context_message(session)
    assert before is not None and "unjudged" in before["content"]

    registry.set_life("emrg", _items(55), "kept the work moving")

    after = server._build_context_message(session)
    assert after is not None
    assert "55 (healthy)" in after["content"], (
        "a score written inside the refresh window is not visible in the next round: "
        "the life section is rendering inside the frozen snapshot"
    )
    assert "kept the work moving" in after["content"]

    # The clock really was frozen — otherwise the assertion above would pass on a
    # tree that refreshes everything every round and the placement would be unproven.
    assert before["content"].splitlines()[0] == after["content"].splitlines()[0]


def test_the_life_section_stays_out_of_the_system_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefix is what a provider's cache matches on; a mutable value may not ride there."""
    server = _make_server()
    registry, path = _register_with(tmp_path, "emrg")
    registry.set_life("emrg", _items(55), "kept the work moving")
    monkeypatch.setattr(daemon_module, "register_for_reading", lambda base=None: path)
    session = Session.create_with_id("life-prefix", tmp_path / "emrg")

    rendered = server._build_system_prompt(session)
    assert "kept the work moving" not in rendered
    assert "Agent life" not in rendered


def test_a_session_in_a_directory_no_row_names_gets_no_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No row is no agent, and a section about nothing is worse than silence."""
    server = _make_server()
    _, path = _register_with(tmp_path, "emrg")
    monkeypatch.setattr(daemon_module, "register_for_reading", lambda base=None: path)
    session = Session.create_with_id("life-elsewhere", tmp_path / "somewhere-else")

    context = server._build_context_message(session)
    assert context is not None
    assert "Agent life" not in context["content"]


def test_the_judge_name_is_the_one_the_installer_registers() -> None:
    """One constant for the name, so the installer and the turn cannot disagree."""
    assert JUDGE_AGENT_NAME == "命"
    assert str(judge_dir()).endswith("life-judge")


# ── 4. the installer ──────────────────────────────────────────────────

def test_the_installer_creates_the_judge_row_and_its_directory() -> None:
    """``serve()`` gives the judge a directory and one row pointing at it.

    Driven through the daemon's own seams — ``register_for_reading`` for the file and
    ``life.judge_dir`` for the directory — so this measures the install as the daemon
    performs it rather than a rewritten copy of it. Both are redirected by the autouse
    fixture, which is also what keeps the host's register out of the run.

    The assertion that matters is the path one: a writer addressing the *name* the
    register belongs at instead of the file the daemon reads resolves past that
    redirect into ``~/.emrg/agents.yml``, where the suite's config guard refuses the
    write and every serving test fails on the aftermath.
    """
    server = _make_server()

    server._install_life_judge()

    path = daemon_module.register_for_reading()
    rows = [r for r in AgentRegistry(path).entries() if r.get("name") == JUDGE_AGENT_NAME]
    assert len(rows) == 1, "the installer left no judge row, or more than one"
    assert rows[0]["path"] == str(judge_dir())
    assert judge_dir().is_dir(), "the row points at a directory nobody created"


def test_a_second_install_leaves_the_judge_row_where_it_was() -> None:
    """A boot is not a judgement: the second call must not restamp the row it finds."""
    server = _make_server()
    server._install_life_judge()
    path = daemon_module.register_for_reading()
    first = AgentRegistry(path).entries()

    server._install_life_judge()

    assert AgentRegistry(path).entries() == first


# ── 6. the items are the judgement ───────────────────────────────────

def test_the_value_is_the_sum_of_the_items_and_not_a_stated_total(
    tmp_path: Path,
) -> None:
    """The number a judged agent reads is arithmetic on a list, never a figure beside it.

    +4 and -2 in one judgement is a net +2, which is the shape a ledger has to have: a
    round that delivered something and repeated a complaint reads as both, and the sum is
    what the value moves by.
    """
    registry, _ = _register_with(tmp_path, "emrg")
    first = registry.set_life(
        "emrg",
        [{"points": 4, "what": "delivered the result"},
         {"points": -2, "what": "asked again to say it plainly"}],
        "delivered, but the same complaint came back",
    )
    assert first is not None
    assert first["life"] == 2, "the value is not the sum of its items"
    assert [item["points"] for item in first["life_items"]] == [4, -2]

    second = registry.set_life("emrg", _items(3), "kept going")
    assert second is not None
    assert second["life"] == 5
    assert second["life_delta"] == 3


def test_a_malformed_item_list_raises_rather_than_storing_a_number(
    tmp_path: Path,
) -> None:
    """The register refuses a list it cannot sum, and writes nothing.

    Raising rather than answering ``None`` keeps the two "nothing was decided" answers
    meaning what they say: ``None`` is an unreadable register or no such row, and a caller
    that also received ``None`` for a malformed list could not tell the three apart.
    """
    registry, path = _register_with(tmp_path, "emrg")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        registry.set_life("emrg", [{"points": 0, "what": "nothing"}], "why not")
    assert path.read_text(encoding="utf-8") == before


def test_the_agent_reads_what_moved_its_value(tmp_path: Path) -> None:
    """The item list reaches the judged agent, not just its total.

    A value the agent can feel but cannot attribute is the defect this list exists to
    cure, so both readers that answer it are asserted here — the per-round context and
    the read tool — each on the text a person wrote rather than on a weight alone.
    """
    registry, path = _register_with(tmp_path, "emrg")
    registry.set_life(
        "emrg",
        [{"points": 4, "what": "delivered the result"},
         {"points": -2, "what": "asked again to say it plainly"}],
        "both happened",
    )
    section = life_section(next(r for r in registry.entries() if r["name"] == "emrg"))
    assert "+4  delivered the result" in section
    assert "-2  asked again to say it plainly" in section

    answered = asyncio.run(GetAgentLifeTool(path).execute({"agent": "emrg"}))
    assert not answered.error
    assert "delivered the result" in answered.content
    assert "asked again to say it plainly" in answered.content


def test_an_all_positive_judgement_must_report_its_search(tmp_path: Path) -> None:
    """A deduction has to be looked for, and the search is what proves it was."""
    registry, path = _register_with(tmp_path, "emrg")
    tool = SetAgentLifeTool(path)
    refused = asyncio.run(tool.execute(
        {"agent": "emrg", "items": _items(5), "reason": "kept shipping"}
    ))
    assert refused.error and "searched" in refused.content
    assert registry.entries()[0].get("life") is None, (
        "a refused judgement wrote a value anyway"
    )

    accepted = asyncio.run(tool.execute({
        "agent": "emrg", "items": _items(5), "reason": "kept shipping",
        "searched": "read the session's replies for objections, and the requirement "
                    "was stated once and honoured",
    }))
    assert not accepted.error, accepted.content
    row = registry.entries()[0]
    assert row["life"] == 5
    assert "read the session's replies for objections" in row["life_reason"], (
        "the search report was thrown away, so a reader cannot see what was looked at"
    )


def test_a_judgement_that_deducts_needs_no_search_report(tmp_path: Path) -> None:
    """The requirement reads the items, not their sum: +4 and -2 does find a deduction."""
    registry, path = _register_with(tmp_path, "emrg")
    answered = asyncio.run(SetAgentLifeTool(path).execute({
        "agent": "emrg", "items": _items(4, -2), "reason": "both happened",
    }))
    assert not answered.error, answered.content
    assert registry.entries()[0]["life"] == 2
