"""Unit tests for scripts/calibrate_silent_drift_threshold.py — empirical
tuning of _SILENT_DRIFT_THRESHOLD from the sub-threshold bias-shift
distribution (issue #1075, Dev.to 3dn2b reidmarlow).

The script is module-friendly; pure functions are tested here without any
live daemon state. Both positive and negative states are covered per the
evolution verification rules (#455/#461/#464 lessons): the boundary decision
is exercised on both sides (crowding vs quiet), plus the no-data and
no-separation states.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import calibrate_silent_drift_threshold as cal  # type: ignore[import-not-found]


class TestPercentile:
    def test_median_even_count(self) -> None:
        # nearest-rank p50 of 1..4: ceil(0.5*4)=2 → index 1 → 2
        assert cal.percentile([1, 2, 3, 4], 50) == 2.0

    def test_p100_returns_max(self) -> None:
        assert cal.percentile([1, 2, 3, 4], 100) == 4.0

    def test_p1_returns_min(self) -> None:
        assert cal.percentile([1, 2, 3, 4], 1) == 1.0

    def test_single_value_any_percentile(self) -> None:
        assert cal.percentile([0.3], 99) == 0.3

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            cal.percentile([], 50)


class TestBiasAbs:
    def test_positive_signed(self) -> None:
        assert cal.bias_abs({"bias_shift": 0.6}) == 0.6

    def test_negative_signed_folds(self) -> None:
        assert cal.bias_abs({"bias_shift": -0.4}) == 0.4

    def test_zero(self) -> None:
        assert cal.bias_abs({"bias_shift": 0}) == 0.0

    def test_missing_field(self) -> None:
        assert cal.bias_abs({"type": "anchor_loss"}) is None

    def test_non_numeric(self) -> None:
        assert cal.bias_abs({"bias_shift": "NaN"}) is None


class TestSplitEvents:
    def _ev(self, **kw) -> dict:
        return {"type": "anchor_bias_observation", "bias_shift": 0.05, **kw}

    def test_observation_goes_to_noise(self) -> None:
        noise, drift = cal.split_events([self._ev(bias_shift=0.06)])
        assert noise == [0.06] and drift == []

    def test_drift_event_goes_to_drift(self) -> None:
        noise, drift = cal.split_events([
            self._ev(type="anchor_provider_drift", bias_shift=-0.8)
        ])
        assert noise == [] and drift == [0.8]

    def test_unrelated_event_types_skipped(self) -> None:
        noise, drift = cal.split_events([
            {"type": "anchor_loss", "est": 100},
            {"type": "anchor_drift", "delta": 50},
        ])
        assert noise == [] and drift == []

    def test_missing_bias_shift_skipped(self) -> None:
        noise, drift = cal.split_events([
            {"type": "anchor_bias_observation"},  # no bias_shift
            self._ev(),
        ])
        assert noise == [0.05] and drift == []

    def test_drill_events_excluded(self) -> None:
        """Issue #1087: synthetic planted-fire drill events (reserved session
        id) must never enter the calibration distribution — they are
        fabricated switches, not real drift."""
        noise, drift = cal.split_events([
            self._ev(type="anchor_provider_drift", bias_shift=0.9,
                     session=cal.DRILL_SESSION),
            self._ev(bias_shift=0.06),
        ])
        assert noise == [0.06] and drift == []


class TestProviderGroups:
    def _obs(self, prov: str, shift: float) -> dict:
        return {
            "type": "anchor_bias_observation",
            "provider": prov,
            "bias_shift": shift,
        }

    def _drift(self, prov: str, shift: float) -> dict:
        return {
            "type": "anchor_provider_drift",
            "provider": prov,
            "bias_shift": shift,
        }

    def test_separates_providers_and_types(self) -> None:
        groups = cal.provider_groups([
            self._obs("api.openai.com", 0.05),
            self._obs("api.openai.com", 0.07),
            self._obs("localhost", 0.2),
            self._drift("localhost", 0.9),
        ])
        assert groups["api.openai.com"]["noise"] == [0.05, 0.07]
        assert groups["api.openai.com"]["drift"] == []
        assert groups["localhost"]["noise"] == [0.2]
        assert groups["localhost"]["drift"] == [0.9]

    def test_empty_events(self) -> None:
        assert cal.provider_groups([]) == {}

    def test_drill_events_excluded_from_groups(self) -> None:
        """Issue #1087: drill events (reserved session id) are synthetic —
        excluded from per-provider calibration groups too."""
        groups = cal.provider_groups([
            self._drift("api.openai.com", 0.9),
            self._obs("api.openai.com", 0.06),
            {**self._drift("api.openai.com", 1.2),
             "session": cal.DRILL_SESSION},
        ])
        assert groups["api.openai.com"]["drift"] == [0.9]
        assert groups["api.openai.com"]["noise"] == [0.06]

    def test_missing_provider_falls_to_question_mark(self) -> None:
        groups = cal.provider_groups([
            {"type": "anchor_bias_observation", "bias_shift": 0.05},
        ])
        assert groups["?"]["noise"] == [0.05]

    def test_unrelated_and_biasless_events_skipped(self) -> None:
        groups = cal.provider_groups([
            {"type": "anchor_loss", "provider": "x", "est": 100},
            {"type": "anchor_bias_observation", "provider": "x"},  # no bias
            {"type": "anchor_bias_observation", "bias_shift": 0.03},  # no prov
        ])
        assert groups == {"?": {"noise": [0.03], "drift": []}}

    def test_negative_shift_folds_per_provider(self) -> None:
        groups = cal.provider_groups([
            self._obs("p", -0.06),
            self._obs("p", 0.04),
        ])
        assert sorted(groups["p"]["noise"]) == [0.04, 0.06]


class TestLoadEvents:
    def test_valid_and_malformed_mixed(self, tmp_path) -> None:
        f = tmp_path / "usage-anchor.jsonl"
        f.write_text(
            '{"type": "anchor_bias_observation", "bias_shift": 0.05}\n'
            "not-json\n"
            '{"type": "anchor_provider_drift", "bias_shift": 0.8}\n',
            encoding="utf-8",
        )
        events, malformed = cal.load_events(f)
        assert len(events) == 2 and malformed == 1

    def test_blank_lines_ignored(self, tmp_path) -> None:
        f = tmp_path / "usage-anchor.jsonl"
        f.write_text('{"type": "anchor_loss"}\n\n\n', encoding="utf-8")
        events, malformed = cal.load_events(f)
        assert len(events) == 1 and malformed == 0

    def test_missing_file_exits(self, tmp_path) -> None:
        with pytest.raises(SystemExit) as exc:
            cal.load_events(tmp_path / "nope.jsonl")
        assert exc.value.code == 1


class TestRecommendThreshold:
    def test_no_observations_keeps_current(self) -> None:
        stats = cal.recommend_threshold([], [], current=0.25)
        assert stats["reason"] == "no-sub-threshold-observations"
        assert stats["recommended"] == 0.25
        assert stats["n_noise"] == 0

    def test_quiet_noise_keeps_current(self) -> None:
        # per-round wobble a few % — p99*1.5 stays under the current 0.25
        noise = [0.01, 0.02, 0.03, 0.04, 0.05]
        stats = cal.recommend_threshold(noise, [], current=0.25)
        assert stats["reason"] == "noise-well-below-boundary"
        assert stats["recommended"] == 0.25
        assert stats["p99"] == pytest.approx(0.05)
        assert stats["crowding"] is False

    def test_noise_crowding_boundary_raises(self) -> None:
        # p99 = 0.24 is within 10% of the 0.25 boundary → recommend 0.36
        noise = [0.05, 0.1, 0.15, 0.2, 0.22, 0.24]
        stats = cal.recommend_threshold(noise, [], current=0.25)
        assert stats["reason"] == "noise-crowding-boundary"
        assert stats["recommended"] == pytest.approx(0.36)
        assert stats["crowding"] is True

    def test_no_clean_separation_keeps_current(self) -> None:
        # noise tail 0.24*1.5 = 0.36 > smallest drift 0.30*0.75 = 0.225 —
        # a single threshold cannot separate the populations
        noise = [0.05, 0.1, 0.15, 0.2, 0.22, 0.24]
        drift = [0.30]
        stats = cal.recommend_threshold(noise, drift, current=0.25)
        assert stats["reason"] == "no-clean-separation"
        assert stats["recommended"] == 0.25  # keep, don't gamble
        assert stats["min_drift"] == pytest.approx(0.30)

    def test_drift_floor_keeps_quiet_separation(self) -> None:
        # noise tiny (p99=0.12), drift far away at 0.45 → unchanged, quiet
        noise = [0.05, 0.08, 0.1, 0.12]
        drift = [0.45]
        stats = cal.recommend_threshold(noise, drift, current=0.25)
        assert stats["reason"] == "noise-well-below-boundary"
        assert stats["recommended"] == 0.25

    def test_recommendation_never_lowers_below_current(self) -> None:
        # even a pathological quiet sample must not lower the working guard
        stats = cal.recommend_threshold([0.001, 0.002], [], current=0.25)
        assert stats["recommended"] >= 0.25


class TestMain:
    def test_end_to_end_report(self, tmp_path, capsys) -> None:
        f = tmp_path / "usage-anchor.jsonl"
        rows = [
            {"type": "anchor_bias_observation", "bias_shift": 0.02,
             "provider": "api.openai.com"},
            {"type": "anchor_bias_observation", "bias_shift": -0.04,
             "provider": "api.openai.com"},
            {"type": "anchor_bias_observation", "bias_shift": 0.05,
             "provider": "api.openai.com"},
            {"type": "anchor_provider_drift", "bias_shift": 0.8,
             "provider": "localhost"},
        ]
        f.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        rc = cal.main(["--path", str(f)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "anchor_bias_observation: 3" in out
        assert "anchor_provider_drift: 1" in out
        assert "distribution (n=3)" in out
        assert "Recommended _SILENT_DRIFT_THRESHOLD: 0.25" in out
        # per-provider table present with both providers, one without noise
        assert "api.openai.com: noise n=3 drift n=0" in out
        assert "localhost: noise n=0 drift n=1" in out

    def test_no_data_report(self, tmp_path, capsys) -> None:
        f = tmp_path / "usage-anchor.jsonl"
        f.write_text(
            '{"type": "anchor_loss", "est": 100}\n', encoding="utf-8"
        )
        rc = cal.main(["--path", str(f)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No sub-threshold observations accumulated yet" in out
        assert "Current threshold stays 0.25" in out
