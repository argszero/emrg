"""A turn ends with exactly one terminal frame, whatever ended it.

Rant 2026-09-27T19:41:13 (host P0), requirement 4. The clients clear "running"
on a frame carrying ``done``, and the scheduler's read loop is a `while True`
that leaves only on ``resp.get("done")`` — so a turn that dies without one reads
as running forever: the client spins, the task handler never returns, and the
only recovery is a daemon restart. Measured 2026-09-28: a task wedged for 64
minutes with no orphan process left to blame, because what was lost was this
frame and nothing else.

The guarantee lives in `_run_tool_loop_locked`'s `finally` — the one exit every
turn passes through — and not in the six emission sites inside the loop, which
is why these tests drive the wrapper with a loop that is *replaced*: what is
under test is the wrapper's judgement about whether the turn spoke, not the
loop's ability to speak.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server.daemon import EmrgServer
from emrg.session import Session


class _FakeWs:
    """Minimal subscriber: records what the daemon sends it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))


def _server() -> EmrgServer:
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    # Nothing here touches the projects log, but pointing it at a temporary path
    # is free and keeps a mistaken future edit from writing the host's real
    # ~/.emrg/projects.yml (the 2026-08-13 leak).
    import tempfile  # noqa: PLC0415 — local to the fixture helper

    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def _subscribe(server: EmrgServer, session: Session) -> _FakeWs:
    ws = _FakeWs()
    # `_broadcast` filters turn frames by the task's cwd, so the subscriber has
    # to be registered with the session's own — the ghost-connection rule of
    # 2026-08-25T17:38:56, which a wrong cwd would silently turn into "the
    # daemon sent nothing".
    server._session_subscribers[session.session_id] = {ws: str(session.cwd)}
    return ws


def _drive(server: EmrgServer, session: Session, loop, cancel: asyncio.Event | None = None):
    """Run one wrapper turn with `loop` standing in for the tool loop."""
    server._run_tool_loop = loop
    req = TaskRequest(id="req-terminal", session_id=session.session_id, prompt="hello")
    asyncio.run(
        server._run_tool_loop_locked(req, None, session, cancel, allow_tools=True)
    )
    return req


def _terminal_frames(ws: _FakeWs) -> list[dict]:
    """Every frame the client would read as "the turn is over"."""
    return [f for f in ws.sent if f.get("done")]


class _LoopDied(BaseException):
    """The defect's own class: a `BaseException`, which `except Exception` misses.

    Modelled rather than borrowed from `CancelledError` so the test says what it
    is about — the loop's caller is a `create_task` nobody awaits, so this is the
    shape that reaches no log, no client and no handler.
    """


def test_a_turn_whose_loop_dies_still_tells_the_client_it_is_over(tmp_path):
    """The requirement, at the exit that is supposed to make it unconditional."""
    server = _server()
    session = Session.create_with_id("dies", tmp_path)
    ws = _subscribe(server, session)

    async def dying_loop(*args, **kwargs):
        raise _LoopDied("the loop never reached a done frame")

    _drive(server, session, dying_loop)

    terminal = _terminal_frames(ws)
    assert len(terminal) == 1, (
        f"expected exactly one terminal frame, got {len(terminal)}: {ws.sent}"
    )
    frame = terminal[0]
    assert frame.get("request_id") == "req-terminal", (
        "a done frame the client cannot attribute to its request is one the TUI "
        "discards"
    )
    assert frame.get("cancelled") is False, (
        "a turn that died is not a turn the user cancelled — saying so would "
        "misreport an error as an ESC"
    )
    errors = [f for f in ws.sent if f.get("error")]
    assert errors and "_LoopDied" in errors[0]["error"], (
        f"the reason must reach the client, not just the daemon log: {ws.sent}"
    )
    assert errors[0].get("request_id") == "req-terminal", (
        "and it must name the turn it is about: this frame is broadcast to the "
        "whole session, so a session's other requests receive it too — unnamed, "
        "a request waiting in the pending queue read the *holding* turn's "
        f"failure as its own (measured 2026-10-02): {errors[0]}"
    )
    assert ws.sent[-1].get("type") == "turn_end", (
        "the terminal frame belongs before the turn's own end marker"
    )
    assert server._session_busy[session.session_id] is False
    assert server._session_terminal_frame == {}, (
        "the turn's record must not outlive the turn"
    )


