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
from emrg.client.python_tui.widgets.chat_row import ChatRow, clock_width, format_message_time
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
    server._agents_log = Path(tempfile.mkdtemp()) / "agents.yml"
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


# ── the client: the clock lands inside the width it is painted into ────────
#
# Issue #2668. The readings above prove the clock is *carried*: they join a row's
# span text and ask whether `09:25` is in it — which an overflowing tail satisfies.
# So the suite stayed green while the host saw no time at all, on every path, for
# every message: the clock is appended after the content has already been wrapped to
# the full width, so it begins past the last column and the terminal clips it.
#
# The only reading that decides whether a person can see the clock is the rendered
# line against the terminal it is painted into. Both halves are asserted, because
# either one alone is satisfiable by a broken row: a line that fits can have padded
# the clock off the end, and a line carrying the clock can be too wide to show it.

_GAP = "  "
_WIDTHS = (60, 80, 100, 120)
_LONG = "word " * 60


def _assert_clock_inside(rows, width: int, label: str) -> None:
    """Every rendered line fits `width`, and the first carries the clock after the text."""
    for row in rows:
        lines = row.render(_ctx(width))
        assert lines, f"{label}: rendered no lines at width {width}"
        for index, line in enumerate(lines):
            text = "".join(span.text for span in line.spans)
            assert len(text) <= width, (
                f"{label}: line {index} is {len(text)} cells wide at terminal width {width} "
                f"— its tail is clipped, so anything sitting there is invisible: {text!r}"
            )
        first = "".join(span.text for span in lines[0].spans)
        assert "09:25" in first, (
            f"{label}: the clock is not on the row's first line at width {width}: {first!r}"
        )
        body = first[: first.rindex("09:25")]
        before_clock = len(body) - len(body.rstrip(" "))
        assert before_clock == len(_GAP), (
            f"{label}: the clock sits {before_clock} cells after the text at width {width}, "
            f"not the {len(_GAP)}-cell gap — the line's trailing padding is ahead of the "
            f"clock, which is what pushes the moment out of view: {first!r}"
        )


@pytest.mark.parametrize("width", _WIDTHS)
def test_a_live_user_row_keeps_its_clock_inside_the_terminal(width):
    """The row a typed message draws, wrapped to fill the line it has to share."""
    _assert_clock_inside([UserMarkdown(_LONG, timestamp="2026-10-09T09:25:00")],
                         width, "live user message")


@pytest.mark.parametrize("width", _WIDTHS)
def test_a_settled_reply_keeps_its_clock_inside_the_terminal(width):
    """The live assistant row, taken from streaming to the moment the frame lands."""
    row = StreamingMarkdown()
    row.feed(_LONG)
    row.timestamp = "2026-10-09T09:25:00"
    row.dirty = True

    _assert_clock_inside([row], width, "live assistant reply")


@pytest.mark.parametrize("width", _WIDTHS)
def test_a_replayed_transcript_keeps_each_clock_inside_the_terminal(width):
    """`/resume` and `/rewind` rebuild the screen through the app's own replay path.

    Driven through `_replay_rows` / `_paint_replay_rows` rather than by building the
    rows here: a row constructed by this test would prove the widgets, not that the
    transcript a reopen paints is the same reading.
    """
    from emrg.client.app import _paint_replay_rows, _replay_rows

    records = [
        {"kind": "message", "role": "user", "content": _LONG,
         "timestamp": "2026-10-09T09:25:00"},
        {"kind": "message", "role": "assistant", "content": _LONG,
         "timestamp": "2026-10-09T09:25:00"},
    ]
    chat = ChatHistory()
    _paint_replay_rows(chat, _replay_rows(records))

    assert len(chat.rows) == 2, "the replay did not rebuild both halves of the transcript"
    _assert_clock_inside(chat.rows, width, "replayed transcript")


@pytest.mark.parametrize("missing", [None, "", "not a timestamp"])
@pytest.mark.parametrize("kind", ["user", "assistant"])
def test_a_row_with_no_moment_still_fits_the_width_it_is_painted_into(kind, missing):
    """A legacy record renders no clock — and must not reserve columns for one either.

    The absence is the interesting half: a renderer that reserved the clock's width
    unconditionally would wrap a clockless row narrower than the terminal for a
    reason the reader cannot see.
    """
    if kind == "user":
        row = UserMarkdown(_LONG, timestamp=missing)
    else:
        row = StreamingMarkdown()
        row.feed(_LONG)
        row.timestamp = missing or ""

    for width in _WIDTHS:
        lines = row.render(_ctx(width))
        for line in lines:
            text = "".join(span.text for span in line.spans)
            assert len(text) <= width, f"{kind}/{missing!r}: {len(text)} cells at {width}"
        assert ":" not in "".join(s.text for s in lines[0].spans).split(_LONG[:10])[-1], (
            f"{kind}/{missing!r}: a row with no readable moment rendered a clock"
        )


