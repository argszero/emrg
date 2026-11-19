"""One `turn_progress` frame per round, carrying the round number and nothing else.

Issue #2959 rebuilt the GUI into an agent cockpit, and the cockpit's status band reports
which round of the turn is running. `round_num` had lived only inside the daemon's tool
loop before that, so no client could see it, and the frame is what makes it observable.

The number is the daemon's and not a client's, which is the whole point of the frame: a
client that counted rounds would answer with its own arithmetic the moment a stream
restarts or a second window watches the same turn.

**The spend is no longer here** (rant 2026-11-19T09:31:00). This frame also carried the
turn's cumulative tokens; that figure reset every time a session was switched, a window
closed or the GUI restarted, so it answered "what has this turn spent" while the host was
asking "what have I spent today". The window — last 24 hours, three segments, read from
the daemon's own ledger — is answered by the `usage_window` request instead, and its
tests are in `test_usage_ledger.py`. What is left to pin here is the round number and the
addresses a window with several sessions open needs to route by.

No daemon is started, stopped or restarted: `_run_tool_loop` is called directly, with
the broadcast stubbed, in the shape `test_tool_call_batch_label.py` established.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.server.tool_types import ToolResult
from emrg.session import Session


class _StubTool:
    """A tool that executes nothing and reports success, so a round can be driven."""

    async def execute(self, args):
        return ToolResult(content="ok")


def _make_server(tmp_path: Path) -> EmrgServer:
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = tmp_path / "agents.yml"
    return server


def _drive(tmp_path: Path, monkeypatch) -> list[dict]:
    """Run one tool loop whose first two rounds emit a tool call, returning its frames.

    The third round emits no tool call, which is what ends the loop. Usage is reported on
    every round because a real provider does — and because a frame must not depend on it.
    """
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH",
                        tmp_path / "planted-fire-heartbeat")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH",
                        tmp_path / "planted-fire-round-complete")

    server = _make_server(tmp_path)
    session = Session.create_with_id("turn-progress", tmp_path)
    stub = _StubTool()
    server.tools.get = lambda name: stub  # type: ignore[method-assign]

    state = {"round": 0}

    async def fake_stream(messages, tools=None):
        index = state["round"]
        state["round"] += 1
        usage = {"prompt_tokens": 10 * index, "completion_tokens": 5}
        if index == 2:
            yield {"content": "done", "tool_calls": None,
                   "finish_reason": "stop", "usage": usage}
        else:
            yield {"content": None, "tool_calls": [
                {
                    "index": 0,
                    "id": f"call_{index}",
                    "function": {"name": "read", "arguments": '{"intent": "x"}'},
                }
            ], "finish_reason": "tool_calls", "usage": usage}

    frames: list[dict] = []

    async def fake_broadcast(session_id, payload):
        frames.append(payload)

    server.llm.chat_stream = fake_stream
    server._broadcast = fake_broadcast
    req = TaskRequest(id="req-progress", session_id=session.session_id, prompt="go")
    asyncio.run(server._run_tool_loop(req, None, session))
    return [f for f in frames if f.get("type") == "turn_progress"]


def test_one_frame_per_round_with_the_round_number(tmp_path, monkeypatch):
    """Three rounds → three frames, numbered 1, 2, 3 in the order they were sent."""
    frames = _drive(tmp_path, monkeypatch)
    assert [f["round"] for f in frames] == [1, 2, 3]


def test_the_frame_carries_no_spend_reading(tmp_path, monkeypatch):
    """The turn's token total is gone (rant 2026-11-19T09:31:00): no frame carries one.

    This is the deletion's guard. A frame that kept `tokens` — even always at 0 — would
    leave the client two sources for the same word, and the per-turn one is the source the
    host complained about.
    """
    frames = _drive(tmp_path, monkeypatch)
    assert frames
    assert all("tokens" not in f for f in frames)


def test_the_frame_names_the_session_and_the_request(tmp_path, monkeypatch):
    """A window with several sessions open can address the frame it was given."""
    frames = _drive(tmp_path, monkeypatch)
    assert frames
    assert frames[0]["session_id"] == "turn-progress"
    assert frames[0]["request_id"] == "req-progress"
