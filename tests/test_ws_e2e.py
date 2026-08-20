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
    import emrg.server.scheduler as sched_mod
    import emrg.connect as connect_mod

    _orig_daemon_cfg = daemon_mod.config_dir
    _orig_sched_cfg = sched_mod.config_dir
    _orig_connect_cfg = connect_mod.config_dir

    # Fixed-port admission (rant 2026-08-19T08:05:21): the daemon now binds a
    # fixed port, but tests must never fight a real daemon (or each other) on
    # the well-known EMRGD_PORT — point BOTH the daemon's serve() and the
    # connect layer at a free loopback port for the duration of the test.
    import socket as _socket

    _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _probe.bind(("127.0.0.1", 0))
    _test_port = _probe.getsockname()[1]
    _probe.close()
    _orig_daemon_port = daemon_mod.EMRGD_PORT
    _orig_connect_port = connect_mod.EMRGD_PORT
    daemon_mod.EMRGD_PORT = _test_port
    connect_mod.EMRGD_PORT = _test_port

    # Isolate config dir to tmp (port file, tasks.yml, projects.yml, etc.)
    daemon_mod.config_dir = lambda: tmp
    sched_mod.config_dir = lambda: tmp  # scheduler builds its own projects_file (#738)
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
    # Wait for the token file (daemon ready)
    for _ in range(200):
        if (tmp / "emrgd.token").exists():
            break
        await asyncio.sleep(0.05)
    assert (tmp / "emrgd.token").exists(), "daemon did not publish token file"

    async def _cleanup():
        server._server.close()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        daemon_mod.config_dir = _orig_daemon_cfg
        sched_mod.config_dir = _orig_sched_cfg
        connect_mod.config_dir = _orig_connect_cfg
        daemon_mod.EMRGD_PORT = _orig_daemon_port
        connect_mod.EMRGD_PORT = _orig_connect_port

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


