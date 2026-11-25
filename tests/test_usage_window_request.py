"""The `usage_window` request: the daemon's last 24 hours, answered over the wire.

Rant 2026-11-19T09:31:00 makes the daemon the single source of the spend reading. This
file pins the three things a client cannot check for itself:

* **the request is answered with three segments and a window.** A client that summed for
  itself would be a second ledger; one that received a total instead of the segments
  could not show input, cache and output apart.
* **the window is the host's, not the caller's.** Two sessions' rounds land in the same
  answer, and a message that names a session id is answered without narrowing to it —
  which is the reading the host rejected ("switching sessions resets it").
* **a restart does not start the day over.** A second server over the same ledger file
  answers the same numbers, and a round whose provider reported no usage adds nothing.

Every turn runs through `_run_tool_loop` — a host message, a scheduled task, a subagent —
so driving that loop is how a round is recorded here, exactly as in
`test_turn_progress_frame.py`. No daemon process is started, stopped or restarted.
"""

from __future__ import annotations

import asyncio
import json
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


class _StubWs:
    """A connection that keeps the frames the daemon sends it, parsed."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send(self, payload: str) -> None:
        self.frames.append(json.loads(payload))


def _make_server(tmp_path: Path) -> EmrgServer:
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = tmp_path / "agents.yml"
    return server


def _run_a_round(server: EmrgServer, session: Session, usage: dict | None) -> None:
    """Drive one round that ends the loop, reporting `usage` the way a provider does."""

    async def fake_stream(messages, tools=None, exchange=None):
        yield {"content": "done", "tool_calls": None,
               "finish_reason": "stop", "usage": usage}

    async def fake_broadcast(session_id, payload):
        return None

    server.llm.chat_stream = fake_stream
    server._broadcast = fake_broadcast
    req = TaskRequest(id="req-usage", session_id=session.session_id, prompt="go")
    asyncio.run(server._run_tool_loop(req, None, session))


def _ask(server: EmrgServer, **message) -> dict:
    """Send `usage_window` through the real dispatcher and return the frame it answered."""
    ws = _StubWs()
    asyncio.run(server._process_message({"type": "usage_window", **message}, ws))
    assert ws.frames, "the request was dispatched and answered nothing"
    return ws.frames[-1]


def _patch_fire_markers(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH",
                        tmp_path / "planted-fire-heartbeat")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH",
                        tmp_path / "planted-fire-round-complete")


def test_the_request_answers_three_segments_and_the_window(tmp_path, monkeypatch):
    """Input, cache and output arrive summed apart, with the window they were summed over."""
    _patch_fire_markers(monkeypatch, tmp_path)
    server = _make_server(tmp_path)
    session = Session.create_with_id("usage-a", tmp_path)
    server.tools.get = lambda name: _StubTool()  # type: ignore[method-assign]

    _run_a_round(server, session,
                 {"prompt_tokens": 1000, "cache_hit_tokens": 800, "completion_tokens": 40})
    _run_a_round(server, session,
                 {"prompt_tokens": 500, "cache_hit_tokens": 400, "completion_tokens": 60})

    frame = _ask(server)
    assert frame["type"] == "usage_window"
    assert frame["input"] == 1500
    assert frame["cache"] == 1200
    assert frame["output"] == 100
    assert frame["window_end"] > frame["window_start"]
    assert "total" not in frame


def test_two_sessions_land_in_one_window(tmp_path, monkeypatch):
    """The window is the host's: what two different sessions spent is one answer.

    This is the guard against narrowing the window to "the session I have open", which is
    the behaviour the earliest rant was filed about. Naming a session id leaves the
    machine's totals alone — and adds that session's own share beside them (issue #3072).
    """
    _patch_fire_markers(monkeypatch, tmp_path)
    server = _make_server(tmp_path)
    server.tools.get = lambda name: _StubTool()  # type: ignore[method-assign]

    first = Session.create_with_id("usage-first", tmp_path)
    second = Session.create_with_id("usage-second", tmp_path)
    _run_a_round(server, first, {"prompt_tokens": 10, "completion_tokens": 1})
    _run_a_round(server, second, {"prompt_tokens": 20, "completion_tokens": 2})

    unnamed = _ask(server)
    assert (unnamed["input"], unnamed["output"]) == (30, 3)

    named = _ask(server, session_id="usage-second")
    # the machine's, identical to the unnamed reading
    assert (named["input"], named["output"]) == (30, 3)
    # and the second session's own share beside it, which the unnamed reading has none of
    assert named["session"] == {"input": 20, "cache": None, "output": 2}
    assert unnamed["session"] == {"input": None, "cache": None, "output": None}


def test_a_named_request_answers_that_sessions_share_beside_the_machines(
    tmp_path, monkeypatch
):
    """Two rounds on one session, one on another: the share excludes the other's spend.

    The mutation this pins shut is the cheap one — answering the machine's figures under
    the session's name, or summing every row once a session was named.
    """
    _patch_fire_markers(monkeypatch, tmp_path)
    server = _make_server(tmp_path)
    server.tools.get = lambda name: _StubTool()  # type: ignore[method-assign]

    mine = Session.create_with_id("usage-mine", tmp_path)
    other = Session.create_with_id("usage-other", tmp_path)
    _run_a_round(server, mine, {"prompt_tokens": 100, "cache_hit_tokens": 60, "completion_tokens": 4})
    _run_a_round(server, mine, {"prompt_tokens": 50, "cache_hit_tokens": 10, "completion_tokens": 6})
    _run_a_round(server, other, {"prompt_tokens": 900, "cache_hit_tokens": 800, "completion_tokens": 90})

    asked = _ask(server, session_id="usage-mine")
    assert (asked["input"], asked["cache"], asked["output"]) == (1050, 870, 100)
    assert asked["session"] == {"input": 150, "cache": 70, "output": 10}
    # the other session reads its own share from the same ledger
    assert _ask(server, session_id="usage-other")["session"] == {
        "input": 900, "cache": 800, "output": 90
    }


def test_a_session_that_spent_nothing_in_the_window_stays_unmeasured(
    tmp_path, monkeypatch
):
    """Item 6: `—`, never 0, and never the machine's figure standing in for it."""
    _patch_fire_markers(monkeypatch, tmp_path)
    server = _make_server(tmp_path)
    server.tools.get = lambda name: _StubTool()  # type: ignore[method-assign]

    spendthrift = Session.create_with_id("usage-spender", tmp_path)
    idle = Session.create_with_id("usage-idle", tmp_path)
    _run_a_round(server, spendthrift, {"prompt_tokens": 40, "completion_tokens": 4})

    asked = _ask(server, session_id="usage-idle")
    assert (asked["input"], asked["output"]) == (40, 4)
    assert asked["session"] == {"input": None, "cache": None, "output": None}
    assert idle.session_id != spendthrift.session_id


def test_a_second_server_over_the_same_ledger_answers_the_same_numbers(tmp_path, monkeypatch):
    """A daemon restart must not start the day over: the window is read from the file."""
    _patch_fire_markers(monkeypatch, tmp_path)
    server = _make_server(tmp_path)
    session = Session.create_with_id("usage-restart", tmp_path)
    server.tools.get = lambda name: _StubTool()  # type: ignore[method-assign]
    _run_a_round(server, session, {"prompt_tokens": 700, "completion_tokens": 30})

    restarted = _make_server(tmp_path)
    restarted.tools.get = lambda name: _StubTool()  # type: ignore[method-assign]
    frame = _ask(restarted)
    assert (frame["input"], frame["output"]) == (700, 30)


def test_a_round_with_no_usage_adds_nothing_and_the_segments_stay_unmeasured(
    tmp_path, monkeypatch
):
    """Item 6: a response with no usage is not a zero-token response."""
    _patch_fire_markers(monkeypatch, tmp_path)
    server = _make_server(tmp_path)
    session = Session.create_with_id("usage-nothing", tmp_path)
    server.tools.get = lambda name: _StubTool()  # type: ignore[method-assign]
    _run_a_round(server, session, None)

    frame = _ask(server)
    assert (frame["input"], frame["cache"], frame["output"]) == (None, None, None)
