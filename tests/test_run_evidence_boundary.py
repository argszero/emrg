"""A run's evidence must start where the run started, not at the session's tail.

Why this file exists
--------------------
A scheduled task runs in **one** session for its whole life: `emrg-evolution-<name>`
is reused by every run. So "the recent history of that session" is not evidence about
*this* run — it is a window that mostly holds the *previous* one whenever this run was
light on narrative (a vote-only run, a verification run, a run whose work is reading).

Measured on `emrg-task.jsonl` 2026-09-30 (issue filed 2026-10-08): two consecutive
records whose `work` strings describe the **same** merge — the second record naming the
first run's work as if it had just happened, and omitting everything the second run
found (the two differ `68` vs `54` tool calls, so the second run was real work).

`work` is also what `recommend_slowdown` rides, so a run mis-read this way can also
mis-cadence itself. And `work` is the only per-run summary a human reads, in the GUI's
task recent-runs table — which is also what a cycle reads when it reconstructs state.

The seam under test
-------------------
The scheduler already knows when a run was dispatched — it is `cycle_time`, the very
value the `task` frame carries as its `timestamp` and its id. Nothing had to be
invented: it had to be *carried* to the summariser. Three assertions, on three layers,
so a break anywhere is visible:

* `records_since` — the pure rule, including the two fail-open directions;
* `Session.get_messages_for_llm(since=…)` — that the rule reaches the reader, against a
  session file on disk holding two runs;
* `EmrgServer._task_vibe_check` — the outcome the defect is about: what the **judge**
  is given, read off the messages the LLM call actually received (a stub `llm` records
  them), because asserting on the history helper alone would be one level short of the
  claim. Both directions are asserted: with the boundary the previous run's work is
  absent, and without it the same session still shows it — otherwise a filter that
  dropped everything would pass.
* the **hand-over** itself, at the sender: every layer above can be correct while the
  fix does nothing, since the instant has to travel from the scheduler to the daemon.
  That is where this bug lived (`cycle_time` existed and was simply not carried), so a
  guard that stopped at the daemon would leave the defect reproducible.

`python -m pytest tests/test_run_evidence_boundary.py -q`
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from emrg.server.daemon import EmrgServer
from emrg.server.scheduler import TaskHandler
from emrg.session import Session, records_since

# The salient work of the *previous* run, and of this one. Distinct vocabularies so
# "did the judge see it" is answerable by substring, the way the defect was measured.
PREVIOUS_RUN_WORK = "merged PR #1760 and cut the v0.3.6 TUI crash"
THIS_RUN_WORK = "voted on the landing tree and wrote the cycle record"

RUN_ONE_START = "2026-10-08T09:00:04"
RUN_TWO_START = "2026-10-08T13:08:25"


def _record(role: str, content: str, at: str) -> dict:
    return {"type": "message", "role": role, "content": content, "timestamp": at}


def _two_run_session(cwd) -> Session:
    """One session file holding two runs, as the real task session is."""
    session = Session.create_with_id("emrg-evolution-boundary-test", cwd)
    for record in (
        _record("user", "run one prompt", "2026-10-08T09:00:05"),
        _record("assistant", f"I {PREVIOUS_RUN_WORK}", "2026-10-08T09:20:00"),
        _record("user", "run two prompt", "2026-10-08T13:08:26"),
        _record("assistant", f"I {THIS_RUN_WORK}", "2026-10-08T13:30:00"),
    ):
        session.append_message(dict(record))
    return session


def _contents(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages)


# --- the pure rule -----------------------------------------------------------


def test_records_since_keeps_only_the_run_it_names() -> None:
    records = [
        _record("assistant", "older", "2026-10-08T09:20:00"),
        _record("assistant", "newer", "2026-10-08T13:30:00"),
    ]
    kept = records_since(records, RUN_TWO_START)
    assert [r["content"] for r in kept] == ["newer"]


def test_a_boundary_on_the_record_is_inclusive() -> None:
    """The `task` frame's own timestamp is the first record's, so >= is the rule."""
    records = [_record("user", "the run's own prompt", RUN_TWO_START)]
    assert records_since(records, RUN_TWO_START) == records


@pytest.mark.parametrize("since", ["", "not-a-timestamp", "2026-13-45T99:99:99"])
def test_an_unusable_boundary_leaves_the_history_alone(since: str) -> None:
    """A window whose start is unknown is the window the session always had.

    Guessing a boundary here would be worse than having none: it is the direction
    that silently deletes evidence, and it would revert to the old behaviour only
    for the cycles nobody is looking at.
    """
    records = [_record("assistant", "work", "2026-10-08T09:20:00")]
    assert records_since(records, since) == records


@pytest.mark.parametrize(
    "record",
    [
        {"type": "message", "role": "assistant", "content": "no timestamp at all"},
        _record("assistant", "unreadable", "not-a-timestamp"),
        _record("assistant", "aware, while the boundary is naive", "2026-10-08T13:30:00+00:00"),
    ],
)
def test_a_record_whose_time_cannot_be_ordered_is_kept(record: dict) -> None:
    """Unmeasurable is not old.

    Dropping it would remove a tool call from the window with nothing to show for
    it; a summary that read one record too many is the smaller error, and stored
    timestamps are naive local time while a boundary could be either.
    """
    assert records_since([record], RUN_TWO_START) == [record]


# --- the reader --------------------------------------------------------------


def test_get_messages_for_llm_can_start_at_a_run_boundary(tmp_path) -> None:
    session = _two_run_session(tmp_path)
    everything = session.get_messages_for_llm()
    assert PREVIOUS_RUN_WORK in _contents(everything), (
        "the fixture must actually hold the previous run, or the assertion below "
        "would pass for the wrong reason"
    )
    assert THIS_RUN_WORK in _contents(everything)

    this_run = session.get_messages_for_llm(since=RUN_TWO_START)
    assert THIS_RUN_WORK in _contents(this_run)
    assert PREVIOUS_RUN_WORK not in _contents(this_run), (
        "the previous run's work is still in the window this run's reader is given"
    )


def test_without_a_boundary_the_reader_is_unchanged(tmp_path) -> None:
    """The default is the whole history — what this method always returned."""
    session = _two_run_session(tmp_path)
    assert session.get_messages_for_llm() == session.get_messages_for_llm(since="")


# --- the wire: the boundary has to be *carried*, not merely supported ---------
#
# This is the half the defect actually lives in. Every test above can pass while
# the fix does nothing at all, because they call the daemon directly: the scheduler
# is the one that knows when the run started, and the daemon can only use what the
# frame delivers. A guard for an "existed but was never handed over" bug therefore
# has to assert the hand-over, at the sender and at the receiver — one layer of a
# three-layer wire is exactly what a green suite would not notice.


class _RecordingWs:
    """The two socket calls `_request_vibe_check` makes, and nothing else."""

    def __init__(self, reply: dict) -> None:
        self.sent: list[dict] = []
        self._reply = reply

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        return json.dumps(self._reply)


def _bare_handler() -> TaskHandler:
    """A handler with only the attributes the sender reaches for.

    `object.__new__` skips `__init__` on purpose: the question is what this method
    puts on the frame, and building a real handler would drag the whole scheduler in.
    """
    handler = object.__new__(TaskHandler)
    handler.name = "emrg-task"
    handler._derived = {}
    handler._logger = logging.getLogger("test.run_evidence_boundary")
    return handler


def test_the_scheduler_carries_the_boundary_on_the_frame() -> None:
    ws = _RecordingWs({"type": "vibe_check_result", "ok": True, "result": {
        "work": THIS_RUN_WORK, "recommend_slowdown": False, "slowdown_reason": "",
    }})
    handler = _bare_handler()

    asyncio.run(handler._request_vibe_check(
        ws, prompt="p", completion_summary="s", cycle_started_at=RUN_TWO_START,
    ))

    assert ws.sent, "the request was never sent"
    frame = ws.sent[0]
    assert frame["type"] == "task_vibe_check"
    assert frame.get("cycle_started_at") == RUN_TWO_START, (
        "the daemon cannot slice at a boundary the scheduler did not send — this is "
        "the defect: the instant existed (`cycle_time`) but never reached the frame"
    )


def test_an_old_caller_still_sends_a_well_formed_frame() -> None:
    """The default is present-but-empty, never absent: the field is part of the frame.

    An absent key and an empty one read the same at the daemon (`msg.get(..., "")`),
    but pinning the empty string is what tells a later refactor that dropping the
    field is a wire change, not a tidy-up.
    """
    ws = _RecordingWs({"type": "vibe_check_result", "ok": True, "result": {
        "work": THIS_RUN_WORK, "recommend_slowdown": False, "slowdown_reason": "",
    }})

    asyncio.run(_bare_handler()._request_vibe_check(
        ws, prompt="p", completion_summary="s",
    ))

    assert ws.sent[0]["cycle_started_at"] == ""


# --- the outcome: what the judge is handed -----------------------------------


class _RecordingLLM:
    """Stands in for the daemon's LLM client and keeps what it was asked about."""

    def __init__(self) -> None:
        self.messages: list[dict] | None = None

    async def chat(self, messages, tools=None):  # noqa: ANN001, ANN201 - the client's shape
        self.messages = messages
        return {"content": json.dumps(
            {"work": THIS_RUN_WORK, "recommend_slowdown": False, "slowdown_reason": ""}
        )}


