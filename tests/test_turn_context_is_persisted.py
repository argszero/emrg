"""A turn's `[context]` message is persisted, so a rebuilt request is a prefix of it.

Rant 2026-10-15T12:57:39 — the "prediction for the next turn" its own progress named as
the step that would close it. Each round of a turn sent a trailing `[context]` user
message (the clock, the write boundary, the two memory indexes), built by
`_build_context_message` and inserted by `_inject_context_message` immediately before
the user prompt. It was never written to the session.

So the request a turn sent read `[system, history, ctx, user]`, and the next turn's
request — rebuilt from records, which could not reproduce that message — read
`[system, history, user, assistant, …, ctx, user]`. The two diverge exactly at the
context message, and everything behind it is re-processed as uncached prompt tokens.
Measured on the session that filed it (`llm.jsonl` `usage.cache_hit_tokens`, miss =
`prompt_tokens − cache_hit_tokens`): **87,440 of 107,920 tokens uncached** at the
2026-10-16T07:43:44 turn boundary, against 134–261 tokens for an ordinary round inside
a turn.

The cure is one record — the context message is written to the session *before* the
user record it precedes, and `records_to_messages` turns it back into a user message in
that same position. `_run_tool_loop` is driven here with a stubbed LLM that captures
the exact `messages` it was handed, so the readings compare two **requests**, not the
shape of the code that built them: deleting either the write or the conversion branch
turns them red.

No daemon is started, stopped or restarted: `_run_tool_loop` is called directly with
its broadcast stubbed, the shape `tests/test_message_time_is_the_daemons.py` uses.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.session import Session

SESSION_ID = "turn-context-prefix"


def _make_server() -> EmrgServer:
    """A minimal server with the projects log pointed away from the host's."""
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def _drive_turns(
    tmp_path: Path, monkeypatch, prompts: list[str]
) -> tuple[list[list[dict]], Session]:
    """Run one complete tool loop per prompt. Returns `(requests, session)`.

    `requests[k]` is the `messages` list the provider was handed at turn `k` — the exact
    object, captured where the stream is consumed, so nothing about the code's shape is
    asserted. Both planted-fire markers are pointed at `tmp_path`: the loop stamps host
    state at round finalization, and a suite run must not write `~/.emrg/logs`.
    """
    monkeypatch.setattr(
        daemon_mod, "_PLANTED_FIRE_MARKER_PATH", tmp_path / "planted-fire-heartbeat"
    )
    monkeypatch.setattr(
        daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH", tmp_path / "planted-fire-round"
    )
    server = _make_server()
    session = Session.create_with_id(SESSION_ID, tmp_path)
    requests: list[list[dict]] = []

    async def fake_stream(messages, tools=None):
        requests.append([dict(m) for m in messages])
        yield {
            "content": "the reply",
            "tool_calls": None,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }

    async def fake_broadcast(session_id, payload):
        return None

    server.llm.chat_stream = fake_stream
    server._broadcast = fake_broadcast

    async def _run_all():
        for n, prompt in enumerate(prompts):
            req = TaskRequest(
                id=f"req-{n}", session_id=session.session_id, prompt=prompt
            )
            await server._run_tool_loop(req, None, session)

    asyncio.run(_run_all())
    return requests, session


def _records_on_disk(session: Session) -> list[dict]:
    lines = session._history_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _describe(msgs: list[dict]) -> str:
    """A one-line rendering of a request's roles, for a readable failure."""
    return " → ".join(
        ("[context]" if str(m.get("content", "")).startswith("[context]") else m.get("role", "?"))
        for m in msgs
    )


# ── the claim: a turn's request is a prefix of the next turn's ─────────────


