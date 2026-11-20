"""The rolling 24-hour ledger: three segments, one window, and a file that outlives us.

Rant 2026-11-19T09:31:00 replaces "this turn's total" with "the last 24 hours". The
properties pinned here are the ones that make that swap mean anything, each named after
the mutation that must turn it red:

* **the window is 24 hours.** A record 23 hours old counts, one 24 hours and a minute old
  does not, and the edge itself is inclusive. A window narrowed to one hour reddens this
  without touching anything else.
* **three segments, never a total.** Input, cache and output are summed apart, and there
  is no key carrying their sum. Folding them into one figure reddens this.
* **cache is what the provider called a cache hit, not input minus it.** A response that
  reports both 100 prompt tokens and 80 cache hits gives a cache segment of 80; deriving
  it as a difference would say 20.
* **a restart does not start the day over.** A second ledger over the same file answers
  the same numbers, which a counter in memory cannot.

Two shapes are decisions rather than arithmetic, and both are load-bearing for the
client: a field no in-window response reported is **None** (the band prints `—`), and a
response that reported no usage at all is **not a record** (item 6). Zero would be a
claim about spending; None is the absence of one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from emrg.server import usage_ledger as ledger_mod
from emrg.server.usage_ledger import WINDOW_SECONDS, UsageLedger

#: A fixed "now" so every record can be placed relative to the window's edge.
NOW = 1_700_000_000.0


def _ledger(tmp_path: Path, now: float = NOW) -> tuple[UsageLedger, list[float]]:
    """A ledger whose clock is a mutable cell, so a test can move time without sleeping."""
    clock = [now]
    return UsageLedger(tmp_path / "usage-ledger.jsonl", clock=lambda: clock[0]), clock


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_window_is_a_rolling_24_hours_and_its_near_edge_is_inclusive(tmp_path):
    """23h counts, exactly 24h counts, 24h and a minute does not."""
    ledger, clock = _ledger(tmp_path)
    clock[0] = NOW - 23 * 3600
    ledger.record({"prompt_tokens": 1})
    clock[0] = NOW - WINDOW_SECONDS
    ledger.record({"prompt_tokens": 2})
    clock[0] = NOW - WINDOW_SECONDS - 60
    ledger.record({"prompt_tokens": 100})

    window = ledger.window(NOW)
    assert window["input"] == 3
    assert window["window_end"] == NOW
    assert window["window_start"] == NOW - WINDOW_SECONDS


def test_three_segments_are_summed_apart_and_there_is_no_total(tmp_path):
    """Input, cache and output each add up on their own; nothing carries their sum."""
    ledger, _ = _ledger(tmp_path)
    ledger.record({"prompt_tokens": 1000, "cache_hit_tokens": 800, "completion_tokens": 40})
    ledger.record({"prompt_tokens": 500, "cache_hit_tokens": 400, "completion_tokens": 60})

    window = ledger.window()
    assert window["input"] == 1500
    assert window["cache"] == 1200
    assert window["output"] == 100
    assert "total" not in window
    assert not any(key.endswith("_total") for key in window)


def test_cache_is_the_reported_hit_not_the_difference_from_input(tmp_path):
    """A hit of 80 out of 100 prompt tokens is a cache segment of 80, not of 20."""
    ledger, _ = _ledger(tmp_path)
    ledger.record({"prompt_tokens": 100, "cache_hit_tokens": 80, "completion_tokens": 5})
    assert ledger.window()["cache"] == 80


def test_a_field_no_response_reported_is_none_rather_than_zero(tmp_path):
    """An endpoint that never reports cache hits gives None, and an empty ledger gives None."""
    ledger, _ = _ledger(tmp_path)
    empty = ledger.window()
    assert (empty["input"], empty["cache"], empty["output"]) == (None, None, None)

    ledger.record({"prompt_tokens": 10, "completion_tokens": 4})
    window = ledger.window()
    assert window["input"] == 10
    assert window["output"] == 4
    assert window["cache"] is None


def test_a_response_with_no_usage_is_not_recorded(tmp_path):
    """Item 6: no usage is not a zero-token response, it is no entry at all."""
    ledger, _ = _ledger(tmp_path)
    ledger.record(None)
    ledger.record({})
    ledger.record({"finish_reason": "stop"})
    assert not (tmp_path / "usage-ledger.jsonl").exists()
    assert ledger.window()["input"] is None


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"prompt_tokens": "10", "completion_tokens": 4}, (None, 4)),
        ({"prompt_tokens": -5, "completion_tokens": 4}, (None, 4)),
        ({"prompt_tokens": True, "completion_tokens": 4}, (None, 4)),
    ],
)
def test_wrong_types_and_negatives_are_not_measured(tmp_path, usage, expected):
    """A string, a bool and a negative number are all "not measured", never 0."""
    ledger, _ = _ledger(tmp_path)
    ledger.record(usage)
    window = ledger.window()
    assert (window["input"], window["output"]) == expected


def test_a_second_ledger_over_the_same_file_answers_the_same_numbers(tmp_path):
    """The daemon restarting must not start the day over — the window is read from disk."""
    ledger, clock = _ledger(tmp_path)
    ledger.record({"prompt_tokens": 700, "completion_tokens": 30})

    restarted, _ = _ledger(tmp_path, now=NOW + 60)
    assert restarted.window()["input"] == 700
    assert restarted.window()["output"] == 30


def test_a_torn_line_does_not_cost_the_window_its_other_records(tmp_path):
    """One unparsable line is skipped; the records around it still sum."""
    ledger, clock = _ledger(tmp_path)
    ledger.record({"prompt_tokens": 5})
    path = tmp_path / "usage-ledger.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"ts": 1.0, "prompt_to\n')
    ledger.record({"prompt_tokens": 7})
    assert ledger.window()["input"] == 12


def test_stale_records_are_dropped_once_the_file_passes_its_cap(tmp_path, monkeypatch):
    """The file's size is bounded by the window: past the cap, out-of-window rows go."""
    monkeypatch.setattr(ledger_mod, "_MAX_BYTES", 1)
    path = tmp_path / "usage-ledger.jsonl"
    path.write_text(
        json.dumps({"ts": NOW - 3 * WINDOW_SECONDS, "prompt_tokens": 9999}) + "\n",
        encoding="utf-8",
    )
    ledger, _ = _ledger(tmp_path)
    ledger.record({"prompt_tokens": 10})

    kept = _lines(path)
    assert [entry["prompt_tokens"] for entry in kept] == [10]
    assert ledger.window()["input"] == 10


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
def test_a_fifo_at_the_ledger_path_is_neither_written_nor_read(tmp_path: Path) -> None:
    """A planted FIFO costs the record, not the daemon.

    The harm the kind check prevents is a **block**: a FIFO's open waits for a peer and
    raises nothing, so no ``except`` bounds it — and both callers run on the daemon's event
    loop, where that wait is the whole daemon and the only recovery is a restart. This
    test finishing is the assertion that neither open happened; the three Nones say the
    round was dropped rather than invented.
    """
    os.mkfifo(tmp_path / "usage-ledger.jsonl")
    ledger, _clock = _ledger(tmp_path)

    ledger.record({"prompt_tokens": 100, "cache_hit_tokens": 80, "completion_tokens": 5})

    window = ledger.window()
    assert (window["input"], window["cache"], window["output"]) == (None, None, None)


