"""A tool's start frame names the instant the call started.

The transcript collapses consecutive tool calls into a group whose header reads
`{count} tool calls {time}`. That time came only from calls the daemon had already
finished: a running call contributed nothing, so a long command left the header frozen at
the finished calls' sum and made one jump when it ended — from the host's side,
indistinguishable from a stuck UI.

The client cannot fix that by reading the clock when the frame arrives, because then the
number it counts up from and the number the daemon reports at the end would be two
different intervals. So the daemon says when the call started, and this file is about
**the frame**, not about a helper: `_run_tool_loop` is driven with a stubbed LLM and the
`tool_start` broadcasts are read in order. A clock read into a local variable and never
attached to the frame fails here, and that is the shape that would otherwise be papered
over on the client side.

Two properties, because either one alone is satisfiable by the wrong value:

* **the instant is on the wall clock** — within a minute of `time.time()`, and nowhere
  near `time.monotonic()`, which is the clock the daemon measures `elapsed` with and
  which another process cannot compare against its own;
* **every start frame of a batch carries one** — so the ticking header does not depend on
  which call of the batch the host happens to be watching.

The absence is asserted too, from the other side: an older daemon's frame carries no such
field, and `ToolStart.from_dict` must then answer `0.0` rather than a fabricated instant.
That zero is what tells a client to show no time instead of counting from epoch.

No daemon is started, stopped or restarted: `_run_tool_loop` is called directly with the
broadcast stubbed, and the two planted-fire heartbeat paths are redirected into `tmp_path`
so the run touches no host state.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest, ToolStart
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.server.tool_types import ToolResult
from emrg.session import Session


class _StubTool:
    """Executes nothing and reports success, so a batch can be driven.

    The subject is the start frame's clock, not any real tool: naming one would drag the
    sandbox and a subprocess into a test about a number.
    """

    async def execute(self, args):  # noqa: ANN001 - mirrors the real tool's shape
        return ToolResult(content="ok")


def _drive_tool_loop(tmp_path, monkeypatch, calls: int) -> list[dict]:
    """Run one tool loop whose first round emits `calls` calls; return every frame."""
    monkeypatch.setattr(
        daemon_mod, "_PLANTED_FIRE_MARKER_PATH", tmp_path / "planted-fire-heartbeat"
    )
    monkeypatch.setattr(
        daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH", tmp_path / "planted-fire-round-complete"
    )
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = Path(tempfile.mkdtemp()) / "agents.yml"
    session = Session.create_with_id(f"start-instant-{calls}", tmp_path)
    stub = _StubTool()
    server.tools.get = lambda name: stub  # type: ignore[method-assign]

    state = {"round": 0}

    async def fake_stream(messages, tools=None, exchange=None):  # noqa: ANN001 - the LLM seam
        state["round"] += 1
        if state["round"] == 1:
            yield {
                "content": None,
                "tool_calls": [
                    {
                        "index": index,
                        "id": f"call_{calls}_{index}",
                        "function": {"name": "read", "arguments": '{"intent": "x"}'},
                    }
                    for index in range(calls)
                ],
                "finish_reason": "tool_calls",
                "usage": None,
            }
        else:
            yield {
                "content": "done",
                "tool_calls": None,
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    frames: list[dict] = []

    async def fake_broadcast(session_id, payload):  # noqa: ANN001 - the broadcast seam
        frames.append(payload)

    server.llm.chat_stream = fake_stream
    server._broadcast = fake_broadcast
    req = TaskRequest(id="req-start", session_id=session.session_id, prompt="go")
    asyncio.run(server._run_tool_loop(req, None, session))
    return frames


def test_every_start_frame_carries_a_wall_clock_instant(tmp_path, monkeypatch):
    """One instant per call, on the wall clock, on the frame the client reads."""
    before = time.time()
    frames = _drive_tool_loop(tmp_path, monkeypatch, calls=3)
    after = time.time()

    starts = [f for f in frames if f.get("type") == "tool_start"]
    assert len(starts) == 3, f"expected three tool_start frames, got {len(starts)}"

    for frame in starts:
        assert "started_at" in frame, (
            "the start frame names no instant, so a client can only invent one - and then "
            f"the number it counts up from is not the one the daemon measures: {frame}"
        )
        started = frame["started_at"]
        assert isinstance(started, float), f"started_at is {type(started).__name__}, not a float"
        # Wall clock, not monotonic: the latter is seconds since boot, which no other
        # process can compare against its own clock. Bracket it by the two readings taken
        # around the run rather than by "roughly now", so a slow run cannot fail it.
        assert before <= started <= after, (
            f"started_at {started} is outside the run [{before}, {after}] - the field is not "
            "the wall-clock instant this call began at"
        )
        assert started > time.monotonic(), (
            "started_at looks like the monotonic clock, which another process cannot compare "
            "against its own - the client would count from an unrelated origin"
        )

    instants = [frame["started_at"] for frame in starts]
    assert instants == sorted(instants), f"the batch's calls started out of order: {instants}"


def test_the_batch_shares_one_round_trip_and_each_call_keeps_its_own_instant(tmp_path, monkeypatch):
    """Two calls in one batch are two instants, and neither is zero.

    A field emitted once for the batch would make the second call's header start counting
    from the first call's start — visibly wrong the moment the first call was the slow one.
    """
    frames = _drive_tool_loop(tmp_path, monkeypatch, calls=2)

    starts = [f for f in frames if f.get("type") == "tool_start"]
    assert len(starts) == 2
    first, second = starts[0]["started_at"], starts[1]["started_at"]
    assert first != 0.0 and second != 0.0, "a zero instant is the absence marker, not a start"
    assert second >= first


def test_an_older_frame_without_the_field_answers_zero():
    """The absence, read from the parser: no instant rather than a fabricated one.

    A client shows no time for such a row (a replayed record from before this field
    existed). Substituting `0.0` here instead of "now" is the whole difference between
    "this was not measured" and "this started in 1970 and has been running for decades".
    """
    assert ToolStart.from_dict({"tool_call_id": "c1"}).started_at == 0.0
    assert ToolStart.from_dict({"tool_call_id": "c1", "started_at": 1_765_000_000.5}).started_at == 1_765_000_000.5
