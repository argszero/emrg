"""Unit tests for LlmClient — payload construction and headers.

These test the pure methods (_make_payload, _headers) that don't
require network access or asyncio event loops.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.server.llm import (
    CONTENT_FILTER_ERROR,
    CONTENT_FILTER_FINISH,
    CONTENT_FILTER_RETRY_INSTRUCTION,
    CONTENT_RISK,
    CONTENT_RISK_ERROR,
    CONTENT_RISK_HINT,
    CONTENT_RISK_LADDER,
    CONTENT_RISK_RUNG_DESCRIPTION,
    CONTEXT_TOO_LONG,
    OTHER_ERROR,
    LlmClient,
    classify_llm_error,
    content_filter_retry_messages,
    content_risk_retry,
    escape_astral_messages,
    escape_astral_text,
    is_overlong_error,
    space_out_messages,
    space_out_text,
)


@pytest.fixture
def cfg() -> LlmConfig:
    """A config with known non-default values for deterministic testing."""
    return LlmConfig(
        base_url="https://api.example.com/v1",
        api_key="sk-test-key",
        model="test-model",
        max_tokens=2048,
        temperature=0.3,
    )


@pytest.fixture
def client(cfg: LlmConfig) -> LlmClient:
    return LlmClient(cfg)


# ── _headers ─────────────────────────────────────────────────────


def test_headers_bearer_token(client):
    """Headers include the Bearer token from config."""
    h = client._headers()
    assert h["Authorization"] == "Bearer sk-test-key"


def test_headers_content_type(client):
    """Headers include Content-Type: application/json."""
    h = client._headers()
    assert h["Content-Type"] == "application/json"


def test_headers_user_agent(client):
    """Headers include a User-Agent string."""
    h = client._headers()
    assert "emrg" in h["User-Agent"].lower()


# ── _make_payload ────────────────────────────────────────────────


def test_payload_basic(client):
    """Basic payload has model, messages, max_tokens, temperature."""
    p = client._make_payload([{"role": "user", "content": "hello"}])
    assert p["model"] == "test-model"
    assert p["messages"] == [{"role": "user", "content": "hello"}]
    assert p["max_tokens"] == 2048
    assert p["temperature"] == 0.3
    assert "tools" not in p
    assert "stream" not in p


def test_payload_with_tools(client):
    """When tools are provided, they're included in the payload."""
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
    p = client._make_payload(
        [{"role": "user", "content": "run tests"}], tools=tools
    )
    assert p["tools"] == tools
    assert len(p["tools"]) == 1


def test_payload_without_tools(client):
    """When tools=None or omitted, no 'tools' key in payload."""
    p = client._make_payload([{"role": "user", "content": "hi"}], tools=None)
    assert "tools" not in p

    p2 = client._make_payload([{"role": "user", "content": "hi"}])
    assert "tools" not in p2


def test_payload_stream_mode(client):
    """Stream mode adds stream=True and stream_options."""
    p = client._make_payload(
        [{"role": "user", "content": "hi"}], stream=True
    )
    assert p["stream"] is True
    assert "stream_options" in p
    assert p["stream_options"] == {"include_usage": False}


def test_payload_non_stream_mode(client):
    """Non-stream mode (default) has no stream-related keys."""
    p = client._make_payload([{"role": "user", "content": "hi"}])
    assert "stream" not in p
    assert "stream_options" not in p


def test_payload_stream_with_tools(client):
    """Stream + tools — both are included."""
    tools = [{"type": "function", "function": {"name": "grep", "parameters": {}}}]
    p = client._make_payload(
        [{"role": "user", "content": "search"}], tools=tools, stream=True
    )
    assert p["stream"] is True
    assert p["stream_options"] == {"include_usage": False}
    assert p["tools"] == tools


def test_payload_empty_tools_list(client):
    """Empty tools list is falsy — should not add 'tools' key."""
    p = client._make_payload(
        [{"role": "user", "content": "x"}], tools=[]
    )
    assert "tools" not in p


def test_payload_preserves_messages_identity(client):
    """Messages list reference is preserved (no defensive copy — intentional)."""
    msgs = [{"role": "system", "content": "you are helpful"}]
    p = client._make_payload(msgs)
    assert p["messages"] is msgs


def test_payload_max_tokens_default():
    """Default max_tokens from LlmConfig is 8192."""
    default_cfg = LlmConfig()
    c = LlmClient(default_cfg)
    p = c._make_payload([{"role": "user", "content": "x"}])
    assert p["max_tokens"] == 8192


def test_payload_temperature_default():
    """Default temperature is 0.7."""
    default_cfg = LlmConfig()
    c = LlmClient(default_cfg)
    p = c._make_payload([{"role": "user", "content": "x"}])
    assert p["temperature"] == 0.7


# ── LLM 错误信息脱敏（20260807-0107）──────────────────────────


def test_redact_headers_masks_sensitive():
    """response headers 敏感键（set-cookie/authorization/token）被遮蔽。"""
    from emrg.server.llm import _redact_headers
    h = {"set-cookie": "session=abc; HttpOnly", "content-type": "application/json",
         "x-request-id": "req-123", "x-api-key": "sk-A1b2C3d4A1b2C3d4A1b2C3d4A1b2C3d4"}
    r = _redact_headers(h)
    assert r["set-cookie"] == "***"
    assert r["x-api-key"] == "***"
    assert r["content-type"] == "application/json"
    assert r["x-request-id"] == "req-123"


def test_redact_headers_masks_inline_secret_in_values():
    """非敏感键但值内联密钥也被遮蔽（如 server 回显 x-error: invalid sk-...）。"""
    from emrg.server.llm import _redact_headers
    r = _redact_headers({"x-error": "invalid key sk-A1b2C3d4A1b2C3d4A1b2C3d4A1b2C3d4"})
    assert "sk-" not in r["x-error"]
    assert "invalid key ***" in r["x-error"]


def test_redact_text_masks_inline_credentials():
    """LLM 错误 body 内联凭据被遮蔽，普通文本保留。"""
    from emrg.server.llm import _redact_text
    assert "sk-" not in _redact_text("bad key sk-A1b2C3d4A1b2C3d4A1b2C3d4A1b2C3d4 supplied")
    assert "ghp_" not in _redact_text("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 rejected")
    assert _redact_text("rate limit exceeded, try later") == "rate limit exceeded, try later"


# ── gzip body 容错（20260807-1240：memory reflection UnicodeDecodeError）──


def test_parse_json_body_plain():
    """Plain JSON body parses unchanged."""
    from emrg.server.llm import _parse_json_body
    data = _parse_json_body(b'{"choices": []}')
    assert data == {"choices": []}


