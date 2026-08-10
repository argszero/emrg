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
from emrg.server.tool_types import ToolResult


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

    def test_large_message_roundtrip(self):
        """>1MB JSON payload round-trips without truncation (max_size=16MB)."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        # Unknown type → error frame, but proves the payload
                        # arrived intact (no 1MB cap truncation / disconnect).
                        big = "x" * (2 * 1024 * 1024)  # 2MB
                        await ws.send(json.dumps({"type": "no_such_type", "payload": big}))
                        frame = await asyncio.wait_for(ws.recv(), timeout=10)
                        data = json.loads(frame)
                        assert data.get("error") == "unknown message type"
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_cjk_message_roundtrip(self):
        """CJK messages round-trip intact."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        await ws.send(json.dumps({"type": "no_such_type", "payload": "你好，世界！"}))
                        frame = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(frame)
                        assert data.get("error") == "unknown message type"
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_disconnect_detected_on_server_close(self):
        """Client recv raises ConnectionClosed when the daemon dies.

        This is the protocol-level precondition for the TUI's
        _reconnect() loop: a dead daemon must surface as an exception
        (not None or a hang), so read_server can trigger reconnection.
        """
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                server, _, cleanup = await _boot_server(Path(tmp))
                ws = await connect_to_server()
                try:
                    # Kill the daemon (server.close() stops accepting & closes conns)
                    server._server.close()
                    await asyncio.sleep(0.3)
                    with pytest.raises(ConnectionClosed):
                        await asyncio.wait_for(ws.recv(), timeout=5)
                finally:
                    await ws.close()
                    await cleanup()
        asyncio.run(_test())


