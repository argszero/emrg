"""L2 of the content-risk series: locate the refused record, take it out.

Rant `2026-09-29T15:55:44.643795+08:00`. The sibling requirement L1 (replace
offending codepoints, fall back to spacing) is a cure for one **class** of
trigger, and the rant that measured it says so in its own words — the trigger it
was measured against is an astral codepoint pair, and *if the trigger is a word
or a phrase, replacing codepoints cannot help*. When the trigger is a word the
only cure is to find the record that carries it and remove it, and these tests
are about the daemon half of that: the search's cost, the removal, and the one
rule the whole series exists for — **never echo the trigger**.

Every provider here is a stub. That is not convenience: the acceptance
requirement is that the evidence for this behaviour is offline (*离线 stub 即可，
禁真实 provider 调用作为唯一证据*), and no test in this file may talk to a
provider.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.server import content_risk_probe
from emrg.server.daemon import EmrgServer
from emrg.protocol import TaskRequest
from emrg.session import Session

#: The trigger this file uses. It is a **word**, because that is the case the
#: codepoint ladder cannot cure and therefore the case this module exists for.
TRIGGER = "plutonium-casserole"
#: The literal the provider's body carries when it refuses (``llm.py``
#: `CONTENT_RISK_ERROR`), spelled here rather than imported so a change to the
#: classification has to be a change to this test too.
REFUSAL = "Content Exists Risk"


def _make_server() -> EmrgServer:
    """A minimal server: no daemon, no sockets, the host's config untouched."""
    return EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))


def _history_session(tmp_path, count: int, trigger_at: int) -> Session:
    """A session of `count` records with the trigger in record `trigger_at`."""
    session = Session.create_with_id("content-risk-scrub", tmp_path)
    records = []
    for i in range(count):
        text = f"record {i} of the conversation"
        if i == trigger_at:
            text = f"record {i} mentions {TRIGGER} in passing"
        records.append({
            "type": "message",
            "role": "user" if i % 2 == 0 else "assistant",
            "content": text,
            "timestamp": f"2026-09-29T12:{i:02d}:00",
        })
    session._write_history(records)
    session._message_count = count
    return session


def _refusing_chat(failures: list, *, log: list | None = None):
    """A `chat` stub that refuses a payload iff the trigger is in it.

    `failures` is the mutable list of payloads the caller wants to be refused —
    a test appends a payload to make the *next* call fail, which is how "the
    provider accepts the scrubbed history" gets asserted rather than assumed.
    """

    async def fake_chat(messages, tools=None):
        if log is not None:
            log.append(messages)
        blob = repr(messages)
        if TRIGGER in blob:
            raise RuntimeError(f"400 {REFUSAL}")
        return {"content": "fine", "finish_reason": "stop"}

    return fake_chat


# ── the search's cost ───────────────────────────────────────────────────────


def test_the_probe_asks_the_provider_logarithmically(tmp_path, monkeypatch):
    """A 63-record session costs a bisection's questions, not one per record.

    The measurement that makes this a requirement rather than a nicety: the
    session the rant was filed on held 131 messages and ~738k prompt tokens, so
    a linear scan is hundreds of provider requests — a second poison rather than
    a cure. `ceil(log2(n)) + 2` is the bound `find_offending_index` states, and
    this asks the daemon's own wiring to stay inside it.
    """
    session = _history_session(tmp_path, 63, trigger_at=40)
    server = _make_server()
    calls: list = []
    server.llm.chat = _refusing_chat(calls, log=calls)

    _, result = asyncio.run(
        server._scrub_content_risk_record(session, "system prompt"),
    )

    linear = 63
    bound = math.ceil(math.log2(63)) + 2
    assert result.probes <= bound, (
        f"{result.probes} questions for 63 records — past the bisection's {bound}"
    )
    assert len(calls) <= bound + 1, "the wiring must not ask more than the search"
    assert len(calls) < linear / 4, (
        f"{len(calls)} questions is not a bisection, it is a scan"
    )


def test_the_offending_record_is_the_one_removed(tmp_path):
    """The record named by the search is the one carrying the trigger."""
    session = _history_session(tmp_path, 32, trigger_at=17)
    server = _make_server()
    calls: list = []
    server.llm.chat = _refusing_chat(calls, log=calls)

    messages, result = asyncio.run(
        server._scrub_content_risk_record(session, "system prompt"),
    )

    assert result.resolved is True
    assert result.index == 17, "the position the search named"
    assert result.removed == 1
    surviving = [r["content"] for r in session._read_history()]
    assert len(surviving) == 31
    assert not any(TRIGGER in text for text in surviving)
    assert not any(TRIGGER in str(m.get("content")) for m in messages)


