"""A malformed message must be answered — never fatal to the connection.

Measured defect (2026-10-02, this tree, the real daemon over a real WebSocket):

    a message whose JSON is valid and whose type is known, carrying one field of the
    wrong **type**, ended the client connection with **no frame at all** — close code
    1000 (normal), so at the client a daemon-side `TypeError` is indistinguishable from
    an ordinary network drop, while the daemon's own log line said `client error`,
    blaming the side that did nothing wrong.

The surface is wide, and measuring it is how it was found: every handler's field names
were parsed out of `daemon.py` and each was sent with a wrong-typed value — **83 of 138
probes tore the connection down** before the fix. Two homes produced all of them:

* the **read loop**'s bookkeeping, which *records* two fields and validates neither —
  `cwd` reaches `os.path.realpath` and `session_id` becomes a **dict key** in
  `_session_subscribers`, so `{"cwd": 5}` and `{"session_id": [1]}` are two shapes of
  one bug: that `except Exception` belongs to `_handle_client` — the *connection's*,
  not the message's. (Guarding only `cwd` left 12 of the 138 still dying, every one of
  them a list `session_id`, which is why this file pins both fields.)
* the **dispatcher** — a handler that indexes or path-ifies an unvalidated field
  (`read_file.path`, `list_files.path`, `set_model.model`, `rant.project`,
  `trigger_task.name`, `compact.session_id`, …).
* the three branches the read loop handles **inline**, keyed off
  `data.get("type") ==` instead of dispatching on `msg_type ==` (`task`, `cancel`,
  `approval_response`). A whole-surface probe that enumerated field names with the
  regex `msg_type == "..."` never saw them — 41 branches against 44 — so the first
  fix left them dying; probed directly, **8 of 15** messages over these three closed
  the socket, and one of them (`task` with a number `prompt`) died a whole screen
  below the branch, at `req.prompt[:60]` in the `task received:` log line.

All three are fixed, and this file measures both directions for each: the malformed
message is answered **and** the well-formed ones still behave exactly as before.

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
    ("session_id as a list, on a plain message", {"type": "ping", "session_id": ["s"]}),
    ("session_id as a dict, on a plain message", {"type": "ping", "session_id": {"s": 1}}),
    ("session_id as a list, with a cwd", {"type": "ping", "session_id": [1], "cwd": "/tmp"}),
    ("cwd as a number, session scope", {"type": "resume_session", "session_id": "s", "cwd": 5}),
    ("cwd as a number, rename", {"type": "rename_session", "session_id": "s", "cwd": 5, "title": "t"}),
    ("session_id as a list, rename", {"type": "rename_session", "session_id": [1], "cwd": "/tmp"}),
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


#: The fields the read loop's three **inline** branches read, each with a wrong type.
#: The second element says what a correct daemon does with it: a `task` missing a
#: usable `session_id`/`cwd` is *refused* (the branch already had a refusal path, it
#: just never got a usable value to test), while the rest are read and the message is
#: handled — so there liveness is the only thing to assert.
INLINE_BRANCH_FIELDS = [
    ("task.session_id as a list", {"type": "task", "session_id": [1], "cwd": "/tmp", "prompt": "hi"}, "refused"),
    ("task.session_id as a dict", {"type": "task", "session_id": {"a": 1}, "cwd": "/tmp", "prompt": "hi"}, "refused"),
    ("task.session_id as a number", {"type": "task", "session_id": 5, "cwd": "/tmp", "prompt": "hi"}, "refused"),
    ("task.cwd as a list", {"type": "task", "session_id": "s", "cwd": [1], "prompt": "hi"}, "refused"),
    ("task.cwd as a number", {"type": "task", "session_id": "s", "cwd": 5, "prompt": "hi"}, "refused"),
    ("task.prompt as a number", {"type": "task", "session_id": "s", "cwd": "/tmp", "prompt": 5}, "handled"),
    ("cancel.session_id as a list", {"type": "cancel", "session_id": [1]}, "handled"),
    ("cancel.session_id as a dict", {"type": "cancel", "session_id": {"a": 1}}, "handled"),
    (
        "approval_response.request_id as a list",
        {"type": "approval_response", "request_id": [1], "approved": True},
        "handled",
    ),
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


async def _drain_until_pong(ws, payload: dict) -> dict:
    """Send a message that may legitimately answer with *nothing*, then prove the
    connection still works: read frames until the ping's pong arrives.

    `cancel` and `approval_response` are quiet by contract (they change state, they do
    not narrate), and a `task` that survives starts a turn whose frames arrive on their
    own schedule — so "no frame" is not the assertion here and a bare `recv()` would
    read the wrong one. The failing direction is still `ConnectionClosed`, which is
    exactly the defect: the socket dies and the ping can never be answered.
    """
    await ws.send(json.dumps(payload))
    await ws.send(json.dumps({"type": "ping"}))
    for _ in range(60):
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout=10)
        except ConnectionClosed as exc:  # the defect's own shape
            pytest.fail(
                f"the daemon closed the connection on {payload!r} ({exc}) — a malformed "
                "message must be answered with a frame, not by dropping the client"
            )
        except asyncio.TimeoutError:
            pytest.fail(f"the ping never came back after {payload!r} — the connection is wedged")
        data = json.loads(frame)
        if data.get("type") == "pong":
            return data
    pytest.fail(f"no pong within 60 frames after {payload!r}")


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


def test_a_bad_session_id_is_not_recorded_as_a_subscription() -> None:
    """The sibling of the `cwd` rule, in the same block: a session id is a string.

    `session_id` does not reach `realpath` — it becomes a **dict key** in
    `_session_subscribers`, so the shape that kills the connection is the one an
    `os.path` call rejects for a different reason: an unhashable value. Measured
    2026-10-02: with `cwd` guarded but `session_id` not, `{"type":"ping",
    "session_id":[1]}` still closed the socket (12 of 138 probes remained), which is
    why this test pins *this* field and not only its neighbour.

    Two directions again: a good id is recorded, a bad one is not — and, crucially,
    the connection is still there afterwards.
    """

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server, _, cleanup = await _boot_server(root)
            try:
                ws = await connect_to_server()
                try:
                    await _send(ws, {"type": "ping", "session_id": "good", "cwd": str(root)})
                    assert "good" in server._session_subscribers, (
                        "a well-formed session_id must still be recorded — the fix must "
                        f"not have disabled the subscription; got {list(server._session_subscribers)}"
                    )

                    await _send(ws, {"type": "ping", "session_id": ["bad"]})
                    for key in server._session_subscribers:
                        assert isinstance(key, str), (
                            f"a non-string session_id became a subscription key: {key!r}"
                        )
                    pong = await _send(ws, {"type": "ping"})
                    assert pong.get("type") == "pong", pong
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


def test_the_inline_branches_answer_a_wrong_typed_field() -> None:
    """`task` / `cancel` / `approval_response` are part of the same rule.

    They are the read loop's own branches (`data.get("type") ==`), not dispatcher
    handlers, and the first fix — written against a field enumeration that could not
    see them — left all three dying: 8 of 15 wrong-typed messages over these branches
    closed the socket with code 1000 and no frame.

    A fresh connection per case, because a `task` that is *accepted* starts a turn and
    this test is about the branches that must not; the control is that the same socket
    still answers a ping afterwards.
    """

    async def _test() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, cleanup = await _boot_server(root)
            try:
                for label, msg, expectation in INLINE_BRANCH_FIELDS:
                    ws = await connect_to_server()
                    try:
                        if expectation == "refused":
                            frame = await _send(ws, msg)
                            assert msg["type"] in frame.get("error", ""), (
                                f"{label}: a task without a usable session_id/cwd must be "
                                f"refused by name, not started; got {frame!r}"
                            )
                            pong = await _send(ws, {"type": "ping"})
                            assert pong.get("type") == "pong", f"{label}: {pong!r}"
                        else:
                            pong = await _drain_until_pong(ws, msg)
                            assert pong.get("type") == "pong", f"{label}: {pong!r}"
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
