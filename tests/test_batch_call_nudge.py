"""The daemon, not the prompt, states the batching rule — at the tail of the next request.

Issue #2683. "A round may carry several tool calls" lived only in the system
prompt. It held while it was the most recent thing said and decayed within a
round or two, after which the model went back to one call per round: a
requirement stated in the prompt sits too far from the point where the next
round's calls are decided.

So the daemon appends a **system** message naming `#RULE_PBEC` — the tag on the
rule's heading in `system.j2` — to the **request** that follows a round whose call
count is below five. A **system** turn and not a user one: the reminder is the
daemon's instruction, while a user turn is the host's voice — the role a client
renders as the host's bubble and a transcript attributes to the host. The readings
here are the `messages` lists the provider was actually handed — captured where the
stream is consumed — so nothing asserts the shape of the code that built them:
removing the append, inverting its condition, or moving it ahead of the tool
results each reddens a test below.

Issue #3193 is the second half, and it is what the first version got wrong. The
body was `batch call` — a name the prompt never used, so the model had to invent a
meaning for it and then quoted its own invention back in later rounds. A **tag**
answers that by being a locator rather than an abbreviation: the reminder says where
to read the rule, and the rule is what the reader reads. The same report measured
the condition as narrower than the rule it refers to — `== 1` reminded a one-call
round and left two, three and four alone, though all four are sub-five.

`request_messages` is a **copy**, which is what keeps the reminder out of every
file: session history, `llm.jsonl` and a rebuilt request are all read from the
loop's own `messages`, and the reminder is never in it.

No daemon is started, stopped or restarted: `_run_tool_loop` is called directly
with its broadcast stubbed, the shape `tests/test_turn_context_is_persisted.py`
uses.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import BATCH_CALL_NUDGE, BATCH_CALL_RULE_TAG, EmrgServer
from emrg.server.tool_types import ToolResult
from emrg.session import Session

SESSION_ID = "batch-call-nudge"
NUDGE = {"role": "system", "content": BATCH_CALL_NUDGE}

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"


class _StubTool:
    """A tool that executes nothing: the subject is the reminder, not the tool."""

    async def execute(self, args):
        return ToolResult(content="ok")


def _make_server(config: LlmConfig | None = None) -> EmrgServer:
    """A minimal server with the agents log pointed away from the host's."""
    server = EmrgServer(config or LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = Path(tempfile.mkdtemp()) / "agents.yml"
    return server


def _drive(
    tmp_path: Path,
    monkeypatch,
    script: list[int],
    config: LlmConfig | None = None,
) -> tuple[list[list[dict]], Session, list[dict]]:
    """Run one tool loop. `script[k]` is how many tool calls round `k+1` dispatches.

    Returns `(requests, session, frames)`: `requests[k]` is the `messages` list the
    provider was handed on request `k+1`, exactly as it was sent.
    """
    monkeypatch.setattr(
        daemon_mod, "_PLANTED_FIRE_MARKER_PATH", tmp_path / "planted-fire-heartbeat"
    )
    monkeypatch.setattr(
        daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH", tmp_path / "planted-fire-round"
    )
    server = _make_server(config)
    server.tools.get = lambda name: _StubTool()  # type: ignore[method-assign]
    session = Session.create_with_id(SESSION_ID, tmp_path)
    requests: list[list[dict]] = []
    frames: list[dict] = []
    state = {"round": 0}

    async def fake_stream(messages, tools=None):
        index = state["round"]
        state["round"] += 1
        requests.append([dict(m) for m in messages])
        calls = script[index] if index < len(script) else 0
        if calls:
            yield {
                "content": None,
                "tool_calls": [
                    {
                        "index": i,
                        "id": f"call_{index}_{i}",
                        "function": {"name": "read", "arguments": '{"intent": "x"}'},
                    }
                    for i in range(calls)
                ],
                "finish_reason": "tool_calls",
                "usage": None,
            }
        else:
            yield {
                "content": "done",
                "tool_calls": None,
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    async def fake_broadcast(session_id, payload):
        frames.append(payload)

    server.llm.chat_stream = fake_stream
    server._broadcast = fake_broadcast

    # Only the summariser is stubbed: the rebuild it feeds runs for real, which
    # is the thing requirement 5 is about. A round only reaches it when
    # compaction fires, and only one test configures that.
    async def fake_summary(session, records, source=None):
        return "a summary of the turn"

    server._compact_with_fallback = fake_summary  # type: ignore[method-assign]

    req = TaskRequest(id="req-nudge", session_id=session.session_id, prompt="go")
    asyncio.run(server._run_tool_loop(req, None, session))
    return requests, session, frames


def _nudges(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "system"
               and m.get("content") == BATCH_CALL_NUDGE)


def _describe(messages: list[dict]) -> str:
    return " → ".join(str(m.get("role", "?")) for m in messages)


# ── requirement 1: the request after a single-call round ends in the reminder ──


def test_the_request_after_a_single_call_round_ends_in_the_reminder(tmp_path, monkeypatch):
    """Requirement 1. A round that dispatched one call is reminded in the next one."""
    requests, _, _ = _drive(tmp_path, monkeypatch, [1, 0])

    assert len(requests) == 2, f"the loop sent {len(requests)} request(s), not 2"
    # The premise: the first request has no previous round, so it carries none —
    # otherwise "the reminder is there" would be satisfied by every request.
    assert _nudges(requests[0]) == 0, _describe(requests[0])
    assert requests[1][-1] == NUDGE, (
        f"the second request ends with {requests[1][-1]!r}, not the reminder: "
        f"{_describe(requests[1])}"
    )
    assert _nudges(requests[1]) == 1, _describe(requests[1])


# ── requirement 2: the condition is the rule's range — one to four, not exactly one ──


def test_a_two_call_round_is_reminded_like_a_one_call_round(tmp_path, monkeypatch):
    """Requirement 2, rewritten: two calls is a sub-five round, so it owes an answer.

    This arm used to assert the opposite — "several calls already did what is asked" — which read
    "several" as the shape the rule wants. The shape is five or more, so a two-call round is a round
    that owes an answer, and withholding the reminder from it is the half of the rule the old
    condition dropped (reported 2026-11-24).
    """
    requests, _, _ = _drive(tmp_path, monkeypatch, [2, 0])

    assert len(requests) == 2
    assert requests[1][-1] == NUDGE, (
        f"a two-call round went unreminded: {_describe(requests[1])}"
    )
    assert _nudges(requests[1]) == 1, _describe(requests[1])


def test_no_reminder_follows_a_round_that_reached_the_rules_shape(tmp_path, monkeypatch):
    """The upper end of the range: five calls is the shape the rule asks for, and it is left alone.

    Both ends are asserted because a condition widened to "any round at all" would pass the test above
    while reminding the round that already complied — the reminder would then be noise, and noise is
    what a model learns to skip.
    """
    requests, _, _ = _drive(tmp_path, monkeypatch, [5, 0])

    assert len(requests) == 2
    assert _nudges(requests[1]) == 0, (
        f"a five-call round was reminded anyway: {_describe(requests[1])}"
    )


def test_no_reminder_precedes_a_round_that_dispatched_no_call(tmp_path, monkeypatch):
    """Requirement 2, the zero arm: no previous round is a round that issued none.

    The observable form of "a round with no tool calls is not reminded" is the
    turn's own first request — the count starts at zero and only a tool-call
    round moves it — plus the helper's zero arm read directly.
    """
    requests, _, _ = _drive(tmp_path, monkeypatch, [0])

    assert len(requests) == 1
    assert _nudges(requests[0]) == 0, _describe(requests[0])
    assert daemon_mod._with_batch_call_nudge([{"role": "user", "content": "go"}], 0) == [
        {"role": "user", "content": "go"}
    ]


# ── requirement 3: every single-call round of a run, not just the first ────────


def test_each_of_two_consecutive_single_call_rounds_is_reminded(tmp_path, monkeypatch):
    """Requirement 3. The count is per round, so it does not fire once and stop."""
    requests, _, _ = _drive(tmp_path, monkeypatch, [1, 1, 0])

    assert len(requests) == 3, f"the loop sent {len(requests)} request(s), not 3"
    for index in (1, 2):
        assert requests[index][-1] == NUDGE, (
            f"request {index + 1} ends with {requests[index][-1]!r}: "
            f"{_describe(requests[index])}"
        )
        assert _nudges(requests[index]) == 1, _describe(requests[index])


# ── requirement 4: it exists only in that request ─────────────────────────────


def test_the_reminder_reaches_no_file_and_no_rebuilt_request(tmp_path, monkeypatch):
    """Requirement 4. Sent once, and readable from nothing the turn wrote."""
    requests, session, _ = _drive(tmp_path, monkeypatch, [1, 1, 0])

    # The premise: it really was sent. Without this the rest would pass on a
    # build that never appended it at all.
    assert any(_nudges(r) for r in requests), (
        f"the reminder was never sent, so 'where is it persisted' has no subject: "
        f"{[_describe(r) for r in requests]}"
    )

    session_dir = session._history_path.parent
    hits = []
    for path in sorted(session_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if BATCH_CALL_NUDGE in text:
            hits.append(str(path.relative_to(session_dir)))
    assert hits == [], f"the reminder is persisted in {hits}"

    rebuilt = session.get_messages_for_llm()
    assert _nudges(rebuilt) == 0, (
        "a request rebuilt from the records carries the reminder: "
        f"{_describe(rebuilt)}"
    )


# ── requirement 5: a rebuild neither duplicates it nor leaves a residue ───────


def test_a_compacted_round_still_carries_exactly_one_reminder(tmp_path, monkeypatch):
    """Requirement 5. The request is rebuilt from records before it is sent.

    The threshold is set so compaction fires on every round, which is the
    harshest form of the case: the reminder is derived from a list that was
    replaced since the last round, and must come out once, at the tail.
    """
    config = LlmConfig(
        base_url="http://localhost",
        api_key="test",
        context_window=10,
        auto_compact_threshold=0.5,
    )
    requests, _, frames = _drive(tmp_path, monkeypatch, [1, 0], config=config)

    # The premise: the rebuild branch really ran. A compaction that silently
    # failed would leave the reminder correct for the wrong reason.
    auto_compacts = [
        f for f in frames
        if f.get("type") == "compact_result" and f.get("auto") is True
    ]
    assert auto_compacts, f"auto-compact never ran, so nothing was rebuilt: {frames}"

    assert len(requests) == 2
    assert requests[1][-1] == NUDGE, _describe(requests[1])
    assert _nudges(requests[1]) == 1, _describe(requests[1])


def test_the_reminder_is_derived_without_touching_the_loops_own_list():
    """Requirement 5, at the seam: the list a rebuild reads is never written to.

    Two consecutive single-call rounds derive their request from the *same*
    `messages`. An append in place would leave one reminder per round in that
    list, and the second request would carry two.
    """
    source = [{"role": "user", "content": "go"}]

    first = daemon_mod._with_batch_call_nudge(source, 1)
    second = daemon_mod._with_batch_call_nudge(source, 1)

    assert _nudges(first) == 1 and _nudges(second) == 1
    assert source == [{"role": "user", "content": "go"}], (
        "the reminder was written into the list the loop keeps, so the next "
        f"round would inherit it: {source!r}"
    )


# ── requirement 6: the condition is the rule's whole range ───────────────────


def test_the_condition_is_the_rules_range_at_the_seam():
    """One to four reminded, zero and five-or-more not — the range read at the helper.

    The loop arms above read two counts each; this reads the whole range, so a condition that moved one
    end without the other is visible as the count it is wrong for rather than as a passing pair.
    """
    source = [{"role": "user", "content": "go"}]
    reminded = [n for n in range(0, 8) if _nudges(daemon_mod._with_batch_call_nudge(source, n))]

    assert reminded == [1, 2, 3, 4], (
        f"the reminder fires for {reminded}, not the rule's sub-five range 1-4: a round that issued "
        "nothing took no action to batch, and a round of five or more already has the shape the rule "
        "asks for"
    )


# ── requirement 7: the tag it names is one the prompt actually carries ───────


def test_the_reminder_names_the_tag_the_prompt_carries():
    """The two carriers of one identifier, compared in both directions.

    The reminder is the model's only pointer to the rule, and the heading is where it lands. Either half
    can move alone — a reminder naming a tag no heading carries leaves the model exactly where `batch
    call` left it, inventing a meaning for a name the prompt never uses; a heading whose tag the reminder
    stopped naming leaves the rule unreachable from the one place it is needed. So both are read here,
    and the reminder's opener is asserted too: a system turn that does not say it is a reminder reads as
    one more thing the host said.
    """
    assert BATCH_CALL_RULE_TAG in BATCH_CALL_NUDGE, (
        f"the reminder does not name its locator: {BATCH_CALL_NUDGE!r}"
    )
    assert BATCH_CALL_NUDGE.startswith("reminder:"), (
        f"the reminder does not say what it is: {BATCH_CALL_NUDGE!r}"
    )

    prompt = SYSTEM_PROMPT.read_text(encoding="utf-8")
    tagged = [line for line in prompt.splitlines()
              if line.startswith("## ") and BATCH_CALL_RULE_TAG in line]
    assert len(tagged) == 1, (
        f"`{BATCH_CALL_RULE_TAG}` is on {len(tagged)} headings in the prompt, not one, so the reminder "
        f"resolves to nothing a reader can find: {tagged}"
    )
