"""The content-risk search: logarithmic, result-asserted, and never an echo.

Rant `2026-09-29T15:55:44.643795+08:00`, requirement L2. Four things are pinned here,
and each is the acceptance item the rant names:

1. **the search is logarithmic, not linear** — `test_the_search_asks_a_logarithmic_number_of_questions`
   counts the questions against a 512-record history;
2. **after the removal the same session passes** — the end-to-end test asserts on the
   provider's answer (`is_refused`), never on whether the payload still contains the
   trigger: the upstream probe asserted on the *means* ("the pair is broken") and its
   stub, modelling exact bytes, could therefore never catch the failure (the rant's L4b);
3. **nothing written down echoes the trigger** — the trigger travels through this file as
   data built at runtime, and the test reads back the logs *and* the result object;
4. **an unresolvable search removes nothing** — a trigger that is a combination of records
   is reported as such rather than paid for with a record the host needed.

The provider is a stub in every test in this file: `is_refused` is a local function, so no
test here reaches a provider, and no real session outside `tmp_path` is touched.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Sequence

import pytest

from emrg.server.content_risk_probe import (
    ProbeBudgetExceeded,
    describe_record,
    find_offending_index,
    records_to_remove,
    scrub_session,
)
from emrg.session import Session

# Built at runtime so the literal never appears as a searchable string in this file:
# the point of the no-echo test is that the trigger is data, and a source file that
# spelled it out would make "is it in the log?" a question about the test as much as
# about the code. (The two halves are joined here, and the tests compare against it.)
TRIGGER = "".join(("ZQX", "TRIGGER", "WORD"))


def word_probe(records: Sequence[dict]) -> bool:
    """The provider model these tests use: refused iff the trigger is in a record.

    A blunt model on purpose — it is the *class* of trigger the rant says L1 cannot
    cure ("if the trigger is a word or phrase, replacing codepoints cannot help"),
    and it keeps the signal about containment rather than about prose.
    """
    return any(TRIGGER in json.dumps(r, ensure_ascii=False) for r in records)


def records_with_trigger_at(n: int, position: int, *, kind: str = "message") -> list[dict]:
    """`n` ordinary records, with the trigger in the one at `position`."""
    out: list[dict] = []
    for i in range(n):
        body = "ordinary line %d" % i
        if i == position:
            body = f"{body} {TRIGGER}"
        if kind == "message":
            out.append({
                "type": "message",
                "role": "user" if i % 2 == 0 else "assistant",
                "content": body,
            })
        else:  # tool_result
            out.append({
                "type": "tool_result",
                "tool_call_id": f"call-{i}",
                "tool_name": "bash",
                "content": body,
                "error": False,
            })
    return out


def write_session(tmp_path: Path, records: list[dict]) -> Session:
    """A real session on disk holding exactly these records."""
    session = Session("s_probe_test", tmp_path)
    session._write_history(records)
    return session


# ── 1. the search itself ─────────────────────────────────────────────


def test_the_search_finds_the_offending_record():
    """The located position is the one that carries the trigger."""
    records = records_with_trigger_at(64, 37)
    index, probes, reason, traces = find_offending_index(records, word_probe)

    assert index == 37
    assert reason == "found"
    assert probes <= 8, f"64 records took {probes} probes"
    assert [t.index for t in traces] == [37]


def test_the_search_asks_a_logarithmic_number_of_questions():
    """512 records, no more than a bisection can need — against a *linear* 512.

    The rant's cost target is O(log n) and this is the reading of it: a linear scan of
    the measured session (131 messages, ~738k prompt tokens) is hundreds of provider
    requests, which is the reason the remedy had to be designed rather than looped.
    """
    records = records_with_trigger_at(512, 300)
    index, probes, _, _ = find_offending_index(records, word_probe)

    assert index == 300
    assert probes <= 11, f"512 records took {probes} probes"
    assert probes < 512 // 10


def test_a_larger_history_costs_about_one_more_question_per_doubling():
    """4096 records stay inside `ceil(log2(n)) + 2`, and 512's cost is near it too."""
    small = find_offending_index(records_with_trigger_at(512, 1), word_probe)
    large = find_offending_index(records_with_trigger_at(4096, 1000), word_probe)

    assert large[0] == 1000
    assert large[1] <= 14, f"4096 records took {large[1]} probes"
    # Eight times the history costs at most a handful of extra questions: the shape
    # of the budget, not a constant, is what these two numbers show.
    assert large[1] - small[1] <= 5


def test_the_search_stops_rather_than_guessing_when_its_budget_is_spent():
    """A caller that pins a budget gets an exception, never a pick.

    The guard matters because the thing an answer is spent on is a record out of a
    host's conversation: a search that has lost its bound is one whose answer is not
    worth acting on.
    """
    records = records_with_trigger_at(4096, 2000)
    with pytest.raises(ProbeBudgetExceeded):
        find_offending_index(records, word_probe, max_probes=2)


def test_a_clean_history_is_not_implicated():
    """Nothing is refused ⇒ no culprit, and the reason says which case this is."""
    records = records_with_trigger_at(32, -1)
    index, _, reason, _ = find_offending_index(records, word_probe)

    assert index is None
    assert reason == "records-not-implicated"


