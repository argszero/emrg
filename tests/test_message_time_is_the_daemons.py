"""A message's time is the daemon's, written once, and travels to every reader.

Rant 2026-10-09T09:25:00 (the host: 「每条消息都没有显示时间」, and the acceptance
that the time a live turn shows is the *same value* a reopen reads).

The defect this file pins is not "a clock is missing" but "there are two clocks".
A client that stamps its own row at render time makes a message's moment a
property of whoever happened to be watching: reopen the session in another client
and the same record reads a different time. So the stamp is produced in exactly
one place — the daemon, at the instant it writes the record — and every frame that
describes that record carries **the same string the record holds**.

Two layers are measured here, and each has its own way of failing:

* **the daemon** — `_run_tool_loop` is driven with a stubbed LLM and a capturing
  `_broadcast`, then the frames are compared against what actually landed in the
  session's history. If a frame drops the moment, or invents one of its own, the
  comparison moves; nothing about the code's shape is asserted. A user record has
  **two** producers — the turn that starts and the message injected into a turn
  already running — and both are measured, because the second one is where the
  divergence was found (the record always had a moment; `steer_committed` never
  carried one).
* **the client** — the rows are rendered and the *rendered line* is asked for the
  time, because the row storing a field proves only that it was handed one. The
  absence case is asserted too: a record written before the moment was carried
  must render as **no clock**, never as empty brackets and never as a rendered
  placeholder (`Invalid Date`, `None`). The parking of an echoed row is measured
  as a layer of its own: more than one row can be in flight, and a frame answers
  **one** of them.

No daemon is started, stopped or restarted here: `_run_tool_loop` and
`_inject_pending_messages` are called directly with their broadcast stubbed.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from emrg.client.python_tui.pending_rows import ParkedUserRows
from emrg.client.python_tui.widgets.base import RenderContext
from emrg.client.python_tui.widgets.chat_row import ChatRow, format_message_time
from emrg.client.python_tui.widgets.markdown import StreamingMarkdown, UserMarkdown
from emrg.client.widgets import ChatHistory
from emrg.config import LlmConfig
from emrg.protocol import TaskRequest, new_task_id
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.session import Session

def _ctx(width: int = 80):
    """A render context — built here because its fields are the row's business."""
    from rich.style import Style

    return RenderContext(width=width, style=Style.null())


def _make_server() -> EmrgServer:
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def _drive_one_turn(tmp_path, monkeypatch) -> tuple[list[dict], Session]:
    """Run one complete tool loop. Returns `(frames, session)`.

    `(frames)` is everything the loop broadcast, in order; `(session)` is the
    session whose history the loop wrote. Both planted-fire markers are pointed at
    `tmp_path` — the loop stamps host state at round finalization, and a suite run
    must not write `~/.emrg/logs`.
    """
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH",
                        tmp_path / "planted-fire-heartbeat")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH",
                        tmp_path / "planted-fire-round-complete")
    server = _make_server()
    session = Session.create_with_id("message-time-test", tmp_path)

    async def fake_stream(messages, tools=None):
        yield {"content": "the reply", "tool_calls": None, "finish_reason": "stop",
               "usage": {"prompt_tokens": 5, "completion_tokens": 2}}

    frames: list[dict] = []

    async def fake_broadcast(session_id, payload):
        frames.append(payload)

    server.llm.chat_stream = fake_stream
    server._broadcast = fake_broadcast
    req = TaskRequest(id="req-time", session_id=session.session_id, prompt="hello")
    asyncio.run(server._run_tool_loop(req, None, session))
    return frames, session


def _records_on_disk(session: Session) -> list[dict]:
    import json
    lines = session._history_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ── the daemon: one stamp, and every reader gets that same string ──────────