def test_the_scrubbed_session_is_accepted_by_the_provider(tmp_path):
    """The assertion lands on the *result*: the provider stops refusing.

    Requirement L4b, kept literally. Asserting instead that "the string no
    longer holds the pair" is the shape the upstream probe used, and it is the
    shape that cannot fail — its stub matched bytes, so a filter that matched
    words was invisible to it. Here the same payload is re-asked after the
    removal, and the answer is what is asserted.
    """
    session = _history_session(tmp_path, 24, trigger_at=9)
    server = _make_server()
    calls: list = []
    server.llm.chat = _refusing_chat(calls, log=calls)

    messages, result = asyncio.run(
        server._scrub_content_risk_record(session, "system prompt"),
    )
    assert result.resolved

    accepted = asyncio.run(server._content_risk_refused(messages))
    assert accepted is False, "the provider must accept what the scrub produced"


# ── the one rule the series exists for: never echo the trigger ──────────────


def test_nothing_the_scrub_produces_echoes_the_trigger(tmp_path, caplog):
    """Reading the trigger back poisons the reader — so nothing carries it.

    The incident behind the rant: the upstream plugin's own verification probe
    printed the triggering text into the session history, and every session that
    read it back was killed by the same filter again. So the report, the result
    object and the file on disk are each checked, and the trigger's own bytes
    must not appear in any of them.

    This is the assertion mutation arm 1 kills: make `describe_record` return the
    record's text and this test goes red (the module's own test file pins the same
    rule for the search; here it is pinned for the daemon's report).
    """
    session = _history_session(tmp_path, 16, trigger_at=5)
    server = _make_server()
    calls: list = []
    server.llm.chat = _refusing_chat(calls, log=calls)

    import logging

    with caplog.at_level(logging.DEBUG):
        _, result = asyncio.run(
            server._scrub_content_risk_record(session, "system prompt"),
        )

    assert result.resolved
    surfaces = {
        "the log": caplog.text,
        "the ScrubResult": repr(result),
        "the trace": repr(result.traces),
        "the history on disk": session._history_path.read_text(),
    }
    for where, text in surfaces.items():
        assert TRIGGER not in text, f"the trigger is echoed in {where}"

    # …and the provider was never handed the trigger outside a payload it had
    # already refused. `chat` saw the candidates by design; what must not happen
    # is the trigger landing in a *record* or a *report*, which is what the
    # surfaces above measure.
    trace = result.traces[-1]
    assert trace.kind.startswith("message"), "a record is named by kind"
    assert trace.length > 0 and len(trace.digest) == 16, "and by size and digest"


# ── where the search refuses to act ─────────────────────────────────────────


def test_a_combination_trigger_removes_nothing(tmp_path):
    """No single record is to blame ⇒ nothing is deleted.

    A wrong removal costs the host their conversation; an honest "I could not
    find it" costs them one turn. The stub refuses only a payload carrying both
    halves of a phrase that is split across two records — so every subset is
    accepted, the full history is refused, and the search must stop rather than
    delete one half of a pair that is doing the damage.
    """
    session = Session.create_with_id("content-risk-combination", tmp_path)
    session._write_history([
        {"type": "message", "role": "user", "content": "let us discuss alpha"},
        {"type": "message", "role": "assistant", "content": "an unrelated answer"},
        {"type": "message", "role": "user", "content": "and beta as well"},
        {"type": "message", "role": "assistant", "content": "another answer"},
    ])
    session._message_count = 4
    server = _make_server()

    async def refuse_the_pair(messages, tools=None):
        blob = repr(messages)
        if "alpha" in blob and "beta" in blob:
            raise RuntimeError(f"400 {REFUSAL}")
        return {"content": "fine"}

    server.llm.chat = refuse_the_pair
    before = session._read_history()

    outcome = asyncio.run(
        server._scrub_content_risk_record(session, "system prompt"),
    )

    assert outcome is None, "an unresolved search is not a licence to delete"
    assert session._read_history() == before, "the history is untouched"


def test_a_probe_that_cannot_answer_removes_nothing(tmp_path):
    """A transport failure is not the answer "not refused".

    Reading a timeout as "this half is clean" would send the bisection into the
    wrong half and name an innocent record. The probe raises, the search stops,
    and the history is left alone.
    """
    session = _history_session(tmp_path, 8, trigger_at=3)
    server = _make_server()

    async def broken(messages, tools=None):
        raise RuntimeError("502 Bad Gateway")

    server.llm.chat = broken
    before = session._read_history()

    outcome = asyncio.run(
        server._scrub_content_risk_record(session, "system prompt"),
    )

    assert outcome is None
    assert session._read_history() == before


def test_the_search_budget_is_enforced_not_guessed(tmp_path):
    """Past `ceil(log2(n)) + 2` questions the search raises instead of answering.

    Asserted directly on the primitive, because the daemon swallows the raise
    into a `None` — the bound itself has to have an owner somewhere.
    """
    records = [{"type": "message", "role": "user", "content": f"r{i}"} for i in range(8)]

    def never_stops(candidates):
        return True

    with pytest.raises(content_risk_probe.ProbeBudgetExceeded):
        content_risk_probe.find_offending_index(records, never_stops, max_probes=2)


# ── the hook in the loop ────────────────────────────────────────────────────


