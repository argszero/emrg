"""A 200 that parses and is not a completion says so — it is not a raw exception.

Measured on this host 2026-10-03 (`cyc20261003-051925`) by calling the real
`LlmClient.chat()` over a stub transport (nothing else stubbed):

==================================  =========================================
body sent                           what the caller got
==================================  =========================================
``[]``                              ``TypeError: list indices must be integers``
``"x"``                             ``TypeError: string indices must be integers``
``{"id": "1"}``                     ``KeyError: 'choices'``
``{"choices": null}``               ``TypeError: 'NoneType' object is not subscriptable``
``{"choices": {…}}``                ``KeyError: 0``
``{"choices": []}``                 ``IndexError: list index out of range``
``{"choices": [null]}``             ``AttributeError: 'NoneType' object has no attribute 'get'``
``{"choices": ["x"]}``              ``AttributeError: 'str' object has no attribute 'get'``
``{"choices": [{"message": null}]}``  ``AttributeError: 'NoneType' object has no attribute 'get'``
``{"choices": [{"message": "ok"}]}``  ``AttributeError: 'str' object has no attribute 'get'``
``{"choices": [{"message": {"content": ["a"]}}]}``  *returned* — a list is stored as the summary
``{"choices": [{"message": {"content": 5}}]}``      *returned* — an int reaches the callers
``{"choices": [{"finish_reason": "stop"}]}``  ``RuntimeError`` — the existing "no answer at all" refusal
==================================  =========================================

Ten bodies, ten raw Python exceptions naming neither the provider, the field nor
the shape — and two that do not raise at all and hand a payload no caller can
read to whoever asked. This is the same family as the malformed tool-call entry
of `cyc20261003-043254`, one layer up: the answer's contract, not the round's.

What is pinned here:

* the rule, over every measured shape and over the readable controls, with the
  part it could not read named;
* the same shapes through `chat()`, which is where a caller meets them — the
  error is named, the raw sentence is gone, and the retry budget the
  unparseable-body branch already spends is spent here too;
* the caller-level harm the content check exists for: the compact path, which
  used to store a non-string as the session's summary;
* the classification: this sentence must not be read as an overlong request or a
  content refusal, because both of those *act* on the error.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from emrg.config import LlmConfig
from emrg.server import llm as llm_mod
from emrg.server.llm import (
    MAX_RETRIES,
    OTHER_ERROR,
    LlmClient,
    SelfExplainingLlmError,
    classify_llm_error,
    completion_shape_problem,
)

#: Every measured shape, with the part of the answer it breaks and the shape word
#: the sentence must use.
MALFORMED = [
    ([], "the response body is not an object", "list"),
    ("x", "the response body is not an object", "str"),
    (5, "the response body is not an object", "int"),
    ({"id": "1"}, "carries no `choices`", ""),
    ({"choices": None}, "`choices` is not a list", "null"),
    ({"choices": {"message": {"content": "ok"}}}, "`choices` is not a list", "dict"),
    ({"choices": []}, "`choices` is empty", ""),
    ({"choices": [None]}, "`choices[0]` is not an object", "null"),
    ({"choices": ["x"]}, "`choices[0]` is not an object", "str"),
    ({"choices": [{"message": None}]}, "`choices[0].message` is not an object", "null"),
    ({"choices": [{"message": "ok"}]}, "`choices[0].message` is not an object", "str"),
    ({"choices": [{"message": {"content": ["a"]}}]},
     "`choices[0].message.content` is not a string", "list"),
    ({"choices": [{"message": {"content": 5}}]},
     "`choices[0].message.content` is not a string", "int"),
]

#: Readable bodies — including the ones that are odd but that this rule must not
#: answer for, because another branch already owns the sentence for them.
READABLE = [
    {"choices": [{"message": {"content": "ok"}}]},
    {"choices": [{"message": {"content": None}}]},
    {"choices": [{"message": {"content": "ok", "tool_calls": []}}]},
    # A choice with no `message` key: the read falls back to `{}` and the
    # existing "returned no answer at all" refusal names finish_reason for it.
    {"choices": [{"finish_reason": "stop"}]},
    # A message with no `content` key: same — the empty-answer branch reads it.
    {"choices": [{"message": {"tool_calls": [{"id": "c1"}]}}]},
    # Extra fields are not this rule's business.
    {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 3}},
]


# ── the rule ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("body,part,shape", MALFORMED)
def test_every_measured_shape_is_refused_by_name(body, part, shape):
    problem = completion_shape_problem(body)
    assert problem is not None, f"{body!r} was accepted"
    assert part in problem, problem
    if shape:
        assert f"got {shape}" in problem, problem


@pytest.mark.parametrize("body", READABLE)
def test_a_readable_body_is_not_refused(body):
    assert completion_shape_problem(body) is None, body


def test_the_rule_does_not_answer_for_the_empty_answer_branch():
    """The boundary that keeps one refusal from becoming two.

    A choice with no `message`, or a message with no `content`, is the *existing*
    branch's business — it names `finish_reason` and says an empty string is not
    a summary. A shape complaint here would replace a sentence that says what
    happened with one about a key that is simply absent.
    """
    assert completion_shape_problem({"choices": [{"finish_reason": "length"}]}) is None
    assert completion_shape_problem({"choices": [{"message": {}}]}) is None


# ── through chat(), which is where a caller meets them ──────────────────────


class _Resp:
    def __init__(self, body: bytes):
        self.status_code = 200
        self.content = body
        self.headers = {}
        self.text = body.decode("utf-8", "replace")


class _Transport:
    def __init__(self, body: bytes):
        self.body = body
        self.calls = 0

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        return _Resp(self.body)


def _chat(body, monkeypatch):
    """Run one real `chat()` over a stub transport; return (message, transport)."""
    async def fast_sleep(_delay):
        pass

    monkeypatch.setattr(llm_mod.asyncio, "sleep", fast_sleep)
    client = LlmClient(LlmConfig(base_url="http://localhost", api_key="test"))
    transport = _Transport(json.dumps(body).encode("utf-8"))
    client._client = transport
    return asyncio.run(client.chat([{"role": "user", "content": "hi"}])), transport


@pytest.mark.parametrize("body,part,shape", MALFORMED)
def test_the_named_sentence_reaches_the_caller(body, part, shape, monkeypatch):
    with pytest.raises(SelfExplainingLlmError) as excinfo:
        _chat(body, monkeypatch)
    message = str(excinfo.value)
    assert part in message, message
    assert "is not a completion" in message, message


@pytest.mark.parametrize("body,part,shape", MALFORMED)
def test_the_raw_python_sentence_never_reaches_the_caller(body, part, shape, monkeypatch):
    """The whole defect, as one assertion: the caller is not shown Python."""
    with pytest.raises(SelfExplainingLlmError) as excinfo:
        _chat(body, monkeypatch)
    message = str(excinfo.value)
    for raw in ("subscriptable", "index out of range", "has no attribute", "KeyError"):
        assert raw not in message, f"{raw!r} is the old raw sentence: {message}"


def test_the_shape_fault_spends_the_whole_retry_budget(monkeypatch):
    """The treatment, not just the sentence.

    The branch three lines above already treats "the answer did not arrive in the
    declared form" as transient and re-asks; a proxy that answers 200 with its own
    envelope is the same artefact one step later, so it gets the same budget. An
    arm that only asserted the message would pass on a version that gave up on
    the first bad answer.
    """
    async def fast_sleep(_delay):
        pass

    monkeypatch.setattr(llm_mod.asyncio, "sleep", fast_sleep)
    client = LlmClient(LlmConfig(base_url="http://localhost", api_key="test"))
    transport = _Transport(json.dumps({"choices": []}).encode("utf-8"))
    client._client = transport
    with pytest.raises(SelfExplainingLlmError):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert transport.calls == MAX_RETRIES + 1, (
        f"the shape fault was tried {transport.calls} time(s); the unparseable "
        f"branch tries {MAX_RETRIES + 1}"
    )


def test_a_completion_still_comes_back(monkeypatch):
    """The control direction: a well-formed answer is returned untouched."""
    message, transport = _chat(
        {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        monkeypatch,
    )
    assert message == {"content": "ok"}, message
    assert transport.calls == 1, "a good answer was re-sent"


def test_a_retry_that_succeeds_is_not_reported(monkeypatch):
    """The reason the treatment is a retry at all: one bad answer, then a good.

    A gateway that hiccups once must not cost the caller this request — the
    reflection loop, the compactor and the title path all call `chat()`.
    """
    async def fast_sleep(_delay):
        pass

    monkeypatch.setattr(llm_mod.asyncio, "sleep", fast_sleep)

    class _Flaky:
        def __init__(self):
            self.calls = 0

        async def post(self, url, headers=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _Resp(b'{"choices": []}')
            return _Resp(json.dumps(
                {"choices": [{"message": {"content": "recovered"}}]}
            ).encode("utf-8"))

    client = LlmClient(LlmConfig(base_url="http://localhost", api_key="test"))
    flaky = _Flaky()
    client._client = flaky
    message = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert message == {"content": "recovered"}, message
    assert flaky.calls == 2, flaky.calls


# ── the classification, because both other classes *act* on the error ───────


@pytest.mark.parametrize("body,part,shape", MALFORMED)
def test_the_refusal_is_not_mistaken_for_an_overlong_request(body, part, shape, monkeypatch):
    """A wrong classification is a wrong action.

    `_compact_with_fallback` hands an overlong failure to the chunked compactor,
    which re-sends the same request; `_probe_content_risk` reads a content refusal
    as its answer. A sentence that matched either vocabulary would have the daemon
    re-send a request whose *answer* the provider cannot shape — the fault is not
    in this session and no retry changes it.
    """
    with pytest.raises(SelfExplainingLlmError) as excinfo:
        _chat(body, monkeypatch)
    assert classify_llm_error(excinfo.value) == OTHER_ERROR, str(excinfo.value)


# ── the caller-level harm the content check exists for ─────────────────────


def test_a_non_string_content_is_not_stored_as_a_summary(tmp_path, monkeypatch):
    """The one shape that does not raise, and the reason it must.

    Measured before the change: `_do_compact` read
    `msg.get("content", "Summary unavailable.")` and returned a **list**, which
    went into the session's summary record and out to the client as the summary —
    a compact that silently replaced a session's context with a payload no reader
    can render. The title path is the same shape one caller over: it calls
    `.strip()` on the value.
    """
    from emrg.server.daemon import EmrgServer
    from emrg.session import Session

    async def fast_sleep(_delay):
        pass

    monkeypatch.setattr(llm_mod.asyncio, "sleep", fast_sleep)

    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = tmp_path / "projects.yml"
    session = Session.create_with_id("compact-shape", tmp_path)
    transport = _Transport(json.dumps(
        {"choices": [{"message": {"content": ["a", "b"]}}]}
    ).encode("utf-8"))
    server.llm._client = transport

    records = [{"type": "message", "role": "user", "content": "hello"}]
    with pytest.raises(SelfExplainingLlmError) as excinfo:
        asyncio.run(server._do_compact(session, records))
    assert "`choices[0].message.content` is not a string" in str(excinfo.value)


def test_a_string_content_is_still_summarized(tmp_path, monkeypatch):
    """The control direction at the same caller: a real summary still lands."""
    from emrg.server.daemon import EmrgServer
    from emrg.session import Session

    async def fast_sleep(_delay):
        pass

    monkeypatch.setattr(llm_mod.asyncio, "sleep", fast_sleep)

    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = tmp_path / "projects.yml"
    session = Session.create_with_id("compact-ok", tmp_path)
    server.llm._client = _Transport(json.dumps(
        {"choices": [{"message": {"content": "a handoff summary"}}]}
    ).encode("utf-8"))

    records = [{"type": "message", "role": "user", "content": "hello"}]
    summary = asyncio.run(server._do_compact(session, records))
    assert summary == "a handoff summary", summary