# ── the client: a clockless row reserves no columns for a clock ─────────────
#
# Issue #2672. The reserve is a function of the clock, and a record written before
# the moment was carried has none, so `clock_width("")` must answer 0 — otherwise a
# legacy row wraps short of the terminal for a reason its reader cannot see.
#
# The reading above cannot separate the two cases: `test_a_row_with_no_moment_still_
# fits_the_width_it_is_painted_into` asks only that every line fit and that no clock
# be rendered, and a renderer that reserved the clock's columns unconditionally
# satisfies both — it wraps seven cells short, still fits, and still shows no clock.
# Line count does separate them, and only at some widths, so the widths are found by
# measuring here rather than listed: a hard-coded pair would answer "rich wraps as it
# did on the day this was written" instead of "the reserve follows the clock".


def _row_for(kind: str, timestamp):
    """The row `kind` draws for `_LONG`, with the clock it is or is not given."""
    if kind == "user":
        return UserMarkdown(_LONG, timestamp=timestamp)
    return _streaming_with(_LONG, timestamp)


def _streaming_with(text: str, timestamp):
    row = StreamingMarkdown()
    row.feed(text)
    row.timestamp = timestamp or ""
    row.dirty = True
    return row


def _widest(lines) -> int:
    return max(len("".join(span.text for span in line.spans)) for line in lines)


def test_the_reserved_columns_are_the_clocks_own_and_none_for_an_absent_clock():
    """Both directions of the unit reading: a clock costs its text plus its gap, absent costs 0."""
    assert clock_width("") == 0, (
        "a row with no moment reserves columns for one, so a legacy record wraps short of "
        "the terminal for a reason its reader cannot see (issue #2672)"
    )
    assert clock_width("09:25") == len(_GAP) + len("09:25"), (
        f"the reserve reads {clock_width('09:25')} cells for a five-cell clock behind a "
        f"{len(_GAP)}-cell gap, so the budget a row subtracts is not the clock's own width"
    )


@pytest.mark.parametrize("kind", ["user", "assistant"])
def test_a_clockless_row_wraps_to_the_whole_terminal(kind):
    """The rendered consequence: with no clock to place, the content gets the columns back.

    Measured rather than asserted per width, because the line count separates the two
    rows only at some — with this content and this tree the user row separates at 80 and
    the assistant row at 60, 80 and 100. A renderer that reserved the clock's columns
    unconditionally separates at none of them, so "no width separates" is the failure
    this asks for, and it names the counts it saw.
    """
    separating: list[int] = []
    for width in _WIDTHS:
        clockless = _row_for(kind, None).render(_ctx(width))
        clocked = _row_for(kind, "2026-10-09T09:25:00").render(_ctx(width))
        assert "09:25" in "".join(span.text for span in clocked[0].spans), (
            f"{kind} at width {width}: the clocked row renders no clock, so this width "
            "measures nothing about what a clock costs"
        )
        if len(clockless) < len(clocked):
            separating.append(width)
            assert _widest(clockless) > _widest(clocked), (
                f"{kind} at width {width}: the clockless row takes {len(clockless)} lines "
                f"against {len(clocked)}, but its widest line is {_widest(clockless)} cells "
                f"against {_widest(clocked)} — it is not using the columns the clock would "
                "have taken"
            )

    assert separating, (
        f"the {kind} row renders to the same number of lines with a clock and without one "
        f"at every width in {list(_WIDTHS)}: the content is being wrapped to the same budget "
        "either way, so a row that has no clock is reserving the clock's columns anyway "
        f"(issue #2672; at width {_WIDTHS[1]} the clockless row took "
        f"{len(_row_for(kind, None).render(_ctx(_WIDTHS[1])))} lines and the clocked one "
        f"{len(_row_for(kind, '2026-10-09T09:25:00').render(_ctx(_WIDTHS[1])))}). If the "
        "reserve still follows the clock, then rich is wrapping this content differently "
        "from the day this reading was written — measure again rather than widen the bound"
    )
