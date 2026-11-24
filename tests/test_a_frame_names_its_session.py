"""A frame that belongs to a session names it, and a query does not move the subscription.

Issue #3191 reports two defects that together let one session's tool card appear in
another session's transcript and never finish:

1. `tool_start` and `tool_end` carried no `session_id`, while `turn_start`,
   `turn_progress` and `done` all carried one. A client could only fill the gap with
   "the session this connection was opened for".
2. The read loop moved a connection's subscription on **any** inbound message that
   named a `session_id`, whatever its type — so a query about another session
   (a history listing, a rename, a delete) relocated the connection, and the frames
   that followed for its *own* session went to a subscriber set it was no longer in.

Neither half is visible on its own: with the subscription intact the missing field is
harmless, and with the field present the moved subscription still draws a card in the
wrong transcript. So both are pinned here, each from the side that fails without it.

The subscription half is measured in the read loop itself, with `_process_message` —
the only thing the rule sits in front of — replaced by a recorder. That reads the
subscription table at the instant a frame is dispatched, which is the only moment it
can be read: the loop's `finally` unsubscribes on disconnect, so a reading taken after
`_handle_client` returns would see every rule as "nothing subscribed". No daemon is
started, stopped or restarted, and no session, index or config file is touched: the
frames never reach a dispatcher.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest, ToolEnd, ToolStart
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.server.tool_types import ToolResult
from emrg.session import Session


class _StubTool:
    """Executes nothing and reports success, so a batch can be driven."""

    async def execute(self, args):  # noqa: ANN001 - mirrors the real tool's shape
        return ToolResult(content="ok")


def _drive_tool_loop(tmp_path, monkeypatch, calls: int = 2) -> tuple[str, list[dict]]:
    """Run one tool loop whose first round emits `calls` calls; return its frames."""
    monkeypatch.setattr(
        daemon_mod, "_PLANTED_FIRE_MARKER_PATH", tmp_path / "planted-fire-heartbeat"
    )
    monkeypatch.setattr(
        daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH", tmp_path / "planted-fire-round-complete"
    )
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = Path(tempfile.mkdtemp()) / "agents.yml"
    session = Session.create_with_id("frame-names-its-session", tmp_path)
    stub = _StubTool()
    server.tools.get = lambda name: stub  # type: ignore[method-assign]

    state = {"round": 0}

    async def fake_stream(messages, tools=None):  # noqa: ANN001 - the LLM seam
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
    req = TaskRequest(id="req-frames", session_id=session.session_id, prompt="go")
    asyncio.run(server._run_tool_loop(req, None, session))
    return session.session_id, frames


def test_both_tool_frames_name_the_session_they_belong_to(tmp_path, monkeypatch):
    """Every start and end frame carries the session, so a client need not guess.

    Both frame types, because the pair is what a client matches: a start that says
    `s1` and an end that says nothing would land the card in one transcript and its
    completion in another — the shape the issue reports.
    """
    session_id, frames = _drive_tool_loop(tmp_path, monkeypatch)

    tool_starts = [f for f in frames if f.get("type") == "tool_start"]
    tool_ends = [f for f in frames if f.get("type") == "tool_end"]
    assert len(tool_starts) == 2, f"expected two tool_start frames, got {len(tool_starts)}"
    assert len(tool_ends) == 2, f"expected two tool_end frames, got {len(tool_ends)}"

    for frame in tool_starts + tool_ends:
        assert "session_id" in frame, (
            f"a {frame.get('type')} frame names no session, so a client can only fall back "
            f"to the connection's own and cannot tell a foreign frame apart: {frame}"
        )
        assert frame["session_id"] == session_id, (
            f"{frame.get('type')} names {frame['session_id']!r}, not the session the turn "
            f"runs in ({session_id!r})"
        )


def test_an_older_tool_frame_names_no_session_rather_than_a_wrong_one():
    """The absence, read from the parsers: `""`, which is what the fallback keys on.

    An empty string is not a fabricated session id. A client reads it as "this frame
    names none" and uses the connection's own — the only thing it can do — and that
    distinction is exactly what the field exists to make checkable.
    """
    assert ToolStart.from_dict({"tool_call_id": "c1"}).session_id == ""
    assert ToolEnd.from_dict({"tool_call_id": "c1"}).session_id == ""

    named = {"tool_call_id": "c1", "session_id": "sess-a"}
    assert ToolStart.from_dict(named).session_id == "sess-a"
    assert ToolEnd.from_dict(named).session_id == "sess-a"


class _FakeSocket:
    """A connection the read loop can drive: `recv` yields frames, then ends."""

    def __init__(self, frames: list[dict]) -> None:
        self._frames = [json.dumps(frame) for frame in frames]
        self.sent: list[dict] = []

    async def recv(self) -> str:
        if not self._frames:
            from websockets.exceptions import ConnectionClosed

            raise ConnectionClosed(None, None)
        return self._frames.pop(0)

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))
        await asyncio.sleep(0)

    async def close(self) -> None:
        pass


def _subscription_seen_at_each_dispatch(
    tmp_path, monkeypatch, frames: list[dict]
) -> list[dict[str, set]]:
    """Drive the real read loop; return the subscription table at each dispatch.

    `_process_message` is the seam: the subscription rule sits immediately in front of
    it, so a recorder placed there answers "where was this connection subscribed when
    this frame was handled" — and it keeps every frame out of a real dispatcher, so no
    session is created and no index is written.
    """
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH", tmp_path / "fire")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH", tmp_path / "rc")
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._auth_token = "test-token"
    seen: list[dict[str, set]] = []

    async def record(data, ws):  # noqa: ANN001 - the dispatch seam
        seen.append(
            {
                sid: set(subs) for sid, subs in server._session_subscribers.items() if subs
            }
        )

    server._process_message = record  # type: ignore[method-assign]
    socket = _FakeSocket([{"type": "auth", "token": "test-token"}, *frames])
    asyncio.run(server._handle_client(socket))
    return seen


def test_a_query_naming_another_session_leaves_the_subscription_alone(tmp_path, monkeypatch):
    """The defect: a history listing or a rename for session B moved the connection off A.

    After the move, A's own `turn_start` / stream / `tool_end` reached nobody — the
    client kept a turn that never finished — and nothing errored, because the
    connection had simply left the set the next broadcast reads.

    `resume_session` opens the connection's own session here because it is the
    attaching message that reaches a dispatcher: `task`, `cancel` and
    `approval_response` are handled inside the read loop before it, so the recorder
    below cannot see them. The set they belong to is named in one place
    (`daemon._SUBSCRIBING_MESSAGE_TYPES`), and this file measures the rule in both
    directions through the members it can observe.
    """
    seen = _subscription_seen_at_each_dispatch(
        tmp_path,
        monkeypatch,
        [
            {"type": "resume_session", "session_id": "sess-a", "cwd": str(tmp_path)},
            {"type": "list_history", "session_id": "sess-b", "cwd": str(tmp_path)},
            {"type": "rename_session", "session_id": "sess-c", "cwd": str(tmp_path), "title": "t"},
        ],
    )

    assert len(seen) == 3, f"expected three dispatched frames, saw {len(seen)}"
    for index, table in enumerate(seen):
        assert "sess-a" in table, (
            f"after frame {index} the connection is no longer subscribed to its own "
            f"session — a query for another one moved it: {table}"
        )
        for foreign in ("sess-b", "sess-c"):
            assert foreign not in table, (
                f"frame {index} subscribed the connection to {foreign}, which it only "
                f"asked a question about: {table}"
            )


def test_an_attaching_message_still_moves_the_subscription(tmp_path, monkeypatch):
    """The control, in the other direction: opening another session does move it.

    Without this the rule above could be satisfied by a read loop that never subscribes
    at all — which would break every client, and would still pass a test that only
    asserts "not B".
    """
    seen = _subscription_seen_at_each_dispatch(
        tmp_path,
        monkeypatch,
        [
            {"type": "resume_session", "session_id": "sess-a", "cwd": str(tmp_path)},
            {"type": "resume_session", "session_id": "sess-d", "cwd": str(tmp_path)},
        ],
    )

    assert len(seen) == 2, f"expected two dispatched frames, saw {len(seen)}"
    assert "sess-a" in seen[0] and "sess-d" not in seen[0], seen[0]
    assert "sess-d" in seen[1], (
        f"opening another session did not move the subscription, so a client that "
        f"switches sessions would receive nothing: {seen[1]}"
    )
    assert "sess-a" not in seen[1], (
        f"the previous session is still subscribed after the switch: {seen[1]}"
    )
