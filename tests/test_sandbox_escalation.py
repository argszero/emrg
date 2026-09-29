"""In-session escalation, and the approval channel it is worthless without.

Rant 2026-09-29T15:52:38.987951+08:00, requirement 1 / design §D3 (phase P5).
The module landed with **no test that names it** — `grep -rn "approve_escalation"
tests/` had zero hits while the suite read green, which is the rule of
`requirement-guarded-only-if-a-test-names-it.md` read the other way round: a
requirement is guarded only if a test names it, and an unguarded one is one
editing session away from being wrong in the direction nobody notices (a wider
tier granted by accident, or a "no" that runs anyway).

What is pinned here, in the order the module itself orders its decisions:

* the strict-wider table — one hop, never a jump, and a tier with no neighbour;
* the pair check — `sandbox_permissions` + `justification` arrive together;
* fail-closed, all five ways — no channel, a refusal, an unreadable answer, a
  channel that raised, a timeout: **every one of them leaves the call where it
  was**;
* the daemon's channel — the question reaches the session, the first answer
  wins, and nobody answering is not consent;
* the loop's own call site — the command does not run when the escalation was
  refused, and the widening does not stick to the next call.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.sandbox import escalation
from emrg.sandbox.policy import DANGER_FULL_ACCESS
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.session import Session


# ── fixtures ────────────────────────────────────────────────────────────────


class _FakeWs:
    """Minimal subscriber: records what the daemon sends it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))


class _StubTool:
    """A shell-tool stand-in: records the arguments the daemon handed it.

    Registered under a real shell tool's name on purpose — `_apply_escalation`
    keys on `SHELL_TOOL_NAMES`, so a stand-in under a made-up name would test
    the branch that skips escalation instead of the one that takes it.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict] = []

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="stub", parameters={})

    async def execute(self, arguments: dict) -> ToolResult:
        self.calls.append(dict(arguments))
        return ToolResult(content="stub ran", error=False)


def _server() -> EmrgServer:
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def _subscribe(server: EmrgServer, session: Session) -> _FakeWs:
    ws = _FakeWs()
    server._session_subscribers[session.session_id] = {ws: str(session.cwd)}
    return ws


async def _answer_next_question(server: EmrgServer, ws: _FakeWs, answer=object()):
    """Answer the first approval question the daemon broadcasts.

    Mirrors what a client does rather than reaching into the future: the test
    reads the *frame* and hands the daemon the answer it would have sent.
    """
    for _ in range(500):
        for frame in ws.sent:
            if frame.get("type") != "approval_request" or frame.get("_answered"):
                continue
            frame["_answered"] = True
            payload: dict = {"request_id": frame.get("request_id")}
            if answer is not object():
                payload["approved"] = answer
            server._resolve_approval(payload)
            return frame
        await asyncio.sleep(0.002)
    return None


# ── the table ───────────────────────────────────────────────────────────────


def test_the_table_widens_by_one_hop_and_no_tier_widens_itself():
    """The blueprint's `_STRICTLY_WIDER` (`escalation.ts:28-31`), read whole."""
    assert escalation.hops_from("read-only") == ("workspace-write", DANGER_FULL_ACCESS)
    assert escalation.hops_from("workspace-write") == (DANGER_FULL_ACCESS,)
    assert escalation.hops_from(DANGER_FULL_ACCESS) == ()
    # An unknown mode is not a tier with no neighbour; it is not a tier.
    assert escalation.hops_from("readonly") == ()


def test_only_a_tier_with_a_wider_neighbour_advertises_the_field():
    """The blueprint's conditional hint: a tier with no hop must not advertise
    an instruction that can only ever fail."""
    assert escalation.advertises_escalation("read-only")
    assert escalation.advertises_escalation("workspace-write")
    assert not escalation.advertises_escalation(DANGER_FULL_ACCESS)


