"""A streamed chunk that is not a chunk says so — it is not a raw exception.

The streaming twin of `tests/test_a_non_completion_is_named.py`, and the path
every ordinary conversation takes. Measured on this host 2026-10-03
(`cyc20261003-054214`) by driving the real `chat_stream` / a real round over a
stub transport — every chunk below is valid JSON:

==================================  =========================================
chunk sent                          what the caller got
==================================  =========================================
``[...]`` (a list)                  ``AttributeError: 'list' object has no attribute 'get'``
``"x"`` / ``5``                     ``AttributeError: 'str' / 'int' object has no attribute 'get'``
``{"choices": {…}}``                ``KeyError: 0`` — the message is the number ``0``
``{"choices": "x"}``                ``AttributeError: 'str' object has no attribute 'get'``
``{"choices": ["x"]}``              ``AttributeError: 'str' object has no attribute 'get'``
``{"choices": [null]}``             ``AttributeError: 'NoneType' object has no attribute 'get'``
``{"choices": [{"delta": null}]}``  ``AttributeError: 'NoneType' object has no attribute 'get'``
``{"choices": [{"delta": "x"}]}``   ``AttributeError: 'str' object has no attribute 'get'``
``{"usage": "x"}``                  ``AttributeError: 'str' object has no attribute 'get'``
==================================  =========================================

and one shape the reader *accepts* and then cannot join —
``{"delta": {"content": ["a"]}}`` — which killed the round in
``"".join(content_parts)`` with **no frame of any kind**: no error, no `done`,
so the client's busy flag is never cleared.

What is pinned here:

* the rule, over every measured shape and over the empty controls the reader
  already skips (refusing those would invent a failure for a chunk that runs
  today);
* the `delta` asymmetry, which is mechanical and measured: `choice.get("delta",
  {})` supplies its default only when the key is *absent*, so an explicit null
  is read and breaks while `{"content": []}` is skipped;
* through `chat_stream`: the named sentence, the retry while nothing has been
  yielded, and **no retry once a delta has been yielded** (the rule `yielded_delta`
  already enforces for transport failures);
* through a real round: the two shapes that used to kill it silently now end it
  with a frame;
* the classification, because a sentence that matched the overlong or refusal
  vocabularies would be acted on rather than reported.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server import llm as llm_mod
from emrg.server.daemon import EmrgServer
from emrg.server.llm import (
    MAX_RETRIES,
    OTHER_ERROR,
    LlmClient,
    SelfExplainingLlmError,
    chunk_shape_problem,
    classify_llm_error,
)
from emrg.session import Session

#: Every shape the reader breaks on today, with the part it breaks and the shape
#: word the sentence must use.
MALFORMED = [
    ([{"choices": []}], "the stream chunk is not an object", "list"),
    ("x", "the stream chunk is not an object", "str"),
    (5, "the stream chunk is not an object", "int"),
    ({"choices": {"delta": {"content": "hi"}}}, "`choices` is not a list", "dict"),
    ({"choices": "x"}, "`choices` is not a list", "str"),
    ({"choices": ["x"]}, "`choices[0]` is not an object", "str"),
    ({"choices": [None]}, "`choices[0]` is not an object", "null"),
    ({"choices": [{"delta": None}]}, "`choices[0].delta` is not an object", "null"),
    ({"choices": [{"delta": "x"}]}, "`choices[0].delta` is not an object", "str"),
    ({"choices": [{"delta": {"content": ["a", "b"]}}]},
     "`choices[0].delta.content` is not a string", "list"),
    ({"choices": [{"delta": {"content": 5}}]},
     "`choices[0].delta.content` is not a string", "int"),
    ({"choices": [{"delta": {"reasoning_content": ["a"]}}]},
     "`choices[0].delta.reasoning_content` is not a string", "list"),
    ({"choices": [{"delta": {"reasoning": ["a"]}}]},
     "`choices[0].delta.reasoning` is not a string", "list"),
    ({"choices": [{"delta": {"content": "hi"}}], "usage": "x"},
     "`usage` is not an object", "str"),
    ({"choices": [{"delta": {"content": "hi"}}], "usage": [1]},
     "`usage` is not an object", "list"),
]

#: Chunks this rule must leave alone, each measured as harmless today: the empty
#: ones (the reader's own `if not …` guards skip them) and the well-formed ones.
READABLE = [
    {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
    {"choices": [{"delta": {}}]},
    {"choices": [{"delta": {"content": None}}]},
    {"choices": [{"delta": {"content": ""}}]},
    {"choices": [{"tool_calls": [{"index": 0, "id": "c1"}]}]},
    {"choices": [{"finish_reason": "stop"}]},
    {"choices": []},
    {"choices": None},
    {"choices": {}},
    {"choices": ""},
    {"choices": 0},
    {"choices": [{"delta": {"content": []}}]},
    {"choices": [{"delta": {"content": 0}}]},
    {"choices": [{"delta": {"content": {}}}]},
    {"choices": [{"delta": {"reasoning_content": []}}]},
    {"choices": [{"delta": {"content": "hi"}}], "usage": {}},
    {"choices": [{"delta": {"content": "hi"}}], "usage": None},
    {"choices": [{"delta": {"content": "hi"}}], "usage": []},
    {"choices": [{"delta": {"content": "hi"}}], "usage": 0},
    # Deliberately unchecked: it breaks no read (the value is only compared to
    # two strings), so a rule about it would be a behaviour change.
    {"choices": [{"delta": {"content": "hi"}, "finish_reason": 5}]},
    {"choices": [{"delta": {"content": "hi"}, "finish_reason": []}]},
    {"id": "1"},
]


# ── the rule ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("chunk,part,shape", MALFORMED)
def test_every_measured_shape_is_refused_by_name(chunk, part, shape):
    problem = chunk_shape_problem(chunk)
    assert problem is not None, f"{chunk!r} was accepted"
    assert part in problem, problem
    assert f"got {shape}" in problem, problem


@pytest.mark.parametrize("chunk", READABLE)
def test_a_readable_chunk_is_not_refused(chunk):
    assert chunk_shape_problem(chunk) is None, chunk


def test_delta_is_the_one_field_read_even_when_empty():
    """The asymmetry, and the reason it is not a slip.

    Every other field is read behind a truthiness test (`if not choices: continue`,
    `if text_content:`), so an empty off-contract value is skipped. `delta` is
    not: `choice.get("delta", {})` supplies its default only when the key is
    *absent*, so an explicit null — or any other falsy non-object — is read as it
    is and breaks. A rule that treated the two alike would leave `{"delta": null}`
    crashing while promising it had been checked.
    """
    for empty in (None, [], 0, ""):
        assert chunk_shape_problem({"choices": [{"delta": empty}]}) is not None, empty
    # …and the field next to it, where the empty value IS skipped.
    assert chunk_shape_problem({"choices": [{"delta": {"content": empty}}]}) is None


def test_a_missing_delta_is_not_a_shape_problem():
    """Absent is what the reader's own default is for."""
    assert chunk_shape_problem({"choices": [{"finish_reason": "stop"}]}) is None