def test_parse_json_body_gzip_without_content_encoding():
    """Gzip body (no Content-Encoding header → httpx won't decompress) is
    transparently decompressed via magic-byte detection."""
    import gzip as gz
    from emrg.server.llm import _parse_json_body
    raw = '{"choices": [{"message": {"content": "hi"}}]}'.encode()
    data = _parse_json_body(gz.compress(raw))
    assert data["choices"][0]["message"]["content"] == "hi"


def test_parse_json_body_corrupt_gzip_raises():
    """Gzip magic bytes with corrupt payload raise OSError (BadGzipFile)."""
    import gzip as gz
    import pytest
    from emrg.server.llm import _parse_json_body
    with pytest.raises(OSError):
        _parse_json_body(b"\x1f\x8bCORRUPTED-NOT-REAL-GZIP")


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes, headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = content.decode("utf-8", "replace")


class _FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        return self.responses.pop(0)


def _patch_fast_sleep(monkeypatch):
    """Make retry backoff instant in tests."""
    import emrg.server.llm as llm_mod

    async def fast_sleep(_delay):
        pass

    monkeypatch.setattr(llm_mod.asyncio, "sleep", fast_sleep)


def test_chat_gzip_body_transparent_decompress(monkeypatch, client):
    """chat() returns the message when the 200 body is gzip-compressed
    without Content-Encoding (the production failure mode)."""
    import asyncio
    import gzip as gz
    body = gz.compress(b'{"choices": [{"message": {"content": "ok"}}]}')
    fake = _FakeHttpClient([_FakeResponse(200, body)])
    client._client = fake
    msg = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert msg == {"content": "ok"}
    assert fake.calls == 1  # no retry needed


def test_chat_malformed_body_retries_then_succeeds(monkeypatch, client):
    """Unparseable 200 body retries with backoff instead of crashing
    (previously: UnicodeDecodeError killed memory reflection outright)."""
    import asyncio
    _patch_fast_sleep(monkeypatch)
    good = b'{"choices": [{"message": {"content": "recovered"}}]}'
    fake = _FakeHttpClient([
        _FakeResponse(200, b"\x1f\x8bCORRUPT"),
        _FakeResponse(200, good),
    ])
    client._client = fake
    msg = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert msg == {"content": "recovered"}
    assert fake.calls == 2


def test_chat_malformed_body_exhausts_retries(monkeypatch, client):
    """Persistently malformed body raises RuntimeError after MAX_RETRIES."""
    import asyncio
    import pytest
    _patch_fast_sleep(monkeypatch)
    fake = _FakeHttpClient([_FakeResponse(200, b"\x1f\x8bBAD")] * 4)
    client._client = fake
    with pytest.raises(RuntimeError, match="unparseable"):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))


class _FakeStreamResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return b"stream body"


class _FakeStreamClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def stream(self, method, url, headers=None, json=None):
        self.calls += 1
        return self.responses.pop(0)


def _drain_stream(client, fake) -> list[str]:
    """Run one chat_stream call to exhaustion, returning accumulated chunks."""
    import asyncio
    parts: list[str] = []

    async def _run():
        async for chunk in client.chat_stream([{"role": "user", "content": "hi"}]):
            if chunk.get("content"):
                parts.append(chunk["content"])

    asyncio.run(_run())
    return parts


def test_first_attempt_silent_no_retry(monkeypatch, client, caplog):
    """First-attempt (normal) requests log NO attempt line (rant
    2026-08-17T14:27:39) — 1/4 on every request was noise."""
    import logging
    import asyncio
    caplog.set_level(logging.DEBUG, logger="emrg.server.llm")
    body = b'{"choices": [{"message": {"content": "ok"}}]}'
    fake = _FakeHttpClient([_FakeResponse(200, body)])
    client._client = fake
    asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert fake.calls == 1
    # no attempt counter for the first attempt
    assert "attempt 1/4" not in caplog.text
    assert "LLM request: url=" not in caplog.text
    assert "LLM stream attempt" not in caplog.text


def test_retry_logs_attempt_counter(monkeypatch, client, caplog):
    """Retries DO log the attempt counter (attempt 2/4+) alongside the
    existing transient-error warning — the retry path stays traceable."""
    import logging
    import asyncio
    _patch_fast_sleep(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="emrg.server.llm")
    good = b'{"choices": [{"message": {"content": "recovered"}}]}'
    fake = _FakeHttpClient([
        _FakeResponse(500, b"boom"),
        _FakeResponse(200, good),
    ])
    client._client = fake
    asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert fake.calls == 2
    # first attempt silent; retry logs the attempt counter + transient warning
    assert "attempt 1/4" not in caplog.text
    assert "LLM transient error 500" in caplog.text
    assert "attempt 2/4" in caplog.text