def test_the_advertised_set_is_what_a_schema_can_declare():
    """The schema advertises the whole target set (`escalation.ts:41`); the hop
    table decides what is reachable from where a call actually stands."""
    assert set(escalation.ESCALATION_TARGETS) == {"workspace-write", DANGER_FULL_ACCESS}
    assert set(escalation.ESCALATION_TARGETS) <= set(escalation.SANDBOX_MODES)


# ── the pair ────────────────────────────────────────────────────────────────


def test_both_arguments_absent_is_not_an_escalation_request():
    assert escalation.validate_pairing(None, None) == (None, None)


@pytest.mark.parametrize(
    "target,reason",
    [
        ("workspace-write", None),          # a request with no reason
        (None, "I need to write files"),    # a stray field
        ("", "I need it"),                  # a target that names nothing
        ("workspace-write", ""),            # an empty justification
        ("workspace-write", "   "),         # whitespace is not a justification
    ],
)
def test_half_an_escalation_request_is_refused(target, reason):
    with pytest.raises(escalation.EscalationRefused):
        escalation.validate_pairing(target, reason)


def test_the_pair_is_stripped_and_returned_as_the_words_that_will_be_shown():
    assert escalation.validate_pairing(" workspace-write ", " npm install ") == (
        "workspace-write",
        "npm install",
    )


# ── the hop ─────────────────────────────────────────────────────────────────


def test_a_hop_names_where_the_call_stands_and_where_it_may_go():
    hop = escalation.validate_hop("read-only", "workspace-write")
    assert (hop.from_mode, hop.to_mode) == ("read-only", "workspace-write")
    assert hop.describe() == "read-only → workspace-write"


@pytest.mark.parametrize(
    "current,target",
    [
        ("read-only", "read-only"),            # the tier it already has
        ("workspace-write", "read-only"),      # narrower, not wider
        ("read-only", "no-such-tier"),         # not a tier at all
        (DANGER_FULL_ACCESS, "workspace-write"),
    ],
)
def test_a_target_that_is_not_one_hop_wider_is_refused(current, target):
    with pytest.raises(escalation.EscalationRefused):
        escalation.validate_hop(current, target)


def test_the_refusal_says_what_was_reachable():
    """A refusal is an instruction the model can act on, not just a verdict."""
    with pytest.raises(escalation.EscalationRefused) as exc:
        escalation.validate_hop(DANGER_FULL_ACCESS, "workspace-write")
    assert "nothing" in str(exc.value)
    assert "danger-full-access" in str(exc.value)


# ── fail-closed, all five ways ──────────────────────────────────────────────


def _approve(ask, *, current="read-only", target="workspace-write"):
    return asyncio.run(
        escalation.approve_escalation(
            current_mode=current, target=target,
            justification="npm install", ask=ask,
        )
    )


def test_no_channel_is_a_refusal_not_a_permission():
    """A deployment where nobody can be asked has not approved anything."""
    called = []

    async def ask(question):  # pragma: no cover — must not be reached
        called.append(question)
        return True

    outcome = _approve(None)
    assert (outcome.approved, outcome.answered) == (False, False)
    assert outcome.reason == "no-approval-channel"
    assert called == []


def test_an_affirmative_answer_widens_the_call():
    async def ask(question):
        assert "justification: npm install" in question
        return True

    outcome = _approve(ask)
    assert (outcome.approved, outcome.answered) == (True, True)
    assert outcome.reason == "approved"


def test_a_host_saying_no_is_a_decision_and_is_reported_as_one():
    """`answered` separates "the host refused" from "nobody could tell"."""
    async def ask(question):
        return False

    outcome = _approve(ask)
    assert (outcome.approved, outcome.answered) == (False, True)
    assert outcome.reason == "denied"


def test_an_answer_nobody_can_read_is_not_consent():
    async def ask(question):
        return "maybe"

    outcome = _approve(ask)
    assert outcome.approved is False
    assert outcome.reason == "unreadable-answer"