# ── through chat_stream, which is where the caller meets them ───────────────


class _Stream:
    status_code = 200
    headers = {}

    def __init__(self, lines):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for chunk in self.lines:
            # The entries are decoded values, this is the wire: a scalar's
            # Python value and its JSON text are not the same string
            # (`5` vs `"5"`), so the encoding lives here and nowhere else.
            yield "data: " + json.dumps(chunk)
        yield "data: [DONE]"


class _Transport:
    """Answers every attempt with the same lines unless told otherwise."""

    def __init__(self, lines, *, later=None):
        self.lines = lines
        self.later = later
        self.calls = 0

    def stream(self, method, url, headers=None, json=None):
        self.calls += 1
        if self.calls == 1 or self.later is None:
            return _Stream(self.lines)
        return _Stream(self.later)


GOOD = [{"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}]


def _drive(lines, monkeypatch, *, later=None, client=None):
    """Drain one real `chat_stream`; return (yields, transport)."""
    async def fast_sleep(_delay):
        pass

    monkeypatch.setattr(llm_mod.asyncio, "sleep", fast_sleep)
    client = client or LlmClient(LlmConfig(base_url="http://localhost", api_key="test"))
    transport = _Transport(lines, later=later)
    client._client = transport
    seen = []

    async def go():
        async for chunk in client.chat_stream([{"role": "user", "content": "hi"}]):
            seen.append(chunk)

    asyncio.run(go())
    return seen, transport


@pytest.mark.parametrize("chunk,part,shape", MALFORMED)
def test_the_named_sentence_reaches_the_caller(chunk, part, shape, monkeypatch):
    with pytest.raises(SelfExplainingLlmError) as excinfo:
        _drive([chunk], monkeypatch)
    message = str(excinfo.value)
    assert part in message, message
    assert "is not a stream this client can read" in message, message


@pytest.mark.parametrize("chunk,part,shape", MALFORMED)
def test_the_raw_python_sentence_never_reaches_the_caller(chunk, part, shape, monkeypatch):
    """The whole defect, as one assertion: the caller is not shown Python."""
    with pytest.raises(SelfExplainingLlmError) as excinfo:
        _drive([chunk], monkeypatch)
    message = str(excinfo.value)
    for raw in ("has no attribute", "sequence item", "KeyError", "subscriptable"):
        assert raw not in message, f"{raw!r} is the old raw sentence: {message}"


def test_a_bad_chunk_is_retried_while_nothing_has_been_yielded(monkeypatch):
    """The treatment: a gateway artefact is worth re-asking once."""
    seen, transport = _drive(
        [{"choices": {"delta": {}}}], monkeypatch, later=GOOD)
    assert transport.calls == 2, transport.calls
    assert [s["content"] for s in seen] == ["done"], seen


def test_a_bad_chunk_is_not_retried_once_a_delta_has_been_yielded(monkeypatch):
    """The stream's own limit, and the arm that keeps the retry honest.

    Re-sending an attempt whose text the client is already showing would
    duplicate it — the rule `yielded_delta` already enforces for transport
    failures, and a chunk refusal must not be the one hole in it.
    """
    # A `finish_reason` would end the stream right after the yield, so the bad
    # chunk has to follow a delta that does *not* finish the answer.
    first = {"choices": [{"delta": {"content": "he"}}]}
    with pytest.raises(SelfExplainingLlmError) as excinfo:
        _drive([first, {"choices": {"delta": {}}}], monkeypatch, later=GOOD)
    assert "`choices` is not a list" in str(excinfo.value)


def test_a_chunk_refusal_spends_the_whole_budget_when_nothing_was_yielded(monkeypatch):
    async def fast_sleep(_delay):
        pass

    monkeypatch.setattr(llm_mod.asyncio, "sleep", fast_sleep)
    client = LlmClient(LlmConfig(base_url="http://localhost", api_key="test"))
    transport = _Transport([{"choices": {"delta": {}}}])
    client._client = transport

    async def go():
        async for _ in client.chat_stream([{"role": "user", "content": "hi"}]):
            pass

    with pytest.raises(SelfExplainingLlmError):
        asyncio.run(go())
    assert transport.calls == MAX_RETRIES + 1, transport.calls


def test_a_well_formed_stream_is_untouched(monkeypatch):
    """The control direction: a real stream still arrives, in order."""
    seen, transport = _drive(
        [{"choices": [{"delta": {"content": "he"}}]},
         {"choices": [{"delta": {"content": "llo"}, "finish_reason": "stop"}]}],
        monkeypatch,
    )
    assert transport.calls == 1, "a good stream was re-sent"
    assert [s["content"] for s in seen] == ["he", "llo"], seen
    assert seen[-1]["finish_reason"] == "stop"


@pytest.mark.parametrize("chunk,part,shape", MALFORMED)
def test_the_refusal_is_not_mistaken_for_an_overlong_request(chunk, part, shape, monkeypatch):
    """A wrong classification is a wrong action: the chunker re-sends the request."""
    with pytest.raises(SelfExplainingLlmError) as excinfo:
        _drive([chunk], monkeypatch)
    assert classify_llm_error(excinfo.value) == OTHER_ERROR, str(excinfo.value)


# ── through a real round: the shapes that used to kill it silently ──────────


class _RoundTransport:
    def __init__(self, lines):
        self.lines = lines
        self.calls = 0

    def stream(self, method, url, headers=None, json=None):
        self.calls += 1
        return _Stream(self.lines)


def _drive_round(lines, tmp_path, monkeypatch):
    """Run one real round over a stub transport; return the frames it sent."""
    async def fast_sleep(_delay):
        pass

    monkeypatch.setattr(llm_mod.asyncio, "sleep", fast_sleep)

    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = tmp_path / "projects.yml"
    session = Session.create_with_id("chunk-round", tmp_path)
    frames: list[dict] = []

    async def fake_broadcast(session_id, frame):
        frames.append(frame)

    server._broadcast = fake_broadcast
    server.llm._client = _RoundTransport(lines)
    req = TaskRequest(id="r", session_id=session.session_id, prompt="go")
    asyncio.run(asyncio.wait_for(server._run_tool_loop(req, None, session), 20))
    return session, frames


@pytest.mark.parametrize("content", [["a", "b"], 5])
def test_a_non_string_content_no_longer_kills_the_round_without_a_frame(
    content, tmp_path, monkeypatch
):
    """The worst of the measured shapes.

    Before the change the round raised `TypeError: sequence item 0: expected str
    instance, list found` out of `_run_tool_loop`, having broadcast **nothing** —
    no error frame, no `done` — so the client's busy flag was never cleared. The
    leg asserts the frame, which is the thing a user sees.
    """
    _session, frames = _drive_round(
        [{"choices": [{"delta": {"content": content}}]}], tmp_path, monkeypatch)
    errors = [f["error"] for f in frames if f.get("error")]
    assert len(errors) == 1, (errors, frames)
    assert "`choices[0].delta.content` is not a string" in errors[0], errors[0]
    assert any(f.get("done") for f in frames), "the round ended without a done frame"


def test_a_well_formed_round_is_untouched(tmp_path, monkeypatch):
    """The control direction at the same site: text still reaches the client."""
    session, frames = _drive_round(
        [{"choices": [{"delta": {"content": "hello"}}]},
         {"choices": [{"delta": {}, "finish_reason": "stop"}]}],
        tmp_path, monkeypatch)
    assert not [f for f in frames if f.get("error")], frames
    deltas = [f for f in frames if f.get("delta")]
    assert [f["content"] for f in deltas] == ["hello"], frames
    persisted = [m for m in session._read_history() if m.get("role") == "assistant"]
    assert [m.get("content") for m in persisted] == ["hello"], persisted