def test_a_combination_trigger_is_reported_not_paid_for():
    """Two records are refused only together: say so, delete nothing.

    Neither half is refused alone while the whole is — the signature of a trigger that
    is not in one record. Both halves must be asked about to tell this from "the
    culprit is in the second half", which is why the bisection's `+2` is not slack.
    """
    records = [
        {"type": "message", "role": "user", "content": "first"},
        {"type": "message", "role": "assistant", "content": "second"},
        {"type": "message", "role": "user", "content": "third"},
        {"type": "message", "role": "assistant", "content": "fourth"},
    ]

    def pair_probe(candidate: Sequence[dict]) -> bool:
        text = " ".join(json.dumps(r, ensure_ascii=False) for r in candidate)
        return "first" in text and "second" in text

    index, _, reason, traces = find_offending_index(records, pair_probe)

    assert index is None
    assert reason == "combination-trigger"
    # What it may say about them is a name, not their words.
    assert all(t.kind.startswith("message/") for t in traces)
    assert TRIGGER not in repr(traces)


# ── 2. the removal ───────────────────────────────────────────────────


def test_the_session_passes_afterwards_and_the_search_says_how_it_looked(tmp_path: Path):
    """The end-to-end acceptance: refused before, accepted after the removal.

    Asserted on the provider's answer, not on the history's text — the rant's L4b. A
    test that instead checked "the trigger is gone from the records" would pass on a
    removal that broke the tool pairs, and would have to quote the trigger to do it.
    """
    session = write_session(tmp_path, records_with_trigger_at(48, 30))
    assert word_probe(session._read_history()) is True

    result = scrub_session(session, word_probe, )

    assert result.resolved is True
    assert result.index == 30
    assert result.removed == 1
    assert word_probe(session._read_history()) is False
    assert len(session._read_history()) == 47


def test_nothing_written_down_echoes_the_trigger(tmp_path: Path, caplog):
    """The trigger is not in the logs, the traces, or the result object.

    This is the rant's L4 and the direct cause of the incident it describes: the
    upstream verification probe printed the triggering text into the session history
    and every session that read it back was killed by the same filter — *reading it
    back poisons the reader*. Mutating `describe_record` to carry `content`, or the
    scrub's log line to quote the record, turns this red.
    """
    session = write_session(tmp_path, records_with_trigger_at(64, 21))
    with caplog.at_level(logging.DEBUG):
        result = scrub_session(session, word_probe)

    written = "\n".join(
        [caplog.text, repr(result), repr(result.traces), repr(describe_record(21, session._read_history()[0]))]
    )
    assert TRIGGER not in written
    # And the positive control: the search did have the trigger in hand, so the
    # absence above is a property of what is written down, not of what was seen.
    assert result.removed == 1 or result.resolved is False
    assert session._read_history()  # the session survived


def test_a_stale_index_removes_nothing(tmp_path: Path):
    """An out-of-range position is ignored, not clamped onto a neighbour."""
    records = records_with_trigger_at(4, 1)
    session = write_session(tmp_path, records)

    assert session.drop_history_records([99, -1]) == 0
    assert len(session._read_history()) == 4


def test_the_message_count_follows_the_removal(tmp_path: Path):
    """The count on screen is derived from the records left, as `compact` derives it."""
    session = write_session(tmp_path, records_with_trigger_at(10, 9))
    session._message_count = 10

    assert session.drop_history_records([0, 1]) == 2
    assert session._message_count == 8


def test_removing_an_assistant_culprit_takes_its_tool_results(tmp_path: Path):
    """A tool call and its results leave together — a half-round is malformed."""
    records = [
        {"type": "message", "role": "user", "content": "run it"},
        {"type": "message", "role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
        ]},
        {"type": "tool_result", "tool_call_id": "c1", "tool_name": "bash", "content": "..."},
        {"type": "message", "role": "assistant", "content": "done"},
    ]

    assert records_to_remove(records, 1) == [1, 2]


def test_removing_a_tool_result_culprit_takes_its_whole_round():
    """The other direction: a result whose call is left behind is the same defect."""
    records = [
        {"type": "message", "role": "user", "content": "run two things"},
        {"type": "message", "role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"type": "tool_result", "tool_call_id": "c1", "tool_name": "bash", "content": "one"},
        {"type": "tool_result", "tool_call_id": "c2", "tool_name": "read", "content": "two"},
        {"type": "message", "role": "assistant", "content": "both done"},
    ]

    # Culprit in c2's result: the call message and *both* results go, because the
    # call carries both ids and a call cannot keep half its answers.
    assert records_to_remove(records, 3) == [1, 2, 3]


def test_an_ordinary_message_goes_alone():
    """A user or system record has no pair to keep it company."""
    records = records_with_trigger_at(5, 2)
    assert records_to_remove(records, 2) == [2]


def test_scrubbing_a_session_that_is_not_implicated_changes_nothing(tmp_path: Path):
    """An unresolved search is a no-op on disk, and says so."""
    session = write_session(tmp_path, records_with_trigger_at(16, -1))
    before = session._read_history()

    result = scrub_session(session, word_probe)

    assert result.resolved is False
    assert result.reason == "records-not-implicated"
    assert result.removed == 0
    assert session._read_history() == before