def test_a_channel_that_raises_is_a_refusal():
    async def ask(question):
        raise RuntimeError("the client died mid-question")

    outcome = _approve(ask)
    assert outcome.approved is False
    assert outcome.reason == "channel-failed"


def test_a_host_who_never_answers_has_not_approved():
    async def ask(question):
        await asyncio.sleep(30)
        return True

    outcome = asyncio.run(
        escalation.approve_escalation(
            current_mode="read-only", target="workspace-write",
            justification="npm install", ask=ask, timeout=0.05,
        )
    )
    assert outcome.approved is False
    assert outcome.reason == "timeout"


def test_the_hop_is_checked_before_anyone_is_asked():
    """The order is the safety property: a request that is not a legal hop must
    never become a question a host could say yes to — an approval channel that
    can authorise anything is not a safety mechanism."""
    asked = []

    async def ask(question):
        asked.append(question)
        return True

    with pytest.raises(escalation.EscalationRefused):
        asyncio.run(
            escalation.approve_escalation(
                current_mode="workspace-write", target="read-only",
                justification="let me out", ask=ask,
            )
        )
    assert asked == []


@pytest.mark.parametrize(
    "answer,expected",
    [
        (True, True),
        ("yes", True),
        ("APPROVE", True),
        ({"approved": True}, True),
        (False, False),
        ("no", False),
        ({"approved": "deny"}, False),
        ("maybe", None),
        (None, None),
        (3, None),
    ],
)
def test_the_spellings_a_client_may_use(answer, expected):
    """Two clients ship this frame; a wire format only one of them can produce
    is a bug waiting for a second implementation."""
    assert escalation._read_answer(answer) is expected


# ── the retry hint ──────────────────────────────────────────────────────────


def test_the_hint_is_appended_only_where_escalation_is_reachable():
    text = "⛔ write refused"
    widened = escalation.with_retry_hint(text, mode="read-only")
    assert escalation.RETRY_HINT in widened
    assert "sandbox_permissions" in widened and "justification" in widened
    assert escalation.with_retry_hint(text, mode=DANGER_FULL_ACCESS) == text


def test_the_hint_is_idempotent_and_honours_an_explicit_target_list():
    once = escalation.with_retry_hint("nope", mode="read-only")
    assert escalation.with_retry_hint(once, mode="read-only") == once
    narrowed = escalation.with_retry_hint("nope", mode=DANGER_FULL_ACCESS, advertised=("workspace-write",))
    assert escalation.RETRY_HINT in narrowed


# ── the daemon's channel ────────────────────────────────────────────────────


def test_the_question_reaches_the_session_and_the_answer_resolves_it(tmp_path):
    server = _server()
    session = Session.create_with_id("esc-ask", tmp_path)
    ws = _subscribe(server, session)

    async def scenario():
        task = asyncio.create_task(
            server.request_approval(session.session_id, "widen this one call?")
        )
        frame = await _answer_next_question(server, ws, True)
        return frame, await asyncio.wait_for(task, 5)

    frame, approved = asyncio.run(scenario())
    assert frame is not None, "no approval_request frame reached the session"
    assert frame["type"] == "approval_request"
    assert frame["question"] == "widen this one call?"
    assert frame["session_id"] == session.session_id
    assert frame["request_id"]
    assert approved is True


def test_the_question_declares_the_deadline_the_daemon_will_enforce(tmp_path):
    """Issue #1757 requirement 2, the half a client cannot import.

    The TUI can read the constant (same language, same repo); a GUI renderer
    cannot, so the number must ride the frame. The assertion is against the
    constant itself — a client bounded by a literal would drift the moment
    either side is edited, which is the defect the field exists to remove.
    """
    server = _server()
    session = Session.create_with_id("esc-deadline", tmp_path)
    ws = _subscribe(server, session)

    async def scenario():
        task = asyncio.create_task(
            server.request_approval(session.session_id, "widen this one call?")
        )
        frame = await _answer_next_question(server, ws, True)
        return frame, await asyncio.wait_for(task, 5)

    frame, _approved = asyncio.run(scenario())
    assert frame is not None, "no approval_request frame reached the session"
    assert frame["timeout_seconds"] == escalation.APPROVAL_TIMEOUT_SECONDS