def test_the_user_frame_carries_the_moment_the_record_was_written(tmp_path, monkeypatch):
    """The live row's clock and the replayed one are the same string, not two clocks.

    The frame is the *only* way a client can learn this: it echoes the host's text
    the instant it is typed, long before the daemon has written anything, so a
    client-supplied value would be a second clock and reopening the session would
    show a different time for the same message.
    """
    frames, session = _drive_one_turn(tmp_path, monkeypatch)

    stamped = [f for f in frames if f.get("type") == "user_message"]
    assert len(stamped) == 1, (
        f"the daemon broadcast {len(stamped)} user_message frame(s) for one turn; "
        "the client has nothing else to take the moment from"
    )
    frame = stamped[0]

    written = [r for r in _records_on_disk(session) if r.get("role") == "user"]
    assert len(written) == 1, written
    assert frame.get("request_id") == "req-time", (
        "the frame must name the request it belongs to, or a client cannot tell its "
        "own message from a peer's — the frame is a broadcast"
    )
    assert frame.get("timestamp") == written[0].get("timestamp"), (
        f"the frame says {frame.get('timestamp')!r}, the record holds "
        f"{written[0].get('timestamp')!r} — the live row and the reopened one would "
        "show different times for one message"
    )
    assert frame.get("timestamp"), "the frame carried no moment at all"


def test_the_done_frame_carries_the_replys_own_moment(tmp_path, monkeypatch):
    """Same rule for the assistant half: the record's stamp rides the ending frame."""
    frames, session = _drive_one_turn(tmp_path, monkeypatch)

    done = [f for f in frames if f.get("done") and f.get("content")]
    assert done, frames
    frame = done[-1]

    written = [r for r in _records_on_disk(session) if r.get("role") == "assistant"]
    assert written, _records_on_disk(session)
    assert frame.get("timestamp") == written[-1].get("timestamp"), (
        f"the done frame says {frame.get('timestamp')!r}, the record holds "
        f"{written[-1].get('timestamp')!r}"
    )
    assert frame.get("timestamp"), "the done frame carried no moment at all"


def test_the_two_moments_are_both_real_instants(tmp_path, monkeypatch):
    """The values are parseable and ordered — a stamp that cannot be read is no clock.

    `format_message_time` swallows an unreadable value into "", which is right for
    a *legacy* record and wrong as a hiding place for a malformed new one. This
    asks the producer's output directly.
    """
    frames, _ = _drive_one_turn(tmp_path, monkeypatch)

    user_at = datetime.fromisoformat(
        [f for f in frames if f.get("type") == "user_message"][0]["timestamp"])
    assistant_at = datetime.fromisoformat(
        [f for f in frames if f.get("done") and f.get("content")][-1]["timestamp"])
    assert user_at <= assistant_at, (
        "the reply is stamped before the message it answers, which no clock does"
    )


# ── the daemon: the *other* producer of a user record — injection ───────────
#
# A message typed while the turn is running is queued and injected at a round
# boundary. Its record was always stamped by `append_message`, but the frame that
# announced it (`steer_committed`) carried no moment, so the row the client had
# already drawn kept no clock until the session was reopened — the divergence the
# rant names, on a path the first version of this feature did not cover.


def _inject_messages(tmp_path, prompts: list[str]) -> tuple[list[dict], Session, list[dict]]:
    """Run one injection round. Returns `(frames, session, messages)`."""
    server = _make_server()
    session = Session.create_with_id("message-time-inject", tmp_path)

    frames: list[dict] = []

    async def fake_broadcast(session_id, payload):
        frames.append(payload)

    server._broadcast = fake_broadcast
    server._session_pending[session.session_id] = [
        (TaskRequest(id=f"inject-{i}", session_id=session.session_id, prompt=p), True)
        for i, p in enumerate(prompts)
    ]
    messages: list[dict] = []
    asyncio.run(server._inject_pending_messages(session, messages))
    return frames, session, messages


def _user_records_by_content(session: Session) -> dict[str, str]:
    return {
        r.get("content"): r.get("timestamp")
        for r in _records_on_disk(session)
        if r.get("role") == "user"
    }