class TestWSVibeCheck:
    """task_vibe_check — one-shot structured LLM ask (rant 2026-08-17T11:39:19).

    The scheduler replaces its git-HEAD empty-cycle heuristic with an agent
    answer: the daemon runs a single Ask-mode LLM call (no tools, no history)
    and returns a strict-JSON {work, recommend_slowdown, slowdown_reason}
    result (fields unified rant 2026-08-20T10:58:55, meaningful deleted).
    """

    def test_vibe_check_returns_structured_result(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                server, _, cleanup = await _boot_server(Path(tmp))
                try:
                    async def fake_chat(messages, tools=None):
                        # echo back a strict-JSON answer; fenced JSON tolerated
                        return {"content": '```json\n{"work": "分析了双实例根因，提交 PR #854", "recommend_slowdown": true, "slowdown_reason": "长期无产出"}\n```'}
                    server.llm.chat = fake_chat

                    ws = await connect_to_server()
                    try:
                        await ws.send(json.dumps({
                            "type": "task_vibe_check",
                            "session_id": "s-vibe",
                            "task_name": "emrg-task",
                            "prompt": "run the evolution cycle",
                            "completion_summary": "nothing to evolve",
                        }, ensure_ascii=False))
                        frame = await asyncio.wait_for(ws.recv(), timeout=10)
                        data = json.loads(frame)
                        assert data.get("type") == "vibe_check_result"
                        assert data.get("ok") is True
                        result = data.get("result", {})
                        # rant 2026-08-20T10:58:55: unified 3-field shape
                        assert result.get("work") == "分析了双实例根因，提交 PR #854"
                        assert result.get("recommend_slowdown") is True
                        assert result.get("slowdown_reason") == "长期无产出"
                        # the ask must carry the fixed system prompt + no tools
                        sent = server.llm.chat
                        assert sent is fake_chat
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_vibe_check_uses_session_history(self):
        """Rant 2026-08-19T10:15:43 (host-finalized): the PRIMARY evidence is
        the task's OWN session history — the daemon loads it by the fixed
        session_id + cwd and passes the recent session messages to the LLM,
        not just the transported prompt/completion_summary."""

    def test_vibe_check_uses_session_history(self):
        """Rant 2026-08-19T10:15:43 (host-finalized): the PRIMARY evidence is
        the task's OWN session history — the daemon loads it by the fixed
        session_id + cwd and passes the recent session messages to the LLM,
        not just the transported prompt/completion_summary."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                # Pre-create the task's session history on disk (session files
                # are organized by cwd: <cwd>/.emrg/sessions/<session_id>/).
                sess_dir = tmp / ".emrg" / "sessions" / "emrg-evolution-deepseek-harness-opensource-task"
                sess_dir.mkdir(parents=True, exist_ok=True)
                (sess_dir / "history.jsonl").write_text(
                    json.dumps({"type": "message", "role": "user",
                                "content": "run the deepseek-harness cycle"}) + "\n" +
                    json.dumps({"type": "message", "role": "assistant",
                                "content": "fetched upstream, analyzed PR, wrote memory"}) + "\n",
                    encoding="utf-8",
                )

                server, _, cleanup = await _boot_server(tmp)
                try:
                    seen = {}

                    async def fake_chat(messages, tools=None):
                        seen["messages"] = messages
                        return {"content": '{"work": "fetch 上游 + 分析 PR + 写 memory", "recommend_slowdown": false, "slowdown_reason": ""}'
                                }
                    server.llm.chat = fake_chat

                    ws = await connect_to_server()
                    try:
                        await ws.send(json.dumps({
                            "type": "task_vibe_check",
                            "session_id": "emrg-evolution-deepseek-harness-opensource-task",
                            "cwd": str(tmp),
                            "task_name": "deepseek-harness-opensource-task",
                            "prompt": "run cycle",
                            "completion_summary": "auxiliary",
                        }, ensure_ascii=False))
                        frame = await asyncio.wait_for(ws.recv(), timeout=10)
                        data = json.loads(frame)
                        assert data.get("ok") is True
                        result = data.get("result", {})
                        assert result.get("work") == "fetch 上游 + 分析 PR + 写 memory"
                        # The LLM must have received the session history
                        # messages (primary evidence), not just the summary.
                        msgs = seen.get("messages", [])
                        contents = [str(m.get("content", "")) for m in msgs]
                        assert any("fetched upstream, analyzed PR" in c for c in contents), contents
                        assert any("run the deepseek-harness cycle" in c for c in contents), contents
                        # system prompt first, user ask last
                        assert msgs[0]["role"] == "system"
                        assert msgs[-1]["role"] == "user"
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_vibe_check_long_session_window_leading_tool_stripped(self):
        """Rant 2026-08-19T19:25:56 (root cause): slicing a validated session
        history to the last 100 messages can orphan a leading role:'tool'
        message whose matching assistant(tool_calls) lies before the window →
        the LLM rejects the request with 400 "tool must follow tool_calls"
        (observed on every task_vibe_check for long sessions). The daemon
        must strip window-boundary orphans before calling the LLM."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                sess_dir = tmp / ".emrg" / "sessions" / "emrg-evolution-emrg-task"
                sess_dir.mkdir(parents=True, exist_ok=True)
                lines = []
                # 60 user → assistant(tool_calls) → tool_result triples:
                # 180 llm messages → [-100:] starts at an index ≡ 2 (mod 3) = tool.
                for i in range(60):
                    lines.append(json.dumps({
                        "type": "message", "role": "user", "content": f"cycle {i}",
                    }, ensure_ascii=False))
                    lines.append(json.dumps({
                        "type": "message", "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": f"x{i}", "type": "function",
                            "function": {"name": "bash", "arguments": "{}"},
                        }],
                    }, ensure_ascii=False))
                    lines.append(json.dumps({
                        "type": "tool_result", "tool_call_id": f"x{i}",
                        "content": "ok",
                    }, ensure_ascii=False))
                (sess_dir / "history.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

                server, _, cleanup = await _boot_server(tmp)
                try:
                    seen = {}

                    async def fake_chat(messages, tools=None):
                        seen["messages"] = messages
                        return {"content": '{"work": "", "recommend_slowdown": false, "slowdown_reason": "nt"}'}
                    server.llm.chat = fake_chat

                    ws = await connect_to_server()
                    try:
                        await ws.send(json.dumps({
                            "type": "task_vibe_check",
                            "session_id": "emrg-evolution-emrg-task",
                            "cwd": str(tmp),
                            "task_name": "emrg-task",
                            "prompt": "run cycle",
                            "completion_summary": "aux",
                        }, ensure_ascii=False))
                        frame = await asyncio.wait_for(ws.recv(), timeout=10)
                        data = json.loads(frame)
                        assert data.get("ok") is True, data
                        msgs = seen.get("messages", [])
                        assert msgs[0]["role"] == "system"
                        assert msgs[-1]["role"] == "user"
                        # no leading tool orphan after system; history portion
                        # must start with a non-tool role
                        first_hist = next(m for m in msgs[1:] if m["role"] != "system")
                        assert first_hist["role"] != "tool", [m["role"] for m in msgs[:6]]
                        assert len(msgs) <= 2 + 100, len(msgs)
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_vibe_check_missing_fields_is_compatible(self):
        """Old models / old parsing omit work/slowdown_reason → empty, no crash."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                server, _, cleanup = await _boot_server(Path(tmp))
                try:
                    async def fake_chat(messages, tools=None):
                        return {"content": '{"recommend_slowdown": false}'}
                    server.llm.chat = fake_chat

                    ws = await connect_to_server()
                    try:
                        await ws.send(json.dumps({
                            "type": "task_vibe_check",
                            "session_id": "s-vibe",
                            "task_name": "t",
                            "prompt": "p",
                            "completion_summary": "c",
                        }))
                        frame = await asyncio.wait_for(ws.recv(), timeout=10)
                        data = json.loads(frame)
                        assert data.get("type") == "vibe_check_result"
                        assert data.get("ok") is True
                        result = data.get("result", {})
                        assert result.get("work") == ""
                        assert result.get("recommend_slowdown") is False
                        assert result.get("slowdown_reason") == ""
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_vibe_check_bad_llm_answer_returns_ok_false(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                server, _, cleanup = await _boot_server(Path(tmp))
                try:
                    async def fake_chat(messages, tools=None):
                        return {"content": "not json at all"}
                    server.llm.chat = fake_chat

                    ws = await connect_to_server()
                    try:
                        await ws.send(json.dumps({
                            "type": "task_vibe_check",
                            "session_id": "s-vibe",
                            "task_name": "t",
                            "prompt": "p",
                            "completion_summary": "c",
                        }))
                        frame = await asyncio.wait_for(ws.recv(), timeout=10)
                        data = json.loads(frame)
                        assert data.get("type") == "vibe_check_result"
                        assert data.get("ok") is False
                        assert "error" in data
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_vibe_check_llm_raises_returns_ok_false(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                server, _, cleanup = await _boot_server(Path(tmp))
                try:
                    async def fake_chat(messages, tools=None):
                        raise RuntimeError("llm down")
                    server.llm.chat = fake_chat

                    ws = await connect_to_server()
                    try:
                        await ws.send(json.dumps({
                            "type": "task_vibe_check",
                            "session_id": "s-vibe",
                            "task_name": "t",
                            "prompt": "p",
                            "completion_summary": "c",
                        }))
                        frame = await asyncio.wait_for(ws.recv(), timeout=10)
                        data = json.loads(frame)
                        assert data.get("type") == "vibe_check_result"
                        assert data.get("ok") is False
                        assert "llm down" in data.get("error", "")
                    finally:
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
                        context_messages = None
                        while True:
                            frame = await asyncio.wait_for(ws.recv(), timeout=10)
                            resp = json.loads(frame)
                            if resp.get("delta") or "content" in resp:
                                got_delta = True
                            if "tool_name" in resp:
                                got_tool = True
                            if resp.get("done"):
                                got_done = True
                                # rant 21:52:18: done frame must report the
                                # current LLM context size — system(1) + user(1)
                                # + assistant(tool_calls)(1) + tool result(1) +
                                # final assistant(1) = 5 for this one-round flow.
                                context_messages = resp.get("context_messages")
                                break
                        assert got_delta and got_tool and got_done
                        assert context_messages == 5
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_tool_intent_logged_and_ignored_by_execution(self):
        """Rant 2026-08-19T10:35:24: the agent's per-call `intent` is carried
        in the tool_start broadcast and logged, but NOT passed to the tool
        executor — the tool runs on its real arguments only."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    seen_args = {}
                    # One chat_stream call per tool-loop round: round 1 emits
                    # the tool call (finish "tool_calls"), round 2 the final
                    # answer (finish "stop"). A "stop" delta in the same
                    # stream as the tool call would make the daemon's Case-1
                    # branch treat the round as a final text answer and drop
                    # the tool calls (established pattern in
                    # _make_fake_chat_stream).
                    state = {"round": 0}

                    async def fake_chat_stream(messages, tools=None):
                        state["round"] += 1
                        if state["round"] == 1:
                            yield {"content": "先跑命令", "tool_calls": None, "finish_reason": None, "usage": None}
                            yield {
                                "content": None,
                                "tool_calls": [{
                                    "index": 0, "id": "call_i1",
                                    "function": {"name": "bash",
                                                 "arguments": '{"command": "echo intent-ok", "intent": "验证 intent 日志"}',
                                                 },
                                }],
                                "finish_reason": "tool_calls", "usage": None,
                            }
                        else:
                            yield {"content": "完成", "tool_calls": None, "finish_reason": "stop",
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

                    server.llm.chat_stream = fake_chat_stream
                    # Capture what the real BashTool executor receives.
                    orig_bash_execute = server.tools.get("bash").execute

                    async def spy_execute(arguments):
                        seen_args["executed"] = dict(arguments)
                        return await orig_bash_execute(arguments)

                    server.tools.get("bash").execute = spy_execute

                    ws = await connect_to_server()
                    try:
                        task = {
                            "type": "task",
                            "id": "t-intent",
                            "session_id": "s_e2e_intent",
                            "cwd": str(cwd),
                            "prompt": "run a command",
                            "stream": True,
                            "timestamp": "2026-08-19T00:00:00",
                        }
                        await ws.send(json.dumps(task, ensure_ascii=False))
                        got_start_intent = None
                        while True:
                            frame = await asyncio.wait_for(ws.recv(), timeout=10)
                            resp = json.loads(frame)
                            if resp.get("type") == "tool_start":
                                got_start_intent = resp.get("intent")
                            if resp.get("done"):
                                break
                        # broadcast carries the intent
                        assert got_start_intent == "验证 intent 日志"
                        # the executor received intent but must still run (bash
                        # ignores unknown keys — command executes fine)
                        assert "intent" in seen_args.get("executed", {})
                        assert seen_args["executed"]["command"] == "echo intent-ok"
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

                        def definition(self):
                            from emrg.server.tool_types import ToolDefinition
                            return ToolDefinition(name="bash")

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

                        def definition(self):
                            from emrg.server.tool_types import ToolDefinition
                            return ToolDefinition(name="bash")

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


class TestWSWorkspacePanel:
    """GUI right-panel workspace commands (rant 2026-08-11T12:20:35 P1.1).

    list_files → files_list (sorted, truncated, absolute-path-only,
    symlinks not followed); read_file → file_content (paging, binary
    detection, 1MB cap).
    """

    @staticmethod
    async def _cmd(ws, payload):
        await ws.send(json.dumps(payload))
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=5))

    def test_list_files_sorted_dirs_first(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                work = cwd / "work"
                work.mkdir()
                (work / "zdir").mkdir()
                (work / "afile.txt").write_text("x", encoding="utf-8")
                (work / "adir").mkdir()
                (work / "bfile.py").write_text("y", encoding="utf-8")
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {"type": "list_files", "path": str(work)})
                        assert resp["type"] == "files_list"
                        assert resp.get("error") is None
                        # 目录在前（adir、zdir），文件在后（afile.txt、bfile.py）
                        names = [e["name"] for e in resp["entries"]]
                        types = [e["type"] for e in resp["entries"]]
                        assert types == ["dir", "dir", "file", "file"], types
                        assert names == ["adir", "zdir", "afile.txt", "bfile.py"], names
                        # 字段只含 name/type/path（无 size/mtime）
                        assert set(resp["entries"][0].keys()) == {"name", "path", "type"}
                        assert resp["truncated"] is False
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_list_files_relative_path_rejected(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {"type": "list_files", "path": "."})
                        assert resp["type"] == "files_list"
                        assert "error" in resp
                        assert "absolute" in resp["error"]
                        resp2 = await self._cmd(ws, {"type": "list_files", "path": str(cwd / "nope")})
                        assert "error" in resp2
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_list_files_symlink_not_followed(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                work = cwd / "work"
                work.mkdir()
                (work / "real").mkdir()
                try:
                    (work / "link").symlink_to(work / "real", target_is_directory=True)
                except (OSError, NotImplementedError):
                    return  # 平台不支持符号链接 → 跳过
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {"type": "list_files", "path": str(work)})
                        by_name = {e["name"]: e for e in resp["entries"]}
                        assert "real" in by_name and by_name["real"]["type"] == "dir"
                        assert "link" in by_name and by_name["link"]["type"] == "file", "symlink must not be expandable"
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_list_files_truncated_at_5000(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                work = cwd / "work"
                work.mkdir()
                for i in range(5100):
                    (work / f"f{i:05d}.txt").write_text("x", encoding="utf-8")
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {"type": "list_files", "path": str(work)})
                        assert resp["type"] == "files_list"
                        assert len(resp["entries"]) == 5000
                        assert resp["truncated"] is True
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_read_file_text_and_paging(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                work = cwd / "work"
                work.mkdir()
                f = work / "doc.txt"
                f.write_text("\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8")
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        # 全文（无分页参数）
                        resp = await self._cmd(ws, {"type": "read_file", "path": str(f)})
                        assert resp["type"] == "file_content"
                        assert resp["content"].split("\n")[0] == "line1"
                        assert resp["content"].split("\n")[-1] == "line10"
                        assert resp["truncated"] is False
                        assert resp["total_lines"] == 10
                        # 分页 start_line=3, line_limit=2
                        resp2 = await self._cmd(ws, {
                            "type": "read_file", "path": str(f),
                            "start_line": 3, "line_limit": 2,
                        })
                        assert resp2["content"].split("\n") == ["line3", "line4"]
                        assert resp2["truncated"] is True
                        # 相对路径拒绝
                        resp3 = await self._cmd(ws, {"type": "read_file", "path": "doc.txt"})
                        assert "error" in resp3
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_read_file_binary_and_large(self):
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                work = cwd / "work"
                work.mkdir()
                bin_f = work / "img.png"
                bin_f.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01binary")
                big_f = work / "big.bin"
                big_f.write_bytes(b"0" * (1024 * 1024 + 1))
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {"type": "read_file", "path": str(bin_f)})
                        assert resp["type"] == "file_content"
                        assert resp.get("binary") is True
                        assert not resp.get("content")
                        resp2 = await self._cmd(ws, {"type": "read_file", "path": str(big_f)})
                        assert "error" in resp2
                        assert "系统工具" in resp2["error"]
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())


