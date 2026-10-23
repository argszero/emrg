"""Delegation to a child session is a tool, not a page of instructions.

Issue #2451. Delegation already existed as a user-level skill that told the model
how to hand-assemble the websocket protocol itself: pick a session id, ask the
daemon whether it was busy, decide alone whether to delete and reuse a slot. A
page of instructions is not a capability — one wrong step costs a whole round —
and the tool loop already has everything needed to run a second session.

What is measured here is the mechanism behind the `subagent` tool, item by item,
because each item is a way the delegation can be wrong without looking wrong:

1. the parent receives the child's answer and its facts, and a cut answer says it
   was cut;
2. a child that outlives ``timeout_s`` is **cancelled**, not merely abandoned;
3. cancelling the parent's turn cancels the child and returns the slot;
4. nine slots are a real bound, and the next one taken is the one closed longest
   ago — not whichever dictionary order offers first;
5. a reused slot is emptied first, and emptied through `Session.delete` so the
   global index does not keep pointing at a directory that is gone;
6. "the child is closed" includes its memory write: the close stamp is taken
   after that write has landed, or the next child of the slot clears a directory
   the previous one is still writing into;
7. a session at the depth limit refuses **and says so**, rather than silently
   doing nothing;
8. every delegation is one greppable line carrying its slot.

No daemon is started, stopped or restarted here: the runner is called directly,
the LLM is a stub, and `_broadcast` goes nowhere. The parent and the files live
under `tmp_path`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.protocol import TaskRequest
from emrg.session import Session
from emrg.tools.subagent_tool import DEFAULT_TIMEOUT_S, SubagentTool


def _run(coro):
    return asyncio.run(coro)


def _server(tmp_path, monkeypatch) -> EmrgServer:
    """A server whose planted-fire markers and projects log stay under `tmp_path`.

    The loop stamps host state at round finalization; a suite run must not write
    `~/.emrg/logs`, and a test that leaves the real marker behind changes what the
    next real cycle reads.
    """
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH",
                        tmp_path / "planted-fire-heartbeat")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH",
                        tmp_path / "planted-fire-round-complete")
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "agents.yml"
    # `_broadcast` is left real: with no connections it sends nowhere, and it is the
    # one place that records a turn's terminal frame. Stubbing it made every child
    # turn report "ended without a terminal frame", which is the stub talking.
    return server


def _stub_reply(server: EmrgServer, text: str) -> None:
    """The child's turn answers `text` and stops."""

    async def fake_stream(messages, tools=None):
        yield {"content": text, "tool_calls": None, "finish_reason": "stop",
               "usage": {"prompt_tokens": 5, "completion_tokens": 2}}

    server.llm.chat_stream = fake_stream

    async def fake_chat(messages, tools=None):
        return {"content": "", "tool_calls": None}

    server.llm.chat = fake_chat


def _delegate(server, parent_id: str, parent_cwd: Path, *, prompt="do the job",
              timeout_s=10):
    return _run(server._run_subagent_tool(
        parent_session_id=parent_id, parent_cwd=str(parent_cwd),
        prompt=prompt, timeout_s=timeout_s, intent="a test delegation",
    ))


def _report_field(report: str, name: str) -> str:
    for line in report.splitlines():
        if line.startswith(f"- {name}: "):
            return line[len(f"- {name}: "):]
    raise AssertionError(f"the report carries no `{name}` line:\n{report}")


def _force_slot_choice(server, parent_id: str, slot: int) -> None:
    """Make `slot` the one closed longest ago, so the next acquire must take it.

    The choice is by close time, so a test that needs a *particular* slot says so
    through the same fact the production code reads — never by relying on
    dictionary order, which the LRU rule exists to ignore.
    """
    table = server._subagent_slot_closes(parent_id)
    for n in table:
        table[n] = time.time() + n
    table[slot] = 0.0


# ── 1. the parent gets the answer and the child's facts ────────────────────