def test_stream_first_attempt_silent(monkeypatch, client, caplog):
    """chat_stream's first attempt is also silent (rant 2026-08-17T14:27:39)."""
    import logging
    _patch_fast_sleep(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="emrg.server.llm")

    class _GoodStream:
        status_code = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def aiter_lines(self):
            import json
            for obj in (
                {"choices": [{"delta": {"content": "hi"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ):
                yield "data: " + json.dumps(obj)

    fake = _FakeStreamClient([_GoodStream()])
    client._client = fake
    parts = _drain_stream(client, fake)
    assert parts == ["hi"]
    assert fake.calls == 1
    assert "LLM stream attempt" not in caplog.text


def test_stream_retry_logs_attempt_counter(monkeypatch, client, caplog):
    """chat_stream retry logs the attempt counter (attempt 2/4)."""
    import logging
    _patch_fast_sleep(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="emrg.server.llm")

    class _GoodStream:
        status_code = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def aiter_lines(self):
            import json
            for obj in (
                {"choices": [{"delta": {"content": "hi"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ):
                yield "data: " + json.dumps(obj)

    fake = _FakeStreamClient([
        _FakeStreamResponse(500),  # first attempt → retry
        _GoodStream(),             # second attempt → success
    ])
    client._client = fake
    parts = _drain_stream(client, fake)
    assert parts == ["hi"]
    assert fake.calls == 2
    assert "LLM stream attempt 2/4" in caplog.text
    assert "attempt 1/4" not in caplog.text
    assert "LLM stream transient error 500" in caplog.text


# ── Streaming transport-error retry (rant 2026-08-31T12:53:13) ──

class _TransportErrorStream:
    """A 200 stream whose aiter_lines raises the given transport error,
    optionally after yielding some SSE chunks first."""

    def __init__(self, exc, chunks_before_raise=()):
        self.status_code = 200
        self.headers = {}
        self._exc = exc
        self._chunks = list(chunks_before_raise)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        import json
        for obj in self._chunks:
            yield "data: " + json.dumps(obj)
        raise self._exc


def test_stream_readtimeout_retries_before_any_delta(monkeypatch, client, caplog):
    """Transport error mid-stream (httpx.ReadTimeout — no data block within the
    120s read timeout) is retried with backoff while no delta has been yielded
    yet (rant 2026-08-31T12:53:13 — previously the exception bubbled out of
    aiter_lines, skipped the retry loop, and killed the whole tool round)."""
    import logging
    import httpx
    _patch_fast_sleep(monkeypatch)
    caplog.set_level(logging.WARNING, logger="emrg.server.llm")

    fake = _FakeStreamClient([
        _TransportErrorStream(httpx.ReadTimeout("timed out")),
        _make_stream(
            {"choices": [{"delta": {"content": "hi"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ),
    ])
    client._client = fake
    parts = _drain_stream(client, fake)
    assert parts == ["hi"]
    assert fake.calls == 2
    assert "LLM stream ReadTimeout, retrying in" in caplog.text


def test_stream_transport_error_after_delta_not_retried(monkeypatch, client, caplog):
    """Once a delta has been yielded (the caller has broadcast it), a transport
    error is NOT retried — retrying would duplicate already-streamed content."""
    import logging
    import httpx
    import pytest
    _patch_fast_sleep(monkeypatch)
    caplog.set_level(logging.WARNING, logger="emrg.server.llm")

    fake = _FakeStreamClient([
        _TransportErrorStream(
            httpx.ReadTimeout("timed out"),
            chunks_before_raise=[{"choices": [{"delta": {"content": "partial"}}]}],
        ),
    ])
    client._client = fake
    with pytest.raises(httpx.ReadTimeout):
        _drain_stream(client, fake)
    assert fake.calls == 1
    assert "retrying in" not in caplog.text


def test_stream_transport_error_exhausts_retries(monkeypatch, client):
    """Persistent transport errors raise after MAX_RETRIES attempts."""
    import httpx
    import pytest
    _patch_fast_sleep(monkeypatch)
    fake = _FakeStreamClient([
        _TransportErrorStream(httpx.ReadTimeout("t1")),
        _TransportErrorStream(httpx.ReadTimeout("t2")),
        _TransportErrorStream(httpx.ReadTimeout("t3")),
        _TransportErrorStream(httpx.ReadTimeout("t4")),
    ])
    client._client = fake
    with pytest.raises(httpx.ReadTimeout):
        _drain_stream(client, fake)
    assert fake.calls == 4


# ── Reasoning / think-block capture (rant 2026-08-18T09:43:23) ──

def _make_stream(*objs):
    """Build a one-shot 200 stream yielding the given SSE chunk objects."""

    class _GoodStream:
        status_code = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def aiter_lines(self):
            import json
            for obj in objs:
                yield "data: " + json.dumps(obj)

    return _GoodStream()


def _collect_chunks(client):
    import asyncio
    chunks = []

    async def _run():
        async for chunk in client.chat_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    asyncio.run(_run())
    return chunks


def test_stream_accumulates_reasoning_content(monkeypatch, client):
    """DeepSeek-style `reasoning_content` deltas accumulate into the yielded
    `reasoning` field; the final chunk carries the full think text."""
    _patch_fast_sleep(monkeypatch)
    fake = _FakeStreamClient([_make_stream(
        {"choices": [{"delta": {"reasoning_content": "Let me "}}]},
        {"choices": [{"delta": {"reasoning_content": "think step by step"}}]},
        {"choices": [{"delta": {"content": "final answer"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )])
    client._client = fake
    chunks = _collect_chunks(client)
    last = chunks[-1]
    assert last["reasoning"] == "Let me think step by step"
    # content is unaffected
    assert "".join(c.get("content") or "" for c in chunks) == "final answer"
    assert fake.calls == 1


def test_stream_accumulates_openai_reasoning(monkeypatch, client):
    """OpenAI-style `reasoning` field name is also accepted."""
    _patch_fast_sleep(monkeypatch)
    fake = _FakeStreamClient([_make_stream(
        {"choices": [{"delta": {"reasoning": "think 1"}}]},
        {"choices": [{"delta": {"reasoning": " think 2"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )])
    client._client = fake
    chunks = _collect_chunks(client)
    assert chunks[-1]["reasoning"] == "think 1 think 2"


def test_stream_no_reasoning_means_none(monkeypatch, client):
    """A model that does not reason → `reasoning` stays None (regression-safe:
    no think block, no field pollution in llm.jsonl)."""
    _patch_fast_sleep(monkeypatch)
    fake = _FakeStreamClient([_make_stream(
        {"choices": [{"delta": {"content": "plain"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )])
    client._client = fake
    chunks = _collect_chunks(client)
    assert all(c.get("reasoning") is None for c in chunks)


def test_stream_usage_reasoning_tokens_top_level_and_nested(monkeypatch, client):
    """usage.reasoning_tokens is captured from the top level AND from the
    completion_tokens_details nesting (two provider conventions)."""
    _patch_fast_sleep(monkeypatch)

    # top-level reasoning_tokens
    fake = _FakeStreamClient([_make_stream(
        {"choices": [{"delta": {"reasoning_content": "x"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 20,
                   "reasoning_tokens": 7}},
    )])
    client._client = fake
    chunks = _collect_chunks(client)
    assert chunks[-1]["usage"]["reasoning_tokens"] == 7
    assert chunks[-1]["usage"]["prompt_tokens"] == 10

    # nested under completion_tokens_details
    fake2 = _FakeStreamClient([_make_stream(
        {"choices": [{"delta": {"reasoning_content": "x"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                   "completion_tokens_details": {"reasoning_tokens": 9}}},
    )])
    client._client = fake2
    chunks2 = _collect_chunks(client)
    assert chunks2[-1]["usage"]["reasoning_tokens"] == 9

    # no usage → None (unchanged behavior)
    fake3 = _FakeStreamClient([_make_stream(
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )])
    client._client = fake3
    chunks3 = _collect_chunks(client)
    assert chunks3[-1]["usage"] is None


def test_stream_usage_cache_hit_tokens_top_level_and_nested(monkeypatch, client):
    """usage.cache_hit_tokens is captured from the top level (DeepSeek native
    prompt_cache_hit_tokens) AND from the prompt_tokens_details nesting
    (OpenAI-compatible cached_tokens). Non-cache endpoints keep the exact
    same record shape (rant 2026-08-23T09:17:14)."""
    _patch_fast_sleep(monkeypatch)

    # top-level prompt_cache_hit_tokens (DeepSeek native)
    fake = _FakeStreamClient([_make_stream(
        {"choices": [{"delta": {"content": "x"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                   "prompt_cache_hit_tokens": 70}},
    )])
    client._client = fake
    chunks = _collect_chunks(client)
    assert chunks[-1]["usage"]["cache_hit_tokens"] == 70
    assert chunks[-1]["usage"]["prompt_tokens"] == 100  # unchanged

    # nested under prompt_tokens_details.cached_tokens (OpenAI compatible)
    fake2 = _FakeStreamClient([_make_stream(
        {"choices": [{"delta": {"content": "x"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 50, "completion_tokens": 5,
                   "prompt_tokens_details": {"cached_tokens": 30}}},
    )])
    client._client = fake2
    chunks2 = _collect_chunks(client)
    assert chunks2[-1]["usage"]["cache_hit_tokens"] == 30

    # no cache fields reported → record shape unchanged (no new field)
    fake3 = _FakeStreamClient([_make_stream(
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    )])
    client._client = fake3
    chunks3 = _collect_chunks(client)
    assert "cache_hit_tokens" not in chunks3[-1]["usage"]
    assert chunks3[-1]["usage"]["prompt_tokens"] == 10


# ── classify_llm_error ───────────────────────────────────────────
#
# The rule the compact paths act on: only a request the provider called too
# long may be re-sent through the chunker. Everything here exists because a
# blanket `"400" in str(e)` fed both refusals and overflows into the same
# branch (rants 2026-09-17T17:55:42 and 2026-09-17T18:19:45).


def _llm_error(status: int, body: str) -> RuntimeError:
    """An error shaped exactly like the ones chat()/chat_stream() raise."""
    return RuntimeError(f"LLM request failed: {status} headers={{'x': 'y'}} body={body}")


def test_classify_content_risk_beats_the_bare_400_fallback():
    """A content-filter refusal is a 400 — and must still NOT be read as
    'too long'. This is the precedence that ends the self-lock."""
    err = _llm_error(
        400,
        '{"error":{"message":"Content Exists Risk","type":"invalid_request_error",'
        '"code":"invalid_request_error"}}',
    )
    assert classify_llm_error(err) == CONTENT_RISK


def test_classify_413_body_buffer_overflow_is_context_too_long():
    """413 'Failed to buffer the request body: length limit exceeded' is a
    length problem — the chunker must be reachable for it (an unrecognised
    overflow leaves a session growing forever, rant 2026-09-17T18:19:45)."""
    err = _llm_error(413, "Failed to buffer the request body: length limit exceeded")
    assert classify_llm_error(err) == CONTEXT_TOO_LONG


@pytest.mark.parametrize("body", [
    "This model's maximum context length is 65536 tokens",
    "context_length_exceeded",
    "the prompt is too long for this model",
    "Please reduce the length of the messages",
    "maximum context window reached",
])
def test_classify_overlong_wording(body):
    """Every provider spelling of 'too long' that the chunker cares about."""
    assert classify_llm_error(_llm_error(400, body)) == CONTEXT_TOO_LONG


def test_classify_opaque_400_still_means_overlong():
    """A 400 with nothing to go on keeps the old fallback — the refusal class
    is now checked first, so only genuine unknowns reach it."""
    assert classify_llm_error(_llm_error(400, "bad request")) == CONTEXT_TOO_LONG


def test_classify_overlong_wording_beyond_400_and_413():
    """The wording itself is a signal, not just the status: gateways pass a
    context overflow through as other statuses too."""
    assert classify_llm_error(
        _llm_error(422, "This model's maximum context length is 65536 tokens")
    ) == CONTEXT_TOO_LONG


def test_classify_unrelated_error_is_other():
    """A non-400/413 failure is neither: it must not be chunked."""
    assert classify_llm_error(_llm_error(500, "internal server error")) == OTHER_ERROR
    assert classify_llm_error(RuntimeError("connection reset")) == OTHER_ERROR


# ── is_overlong_error: one word list, asked in one place (issue #1336) ──
#
# The chunker's two branches used to test
# `"context length" in err or "length limit" in err` themselves, so a spelling
# added to the classifier never reached them: the same 413 body overflow was a
# length problem at the compact gate and an opaque failure two frames deeper.
# These tests pin the delegation and mechanise the rule it replaces.

_OVERLONG_SPELLINGS = (
    "context length",
    "context window",
    "prompt is too long",
    "too long",
    "length limit",
    "length exceeded",
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _respelled_overlong_sites(root: Path) -> list[tuple[str, str]]:
    """`(file, spelling)` for every `<spelling>` member-test outside llm.py.

    A local instrument rather than a grep so it can be pointed at a tree this
    test builds — see the spoofed-tree control below.
    """
    found: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "llm.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for spelling in _OVERLONG_SPELLINGS:
            for quote in ('"', "'"):
                if f"{quote}{spelling}{quote} in" in text:
                    found.append((path.name, spelling))
    return found


def test_is_overlong_error_is_the_classifier_not_a_second_answer():
    """The predicate agrees with the classifier on all three classes, including
    the one that must never be split: a refusal is not a length problem."""
    assert is_overlong_error(
        _llm_error(413, "Failed to buffer the request body: length limit exceeded")
    )
    assert is_overlong_error(_llm_error(400, "the prompt is too long for this model"))
    assert is_overlong_error(_llm_error(400, "bad request"))  # the bare-400 fallback
    assert not is_overlong_error(
        _llm_error(
            400,
            '{"error":{"message":"Content Exists Risk","type":"invalid_request_error"}}',
        )
    )
    assert not is_overlong_error(_llm_error(500, "internal server error"))


def test_no_module_respells_the_overlong_markers():
    """The rule is mechanised: the marker list lives in `llm.py` and nowhere
    else, so a spelling cannot be added to one copy and missed by the other."""
    assert _respelled_overlong_sites(REPO_ROOT / "emrg") == []


def test_the_respelling_scan_is_not_blind(tmp_path):
    """Positive control: the scan finds a respelling when one exists, and still
    exempts `llm.py` (the authority, whose own list is the thing being kept
    single). Without this, a zero-hit reading would be indistinguishable from a
    broken instrument."""
    (tmp_path / "llm.py").write_text('if "context length" in err:\n    pass\n', encoding="utf-8")
    (tmp_path / "somewhere_else.py").write_text(
        'if "length limit" in err:\n    raise\n', encoding="utf-8"
    )
    assert _respelled_overlong_sites(tmp_path) == [("somewhere_else.py", "length limit")]


# ── escape_astral_messages ───────────────────────────────────────


def test_escape_astral_text_writes_astral_codepoints_as_notation():
    """The one transform a filter that strips `isspace()` can actually see.

    Measured (rant 2026-09-28T15:57:31): the provider normalises away every
    character `str.isspace()` accepts *before* matching, so a spaced payload is
    the same bytes to it. Asserted on the result — the notation the filter is
    handed — not on the shape of the loop that produced it (L4b).
    """
    assert escape_astral_text("abc") == "abc"          # identity below the plane
    assert escape_astral_text("") == ""
    assert escape_astral_text("\U0001F1E6") == "<U+1F1E6>"
    # Mixed text keeps its non-astral characters byte-for-byte, so a session
    # whose only astral content is one emoji pays +0.27% and loses nothing else.
    assert escape_astral_text("a\U0001F1E6b") == "a<U+1F1E6>b"
    # A BMP private-use character is *not* astral: ord() is the whole test.
    assert escape_astral_text("\uE000") == "\uE000"


def test_escape_astral_messages_shares_the_traversal_and_the_pairing_rule():
    """Same scope as the spacing rung, because it is the same traversal.

    Every role's ``content`` and every multimodal ``text`` part is transformed;
    identifiers and ``tool_calls`` are copied untouched (``function.arguments``
    is JSON, and an assistant message must stay paired with its tool result);
    and the caller's messages are never mutated. Running this on the escaping
    rung is the point: a rung that rewrote a tool call would corrupt the pair
    it is trying to save.
    """
    messages = [
        {"role": "system", "content": "s\U0001F1E6"},
        {"role": "user", "content": [
            {"type": "text", "text": "\U0001F1E7b"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ]},
        {"role": "assistant", "content": "ok", "tool_calls": [{
            "id": "call_\U0001F1E8",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"cmd": "echo \U0001F1E9"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_\U0001F1E8", "content": "out"},
    ]
    before = copy.deepcopy(messages)
    out = escape_astral_messages(messages)

    assert out[0]["content"] == "s<U+1F1E6>"
    assert out[1]["content"][0]["text"] == "<U+1F1E7>b"
    assert out[1]["content"][1] == messages[1]["content"][1]
    assert out[2]["content"] == "ok"
    assert out[2]["tool_calls"] == messages[2]["tool_calls"]
    assert out[2]["tool_calls"][0]["id"] == "call_\U0001F1E8"
    assert out[3]["tool_call_id"] == "call_\U0001F1E8"
    assert messages == before, "the transform must be a copy"


# ── space_out_messages ───────────────────────────────────────────


def test_space_out_text_spaces_every_pair():
    assert space_out_text("abc") == "a b c"
    assert space_out_text("") == ""
    assert space_out_text("a") == "a"


def test_space_out_messages_transforms_every_role_content():
    """The poisoned fragment can sit in any message — system / user /
    assistant / tool (a tool_result body included)."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "tool_call_id": "c1", "content": "out"},
    ]
    out = space_out_messages(messages)
    assert [m["content"] for m in out] == ["s y s", "h i", "o k", "o u t"]


def test_space_out_messages_preserves_pairing_and_arguments():
    """Identifiers and tool_calls are copied untouched: an assistant message
    must stay paired with its tool result, and function.arguments is JSON —
    spacing every two characters would stop it parsing."""
    messages = [{
        "role": "assistant",
        "content": "calling",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"cmd": "ls -la"}'},
        }],
    }]
    out = space_out_messages(messages)
    assert out[0]["tool_calls"] == messages[0]["tool_calls"]
    assert out[0]["tool_calls"][0]["id"] == "call_1"
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"cmd": "ls -la"}


def test_space_out_messages_leaves_the_caller_untouched():
    """The transform is a copy — the caller's history must be unaffected."""
    messages = [{"role": "user", "content": "hi"}]
    space_out_messages(messages)
    assert messages == [{"role": "user", "content": "hi"}]


def test_space_out_messages_transforms_multimodal_text_only():
    """Multimodal parts: the text field is spaced, the image reference is not
    (rewriting a URL would break the attachment)."""
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "ab"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ],
    }]
    out = space_out_messages(messages)
    assert out[0]["content"][0]["text"] == "a b"
    assert out[0]["content"][1] == messages[0]["content"][1]


class _RecordingHttpClient:
    """Records every payload it is asked to send.

    Snapshots each payload (deep copy): a real client serialises the body at
    send time, so a recorder that kept the object it was handed would show
    later mutations instead of what was sent — which is exactly how the
    spaced retry would look like it had rewritten the first request.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    async def post(self, url, headers=None, json=None):
        self.payloads.append(copy.deepcopy(json))
        return self.responses.pop(0)


_CONTENT_RISK_BODY = (
    b'{"error":{"message":"Content Exists Risk","type":"invalid_request_error"}}'
)


class _RecordingStreamClient(_FakeStreamClient):
    """`_FakeStreamClient` that also records each payload it was asked to send."""

    def __init__(self, responses):
        super().__init__(responses)
        self.payloads: list[dict] = []

    def stream(self, method, url, headers=None, json=None):
        self.payloads.append(copy.deepcopy(json))
        return super().stream(method, url, headers=headers, json=json)


class _RefusingStream:
    """A non-200 on the streaming path, where the body is read with `aread()`."""

    def __init__(self, body: bytes):
        self.status_code = 400
        self.headers = {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return self._body


def test_stream_retries_on_the_same_ladder_as_chat(monkeypatch, client, caplog):
    """The streaming twin walks the same ladder — and it had no test before this.

    `chat()` and `chat_stream()` spell the retry twice, so the rung order is
    pinned on both paths: a fix applied to one of them is not a fix. The
    assertion is on the payload that was sent, because a branch that logged the
    right warning and re-sent the *unfixed* body would read identically in the
    log.
    """
    import asyncio
    import logging
    _patch_fast_sleep(monkeypatch)
    caplog.set_level(logging.WARNING, logger="emrg.server.llm")

    fake = _RecordingStreamClient([
        _RefusingStream(_CONTENT_RISK_BODY),
        _make_stream(
            {"choices": [{"delta": {"content": "hi"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ),
    ])
    client._client = fake

    async def _run():
        return [
            chunk["content"]
            async for chunk in client.chat_stream(
                [{"role": "user", "content": "hi \U0001F1E6"}]
            )
            if chunk.get("content")
        ]

    parts = asyncio.run(_run())
    assert parts == ["hi"]
    assert fake.calls == 2
    assert fake.payloads[0]["messages"] == [{"role": "user", "content": "hi \U0001F1E6"}]
    assert fake.payloads[1]["messages"] == [{"role": "user", "content": "hi <U+1F1E6>"}]
    assert "astral-plane codepoints written as <U+XXXX>" in caplog.text


def test_chat_retries_with_escaped_astral_codepoints_first(monkeypatch, client):
    """A refusal is answered by writing the astral codepoints out — the rung that
    recovers, measured 400 -> 200 where the spaced form stayed 400."""
    import asyncio
    good = b'{"choices": [{"message": {"content": "recovered"}}]}'
    fake = _RecordingHttpClient([
        _FakeResponse(400, _CONTENT_RISK_BODY),
        _FakeResponse(200, good),
    ])
    client._client = fake
    trigger = "\U0001F1E6"
    msg = asyncio.run(client.chat([{"role": "user", "content": f"hi {trigger}"}]))
    assert msg == {"content": "recovered"}
    assert len(fake.payloads) == 2
    assert fake.payloads[0]["messages"] == [{"role": "user", "content": f"hi {trigger}"}]
    assert fake.payloads[1]["messages"] == [{"role": "user", "content": "hi <U+1F1E6>"}]
    # The retried payload is what llm.jsonl records (it is what was sent).
    assert client.last_payload["messages"] == [{"role": "user", "content": "hi <U+1F1E6>"}]


def test_chat_spaces_out_only_after_the_escape_rung_failed(monkeypatch, client):
    """The second rung is built from the **original** payload, never from the
    escaped one.

    This is the ladder's invariant, and it is asserted on the payload that is
    actually sent: a payload that had been escaped *and then* spaced would be
    neither transform, would carry `< U + 1 F 1 E 6 >`, and would make the
    fallback's cost depend on the first rung's — which is what the requirement
    forbids ("不得叠加成「替换+插空格」同一包").

    The payload carries an astral codepoint on purpose. Measured while writing
    this: the first version of this test used an ASCII body, where
    ``space_out(original)`` and ``space_out(escape(original))`` are the *same
    string* — the mutation arm that builds rung 2 on rung 1's output survived
    it, so the assertion was about nothing.
    """
    import asyncio
    good = b'{"choices": [{"message": {"content": "recovered"}}]}'
    fake = _RecordingHttpClient([
        _FakeResponse(400, _CONTENT_RISK_BODY),
        _FakeResponse(400, _CONTENT_RISK_BODY),
        _FakeResponse(200, good),
    ])
    client._client = fake
    trigger = "\U0001F1E6"
    original = f"A {trigger} x"
    msg = asyncio.run(client.chat([{"role": "user", "content": original}]))
    assert msg == {"content": "recovered"}
    assert len(fake.payloads) == 3
    assert fake.payloads[0]["messages"] == [{"role": "user", "content": original}]
    # Rung 1: the astral codepoint written out.
    assert fake.payloads[1]["messages"] == [{"role": "user", "content": "A <U+1F1E6> x"}]
    # Rung 2: spaced from the ORIGINAL, so the trigger is still a codepoint here
    # — no notation anywhere in the body that was sent.
    assert fake.payloads[2]["messages"] == [{"role": "user", "content": f"A   {trigger}   x"}]
    assert "<" not in fake.payloads[2]["messages"][0]["content"], (
        "rung 2 was built on rung 1's output: the payload carries both transforms"
    )
    assert client.last_payload["messages"] == [{"role": "user", "content": f"A   {trigger}   x"}]


def test_chat_spends_the_escape_rung_even_when_it_is_the_identity_transform(
    monkeypatch, client
):
    """A body with no astral codepoint is still sent on rung 1, unchanged.

    Pinned because it is a choice, not an accident: the escaping rung is the
    identity transform for such a body, so that request cannot succeed where the
    first failed — and it is still spent. The ladder is defined as the retries a
    refusal is answered with, not as the retries that might help, and deciding
    "would this transform change anything?" at the call site is how the two call
    sites would drift. A refusal costs two extra requests at most.
    """
    import asyncio
    good = b'{"choices": [{"message": {"content": "recovered"}}]}'
    fake = _RecordingHttpClient([
        _FakeResponse(400, _CONTENT_RISK_BODY),
        _FakeResponse(400, _CONTENT_RISK_BODY),
        _FakeResponse(200, good),
    ])
    client._client = fake
    msg = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert msg == {"content": "recovered"}
    assert len(fake.payloads) == 3
    assert fake.payloads[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert fake.payloads[1]["messages"] == [{"role": "user", "content": "hi"}]
    assert fake.payloads[2]["messages"] == [{"role": "user", "content": "h i"}]


def test_the_host_facing_hint_names_every_rung_of_the_ladder():
    """The hint the host reads must describe the ladder the code walks.

    This is the one place the ladder is spelled twice — once as the rungs the
    retry runs, once as the sentence explaining what was tried — so it is
    checked mechanically rather than by review: rename, reorder or add a rung
    without touching the hint and this fails. Without it, the host is told a
    retry shape that no longer exists, which is where this whole defect came
    from (the old hint promised "re-sent once with the text spaced out" while
    the measurement said that retry had never once worked).
    """
    for rung in CONTENT_RISK_LADDER:
        assert CONTENT_RISK_RUNG_DESCRIPTION[rung] in CONTENT_RISK_HINT, (
            f"rung {rung!r} is in the ladder but not in the host-facing hint"
        )
    assert CONTENT_RISK_ERROR in CONTENT_RISK_HINT
    assert "start a new session" in CONTENT_RISK_HINT


def test_content_risk_retry_ladder_is_one_shot_per_rung_then_spent():
    """The ladder's own reading: two rungs, in order, then nothing.

    Asked directly, so the caller's `content_risk_stage` and the ladder cannot
    drift apart, and so a third rung added later is visible here rather than
    silently doubling what a refusal costs.
    """
    original = [{"role": "user", "content": "\U0001F1E6 x"}]
    first = content_risk_retry(0, original)
    second = content_risk_retry(1, original)
    assert first is not None and second is not None
    rung1, messages1 = first
    rung2, messages2 = second
    assert (rung1, rung2) == ("escape", "space_out")
    assert messages1 == [{"role": "user", "content": "<U+1F1E6> x"}]
    assert messages2 == [{"role": "user", "content": "\U0001F1E6   x"}]
    assert messages2 != [{"role": "user", "content": "< U + 1 F 1 E 6 >   x"}]
    assert content_risk_retry(2, original) is None, "the ladder must end"
    assert list(CONTENT_RISK_LADDER) == ["escape", "space_out"], CONTENT_RISK_LADDER


def test_chat_content_risk_after_the_whole_ladder_reports_honestly(monkeypatch, client):
    """Exactly one retry per rung: a refusal after both is raised with the
    host-facing hint, and the error still classifies as content_risk so no
    caller can hand it to the chunked compactor."""
    import asyncio
    fake = _RecordingHttpClient([
        _FakeResponse(400, _CONTENT_RISK_BODY),
        _FakeResponse(400, _CONTENT_RISK_BODY),
        _FakeResponse(400, _CONTENT_RISK_BODY),
    ])
    client._client = fake
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert len(fake.payloads) == 3  # two rungs, not a loop
    assert classify_llm_error(excinfo.value) == CONTENT_RISK
    assert CONTENT_RISK_ERROR in str(excinfo.value)
    assert "start a new session" in str(excinfo.value)


def test_chat_plain_400_is_not_spaced(monkeypatch, client):
    """Positive control: a 400 that is NOT a content refusal keeps the old
    path — one call, messages untouched (the trigger is the marker, not
    'any 400')."""
    import asyncio
    fake = _RecordingHttpClient([_FakeResponse(400, b"bad request")])
    client._client = fake
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert len(fake.payloads) == 1
    assert fake.payloads[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert classify_llm_error(excinfo.value) == CONTEXT_TOO_LONG
    assert CONTENT_RISK_HINT not in str(excinfo.value)


# ── the provider refuses the model's OWN output (finish_reason=content_filter) ─
#
# Rant 2026-09-28T16:58:08. The request succeeds and the answer does not, so the
# refusal arrives as an HTTP 200 with `content: ""` — measured on this host: 34
# such responses over two days, every one carrying a non-empty `reasoning` and
# an empty `content`, i.e. the filter fired during thinking and not one visible
# character ever existed. Two things follow, and both are asserted below: the
# ladder runs for this shape too, and every retry asks the model for *less*
# output (a transform alone re-samples the same prompt and gets the same
# refusal). Assertions are on the payload that was sent — a branch that logged
# the right warning and re-sent the unfixed body reads identically in the log.

_CONTENT_FILTER_BODY = (
    b'{"choices":[{"message":{"content":""},"finish_reason":"content_filter"}]}'
)
_CONTENT_FILTER_CHUNK = {"choices": [{"delta": {}, "finish_reason": "content_filter"}]}


def test_chat_content_filter_finish_walks_the_ladder_with_the_instruction(
    monkeypatch, client
):
    """A 200 + finish_reason=content_filter walks the same one-shot ladder as a
    400 refusal, and every retry carries the shorter-output instruction."""
    import asyncio
    trigger = "\U0001F1E6"
    original = [{"role": "user", "content": f"hi {trigger}"}]
    fake = _RecordingHttpClient([_FakeResponse(200, _CONTENT_FILTER_BODY)] * 3)
    client._client = fake

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(client.chat(list(original)))

    assert len(fake.payloads) == 3, "the ladder is one shot per rung, not a loop"
    # The first attempt is the caller's payload, verbatim — no instruction.
    assert fake.payloads[0]["messages"] == original
    assert original == [{"role": "user", "content": f"hi {trigger}"}], (
        "the caller's messages were mutated — the instruction is this retry's only"
    )
    # Rung 1: escaped, then the instruction as the final turn.
    assert fake.payloads[1]["messages"][0] == {
        "role": "user", "content": "hi <U+1F1E6>",
    }
    assert fake.payloads[1]["messages"][-1] == {
        "role": "user", "content": CONTENT_FILTER_RETRY_INSTRUCTION,
    }
    # Rung 2: built from the ORIGINAL (the trigger is still a codepoint, no
    # notation anywhere), plus the same instruction.
    assert fake.payloads[2]["messages"][0] == {
        "role": "user", "content": f"h i   {trigger}",
    }
    assert "<" not in fake.payloads[2]["messages"][0]["content"]
    assert fake.payloads[2]["messages"][-1] == {
        "role": "user", "content": CONTENT_FILTER_RETRY_INSTRUCTION,
    }
    assert str(excinfo.value) == CONTENT_FILTER_ERROR


def test_chat_content_filter_finish_recovers_on_the_next_rung(monkeypatch, client):
    """The refusal is answered, not propagated, while rungs remain."""
    import asyncio
    good = b'{"choices":[{"message":{"content":"recovered"},"finish_reason":"stop"}]}'
    fake = _RecordingHttpClient([_FakeResponse(200, _CONTENT_FILTER_BODY),
                                 _FakeResponse(200, good)])
    client._client = fake

    msg = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

    assert msg == {"content": "recovered"}
    assert len(fake.payloads) == 2
    assert fake.payloads[1]["messages"][-1]["content"] == CONTENT_FILTER_RETRY_INSTRUCTION
    # The retried payload is what llm.jsonl records (it is what was sent).
    assert client.last_payload["messages"][-1]["content"] == (
        CONTENT_FILTER_RETRY_INSTRUCTION
    )


def test_the_content_filter_error_is_not_the_credential_free_advice(monkeypatch, client):
    """The two refusals are different failures and must not share a hint.

    `CONTENT_RISK_HINT` sends the host to the session history, which is the
    right place for a *request* the provider refused and the wrong place for an
    answer the provider refused: here the trigger was in the model's own
    generation, so nothing in the history is at fault. Pinned because reusing
    the hint is the tempting shortcut (rant 2026-09-28T16:58:08, and the
    `with_content_risk_hint` helper makes it a one-liner).
    """
    assert CONTENT_RISK_HINT not in CONTENT_FILTER_ERROR
    assert "start a new session" not in CONTENT_FILTER_ERROR
    assert CONTENT_FILTER_FINISH in CONTENT_FILTER_ERROR
    for rung in CONTENT_RISK_LADDER:
        assert CONTENT_RISK_RUNG_DESCRIPTION[rung] in CONTENT_FILTER_ERROR, (
            f"the error does not name the {rung!r} rung it claims to have tried"
        )


def test_chat_refuses_an_altogether_empty_answer(monkeypatch, client):
    """`chat()`'s callers are all "give me text" — a summary, a title, a
    reflection. An empty 200 must be an error, never a stored empty string:
    that is what flattened a compact summary in the measured incident."""
    import asyncio
    empty = b'{"choices":[{"message":{"content":""},"finish_reason":"stop"}]}'
    fake = _RecordingHttpClient([_FakeResponse(200, empty)])
    client._client = fake

    with pytest.raises(RuntimeError, match="no answer at all") as excinfo:
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

    assert len(fake.payloads) == 1, "an empty answer is not retried — it is reported"
    assert classify_llm_error(excinfo.value) == OTHER_ERROR, (
        "an empty answer must not classify as content_risk: the chunker would "
        "re-send the same text into every depth of the recursion"
    )


def test_chat_keeps_a_tool_call_that_carries_no_text(monkeypatch, client):
    """Negative control for the empty-answer guard: `content: null` with tool
    calls is a legitimate response, and the guard keys on *both* facts."""
    import asyncio
    body = (b'{"choices":[{"message":{"content":null,'
            b'"tool_calls":[{"id":"c1","type":"function",'
            b'"function":{"name":"bash","arguments":"{}"}}]},'
            b'"finish_reason":"tool_calls"}]}')
    fake = _RecordingHttpClient([_FakeResponse(200, body)])
    client._client = fake

    msg = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

    assert msg["tool_calls"][0]["id"] == "c1"


def test_content_filter_retry_messages_appends_without_touching_the_caller():
    """The instruction is a *request-side* append on a copy.

    Nothing else may do: it must never reach the session history, or every
    later turn of the conversation would carry an explanation of an event the
    conversation has moved past.
    """
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    out = content_filter_retry_messages(messages)

    assert messages == [{"role": "system", "content": "s"},
                        {"role": "user", "content": "hi"}]
    assert out[:2] == messages
    assert out[2] == {"role": "user", "content": CONTENT_FILTER_RETRY_INSTRUCTION}
    assert len(out) == len(messages) + 1


def test_stream_content_filter_finish_walks_the_ladder_with_the_instruction(
    monkeypatch, client, caplog
):
    """The streaming twin, which is where the defect was actually seen."""
    import asyncio
    import logging
    caplog.set_level(logging.WARNING, logger="emrg.server.llm")
    trigger = "\U0001F1E6"
    original = [{"role": "user", "content": f"hi {trigger}"}]
    fake = _RecordingStreamClient([_make_stream(_CONTENT_FILTER_CHUNK)] * 3)
    client._client = fake

    async def _run():
        return [chunk async for chunk in client.chat_stream(list(original))]

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_run())

    assert len(fake.payloads) == 3, "the ladder is one shot per rung, not a loop"
    assert fake.payloads[0]["messages"] == original
    assert original == [{"role": "user", "content": f"hi {trigger}"}]
    assert fake.payloads[1]["messages"][-1]["content"] == CONTENT_FILTER_RETRY_INSTRUCTION
    assert fake.payloads[2]["messages"][-1]["content"] == CONTENT_FILTER_RETRY_INSTRUCTION
    assert fake.payloads[2]["messages"][0] == {"role": "user", "content": f"h i   {trigger}"}
    assert str(excinfo.value) == CONTENT_FILTER_ERROR
    # Requirement E: the reason is visible, with the rung and the verdict.
    assert CONTENT_FILTER_FINISH in caplog.text
    assert "shorter-output instruction" in caplog.text
    assert "again after 2 retries" in caplog.text, (
        "the terminal refusal was not reported: a spent ladder must be visible"
    )


def test_stream_content_filter_finish_recovers_and_never_yields_the_refusal(
    monkeypatch, client
):
    """A refused attempt is invisible to the caller: no chunk carrying
    finish_reason=content_filter ever reaches it, because the tool loop reads a
    finish_reason as the round's verdict."""
    import asyncio
    fake = _RecordingStreamClient([
        _make_stream(_CONTENT_FILTER_CHUNK),
        _make_stream(
            {"choices": [{"delta": {"content": "hi"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ),
    ])
    client._client = fake

    async def _run():
        return [chunk async for chunk in client.chat_stream(
            [{"role": "user", "content": "hi"}]
        )]

    chunks = asyncio.run(_run())

    assert [c["content"] for c in chunks if c.get("content")] == ["hi"]
    assert [c["finish_reason"] for c in chunks] == [None, "stop"], (
        "the caller saw the refusal's finish_reason, which the tool loop would "
        "read as this round's verdict"
    )
    assert fake.calls == 2
    assert fake.payloads[1]["messages"][-1]["content"] == CONTENT_FILTER_RETRY_INSTRUCTION


def test_stream_does_not_retry_a_filter_once_text_has_been_streamed(monkeypatch, client):
    """Requirement B, and the guard the tool loop depends on.

    Measured shape: the answer streams, then the filter fires — the client is
    already showing that text, so a retry would print the answer twice. The
    refusal is handed on as the round's finish_reason instead, and the count of
    requests is the assertion (a guard removed here is invisible in the log —
    both runs log "content filter blocked the answer").
    """
    import asyncio
    fake = _RecordingStreamClient([
        _make_stream(
            {"choices": [{"delta": {"content": "partial answer"}}]},
            _CONTENT_FILTER_CHUNK,
        ),
    ])
    client._client = fake

    async def _run():
        return [chunk async for chunk in client.chat_stream(
            [{"role": "user", "content": "hi"}]
        )]

    chunks = asyncio.run(_run())

    assert [c["content"] for c in chunks if c.get("content")] == ["partial answer"]
    assert chunks[-1]["finish_reason"] == CONTENT_FILTER_FINISH
    assert fake.calls == 1, "a refusal after visible text was re-sent"
    assert len(fake.payloads) == 1


def test_stream_does_not_retry_a_filter_once_a_tool_call_has_been_streamed(
    monkeypatch, client
):
    """The same guard for the other half of "produced something": a tool call
    that already reached the caller must not be asked for again."""
    import asyncio
    fake = _RecordingStreamClient([
        _make_stream(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "bash", "arguments": "{}"}},
            ]}}]},
            _CONTENT_FILTER_CHUNK,
        ),
    ])
    client._client = fake

    async def _run():
        return [chunk async for chunk in client.chat_stream(
            [{"role": "user", "content": "hi"}]
        )]

    chunks = asyncio.run(_run())

    assert chunks[-1]["finish_reason"] == CONTENT_FILTER_FINISH
    assert chunks[-1]["tool_calls"][0]["id"] == "c1"
    assert fake.calls == 1, "a refusal after a streamed tool call was re-sent"


# ── A tool call's `arguments` as it arrives from the provider ───────────────
#
# Measured 2026-10-02: the accumulator below did `arguments += value` for every
# truthy value, so anything that was not a string raised
# `TypeError: can only concatenate str (not "dict") to str` **out of the stream
# generator** — the turn died before any tool ran, and no frame named the call.
# The OpenAI contract is text accumulating across deltas, so a string is one more
# fragment; a dict is an off-contract whole payload from an OpenAI-compatible
# endpoint, and it is rendered to its text rather than dropped (dropping it would
# run the tool with no arguments at all).


class _ChunkStream:
    """One SSE response carrying a single tool_call delta with `arguments`."""

    status_code = 200
    headers = {}

    def __init__(self, arguments):
        self.arguments = arguments

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        delta = {"tool_calls": [{
            "index": 0, "id": "c1",
            "function": {"name": "bash", "arguments": self.arguments},
        }]}
        yield "data: " + json.dumps({"choices": [{"delta": delta}]})
        yield "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})


def _accumulated_arguments(client, arguments):
    """The `arguments` the stream ends up reporting for one such chunk."""
    import asyncio

    fake = _FakeStreamClient([_ChunkStream(arguments)])
    client._client = fake
    out: list[str] = []

    async def _run():
        async for chunk in client.chat_stream([{"role": "user", "content": "hi"}]):
            for tc in chunk.get("tool_calls") or []:
                out.append(tc["function"]["arguments"])

    asyncio.run(_run())
    return out[-1] if out else None


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ('{"command": "hi"}', '{"command": "hi"}'),
        ('{"command": ', '{"command": '),          # a fragment: never parsed here
        ("", ""),
        ({"command": "hi"}, '{"command": "hi"}'),  # off-contract, preserved
        ({"命令": "hi"}, '{"命令": "hi"}'),
        (5, ""),
        (["hi"], ""),
        (None, ""),
        (True, ""),
    ],
    ids=["text", "fragment", "empty", "dict", "dict-unicode", "int", "list", "none", "bool"],
)
def test_stream_accumulates_any_arguments_shape_without_raising(client, arguments, expected):
    """Every shape yields a `str` for the accumulator, and never an exception."""
    assert _accumulated_arguments(client, arguments) == expected
