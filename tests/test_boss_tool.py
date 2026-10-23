"""The owner role is a tool, and its session is a role held across rounds (issue #2467).

Every scheduled task is one agent that decides the direction and does the work in
the same round. Two cheaper ways of separating those jobs were tried and both
failed: review semantics baked into the generic ``subagent`` schema (refused — a
general tool with a special purpose is no longer general), and "call a subagent
first and brief it thus" written into the templates (16% → 56% → 31% of rounds, so
the wording is not the lever). A parent-written brief is structurally wrong in any
case: a brief is a summary, and the position the owner exists to supply is exactly
what a summary drops.

What is asserted here, and why each arm is a separate subject:

* **the roster** — ``boss`` is registered, is offered to a scheduled round, and is
  **not** offered to a host conversation. That last fact is the tool's whole
  authorization story: it lives in one whitelist and nowhere else, so a second
  mechanism inside the tool asking "who called me?" would be a copy of a fact the
  producer already holds;
* **the arguments** — ``intent`` is required, the schema carries no ``focus`` at
  all, and every refusal names the parameter it refused;
* **the quiet window** — a second call inside four hours starts no owner turn and
  says so in a shape a caller can detect; the clock is the session's own, so a
  rebuilt session is still busy and a turn that died still leaves its stamp;
* **where the mission comes from** — driven for real: the owner's first request
  does **not** contain the mission and its second one does, which can only happen
  if the owner read the parent's history itself. A test that asserted the prompt
  names the path would pass on a design where the parent still supplied the text;
* **the directory** — created on first use, reused on the next call (a marker
  written inside survives), and gone with the parent's ``sub-agent/``;
* **the templates** — all eight task templates carry the same sentence, with a
  control arm showing the guard goes red when one is stripped, and ``boss.j2`` is
  a file the host edits rather than a string in the code.

No daemon is started, stopped or restarted: the runners are called directly, and
only the LLM, the broadcast and the memory reflection are stubbed.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import (
    EmrgServer,
    _get_jinja_env,
    build_tool_registry,
    default_tool_names,
    scheduled_task_tool_names,
)
from emrg.protocol import TaskRequest
from emrg.session import Session
from emrg.tools.boss_tool import (
    BUSY_MARKER,
    DEFAULT_TIMEOUT_S,
    REASK_WINDOW_S,
    BossTool,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS = REPO_ROOT / "emrg" / "server" / "prompts"
TEMPLATES = (
    "evolution_prompt.md",
    "paper_prompt.md",
    "open_source_prompt.md",
    "promote_prompt.md",
    "journal_prompt.md",
    "competition_prompt.md",
    "ios_app_prompt.md",
    "feishu_prompt.md",
)

#: The sentence every task template must carry, verbatim. One string, so the guard
#: and the templates cannot drift apart by a word.
THE_ASK = "**The round's first step is to call `boss`**"

#: A string that appears in the mission and nowhere else. What makes the "the owner
#: read it itself" arm possible: it can only reach the owner's second request
#: through the file.
MISSION_MARKER = "MISSION-ONLY-8f3a91c2"


# ── the roster ────────────────────────────────────────────────────────


def test_boss_is_registered_and_offered_to_scheduled_rounds_alone():
    registry = build_tool_registry(None, None)
    assert registry.get("boss") is not None, (
        "boss is in a whitelist but not registered — the whitelist and the "
        "registration have drifted apart"
    )
    assert "boss" in scheduled_task_tool_names(), (
        "the owner role is not offered to a scheduled round, so no task can ask for "
        "one and the tool is dead weight"
    )
    assert "boss" not in default_tool_names(), (
        "the owner role is offered to a host conversation: the split in the two "
        "whitelists is the whole of its authorization, so this is a leak, not a "
        "convenience"
    )


# ── the arguments ─────────────────────────────────────────────────────


class _Runner:
    """Records what the tool handed the daemon, and answers with a stance."""

    def __init__(self, answer: str = "a stance") -> None:
        self.calls: list[dict] = []
        self._answer = answer

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"text": self._answer}


def _injected(**extra):
    """The arguments the daemon injects, plus whatever a test adds."""
    args = {
        "intent": "ask the owner where this round goes",
        "_parent_session_id": "emrg-task",
        "_parent_cwd": "/tmp/whatever",
    }
    args.update(extra)
    return args


def test_the_owner_is_reached_with_the_injected_identity():
    runner = _Runner()
    result = asyncio.run(BossTool(runner).execute(_injected()))
    assert not result.error, result.content
    assert result.content == "a stance"
    call = runner.calls[0]
    assert call["parent_session_id"] == "emrg-task"
    assert call["parent_cwd"] == "/tmp/whatever"
    assert call["timeout_s"] == DEFAULT_TIMEOUT_S
    # No `prompt` is passed and none may be: the owner reads the mission itself,
    # and a brief here is the thing this tool exists to replace.
    assert "prompt" not in call


def test_a_call_without_intent_is_refused():
    runner = _Runner()
    result = asyncio.run(BossTool(runner).execute({
        "_parent_session_id": "s", "_parent_cwd": "/tmp",
    }))
    assert result.error
    assert "intent" in result.content
    assert runner.calls == [], "the owner ran without the log keeping a reason"


def test_a_call_without_a_parent_session_is_refused():
    runner = _Runner()
    result = asyncio.run(BossTool(runner).execute({"intent": "x"}))
    assert result.error
    assert "parent session" in result.content
    assert runner.calls == []


def test_the_schema_carries_no_focus_parameter():
    """Issue #2495: choosing the question is choosing the direction.

    Asserted on the schema rather than on behaviour, and the difference is the
    guard: the tool ignores keys it does not know, so a test that passed `focus` in
    and watched for no effect would stay green with the parameter restored.
    """
    properties = BossTool(_Runner()).definition().parameters["properties"]
    assert "focus" not in properties, (
        "the executor can still name what the owner should judge, which hands the "
        "direction back to the session being directed"
    )
    assert set(properties) == {"intent", "timeout_s"}


def test_the_runner_is_handed_no_focus():
    """The other half: a removed parameter leaves the call as well as the schema."""
    runner = _Runner()
    asyncio.run(BossTool(runner).execute(_injected()))
    assert "focus" not in runner.calls[0], (
        "the daemon is still handed a focus, so the parameter is only half gone"
    )
    assert "prompt" not in runner.calls[0]


@pytest.mark.parametrize("bad", [True, "600", 0, -1, 1.5])
def test_the_timeout_domain_is_refused(bad):
    result = asyncio.run(BossTool(_Runner()).execute(_injected(timeout_s=bad)))
    assert result.error, f"{bad!r} was accepted as a bound on the owner's run"
    assert "timeout_s" in result.content


def test_an_error_from_the_runner_is_reported_as_an_error():
    class _Failing:
        async def __call__(self, **kwargs):
            return {"error": "boss: refused for a reason"}

    result = asyncio.run(BossTool(_Failing()).execute(_injected()))
    assert result.error
    assert "refused for a reason" in result.content


# ── the templates ─────────────────────────────────────────────────────


def _templates_missing_the_ask(texts: dict[str, str]) -> list[str]:
    """Which of the eight task templates do not carry the sentence."""
    return sorted(name for name, text in texts.items() if THE_ASK not in text)


def _template_texts() -> dict[str, str]:
    return {
        name: (REPO_ROOT / "emrg" / "server" / name).read_text(encoding="utf-8")
        for name in TEMPLATES
    }


def test_every_task_template_asks_for_the_owner():
    missing = _templates_missing_the_ask(_template_texts())
    assert missing == [], (
        f"these templates never tell the round to ask its owner: {missing}"
    )


def test_the_template_guard_catches_a_stripped_sentence():
    """The control arm: a reading that is green whatever it is given measured nothing."""
    texts = _template_texts()
    subject = "feishu_prompt.md"
    texts[subject] = texts[subject].replace(THE_ASK, "")
    assert _templates_missing_the_ask(texts) == [subject], (
        "the guard did not notice the sentence missing from one template, so the "
        "green arm above says nothing about the other seven"
    )


def test_the_boss_template_is_a_file_the_host_edits():
    """No restart: the render reads the file, so an edit is live on the next call."""
    source = PROMPTS / "boss.j2"
    assert source.is_file(), "boss.j2 must be a file in the prompts directory"
    original = source.read_text(encoding="utf-8")
    try:
        rendered = _get_jinja_env().get_template("boss.j2").render(
            parent_history="/tmp/session/history.jsonl",
        )
        assert "/tmp/session/history.jsonl" in rendered, (
            "the template does not receive the path to the parent's history, so the "
            "owner has no way to read the mission"
        )

        marker = "TEMPLATE-EDIT-IS-LIVE-4b7d"
        source.write_text(original + "\n" + marker + "\n", encoding="utf-8")
        again = _get_jinja_env().get_template("boss.j2").render(
            parent_history="/tmp/session/history.jsonl",
        )
        assert marker in again, (
            "an edit to boss.j2 did not reach the next render — the wording is the "
            "host's, and a cached copy makes it the code's"
        )
    finally:
        source.write_text(original, encoding="utf-8")


# ── the session the owner runs in ─────────────────────────────────────


def _server(tmp_path, monkeypatch) -> EmrgServer:
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH", tmp_path / "fire")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH", tmp_path / "rc")
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = tmp_path / "agents.yml"
    server._maybe_reflect_memory = lambda *a, **k: None
    return server


def _parent(tmp_path, session_id: str = "task-1") -> Session:
    """A parent session whose history holds a mission nothing else carries."""
    parent = Session.create_with_id(session_id, tmp_path)
    parent.append_message({
        "type": "message", "role": "user",
        "content": f"{MISSION_MARKER} — do the task the host described.",
        "timestamp": "2026-10-22T00:00:00",
    })
    parent.append_message({
        "type": "message", "role": "assistant", "content": "round one done",
        "timestamp": "2026-10-22T00:01:00",
    })
    return parent


def _stub_the_loop(server: EmrgServer) -> list[dict]:
    """Replace the owner's turn with a no-op that records the request it got."""
    seen: list[dict] = []

    async def loop(req, ws, session, cancel_event, **kwargs):
        seen.append({"prompt": req.prompt, "cwd": req.cwd, "session": session.session_id})

    server._run_tool_loop_locked = loop
    return seen