class TestWSHistoryPagination:
    """list_history pagination (rant 2026-08-13T14:15:12).

    limit/offset count from the NEWEST message backwards (offset=0 = latest);
    absent limit keeps the full list (backward compatible, used by /rewind);
    response includes has_more.
    """

    @staticmethod
    async def _cmd(ws, payload):
        await ws.send(json.dumps(payload))
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=5))

    @staticmethod
    def _seed_history(cwd: Path, sid: str, n: int) -> None:
        from emrg.session import Session
        sess = Session(sid, cwd)
        records = []
        for i in range(n):
            records.append({
                "type": "message",
                "role": "user",
                "content": f"msg-{i:02d}",
                "timestamp": f"2026-08-13T{i:02d}:00:00",
            })
        sess._write_history(records)

    def test_full_list_without_limit(self):
        """Absent limit → full list (backward compatible)."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                sid = "s_hist"
                self._seed_history(cwd, sid, 3)
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {
                            "type": "list_history", "session_id": sid, "cwd": str(cwd),
                        })
                        assert resp["type"] == "history_list"
                        msgs = resp["messages"]
                        assert [m["content"] for m in msgs] == ["msg-00", "msg-01", "msg-02"]
                        # has_more present (False for full list), no error
                        assert resp.get("has_more") is False
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_limit_returns_newest(self):
        """limit=2 → newest 2 messages in time order."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                sid = "s_hist"
                self._seed_history(cwd, sid, 5)
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {
                            "type": "list_history", "session_id": sid,
                            "cwd": str(cwd), "limit": 2,
                        })
                        msgs = resp["messages"]
                        assert [m["content"] for m in msgs] == ["msg-03", "msg-04"]
                        assert resp.get("has_more") is True
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_offset_pages_older(self):
        """limit=2 offset=2 → the 2 messages before the newest 2."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                sid = "s_hist"
                self._seed_history(cwd, sid, 5)
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {
                            "type": "list_history", "session_id": sid,
                            "cwd": str(cwd), "limit": 2, "offset": 2,
                        })
                        msgs = resp["messages"]
                        assert [m["content"] for m in msgs] == ["msg-01", "msg-02"]
                        assert resp.get("has_more") is True
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_offset_beyond_all_has_more_false(self):
        """offset beyond available messages → empty + has_more False."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                sid = "s_hist"
                self._seed_history(cwd, sid, 3)
                _, _, cleanup = await _boot_server(cwd)
                try:
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {
                            "type": "list_history", "session_id": sid,
                            "cwd": str(cwd), "limit": 10, "offset": 5,
                        })
                        assert resp["messages"] == []
                        assert resp.get("has_more") is False
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())