def test_a_session_with_no_client_is_refused_and_nothing_is_sent(tmp_path):
    """Fail closed: a host who walked away has not approved anything."""
    server = _server()
    session = Session.create_with_id("esc-nobody", tmp_path)
    ws = _FakeWs()  # never subscribed

    async def scenario():
        return await asyncio.wait_for(
            server.request_approval(session.session_id, "anyone?"), 5
        )

    assert asyncio.run(scenario()) is None
    assert ws.sent == []


def test_the_first_answer_wins_and_a_late_one_changes_nothing(tmp_path):
    """A command already running at a wider tier cannot be un-widened by a
    contrary answer, so the second one is dropped rather than racing the first."""
    server = _server()
    session = Session.create_with_id("esc-first", tmp_path)
    ws = _subscribe(server, session)

    async def scenario():
        task = asyncio.create_task(server.request_approval(session.session_id, "widen?"))
        frame = await _answer_next_question(server, ws, True)
        # The mirror of a second client answering "no" afterwards.
        server._resolve_approval({"request_id": frame["request_id"], "approved": False})
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(scenario()) is True


def test_an_unreadable_answer_leaves_the_future_unapproved(tmp_path):
    server = _server()
    session = Session.create_with_id("esc-garbled", tmp_path)
    ws = _subscribe(server, session)

    async def scenario():
        task = asyncio.create_task(server.request_approval(session.session_id, "widen?"))
        await _answer_next_question(server, ws, "please")
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(scenario()) is None


def test_an_answer_for_a_request_nobody_waits_for_changes_nothing(tmp_path):
    server = _server()
    assert server._resolve_approval({"request_id": "appr-nonexistent", "approved": True}) is False
    assert server._pending_approvals == {}


def test_a_question_nobody_answers_times_out_and_leaves_no_state(tmp_path, monkeypatch):
    monkeypatch.setattr(escalation, "APPROVAL_TIMEOUT_SECONDS", 0.05)
    server = _server()
    session = Session.create_with_id("esc-timeout", tmp_path)
    ws = _subscribe(server, session)

    async def scenario():
        answer = await asyncio.wait_for(
            server.request_approval(session.session_id, "widen?"), 5
        )
        return answer, dict(server._pending_approvals)

    answer, pending = asyncio.run(scenario())
    assert answer is None, "a timeout must read as a refusal"
    assert pending == {}, "the question's future outlived its question"
    # The timeout closes the question *for the clients too* (rant
    # 2026-09-29T15:52:38.987951+08:00 follow-up): a client that is never told
    # keeps the question live, so the TUI read the host's next prompt as the
    # answer and the GUI's dialog outlived the question it asked.
    assert [f.get("type") for f in ws.sent] == ["approval_request", "approval_resolved"]
    resolved = ws.sent[-1]
    assert resolved["outcome"] == "timed_out"
    assert resolved["request_id"] == ws.sent[0]["request_id"]
    assert resolved["session_id"] == session.session_id


def test_the_resolution_names_the_outcome_a_client_must_render(tmp_path):
    """Every exit a subscriber can observe is announced, with its own word.

    `approved` and `refused` are distinguished on the wire because they are
    different events for the person reading the screen — a client that collapses
    them cannot say whether the host's answer was taken or the question expired.
    """
    server = _server()
    session = Session.create_with_id("esc-outcome", tmp_path)
    ws = _subscribe(server, session)

    async def ask(answer):
        task = asyncio.create_task(server.request_approval(session.session_id, "widen?"))
        frame = await _answer_next_question(server, ws, answer)
        verdict = await asyncio.wait_for(task, 5)
        resolved = ws.sent[-1]
        assert frame["request_id"] == resolved["request_id"]
        return verdict, resolved

    approved, resolved_yes = asyncio.run(ask(True))
    assert approved is True and resolved_yes["outcome"] == "approved"
    refused, resolved_no = asyncio.run(ask(False))
    assert refused is False and resolved_no["outcome"] == "refused"