def test_the_parent_receives_the_childs_answer_and_its_facts(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    _stub_reply(server, "the child did the job and says so")

    outcome = _delegate(server, "parent-1", tmp_path)

    assert "error" not in outcome, outcome
    report = outcome["text"]
    assert "the child did the job and says so" in report, (
        "the child's final answer is the one thing the parent asked for; a report "
        f"without it is not a delegation:\n{report}"
    )
    child_id = _report_field(report, "session")
    assert child_id.startswith("sub-parent-1-"), child_id
    cwd = Path(_report_field(report, "cwd"))
    assert cwd.is_dir(), f"the report names a cwd that does not exist: {cwd}"
    assert str(tmp_path) in str(cwd), (
        "a child's cwd must sit inside the parent's session tree, or its memory is "
        f"planted in a project root nobody asked about: {cwd}"
    )
    assert _report_field(report, "rounds").isdigit()
    assert float(_report_field(report, "elapsed").rstrip("s")) >= 0.0


def test_a_cut_answer_says_it_was_cut(tmp_path, monkeypatch):
    """A silently truncated report reads as a complete one — the marker is the point."""
    server = _server(tmp_path, monkeypatch)
    # The subject is a fixed answer, far longer than any ceiling this tool would
    # plausibly cut at. Deriving its length from the constant would make the test
    # move with the thing it measures: raise the ceiling and both sides rise, so no
    # arm could show it red (measured — that is exactly how the first version of
    # this test survived `_SUBAGENT_ANSWER_CHARS = 60000`).
    whole = 50_000
    _stub_reply(server, "x" * whole)

    outcome = _delegate(server, "parent-cut", tmp_path)

    assert "error" not in outcome, outcome
    report = outcome["text"]
    answer = report.split(
        "Its final answer, and nothing else it said along the way:\n\n", 1
    )[-1]
    kept = len(answer) - len(answer.lstrip("x"))
    assert kept < whole, (
        f"the whole {whole}-character answer came back, so nothing was cut and this "
        "test has no subject to measure"
    )
    assert kept == daemon_mod._SUBAGENT_ANSWER_CHARS, (
        f"the ceiling kept {kept} characters rather than "
        f"{daemon_mod._SUBAGENT_ANSWER_CHARS}"
    )
    assert "truncated" in answer, (
        "the answer was cut and nothing said so; a caller cannot tell a short "
        f"answer from a lost one:\n{answer[-300:]}"
    )
    assert str(whole) in answer, (
        "the marker does not say how much was dropped, so a caller cannot tell a "
        f"slightly long answer from a huge one:\n{answer[-300:]}"
    )


# ── 2. the timeout cancels the child ───────────────────────────────────────


def test_a_child_that_outlives_the_timeout_is_cancelled(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    cancelled = {"stream": False}

    async def hanging_stream(messages, tools=None):
        try:
            await asyncio.Event().wait()
            yield {"content": "", "tool_calls": None, "finish_reason": "stop",
                   "usage": {}}
        except asyncio.CancelledError:
            cancelled["stream"] = True
            raise

    server.llm.chat_stream = hanging_stream

    async def fake_chat(messages, tools=None):
        return {"content": "", "tool_calls": None}

    server.llm.chat = fake_chat

    started = time.monotonic()
    outcome = _delegate(server, "parent-timeout", tmp_path, timeout_s=1)
    elapsed = time.monotonic() - started

    assert outcome.get("error"), outcome
    assert "1s" in outcome["error"], outcome["error"]
    assert elapsed < 10, f"the timeout did not bound the call: {elapsed:.1f}s"
    assert cancelled["stream"], (
        "the child's stream was left running after the parent gave up; the child "
        "must be cancelled, not abandoned"
    )
    assert not any(
        sid.startswith("sub-parent-timeout") and busy
        for sid, busy in server._session_busy.items()
    ), "a timed-out child is still marked busy"


# ── 3. cancelling the parent returns the slot at once ─────────────────────


def test_cancelling_the_parent_cancels_the_child_and_returns_the_slot(
    tmp_path, monkeypatch,
):
    server = _server(tmp_path, monkeypatch)

    async def hanging_stream(messages, tools=None):
        await asyncio.Event().wait()
        yield {"content": "", "tool_calls": None, "finish_reason": "stop",
               "usage": {}}

    server.llm.chat_stream = hanging_stream

    async def fake_chat(messages, tools=None):
        return {"content": "", "tool_calls": None}

    server.llm.chat = fake_chat

    async def scenario():
        task = asyncio.create_task(server._run_subagent_tool(
            parent_session_id="parent-cancel", parent_cwd=str(tmp_path),
            prompt="go", timeout_s=60, intent="cancel me",
        ))
        # Let it take a slot and start the child's turn.
        for _ in range(200):
            if server._subagent_busy.get("parent-cancel"):
                break
            await asyncio.sleep(0.01)
        assert server._subagent_busy.get("parent-cancel"), "no slot was ever taken"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())

    assert not server._subagent_busy.get("parent-cancel"), (
        "the slot is still held after the parent's turn was cancelled, so the next "
        "delegation waits for a child nobody is running"
    )
    assert not any(
        sid.startswith("sub-parent-cancel") and busy
        for sid, busy in server._session_busy.items()
    ), "the child is still marked busy after its parent was cancelled"


# ── 4. nine slots, and the least recently closed one goes next ────────────


def test_the_tenth_delegation_waits_and_takes_nothing(tmp_path, monkeypatch):
    """Nine are the bound; a tenth waits, and the call's own timeout ends the wait."""
    server = _server(tmp_path, monkeypatch)
    _stub_reply(server, "unused")
    server._subagent_busy["parent-full"] = set(range(1, EmrgServer.SUBAGENT_SLOTS + 1))

    outcome = _delegate(server, "parent-full", tmp_path, timeout_s=1)

    assert outcome.get("error"), outcome
    assert "did not finish" in outcome["error"] or "slot" in outcome["error"], (
        outcome["error"]
    )
    child_root = tmp_path / ".emrg" / "sessions" / "parent-full" / "sub-agent"
    assert not child_root.exists() or not any(child_root.iterdir()), (
        "a delegation that never got a slot still created a child"
    )


def test_the_next_slot_is_the_one_closed_longest_ago(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    parent = "parent-lru"
    taken = [server._subagent_acquire(parent) for _ in range(EmrgServer.SUBAGENT_SLOTS)]
    assert sorted(taken) == list(range(1, EmrgServer.SUBAGENT_SLOTS + 1)), taken
    with pytest.raises(LookupError):
        server._subagent_acquire(parent)

    # Two slots come free, and the *later-numbered* one was closed first.
    server._subagent_release(parent, 5)
    server._subagent_release(parent, 7)
    table = server._subagent_slot_closes(parent)
    table[5] = 200.0
    table[7] = 100.0

    assert server._subagent_acquire(parent) == 7, (
        "the slot closed longest ago must go next; taking the first free slot "
        "instead makes reuse depend on dictionary order"
    )


# ── 5. a reused slot is emptied first ─────────────────────────────────────


def test_a_reused_slot_is_emptied_before_it_is_used(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    _stub_reply(server, "first run")
    parent = "parent-reuse"

    _force_slot_choice(server, parent, 1)
    first = _delegate(server, parent, tmp_path)
    assert "error" not in first, first
    child_id = _report_field(first["text"], "session")
    assert child_id == "sub-parent-reuse-1", child_id
    child_dir = Path(_report_field(first["text"], "cwd")) / ".emrg" / "sessions" / child_id
    assert child_dir.is_dir(), child_dir
    (child_dir / "stale-marker.txt").write_text("from the first child", encoding="utf-8")
    old_history = len(child_dir.joinpath("history.jsonl").read_text(encoding="utf-8").splitlines())
    assert old_history > 0

    _stub_reply(server, "second run")
    _force_slot_choice(server, parent, 1)
    second = _delegate(server, parent, tmp_path)
    assert "error" not in second, second
    assert _report_field(second["text"], "session") == child_id

    assert not (child_dir / "stale-marker.txt").exists(), (
        "the slot was reused without being emptied first, so the previous child's "
        "files are still in the directory the new child is told is its own"
    )
    fresh = child_dir.joinpath("history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(fresh) < old_history + 1 or "from the first child" not in "".join(fresh), (
        "the previous child's history survived into the reused slot"
    )


# ── 6. the close stamp is taken after the child's memory write ────────────


def test_the_slot_is_closed_only_after_the_childs_memory_was_written(
    tmp_path, monkeypatch,
):
    """Measured, not asserted in prose: the reflection is slow, and the stamp waits.

    The reflection is fire-and-forget, so the only thing that can order the two is
    an explicit wait. Without it the close precedes the write, and the next child of
    that slot clears a directory the previous one is still writing into.
    """
    server = _server(tmp_path, monkeypatch)
    _stub_reply(server, "a reply long enough to be worth reflecting on")
    parent = "parent-memory"
    reflection = {"finished_at": None, "seen": 0}

    async def slow_reflection(messages, tools=None):
        text = json.dumps(messages, ensure_ascii=False).lower()
        if "memory" in text:
            await asyncio.sleep(0.2)
            reflection["finished_at"] = time.time()
            reflection["seen"] += 1
        return {"content": "", "tool_calls": None}

    server.llm.chat = slow_reflection

    _force_slot_choice(server, parent, 1)
    outcome = _delegate(server, parent, tmp_path)

    assert "error" not in outcome, outcome
    assert reflection["seen"] > 0, (
        "the child never reflected, so this test has no subject — the premise of "
        "the ordering is that a memory write was actually scheduled"
    )
    closed_at = server._subagent_slot_closes(parent)[1]
    assert closed_at >= reflection["finished_at"], (
        f"the slot was stamped closed at {closed_at} but the child's memory write "
        f"only finished at {reflection['finished_at']}: closing before the write "
        "means the next child of this slot can clear a directory still in use"
    )


# ── 7. the depth limit refuses, and says so ───────────────────────────────


def test_a_session_at_the_depth_limit_refuses_and_says_so(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    _stub_reply(server, "unused")
    parent = Session.create_with_id("parent-deep", tmp_path)
    parent.set_depth(EmrgServer.SUBAGENT_MAX_DEPTH)

    outcome = _delegate(server, "parent-deep", tmp_path)

    assert outcome.get("error"), outcome
    assert "refused" in outcome["error"], outcome["error"]
    assert str(EmrgServer.SUBAGENT_MAX_DEPTH) in outcome["error"], (
        "the refusal must name the limit it hit, or a caller cannot tell a boundary "
        f"from a bug: {outcome['error']}"
    )
    child_root = tmp_path / ".emrg" / "sessions" / "parent-deep" / "sub-agent"
    assert not child_root.exists() or not any(child_root.iterdir()), (
        "a refused delegation still started a child"
    )


def test_a_childs_depth_is_its_parents_plus_one(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    _stub_reply(server, "child answer")
    parent = Session.create_with_id("parent-depth", tmp_path)
    parent.set_depth(2)

    outcome = _delegate(server, "parent-depth", tmp_path)

    assert "error" not in outcome, outcome
    child_id = _report_field(outcome["text"], "session")
    child_cwd = Path(_report_field(outcome["text"], "cwd"))
    reloaded = Session.load(child_id, child_cwd)
    assert reloaded.depth == 3, (
        "the child's depth must be written into its own meta: the child is the one "
        "that has to know how deep it is when it delegates again"
    )


# ── 7. the child runs at its parent's tier, not one it can choose ─────────


def test_the_child_runs_at_its_parents_sandbox_tier(tmp_path, monkeypatch):
    """Authorization a caller can pick for the thing it authorizes is not a boundary.

    The tool takes no tier argument on purpose, so the only tier a child can have is
    the parent's. Asserted on the child's own `meta.json` — the file a later turn of
    that child reads — rather than on the object this call happened to hold.
    """
    server = _server(tmp_path, monkeypatch)
    _stub_reply(server, "child answer")
    parent = Session.create_with_id("parent-tier", tmp_path)
    parent.set_sandbox("read-only")

    outcome = _delegate(server, "parent-tier", tmp_path)

    assert "error" not in outcome, outcome
    child_id = _report_field(outcome["text"], "session")
    child_cwd = Path(_report_field(outcome["text"], "cwd"))
    reloaded = Session.load(child_id, child_cwd)
    assert reloaded.sandbox == "read-only", (
        "the child is stored at a different tier than its parent; a child that does "
        "not inherit is a child nobody confined, and a child that chooses is a "
        "delegation that escapes its parent's boundary"
    )


# ── 8. one greppable line per delegation ──────────────────────────────────


def test_every_delegation_is_one_greppable_log_line(tmp_path, monkeypatch, caplog):
    server = _server(tmp_path, monkeypatch)
    _stub_reply(server, "logged")

    with caplog.at_level(logging.INFO, logger="emrg.server.daemon"):
        outcome = _delegate(server, "parent-log", tmp_path)

    assert "error" not in outcome, outcome
    lines = [r.getMessage() for r in caplog.records]
    delegations = [m for m in lines if "delegate" in m]
    assert delegations, (
        "no line names the delegation, so one delegation cannot be picked out of "
        f"emrgd.log:\n" + "\n".join(lines[-8:])
    )
    for message in delegations:
        assert "[sub-" in message, (
            f"a delegation line carries no slot label: {message!r}"
        )
    assert any("delegate start" in m for m in delegations)
    assert any("delegate done" in m for m in delegations)


# ── the tool's own contract ───────────────────────────────────────────────


def test_the_tool_refuses_a_call_with_no_parent():
    """A call outside a daemon turn has no parent, and inventing one is worse."""
    tool = SubagentTool(lambda **_: None)  # never called

    result = _run(tool.execute({"prompt": "do it"}))

    assert result.error
    assert "parent session" in result.content


def test_the_tool_refuses_an_impossible_timeout():
    tool = SubagentTool(lambda **_: None)

    for bad in (0, -5, "soon", 2.5):
        result = _run(tool.execute({
            "prompt": "do it",
            "_parent_session_id": "p",
            "_parent_cwd": "/tmp",
            "timeout_s": bad,
        }))
        assert result.error, f"timeout_s={bad!r} was accepted"
        assert "timeout_s" in result.content


def test_the_tool_passes_the_default_timeout_through():
    seen = {}

    async def runner(**kwargs):
        seen.update(kwargs)
        return {"text": "ok"}

    tool = SubagentTool(runner)
    result = _run(tool.execute({
        "prompt": "do it",
        "_parent_session_id": "p",
        "_parent_cwd": "/tmp",
        "intent": "because",
    }))

    assert not result.error, result.content
    assert seen["timeout_s"] == DEFAULT_TIMEOUT_S
    assert seen["parent_session_id"] == "p"
    assert seen["intent"] == "because"


def test_a_task_delegation_inherits_the_turns_tier(tmp_path, monkeypatch):
    """A scheduled task's tier reaches its child, and it is the *turn's* tier.

    A task's tier is resolved from `tasks.yml` into the request and never written
    onto the session object, so a child that read the session inherited nothing and
    ran unconfined while the task body itself was confined — the escalation this
    test exists to refuse. Driven through `_inject_tool_arguments` and then the
    tool, never by handing the tier straight to the runner: the injection is the
    half that was missing, and a test below it would have stayed green.
    """
    server = _server(tmp_path, monkeypatch)
    _stub_reply(server, "child answer")
    parent = Session.create_with_id("parent-task-turn", tmp_path)
    assert parent.sandbox is None, (
        "this test models a scheduled task, whose tier lives on the request; a "
        "parent that carries one is the client shape the other test covers"
    )
    req = TaskRequest(
        id="tier-turn", session_id=parent.session_id, cwd=str(tmp_path),
        prompt="a task's tick", sandbox="read-only",
    )

    args = {"prompt": "do the job", "timeout_s": 10}
    EmrgServer._inject_tool_arguments("subagent", args, parent, req, None)

    assert args.get("_parent_sandbox") == "read-only", (
        "the turn's tier never reached the call, so the child cannot inherit it: "
        f"{args}"
    )
    result = _run(SubagentTool(server._run_subagent_tool).execute(args))
    assert not result.error, result.content

    child = Session.load(
        _report_field(result.content, "session"),
        Path(_report_field(result.content, "cwd")),
    )
    assert child.sandbox == "read-only", (
        "a child of a confined task ran at a wider tier; a delegation is the same "
        "authorisation one level down, never a way around it"
    )
