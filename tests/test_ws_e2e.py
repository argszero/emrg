"""End-to-end WebSocket protocol tests (Phase 1).

Boots a real EmrgServer over WebSocket with a mocked LLM and an isolated
config dir, then exercises the full protocol: auth, ping, task streaming,
invalid messages, cancel. Follows the design doc §5.1 isolation rules:
mock the LLM (no real API calls) and mock the scheduler (no task config
pollution).

Tests use asyncio.run() directly since pytest-asyncio is not installed.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from emrg.config import LlmConfig
from emrg.connect import connect_to_server


def _make_config() -> LlmConfig:
    return LlmConfig(
        base_url="http://localhost:9999/v1",
        api_key="test-key",
        model="test-model",
        max_tokens=100,
        temperature=0.0,
        context_window=4096,
        auto_compact_threshold=0.0,
        vision=False,
    )


def _make_fake_chat_stream():
    """Round-aware fake LLM stream.

    Round 1 yields a tool call (bash echo hi); round 2+ yields a plain
    text stop. This mirrors a real LLM: tool calls and stop live in
    different rounds, so the daemon's Case 1 (stop) / Case 2 (tool_calls)
    branches are both exercised.
    """
    state = {"round": 0}

    async def fake_chat_stream(messages, tools=None):
        state["round"] += 1
        if state["round"] == 1:
            yield {"content": "让我调用工具", "tool_calls": None, "finish_reason": None, "usage": None}
            yield {
                "content": None,
                "tool_calls": [{
                    "index": 0, "id": "call_1",
                    "function": {"name": "bash", "arguments": '{"command":"echo hi"}'},
                }],
                "finish_reason": "tool_calls", "usage": None,
            }
        else:
            yield {"content": "完成", "tool_calls": None, "finish_reason": "stop",
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    return fake_chat_stream


async def _boot_server(tmp: Path):
    """Boot a real EmrgServer in the isolated config dir.

    serve() contains serve_forever(), so run it as a background task and
    wait for the port file to appear. Returns (server, serve_task).
    """
    import emrg.server.daemon as daemon_mod
    import emrg.connect as connect_mod

    _orig_daemon_cfg = daemon_mod.config_dir
    _orig_connect_cfg = connect_mod.config_dir

    # Isolate config dir to tmp (port file, tasks.yml, etc.)
    daemon_mod.config_dir = lambda: tmp
    connect_mod.config_dir = lambda: tmp

    server = daemon_mod.EmrgServer(_make_config())
    server.llm = AsyncMock()
    server.llm.config = _make_config()  # real config so _run_tool_loop reads thresholds
    server.llm.last_payload = {}
    server.llm.last_response_status = 200
    server.llm.last_response_headers = {}
    server._scheduler = AsyncMock()  # no real scheduler / tasks.yml

    server.llm.chat_stream = _make_fake_chat_stream()

    task = asyncio.create_task(server.serve())
    # Wait for the port file (daemon ready)
    for _ in range(200):
        if (tmp / "emrgd.port").exists():
            break
        await asyncio.sleep(0.05)
    assert (tmp / "emrgd.port").exists(), "daemon did not publish port file"

    async def _cleanup():
        server._server.close()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        daemon_mod.config_dir = _orig_daemon_cfg
        connect_mod.config_dir = _orig_connect_cfg

    return server, task, _cleanup


class TestWSAuth:
    def test_ping_pong_roundtrip(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                _, _, cleanup = await _boot_server(Path(tmp))
                try:
                    ws = await connect_to_server()
                    try:
                        await ws.send(json.dumps({"type": "ping"}))
                        frame = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(frame)
                        assert "uptime_seconds" in data
                        assert "identity" in data
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_wrong_token_rejected(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                server, _, cleanup = await _boot_server(Path(tmp))
                try:
                    port = server._server.sockets[0].getsockname()[1]
                    ws = await connect(f"ws://127.0.0.1:{port}", max_size=16 * 1024 * 1024)
                    await ws.send(json.dumps({"type": "auth", "token": "wrong-token"}))
                    with pytest.raises(ConnectionClosed):
                        await asyncio.wait_for(ws.recv(), timeout=5)
                    await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_invalid_auth_json_rejected(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                server, _, cleanup = await _boot_server(Path(tmp))
                try:
                    port = server._server.sockets[0].getsockname()[1]
                    ws = await connect(f"ws://127.0.0.1:{port}", max_size=16 * 1024 * 1024)
                    await ws.send("not json")
                    with pytest.raises(ConnectionClosed):
                        await asyncio.wait_for(ws.recv(), timeout=5)
                    await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())


class TestWSProtocol:
    def test_bad_json_message_gets_error_frame(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                _, _, cleanup = await _boot_server(Path(tmp))
                try:
                    ws = await connect_to_server()
                    try:
                        await ws.send("{invalid json")
                        frame = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(frame)
                        assert "error" in data
                        assert "invalid json" in data["error"]
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_non_dict_json_gets_error_frame(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                _, _, cleanup = await _boot_server(Path(tmp))
                try:
                    ws = await connect_to_server()
                    try:
                        await ws.send("[1,2]")
                        frame = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(frame)
                        assert data.get("error") == "message must be a JSON object"
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_streaming_task_with_tool_calls(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        task = {
                            "type": "task",
                            "id": "t1",
                            "session_id": "s_e2e_stream",
                            "cwd": str(cwd),
                            "prompt": "你好",
                            "stream": True,
                            "timestamp": "2026-08-02T00:00:00",
                        }
                        await ws.send(json.dumps(task, ensure_ascii=False))
                        got_delta = got_tool = got_done = False
                        while True:
                            frame = await asyncio.wait_for(ws.recv(), timeout=10)
                            resp = json.loads(frame)
                            if resp.get("delta") or "content" in resp:
                                got_delta = True
                            if "tool_name" in resp:
                                got_tool = True
                            if resp.get("done"):
                                got_done = True
                                break
                        assert got_delta and got_tool and got_done
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_cancel_stops_task(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        task = {
                            "type": "task",
                            "id": "t2",
                            "session_id": "s_e2e_cancel",
                            "cwd": str(cwd),
                            "prompt": "你好",
                            "stream": True,
                            "timestamp": "2026-08-02T00:00:00",
                        }
                        await ws.send(json.dumps(task, ensure_ascii=False))
                        await asyncio.sleep(0.3)  # let the tool loop start
                        await ws.send(json.dumps({"type": "cancel", "session_id": "s_e2e_cancel"}))
                        while True:
                            frame = await asyncio.wait_for(ws.recv(), timeout=10)
                            resp = json.loads(frame)
                            if resp.get("type") == "cancelled":
                                break
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())
