"""One queued message can be withdrawn, and the queue says what is left.

Every removal path before this popped the whole pending list, so a client could express
only "drop the queue" — and the interface had no delete control to offer, because a
button with no protocol behind it does nothing. A queue also carried a position computed
once at enqueue and never recomputed, so a position left over from before a removal
reports a place that no longer exists.

Two things are pinned here, and they are one subject: a withdrawal names **one** request
and the answer carries the queue as it now stands, so the client redraws rather than
renumbering its own copy. Two computers of one number is how they come to disagree.

`_process_message` is driven directly with a writer stand-in, the way the rest of this
suite does it. No daemon is started, stopped or restarted.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server.daemon import EmrgServer, _queue_preview


class _FakeWriter:
    """Minimal writer stand-in: `_send` reaches the client through `ws.send`."""

    def __init__(self) -> None:
        self._frames: list[bytes] = []

    async def send(self, data) -> None:
        if isinstance(data, (bytes, bytearray)):
            self._frames.append(bytes(data))
        else:
            self._frames.append(data.encode() if isinstance(data, str) else data)


def _server() -> EmrgServer:
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    # Never the host's real register: a test that writes it has already failed outside
    # its own assertions.
    server._agents_log = Path(tempfile.mkdtemp()) / "agents.yml"
    return server


def _drive(server: EmrgServer, msg: dict) -> list[dict]:
    """One `_process_message` call, returning the frames it sent."""
    writer = _FakeWriter()
    asyncio.run(server._process_message(msg, writer))  # type: ignore[arg-type]
    return [json.loads(raw) for raw in writer._frames]


def _queue(server: EmrgServer, session_id: str, *ids: str) -> None:
    """Put `ids` in the session's pending queue, in order."""
    server._session_pending[session_id] = [
        (TaskRequest(id=rid, session_id=session_id, prompt=f"prompt {rid}"), True) for rid in ids
    ]


def _positions(server: EmrgServer, session_id: str) -> list[tuple[str, int]]:
    pending = server._session_pending.get(session_id) or []
    view = EmrgServer._queued_view(session_id, pending)
    return [(row["request_id"], row["position"]) for row in view]


# ── the withdrawal itself ─────────────────────────────────────────


def test_withdrawing_one_leaves_the_others_and_renumbers_them():
    """The queue the answer carries is the one that exists, numbered from 1.

    The remaining messages are asserted **by name**, not by count: a withdrawal that
    removed the wrong element leaves the same length behind.
    """
    server = _server()
    _queue(server, "s1", "r1", "r2", "r3")

    frames = _drive(server, {"type": "cancel_queued", "session_id": "s1", "request_id": "r1"})

    assert _positions(server, "s1") == [("r2", 1), ("r3", 2)]
    ack = frames[-1]
    assert ack["type"] == "queued_removed"
    assert ack["changed"] is True
    assert [row["request_id"] for row in ack["queue"]] == ["r2", "r3"]
    assert [row["position"] for row in ack["queue"]] == [1, 2]


def test_the_last_one_leaves_no_empty_queue_behind():
    """An empty list and an absent key must not both mean "nothing queued".

    Every other reader treats them alike, so keeping the key would make
    `_session_pending` grow one entry per session that ever queued — a leak nothing
    would notice until a long-running daemon had a few thousand of them.
    """
    server = _server()
    _queue(server, "s1", "r1")

    _drive(server, {"type": "cancel_queued", "session_id": "s1", "request_id": "r1"})

    assert "s1" not in server._session_pending


def test_withdrawing_something_not_queued_changes_nothing():
    """Not an error: the message may have been injected or withdrawn a moment earlier.

    The queue in the answer still says what is true now, which is the only thing the
    interface needs — a refusal here would show a host an error for a race they did not
    cause and cannot act on.
    """
    server = _server()
    _queue(server, "s1", "r1", "r2")

    frames = _drive(server, {"type": "cancel_queued", "session_id": "s1", "request_id": "gone"})

    assert _positions(server, "s1") == [("r1", 1), ("r2", 2)]
    ack = frames[-1]
    assert ack["changed"] is False
    assert [row["request_id"] for row in ack["queue"]] == ["r1", "r2"]


def test_a_frame_missing_a_field_is_refused_and_named():
    """The refusal says which field is missing, so the reader does not have to guess."""
    server = _server()
    _queue(server, "s1", "r1")

    frames = _drive(server, {"type": "cancel_queued", "session_id": "s1"})

    assert frames[-1]["type"] == "queued_removed"
    assert "request_id" in frames[-1]["error"]
    # …and nothing was removed on the way to saying so.
    assert _positions(server, "s1") == [("r1", 1)]


def test_a_withdrawal_is_broadcast_so_a_second_client_sees_it():
    """A queue belongs to the session, not to the connection that typed into it."""
    server = _server()
    _queue(server, "s1", "r1", "r2")
    broadcasts: list[tuple[str, dict]] = []

    async def _record(session_id: str, data: dict) -> None:
        broadcasts.append((session_id, data))

    server._broadcast = _record  # type: ignore[method-assign]
    _drive(server, {"type": "cancel_queued", "session_id": "s1", "request_id": "r1"})

    assert [(sid, data["type"]) for sid, data in broadcasts] == [("s1", "queue_changed")]
    assert [row["request_id"] for row in broadcasts[0][1]["queue"]] == ["r2"]


# ── the view a client draws ───────────────────────────────────────


def test_the_view_numbers_from_one_and_carries_the_opening_words():
    server = _server()
    _queue(server, "s1", "r1", "r2")

    view = EmrgServer._queued_view("s1", server._session_pending["s1"])

    assert [row["position"] for row in view] == [1, 2]
    assert [row["preview"] for row in view] == ["prompt r1", "prompt r2"]


def test_an_empty_queue_is_an_empty_view():
    """`[]`, not a missing key and not a crash — the shape a client redraws from."""
    assert EmrgServer._queued_view("s1", []) == []


def test_a_preview_is_one_line_and_marks_where_it_was_cut():
    """A multi-line prompt must not break the summary row, and a cut must be visible.

    An unmarked cut is the worse half: the fragment reads as a whole sentence, so a host
    comparing two queued messages would compare two truncations and think they match.
    """
    assert _queue_preview("line one\nline two") == "line one line two"

    long = "x" * 200
    preview = _queue_preview(long)
    assert preview.endswith("…")
    assert len(preview) == 61  # 60 characters plus the ellipsis
    assert preview[:-1] == long[:60]


def test_a_preview_that_fits_is_left_alone():
    exact = "y" * 60
    assert _queue_preview(exact) == exact
    assert not _queue_preview(exact).endswith("…")


def test_nothing_to_preview_is_an_empty_string():
    """A prompt that is empty or whitespace gets no invented placeholder."""
    assert _queue_preview("") == ""
    assert _queue_preview("   \n\t ") == ""
