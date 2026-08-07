"""Unit tests for LlmClient — payload construction and headers.

These test the pure methods (_make_payload, _headers) that don't
require network access or asyncio event loops.
"""

from __future__ import annotations

import pytest

from emrg.config import LlmConfig
from emrg.server.llm import LlmClient


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
    assert fake.calls == 4  # 1 initial + 3 retries
