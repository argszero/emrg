"""Session-level sandbox tier — one stored value, one writer, one resolver.

Rant 2026-09-30T09:30:16 (host ruling "B", 2026-09-30T09:29:07): the tier is a
property of the **session**, persisted in its ``meta.json``; both clients are
entry points and displays and hold no logic. The default for a session nobody
ever set is ``workspace-write``, applied where a *client's* turn begins —
because the other consumer of the same injection rule, the auto-upgrade session,
keeps its declared silence meaning "unconfined" (``policy.DEFAULT_MODE``, whose
own pins live in ``test_bash_v2_policy.py`` and ``test_upgrade_session_tier.py``).

Two boundaries these tests exist to hold, each with its own guard below:

* the default must land on the client-turn path and **not** in
  ``_inject_tool_arguments``, which the scheduler's turns and the upgrade
  session share;
* the default must not be spelled by changing ``policy.DEFAULT_MODE``.
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import json
import textwrap
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.sandbox.policy import DANGER_FULL_ACCESS, DEFAULT_MODE, SANDBOX_MODES
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer, resolve_client_tier
from emrg.session import Session


class _RecordingWs:
    """A connection that records every frame the daemon sends it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))


def _server() -> EmrgServer:
    return EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))


def _meta(session: Session) -> dict:
    return json.loads(session._meta_path.read_text(encoding="utf-8"))


# ── the stored value ──────────────────────────────────────────────────────


def test_a_session_that_never_declared_a_tier_writes_no_key(tmp_path):
    """Silence is not a stored value.

    The key's absence is load-bearing: it is what tells a client turn (which
    defaults) from the upgrade session's turn (which must not). A writer that
    stamped the default into every ``meta.json`` would erase that difference for
    every session on disk.
    """
    session = Session.create_with_id("s_no_tier", tmp_path)

    assert session.sandbox is None
    assert "sandbox" not in _meta(session)


def test_the_tier_is_persisted_and_survives_a_reload(tmp_path):
    session = Session.create_with_id("s_persist", tmp_path)
    session.set_sandbox("read-only")

    assert _meta(session)["sandbox"] == "read-only"
    # A reload is what a restart looks like: the daemon loads the session from
    # disk on the next turn, so the tier has to come back from the file.
    assert Session.load("s_persist", tmp_path).sandbox == "read-only"


def test_every_other_meta_rewrite_keeps_the_tier(tmp_path):
    """``_save_meta_with_title`` has six callers and each rewrites the file whole.

    A tier carried anywhere but that one writer would be silently dropped by the
    next appended message, rename or compact — so this walks the rewrites a real
    session performs rather than trusting the single writer's name.
    """
    session = Session.create_with_id("s_rewrites", tmp_path)
    session.set_sandbox("workspace-write")

    session.append_message({"type": "message", "role": "user", "content": "hi"})
    assert _meta(session)["sandbox"] == "workspace-write"

    session.rename("a title")
    assert _meta(session)["sandbox"] == "workspace-write"

    session.clear()
    assert _meta(session)["sandbox"] == "workspace-write"


def test_an_unknown_mode_is_refused_and_nothing_is_written(tmp_path):
    """A typo must not become a stored tier every later turn inherits."""
    session = Session.create_with_id("s_bad_mode", tmp_path)
    before = session._meta_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        session.set_sandbox("readonly")

    assert "readonly" in str(excinfo.value)
    assert session.sandbox is None
    assert session._meta_path.read_text(encoding="utf-8") == before
    assert "sandbox" not in _meta(session)


# ── the resolver ──────────────────────────────────────────────────────────


def test_the_sessions_tier_wins_over_the_tier_a_message_carried(tmp_path):
    """The message is not the source of truth — the session is.

    This is the shape the GUI still has (its chip rides every request): once the
    host has set the session's tier, a message that names another one must not
    override it, or "set it in one client and every client agrees" would be false
    for every turn that client sends.
    """
    session = Session.create_with_id("s_wins", tmp_path)
    session.set_sandbox("read-only")

    assert resolve_client_tier(session, "danger-full-access") == "read-only"


def test_a_message_tier_still_counts_when_the_session_has_none(tmp_path):
    """The fallback an older client relies on: no regression while it exists."""
    session = Session.create_with_id("s_fallback", tmp_path)

    assert resolve_client_tier(session, "danger-full-access") == DANGER_FULL_ACCESS


def test_a_session_with_no_tier_and_no_message_tier_gets_the_ruling_default(tmp_path):
    """Host ruling B: a session nobody has set runs confined to its workspace."""
    session = Session.create_with_id("s_default", tmp_path)

    resolved = resolve_client_tier(session)

    assert resolved == "workspace-write"
    assert resolved in SANDBOX_MODES