class TestOneSessionsShare:
    """The machine's 24 hours and one session's 24 hours, read side by side (issue #3072).

    The machine's three segments stay exactly what they were, digit for digit: this set
    exists to add a second reading, not to move the first. Each test names the mutation
    that must redden it — a row that forgets its session, a session sum that quietly
    widens to the machine's, or a request naming no session answered with the machine's
    figures under a session's name.
    """

    def test_a_record_carries_the_session_it_belongs_to(self, tmp_path):
        """Mutating `record` to drop the argument reddens this and nothing else."""
        ledger, _clock = _ledger(tmp_path)
        ledger.record({"prompt_tokens": 10, "completion_tokens": 1}, "s-one")
        ledger.record({"prompt_tokens": 20, "completion_tokens": 2}, "s-two")

        lines = _lines(tmp_path / "usage-ledger.jsonl")
        assert [line.get("sid") for line in lines] == ["s-one", "s-two"]
        assert (lines[0]["prompt_tokens"], lines[0]["completion_tokens"]) == (10, 1)

    def test_a_record_with_no_session_named_writes_no_sid_at_all(self, tmp_path):
        """An absent key, not an empty string: a blank id would match no session either
        way, and inventing one puts a claim in the file that nothing measured."""
        ledger, _clock = _ledger(tmp_path)
        ledger.record({"prompt_tokens": 5, "completion_tokens": 1})

        assert "sid" not in _lines(tmp_path / "usage-ledger.jsonl")[0]

    def test_the_session_side_sums_only_that_session_while_the_machine_keeps_both(
        self, tmp_path
    ):
        """Mutating the session sum to read `sums` instead of `mine` reddens this."""
        ledger, _clock = _ledger(tmp_path)
        ledger.record({"prompt_tokens": 10, "cache_hit_tokens": 4, "completion_tokens": 1}, "s-one")
        ledger.record({"prompt_tokens": 20, "cache_hit_tokens": 6, "completion_tokens": 2}, "s-two")

        window = ledger.window(session_id="s-one")
        # the machine's, unchanged: both rounds
        assert (window["input"], window["cache"], window["output"]) == (30, 10, 3)
        # this session's alone: one round, and the other session's spend is not in it
        assert window["session"] == {"input": 10, "cache": 4, "output": 1}

    def test_a_row_written_before_rows_carried_a_session_counts_for_the_machine_only(
        self, tmp_path
    ):
        """Item 4. Mutating the read to infer an owner from the timestamp reddens this."""
        path = tmp_path / "usage-ledger.jsonl"
        path.write_text(
            json.dumps({"ts": NOW - 60, "prompt_tokens": 7, "completion_tokens": 3}) + "\n",
            encoding="utf-8",
        )
        ledger, _clock = _ledger(tmp_path)
        ledger.record({"prompt_tokens": 10, "completion_tokens": 1}, "s-one")

        for named in ("s-one", "s-two"):
            window = ledger.window(session_id=named)
            assert (window["input"], window["output"]) == (17, 4)
            assert window["session"]["input"] == (10 if named == "s-one" else None)

    def test_an_unnamed_window_answers_none_for_the_session_not_the_machine_figure(
        self, tmp_path
    ):
        """Item 5. The fallback this pins shut is the tempting one: reuse `sums`."""
        ledger, _clock = _ledger(tmp_path)
        ledger.record({"prompt_tokens": 10, "completion_tokens": 1}, "s-one")

        window = ledger.window()
        assert (window["input"], window["output"]) == (10, 1)
        assert window["session"] == {"input": None, "cache": None, "output": None}
        assert ledger.window(session_id="")["session"] == {
            "input": None, "cache": None, "output": None
        }

    def test_the_window_edge_is_one_edge_for_both_answers(self, tmp_path):
        """Item 2: the session side is not narrowed to a different window."""
        ledger, clock = _ledger(tmp_path)
        ledger.record({"prompt_tokens": 100, "completion_tokens": 10}, "s-one")
        clock[0] = NOW + WINDOW_SECONDS  # exactly 24 hours later: still inside
        assert ledger.window(session_id="s-one")["session"]["input"] == 100

        clock[0] = NOW + WINDOW_SECONDS + 60  # a minute past it: out of both
        window = ledger.window(session_id="s-one")
        assert window["input"] is None
        assert window["session"]["input"] is None