def _boss_cwd(tmp_path, session_id: str = "task-1") -> Path:
    return (
        tmp_path / ".emrg" / "sessions" / session_id / "sub-agent" / "sub-boss"
    )


def _ask(server: EmrgServer, tmp_path, session_id: str = "task-1") -> dict:
    return asyncio.run(server._run_boss_child(
        parent_session_id=session_id, parent_cwd=str(tmp_path),
        intent="ask", state={"child": None}, parent_tools=["read"],
    ))


def test_the_owner_is_given_the_path_to_the_history_and_not_the_mission(
    tmp_path, monkeypatch,
):
    server = _server(tmp_path, monkeypatch)
    _parent(tmp_path)
    seen = _stub_the_loop(server)

    _ask(server, tmp_path)

    history = tmp_path / ".emrg" / "sessions" / "task-1" / "history.jsonl"
    assert str(history) in seen[0]["prompt"], (
        "the owner was not told where the mission is, so it cannot read it"
    )
    assert MISSION_MARKER not in seen[0]["prompt"], (
        "the mission reached the owner through the prompt: the executor is briefing "
        "the owner again, which is the design this tool replaced"
    )
    assert _boss_cwd(tmp_path).is_dir()


def test_the_owner_reads_the_mission_itself(tmp_path, monkeypatch):
    """The real tool loop: the owner's second request carries the mission.

    That can only be true if the owner read the parent's history — the string
    exists nowhere else — so this is the arm a prompt-shape assertion cannot reach.
    """
    server = _server(tmp_path, monkeypatch)
    _parent(tmp_path)
    history = tmp_path / ".emrg" / "sessions" / "task-1" / "history.jsonl"

    seen: list[str] = []
    state = {"round": 0}

    async def fake_stream(messages, tools=None):
        state["round"] += 1
        seen.append(json.dumps(messages, ensure_ascii=False))
        if state["round"] == 1:
            yield {
                "content": None,
                "tool_calls": [{
                    "index": 0,
                    "id": "call_0",
                    "function": {
                        "name": "read",
                        "arguments": json.dumps({
                            "file_path": str(history),
                            "intent": "read the mission the task was given",
                        }),
                    },
                }],
                "finish_reason": "tool_calls",
                "usage": None,
            }
        else:
            yield {
                "content": "Aim at the mission; do not re-plan.", "tool_calls": None,
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    async def fake_broadcast(session_id, payload):
        pass

    server.llm.chat_stream = fake_stream
    server._broadcast = fake_broadcast

    out = _ask(server, tmp_path)

    assert "Aim at the mission; do not re-plan." in out["text"], (
        f"the owner's answer did not reach the executor: {out!r}"
    )
    assert MISSION_MARKER not in seen[0], (
        "the mission was in the owner's first request, which means the executor put "
        "it there"
    )
    assert MISSION_MARKER in seen[-1], (
        "the owner's second request does not carry the mission, so it never read the "
        "parent's history — the one thing that separates this from a briefed subagent"
    )


def test_the_owner_session_is_reused_rather_than_rebuilt(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    _parent(tmp_path)
    _stub_the_loop(server)

    _ask(server, tmp_path)
    marker = _boss_cwd(tmp_path) / ".emrg" / "sessions" / "boss-task-1" / "keep-me"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("survives", encoding="utf-8")

    _ask(server, tmp_path)

    assert marker.read_text(encoding="utf-8") == "survives", (
        "the owner's session was cleared between calls: the role is held across "
        "rounds, and a cleared one re-reads the whole mission every round"
    )


def test_the_owner_goes_away_with_the_parent(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    _parent(tmp_path)
    _stub_the_loop(server)

    _ask(server, tmp_path)
    assert _boss_cwd(tmp_path).is_dir()

    # The parent's own teardown, not a hand-rolled rmtree: this is what a deleted
    # session does, and the owner's directory sits under it.
    Session.delete("task-1", tmp_path)
    assert not _boss_cwd(tmp_path).exists(), (
        "the owner's directory outlived the session it belongs to"
    )


def test_the_depth_limit_bounds_an_owner_that_asks_for_an_owner(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    parent = _parent(tmp_path)
    parent.set_depth(server.SUBAGENT_MAX_DEPTH)
    seen = _stub_the_loop(server)

    out = _ask(server, tmp_path)

    assert out.get("error"), "an owner was started past the delegation depth limit"
    assert "delegations deep" in out["error"]
    assert seen == [], "the refused call still ran a turn"


def test_an_owner_of_a_task_inherits_the_turns_tier(tmp_path, monkeypatch):
    """The owner of a scheduled task runs where the task runs, not unconfined.

    A task's tier is resolved from `tasks.yml` into the request and never written
    onto the session object, so an owner that read only the session inherited
    nothing — and an owner with a wider tier than its executor is the one shape of
    this bug that can act on the answer it then gives. Driven through
    `_inject_tool_arguments` and then the tool, because the injection is the half
    that was missing.
    """
    server = _server(tmp_path, monkeypatch)
    _stub_the_loop(server)
    parent = Session.create_with_id("task-tier-owner", tmp_path)
    assert parent.sandbox is None, (
        "this test models a scheduled task, whose tier lives on the request"
    )
    req = TaskRequest(
        id="owner-tier-turn", session_id=parent.session_id, cwd=str(tmp_path),
        prompt="a task's tick", sandbox="read-only",
    )

    args = {"timeout_s": 10, "intent": "ask the owner"}
    EmrgServer._inject_tool_arguments("boss", args, parent, req, None)
    assert args.get("_parent_sandbox") == "read-only", (
        f"the turn's tier never reached the call: {args}"
    )
    result = asyncio.run(BossTool(server._run_boss_tool).execute(args))
    assert not result.error, result.content

    owner = Session.load(
        f"boss-{parent.session_id}", _boss_cwd(tmp_path, parent.session_id),
    )
    assert owner.sandbox == "read-only", (
        "the owner of a confined task ran at a wider tier: an owner that may go "
        "where its executor may not is the escape this inheritance refuses"
    )


# ── the quiet window (issue #2495) ────────────────────────────────────


def test_the_quiet_window_is_four_hours():
    """The number the description advertises and the daemon enforces is one number."""
    assert REASK_WINDOW_S == 4 * 60 * 60, (
        "the window moved: the tool's own description is rendered from this "
        "constant, so a second number here would be a second answer"
    )


def test_a_second_call_inside_the_window_starts_no_owner_turn(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    seen = _stub_the_loop(server)

    first = _ask(server, tmp_path)
    assert not first.get("error"), first
    assert BUSY_MARKER not in first["text"], "the first call was answered as busy"
    assert len(seen) == 1, "the first call must actually run the owner"

    second = _ask(server, tmp_path)
    assert len(seen) == 1, (
        "a second owner turn started inside the window, which pays twice for a "
        "judgement that cannot have changed"
    )
    assert second["text"].startswith(BUSY_MARKER), second["text"][:80]


def test_the_busy_answer_is_a_result_and_says_no_stance_was_decided(
    tmp_path, monkeypatch,
):
    """A busy owner is not a failure, and the answer must not read as direction."""
    server = _server(tmp_path, monkeypatch)
    _stub_the_loop(server)
    _ask(server, tmp_path)

    busy = _ask(server, tmp_path)
    assert "error" not in busy, (
        "a busy owner came back as an error, so a caller reads a normal wait as a "
        "broken round"
    )
    assert busy["text"].startswith(BUSY_MARKER)
    assert "remaining:" in busy["text"], "the answer does not say how long the wait is"
    assert "no direction was decided" in busy["text"], (
        "the answer does not say in as many words that this round has no stance, "
        "and the tool otherwise promises every answer is one"
    )


def test_the_owner_is_asked_again_once_the_window_has_passed(tmp_path, monkeypatch):
    """The window is a bound, not a permanent refusal at the other end."""
    server = _server(tmp_path, monkeypatch)
    seen = _stub_the_loop(server)
    _ask(server, tmp_path)

    owner = server._get_or_create_session("boss-task-1", _boss_cwd(tmp_path))
    owner.set_boss_asked_at(time.time() - REASK_WINDOW_S - 1)

    again = _ask(server, tmp_path)
    assert len(seen) == 2, "the window refused a call that was outside it"
    assert BUSY_MARKER not in again["text"]


def test_the_window_survives_a_rebuilt_session(tmp_path, monkeypatch):
    """A daemon restart must not reset the clock: the owner session lives on disk."""
    first = _server(tmp_path, monkeypatch)
    _stub_the_loop(first)
    _ask(first, tmp_path)

    reopened = _server(tmp_path, monkeypatch)
    seen = _stub_the_loop(reopened)
    out = _ask(reopened, tmp_path)

    assert seen == [], "a rebuilt session forgot it had been asked, so it paid again"
    assert out["text"].startswith(BUSY_MARKER)


def test_a_turn_that_died_still_leaves_the_clock(tmp_path, monkeypatch):
    """The stamp is written before the turn runs, never after it."""
    server = _server(tmp_path, monkeypatch)

    async def dead_loop(*args, **kwargs):
        raise RuntimeError("the owner died mid-turn")

    server._run_tool_loop_locked = dead_loop
    with pytest.raises(RuntimeError):
        _ask(server, tmp_path)

    seen = _stub_the_loop(server)
    out = _ask(server, tmp_path)
    assert seen == [], (
        "the clock was written only once the turn finished, so a turn that died "
        "left no trace and the next call bought the same judgement again"
    )
    assert out["text"].startswith(BUSY_MARKER)


def test_the_clock_is_written_by_its_own_setter(tmp_path):
    """The value on disk is the setter's, not a neighbour's save carrying it along.

    Without this arm, deleting the setter's `_save_meta()` survives every test
    above: `set_depth` saves the same `meta.json` immediately afterwards and takes
    the new key with it, so the call path looks correct while the writer is broken.
    Asserted on the file, because the file is the thing the restart reads.
    """
    session = Session.create_with_id("boss-clock", tmp_path)
    assert session.boss_asked_at is None, "a fresh session claims it was asked"

    session.set_boss_asked_at(1761000000.0)

    meta_path = tmp_path / ".emrg" / "sessions" / "boss-clock" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta.get("boss_asked_at") == 1761000000.0, (
        "the setter did not write the clock: on disk is where a restart reads it"
    )
    assert session.boss_asked_at == 1761000000.0


# ── a call that delivered nothing does not consume the window (issue #2499) ──
#
# The clock is stamped before the turn runs, so a turn that *died* keeps it — that
# guarantee is what `test_a_turn_that_died_still_leaves_the_clock` pins. The two
# branches below are the other case: the turn was taken away before it could
# answer, and a stamp left standing refuses the very retry the timeout error asks
# for. "Delivered nothing" and "died mid-way" are different outcomes, so they are
# told apart rather than the stamp moved wholesale to after the turn.


def _ask_with_timeout(
    server: EmrgServer, tmp_path, timeout_s: int = 30, session_id: str = "task-1",
) -> dict:
    """One whole `boss` call, bound included — the layer the timeout lives in."""
    return asyncio.run(server._run_boss_tool(
        parent_session_id=session_id, parent_cwd=str(tmp_path),
        timeout_s=timeout_s, intent="ask", parent_tools=["read"],
    ))


def test_a_call_that_timed_out_does_not_consume_the_window(tmp_path, monkeypatch):
    """Issue #2499: the retry a timeout error asks for must start a turn.

    Nothing was decided by a call that was cancelled, so there is no stance for
    the window to protect — and the busy notice that a standing stamp would
    produce tells the round to carry on as if one had been given.
    """
    server = _server(tmp_path, monkeypatch)

    async def never_answers(req, ws, session, cancel_event, **kwargs):
        await asyncio.sleep(3600)

    server._run_tool_loop_locked = never_answers

    timed_out = _ask_with_timeout(server, tmp_path, timeout_s=1)
    assert timed_out.get("error"), (
        f"a call that never answered came back as a result: {timed_out}"
    )
    assert "did not answer within" in timed_out["error"]

    seen = _stub_the_loop(server)
    retry = _ask_with_timeout(server, tmp_path)

    assert len(seen) == 1, (
        "the retry after a timeout started no owner turn: the cancelled call "
        "consumed the four-hour window, so `Raise timeout_s` — the only remedy the "
        "error offers — cannot be carried out"
    )
    assert BUSY_MARKER not in retry.get("text", ""), (
        "the retry was answered with the busy notice instead of a stance"
    )


def test_a_call_cancelled_with_its_round_does_not_consume_the_window(
    tmp_path, monkeypatch,
):
    """The round that asked was cancelled: same outcome, nothing delivered."""
    server = _server(tmp_path, monkeypatch)
    started = asyncio.Event()

    async def never_answers(req, ws, session, cancel_event, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    server._run_tool_loop_locked = never_answers

    async def scenario():
        task = asyncio.create_task(server._run_boss_tool(
            parent_session_id="task-1", parent_cwd=str(tmp_path),
            timeout_s=600, intent="ask", parent_tools=["read"],
        ))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    owner = server._get_or_create_session("boss-task-1", _boss_cwd(tmp_path))
    assert owner.boss_asked_at is None, (
        "a call whose round was cancelled wrote the clock, so the same session's "
        "next round is refused for four hours by a call that delivered nothing"
    )
    meta_path = _boss_cwd(tmp_path).parent.parent / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "boss_asked_at" not in meta, (
        "the clock is still on disk, and on disk is what a restart reads"
    )


def test_a_call_that_answered_still_consumes_the_window(tmp_path, monkeypatch):
    """The control: the window's own feature must survive the fix.

    Without this arm the timeout fix could be made by stamping only on delivery,
    or by deleting the stamp — both of which would delete issue #2495's feature
    and leave every test above green.
    """
    server = _server(tmp_path, monkeypatch)
    _stub_the_loop(server)

    answered = _ask_with_timeout(server, tmp_path)
    assert not answered.get("error"), answered
    assert "The owner answered" in answered["text"], answered["text"][:120]

    seen = _stub_the_loop(server)
    second = _ask_with_timeout(server, tmp_path)

    assert seen == [], (
        "an owner that answered did not consume the window, so the second call "
        "paid for the same judgement again"
    )
    assert second["text"].startswith(BUSY_MARKER), second["text"][:80]