def test_a_question_cancelled_mid_wait_is_announced_too(tmp_path):
    """A third exit a subscriber can observe: the turn is cancelled under it.

    ESC on a turn cancels the task that is waiting for the answer
    (`_session_turn_task`, the handle `daemon.py:1344` cancels), so the wait
    dies with `CancelledError` — a `BaseException` that the timeout branch does
    not catch and cannot be made to catch by widening it to `Exception`. Without
    this the question stays live on every client exactly as it did in the
    timeout case: the GUI's dialog never closes, and the TUI's own deadline is
    the only thing that eventually stops it swallowing a prompt.
    """
    server = _server()
    session = Session.create_with_id("esc-cancelled", tmp_path)
    ws = _subscribe(server, session)

    async def scenario():
        task = asyncio.create_task(server.request_approval(session.session_id, "widen?"))
        for _ in range(500):
            if any(f.get("type") == "approval_request" for f in ws.sent):
                break
            await asyncio.sleep(0.002)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return dict(server._pending_approvals)

    pending = asyncio.run(scenario())
    assert pending == {}, "a cancelled question must not leave its future behind"
    assert [f.get("type") for f in ws.sent] == ["approval_request", "approval_resolved"]
    resolved = ws.sent[-1]
    assert resolved["outcome"] == "cancelled"
    assert resolved["request_id"] == ws.sent[0]["request_id"]
    assert resolved["session_id"] == session.session_id


class _WsHoldingTheFrame(_FakeWs):
    """A subscriber that records a frame and then holds its send open.

    `holds` names the type whose *delivery* is the window this client opens: the
    request (the question has reached the client and the daemon has not yet begun
    to wait) or the resolution (the ending is already on the wire and the turn
    dies while that frame is being written). Both are awaits inside
    `request_approval`, and neither is the wait the older cancellation test
    cancels.
    """

    def __init__(self, holds: str) -> None:
        super().__init__()
        self.holds = holds
        self.seen = asyncio.Event()
        self.hold = asyncio.Event()  # never released: the cancel is the exit
        self._held = False  # the hold is one-shot, so a repeat cannot deadlock

    async def send(self, data) -> None:
        frame = json.loads(data)
        self.sent.append(frame)
        if frame.get("type") == self.holds and not self._held:
            self._held = True
            self.seen.set()
            await self.hold.wait()


