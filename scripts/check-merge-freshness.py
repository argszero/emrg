#!/usr/bin/env python3
"""Check that a pull request's green CI still describes the tree that would merge.

The class this exists for
------------------------
On 2026-09-11 PR #1137 was `MERGEABLE/CLEAN` with both CI jobs green. The merge
was nevertheless unsafe, and the way it was unsafe is reproducible:

    merge base cb651a4 : Agent.md count line 1393   own collection 1393
    ours    ea0a06a    : Agent.md count line 1397   own collection 1397  (+4 tests)
    master  64bab52    : Agent.md count line 1397   own collection 1397  (+4 other tests)

Both sides set the count line to the same number, so git merged it without a
conflict, kept 1397, and the merged tree collected 1401. Two guards
(`test_doc_counts.py::test_python_count_matches_docs` and
`test_check_doc_count.py::test_real_tree_is_consistent`) went red - *after* the
merge, on master, where nobody was looking.

The CI verdict was not wrong. It was about a different tree. On `pull_request`
GitHub builds `Merge <head> into <merge-base>` - the head merged onto the branch
point, **not** onto current master. While the branch point is master's tip those
are the same tree; once master moves they are not, and nothing re-runs the check:
the `synchronize` event fires on a branch push, and master moving is not a branch
push.

The question, made structural
-----------------------------
"Is this verdict current?" reduces exactly to: **is master's tip an ancestor of
the head?** If it is, the merge base *is* master's tip, so the tree CI built and
the tree that would merge are the same commit and the verdict transfers. If it is
not, CI judged a merge onto an older master and the verdict is about a tree that
can no longer be merged.

That is a graph property, so this tool asks the graph instead of comparing
timestamps. Deliberately so: two timestamps are a proxy that can be wrong (clock
skew, a run created a second before the merge commit), whereas ancestry is the
thing itself. It also gets the "master has not moved" case right for free - the
verdict is simply fresh, and no re-run is needed.

Ancestry alone is only half the question, so a second condition is checked
----------------------------------------------------------------------------
Ancestry answers "would a verdict transfer". It does not answer "is there a
verdict". A head can contain master's tip and still have **no CI run at all** - a
dropped push event leaves the branch with zero checks, which reads as `no checks
reported` and is not evidence of anything. So a head only counts as FRESH if it
is an ancestor-descendant of master **and** a run exists for that exact SHA.

Keyed on the SHA, not the branch: a branch pushed twice has two runs, and reading
the older one as the current verdict is the same class of mistake in miniature.
The query asks GitHub for that SHA's runs directly, so there is no window to fall
out of either. It is also pinned to the workflow whose verdict is being claimed -
`test.yml` - because "some passing run" is only the test verdict while nothing
else happens to run on a PR head.

The run is also required to have *passed* - a failing or cancelled run is not a
stale verdict, it is a verdict the committer has to deal with on its own terms,
and this tool says so rather than calling it fresh.

Why the obvious shortcut is wrong
---------------------------------
`gh pr view --json mergeable` returns `CLEAN` here and is actively misleading:
GitHub computes mergeability as "does this textually merge", which is exactly the
property that failed. A cleanly auto-merged line is the *dangerous* case - when
the count line conflicts, a human is forced to look at it.

Usage
-----
    uv run --no-sync python3 scripts/check-merge-freshness.py <PR> [<PR> ...]
    uv run --no-sync python3 scripts/check-merge-freshness.py <PR> --json

Exit codes
----------
    0  every head contains master's tip - CI's merge base is master itself
    1  at least one head does NOT contain master's tip - the verdict is stale
    2  the check could not be made (bad PR, gh failed, unreadable response) -
       fail loud; never report "fresh" for a question that was not answered

`gh` is required, and so is network access to GitHub. There is no offline mode:
the whole question is about a remote verdict, and a local guess would be the
failure mode this tool exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass

REPO = "argszero/emrg"

# GitHub `compare` statuses, split by the one property that decides freshness:
# is master's tip an ancestor of the head?
#
#   identical / ahead  -> master is an ancestor -> merge base IS master -> fresh
#   behind / diverged  -> master is not an ancestor -> stale
#
# Both sets are named rather than expressed as `status == "ahead"`, so an
# unrecognised status (a new GitHub value) falls through to the fail-loud branch
# instead of being silently treated as fresh.
_FRESH_STATUSES = frozenset({"identical", "ahead"})
_STALE_STATUSES = frozenset({"behind", "diverged"})

# A run that has not concluded yet has judged nothing, so it is not a verdict to
# expire - it is a verdict still being formed. Reported as such, never as fresh.
_UNFINISHED = frozenset({"", "pending", "queued", "in_progress", "requested", "waiting"})

# The workflow whose verdict this tool speaks about, by the name GitHub reports.
# `test.yml` ("Test") is the only workflow triggered by `pull_request` in this
# repo, so it is the one whose greenness a merge rests on. Pinned rather than
# "any passing run": the claim "the verdict is about the tests" was otherwise
# carried by coincidence (today nothing else runs on a PR head), and the first
# workflow added on a branch would silently become the verdict instead. A PR head
# that has runs *but none from this workflow* is reported distinctly, so a rename
# here reads as "the verdict workflow did not run", not as "the branch has no CI".
_VERDICT_WORKFLOW = "Test"


def _gh_json(args: list[str]) -> object:
    """Run `gh` and parse JSON, failing loud rather than guessing.

    `args` are gh's arguments *without* the program name; it is prepended here so
    every call site cannot forget it. Measured 2026-09-11: a call site that passed
    `["pr", "view", ...]` to a helper that also omitted the program name ran the
    POSIX `pr` utility instead - which took `view` and the PR number as filenames
    and failed with `pr: cannot open view`, a message that names neither gh nor
    the real mistake.
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


