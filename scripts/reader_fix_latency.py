#!/usr/bin/env python3
"""Measure the reader-feedback -> merged-fix loop: median issue-to-fix latency.

Issue #1027 secondary suggestion (reader comment heinrichneb, Dev.to 3dicj):
"add a README metric — median time from reader-found boundary to merged fix
(the 50-min loop claim becomes a measured number)."

The Dev.to article claimed a ~50-minute issue-to-merged-fix loop (#1000: issue
created 2026-08-26T10:21Z, fix PR #1003 merged 2026-08-26T11:11Z = 49m19s).
This script makes that measurable instead of anecdotal.

Definition: for every CLOSED *issue* (real issue, not a PR) in the repo that
has at least one cross-referenced MERGED pull request (the standard "Fixes #N"
auto-close link), the latency is:

    issue.created_at  ->  merged_at of the EARLIEST merged linked PR

The median across all such issues is the headline number. Only the earliest
merged PR counts — a fix is "shipped" when its first merged PR lands.

The script also reports the OPEN side of the loop (issue #1056, Dev.to
comment 3djoa, heinrichneb 2026-08-27): an open-issue age counter — how
many open issues exist, their median age in days, and the oldest few. A
median only over already-fixed issues hides issues that are still waiting;
the two numbers together bound the loop from both ends.

The median line prints the sample size ``n`` so a median from 2 samples is
not mistaken for one from 200.

Usage:
    python scripts/reader_fix_latency.py [--repo owner/name] [--limit N]

Requires `gh` authenticated. Talks only to api.github.com (works even when
git-over-https to github.com:443 is down, the documented EMRG network shape).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import subprocess
import sys
from typing import Any


def is_real_issue(row: dict) -> bool:
    """A closed-issue API row is a real issue iff it has no pull_request key."""
    return "pull_request" not in row


def linked_pr_numbers(timeline: list[dict]) -> set[int]:
    """PR numbers referenced from an issue via cross-referenced events."""
    nums: set[int] = set()
    for ev in timeline:
        if ev.get("event") != "cross-referenced":
            continue
        src = ev.get("source") or {}
        issue = src.get("issue") or {}
        if "pull_request" not in issue:
            continue
        num = issue.get("number")
        if num is not None:
            nums.add(int(num))
    return nums


def latency_minutes(issue_created: str, pr_merged: str) -> float:
    """Minutes between issue creation and PR merge (ISO-8601 strings)."""
    def _parse(s: str) -> dt.datetime:
        # gh API emits e.g. 2026-08-26T10:21:46Z
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))

    return (_parse(pr_merged) - _parse(issue_created)).total_seconds() / 60.0


def age_days(created_at: str, now: dt.datetime | None = None) -> float:
    """Days between issue creation and now (UTC, fractional).

    ``now`` is injectable for tests; defaults to the current UTC time.
    """
    def _parse(s: str) -> dt.datetime:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))

    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    return (now - _parse(created_at)).total_seconds() / 86400.0


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _gh(args: str | list[str]) -> Any:
    """Run a gh api subprocess and parse the JSON payload."""
    args_list = [args] if isinstance(args, str) else args
    out = subprocess.run(
        ["gh", "api", *args_list],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def _paginate_closed_issues(repo: str, limit: int) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while len(rows) < limit:
        batch = _gh(
            f"repos/{repo}/issues?state=closed&per_page=100&page={page}"
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return rows[:limit]


def _paginate_open_issues(repo: str, limit: int) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while len(rows) < limit:
        batch = _gh(
            f"repos/{repo}/issues?state=open&per_page=100&page={page}"
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return rows[:limit]


def collect_latencies(repo: str, limit: int) -> list[tuple[int, float, int, str]]:
    """Return [(issue_number, latency_minutes, fix_pr_number, merged_at), ...]."""
    out: list[tuple[int, float, int, str]] = []
    for row in _paginate_closed_issues(repo, limit):
        if not is_real_issue(row):
            continue
        num = row["number"]
        timeline = _gh(f"repos/{repo}/issues/{num}/timeline")
        candidates = linked_pr_numbers(timeline)
        if not candidates:
            continue
        merged_at: str | None = None
        fix_pr: int | None = None
        for pr_num in sorted(candidates):
            pr = _gh(f"repos/{repo}/pulls/{pr_num}")
            if pr.get("merged_at"):
                merged_at = pr["merged_at"]
                fix_pr = pr_num
                break
        if merged_at is None or fix_pr is None:
            continue
        out.append((num, latency_minutes(row["created_at"], merged_at), fix_pr, merged_at))
    return out


def collect_open_issue_ages(repo: str, limit: int) -> list[tuple[int, float, str]]:
    """Return [(issue_number, age_days, created_at), ...] for open real issues."""
    out: list[tuple[int, float, str]] = []
    for row in _paginate_open_issues(repo, limit):
        if not is_real_issue(row):
            continue
        created = row["created_at"]
        out.append((row["number"], age_days(created), created))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="argszero/emrg")
    parser.add_argument("--limit", type=int, default=300)
    args = parser.parse_args(argv)

    samples = collect_latencies(args.repo, args.limit)
    if not samples:
        print("no closed issues with a merged fix PR found — nothing to measure")
        return 0

    vals = [s[1] for s in samples]
    med = median(vals)
    h, m = divmod(int(round(med)), 60)
    print(
        f"reader-fix latency ({args.repo}, {len(samples)} closed issue(s) "
        f"with a merged fix PR, scanned {args.limit} closed rows):"
    )
    print(f"median: {int(med)} min ({h}h{m:02d}m, n={len(samples)})")
    worst = sorted(samples, key=lambda s: -s[1])[:3]
    fastest = sorted(samples, key=lambda s: s[1])[:3]
    print("slowest:")
    for num, mins, pr, merged in worst:
        print(f"  #{num}: {int(round(mins))} min (fix PR #{pr} merged {merged})")
    print("fastest:")
    for num, mins, pr, merged in fastest:
        print(f"  #{num}: {int(round(mins))} min (fix PR #{pr} merged {merged})")

    # Open-issue age counter (issue #1056): the unfixed side of the loop.
    open_rows = collect_open_issue_ages(args.repo, args.limit)
    if not open_rows:
        print("open-issue age: no open issues found")
        return 0
    ages = [a[1] for a in open_rows]
    med_age = median(ages)
    print(
        f"open-issue age ({args.repo}, {len(open_rows)} open issue(s), "
        f"scanned {args.limit} open rows):"
    )
    print(f"median age: {med_age:.1f} days (n={len(open_rows)})")
    oldest = sorted(open_rows, key=lambda s: -s[1])[:5]
    print("oldest open issues:")
    for num, days, created in oldest:
        print(f"  #{num}: {days:.1f} days old (created {created[:10]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