def test_a_turn_whose_loop_says_done_gets_no_second_frame(tmp_path):
    """The other direction, without which the guarantee is a duplicate-frame bug.

    A wrapper that always emitted its fallback would satisfy the test above
    while every ordinary turn — the overwhelming majority — ended twice, which
    clients would render as a turn that finishes and then finishes again.
    """
    server = _server()
    session = Session.create_with_id("normal", tmp_path)
    ws = _subscribe(server, session)

    async def ending_loop(*args, **kwargs):
        await server._broadcast(session.session_id, {
            "request_id": "req-terminal",
            "content": "the answer",
            "done": True,
            "delta": False,
            "session_id": session.session_id,
        })

    _drive(server, session, ending_loop)

    terminal = _terminal_frames(ws)
    assert len(terminal) == 1, f"the loop's own done was doubled: {ws.sent}"
    assert terminal[0]["content"] == "the answer", (
        "the fallback replaced a real answer with an empty frame"
    )
    assert not [f for f in ws.sent if f.get("error")]


def test_a_cancelled_turn_is_reported_as_cancelled(tmp_path):
    """A turn that ends because it was cancelled must say so.

    The cancellation branch of the fallback carries `cancelled: True`, which is
    what the TUI reads to decide whether a held receipt is spent — reporting it
    as an error would make an ESC look like a crash.
    """
    server = _server()
    session = Session.create_with_id("cancelled", tmp_path)
    ws = _subscribe(server, session)
    cancel = asyncio.Event()

    async def cancelled_loop(*args, **kwargs):
        # A loop that unwinds while the cancel flag is set, without speaking:
        # the shape a cancellation that arrives between rounds leaves behind.
        cancel.set()

    req = TaskRequest(id="req-terminal", session_id=session.session_id, prompt="hello")
    server._run_tool_loop = cancelled_loop
    asyncio.run(
        server._run_tool_loop_locked(req, None, session, cancel, allow_tools=True)
    )

    terminal = _terminal_frames(ws)
    assert len(terminal) == 1, f"expected exactly one terminal frame: {ws.sent}"
    assert terminal[0].get("cancelled") is True, (
        f"a cancelled turn was reported as something else: {terminal[0]}"
    )
    assert not [f for f in ws.sent if f.get("error")], (
        "a cancellation is not an error to show the user"
    )


def test_a_raised_cancelled_error_also_ends_the_turn(tmp_path):
    """`CancelledError` is a `BaseException`, so the wrapper must catch it too.

    This is the exact class the defect arrived as (`_collect` re-raised it on an
    already-cancelled task) — the reason `except Exception` was not enough. The
    frame is emitted on the way out even though the cancellation itself must
    still propagate.
    """
    server = _server()
    session = Session.create_with_id("cancelled-error", tmp_path)
    ws = _subscribe(server, session)

    async def raising_loop(*args, **kwargs):
        raise asyncio.CancelledError()

    req = TaskRequest(id="req-terminal", session_id=session.session_id, prompt="hello")
    server._run_tool_loop = raising_loop
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            server._run_tool_loop_locked(req, None, session, None, allow_tools=True)
        )

    terminal = _terminal_frames(ws)
    assert len(terminal) == 1, (
        f"a CancelledError reached neither the client nor a handler: {ws.sent}"
    )
    assert terminal[0].get("cancelled") is True
    assert server._session_busy[session.session_id] is False


def test_a_second_turn_on_the_same_session_is_guarded_too(tmp_path):
    """The record is per turn, not per session.

    A normal turn leaves "this session has said done"; the next turn on the same
    session must not inherit that answer, or the first failure of an already-used
    session would be the one shape the guarantee missed — and the sessions that
    matter (a scheduled task's) are reused every cycle.
    """
    server = _server()
    session = Session.create_with_id("two-turns", tmp_path)
    ws = _subscribe(server, session)

    async def ending_loop(*args, **kwargs):
        await server._broadcast(session.session_id, {
            "request_id": "req-terminal", "content": "first", "done": True,
        })

    _drive(server, session, ending_loop)

    async def dying_loop(*args, **kwargs):
        raise _LoopDied("second turn")

    _drive(server, session, dying_loop)

    terminal = _terminal_frames(ws)
    assert len(terminal) == 2, (
        f"the second turn inherited the first turn's answer: {ws.sent}"
    )
    assert terminal[1].get("cancelled") is False
    assert [f for f in ws.sent if f.get("error")], "the second turn's failure was silent"


