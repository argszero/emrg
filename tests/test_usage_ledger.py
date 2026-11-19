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
