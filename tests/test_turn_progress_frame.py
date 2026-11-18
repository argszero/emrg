"""One `turn_progress` frame per round: the round number and the turn's token total.

Issue #2959 rebuilt the GUI into an agent cockpit, and the cockpit's status band
reports two numbers the contract names — which round of the turn is running, and how
many tokens the turn has spent. Both were previously unobservable from any client:
`round_num` lived only inside the daemon's tool loop, and the usage anchors were
server-internal bookkeeping for auto-compaction.

The reason the numbers are the daemon's and not a client's is the whole point of the
frame: a client that counted rounds would answer with its own arithmetic the moment a
stream restarts or a second window watches the same turn, and a client that added up
usage would be a second ledger that drifts from the one the provider bills. So the
subjects here are:

* **the live frame, read off the wire.** `_run_tool_loop` is driven with a stubbed LLM
  that reports usage on every round, and the `turn_progress` broadcasts are read in
  order. A counter computed but never attached to a frame fails here, which a unit test
  of `_counted_tokens` alone would have passed.
* **the ordering the band depends on.** The frame is sent at the round's *start*, so a
  client watching a slow round sees that it is on round 4 rather than on round 3 — the
  round now beginning has no usage yet, so its frame carries what the finished rounds
  spent.
* **the count itself, in both directions.** A provider that reports only one of the two
  fields is still a measurement; a missing field, a string, a bool or a negative number
  is not, and none of them may raise.

No daemon is started, stopped or restarted: `_run_tool_loop` is called directly, with
the broadcast stubbed, in the shape `test_tool_call_batch_label.py` established.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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


def _drive(tmp_path: Path, monkeypatch, usages: list[dict | None]) -> list[dict]:
    """Run one tool loop whose first rounds emit a tool call, returning every frame.

    `usages[i]` is the usage the provider reports on round `i`. The last round emits
    no tool call, which is what ends the loop.
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
        usage = usages[index] if index < len(usages) else None
        last = index == len(usages) - 1
        if last:
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
    frames = _drive(tmp_path, monkeypatch, [
        {"prompt_tokens": 10, "completion_tokens": 5},
        {"prompt_tokens": 20, "completion_tokens": 5},
        {"prompt_tokens": 30, "completion_tokens": 5},
    ])
    assert [f["round"] for f in frames] == [1, 2, 3]


def test_the_total_is_the_rounds_already_finished(tmp_path, monkeypatch):
    """Round 1 reports 0 spent; each later frame carries the finished rounds' total."""
    frames = _drive(tmp_path, monkeypatch, [
        {"prompt_tokens": 10, "completion_tokens": 5},
        {"prompt_tokens": 20, "completion_tokens": 3},
        {"prompt_tokens": 30, "completion_tokens": 1},
    ])
    assert [f["tokens"] for f in frames] == [0, 15, 38]


def test_a_round_with_no_usage_adds_nothing_and_does_not_raise(tmp_path, monkeypatch):
    """A provider that reports no usage on one round is a 0, not an exception."""
    frames = _drive(tmp_path, monkeypatch, [
        None,
        {"prompt_tokens": 7, "completion_tokens": 3},
    ])
    assert [f["tokens"] for f in frames] == [0, 0]


def test_the_frame_names_the_session_and_the_request(tmp_path, monkeypatch):
    """A window with several sessions open can address the frame it was given."""
    frames = _drive(tmp_path, monkeypatch, [{"prompt_tokens": 1, "completion_tokens": 1}])
    assert frames
    assert frames[0]["session_id"] == "turn-progress"
    assert frames[0]["request_id"] == "req-progress"


@pytest.mark.parametrize(
    "usage,expected",
    [
        (None, 0),
        ({}, 0),
        ({"prompt_tokens": 10}, 10),
        ({"completion_tokens": 4}, 4),
        ({"prompt_tokens": 10, "completion_tokens": 4}, 14),
        # 非数字与负数是「那次读数不可用」，不是 0 也算不进总数
        ({"prompt_tokens": "10", "completion_tokens": 4}, 4),
        ({"prompt_tokens": -5, "completion_tokens": 4}, 4),
        ({"prompt_tokens": True, "completion_tokens": 4}, 4),
    ],
)
def test_counted_tokens_reads_what_the_provider_reported(usage, expected):
    """One field is a measurement; a wrong type or a negative number is not."""
    assert EmrgServer._counted_tokens(usage) == expected