class TestWSTaskWire:
    """GUI task CRUD wire frames — task type field contract (rant 2026-08-14T21:48:00).

    The GUI sends the task type under `task_type` (never `type`) so it cannot
    collide with the wire message type. The daemon must read `task_type` —
    reading `msg["type"]` would swallow the wire message type after the
    sendCommand fix (`{ ...params, type }`), e.g. writing "task_update" as the
    task type.
    """

    @staticmethod
    async def _cmd(ws, payload):
        await ws.send(json.dumps(payload))
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=5))

    @staticmethod
    def _mock_scheduler(create_side_effect=None, update_side_effect=None):
        """Scheduler mock with async methods the daemon awaits (apply_tasks/wait_all)."""
        from unittest.mock import AsyncMock, Mock
        sched = Mock()
        sched.task_create = Mock(side_effect=create_side_effect or (lambda **kw: (True, {"name": "x"})))
        sched.task_update = Mock(side_effect=update_side_effect or (lambda name, **fields: (True, {"name": name})))
        sched.apply_tasks = AsyncMock(return_value="")
        sched.wait_all = AsyncMock()
        sched.stop_all = Mock()
        sched._load_tasks = Mock(return_value=[])
        return sched

    def test_task_create_reads_task_type(self):
        """task_create with task_type → scheduler receives task_type, not the wire type."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    calls = {}

                    def fake_create(name, task_type, project, interval=None, enabled=True, repo=None, description=None, sandbox=None):
                        calls.update(name=name, task_type=task_type, project=project)
                        return True, {"name": name, "type": task_type}

                    server._scheduler = self._mock_scheduler(create_side_effect=fake_create)
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {
                            "type": "task_create", "name": "daily",
                            "task_type": "evolution", "project": "emrg",
                        })
                        assert resp["type"] == "task_result"
                        assert resp.get("ok") is True
                        assert calls["task_type"] == "evolution", "daemon must read task_type"
                        assert calls["project"] == "emrg"
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_task_create_missing_task_type_not_read_from_wire_type(self):
        """Missing task_type → scheduler receives '' (never the wire type 'task_create')."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    calls = {}

                    def fake_create(name, task_type, project, interval=None, enabled=True, repo=None, description=None, sandbox=None):
                        calls.update(name=name, task_type=task_type, project=project)
                        return True, {"name": name, "type": task_type}

                    server._scheduler = self._mock_scheduler(create_side_effect=fake_create)
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {
                            "type": "task_create", "name": "daily", "project": "emrg",
                        })
                        assert resp["type"] == "task_result"
                        assert calls["task_type"] == "", "must not fall back to wire type 'task_create'"
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())

    def test_task_update_maps_task_type_to_type(self):
        """task_update with task_type → scheduler receives fields['type'] (its internal name)."""
        async def _test():
            with tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                server, _, cleanup = await _boot_server(cwd)
                try:
                    calls = {}

                    def fake_update(name, **fields):
                        calls.update(name=name, fields=fields)
                        return True, {"name": name, "type": fields.get("type", "evolution")}

                    server._scheduler = self._mock_scheduler(update_side_effect=fake_update)
                    ws = await connect_to_server()
                    try:
                        resp = await self._cmd(ws, {
                            "type": "task_update", "name": "daily",
                            "task_type": "open-source", "interval": 300,
                        })
                        assert resp["type"] == "task_result"
                        assert resp.get("ok") is True
                        assert calls["fields"]["type"] == "open-source", "task_type mapped to scheduler type field"
                        assert "task_type" not in calls["fields"]
                        assert calls["fields"]["interval"] == 300
                    finally:
                        await ws.close()
                finally:
                    await cleanup()
        asyncio.run(_test())
