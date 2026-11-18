"""A tool row's duration is a measurement the daemon takes, and both carriers carry it.

Rant 2026-11-18T18:27:04 (issue #2980). The host reported that the conversation area
shows the wrong numbers: every tool row reads "· 0.0s" and every collapsed group reads
"{count} 个工具执行 · 0.0s", however long the tools actually took.

The cause was not a rendering bug. `ToolEndData.elapsed` existed on the client, the
transcript store already recorded it (`if (ok && data.elapsed !== undefined)`), the
group summary already summed it, and the client tests already passed a value in — but
**no producer ever sent one**: the daemon's `tool_end` frame had no such field, and the
`tool_result` record it persists had none either, so history replay (which reads the
record, not the frame) could not have one. An optional field read by a consumer with no
producer writes nothing and fails silently, which is why every test stayed green.

So the subjects here are the two things a client actually receives, and only those:

* **the live frame**, read off the wire. `_run_tool_loop` is driven with a tool that
  really sleeps, so the assertion is a measurement and not a shape: a hard-coded `0.0`
  passes any "the key exists" test and fails this one.
* **the persisted record**, read back through `_read_history` — the same reader the
  replay path uses, so a run that writes the frame and forgets the record fails here.
* **the history projection**, read through the real `list_history` handler with
  `include_records`, because that payload is what a reopening client replays.
* **the per-call clock.** Two calls in one response are timed separately; one clock for
  the whole response would give both rows the same number and look plausible.
* **the absence.** A record written before the daemon began measuring carries no
  duration and must project exactly as it did — no `elapsed: null` on old sessions.

No daemon is started, stopped or restarted: `_run_tool_loop` and `_process_message` are
called directly, with the broadcast and the websocket stubbed, the way
`tests/test_tool_call_batch_label.py` does.
"""

from __future__ import annotations

import asyncio
import json

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.server.tool_types import ToolResult
from emrg.session import Session

# Long enough that a hard-coded 0 cannot pass, short enough to keep the suite fast.
SLOW = 0.08


class _TimedTool:
    """A tool whose duration is real, and whose per-call duration is knowable.

    Naming one of the real tools would drag the sandbox, the platform's shell dialect
    and an actual subprocess into a test about a stopwatch.
    """

    def __init__(self, delays: list[float] | None = None) -> None:
        self.delays = delays or [SLOW]
        self.calls: list[dict] = []

    async def execute(self, args):
        index = len(self.calls)
        self.calls.append(dict(args))
        await asyncio.sleep(self.delays[min(index, len(self.delays) - 1)])
        return ToolResult(content="ok")


