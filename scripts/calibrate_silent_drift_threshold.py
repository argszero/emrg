#!/usr/bin/env python3
"""Calibrate _SILENT_DRIFT_THRESHOLD from the empirical bias-shift
distribution accumulated in usage-anchor.jsonl (issue #1075).

Reader comment (reidmarlow, Dev.to 3dn2b) on "A guard that has never fired
and a guard that stopped running look identical on disk": the silent-drift
detector's threshold (emrg/server/daemon.py:4300, default 0.25 = 25%
relative bias-ratio shift) was an a-priori guess. To tune it empirically the
guard must first ACCUMULATE the sub-threshold observations — every anchored
round whose |bias_shift| stays under the threshold now appends an
``anchor_bias_observation`` event (issue #1075, daemon change), while
over-threshold rounds append ``anchor_provider_drift`` events as before.

This script reads that file and reports:

- how many sub-threshold observations and real-drift events exist (and the
  file's time span), so a missing distribution is distinguishable from a
  quiet one — the "stopped running" case;
- the sub-threshold |bias_shift| distribution (mean / p50 / p90 / p95 / p99 /
  max);
- a threshold recommendation:
  * no observations yet  → keep the current threshold (nothing to calibrate);
  * noise tail crowding the boundary (p99 >= 0.9 * threshold) → recommend
    raising to p99 * 1.5 (the guard is set too tight; per-round noise can
    trip it);
  * noise far below the boundary → keep the current threshold (the guard
    demonstrably fires only on real drift);
  * if the recommended value would land above the smallest observed real
    drift → "no clean separation": noise and drift overlap, a single fixed
    threshold cannot separate them (keep the current one, investigate
    providers / per-provider thresholds instead).

The recommendation never lowers the threshold below its current value:
lowering a working guard only risks false alarms, and drift events already
prove the current boundary catches real drift.

Usage:
    python scripts/calibrate_silent_drift_threshold.py [--path ~/.emrg/logs/usage-anchor.jsonl] [--current 0.25]

Exit codes: 0 on success (including "not enough data"), 1 on an unreadable
file.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path.home() / ".emrg" / "logs" / "usage-anchor.jsonl"
DEFAULT_CURRENT = 0.25


def load_events(path: Path) -> tuple[list[dict], int]:
    """Parse a usage-anchor.jsonl file into events.

    Returns (events, malformed_count). A missing or unreadable file raises
    SystemExit(1) with a message — the script must not guess when its input
    is gone (the "guard stopped running" failure mode).
    """
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    events: list[dict] = []
    malformed = 0
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except (ValueError, TypeError):
                malformed += 1
    return events, malformed


def bias_abs(record: dict) -> float | None:
    """Absolute bias_shift of an event, or None when missing/non-numeric.

    The events store a SIGNED ratio (+0.6 = tokenizer counts ~60% more than
    before); calibration cares about magnitude only, so both directions fold
    onto the same axis.
    """
    val = record.get("bias_shift")
    try:
        magnitude = abs(float(val))
    except (TypeError, ValueError):
        return None
    if math.isnan(magnitude) or math.isinf(magnitude):
        return None
    return magnitude


def split_events(events: list[dict]) -> tuple[list[float], list[float]]:
    """Partition events into (noise, drift) by |bias_shift|.

    noise = sub-threshold observations (anchor_bias_observation, the
    calibration sample — censored below the current threshold by
    construction), drift = over-threshold events (anchor_provider_drift, the
    real-drift sightings). Events without a numeric bias_shift are skipped.
    """
    noise: list[float] = []
    drift: list[float] = []
    for ev in events:
        shift = bias_abs(ev)
        if shift is None:
            continue
        ev_type = ev.get("type")
        if ev_type == "anchor_bias_observation":
            noise.append(shift)
        elif ev_type == "anchor_provider_drift":
            drift.append(shift)
    return noise, drift


def provider_groups(events: list[dict]) -> dict[str, dict[str, list[float]]]:
    """Group events by provider, split into per-provider noise/drift lists.

    A global threshold hides provider-specific noise floors: OpenAI's
    tokenizer may sit at a ~1.5x bias with tiny per-round wobble while a
    local endpoint wobbles 20%+ — aggregating them into one distribution can
    produce a "no-clean-separation" verdict even though every provider is
    cleanly separable on its own (the script's own no-clean-separation
    message says "consider per-provider thresholds" — this table is the data
    to do that). Events without a numeric bias_shift are skipped; events
    without a provider field fall under "?".
    """
    groups: dict[str, dict[str, list[float]]] = {}
    for ev in events:
        shift = bias_abs(ev)
        if shift is None:
            continue
        ev_type = ev.get("type")
        if ev_type not in ("anchor_bias_observation", "anchor_provider_drift"):
            continue
        prov = ev.get("provider") or "?"
        group = groups.setdefault(prov, {"noise": [], "drift": []})
        if ev_type == "anchor_bias_observation":
            group["noise"].append(shift)
        else:
            group["drift"].append(shift)
    return groups


def percentile(sorted_vals: list[float], p: float) -> float:
    """Nearest-rank percentile (0 < p <= 100) of an already-sorted list.

    Deterministic and stable for small samples (no interpolation), which
    matters here: the calibration sample is often only tens of points.
    """
    if not sorted_vals:
        raise ValueError("percentile of an empty list")
    rank = max(1, min(len(sorted_vals), math.ceil(p / 100.0 * len(sorted_vals))))
    return sorted_vals[rank - 1]


def recommend_threshold(
    noise: list[float], drift: list[float], current: float = DEFAULT_CURRENT
) -> dict[str, Any]:
    """Empirical threshold recommendation from the censored noise sample.

    noise is the sub-threshold |bias_shift| distribution (censored below
    `current` by construction), drift the observed over-threshold magnitudes.

    Returns a dict with distribution stats, the recommendation and a reason
    key (one of: no-sub-threshold-observations | noise-well-below-boundary |
    noise-crowding-boundary | no-clean-separation).
    """
    noise_sorted = sorted(noise)
    drift_sorted = sorted(drift)
    if not noise_sorted:
        return {
            "n_noise": 0,
            "n_drift": len(drift_sorted),
            "recommended": current,
            "reason": "no-sub-threshold-observations",
        }
    mean = statistics.fmean(noise_sorted)
    p50 = percentile(noise_sorted, 50)
    p90 = percentile(noise_sorted, 90)
    p95 = percentile(noise_sorted, 95)
    p99 = percentile(noise_sorted, 99)
    worst = noise_sorted[-1]
    # The noise tail with 1.5x margin is the smallest defensible boundary —
    # but never below the current value (lowering a working guard only adds
    # false alarms; drift events already prove the boundary catches real
    # drift).
    lower = max(current, p99 * 1.5)
    ceiling = 0.5  # hard cap: >50% shift on one round is a tokenizer change
    if drift_sorted:
        # The boundary must sit below the smallest real drift (with headroom)
        # or the two populations are indistinguishable.
        ceiling = min(ceiling, drift_sorted[0] * 0.75)
    crowding = p99 >= 0.9 * current
    if lower <= ceiling:
        recommended = round(lower, 4)
        reason = "noise-crowding-boundary" if crowding else "noise-well-below-boundary"
    else:
        recommended = current
        reason = "no-clean-separation"
    return {
        "n_noise": len(noise_sorted),
        "n_drift": len(drift_sorted),
        "min_drift": drift_sorted[0] if drift_sorted else None,
        "mean": mean,
        "p50": p50,
        "p90": p90,
        "p95": p95,
        "p99": p99,
        "worst": worst,
        "crowding": crowding,
        "recommended": recommended,
        "reason": reason,
    }


def _fmt(x: float) -> str:
    return f"{x:.4f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Calibrate _SILENT_DRIFT_THRESHOLD from the sub-threshold "
            "bias-shift distribution in usage-anchor.jsonl (issue #1075)."
        )
    )
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH,
                    help="usage-anchor.jsonl to analyze (default: %(default)s)")
    ap.add_argument("--current", type=float, default=DEFAULT_CURRENT,
                    help="current _SILENT_DRIFT_THRESHOLD (default: %(default)s)")
    args = ap.parse_args(argv)

    events, malformed = load_events(args.path)
    noise, drift = split_events(events)
    groups = provider_groups(events)
    stats = recommend_threshold(noise, drift, current=args.current)

    first_ts = next((e.get("timestamp") for e in events if e.get("timestamp")), "?")
    last_ts = next((e.get("timestamp") for e in reversed(events) if e.get("timestamp")), "?")
    by_type: dict[str, int] = {}
    for ev in events:
        by_type[ev.get("type", "?")] = by_type.get(ev.get("type", "?"), 0) + 1

    print(f"usage-anchor events: {len(events)} (malformed: {malformed})")
    print(f"  first: {first_ts}  last: {last_ts}")
    for ev_type in sorted(by_type):
        print(f"  {ev_type}: {by_type[ev_type]}")
    if malformed:
        print(f"  !! {malformed} malformed line(s) skipped")

    reason = stats["reason"]
    if reason == "no-sub-threshold-observations":
        print(
            f"\nNo sub-threshold observations accumulated yet "
            f"(n_noise=0, n_drift={stats['n_drift']}). The detector writes an "
            f"anchor_bias_observation per anchored round — re-run after the "
            f"daemon has seen some usage. Current threshold stays "
            f"{args.current}."
        )
        return 0

    print(
        f"\nsub-threshold |bias_shift| distribution (n={stats['n_noise']}): "
        f"mean={_fmt(stats['mean'])} p50={_fmt(stats['p50'])} "
        f"p90={_fmt(stats['p90'])} p95={_fmt(stats['p95'])} "
        f"p99={_fmt(stats['p99'])} max={_fmt(stats['worst'])}"
    )
    if stats["n_drift"]:
        print(f"real-drift events seen: {stats['n_drift']} (over-threshold sightings)")
    else:
        print("real-drift events seen: 0 (guard has never fired)")

    if groups:
        print("\nper-provider |bias_shift| (noise n, drift n, noise p90/p99):")
        for prov in sorted(groups, key=lambda p: (-len(groups[p]["noise"]), p)):
            group = groups[prov]
            n = sorted(group["noise"])
            n_drift = len(group["drift"])
            if n:
                p90 = percentile(n, 90)
                p99 = percentile(n, 99)
                flag = "  <-- CROWDING (p99 >= 90% of current threshold)" \
                    if p99 >= 0.9 * args.current else ""
                print(
                    f"  {prov}: noise n={len(n)} drift n={n_drift} "
                    f"p90={_fmt(p90)} p99={_fmt(p99)}{flag}"
                )
            else:
                print(
                    f"  {prov}: noise n=0 drift n={n_drift} "
                    f"(no sub-threshold observations)"
                )

    if reason == "noise-crowding-boundary":
        print(
            f"\nNOISE CROWDING THE BOUNDARY: p99 ({_fmt(stats['p99'])}) is within "
            f"10% of the current threshold {args.current} — per-round estimate "
            f"noise is close to tripping the guard. Recommended "
            f"_SILENT_DRIFT_THRESHOLD: {stats['recommended']} "
            f"(= max(current, p99 * 1.5))."
        )
    elif reason == "noise-well-below-boundary":
        print(
            f"\nNoise is well below the boundary (p99={_fmt(stats['p99'])}, "
            f"current={args.current}) — the guard demonstrably fires only on "
            f"real drift. Recommended _SILENT_DRIFT_THRESHOLD: "
            f"{stats['recommended']} (unchanged)."
        )
    elif reason == "no-clean-separation":
        print(
            f"\nNO CLEAN SEPARATION: noise tail p99 ({_fmt(stats['p99'])}) * 1.5 "
            f"exceeds the smallest observed drift "
            f"({_fmt(stats['min_drift']) if stats['min_drift'] is not None else 'n/a'} "
            f"* 0.75 headroom) — a single fixed threshold cannot separate the "
            f"two populations. Keeping current threshold {args.current}; "
            f"consider per-provider thresholds or investigating the noisy "
            f"providers."
        )
    else:  # pragma: no cover - defensive
        print(f"\nRecommendation: {stats['recommended']} (reason={reason})")

    print(
        "\nTo apply: edit _SILENT_DRIFT_THRESHOLD in "
        "emrg/server/daemon.py (~line 4300)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