def test_an_injected_message_carries_its_moment_to_the_clients(tmp_path):
    """The frame that announces the injection is the only place a live row can get it."""
    frames, session, messages = _inject_messages(tmp_path, ["typed while busy"])

    steer = [f for f in frames if f.get("type") == "steer_committed"]
    assert len(steer) == 1, (
        "an injected message must be announced exactly once, or the row the client "
        "drew for it is never filled"
    )
    assert steer[0].get("request_id") == "inject-0", steer[0]
    assert steer[0].get("timestamp"), (
        "the injected record has a moment but no frame carries it — the live row "
        "stays clockless until a reopen"
    )
    written = _user_records_by_content(session)
    assert written == {"typed while busy": steer[0]["timestamp"]}, (
        f"the frame says {steer[0]['timestamp']!r}, the record holds {written!r} — the "
        "live row and the replayed one would show different times for one message"
    )
    assert len(messages) == 1, "the message did not reach the round it was injected into"


def test_each_injected_message_carries_its_own_moment(tmp_path):
    """Two in one round are two records and two frames, each with its own stamp.

    A single shared stamp would make messages sent seconds apart claim the same
    instant, and a frame that reported the loop's moment rather than the record's
    would stop matching the history it describes.
    """
    frames, session, _ = _inject_messages(tmp_path, ["first", "second"])

    steer = [f for f in frames if f.get("type") == "steer_committed"]
    assert [f.get("request_id") for f in steer] == ["inject-0", "inject-1"], steer
    written = _user_records_by_content(session)
    assert set(written) == {"first", "second"}, written
    assert steer[0]["timestamp"] == written["first"]
    assert steer[1]["timestamp"] == written["second"]


def test_the_minted_request_id_is_the_id_the_request_carries(monkeypatch):
    """`new_task_id` exists so the client knows the id *before* it sends.

    The client keys its echoed row by that id, so a mint that disagreed with the
    request it fills in would park the row under a name no frame ever uses — the
    bug would be invisible until a live row silently kept no clock. Hence one mint
    site: the request's own default is asked for it, and both answers are measured.
    """
    rid = new_task_id()
    assert isinstance(rid, str) and rid
    assert TaskRequest(id=rid).to_dict()["id"] == rid

    monkeypatch.setattr("emrg.protocol.new_task_id", lambda: "minted-elsewhere")
    assert TaskRequest().id == "minted-elsewhere", (
        "the request mints its own id instead of asking `new_task_id`, so the id a "
        "client parks a row under need not be the id the request carries"
    )


# ── the client: the rendered line carries the clock, or nothing ─────────────


def _rendered_text(row) -> str:
    lines = row.render(_ctx())
    return "\n".join("".join(span.text for span in line.spans) for line in lines)


def test_an_assistant_row_renders_the_moment_it_was_given():
    row = ChatRow(role="assistant", content="hi", timestamp="2026-10-09T09:25:00")

    assert "09:25" in _rendered_text(row)


def test_a_user_row_renders_the_moment_it_was_given():
    row = UserMarkdown("hi", timestamp="2026-10-09T09:25:00")

    assert "09:25" in _rendered_text(row)


def test_a_streaming_row_renders_the_moment_once_it_is_settled():
    """The live reply has no moment while it streams, and one when the frame lands."""
    row = StreamingMarkdown()
    row.feed("the reply")
    assert ":" not in _rendered_text(row).split("the reply")[-1], (
        "a streaming row claims a clock before the daemon has written one"
    )

    row.timestamp = "2026-10-09T09:25:00"
    row.dirty = True
    assert "09:25" in _rendered_text(row)


@pytest.mark.parametrize("missing", [None, "", "not a timestamp", "2026-13-45T99:99"])
def test_a_row_with_no_readable_moment_renders_no_clock_at_all(missing):
    """Records written before the field existed must not render a placeholder.

    An empty pair of brackets or an `Invalid Date` in the transcript is worse than
    a missing clock: it claims a time was known and lost, and it is what the host
    actually sees when a legacy session is reopened.
    """
    row = ChatRow(role="assistant", content="hi", timestamp=missing)

    text = _rendered_text(row)
    assert "()" not in text and "Invalid Date" not in text and "None" not in text
    assert text.strip() == "● hi", f"rendered {text!r}"