def _bare_server(llm: _RecordingLLM) -> EmrgServer:
    """An `EmrgServer` with only the attribute `_task_vibe_check` reaches for.

    `object.__new__` skips `__init__` deliberately: this test is about the evidence
    the summariser is given, and standing up a real server would test the server.
    """
    server = object.__new__(EmrgServer)
    server.llm = llm
    return server


def test_the_judge_is_not_shown_the_previous_runs_work(tmp_path) -> None:
    """The defect itself: a `work` string about the neighbour must be unproducible.

    Measured at the seam that matters — the messages the LLM call received — rather
    than at the helper that built them. Driven with `asyncio.run`, the convention
    this suite uses (`pytest-asyncio` is not installed here): a bare
    `@pytest.mark.asyncio` test is collected and then *not run*, which would make
    this file green while asserting nothing.
    """
    _two_run_session(tmp_path)
    llm = _RecordingLLM()
    server = _bare_server(llm)

    asyncio.run(server._task_vibe_check(
        "emrg-task", "emrg-evolution-boundary-test", str(tmp_path),
        prompt="run two prompt", completion_summary="done",
        cycle_started_at=RUN_TWO_START,
    ))

    assert llm.messages is not None
    seen = _contents(llm.messages)
    assert THIS_RUN_WORK in seen
    assert PREVIOUS_RUN_WORK not in seen, (
        "the judge was given the previous run's work — this is the defect"
    )


def test_the_same_session_still_shows_both_runs_without_a_boundary(tmp_path) -> None:
    """The other direction: the filter narrows, it does not empty.

    Without this arm, a `_task_vibe_check` that passed a boundary of *now* — or an
    empty history — would satisfy the test above while summarising nothing.
    """
    _two_run_session(tmp_path)
    llm = _RecordingLLM()
    server = _bare_server(llm)

    asyncio.run(server._task_vibe_check(
        "emrg-task", "emrg-evolution-boundary-test", str(tmp_path),
        prompt="run two prompt", completion_summary="done",
    ))

    assert llm.messages is not None
    seen = _contents(llm.messages)
    assert PREVIOUS_RUN_WORK in seen and THIS_RUN_WORK in seen, (
        "an absent boundary must keep the behaviour this session always had"
    )