class TestWSBroadcast:
    """Phase 2 broadcast model (protocol-contract §2.6).

    Same session → all subscribed connections see the same streaming
    response. Concurrent task on a busy session → queued (task_queued,
    P1 rant 2026-08-10T21:55:37), injected at the next round boundary or
    re-sent via queued_requeue when the turn ends normally.
    """

    def test_broadcast_to_subscribers(self):
        """Two connections on the same session: B sees A's streaming task."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws_a = await connect_to_server()
                    ws_b = await connect_to_server()
                    try:
                        # B subscribes to the session first (any message carrying
                        # session_id triggers the subscription in the read loop).
                        await ws_b.send(json.dumps({
                            "type": "list_history",
                            "session_id": "s_bcast",
                            "cwd": str(cwd),
                        }))
                        await asyncio.wait_for(ws_b.recv(), timeout=5)  # history_list
                        task = {
                            "type": "task",
                            "id": "t-bcast",
                            "session_id": "s_bcast",
                            "cwd": str(cwd),
                            "prompt": "你好",
                            "stream": True,
                            "timestamp": "2026-08-02T00:00:00",
                        }
                        await ws_a.send(json.dumps(task, ensure_ascii=False))
                        # B must see the same streaming events as A
                        got_delta = got_tool = got_done = False
                        while True:
                            frame = await asyncio.wait_for(ws_b.recv(), timeout=10)
                            resp = json.loads(frame)
                            if resp.get("delta"):
                                got_delta = True
                            if "tool_name" in resp:
                                got_tool = True
                            if resp.get("done"):
                                got_done = True
                                break
                        assert got_delta and got_tool and got_done
                    finally:
                        await ws_a.close()
                        await ws_b.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_task_queued_instead_of_busy_error(self):
        """A's task holds the session lock; B's task is queued (task_queued,
        NOT 'session busy'), then injected into A's turn at the stop boundary
        (steer_committed) — the message is never lost."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    async def slow_chat_stream(messages, tools=None):
                        yield {"content": "处理中", "tool_calls": None, "finish_reason": None, "usage": None}
                        await asyncio.sleep(0.6)
                        yield {"content": "完成", "tool_calls": None, "finish_reason": "stop", "usage": None}
                    server.llm.chat_stream = slow_chat_stream
                    ws_a = await connect_to_server()
                    ws_b = await connect_to_server()
                    try:
                        task = {
                            "type": "task", "id": "t-busy-a", "session_id": "s_busy",
                            "cwd": str(cwd), "prompt": "hi", "stream": True,
                            "timestamp": "2026-08-02T00:00:00",
                        }
                        await ws_a.send(json.dumps(task, ensure_ascii=False))
                        await asyncio.sleep(0.2)  # let A's task grab the lock
                        task_b = {**task, "id": "t-busy-b"}
                        await ws_b.send(json.dumps(task_b, ensure_ascii=False))
                        # B must get task_queued (with position), NOT "session busy"
                        resp = json.loads(await asyncio.wait_for(ws_b.recv(), timeout=5))
                        assert resp.get("type") == "task_queued", f"got {resp!r}"
                        assert resp.get("request_id") == "t-busy-b"
                        assert resp.get("session_id") == "s_busy"
                        assert resp.get("position") == 1
                        # A's stop branch injects B's message into the same turn
                        fsc = await _recv_until(
                            ws_b, lambda f: f.get("type") == "steer_committed",
                            what="steer_committed")
                        assert fsc.get("request_id") == "t-busy-b"
                        # A's turn (now containing B's message) completes
                        await _recv_until(
                            ws_a,
                            lambda f: f.get("done") and f.get("request_id") == "t-busy-a",
                            what="t-busy-a done")
                    finally:
                        await ws_a.close()
                        await ws_b.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_queued_requeue_on_normal_end(self):
        """Turn ends with a message still queued (race window: it arrived
        after the last injection drain) → the wrapper broadcasts
        queued_requeue with the request_ids so clients re-send. White-box:
        _inject_pending_messages is stubbed to never drain, forcing the
        queue to survive until the wrapper's finally."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    async def slow_chat_stream(messages, tools=None):
                        yield {"content": "处理中", "tool_calls": None, "finish_reason": None, "usage": None}
                        await asyncio.sleep(0.5)
                        yield {"content": "完成", "tool_calls": None, "finish_reason": "stop", "usage": None}
                    server.llm.chat_stream = slow_chat_stream
                    async def no_drain(session, messages):
                        return 0, False
                    server._inject_pending_messages = no_drain  # type: ignore[assignment]
                    ws_a = await connect_to_server()
                    ws_b = await connect_to_server()
                    try:
                        task = {
                            "type": "task", "id": "t-rq-a", "session_id": "s_rq",
                            "cwd": str(cwd), "prompt": "hi", "stream": True,
                            "timestamp": "2026-08-02T00:00:00",
                        }
                        await ws_a.send(json.dumps(task, ensure_ascii=False))
                        await asyncio.sleep(0.2)
                        task_b = {**task, "id": "t-rq-b"}
                        await ws_b.send(json.dumps(task_b, ensure_ascii=False))
                        resp = json.loads(await asyncio.wait_for(ws_b.recv(), timeout=5))
                        assert resp.get("type") == "task_queued"
                        # Turn ends normally → queued_requeue carries the ids
                        requeue = await _recv_until(
                            ws_b, lambda f: f.get("type") == "queued_requeue",
                            what="queued_requeue")
                        assert "t-rq-b" in requeue.get("request_ids", [])
                        # Re-send now that the lock is released → normal execution
                        await ws_b.send(json.dumps(task_b, ensure_ascii=False))
                        await _recv_until(
                            ws_b,
                            lambda f: f.get("done") and f.get("request_id") == "t-rq-b",
                            what="t-rq-b done")
                    finally:
                        await ws_a.close()
                        await ws_b.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_unsubscribe_on_disconnect(self):
        """B disconnects → removed from the session's subscriber set.

        Note: server-side subscription holds ServerConnection objects (the
        handler's ws), which differ from the client-side ClientConnection
        handles. We assert by subscriber-set size: 2 before, 1 after B closes.
        """
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    ws_a = await connect_to_server()
                    ws_b = await connect_to_server()
                    try:
                        for w in (ws_a, ws_b):
                            await w.send(json.dumps({
                                "type": "list_history",
                                "session_id": "s_unsub",
                                "cwd": str(cwd),
                            }))
                            await asyncio.wait_for(w.recv(), timeout=5)
                        assert len(server._session_subscribers.get("s_unsub", set())) == 2
                    finally:
                        await ws_b.close()
                    # Daemon processes the disconnect → unsubscribes B
                    await asyncio.sleep(0.3)
                    subs = server._session_subscribers.get("s_unsub", set())
                    assert len(subs) == 1  # B removed, A still subscribed
                    await ws_a.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_model_set_broadcast(self):
        """B switches model → A receives the model_set broadcast (global state)."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws_a = await connect_to_server()
                    ws_b = await connect_to_server()
                    try:
                        await ws_b.send(json.dumps({"type": "set_model", "model": "model-x"}))
                        # A must receive the broadcast model_set
                        while True:
                            frame = await asyncio.wait_for(ws_a.recv(), timeout=5)
                            resp = json.loads(frame)
                            if resp.get("type") == "model_set":
                                assert resp["model"] == "model-x"
                                break
                    finally:
                        await ws_a.close()
                        await ws_b.close()
                finally:
                    await cleanup()
        asyncio.run(_test())


class TestWSEvolutionSummary:
    """evolution_summary command (WorkBuddy P3, #502) — count + recent list.

    Covers: empty logs, ordered recent list, limit clamp (1-20), corrupt
    file skip. Uses the isolated tmp config dir as the logs root.
    """

    def _write_log(self, tmp: Path, name: str, timestamp: str, operations, impact=None):
        """Write an evolution-*.json log file under tmp/logs."""
        (tmp / "logs").mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": timestamp, "operations": operations, "impact": impact or []}
        (tmp / "logs" / name).write_text(json.dumps(payload), encoding="utf-8")

    def test_evolution_summary_empty_and_with_logs(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                server, _, cleanup = await _boot_server(tmp)
                try:
                    # ── 1. empty: no log files → recent=[], count=0 ──
                    ws = await connect_to_server()
                    try:
                        await ws.send(json.dumps({"type": "evolution_summary", "limit": 5}))
                        frame = await asyncio.wait_for(ws.recv(), timeout=5)
                        resp = json.loads(frame)
                        assert resp["type"] == "evolution_summary"
                        assert resp["count"] == 0
                        assert resp["recent"] == []
                    finally:
                        await ws.close()

                    # ── 2. with logs: newest-first, corrupt skipped ──
                    self._write_log(tmp, "evolution-20260806-090000.json", "2026-08-06T09:00:00",
                                     ["llm-reflection"])
                    self._write_log(tmp, "evolution-20260806-100000.json", "2026-08-06T10:00:00",
                                     ["tool-execution"], impact=["fixed-x"])
                    (tmp / "logs" / "evolution-20260806-110000.json").write_text(
                        "{corrupt json", encoding="utf-8")  # must be skipped, not crash

                    ws = await connect_to_server()
                    try:
                        await ws.send(json.dumps({"type": "evolution_summary", "limit": 10}))
                        frame = await asyncio.wait_for(ws.recv(), timeout=5)
                        resp = json.loads(frame)
                        assert resp["type"] == "evolution_summary"
                        assert resp["count"] == 2  # valid disk logs counted (corrupt skipped)
                        # newest first (reverse lexicographic = chronological)
                        assert [r["timestamp"] for r in resp["recent"]] == [
                            "2026-08-06T10:00:00", "2026-08-06T09:00:00"]
                        assert resp["recent"][0]["operations"] == ["tool-execution"]
                        assert resp["recent"][0]["impact"] == ["fixed-x"]
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_evolution_summary_limit_clamped_to_20(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                _, _, cleanup = await _boot_server(tmp)
                try:
                    # write 25 valid logs
                    for i in range(25):
                        self._write_log(tmp, f"evolution-20260806-{i:06d}.json",
                                        f"2026-08-06T00:{i:02d}:00", [f"op-{i}"])
                    ws = await connect_to_server()
                    try:
                        await ws.send(json.dumps({"type": "evolution_summary", "limit": 100}))
                        frame = await asyncio.wait_for(ws.recv(), timeout=5)
                        resp = json.loads(frame)
                        assert resp["type"] == "evolution_summary"
                        assert resp["count"] == 25, f"count must include all valid disk logs, got {resp['count']}"
                        assert len(resp["recent"]) == 20, f"limit must clamp to 20, got {len(resp['recent'])}"
                        # newest-first: first entry is the highest timestamp
                        assert resp["recent"][0]["timestamp"] == "2026-08-06T00:24:00"
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())


async def _recv_until(ws, pred, timeout=10, limit=50, what="frame"):
    """Read frames (skipping unrelated broadcasts) until pred(frame) or fail."""
    for _ in range(limit):
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if pred(frame):
            return frame
    raise AssertionError(f"expected {what} not received in {limit} frames")


class TestWSQueueInjection:
    """P1 queue-injection (rant 2026-08-10T21:55:37, design doc
    mid-turn-input-queue): messages sent while a session's tool loop is busy
    are queued per session and injected at the next round boundary (after the
    current round's LLM request + ALL tool executions, before the next LLM
    request) — never interrupting tools, never losing messages.

    All clients subscribed to a session receive that session's broadcast
    stream, so every read here filters for the expected frame type (queue
    frames interleave with the active turn's deltas/tool frames).
    """

    @staticmethod
    def _task(sid, tid, prompt, cwd, mode="auto"):
        return {
            "type": "task", "id": tid, "session_id": sid,
            "cwd": str(cwd), "prompt": prompt, "stream": True,
            "mode": mode, "timestamp": "2026-08-10T21:55:37",
        }

    def test_pending_injected_at_round_boundary_after_tools(self):
        """B's message queued while A's tool executes is injected at the
        round boundary: round 2's LLM request sees it, the tool ran to
        completion first (no interruption), and steer_committed is
        broadcast. Nothing is lost."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    loop = asyncio.get_running_loop()
                    seen = {"round2_user_texts": None, "round2_tools": None,
                            "t_inject_seen": None, "t_tool_end": None}

                    class _SlowBash:
                        async def execute(self, args):
                            await asyncio.sleep(0.6)
                            seen["t_tool_end"] = loop.time()
                            return ToolResult(tool_call_id="call_1", name="bash",
                                              content="hi", error=False)

                    orig_get = server.tools.get
                    server.tools.get = lambda name: _SlowBash() if name == "bash" else orig_get(name)

                    async def chat_stream(messages, tools=None):
                        user_texts = [m.get("content") for m in messages
                                      if m.get("role") == "user"]
                        if any("steer-mid" in str(t) for t in user_texts):
                            seen["round2_user_texts"] = user_texts
                            seen["round2_tools"] = tools
                            seen["t_inject_seen"] = loop.time()
                            yield {"content": "收到", "tool_calls": None,
                                   "finish_reason": "stop", "usage": None}
                            return
                        yield {"content": "开始", "tool_calls": None,
                               "finish_reason": None, "usage": None}
                        yield {"content": None, "tool_calls": [{
                            "index": 0, "id": "call_1",
                            "function": {"name": "bash",
                                         "arguments": '{"command":"echo hi"}'},
                        }], "finish_reason": "tool_calls", "usage": None}
                    server.llm.chat_stream = chat_stream

                    ws_a = await connect_to_server()
                    ws_b = await connect_to_server()
                    try:
                        await ws_a.send(json.dumps(
                            self._task("s_inj", "t-inj-a", "turn-one", cwd), ensure_ascii=False))
                        await asyncio.sleep(0.15)  # round 1 tool now executing
                        await ws_b.send(json.dumps(
                            self._task("s_inj", "t-inj-b", "steer-mid", cwd), ensure_ascii=False))

                        # B: task_queued then steer_committed
                        fq = await _recv_until(
                            ws_b, lambda f: f.get("type") == "task_queued", what="task_queued")
                        assert fq.get("position") == 1
                        fsc = await _recv_until(
                            ws_b, lambda f: f.get("type") == "steer_committed",
                            what="steer_committed")
                        assert fsc.get("request_id") == "t-inj-b"

                        # A: tool_end before done
                        tool_end_seen = done_seen = False
                        while not done_seen:
                            fa = json.loads(await asyncio.wait_for(ws_a.recv(), timeout=10))
                            if fa.get("type") == "tool_end":
                                tool_end_seen = True
                            if fa.get("done"):
                                done_seen = True
                        assert tool_end_seen, "tool must run to completion"

                        # round 2 LLM request received the injected message
                        assert seen["round2_user_texts"] is not None
                        assert any("steer-mid" in str(t) for t in seen["round2_user_texts"])
                        # injection happened strictly after the tool finished
                        assert seen["t_inject_seen"] >= seen["t_tool_end"] - 0.05
                        # tools preserved for auto-mode rounds
                        assert seen["round2_tools"] is not None
                        assert len(seen["round2_tools"]) > 0
                    finally:
                        await ws_a.close()
                        await ws_b.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_pending_ask_injects_empty_tools(self):
        """A queued Ask-mode message is injected with an empty tool set
        (mode=ask → tools=[]), so the injected round can only reply."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    loop = asyncio.get_running_loop()
                    seen = {"tools": "unset"}

                    class _SlowBash:
                        async def execute(self, args):
                            await asyncio.sleep(0.5)
                            return ToolResult(tool_call_id="call_1", name="bash",
                                              content="hi", error=False)

                    orig_get = server.tools.get
                    server.tools.get = lambda name: _SlowBash() if name == "bash" else orig_get(name)

                    async def chat_stream(messages, tools=None):
                        user_texts = [m.get("content") for m in messages
                                      if m.get("role") == "user"]
                        if any("ask-me" in str(t) for t in user_texts):
                            seen["tools"] = tools
                            yield {"content": "仅回复", "tool_calls": None,
                                   "finish_reason": "stop", "usage": None}
                            return
                        yield {"content": None, "tool_calls": [{
                            "index": 0, "id": "call_1",
                            "function": {"name": "bash",
                                         "arguments": '{"command":"echo hi"}'},
                        }], "finish_reason": "tool_calls", "usage": None}
                    server.llm.chat_stream = chat_stream

                    ws_a = await connect_to_server()
                    ws_b = await connect_to_server()
                    try:
                        await ws_a.send(json.dumps(
                            self._task("s_ask", "t-ask-a", "turn", cwd), ensure_ascii=False))
                        await asyncio.sleep(0.15)
                        await ws_b.send(json.dumps(
                            self._task("s_ask", "t-ask-b", "ask-me", cwd, mode="ask"),
                            ensure_ascii=False))
                        fq = await _recv_until(
                            ws_b, lambda f: f.get("type") == "task_queued", what="task_queued")
                        while seen["tools"] == "unset":
                            await asyncio.wait_for(ws_a.recv(), timeout=10)
                        assert seen["tools"] == [], f"ask round must have empty tools, got {seen['tools']}"
                    finally:
                        await ws_a.close()
                        await ws_b.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_queued_cancelled_on_cancel(self):
        """A cancels mid-turn → the queued message is not lost silently:
        clients get queued_cancelled (queue dropped, client can re-send)."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    async def long_stream(messages, tools=None):
                        yield {"content": "工作中", "tool_calls": None,
                               "finish_reason": None, "usage": None}
                        await asyncio.sleep(5)
                        yield {"content": "完成", "tool_calls": None,
                               "finish_reason": "stop", "usage": None}
                    server.llm.chat_stream = long_stream
                    ws_a = await connect_to_server()
                    ws_b = await connect_to_server()
                    try:
                        await ws_a.send(json.dumps(
                            self._task("s_can", "t-can-a", "long", cwd), ensure_ascii=False))
                        await asyncio.sleep(0.2)
                        await ws_b.send(json.dumps(
                            self._task("s_can", "t-can-b", "queued", cwd), ensure_ascii=False))
                        fq = await _recv_until(
                            ws_b, lambda f: f.get("type") == "task_queued", what="task_queued")
                        # A cancels
                        await ws_a.send(json.dumps({"type": "cancel", "session_id": "s_can"}))
                        # A gets cancelled frame
                        got_cancelled = False
                        while not got_cancelled:
                            fa = json.loads(await asyncio.wait_for(ws_a.recv(), timeout=10))
                            if fa.get("type") == "cancelled":
                                got_cancelled = True
                        # B gets queued_cancelled (skipping A's cancelled-done broadcast)
                        fb = await _recv_until(
                            ws_b, lambda f: f.get("type") == "queued_cancelled",
                            what="queued_cancelled")
                        assert fb.get("session_id") == "s_can"
                    finally:
                        await ws_a.close()
                        await ws_b.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_clear_session_drops_pending(self):
        """clear_session pops the session's pending queue and broadcasts
        queued_cancelled (Change F, design doc)."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    async def long_stream(messages, tools=None):
                        yield {"content": "工作中", "tool_calls": None,
                               "finish_reason": None, "usage": None}
                        await asyncio.sleep(1.0)
                        yield {"content": "完成", "tool_calls": None,
                               "finish_reason": "stop", "usage": None}
                    server.llm.chat_stream = long_stream
                    ws_a = await connect_to_server()
                    ws_b = await connect_to_server()
                    try:
                        await ws_a.send(json.dumps(
                            self._task("s_drop", "t-drop-a", "long", cwd), ensure_ascii=False))
                        await asyncio.sleep(0.2)
                        await ws_b.send(json.dumps(
                            self._task("s_drop", "t-drop-b", "queued", cwd), ensure_ascii=False))
                        fq = await _recv_until(
                            ws_b, lambda f: f.get("type") == "task_queued", what="task_queued")
                        assert len(server._session_pending.get("s_drop", [])) == 1
                        # B clears the session while its message is queued
                        await ws_b.send(json.dumps({
                            "type": "clear_session", "session_id": "s_drop", "cwd": str(cwd),
                        }))
                        got_clear = got_qc = False
                        while not (got_clear and got_qc):
                            fb = json.loads(await asyncio.wait_for(ws_b.recv(), timeout=10))
                            if fb.get("type") == "clear_result" and fb.get("ok"):
                                got_clear = True
                            if fb.get("type") == "queued_cancelled":
                                got_qc = True
                        assert got_clear and got_qc
                        assert server._session_pending.get("s_drop") in (None, [])
                        # A's turn ends normally → no requeue (queue was dropped)
                        got_done = False
                        while not got_done:
                            fa = json.loads(await asyncio.wait_for(ws_a.recv(), timeout=10))
                            if fa.get("done"):
                                got_done = True
                        assert server._session_pending.get("s_drop") in (None, [])
                    finally:
                        await ws_a.close()
                        await ws_b.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_per_session_isolation(self):
        """The pending queue is per-session: a task on a different session
        runs immediately (not queued); a task on the busy session queues."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    async def chat_stream(messages, tools=None):
                        user_texts = [m.get("content") for m in messages
                                      if m.get("role") == "user"]
                        if any("iso2" in str(t) for t in user_texts):
                            yield {"content": "iso2-done", "tool_calls": None,
                                   "finish_reason": "stop", "usage": None}
                            return
                        yield {"content": "iso1-start", "tool_calls": None,
                               "finish_reason": None, "usage": None}
                        await asyncio.sleep(1.2)
                        yield {"content": "iso1-done", "tool_calls": None,
                               "finish_reason": "stop", "usage": None}
                    server.llm.chat_stream = chat_stream
                    ws_a = await connect_to_server()
                    ws_b = await connect_to_server()
                    try:
                        # A busy on s_iso1
                        await ws_a.send(json.dumps(
                            self._task("s_iso1", "t-iso-a", "iso1 long", cwd), ensure_ascii=False))
                        await asyncio.sleep(0.2)
                        # B on s_iso2 → NOT busy → immediate streaming, no task_queued
                        await ws_b.send(json.dumps(
                            self._task("s_iso2", "t-iso-b2", "iso2 fast", cwd), ensure_ascii=False))
                        got_done = False
                        saw_queued = False
                        while not got_done:
                            fb = json.loads(await asyncio.wait_for(ws_b.recv(), timeout=10))
                            if fb.get("type") == "task_queued":
                                saw_queued = True
                            if fb.get("done"):
                                got_done = True
                        assert not saw_queued, "different session must not be queued"
                        assert got_done
                        # B on s_iso1 (busy) → queued
                        await ws_b.send(json.dumps(
                            self._task("s_iso1", "t-iso-b1", "iso1 queued", cwd), ensure_ascii=False))
                        fq = await _recv_until(
                            ws_b, lambda f: f.get("type") == "task_queued", what="task_queued")
                        assert fq.get("session_id") == "s_iso1"
                    finally:
                        await ws_a.close()
                        await ws_b.close()
                finally:
                    await cleanup()
        asyncio.run(_test())