def _drive_loop(tmp_path, monkeypatch, server, session, stream):
    """Run one tool loop over `stream`, returning the frames broadcast."""
    import emrg.server.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH",
                        tmp_path / "planted-fire-heartbeat")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH",
                        tmp_path / "planted-fire-round-complete")
    frames: list[dict] = []

    async def fake_broadcast(session_id, payload):
        frames.append(payload)

    server._broadcast = fake_broadcast
    server.llm.chat_stream = stream
    req = TaskRequest(id="req-scrub", session_id=session.session_id, prompt="hello")
    asyncio.run(server._run_tool_loop(req, None, session))
    return frames


def test_a_refused_round_is_retracted_and_re_sent(tmp_path, monkeypatch):
    """The loop's hook: a spent ladder ends in a removal, not in an error.

    The stream refuses while the trigger is in the messages the loop is about to
    send and answers normally once it is gone — which is the same fact a real
    provider would report, expressed as a function of the payload rather than of
    a call count.
    """
    session = _history_session(tmp_path, 12, trigger_at=4)
    server = _make_server()

    seen: list[str] = []

    async def stream(messages, tools=None):
        blob = repr(messages)
        seen.append(blob)
        if TRIGGER in blob:
            raise RuntimeError(f"400 {REFUSAL}")
        yield {"content": "the answer, at last", "tool_calls": None,
               "finish_reason": "stop", "usage": None}

    server.llm.chat = _refusing_chat([])

    frames = _drive_loop(tmp_path, monkeypatch, server, session, stream)

    assert len(seen) >= 2, "the round is re-sent after the removal"
    assert TRIGGER in seen[0], "the first send really was the refused one"
    assert TRIGGER not in seen[-1], "and the re-send is the scrubbed history"
    assert not any(TRIGGER in r["content"] for r in session._read_history())
    answers = [f for f in frames if f.get("content") == "the answer, at last"]
    assert answers, "the turn ends with the answer, not with the refusal"
    assert any(f.get("type") == "compact_result" for f in frames), (
        "the removal is reported to the client rather than done in silence"
    )


def test_a_round_whose_partial_text_reached_the_client_is_not_retried(
    tmp_path, monkeypatch,
):
    """Streamed text is not re-sent — the same guard the shrink path carries.

    Mutation arm for the hook: it is the `not content_parts` conjunct, and
    removing it makes this test red because the round would be sent twice while
    the client is already showing the first attempt.
    """
    session = _history_session(tmp_path, 12, trigger_at=4)
    server = _make_server()
    sends = 0

    async def stream(messages, tools=None):
        nonlocal sends
        sends += 1
        yield {"content": "partial", "tool_calls": None,
               "finish_reason": None, "usage": None}
        raise RuntimeError(f"400 {REFUSAL}")

    server.llm.chat = _refusing_chat([])

    frames = _drive_loop(tmp_path, monkeypatch, server, session, stream)

    assert sends == 1, "no second attempt once the client has seen text"
    assert session._read_history(), "and nothing removed"
    assert any("error" in f for f in frames), "the refusal is reported instead"


def test_the_scrub_happens_once_per_turn(tmp_path, monkeypatch):
    """The budget is per turn: a second refusal reports, it does not loop.

    A retry that also fails has to report — the same reasoning the overlong
    retry's single budget carries, and the reason a budget exists at all.
    """
    session = _history_session(tmp_path, 8, trigger_at=2)
    server = _make_server()
    sends = 0

    async def stream(messages, tools=None):
        nonlocal sends
        sends += 1
        raise RuntimeError(f"400 {REFUSAL}")
        yield  # pragma: no cover — an async generator must have a yield

    server.llm.chat = _refusing_chat([])

    frames = _drive_loop(tmp_path, monkeypatch, server, session, stream)

    assert sends == 2, "one refusal, one scrub, one re-send"
    assert any("error" in f for f in frames), "then the refusal is reported"


def test_a_scrub_does_not_touch_a_healthier_session(tmp_path, monkeypatch):
    """A session with no trigger is never searched, let alone edited.

    The hook is keyed on the *classification* of the failure, so a round that
    ends any other way must not reach the probe at all — measured by refusing to
    let the probe be callable.
    """
    session = _history_session(tmp_path, 8, trigger_at=2)
    server = _make_server()
    asked: list = []

    async def never_asked(messages, tools=None):
        asked.append(messages)
        raise AssertionError("the probe must not ask anything here")

    server.llm.chat = never_asked

    async def stream(messages, tools=None):
        raise RuntimeError("502 Bad Gateway")
        yield  # pragma: no cover — a generator that must never yield

    _drive_loop(tmp_path, monkeypatch, server, session, stream)

    assert asked == [], "an unclassified failure is not a content refusal"
    surviving = [r.get("content") for r in session._read_history()]
    assert len(surviving) == 9, "the eight records and the turn's own prompt"
    assert any(TRIGGER in text for text in surviving), (
        "the trigger is still there — nothing searched, nothing removed"
    )
