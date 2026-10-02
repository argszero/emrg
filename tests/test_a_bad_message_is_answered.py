"""A malformed message must be answered — never fatal to the connection.

Measured defect (2026-10-02, this tree, the real daemon over a real WebSocket):

    9 of 18 malformed messages ended the client connection with **no frame at all**.
    The close code was 1000 (normal), so at the client a daemon-side `TypeError` is
    indistinguishable from an ordinary network drop — and the daemon's own log line
    says `client error`, blaming the side that did nothing wrong.

Two homes produced all nine:

* the **read loop**'s `cwd` bookkeeping — `if data.get("cwd"): … self._touch_project(cwd)`
  with `os.path.realpath(cwd)` inside, so `{"type": "ping", "cwd": 5}` raised before
  the dispatcher was even reached. That `except Exception` belongs to
  `_handle_client` — the *connection's*, not the message's — so the whole client went
  away over one field;
* the **dispatcher** — a handler that indexes or path-ifies an unvalidated field
  (`read_file.path`, `list_files.path`, `set_model.model`, `rant.project`,
  `trigger_task.name`, `compact.session_id`, `resume_session.cwd` …).

Both are fixed, and this file measures both directions for each: the malformed message
is answered **and** the well-formed ones still behave exactly as before.

The harness is the repo's own: the in-process server from `tests/test_ws_e2e.py`
(mocked LLM, isolated config dir, upgrade tick forced off) — the same reuse
`tests/test_daemon_manager_e2e.py` documents.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosed

from emrg.connect import connect_to_server
from tests.test_ws_e2e import _boot_server

#: Messages that are *well-formed JSON of a known type* and carry one field whose
#: **type** is wrong. Every one of these ended the connection before the fix; every
#: one must now be answered and leave the connection usable.
MALFORMED = [
    ("cwd as a number, on a plain message", {"type": "ping", "cwd": 5}),
    ("cwd as a list, on a plain message", {"type": "ping", "cwd": ["/tmp"]}),
    ("cwd as a number, session scope", {"type": "resume_session", "session_id": "s", "cwd": 5}),
    ("cwd as a number, rename", {"type": "rename_session", "session_id": "s", "cwd": 5, "title": "t"}),
    ("read_file.path as a number", {"type": "read_file", "path": 5}),
    ("read_file.path as a list", {"type": "read_file", "path": [5]}),
    ("list_files.path as a number", {"type": "list_files", "path": 5}),
    ("list_files.path as a list", {"type": "list_files", "path": [5]}),
    ("set_model.model as a number", {"type": "set_model", "model": 5}),
    ("rant.project as a number", {"type": "rant", "message": "x", "project": 5}),
    ("trigger_task.name as a number", {"type": "trigger_task", "name": 5}),
    ("compact.session_id as a number", {"type": "compact", "session_id": 5}),
]

#: Messages that name a handler needing the bad field: those must be answered with an
#: error frame the client can act on, naming the message type.
NEEDS_THE_FIELD = [
    ("read_file.path as a number", {"type": "read_file", "path": 5}),
    ("list_files.path as a list", {"type": "list_files", "path": [5]}),
    ("set_model.model as a number", {"type": "set_model", "model": 5}),
    ("rant.project as a number", {"type": "rant", "message": "x", "project": 5}),
    ("compact.session_id as a number", {"type": "compact", "session_id": 5}),
]


async def _send(ws, payload: dict | str) -> dict:
    """Send one raw payload and return the next frame, failing if the socket dies."""
    await ws.send(payload if isinstance(payload, str) else json.dumps(payload))
    try:
        frame = await asyncio.wait_for(ws.recv(), timeout=10)
    except ConnectionClosed as exc:  # the defect's own shape
        pytest.fail(
            f"the daemon closed the connection on {payload!r} ({exc}) — a malformed "
            "message must be answered with a frame, not by dropping the client"
        )
    except asyncio.TimeoutError:
        pytest.fail(f"no frame came back for {payload!r} — the message was swallowed")
    return json.loads(frame)


def test_every_malformed_message_is_answered_and_the_connection_survives() -> None:
    """All twelve over one connection: each answered, then a ping still gets a pong."""

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, cleanup = await _boot_server(Path(tmp))
            try:
                ws = await connect_to_server()
                try:
                    for label, msg in MALFORMED:
                        frame = await _send(ws, msg)
                        assert isinstance(frame, dict) and frame, (
                            f"{label}: the daemon answered with {frame!r}"
                        )
                    pong = await _send(ws, {"type": "ping"})
                    assert pong.get("type") == "pong", (
                        "after twelve malformed messages the connection is expected to be "
                        f"usable; ping answered {pong!r}"
                    )
                finally:
                    await ws.close()
            finally:
                await cleanup()

    asyncio.run(_test())


def test_a_handler_needing_the_bad_field_says_so() -> None:
    """The answer names the message and the fault — not a bare close."""

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, cleanup = await _boot_server(Path(tmp))
            try:
                ws = await connect_to_server()
                try:
                    for label, msg in NEEDS_THE_FIELD:
                        frame = await _send(ws, msg)
                        error = frame.get("error", "")
                        assert msg["type"] in error, (
                            f"{label}: the frame must name the message that failed; got {frame!r}"
                        )
                finally:
                    await ws.close()
            finally:
                await cleanup()

    asyncio.run(_test())


def test_a_non_string_cwd_is_ignored_rather_than_recorded() -> None:
    """The read loop's rule: only a string is a cwd — and a real one is still recorded.

    Two directions on the same field: a *good* `cwd` still reaches the projects
    registry (so the fix did not disable the bookkeeping), a *bad* one neither crashes
    nor is written.
    """

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, cleanup = await _boot_server(root)
            try:
                ws = await connect_to_server()
                try:
                    good = root / "a-real-project"
                    good.mkdir()
                    await _send(ws, {"type": "ping", "cwd": str(good), "session_id": "s1"})
                    registry = root / "projects.yml"
                    assert registry.exists(), (
                        "a well-formed cwd must still be recorded — the fix must not "
                        "have disabled the project registry"
                    )
                    recorded = registry.read_text(encoding="utf-8")
                    assert os.path.realpath(str(good)) in recorded, (
                        f"the good cwd never reached the registry:\n{recorded}"
                    )

                    # A bad cwd: answered, connection alive, and *not* written.
                    await _send(ws, {"type": "ping", "cwd": 5, "session_id": "s2"})
                    pong = await _send(ws, {"type": "ping"})
                    assert pong.get("type") == "pong", pong
                    assert "s2" not in registry.read_text(encoding="utf-8"), (
                        "a non-string cwd must not be recorded anywhere"
                    )
                finally:
                    await ws.close()
            finally:
                await cleanup()

    asyncio.run(_test())


def test_the_well_formed_paths_still_answer_as_before() -> None:
    """The wrapper must not have swallowed the normal paths — the honest control."""
    #: (payload, a fragment the answer must contain)
    controls = [
        ({"type": "ping"}, ["pong"]),
        ({"type": "no_such_type"}, ["unknown message type"]),
        ("{not json", ["invalid json"]),
        ({"type": "list_rants"}, ["rants_list"]),
    ]

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, cleanup = await _boot_server(Path(tmp))
            try:
                ws = await connect_to_server()
                try:
                    for payload, fragments in controls:
                        frame = await _send(ws, payload)
                        body = json.dumps(frame)
                        for fragment in fragments:
                            assert fragment in body, (
                                f"{payload!r} answered {frame!r}, which does not contain "
                                f"{fragment!r}"
                            )
                finally:
                    await ws.close()
            finally:
                await cleanup()

    asyncio.run(_test())


def test_a_malformed_message_does_not_end_the_connection() -> None:
    """The defect, stated once: the socket stays open (this is the assertion master fails).

    `recv()` on a closed socket raises `ConnectionClosed`; on master the very first
    malformed message closes it, so this fails with the close, not with a missing
    fragment.
    """

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, cleanup = await _boot_server(Path(tmp))
            try:
                ws = await connect_to_server()
                try:
                    await _send(ws, {"type": "ping", "cwd": 5})
                    assert ws.state.name == "OPEN", f"the socket is {ws.state}"
                finally:
                    await ws.close()
            finally:
                await cleanup()

    asyncio.run(_test())