def test_the_default_is_not_the_policys_default():
    """The shortcut that would re-kill the auto-upgrade, refused by name.

    Changing ``policy.DEFAULT_MODE`` to ``workspace-write`` would give a client
    session the ruling's default *and* confine the upgrade session, whose silence
    resolving to ``DANGER_FULL_ACCESS`` is a deployment decision its consumer
    declared (23 attempts / 132 minutes / zero bytes written when it was confined
    instead). So the two must stay different values.
    """
    assert DEFAULT_MODE == DANGER_FULL_ACCESS
    assert daemon_mod.SESSION_DEFAULT_SANDBOX_MODE != DEFAULT_MODE


def test_the_client_turn_path_resolves_the_tier_before_the_loop_starts():
    """Where the resolution lives, and where it must not be moved to.

    ``_inject_tool_arguments`` is shared with the scheduler's task turns and with
    the upgrade session, so a session lookup there would hand the task's own tier
    to whichever session happened to be loaded. The client's read loop is the one
    place that knows a turn is a client's, so the call belongs there — asserted
    because a later refactor moving it back into the injection rule would leave
    every other test in this file green.
    """
    loop_source = inspect.getsource(EmrgServer._handle_client)
    assert "resolve_client_tier(session" in loop_source

    inject_source = inspect.getsource(EmrgServer._inject_tool_arguments)
    assert "session.sandbox" not in inject_source
    # The rule is about a **call**, not about the word: measured 2026-10-03
    # (`cyc20261003-090949`), this leg was a substring check on the source text,
    # so a docstring that *explains* who passes the tier (``resolve_client_tier
    # (session)`` for the reflection loop, ``req.sandbox`` for the main loop)
    # turned it red while the rule itself was intact. A prose mention is not a
    # session lookup, so the assertion is now the structural one it always meant:
    # no call to the resolver inside this function.
    called = {
        node.func.id
        for node in ast.walk(ast.parse(textwrap.dedent(inject_source)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "resolve_client_tier" not in called, (
        "the injection rule must not resolve the tier itself - the callers do, "
        "because the scheduler's turns and the upgrade session have tiers of "
        "their own"
    )


# ── the daemon's one write path and its broadcast ─────────────────────────


def _drive(server: EmrgServer, msg: dict) -> list[dict]:
    """One `_process_message` call, returning the frames the sender received."""
    ws = _RecordingWs()
    server._all_connections.add(ws)  # the broadcast target set
    asyncio.run(server._process_message(msg, ws))
    return ws.sent


def test_set_sandbox_persists_and_answers_the_requester(tmp_path):
    session = Session.create_with_id("s_set", tmp_path)
    server = _server()

    frames = _drive(server, {
        "type": "set_sandbox",
        "session_id": session.session_id,
        "cwd": str(tmp_path),
        "mode": "read-only",
    })

    assert [f for f in frames if f.get("type") == "sandbox_set" and "error" not in f], frames
    assert Session.load(session.session_id, tmp_path).sandbox == "read-only"


def test_set_sandbox_reaches_a_connection_that_did_not_ask(tmp_path):
    """The broadcast *is* the "every client sees it at once" mechanism."""
    session = Session.create_with_id("s_broadcast", tmp_path)
    server = _server()
    other = _RecordingWs()
    server._all_connections.add(other)
    asker = _RecordingWs()
    server._all_connections.add(asker)

    asyncio.run(server._process_message({
        "type": "set_sandbox",
        "session_id": session.session_id,
        "cwd": str(tmp_path),
        "mode": "danger-full-access",
    }, asker))

    assert [f for f in other.sent if f.get("type") == "sandbox_set"], other.sent
    assert other.sent[-1]["mode"] == "danger-full-access"
    # The asker got its own reply, not a second copy of it.
    assert len([f for f in asker.sent if f.get("type") == "sandbox_set"]) == 1


def test_an_unknown_mode_is_refused_by_the_daemon_and_nothing_is_written(tmp_path):
    server = _server()

    frames = _drive(server, {
        "type": "set_sandbox",
        "session_id": "s_refused",
        "cwd": str(tmp_path),
        "mode": "readonly",
    })

    errors = [f for f in frames if f.get("error")]
    assert errors, frames
    assert "readonly" in errors[0]["error"]
    meta = tmp_path / ".emrg" / "sessions" / "s_refused" / "meta.json"
    assert not meta.exists(), "a refused mode must not create or write the session"


def test_set_sandbox_without_an_identity_is_refused(tmp_path):
    server = _server()

    frames = _drive(server, {"type": "set_sandbox", "mode": "read-only"})

    assert [f for f in frames if f.get("error")], frames


def test_the_resume_snapshot_carries_the_tier(tmp_path):
    """A client opening a session must learn the tier, not wait to be told.

    ``sandbox_set`` is a broadcast, never replayed — the same reason the turn
    snapshot rides this frame (rant 2026-09-27T18:41:52).
    """
    session = Session.create_with_id("s_resume_tier", tmp_path)
    session.append_message({"type": "message", "role": "user", "content": "hi"})
    session.set_sandbox("read-only")
    server = _server()
    ws = _RecordingWs()

    asyncio.run(server._handle_resume_session(session.session_id, tmp_path, ws))

    results = [f for f in ws.sent if f.get("type") == "resume_result"]
    assert results, ws.sent
    assert results[-1]["meta"]["sandbox"] == "read-only"


def test_the_resume_snapshot_of_an_unset_session_names_the_effective_tier(tmp_path):
    """Nothing set is not "nothing to show": turns here run confined anyway."""
    session = Session.create_with_id("s_resume_default", tmp_path)
    session.append_message({"type": "message", "role": "user", "content": "hi"})
    server = _server()
    ws = _RecordingWs()

    asyncio.run(server._handle_resume_session(session.session_id, tmp_path, ws))

    results = [f for f in ws.sent if f.get("type") == "resume_result"]
    assert results[-1]["meta"]["sandbox"] == "workspace-write"
    assert "sandbox" not in _meta(session)


def test_the_turn_start_frame_carries_the_tier_the_turn_runs_at(tmp_path):
    """The frame a client needs to show a tier it was never told about.

    The loop itself is replaced: what is under test is the frame the wrapper
    emits, not the LLM conversation behind it.
    """
    session = Session.create_with_id("s_turn_frame", tmp_path)
    server = _server()
    ws = _RecordingWs()
    server._session_subscribers[session.session_id] = {ws: str(tmp_path)}
    ran: list = []

    async def _fake_loop(req, ws_, session_, cancel_event=None, allow_tools=True):
        ran.append(req.sandbox)

    server._run_tool_loop = _fake_loop  # type: ignore[assignment]
    req = TaskRequest(session_id=session.session_id, cwd=str(tmp_path), prompt="hi")
    req.sandbox = resolve_client_tier(session, req.sandbox)

    asyncio.run(server._run_tool_loop_locked(req, ws, session, asyncio.Event()))

    starts = [f for f in ws.sent if f.get("type") == "turn_start"]
    assert starts, ws.sent
    assert starts[0]["sandbox"] == "workspace-write"
    assert ran == ["workspace-write"]


def test_the_upgrade_sessions_silence_stays_silent():
    """The exemption, stated where it can fail.

    The upgrade session's request declares no tier on purpose, and it never goes
    through the client's read loop — so the ruling's default must not reach it.
    Pinned here as the shape of its own request, because the value it resolves to
    is what lets the upgrade write outside its cwd at all.
    """
    req = TaskRequest(session_id="emrg-upgrade", cwd="/tmp", prompt="upgrade")
    assert req.sandbox is None
    source = inspect.getsource(EmrgServer._run_upgrade_session)
    assert "sandbox=" not in source
    assert "resolve_client_tier" not in source


# ── the TUI: an entry point and a display, never a store ──────────────────


def test_the_tui_status_line_shows_the_tier_the_daemon_reported():
    from emrg.client.app import _format_status_left

    out = _format_status_left("main", "s_1", "m", None, "read-only")

    assert out.endswith("[read-only]")


def test_the_tui_status_line_says_nothing_about_a_tier_it_does_not_know():
    """Only once a value arrived — an older daemon never sends one.

    Same rule as the `vision` segment beside it: a client that invents a tier is
    a second source of truth, which is the whole thing this change removes.
    """
    from emrg.client.app import _format_status_left

    assert "[" not in _format_status_left("main", "s_1", "")


def test_the_tui_offers_the_command():
    from emrg.client.widgets import _COMMAND_HELP

    assert "/sandbox" in _COMMAND_HELP


def test_the_picker_offers_exactly_the_daemons_modes():
    """The list is drawn from the one vocabulary, so a mode cannot be invented
    in a client and refused by the daemon."""
    from emrg.client.widgets import SandboxSelector

    sel = SandboxSelector(list(SANDBOX_MODES), current="read-only")

    assert list(sel.modes) == list(SANDBOX_MODES)
    assert sel.selected_mode == SANDBOX_MODES[0]
    sel.move_up()  # already at the top
    assert sel.selected_mode == SANDBOX_MODES[0]
    sel.move_down()
    assert sel.selected_mode == SANDBOX_MODES[1]
    assert sel.current == "read-only"


def test_the_tui_sends_the_command_and_keeps_no_tier():
    """The entry point's contract: a command carrying the session identity.

    A source-level guard, and a stated limit: what it proves is that both the
    `/sandbox <mode>` path and the picker hand `session_id`/`cwd`/`mode` to
    `set_sandbox`, and that nothing in the client reaches the session's file —
    the key has one writer, in the daemon. The rendering of the answer is the
    daemon's own test above.
    """
    from emrg.client import app as app_mod

    source = inspect.getsource(app_mod)

    assert source.count('"set_sandbox"') == 2, "one send site per entry point (argument, picker)"
    assert source.count('"set_sandbox", session_id=session_id, cwd=cwd') == 2
    assert "meta.json" not in source
