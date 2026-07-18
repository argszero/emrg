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
    """Default max_tokens from LlmConfig is 4096."""
    default_cfg = LlmConfig()
    c = LlmClient(default_cfg)
    p = c._make_payload([{"role": "user", "content": "x"}])
    assert p["max_tokens"] == 4096


def test_payload_temperature_default():
    """Default temperature is 0.7."""
    default_cfg = LlmConfig()
    c = LlmClient(default_cfg)
    p = c._make_payload([{"role": "user", "content": "x"}])
    assert p["temperature"] == 0.7
