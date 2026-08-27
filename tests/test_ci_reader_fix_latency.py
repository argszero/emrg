"""Unit tests for scripts/reader-fix-latency.py — the reader-feedback ->
merged-fix latency metric (issue #1027 secondary suggestion, Dev.to 3dicj:
"the 50-min loop claim becomes a measured number").

The script is module-friendly; pure functions are tested here without gh.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import reader_fix_latency as rfl  # type: ignore[import-not-found]


class TestIsRealIssue:
    def test_real_issue(self) -> None:
        assert rfl.is_real_issue({"number": 1000, "created_at": "2026-08-26T10:21:46Z"})

    def test_pr_row_excluded(self) -> None:
        assert not rfl.is_real_issue(
            {"number": 1002, "pull_request": {"merged_at": None}}
        )


class TestLinkedPrNumbers:
    def test_cross_referenced_merged_pr(self) -> None:
        timeline = [
            {
                "event": "cross-referenced",
                "source": {"issue": {"number": 1003, "pull_request": {"merged_at": "2026-08-26T11:11:05Z"}}},
            }
        ]
        assert rfl.linked_pr_numbers(timeline) == {1003}

    def test_cross_referenced_issue_only_skipped(self) -> None:
        # a cross-reference to a plain issue (no pull_request key) is not a fix PR
        timeline = [{"event": "cross-referenced", "source": {"issue": {"number": 999}}}]
        assert rfl.linked_pr_numbers(timeline) == set()

    def test_unrelated_events_ignored(self) -> None:
        timeline = [
            {"event": "closed", "actor": {"login": "argszero"}},
            {"event": "referenced", "commit_id": "abc123"},
            {"event": "labeled", "label": {"name": "bug"}},
        ]
        assert rfl.linked_pr_numbers(timeline) == set()

    def test_multiple_prs_all_collected(self) -> None:
        timeline = [
            {"event": "cross-referenced", "source": {"issue": {"number": 1001, "pull_request": {}}}},
            {"event": "cross-referenced", "source": {"issue": {"number": 1002, "pull_request": {}}}},
            {"event": "cross-referenced", "source": {"issue": {"number": 1003, "pull_request": {}}}},
        ]
        assert rfl.linked_pr_numbers(timeline) == {1001, 1002, 1003}


class TestLatencyMinutes:
    def test_known_50min_loop_shape(self) -> None:
        # the Dev.to "#1000 = 50 minutes" claim, verified:
        # issue created 10:21:46Z, fix PR #1003 merged 11:11:05Z = 49m19s
        assert rfl.latency_minutes(
            "2026-08-26T10:21:46Z", "2026-08-26T11:11:05Z"
        ) == pytest.approx(49.3166, abs=0.01)

    def test_zero_for_same_instant(self) -> None:
        assert rfl.latency_minutes("2026-08-26T10:00:00Z", "2026-08-26T10:00:00Z") == 0.0

    def test_cross_day(self) -> None:
        assert rfl.latency_minutes(
            "2026-08-25T23:30:00Z", "2026-08-26T00:30:00Z"
        ) == 60.0


class TestMedian:
    def test_odd_count(self) -> None:
        assert rfl.median([10.0, 20.0, 30.0]) == 20.0

    def test_even_count(self) -> None:
        assert rfl.median([10.0, 20.0, 30.0, 40.0]) == 25.0

    def test_empty(self) -> None:
        assert rfl.median([]) == 0.0


class TestAgeDays:
    def test_known_age(self) -> None:
        # exactly 5 days = 432_000 seconds
        now = dt.datetime(2026, 8, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
        assert rfl.age_days("2026-08-23T12:00:00Z", now=now) == pytest.approx(5.0)

    def test_zero_for_now(self) -> None:
        now = dt.datetime(2026, 8, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
        assert rfl.age_days("2026-08-28T12:00:00Z", now=now) == pytest.approx(0.0)

    def test_future_issue_negative(self) -> None:
        now = dt.datetime(2026, 8, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
        assert rfl.age_days("2026-08-29T12:00:00Z", now=now) == pytest.approx(-1.0)


class TestCollectOpenIssueAges:
    def test_filters_pr_rows(self, monkeypatch) -> None:
        def fake_gh(args):
            if "state=open" in args:
                return [
                    {"number": 1056, "created_at": "2026-08-27T22:49:16Z"},
                    {"number": 999, "pull_request": {"merged_at": None}},  # PR, skip
                    {"number": 1000, "created_at": "2026-08-26T10:21:46Z"},
                ]
            return []

        monkeypatch.setattr(rfl, "_gh", fake_gh)
        rows = rfl.collect_open_issue_ages("argszero/emrg", 300)
        nums = [r[0] for r in rows]
        assert nums == [1056, 1000]
        assert all(len(r) == 3 for r in rows)  # (number, age_days, created_at)
