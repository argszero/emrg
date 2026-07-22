"""LLM client for task routing and tool calling.

Calls OpenAI-compatible chat completion endpoints.
Supports multi-turn conversations with tool calling and streaming.

The streaming protocol for tool_calls is nuanced:
1. Individual chunks carry delta.tool_calls[{index, id, function: {name?, arguments}}]
2. The id and name arrive once (usually in the first chunk for that index)
3. arguments arrive incrementally across multiple chunks
4. finish_reason == "tool_calls" signals the end and final flush
5. The client accumulates across chunks and yields aggregated dicts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

import httpx

from emrg.config import LlmConfig

logger = logging.getLogger(__name__)

# HTTP status codes that warrant a retry
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds, doubled each retry


class LlmClient:
    """Async LLM client with tool calling and multi-turn streaming support."""

    def __init__(self, config: LlmConfig) -> None:
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    def _make_payload(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        stream: bool = False,
    ) -> dict:
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream"] = True
            if self.config.stream_options is not None:
                payload["stream_options"] = self.config.stream_options
        return payload

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "emrg/0.1",
        }

    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> dict:
        """Non-streaming chat completion with optional tool calling.

        Returns the full message dict from choices[0].message,
        which may contain 'content' (str or None) and/or 'tool_calls'.

        Retries on transient errors (429, 5xx) with exponential backoff.
        """
        client = await self._get_client()
        url = f"{self.config.base_url}/chat/completions"
        payload = self._make_payload(messages, tools, stream=False)
        headers = self._headers()

        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            logger.debug("LLM request: url=%s model=%s (attempt %d/%d)",
                         url, self.config.model, attempt + 1, MAX_RETRIES + 1)

            resp = await client.post(url, headers=headers, json=payload)

            if resp.status_code == 200:
                data = resp.json()
                choice = data["choices"][0]
                return choice.get("message", {})

            text = resp.text[:500]
            if resp.status_code in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "LLM transient error %d, retrying in %.1fs (attempt %d/%d): %s",
                    resp.status_code, delay, attempt + 1, MAX_RETRIES, text[:200],
                )
                await asyncio.sleep(delay)
                last_error = RuntimeError(
                    f"LLM request failed: {resp.status_code} - {text}"
                )
                continue

            hdr = dict(resp.headers)
            logger.error("LLM error: %s headers=%s body=%s", resp.status_code, hdr, text)
            raise RuntimeError(
                f"LLM request failed: {resp.status_code} headers={hdr} body={text}"
            )

        raise last_error  # type: ignore[misc]

    async def chat_stream(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> AsyncIterator[dict]:
        """Streaming chat with optional tool calling.

        Yields dicts of shape:
            {"content": str | None, "tool_calls": list[dict] | None,
             "finish_reason": str | None, "usage": dict | None}

        tool_calls are accumulated across chunks (by index). Each yield
        carries the current accumulated state so callers can track progress.

        The final yield includes usage (prompt_tokens, completion_tokens)
        when the API provides it.

        Finish reasons: "stop" (final text), "tool_calls" (model wants tools),
        "length" (max_tokens hit), "content_filter" (blocked).

        Retries on transient HTTP errors (429, 5xx) with exponential backoff,
        same as chat().
        """
        client = await self._get_client()
        url = f"{self.config.base_url}/chat/completions"
        payload = self._make_payload(messages, tools, stream=True)
        headers = {**self._headers(), "Accept": "text/event-stream"}

        logger.debug("LLM stream: url=%s model=%s", url, self.config.model)

        # Accumulated state across chunks (reset on retry)
        content_parts: list[str] = []
        tc_by_index: dict[int, dict] = {}

        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            logger.debug("LLM stream attempt %d/%d", attempt + 1, MAX_RETRIES + 1)
            # Reset accumulators before each attempt
            content_parts[:] = []
            tc_by_index.clear()

            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    text = await resp.aread()
                    if resp.status_code in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                        delay = RETRY_BASE_DELAY * (2 ** attempt)
                        logger.warning(
                            "LLM stream transient error %d, retrying in %.1fs "
                            "(attempt %d/%d): %s",
                            resp.status_code, delay, attempt + 1, MAX_RETRIES,
                            text[:200],
                        )
                        await asyncio.sleep(delay)
                        last_error = RuntimeError(
                            f"LLM stream request failed: {resp.status_code} - {text}"
                        )
                        continue
                    logger.error("LLM stream error: %s %s", resp.status_code, text[:500])
                    hdr = dict(resp.headers)
                    raise RuntimeError(
                        f"LLM stream request failed: {resp.status_code} "
                        f"headers={hdr} body={text[:1000]}"
                    )

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or line == "[DONE]" or not line.startswith("data: "):
                        continue

                    json_str = line[6:]
                    try:
                        chunk = json.loads(json_str)
                    except json.JSONDecodeError:
                        logger.debug("SSE parse skip: %s", json_str[:80])
                        continue

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    finish = choices[0].get("finish_reason")

                    # Accumulate text content
                    text_content = delta.get("content", "")
                    if text_content:
                        content_parts.append(text_content)

                    # Accumulate tool_calls from delta
                    for tc in delta.get("tool_calls", []):
                        idx = tc.get("index", 0)
                        if idx not in tc_by_index:
                            tc_by_index[idx] = {
                                "index": idx,
                                "id": tc.get("id", ""),
                                "function": {"name": "", "arguments": ""},
                            }
                        acc = tc_by_index[idx]
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            acc["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            acc["function"]["arguments"] += fn["arguments"]

                    # Build current accumulated tool_calls list
                    current_tool_calls: list[dict] | None = None
                    if tc_by_index:
                        current_tool_calls = [
                            tc_by_index[i] for i in sorted(tc_by_index.keys())
                        ]

                    # Capture usage from chunk (may appear in any chunk or only in final)
                    usage = chunk.get("usage")

                    yield {
                        "content": text_content or None,
                        "tool_calls": current_tool_calls,
                        "finish_reason": finish,
                        "usage": {
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                        } if usage else None,
                    }

                    # On finish, we're done with this stream
                    if finish:
                        return

                # Stream ended without finish_reason (e.g. connection drop
                # mid-stream). Treat as transient error — retry if attempts remain.
                last_error = RuntimeError(
                    "LLM stream ended without finish_reason"
                )
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "LLM stream ended prematurely, retrying in %.1fs "
                        "(attempt %d/%d)",
                        delay, attempt + 1, MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue

        raise last_error  # type: ignore[misc]

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