@dataclass
class Verdict:
    pr: int
    title: str
    head_sha: str
    merge_base: str
    ahead_by: int
    behind_by: int
    run_created_at: str | None
    run_conclusion: str | None
    stale: bool
    reason: str


def _latest_run_for_head(head: str) -> dict | None:
    """The newest `_VERDICT_WORKFLOW` run for this exact commit, or None.

    Asked by `head_sha`, not by branch + a window: the run set wanted is directly
    addressable, and the branch form has two failure modes with one cause. It
    carried `--limit 30`, so a branch pushed more than 30 times would report
    "no CI run" for a head that has one - fail-loud, but with the wrong reason
    (measured 2026-09-11 by pm25coder on #1138, who also confirmed the SHA form
    returns the identical answer). It also assumed the run is reachable under the
    head *branch* name, which a fork PR or a renamed branch breaks.

    Filtering to `_VERDICT_WORKFLOW` is the second half of the same point: a
    passing run from *any* workflow is not a test verdict.
    """
    payload = _gh_json(
        [
            "api",
            f"repos/{REPO}/actions/runs?head_sha={head}&per_page=100",
            "--jq",
            "{runs: [.workflow_runs[] | {headSha: .head_sha, name, "
            "createdAt: .created_at, conclusion}]}",
        ]
    )
    assert isinstance(payload, dict)
    runs_raw = payload.get("runs")
    assert isinstance(runs_raw, list)
    matching = [
        r
        for r in runs_raw
        if isinstance(r, dict)
        and r.get("headSha") == head
        and r.get("name") == _VERDICT_WORKFLOW
    ]
    if not matching:
        return None
    return max(matching, key=lambda r: str(r.get("createdAt") or ""))


def check_pr(number: int) -> Verdict:
    view = _gh_json(
        [
            "pr",
            "view",
            str(number),
            "-R",
            REPO,
            "--json",
            "number,title,headRefOid",
        ]
    )
    assert isinstance(view, dict)
    head_sha = str(view["headRefOid"])

    cmp_raw = _gh_json(
        [
            "api",
            f"repos/{REPO}/compare/master...{head_sha}",
            "--jq",
            "{status, ahead_by, behind_by, merge_base: .merge_base_commit.sha}",
        ]
    )
    assert isinstance(cmp_raw, dict)
    status = str(cmp_raw["status"])
    ahead_by = int(cmp_raw["ahead_by"])
    behind_by = int(cmp_raw["behind_by"])
    merge_base = str(cmp_raw["merge_base"])

    run = _latest_run_for_head(head_sha)
    created = str(run.get("createdAt") or "") if run else None
    conclusion = str(run.get("conclusion") or "") if run else None

    common = dict(
        pr=number,
        title=str(view["title"]),
        head_sha=head_sha,
        merge_base=merge_base,
        ahead_by=ahead_by,
        behind_by=behind_by,
        run_created_at=created,
        run_conclusion=conclusion,
    )

    if status not in _FRESH_STATUSES and status not in _STALE_STATUSES:
        raise RuntimeError(
            f"unrecognised compare status {status!r} for #{number}; refusing to call it fresh"
        )

    if status in _STALE_STATUSES:
        return Verdict(
            **common,
            stale=True,
            reason=(
                f"head does not contain master (status={status}, behind_by={behind_by}) "
                f"- CI's merge base was {merge_base[:8]}, so the verdict is about a tree "
                "that can no longer be merged"
            ),
        )

    # Master is an ancestor. That answers "would a verdict transfer"; now answer
    # "is there one".
    if run is None:
        return Verdict(
            **common,
            stale=True,
            reason=(
                f"master is an ancestor (status={status}) but there is NO {_VERDICT_WORKFLOW} "
                f"run for head {head_sha[:8]} - an unjudged head, which `gh pr checks` reports "
                "as 'no checks reported'"
            ),
        )
    if conclusion in _UNFINISHED:
        return Verdict(
            **common,
            stale=True,
            reason=f"CI is still {conclusion or 'pending'} on head {head_sha[:8]} - no verdict yet",
        )
    if conclusion != "success":
        return Verdict(
            **common,
            stale=True,
            reason=(
                f"CI concluded {conclusion!r} on head {head_sha[:8]} - a failing verdict, "
                "not a stale one; re-running will not make it fresh"
            ),
        )
    return Verdict(
        **common,
        stale=False,
        reason=(
            f"master is an ancestor (status={status}, behind_by={behind_by}) and head "
            f"{head_sha[:8]} has a passing run - merge base {merge_base[:8]} IS master's tip"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-merge-freshness.py",
        description="Is each PR's green CI still about the tree that would merge?",
    )
    parser.add_argument("prs", nargs="+", type=int, help="pull request number(s)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    args = parser.parse_args(argv)

    try:
        verdicts = [check_pr(n) for n in args.prs]
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
                        "merge_base": v.merge_base,
                        "ahead_by": v.ahead_by,
                        "behind_by": v.behind_by,
                        "stale": v.stale,
                        "reason": v.reason,
                    }
                    for v in verdicts
                ],
                indent=2,
            )
        )
    else:
        for v in verdicts:
            mark = "STALE" if v.stale else "FRESH"
            print(f"#{v.pr} {mark} (head {v.head_sha[:8]}, base {v.merge_base[:8]}) - {v.reason}")

    if any(v.stale for v in verdicts):
        print(
            "\nRe-merge master into each stale branch, then let CI run. On `pull_request` "
            "GitHub builds Merge<head> into <merge-base>; merging current master in is what "
            "moves the merge base to master, so CI then judges the real merged tree.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
