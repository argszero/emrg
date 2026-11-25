"""A message's pictures survive the trip from the record to a reopening client.

Host 2026-11-25T18:17:18: a picture sent from the GUI chat window reached the
conversation as the literal text `[📷 label]` — no picture, and nowhere to click to
see or download the original.

The bytes were never lost. They sit in `<cwd>/.emrg/sessions/<sid>/images/`, and the
record names them: the write side puts `images` on the user record next to the text.
What was missing is on the **reading** side — `list_history`'s record projection took
`content` and `timestamp` and dropped `images`, so a client that reopens a session
receives a message with no picture in it at all, however the interface draws it.

So the subjects here are the payload a client actually receives:

* **the projection carries the record's images**, unchanged — read through the real
  `list_history` handler with `include_records`, because that is what a reopening
  client replays.
* **the absence.** A record written before attachments projected `images` would be a
  key the record never had; it must project exactly as it did, the rule issue #2980
  established one field over.

No daemon is started, stopped or restarted: `_process_message` is called directly with
the websocket stubbed, the way `tests/test_tool_call_batch_label.py` does.
"""

from __future__ import annotations

import asyncio
import json

from emrg.config import LlmConfig
from emrg.server.daemon import EmrgServer, _projected_images
from emrg.session import Session

IMAGE = {
    "path": "/tmp/s_260101_0000_aaaa/images/shot_1a2b3c4d.png",
    "label": "[📷 shot]",
    "mime": "image/png",
    "position": 0,
}


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


def _history_messages(tmp_path, records: list[dict]) -> list[dict]:
    """Run `records` through the real `list_history` handler."""
    server = _make_server(tmp_path)
    session = Session.create_with_id("images-history", tmp_path)
    for record in records:
        session.append_message(record)
    ws = _FakeWs()
    asyncio.run(server._process_message(
        {"type": "list_history", "session_id": session.session_id,
         "cwd": str(tmp_path), "include_records": True},
        ws,
    ))
    frames = [f for f in ws.sent if f.get("type") == "history_list"]
    assert frames, f"no history_list frame was sent: {ws.sent}"
    return frames[0]["messages"]


def test_the_projection_carries_the_images_the_message_was_sent_with(tmp_path):
    """Replay reads the record; an image only on the live frame never reaches it."""
    messages = _history_messages(tmp_path, [
        {
            "type": "message",
            "role": "user",
            "content": "[📷 shot] what is this?",
            "timestamp": "2026-11-25T18:17:18",
            "images": [IMAGE],
        },
    ])

    users = [m for m in messages if m.get("kind") == "message" and m.get("role") == "user"]
    assert len(users) == 1, messages
    assert users[0].get("images") == [IMAGE], (
        "the history projection dropped the recorded images, so a reopened session "
        f"replays a message with no picture: {users[0]!r}"
    )


def test_a_record_written_without_images_projects_without_the_key(tmp_path):
    """An old session must replay exactly as it did — not with `images: []`."""
    messages = _history_messages(tmp_path, [
        {
            "type": "message",
            "role": "user",
            "content": "no picture here",
            "timestamp": "2026-11-20T09:00:00",
        },
    ])

    users = [m for m in messages if m.get("kind") == "message" and m.get("role") == "user"]
    assert len(users) == 1, messages
    assert "images" not in users[0], (
        f"a record with no images gained an `images` key in projection: {users[0]!r}"
    )


def test_the_projection_hands_out_a_copy_of_the_images():
    """What a client is given is a copy of the record's list, entry by entry.

    Read **before** serialisation, because after it there is nothing left to see: a
    frame travels through `json.dumps`/`json.loads` in `_FakeWs`, which replaces every
    object, so a mutation made through the frame can never reach the record however
    the projection behaves. The earlier version of this test read the frame, made
    exactly that edit, and passed with no copy in place — it was measuring the
    round-trip. The subject is the projection's own return value.
    """
    sent = dict(IMAGE)
    record = {"type": "message", "role": "user", "content": "[📷 shot] hi", "images": [sent]}
    projected = _projected_images(record)

    assert projected == [IMAGE], "the projection lost the pictures on the way out"
    assert projected is not record["images"], (
        "the projection handed out the record's own list, so a client can rewrite a "
        "message's attachments in the session it is reading"
    )
    projected[0]["label"] = "[📷 tampered]"
    assert sent["label"] == "[📷 shot]", (
        "an edit through the projection reached the record's own dict"
    )


def test_a_record_without_images_projects_no_images_through_the_helper():
    """The helper answers `None`, which is what keeps the key absent for old records."""
    assert _projected_images({"content": "no picture here"}) is None
    assert _projected_images({"images": []}) is None