class _FakeWs:
    """A subscriber that records the frames the daemon sends it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))


def _make_server(tmp_path) -> EmrgServer:
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = tmp_path / "agents.yml"
    return server


def _drive(tmp_path, monkeypatch, calls: int = 1, tool=None):
    """Run one tool loop whose first round emits `calls` tool calls.

    Returns the server, the session, the stub tool and the frames, so a test can ask
    each carrier the same run produced.
    """
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH",
                        tmp_path / "planted-fire-heartbeat")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH",
                        tmp_path / "planted-fire-round-complete")
    server = _make_server(tmp_path)
    session = Session.create_with_id(f"elapsed-{calls}", tmp_path)
    stub = tool or _TimedTool()
    server.tools.get = lambda name: stub  # type: ignore[method-assign]

    state = {"round": 0}

    async def fake_stream(messages, tools=None):
        state["round"] += 1
        if state["round"] == 1:
            yield {"content": None, "tool_calls": [
                {
                    "index": index,
                    "id": f"call_{calls}_{index}",
                    "function": {"name": "read", "arguments": '{"intent": "x"}'},
                }
                for index in range(calls)
            ], "finish_reason": "tool_calls", "usage": None}
        else:
            yield {"content": "done", "tool_calls": None, "finish_reason": "stop",
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    frames: list[dict] = []

    async def fake_broadcast(session_id, payload):
        frames.append(payload)

    server.llm.chat_stream = fake_stream
    server._broadcast = fake_broadcast
    req = TaskRequest(id="req-elapsed", session_id=session.session_id, prompt="go")
    asyncio.run(server._run_tool_loop(req, None, session))
    return server, session, stub, frames


def _end_frames(frames: list[dict]) -> list[dict]:
    ends = [f for f in frames if f.get("type") == "tool_end"]
    assert ends, f"no tool_end frame was broadcast: {frames}"
    return ends


# ── the live frame ─────────────────────────────────────────────────────────


def test_the_end_frame_carries_the_measured_duration(tmp_path, monkeypatch):
    """The number on the row is a stopwatch reading, not a constant."""
    _server, _session, tool, frames = _drive(tmp_path, monkeypatch)

    assert tool.calls, "the stub never ran — the duration would be measuring nothing"
    elapsed = _end_frames(frames)[0].get("elapsed")
    assert isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool), (
        f"tool_end carries elapsed={elapsed!r}"
    )
    assert elapsed >= SLOW * 0.75, (
        f"tool_end reports {elapsed}s for a tool that slept {SLOW}s — the field is filled "
        "with something other than a measurement"
    )
    assert elapsed < 5.0, f"tool_end reports {elapsed}s for a tool that slept {SLOW}s"


def test_two_calls_of_one_response_are_timed_separately(tmp_path, monkeypatch):
    """One clock for the whole response would hand both rows the same number."""
    slow = _TimedTool(delays=[0.02, 0.20])
    _server, _session, tool, frames = _drive(tmp_path, monkeypatch, calls=2, tool=slow)

    assert len(tool.calls) == 2, f"expected two executed calls, got {len(tool.calls)}"
    first, second = _end_frames(frames)
    assert second["elapsed"] > first["elapsed"] + 0.1, (
        "the two calls report "
        f"{first['elapsed']}s and {second['elapsed']}s for 0.02s and 0.20s of work"
    )


# ── the persisted record ───────────────────────────────────────────────────


def test_the_record_carries_the_same_duration_the_frame_carried(tmp_path, monkeypatch):
    """One measurement, two carriers — not two readings that can drift apart."""
    _server, session, _tool, frames = _drive(tmp_path, monkeypatch)

    records = [r for r in session._read_history() if r.get("type") == "tool_result"]
    assert len(records) == 1, records
    assert records[0].get("elapsed") == _end_frames(frames)[0]["elapsed"], (
        "the record and the frame disagree about how long the call took: "
        f"{records[0].get('elapsed')!r} vs {_end_frames(frames)[0]['elapsed']!r}"
    )


# ── the history projection a reopening client replays ───────────────────────


def _history_messages(server: EmrgServer, session: Session, cwd) -> list[dict]:
    ws = _FakeWs()
    asyncio.run(server._process_message(
        {"type": "list_history", "session_id": session.session_id,
         "cwd": str(cwd), "include_records": True},
        ws,
    ))
    frames = [f for f in ws.sent if f.get("type") == "history_list"]
    assert frames, f"no history_list frame was sent: {ws.sent}"
    return frames[0]["messages"]


def test_the_history_projection_carries_the_duration(tmp_path, monkeypatch):
    """Replay reads the record; a duration only on the frame never reaches it."""
    server, session, _tool, frames = _drive(tmp_path, monkeypatch)
    measured = _end_frames(frames)[0]["elapsed"]

    projected = [m for m in _history_messages(server, session, tmp_path)
                 if m.get("kind") == "tool_result"]
    assert len(projected) == 1, projected
    assert projected[0].get("elapsed") == measured, (
        "the history projection dropped the recorded duration, so a reopened session "
        f"replays 0.0s: {projected[0]!r}"
    )


def test_a_record_from_before_the_daemon_measured_projects_without_the_key(
    tmp_path, monkeypatch,
):
    """An old session must replay exactly as it did — not with `elapsed: null`."""
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH",
                        tmp_path / "planted-fire-heartbeat")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH",
                        tmp_path / "planted-fire-round-complete")
    server = _make_server(tmp_path)
    session = Session.create_with_id("elapsed-legacy", tmp_path)
    session.append_message({
        "type": "tool_result",
        "tool_name": "bash",
        "tool_call_id": "call_legacy",
        "content": "ok",
        "error": False,
    })

    projected = [m for m in _history_messages(server, session, tmp_path)
                 if m.get("kind") == "tool_result"]
    assert len(projected) == 1, projected
    assert "elapsed" not in projected[0], (
        f"a record with no measured duration projected one: {projected[0]!r}"
    )