def test_the_formatter_is_one_reading_of_one_value():
    """`HH:MM`, and nothing else — both row kinds use this, so they cannot diverge."""
    assert format_message_time("2026-10-09T09:25:00.123456") == "09:25"
    assert format_message_time("2026-10-09T23:05:59") == "23:05"
    assert format_message_time("2026-10-09T09:25:00Z") == "09:25"
    assert format_message_time(datetime(2026, 10, 9, 9, 25)) == "09:25"
    for unreadable in (None, "", "nonsense", 42):
        assert format_message_time(unreadable) == ""


def test_the_chat_history_threads_the_moment_to_the_row_it_builds():
    """The wiring, not the widgets: `add` is the only door a row comes through."""
    chat = ChatHistory()
    row = chat.add("assistant", "hi", timestamp="2026-10-09T09:25:00")

    assert row is chat.rows[-1], "add must return the row it appended"
    assert "09:25" in _rendered_text(row)


def test_the_chat_history_leaves_an_unknown_moment_empty():
    chat = ChatHistory()
    row = chat.add("user", "hi")

    assert row.timestamp is None
    assert "09:25" not in _rendered_text(row)


# ── the client: the row a frame fills is the row its request drew ───────────
#
# The client echoes a message the instant it is typed and parks the row until the
# daemon says when it was written. One slot could not hold two rows, and the frame
# is a broadcast, so a peer's turn reaches this client too — the two ways a row was
# filled by something that did not cause it (finding on #1978).


def _park_two() -> tuple[ParkedUserRows, UserMarkdown, UserMarkdown]:
    parked = ParkedUserRows()
    first, second = UserMarkdown("first"), UserMarkdown("second")
    parked.park("req-first", first)
    parked.park("req-second", second)
    return parked, first, second


def test_a_second_submit_does_not_displace_the_row_still_waiting():
    """A turn running long enough for the host to submit again parks two rows."""
    parked, first, second = _park_two()

    assert parked.fill("req-first", "2026-10-09T09:25:00") is True

    assert "09:25" in _rendered_text(first), "the first row never got its moment"
    assert "09:25" not in _rendered_text(second), (
        "the reply to one message stamped the row of another"
    )


def test_the_second_row_is_filled_by_its_own_frame_afterwards():
    """Order is the daemon's, not the parking's: either row may be answered first."""
    parked, first, second = _park_two()

    parked.fill("req-second", "2026-10-09T09:26:00")
    parked.fill("req-first", "2026-10-09T09:25:00")

    assert "09:25" in _rendered_text(first)
    assert "09:26" in _rendered_text(second)


def test_a_frame_for_a_request_this_client_never_drew_fills_nothing():
    """The frame is a broadcast: a peer client's turn, or a scheduled cycle's.

    Filling "the last parked row" would stamp this client's own row with a
    stranger's moment — and the row it really belongs to would then have none left.
    """
    parked, first, _ = _park_two()

    assert parked.fill("someone-elses-request", "2026-10-09T09:25:00") is False
    assert "09:25" not in _rendered_text(first), (
        "a peer's frame stamped a row it did not cause"
    )
    assert parked.fill("req-first", "2026-10-09T09:31:00") is True, (
        "a stranger's frame consumed the parked row, so its own frame can never fill it"
    )
    assert "09:31" in _rendered_text(first)


def test_a_frame_that_reports_no_moment_leaves_the_row_without_a_clock():
    """An absence is rendered as an absence, never replaced by a local `now()`.

    A daemon that could not report an instant is a fact; a client that invented one
    would be the second clock this whole feature exists to remove.
    """
    parked = ParkedUserRows()
    row = UserMarkdown("typed while busy")
    parked.park("req-a", row)

    assert parked.fill("req-a", "") is True, "the frame did answer this request"
    assert row.timestamp is None, (
        f"an empty moment became {row.timestamp!r}; the row must stay clockless"
    )
    assert "09:25" not in _rendered_text(row)
    assert len(parked) == 0, "the frame was the only one coming — the row left custody"


def test_clearing_drops_every_row_that_was_waiting():
    """A cleared or replaced session owes no moments: its transcript is gone."""
    parked, _, _ = _park_two()

    parked.clear()

    assert len(parked) == 0
    assert parked.fill("req-first", "2026-10-09T09:25:00") is False