class TestTheWindowGroupedBySession:
    """Rant 2026-11-20T17:30:50: one window, three rankings, and rows that reconcile.

    A ranking needs each session's three segments and its call count, and the panel prints
    a total beside them. The properties pinned here are the ones that make that total an
    identity rather than a second measurement:

    * **the rows are the window.** Every named group plus the unnamed row sums to the
      machine's three figures; folding the unnamed volume into a session, or dropping it,
      reddens the sum.
    * **an unnamed record is a row nobody can open.** Its session is None — not an empty
      string, not the first session's id — so a client cannot render it as a conversation.
    * **calls count records.** It is the denominator of output per call, so deriving it
      from a segment that may be missing reddens the count.
    """

    def test_the_rows_add_up_to_the_machine_totals(self, tmp_path):
        ledger, _clock = _ledger(tmp_path)
        ledger.record({"prompt_tokens": 100, "cache_hit_tokens": 80, "completion_tokens": 20}, "s-a")
        ledger.record({"prompt_tokens": 50, "completion_tokens": 10}, "s-b")
        ledger.record({"prompt_tokens": 7})

        reading = ledger.reading()

        rows = {row["session"]: row for row in reading["groups"]}
        assert rows["s-a"]["input"] == 100
        assert rows["s-b"]["output"] == 10
        assert rows[None]["input"] == 7, "the record with no session is its own row"
        for segment in ("input", "cache", "output"):
            measured = [row[segment] for row in reading["groups"] if row[segment] is not None]
            assert reading[segment] == (sum(measured) if measured else None), (
                f"the {segment} rows do not add up to the machine figure beside them"
            )

    def test_the_unnamed_volume_is_not_folded_into_any_session(self, tmp_path):
        """Item 5. Mutating the grouping to reuse the last session id reddens this."""
        path = tmp_path / "usage-ledger.jsonl"
        path.write_text(
            json.dumps({"ts": NOW - 60, "prompt_tokens": 7, "completion_tokens": 3}) + "\n",
            encoding="utf-8",
        )
        ledger, _clock = _ledger(tmp_path)
        ledger.record({"prompt_tokens": 10, "completion_tokens": 1}, "s-a")

        rows = {row["session"]: row for row in ledger.reading()["groups"]}

        assert rows["s-a"]["input"] == 10, "the older row was attributed to a session"
        assert rows[None]["input"] == 7
        assert rows[None]["calls"] == 1

    def test_calls_count_the_records_not_a_segment(self, tmp_path):
        """Item 5's denominator: a call with no output still counts as a call."""
        ledger, _clock = _ledger(tmp_path)
        ledger.record({"prompt_tokens": 10, "completion_tokens": 5}, "s-a")
        ledger.record({"prompt_tokens": 20}, "s-a")

        row = next(r for r in ledger.reading()["groups"] if r["session"] == "s-a")

        assert row["calls"] == 2
        assert row["output"] == 5, "a call that measured no output must not lower the sum"

    def test_a_segment_a_session_never_measured_is_none_rather_than_zero(self, tmp_path):
        ledger, _clock = _ledger(tmp_path)
        ledger.record({"completion_tokens": 4}, "s-a")

        row = next(r for r in ledger.reading()["groups"] if r["session"] == "s-a")

        assert (row["input"], row["cache"]) == (None, None)
        assert row["output"] == 4

    def test_the_grouped_read_keeps_the_window_edge(self, tmp_path):
        """Item 10: the ranking and the band show one window, not two."""
        ledger, clock = _ledger(tmp_path)
        ledger.record({"prompt_tokens": 100, "completion_tokens": 10}, "s-a")

        clock[0] = NOW + WINDOW_SECONDS  # exactly the near edge: still inside
        assert any(r["session"] == "s-a" for r in ledger.reading()["groups"])

        clock[0] = NOW + WINDOW_SECONDS + 1  # one second past it
        assert not any(r["session"] == "s-a" for r in ledger.reading()["groups"])
