"""A tool frame's session survives the parser that reads it.

The daemon names the session on `tool_start` and `tool_end`. `ToolStart` and `ToolEnd`
declare what a frame of each kind *is*, and `from_dict` is how every reader gets at it —
the TUI parses both through them (`emrg/client/app.py:1656`, `:1684`). Nothing measured
that the parsed shape still carried what the frame named, so a field present on the wire
and absent from the declaration was invisible: the value reached the parser and stopped.

These tests therefore take the frame the daemon **actually builds**, not a dictionary
written by hand. A hand-written dictionary pins the parser against a copy of the frame's
shape, and a copy can be wrong in exactly the way the declaration was — which is how the
two drifted apart with every existing test still green.

Both directions are pinned, because either alone is satisfiable by the wrong thing:

* a frame that names a session parses to that session (the wire and the declaration agree);
* a frame that names none parses to `""` — the absence marker a client reads as "this frame
  names none", never a fabricated id.

No daemon is started, stopped or restarted: `_run_tool_loop` is called directly with the
broadcast stubbed, and the two planted-fire heartbeat paths are redirected into `tmp_path`
so the run touches no host state.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest, ToolEnd, ToolStart
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.server.tool_types import ToolResult
from emrg.session import Session


class _StubTool:
    """Executes nothing and reports success, so a batch can be driven.

    The subject is what the frame carries into the parser, not any real tool: naming one
    would drag the sandbox and a subprocess into a test about a field.
    """

    async def execute(self, args):  # noqa: ANN001 - mirrors the real tool's shape
        return ToolResult(content="ok")


def _frames_from_one_tool_loop(tmp_path, monkeypatch, calls: int = 2) -> tuple[str, list[dict]]:
    """Run one tool loop whose first round emits `calls` calls; return its session and frames."""
    monkeypatch.setattr(
        daemon_mod, "_PLANTED_FIRE_MARKER_PATH", tmp_path / "planted-fire-heartbeat"
    )
    monkeypatch.setattr(
        daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH", tmp_path / "planted-fire-round-complete"
    )
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = Path(tempfile.mkdtemp()) / "agents.yml"
    session = Session.create_with_id(f"parser-agreement-{calls}", tmp_path)
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
    req = TaskRequest(id="req-parser", session_id=session.session_id, prompt="go")
    asyncio.run(server._run_tool_loop(req, None, session))
    return session.session_id, frames


def test_the_session_a_frame_names_survives_its_parser(tmp_path, monkeypatch):
    """Both frame kinds, parsed: what the daemon put on the wire is what the reader sees."""
    session_id, frames = _frames_from_one_tool_loop(tmp_path, monkeypatch)

    starts = [f for f in frames if f.get("type") == "tool_start"]
    ends = [f for f in frames if f.get("type") == "tool_end"]
    assert len(starts) == 2, f"expected two tool_start frames, got {len(starts)}"
    assert len(ends) == 2, f"expected two tool_end frames, got {len(ends)}"

    for frame in starts:
        assert "session_id" in frame, (
            f"the daemon's start frame names no session, so there is nothing for the parser "
            f"to keep: {frame}"
        )
        parsed = ToolStart.from_dict(frame)
        assert parsed.session_id == session_id, (
            f"the frame names {frame['session_id']!r}, but {ToolStart.__name__}.from_dict "
            f"answered {parsed.session_id!r} - the declaration and the wire disagree, and "
            f"the field is dropped at the parse step"
        )

    for frame in ends:
        assert "session_id" in frame, (
            f"the daemon's end frame names no session, so the card it closes cannot be "
            f"matched to the one it opened: {frame}"
        )
        parsed = ToolEnd.from_dict(frame)
        assert parsed.session_id == session_id, (
            f"the frame names {frame['session_id']!r}, but {ToolEnd.__name__}.from_dict "
            f"answered {parsed.session_id!r}"
        )


def test_a_frame_that_names_no_session_parses_to_the_absence_marker():
    """Silence is not a fabricated id: `""` is what a client falls back on.

    A frame from an older daemon carries no `session_id`. Answering an empty string is the
    whole difference between "this frame names none" and "this frame belongs to session
    ''" — the second would be read as a real key by anything that buckets by it.
    """
    for frame_type in (ToolStart, ToolEnd):
        assert frame_type.from_dict({"tool_call_id": "c1"}).session_id == ""
        assert frame_type.from_dict(
            {"tool_call_id": "c1", "session_id": "sess-a"}
        ).session_id == "sess-a"
