"""A tool row says which call of its batch it is — and only the daemon decides that.

Rant 2026-10-20T12:22:21. The host asked that a tool call's display carry `(x/y)`
so a batch of calls from one LLM response can be told apart from calls that arrived
one at a time, and asked for one implementation: **the daemon decides what a batch
is and hands each call a finished string**; the clients print what they are given.

That requirement is why this file asserts what it does. A helper's return value
proves only that the helper is right; it says nothing about whether the frames and
the projected history — the two things clients actually receive — carry the label.
So the subjects here are:

* **the live frame, read off the wire.** `_run_tool_loop` is driven with a stubbed
  LLM that emits a three-call response, and the `tool_start` broadcasts are read in
  order. A label computed but never attached to the frame fails here, which a unit
  test of the helper would have passed.
* **the history projection, read through `list_history`.** A session recorded
  **before** the field existed is projected through the real message handler, so the
  assertion is about what a reopening client receives — including for a session
  written earlier, which is the whole point of computing the label at read time
  rather than persisting it.
* **the TUI's rendered line.** `ToolCard.render` is asked for its text, because a
  card that stores a label and does not draw it is the failure mode a field test
  cannot see (the same lesson as the message-time rant: assert the rendering, not
  the field).
* **the absence, twice.** A frame and a record with no `call_label` must render
  **exactly** as they do without this feature — and specifically must not invent
  `(1/1)`, which is what a client that counted the calls itself would show.

No daemon is started, stopped or restarted: `_run_tool_loop` and `_process_message`
are called directly, with the broadcast and the websocket stubbed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from rich.style import Style

from emrg.client.app import _cards_from_tool_calls
from emrg.client.python_tui.widgets.base import RenderContext
from emrg.client.python_tui.widgets.tool_card import ToolCard
from emrg.config import LlmConfig
from emrg.protocol import TaskRequest, ToolStart
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.server.tool_types import ToolResult
from emrg.session import Session


def _ctx(width: int = 100) -> RenderContext:
    return RenderContext(width=width, style=Style.null())


def _make_server() -> EmrgServer:
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile_dir()) / "projects.yml"
    return server


def tempfile_dir() -> str:
    import tempfile

    return tempfile.mkdtemp()


class _StubTool:
    """A tool that executes nothing and reports success, so a batch can be driven.

    The subject is the label, not the tool: naming a real tool would drag the
    sandbox, the platform's shell dialect and an actual subprocess into a test
    about a string.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, args):
        self.calls.append(dict(args))
        return ToolResult(content="ok")


def _drive_tool_loop(tmp_path, monkeypatch, calls: int) -> list[dict]:
    """Run one tool loop whose first round emits `calls` tool calls; return the frames."""
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH",
                        tmp_path / "planted-fire-heartbeat")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH",
                        tmp_path / "planted-fire-round-complete")
    server = _make_server()
    session = Session.create_with_id(f"batch-label-{calls}", tmp_path)
    stub = _StubTool()
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
    req = TaskRequest(id="req-batch", session_id=session.session_id, prompt="go")
    asyncio.run(server._run_tool_loop(req, None, session))
    return frames


