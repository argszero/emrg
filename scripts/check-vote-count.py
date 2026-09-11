#!/usr/bin/env python3
"""Count a PR's *valid* LGTM votes: the ones that are still about the current head.

The class this exists for
------------------------
The merge rule for this repo is "3 consecutive ✅ LGTMs from different evolution
cycles with no ❌ in between". Reading that off the comment history is misleading,
and every recent cycle has re-derived it by hand and got it wrong at least once:

* **A vote that predates the head push is void.** When a cycle re-bases and pushes,
  every earlier ✅ becomes a statement about a commit that no longer exists. The
  comment history still shows five or six "✅ LGTM" lines, so the PR *looks* ready
  while it has zero counting votes. Measured on 2026-09-11: #1133/#1134/#1136/#1137
  each showed 4-6 LGTMs and each had **0 valid votes** after being unblocked.
* **A ❌ resets the run.** Three ✅, then a "needs fix", then a ✅ is one vote, not
  four.
* **One cycle voting twice is one vote.** Votes are counted per cycle, not per
  comment, or a single cycle could carry a PR to the threshold alone.

So the counting is mechanical, and doing it by hand is exactly the kind of repeated
work that should be a tool. This is the companion to `check-merge-freshness.py`:
that one asks "is the CI verdict still about the tree that would merge?", this one
asks "do we have the votes to merge it at all?".

How a vote is recognised
------------------------
By the **first line** of the review body, because the review `state` cannot be used:
every vote in this repo is posted with `gh pr review --comment`, so GitHub records
`COMMENTED` for both "✅ LGTM" and "❌ needs fix". The state field is useless here,
which is worth knowing before writing something that trusts it.

* first line names ❌ -> a veto
* first line names ✅ (or says LGTM) -> an approval
* anything else -> an ordinary comment, ignored

The cycle id is read from the body (`cyc20260911-091230`); a vote without one is
reported as unattributable rather than counted, since distinctness cannot be shown.

Push time, and the honest bound
-------------------------------
A vote counts only if it was submitted *after the head was pushed*. The push time is
taken from the earliest workflow run created for that exact SHA, because that is
when GitHub received the push event - precisely the moment the earlier votes stopped
being about the current head. When no run exists for the head, this falls back to
the head commit's committer date and **says so in the output**: a commit date can
precede the push, so the fallback is the optimistic direction and must not be
silently trusted. The fallback is also the case where the PR has no CI at all,
which is not mergeable anyway.

Usage
-----
    uv run --no-sync python3 scripts/check-vote-count.py <PR> [<PR> ...]
    uv run --no-sync python3 scripts/check-vote-count.py <PR> --json
    uv run --no-sync python3 scripts/check-vote-count.py <PR> --min-votes 2

Exit codes
----------
    0  every PR has >= --min-votes (default 3) valid votes
    1  at least one PR is short
    2  the check could not be made (gh failed, unparseable response) - fail loud;
       never report a count for a question that was not answered

`gh` and network access to GitHub are required; there is no offline mode.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

REPO = "argszero/emrg"

# The runnable form, as Agent.md documents it. A constant (the same convention as
# check-doc-count.py) so the doc line and the guard that checks it cannot drift
# into agreeing on a string that no longer runs anything.
INVOCATION = "uv run --no-sync python3 scripts/check-vote-count.py"

# `cyc20260911-091230` - the cycle id the vote comments carry.
_CYCLE_RE = re.compile(r"cyc\d{8}-\d{6}")

# A veto wins over an approval on the same line: "✅ but ❌ on the second point"
# is a request for changes, and undercounting the veto is the dangerous direction
# (it would let a PR merge on a review that asked for a fix).
_VETO_MARK = "\u274c"  # ❌
_LGTM_MARK = "\u2705"  # ✅


def _gh_json(args: list[str]) -> object:
    """Run `gh` and parse JSON, failing loud rather than guessing.

    `args` excludes the program name, which is prepended here so no call site can
    omit it (a call site that passed bare gh arguments once ran the POSIX `pr`
    utility instead, whose error names neither gh nor the mistake).
    """
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh failed (rc={proc.returncode}): gh {' '.join(args)}\n{proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def _gh_json_paginated(args: list[str]) -> list:
    """Every page of a list endpoint, as one list.

    Needed because a single request truncates silently: the reviews endpoint
    returns 30 by default and orders **oldest first**, so a PR with more than 30
    reviews would drop its *newest* votes - precisely the ones that count, since
    the whole rule is about votes cast after the head push. #1134 already carries
    7 reviews and #1136 has been through three unblocks; this is a merge gate, so
    the count must not depend on how busy a PR has been.

    `--paginate` with a `.[] | {…}` filter emits one JSON object per line across
    pages; parsed per line rather than as one document, since concatenated page
    arrays are not valid JSON.

    **The filter belongs to the caller.** An earlier version appended
    `--jq ".[]"` here, so a call site that also passed `--jq` gave gh two of them:
    the later flag won, the projection was dropped, and every review came back
    with its raw field names. The tool then read `at` as `None` for every vote, and
    since `"" <= push_time` is true it voided **all** of them - a PR with two valid
    votes reported 0/3. It was invisible in the tests because the fake returns
    dicts directly and never models the jq contract, and invisible in the count
    (which only looked short, a plausible state). Found by running it against the
    live PRs after the change; the caller now owns the filter, and the shape is
    asserted instead of assumed.
    """
    proc = subprocess.run(
        ["gh", *args, "--paginate"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh failed (rc={proc.returncode}): gh {' '.join(args)} --paginate\n"
            f"{proc.stderr.strip()}"
        )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _classify(body: str) -> str:
    """`veto`, `approve` or `comment`, from the mark the body *begins* with.

    Leading mark, not "a mark somewhere in the first line": measured 2026-09-11,
    the first version searched the line and read this approval as a veto -

        ✅ **LGTM — third vote at this head** ... (two prior ✅; no ❌ at this head)

    The body begins with ✅ (it is a vote), and the ❌ is prose *about* the absence
    of a veto. Searching anywhere in the line cannot tell those apart, and the
    failure direction is bad: it silently voids a real vote, so an 11-vote PR looks
    like it has 9. The repo's convention is that the verdict mark is the first
    character, so that is what is read.
    """
    stripped = body.lstrip()
    if stripped.startswith(_VETO_MARK):
        return "veto"
    if stripped.startswith(_LGTM_MARK):
        return "approve"
    # A body that does not open with a mark is an ordinary comment unless its first
    # line claims LGTM; those are the human-written reviews this repo also has.
    first = next((ln for ln in body.splitlines() if ln.strip()), "")
    if "LGTM" in first.upper():
        return "approve"
    return "comment"


@dataclass
class Vote:
    at: str
    kind: str
    cycle: str | None
    valid: bool
    why: str


@dataclass
class Verdict:
    pr: int
    title: str
    head_sha: str
    push_time: str
    push_time_exact: bool
    votes: list[Vote] = field(default_factory=list)
    valid_count: int = 0
    needed: int = 3

    @property
    def short(self) -> bool:
        return self.valid_count < self.needed


def _head_push_time(head: str) -> tuple[str, bool]:
    """Earliest CI run creation time for this SHA, else the commit date.

    Returns `(timestamp, is_exact)`. The run's `createdAt` is the push event time;
    a commit date can precede the push, so the fallback is flagged rather than
    silently used.
    """
    runs = _gh_json(
        [
            "api",
            f"repos/{REPO}/actions/runs?head_sha={head}&per_page=100",
            "--jq",
            '{t: ([.workflow_runs[].created_at] | sort | .[0] // "")}',
        ]
    )
    assert isinstance(runs, dict)
    created = runs.get("t")
    if isinstance(created, str) and created:
        return created, True

    commit = _gh_json(["api", f"repos/{REPO}/commits/{head}", "--jq", "{t: .commit.committer.date}"])
    assert isinstance(commit, dict)
    committer_date = commit.get("t")
    if not isinstance(committer_date, str) or not committer_date:
        raise RuntimeError(f"cannot determine a push time for head {head[:8]}")
    return committer_date, False


def check_pr(number: int, needed: int) -> Verdict:
    view = _gh_json(
        ["pr", "view", str(number), "-R", REPO, "--json", "number,title,headRefOid"]
    )
    assert isinstance(view, dict)
    head = str(view["headRefOid"])

    push_time, exact = _head_push_time(head)

    # Every page, not the first 30: a truncated list drops the newest reviews,
    # which are exactly the votes that count (and the endpoint orders oldest
    # first, so the loss is invisible in the output - it just looks short).
    reviews = _gh_json_paginated(
        [
            "api",
            f"repos/{REPO}/pulls/{number}/reviews",
            "--jq",
            ".[] | {at: .submitted_at, body: .body}",
        ]
    )
    reviews.sort(key=lambda r: str(r.get("at") or ""))

    votes: list[Vote] = []
    for r in reviews:
        assert isinstance(r, dict)
        # The projection must have applied: without it the fields arrive under
        # their raw names and `at` reads as "", which makes `"" <= push_time` true
        # and voids every vote - a full PR reporting 0/3, indistinguishable in the
        # count from a genuinely unvoted one. Cheap to check, and it is the exact
        # failure the caller-owns-the-filter rule above was written for.
        if not r.get("at"):
            raise RuntimeError(
                f"review payload for #{number} has no `at` field (keys: "
                f"{sorted(r)[:5]}) - the --jq projection did not apply, so vote "
                "times are unknown; refusing to report a count"
            )
        body = str(r.get("body") or "")
        at = str(r.get("at"))
        kind = _classify(body)
        if kind == "comment":
            continue
        match = _CYCLE_RE.search(body)
        cycle = match.group(0) if match else None
        if at <= push_time:
            votes.append(
                Vote(at, kind, cycle, False, f"submitted before the head push ({push_time})")
            )
        elif cycle is None:
            votes.append(Vote(at, kind, cycle, False, "no cycle id in the vote body"))
        else:
            votes.append(Vote(at, kind, cycle, True, ""))

    # Walk the votes in order, resetting the run on a veto, and counting each
    # cycle at most once inside the trailing run.
    run = 0
    seen: set[str] = set()
    for v in votes:
        if v.kind == "veto":
            run = 0
            seen.clear()
        elif v.valid and v.cycle is not None and v.cycle not in seen:
            seen.add(v.cycle)
            run += 1
        # invalid approvals and repeat-cycle approvals leave the run untouched

    return Verdict(
        pr=number,
        title=str(view["title"]),
        head_sha=head,
        push_time=push_time,
        push_time_exact=exact,
        votes=votes,
        valid_count=run,
        needed=needed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-vote-count.py",
        description="Count the LGTM votes that are still about a PR's current head.",
    )
    parser.add_argument("prs", nargs="+", type=int, help="pull request number(s)")
    parser.add_argument("--min-votes", type=int, default=3, help="votes required (default 3)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    args = parser.parse_args(argv)

    try:
        verdicts = [check_pr(n, args.min_votes) for n in args.prs]
    except (RuntimeError, KeyError, ValueError, AssertionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "pr": v.pr,
                        "head": v.head_sha,
                        "push_time": v.push_time,
                        "push_time_exact": v.push_time_exact,
                        "valid_votes": v.valid_count,
                        "needed": v.needed,
                        "ready": not v.short,
                    }
                    for v in verdicts
                ],
                indent=2,
            )
        )
    else:
        for v in verdicts:
            mark = "READY" if not v.short else "SHORT"
            src = "" if v.push_time_exact else "  (no CI run: push time approximated by commit date)"
            print(
                f"#{v.pr} {mark} {v.valid_count}/{v.needed} valid votes "
                f"(head {v.head_sha[:8]}, pushed {v.push_time}){src}"
            )
            for vote in v.votes:
                # The mark column reports *counting*, not the vote's kind: an
                # approval that predates the head push is a real ✅ and still does
                # not count, and rendering it "OK ... VOID" says both at once
                # (measured 2026-09-11 on #1133: four lines read "OK - VOID"). The
                # kind is already visible in the reason, so the column is free to
                # answer the only question the reader has.
                if vote.valid:
                    mark, note = "OK  ", "counts"
                elif vote.kind == "veto":
                    mark, note = "NO  ", vote.why
                else:
                    mark, note = "VOID", vote.why
                print(f"    {vote.at} {mark} {vote.cycle or '(no cycle id)'} - {note}")

    if any(v.short for v in verdicts):
        print(
            f"\nNot enough votes yet (need {args.min_votes} consecutive, from different "
            "cycles, none predating the head push). A rebase voids every earlier vote.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