def test_the_first_turn_is_a_prefix_of_the_second_turns_request(tmp_path, monkeypatch):
    """Issue #2311, acceptance 1 — the property that was false, measured on requests.

    Turn 1's request minus its system prompt must survive, byte for byte and in order,
    as the head of turn 2's. On the tree this pins, it does not: the context message was
    only ever in the live request, so turn 2's rebuild put the user message where the
    context had been and the provider re-processed everything behind that point.
    """
    requests, _ = _drive_turns(tmp_path, monkeypatch, ["first", "second"])

    assert len(requests) == 2, f"expected two turns, captured {len(requests)}"
    sent_first = requests[0][1:]  # everything after the system prompt
    assert sent_first, "turn 1 sent no history at all — the fixture did not take"

    head = requests[1][1:1 + len(sent_first)]

    assert head == sent_first, (
        "the request a turn sent is not a prefix of the next turn's rebuild, so the "
        "provider's prompt cache stops at the divergence and re-processes the rest of "
        "the previous turn as uncached tokens.\n"
        f"  turn 1: {_describe(requests[0])}\n"
        f"  turn 2: {_describe(requests[1])}\n"
        f"  head of turn 2: {_describe(head)}\n"
        f"  turn 1's own:   {_describe(sent_first)}"
    )


def test_the_context_rides_immediately_before_the_user_prompt(tmp_path, monkeypatch):
    """The placement half — and the reason the write has to sit where it does.

    A fix that dropped the injection would satisfy the prefix property trivially, so the
    context is asserted to still be *there*, in the three shapes the rant's placement
    rule covers: right before a final user message, at any round.
    """
    requests, _ = _drive_turns(tmp_path, monkeypatch, ["first", "second"])

    for n, request in enumerate(requests, start=1):
        assert request[-1].get("role") == "user", (
            f"turn {n}: the request does not end in a user message: {_describe(request)}"
        )
        assert str(request[-2].get("content", "")).startswith("[context]"), (
            f"turn {n}: the context message is no longer immediately before the user "
            f"prompt: {_describe(request)}"
        )


# ── the record: what was sent is what was stored, once per turn ────────────


def test_the_stored_context_is_exactly_what_was_sent(tmp_path, monkeypatch):
    """Issue #2311, acceptance 2 — one build per turn, and the stored bytes are the sent ones.

    The whole value of the write is that the rebuild reproduces the request byte for
    byte; storing a re-render (a second call to `_build_context_message` a second later)
    would produce a *different* message and the prefix would break again at the same
    place, only less visibly.
    """
    requests, session = _drive_turns(tmp_path, monkeypatch, ["only"])

    stored = [r for r in _records_on_disk(session) if r.get("type") == "context"]

    assert len(stored) == 1, (
        f"one turn wrote {len(stored)} context record(s) — the rebuild needs exactly the "
        "one the turn sent"
    )
    assert "role" not in stored[0], (
        "the stored frame carries a role, so it matches every reader that selects "
        "history records by `role == \"user\"` — the conversion in `records_to_messages` "
        "is the one place that decides it reads as a user message"
    )
    assert stored[0]["content"] == requests[0][-2]["content"], (
        "the stored context is not the message that was sent: a later rebuild reproduces "
        "different bytes than the request it is supposed to be a prefix of"
    )


def test_each_turn_writes_one_context_record_and_the_count_ignores_it(tmp_path, monkeypatch):
    """Issue #2311, acceptance 3 — the record is storage for the model, not a message.

    `message_count` is what a client displays as the session's size, and `append_message`
    counts only `type == "message"`. A `context` record must not inflate it, and it must
    sit immediately before the user record of its own turn so record order is the
    request's order.
    """
    prompts = ["first", "second"]
    _, session = _drive_turns(tmp_path, monkeypatch, prompts)
    records = _records_on_disk(session)

    contexts = [i for i, r in enumerate(records) if r.get("type") == "context"]
    assert len(contexts) == 2, (
        f"two turns wrote {len(contexts)} context record(s), not one per turn"
    )
    for i, prompt in zip(contexts, prompts):
        follower = records[i + 1]
        assert follower.get("type") == "message" and follower.get("role") == "user", (
            f"the context record at {i} is not followed by a user record: {follower}"
        )
        assert follower.get("content") == prompt

    user_facing = [r for r in records if r.get("type") == "message"]
    assert session.message_count == len(user_facing), (
        f"message_count is {session.message_count} for {len(user_facing)} message "
        "record(s): a context record was counted as a message"
    )