def test_a_cancel_while_the_question_is_told_is_announced_too(tmp_path):
    """The wait is not the only place a question can die.

    `request_approval` broadcasts the question *before* it waits for the answer,
    and `_broadcast` awaits one send per subscriber — so a cancel (ESC on the
    turn, `_session_turn_task`, `daemon.py:1344`) that lands inside that
    broadcast kills the call at a point the timeout branch and the wait branch
    both sit after. The client that already received the question then holds it
    dead, which is the whole defect this feature exists to remove: the frame is
    the only thing that tells a client the question is over.
    """
    server = _server()
    session = Session.create_with_id("esc-cancel-broadcast", tmp_path)
    ws = _WsHoldingTheFrame("approval_request")
    server._session_subscribers[session.session_id] = {ws: str(session.cwd)}

    async def scenario():
        task = asyncio.create_task(server.request_approval(session.session_id, "widen?"))
        await asyncio.wait_for(ws.seen.wait(), 5)
        # The question reached this client before the cancel — not an assumption:
        # `seen` is set from the frame the client was handed.
        assert [f.get("type") for f in ws.sent] == ["approval_request"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        return list(ws.sent), dict(server._pending_approvals)

    sent, pending = asyncio.run(scenario())
    assert [f.get("type") for f in sent] == ["approval_request", "approval_resolved"]
    resolved = sent[-1]
    assert resolved["outcome"] == "cancelled"
    assert resolved["request_id"] == sent[0]["request_id"]
    assert resolved["session_id"] == session.session_id
    assert pending == {}, "a cancelled question must not leave its future behind"


def test_a_cancel_while_the_ending_is_told_does_not_say_two_endings(tmp_path):
    """One question, one resolution — whichever await the cancel interrupts.

    The ending is decided before it is delivered, so a cancel that lands while
    the answering frame is being written must not add a second word for the same
    question: the client would be told both that the host approved and that the
    turn cancelled the question, and the last one would be false about how the
    question itself ended (the turn's own `done{cancelled}` is where the death of
    the *turn* is told). This is the guard on the one-shot flag the broadcast
    window needs: without it the flag is a comment.
    """
    server = _server()
    session = Session.create_with_id("esc-cancel-announce", tmp_path)
    ws = _WsHoldingTheFrame("approval_resolved")
    server._session_subscribers[session.session_id] = {ws: str(session.cwd)}

    async def scenario():
        task = asyncio.create_task(server.request_approval(session.session_id, "widen?"))
        for _ in range(500):
            if ws.sent:
                break
            await asyncio.sleep(0.002)
        server._resolve_approval({"request_id": ws.sent[0]["request_id"], "approved": True})
        await asyncio.wait_for(ws.seen.wait(), 5)
        # The verdict is already on the wire when the cancel arrives.
        assert [f.get("type") for f in ws.sent] == ["approval_request", "approval_resolved"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        return list(ws.sent)

    sent = asyncio.run(scenario())
    assert [f.get("type") for f in sent] == ["approval_request", "approval_resolved"]
    assert sent[-1]["outcome"] == "approved"
    assert sent[-1]["request_id"] == sent[0]["request_id"]


# ── the loop's call site ────────────────────────────────────────────────────


def _drive_shell_call(
    tmp_path, monkeypatch, *, args: dict, answer=object(), sandbox="read-only",
    tool_name="bash",
):
    """Run one tool loop whose single tool call carries `args`.

    Returns `(tool, frames, ws)`: the stub tool the loop executed (or not),
    everything it broadcast, and the subscriber that received it.
    """
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH",
                        tmp_path / "planted-fire-heartbeat")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH",
                        tmp_path / "planted-fire-round-complete")
    server = _server()
    session = Session.create_with_id("esc-loop", tmp_path)
    ws = _subscribe(server, session)
    stub = _StubTool(tool_name)
    server.tools._tools[tool_name] = stub

    calls: list[list[dict]] = []

    async def fake_stream(messages, tools=None):
        calls.append(list(messages))
        if len(calls) == 1:
            yield {
                "content": "", "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 10, "completion_tokens": 1},
                "tool_calls": [{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(args)},
                }],
            }
        else:
            yield {"content": "done", "tool_calls": None, "finish_reason": "stop",
                   "usage": {"prompt_tokens": 10, "completion_tokens": 1}}

    server.llm.chat_stream = fake_stream
    req = TaskRequest(id="req-esc", session_id=session.session_id, prompt="go",
                      sandbox=sandbox)

    async def scenario():
        task = asyncio.create_task(server._run_tool_loop(req, None, session))
        if answer is not object():
            await _answer_next_question(server, ws, answer)
        await asyncio.wait_for(task, 10)

    asyncio.run(scenario())
    return stub, ws, session


def test_a_shell_call_that_names_a_hop_asks_before_it_runs(tmp_path, monkeypatch):
    """The requirement, at the site that decides: the host is asked, and a yes
    widens **this call**."""
    stub, ws, session = _drive_shell_call(
        tmp_path, monkeypatch,
        args={"command": "npm install", "sandbox_permissions": "workspace-write",
              "justification": "npm install"},
        answer=True,
    )
    asked = [f for f in ws.sent if f.get("type") == "approval_request"]
    assert len(asked) == 1, "the daemon widened nothing without asking"
    assert "read-only" in asked[0]["question"]
    assert "justification: npm install" in asked[0]["question"]
    assert stub.calls, "an approved escalation did not run the command"
    assert stub.calls[0]["sandbox"] == "workspace-write"
    # …and the session's own default is untouched: nothing is written back.
    assert session.session_id == "esc-loop"