def test_a_done_frame_from_outside_a_turn_does_not_silence_the_next_one(tmp_path):
    """The reset at turn start is load-bearing, and this is the state it defends.

    `_broadcast` records any `done` it sends for a session — it has to, because
    that is the only path the loop's six emissions take — so the record is
    keyed by session and could outlive a turn. Today no path *outside* the tool
    loop broadcasts a `done` (measured: every `done: True` in `daemon.py` sits
    inside `_run_tool_loop`), so the state cannot in fact arrive this way yet.
    The frame below is built through `_broadcast` itself rather than invented,
    because that is the surface that could grow a second caller: the reset is
    what keeps the guarantee per turn instead of depending on that staying true,
    and a guarantee that holds only for a session's first turn is the worst
    shape for one.
    """
    server = _server()
    session = Session.create_with_id("stray", tmp_path)
    ws = _subscribe(server, session)

    async def stray_done():
        await server._broadcast(session.session_id, {"done": True, "content": "compact"})

    asyncio.run(stray_done())
    assert server._session_terminal_frame, (
        "the fixture must really leave the record behind, or this test proves "
        "nothing about the reset"
    )

    async def dying_loop(*args, **kwargs):
        raise _LoopDied("after a stray done")

    _drive(server, session, dying_loop)

    terminal = _terminal_frames(ws)
    assert len(terminal) == 2, (
        f"the stray frame was read as this turn's ending: {ws.sent}"
    )
    assert terminal[-1].get("request_id") == "req-terminal"
    assert [f for f in ws.sent if f.get("error")], "the turn's failure was silent"


def _broadcast_payloads(source: str) -> list[tuple[int, dict]]:
    """Every `_broadcast(...)` payload written as a dict literal, with its line."""
    import ast

    found: list[tuple[int, dict]] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "_broadcast"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Dict)):
            continue
        keys = [k.value if isinstance(k, ast.Constant) else "<**>"
                for k in node.args[1].keys]
        found.append((node.lineno, {k: True for k in keys}))
    return found


def test_every_frame_about_a_turn_names_the_request_it_is_about():
    """The invariant the scheduler's read loop now leans on, made mechanical.

    A session's frames are broadcasts: `daemon.py::_broadcast` reaches every
    subscriber of the session, the originator included. So a frame about a turn
    is read by *every* request in that session, and only the name on it says
    whose turn it is. `turn_start`, the wrapper's fallback error and the loop's
    own LLM-error exit were the three that did not — measured 2026-10-02: a
    cycle whose request sat in the pending queue read the holding turn's `done`
    as its own completion (an EvolutionLog appended for a turn that never ran),
    counted its tool calls into its own progress, and filed its failure as its
    own `server-error`.

    The rule is the one a reader can check without running anything: a payload
    that carries a turn's own words — `done`, `delta`, `tool_name` — or that
    carries no `type` at all (the shape every turn frame has) must name its
    request. Session-level frames (`turn_end`, `queued_*`, `messages_compacted`,
    `session_cancelled`) carry a `type` and no request, and are exempt by the
    same sentence.
    """
    from emrg.server import daemon as daemon_mod

    # The file of the module under test, not a relative path: a relative one
    # resolves against the cwd and, in a probe run as a script, against the
    # *installed* copy rather than this tree (measured 2026-10-02).
    source = Path(daemon_mod.__file__).read_text()
    payloads = _broadcast_payloads(source)
    assert len(payloads) > 20, (
        "the scan found almost no broadcast payloads — the file it read is not "
        f"the daemon: {len(payloads)}"
    )
    unnamed = [
        (line, keys) for line, keys in payloads
        if "request_id" not in keys
        and ("type" not in keys or any(k in keys for k in ("done", "delta", "tool_name")))
    ]
    assert not unnamed, (
        "these broadcast payloads are about a turn but do not name it, so any "
        f"request in the session may read them as its own: {unnamed}"
    )