class _FakeWs:
    """A subscriber that records the frames the daemon sends it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))


def _history_messages(tmp_path, records: list[dict]) -> list[dict]:
    """Project `records` through the real `list_history` handler."""
    server = _make_server()
    session = Session.create_with_id("batch-label-history", tmp_path)
    for record in records:
        session.append_message(record)
    ws = _FakeWs()
    asyncio.run(server._process_message(
        {"type": "list_history", "session_id": session.session_id, "cwd": str(tmp_path),
         # Records mode: the projection — and therefore the label — lives on this
         # payload, which is what a client replaying a session asks for.
         "include_records": True},
        ws,
    ))
    frames = [f for f in ws.sent if f.get("type") == "history_list"]
    assert frames, f"no history_list frame was sent: {ws.sent}"
    return frames[0]["messages"]


def _tool_call(index: int) -> dict:
    return {
        "id": f"call_old_{index}",
        "type": "function",
        "function": {"name": "read", "arguments": '{"intent": "x"}'},
    }


# ── the live frames ────────────────────────────────────────────────────────


def test_a_three_call_response_labels_each_start_frame_in_order(tmp_path, monkeypatch):
    """`(1/3) `, `(2/3) `, `(3/3) ` on the frames the client reads — not on a helper."""
    frames = _drive_tool_loop(tmp_path, monkeypatch, calls=3)

    starts = [f for f in frames if f.get("type") == "tool_start"]
    assert [f["call_label"] for f in starts] == ["(1/3) ", "(2/3) ", "(3/3) "], (
        f"the frames carry {[f.get('call_label') for f in starts]!r}"
    )
    assert [f["tool_call_id"] for f in starts] == ["call_3_0", "call_3_1", "call_3_2"]


def test_a_single_call_response_carries_no_label(tmp_path, monkeypatch):
    """A batch of one shows nothing: `(1/1)` would be noise on every ordinary turn."""
    frames = _drive_tool_loop(tmp_path, monkeypatch, calls=1)

    starts = [f for f in frames if f.get("type") == "tool_start"]
    assert len(starts) == 1, f"expected one tool_start, got {len(starts)}"
    assert starts[0]["call_label"] == ""


def test_the_end_frame_does_not_carry_a_second_copy_of_the_label(tmp_path, monkeypatch):
    """One source, not two: the card already holds the label, so `tool_end` adds none.

    A second carrier is a second thing to keep in step, and the client would then
    have two ways to spell the same row (rant 2026-10-20T12:22:21).
    """
    frames = _drive_tool_loop(tmp_path, monkeypatch, calls=2)

    ends = [f for f in frames if f.get("type") == "tool_end"]
    assert len(ends) == 2, f"expected two tool_end frames, got {len(ends)}"
    assert all("call_label" not in f for f in ends), (
        "tool_end carries call_label — the label belongs to the start frame only"
    )


# ── the history projection ─────────────────────────────────────────────────


def test_history_projection_labels_a_batch_recorded_without_one(tmp_path):
    """A session recorded before this change replays with `(x/y)`: computed at read time."""
    messages = _history_messages(tmp_path, [{
        "type": "message", "role": "assistant", "content": None,
        "tool_calls": [_tool_call(0), _tool_call(1), _tool_call(2)],
    }])

    assistant = [m for m in messages if m.get("kind") == "message"]
    assert len(assistant) == 1, messages
    projected = assistant[0]["tool_calls"]
    assert [c["call_label"] for c in projected] == ["(1/3) ", "(2/3) ", "(3/3) "]
    assert [c["id"] for c in projected] == ["call_old_0", "call_old_1", "call_old_2"]


def test_the_projection_copies_the_record_instead_of_mutating_it(tmp_path):
    """The label is a reading, not a write: the stored record keeps its own shape.

    Persisting it would make the answer a property of when the session was
    recorded — the defect this whole design avoids.
    """
    server = _make_server()
    session = Session.create_with_id("batch-label-purity", tmp_path)
    session.append_message({
        "type": "message", "role": "assistant", "content": None,
        "tool_calls": [_tool_call(0), _tool_call(1)],
    })
    ws = _FakeWs()
    asyncio.run(server._process_message(
        {"type": "list_history", "session_id": session.session_id, "cwd": str(tmp_path),
         # Records mode: the projection — and therefore the label — lives on this
         # payload, which is what a client replaying a session asks for.
         "include_records": True},
        ws,
    ))

    on_disk = session._read_history()
    stored = [r for r in on_disk if r.get("tool_calls")][0]["tool_calls"]
    assert all("call_label" not in call for call in stored), (
        f"the projection wrote into the stored record: {stored!r}"
    )


def test_history_projection_leaves_a_single_call_unlabelled(tmp_path):
    messages = _history_messages(tmp_path, [{
        "type": "message", "role": "assistant", "content": None,
        "tool_calls": [_tool_call(0)],
    }])

    projected = [m for m in messages if m.get("kind") == "message"][0]["tool_calls"]
    assert [c["call_label"] for c in projected] == [""]


# ── the TUI's rendered line ────────────────────────────────────────────────


def _rendered(card: ToolCard) -> str:
    """The text the card really draws, joined from its spans."""
    lines = card.render(_ctx())
    return "".join(span.text for span in lines[0].spans)


def test_the_tui_row_prints_the_label_before_the_name():
    card = ToolCard(name="bash", command="run the tests", status="done", call_label="(2/3) ")

    rendered = _rendered(card)

    assert "(2/3) " in rendered, rendered
    assert rendered.index("(2/3) ") < rendered.index("bash"), (
        f"the label must precede the name: {rendered!r}"
    )


def test_a_row_without_a_label_renders_exactly_as_before():
    """The absence guard: no placeholder, no `(1/1)`, no empty brackets."""
    rendered = _rendered(ToolCard(name="bash", command="run the tests", status="done"))

    assert rendered == "✓ bash: run the tests", rendered
    assert "(1/1)" not in rendered


def test_a_replayed_batch_labels_its_cards_from_the_projected_record(tmp_path):
    """The TUI's replay path takes the label off the record, and computes nothing.

    Two halves in one reading: a projected record produces labelled cards, and a
    record with **no** `call_label` produces cards whose rendering is byte-identical
    to the pre-change one — including that no client-invented `(1/1)` appears.
    """
    projected = [
        {**_tool_call(0), "call_label": "(1/2) "},
        {**_tool_call(1), "call_label": "(2/2) "},
    ]
    cards = _cards_from_tool_calls(projected)
    labelled = [_rendered(c) for c in cards]
    assert all("(1/2) " in text or "(2/2) " in text for text in labelled), labelled
    assert labelled[0].index("(1/2) ") < labelled[0].index("read"), labelled

    # The absence half: a record with no `call_label` must render exactly as a card
    # built the way this feature did not exist — which is the proof that the client
    # computes nothing of its own.
    bare = _cards_from_tool_calls([_tool_call(0), _tool_call(1)])
    for index, card in enumerate(bare):
        baseline = ToolCard(name="read", command=card.command, status="pending",
                            tool_call_id=card.tool_call_id, arguments=card.arguments)
        assert _rendered(card) == _rendered(baseline), (
            f"row {index} renders differently once the field is absent: "
            f"{_rendered(card)!r} vs {_rendered(baseline)!r}"
        )
        assert "(1/1)" not in _rendered(card)
        assert "(1/2)" not in _rendered(card)


def test_the_protocol_defaults_the_label_for_a_frame_that_predates_it():
    """An older frame has no field; it must read as a single call, not crash."""
    frame = ToolStart.from_dict({
        "request_id": "r", "tool_name": "bash", "tool_call_id": "c",
        "arguments": {}, "intent": "x",
    })

    assert frame.call_label == ""