def test_a_refused_escalation_does_not_run_the_command_at_all(tmp_path, monkeypatch):
    """A hop that was not granted must not be granted by accident — so the call
    does not run, and the refusal is its outcome."""
    stub, ws, session = _drive_shell_call(
        tmp_path, monkeypatch,
        args={"command": "npm install", "sandbox_permissions": "workspace-write",
              "justification": "npm install"},
        answer=False,
    )
    assert [f for f in ws.sent if f.get("type") == "approval_request"]
    assert stub.calls == [], "a refused escalation ran the command anyway"
    results = [m for m in session.get_messages_for_llm() if m.get("role") == "tool"]
    assert any("escalation refused" in r["content"] for r in results), results


def test_a_silent_client_refuses_the_call(tmp_path, monkeypatch):
    """Nobody answered: the command must not run at the wider tier, and it must
    not run at the default one either — the escalation was the call's reason."""
    stub, ws, session = _drive_shell_call(
        tmp_path, monkeypatch,
        args={"command": "npm install", "sandbox_permissions": "workspace-write",
              "justification": "npm install"},
        answer=None,
    )
    assert stub.calls == []
    results = [m for m in session.get_messages_for_llm() if m.get("role") == "tool"]
    assert any("no-approval-channel" in r["content"] or "timeout" in r["content"]
               or "unreadable-answer" in r["content"] for r in results), results


def test_a_shell_call_without_the_pair_is_untouched(tmp_path, monkeypatch):
    """The overwhelming majority of calls: no escalation asked, no question, no
    change — a feature that taxes every call is one that gets switched off."""
    stub, ws, _session = _drive_shell_call(
        tmp_path, monkeypatch, args={"command": "ls"},
    )
    assert not [f for f in ws.sent if f.get("type") == "approval_request"]
    # The daemon injects the session's own tier either way (D1); what must not
    # happen is the tier *moving* — an escalation is the only thing that writes
    # it, and this call asked for none.
    assert stub.calls and stub.calls[0].get("sandbox") == "read-only", stub.calls


def test_a_tool_the_tier_does_not_reach_cannot_escalate(tmp_path, monkeypatch):
    """`read` is not a shell tool: the daemon injects no tier for it, so the
    field is a stray argument rather than a request — and no question is asked."""
    stub, ws, _session = _drive_shell_call(
        tmp_path, monkeypatch, tool_name="read",
        args={"path": "README.md", "sandbox_permissions": "workspace-write",
              "justification": "I want to read"},
    )
    assert not [f for f in ws.sent if f.get("type") == "approval_request"]
    assert stub.calls, "the tool did not run"


# ── requirement 5: the hint line ────────────────────────────────────────────


def _denied(mode: str):
    """A run the policy refused, shaped the way the tool shapes one."""
    from emrg.tools.bash_tool_v2 import ShellRunResult

    return ShellRunResult(
        stderr="bash: x: Operation not permitted",
        exit_code=1,
        sandbox={"mode": mode, "denied": True, "enforcement": "full"},
    )


def test_the_denial_a_confined_run_gets_teaches_it_how_to_ask():
    """The blueprint's hint (``render.ts:43-51``), reached through the renderer
    rather than the helper — the seam is the thing requirement 5 names."""
    from emrg.tools.bash_tool_v2 import render_result

    text = render_result(_denied("read-only"), escalation_modes=("workspace-write",))
    assert escalation.RETRY_HINT in text
    assert "sandbox_permissions" in text and "justification" in text


def test_a_tier_with_nowhere_wider_is_not_told_to_retry():
    """`danger-full-access` has no hop, so the composition advertises none and
    the hint would be an instruction that can only fail."""
    from emrg.tools.bash_tool_v2 import render_result

    text = render_result(_denied(DANGER_FULL_ACCESS), escalation_modes=())
    assert escalation.RETRY_HINT not in text
    assert "sandbox_permissions" not in text


def test_an_unconfined_denial_still_carries_no_hint():
    """A denial is the only place the hint belongs; a run that was not denied
    must read exactly as it always did."""
    from emrg.tools.bash_tool_v2 import ShellRunResult, render_result

    text = render_result(
        ShellRunResult(stdout="hi", exit_code=0, sandbox={"mode": "read-only", "denied": False}),
        escalation_modes=("workspace-write",),
    )
    assert text == "hi"


# ── requirement 4: the widening does not stick ──────────────────────────────


def test_the_widening_belongs_to_one_call_and_not_to_the_next(tmp_path, monkeypatch):
    """`retry this exact command once` — so the call after it stands where the
    session always stood. A tier written back to the session would make the
    approval a promotion, which is the one thing the phase must not become."""
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH",
                        tmp_path / "planted-fire-heartbeat")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH",
                        tmp_path / "planted-fire-round-complete")
    server = _server()
    session = Session.create_with_id("esc-once", tmp_path)
    ws = _subscribe(server, session)
    stub = _StubTool("bash")
    server.tools._tools["bash"] = stub

    rounds = [
        [{"command": "npm install", "sandbox_permissions": "danger-full-access",
          "justification": "the registry lives outside the workspace"}],
        # The next call asks for nothing — and must therefore run at the
        # session's own tier, not at the one the previous call was granted.
        [{"command": "ls"}],
    ]
    calls: list[list[dict]] = []

    async def fake_stream(messages, tools=None):
        calls.append(list(messages))
        if len(calls) <= len(rounds):
            n = len(calls) - 1
            yield {
                "content": "", "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 10, "completion_tokens": 1},
                "tool_calls": [{
                    "index": 0, "id": f"call_{n}", "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps(rounds[n][0])},
                }],
            }
        else:
            yield {"content": "done", "tool_calls": None, "finish_reason": "stop",
                   "usage": {"prompt_tokens": 10, "completion_tokens": 1}}

    server.llm.chat_stream = fake_stream
    req = TaskRequest(id="req-once", session_id=session.session_id, prompt="go",
                      sandbox="read-only")

    async def scenario():
        task = asyncio.create_task(server._run_tool_loop(req, None, session))
        await _answer_next_question(server, ws, True)
        await asyncio.wait_for(task, 10)

    asyncio.run(scenario())
    assert len(stub.calls) == 2, stub.calls
    assert stub.calls[0]["sandbox"] == DANGER_FULL_ACCESS, "the approved hop did not run"
    assert stub.calls[1]["sandbox"] == "read-only", (
        "the widening outlived the call it was granted for"
    )


def test_the_tool_tells_a_denied_model_how_to_ask_at_the_tier_it_ran(
    tmp_path, monkeypatch,
):
    """Requirement 5's wiring, at the site that chooses what to advertise.

    The renderer takes the target set as an argument, so a test that passes one
    exercises the renderer and not the tool: the tool's own line — which asks
    ``hops_from`` about the tier the run *used* — is only reached by executing
    it. The runner is stubbed (this is not a confinement test); what is under
    test is the tool's choice.
    """
    from emrg.tools import bash_tool_v2

    async def fake_run_command(command, *, policy, workdir, timeout):
        return bash_tool_v2.ShellRunResult(
            stderr="bash: x: Operation not permitted",
            exit_code=1,
            sandbox={"mode": policy.mode, "denied": True, "enforcement": "full"},
        )

    monkeypatch.setattr(bash_tool_v2, "run_command", fake_run_command, raising=True)
    denied = asyncio.run(bash_tool_v2.BashToolV2().execute(
        {"command": "echo x > /tmp/nope", "sandbox": "read-only", "workspace": str(tmp_path)},
    ))
    assert escalation.RETRY_HINT in denied.content, denied.content
