#!/usr/bin/env python3
"""Forecast which open PRs each merge would dirty, before choosing an order.

The class this exists for
------------------------
On 2026-09-11, eleven open PRs were each individually `MERGEABLE/CLEAN` against
master and each independently green in CI. Every one of them was also ahead of
master, so **any** one of them could be merged. Two cycles derived by hand what
that meant:

* a contributor review noted "the first of them to merge leaves the other three
  conflicting on that one line";
* this repo's own cycle then measured the full matrix with a throwaway
  `git merge-tree` loop and got **51 of 55 pairs conflicting**, every single one on
  `Agent.md` alone - one line, the documented Python test count.

The friction is not the fix, which is mechanical
(`check-doc-count.py --resolve-conflict` re-measures the merged tree and never
picks a side). It is that the *cost* was invisible until afterwards: merging one
ready PR made the other ten `CONFLICTING/DIRTY`, which meant no CI (a dirty PR
gets no `pull_request` run), no merge, and - because resolving requires a push -
**every vote on them void**. Three PRs that were one vote from landing went back
to 0/3. That is a real cost paid out of the 3-consecutive-vote rule, and it was
paid unknowingly.

Why measure instead of reason about it
--------------------------------------
The intuitive account of the cascade ("they all touch the count line") is *nearly*
right and unusable as a rule: on the same day, four of the 55 pairs were clean
despite sharing `Agent.md`, because whether two edits to one file conflict depends
on how close they land. Pairwise conflict is a property of the trees, not of the
file lists, so it is asked of `git merge-tree` rather than inferred.

What was measured
-----------------
`git merge-tree --write-tree <a> <b>` is run per pair. Exit 0 means git produced a
merged tree with no conflicts; non-zero means it wrote conflicts, and the paths are
in the output's first block (one `100644 <blob> <stage>\t<path>` line per side per
conflicted path, stages 1/2/3). The paths are read from that block, so the report
names **which** file collides, not merely that something did.

This deliberately does **not** attempt the merge or touch the working tree: the
question is "what would happen", and answering it must not itself dirty the
checkout - a forecast that has to be cleaned up is worse than no forecast.

The base is resolved to a commit **before** any PR head is fetched
------------------------------------------------------------------
Found by running this tool against the live queue on the day it was written
(`cyc20260911-225712`). It fetched master into `FETCH_HEAD` and passed the *name*
`FETCH_HEAD` as the base - but `git fetch refs/pull/<N>/head` rewrites
`FETCH_HEAD` too, so by the time each pair was measured the base had been
repointed at the most recently fetched PR head. The output was entirely
plausible: #1152 reported as the only PR mergeable against "master", everything
else conflicting. It was also nonsense - the base was #1152's own head, which is
why the two "sides" were identical there.

The tell was the count line: the run printed nothing about master, but master
really sat at 1490 while the run behaved as if the base said 1503. So the rule is
that a **mutable ref name must never reach `merge-tree`**: the base is resolved
with `rev-parse` first, and the test
`test_no_mutable_ref_name_reaches_merge_tree` asserts that invariant over the
argv the tool builds, which is what would have caught this.

The order it recommends
-----------------------
A merge costs one resolution per *later* PR it dirties. So the PR that dirties the
fewest others is the cheapest first move, and the shape to look for is a set of PRs
that conflict with everything (usually a shared line everyone edits) plus a few that
conflict with nothing. The tool prints the count per PR and names the collision
sets; the ordering decision stays with the cycle, because "cheapest first" is not
always "most valuable first" - on 2026-09-11 the right first move was the CI-reach
fix (#1149), which conflicted with everything but unblocked CI for every stacked PR
behind it.

Usage
-----
    uv run --no-sync python3 scripts/check-merge-order.py [PR ...]
    uv run --no-sync python3 scripts/check-merge-order.py --json
    uv run --no-sync python3 scripts/check-merge-order.py --base <ref>

With no PR numbers, every open PR is used. Heads are resolved by fetching
`refs/pull/<N>/head` into a temporary ref, so the measurement uses each PR's real
head rather than whatever a local branch of a similar name happens to point at.

Exit codes
----------
    0  measurement made (which is not an endorsement of any order)
    1  at least one PR conflicts with the base - not mergeable as it stands
    2  the measurement could not be made (gh/git failed, unparseable output) -
       fail loud; never report an order for a question that was not answered

`gh` and network access to GitHub are required to list PRs and fetch their heads;
there is no offline mode.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

# `100644 <blob> <stage>\t<path>` - the conflict block merge-tree writes first,
# one line per side per conflicted path. Stage 1/2/3 are base/ours/theirs.
_CONFLICT_LINE = re.compile(r"^[0-7]{6} [0-9a-f]+ [123]\t(.+)$")


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command with the locale independent of the environment."""
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")


def _rev_parse(ref: str) -> str:
    """Resolve a ref to a commit SHA.

    Every ref used in a merge question goes through here first. A *name* is
    mutable in a way a SHA is not: `FETCH_HEAD` is rewritten by any `git fetch`,
    so passing the name to `merge-tree` answers about whatever the fetch happened
    to leave behind. That is not hypothetical - it is the defect this tool shipped
    with, and the reason the invariant is asserted in the tests.
    """
    proc = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"])
    if proc.returncode != 0:
        raise RuntimeError(f"could not resolve {ref!r} to a commit: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _open_pr_numbers(repo: str) -> list[int]:
    """The open PR numbers, ascending - the default subject of the forecast."""
    proc = _run(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--limit",
            "100",
            "--state",
            "open",
            "--json",
            "number",
            "--jq",
            ".[].number",
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {proc.stderr.strip()}")
    numbers = [int(line) for line in proc.stdout.split() if line.strip()]
    if not numbers:
        raise RuntimeError("no open PRs reported - nothing to forecast")
    return sorted(numbers)


def _fetch_head(repo: str, number: int) -> str:
    """Fetch a PR's real head into a temp ref and return the ref name.

    By ref, not by local branch name: a local branch called `pr<N>` may point at a
    stale commit, and the whole value of this tool is that it measures the trees
    that would actually merge.

    The refspec is **forced** (`+`), and must be. A PR head is routinely re-pushed
    to a commit that is not a descendant of the previous one - every conflict
    resolution in this repo pushes a new head over the old - so the second run of
    this tool against a branch whose head moved would otherwise be rejected:

        ! [rejected]  pull/1148/head -> refs/emrg-forecast/pr1148  (non-fast-forward)

    The fetch then exits 1 **and leaves the stale ref in place**, so the failure is
    not merely noisy: the ref the run would have measured is still the *old* head,
    i.e. the tool would answer about a tree that is no longer the PR. Reproduced
    against a real pair of divergent heads (`cyc20260911-235001`); with the `+` the
    same two fetches both succeed and the ref ends at the true head.
    """
    ref = f"refs/emrg-forecast/pr{number}"
    proc = _run(
        ["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"]
    )
    if proc.returncode != 0:
        # Not `--quiet`: it suppresses the rejection diagnostic as well, which is
        # how this surfaced as an undiagnosable "unknown error" with empty stderr.
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"could not fetch PR #{number}: {detail}")
    return ref


def _conflict_paths(a: str, b: str) -> list[str] | None:
    """Paths that conflict when `a` and `b` are merged; None if the run failed.

    An empty list means the merge is clean - distinct from None, which means the
    question was not answered (a bad ref, a git that rejects `--write-tree`).
    """
    proc = _run(["git", "merge-tree", "--write-tree", a, b])
    if proc.returncode == 0:
        return []
    # A conflict exits 1 with the block on stdout; anything else is a failure to
    # measure rather than a conflict to report.
    if proc.returncode != 1:
        return None
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            break  # end of the conflict block; the rest is the message
        match = _CONFLICT_LINE.match(line)
        if match:
            path = match.group(1)
            if path not in paths:
                paths.append(path)
        elif paths:
            break
    return paths or None


def forecast(base: str, numbers: list[int], repo: str) -> dict:
    """Per-PR: does it conflict with the base, and which PRs would it dirty."""
    # Resolve the base first: fetching the PR heads below rewrites FETCH_HEAD, so a
    # name held across them would silently become the last head fetched.
    base_sha = _rev_parse(base)
    heads = {number: _rev_parse(_fetch_head(repo, number)) for number in numbers}
    report: dict = {"base": base_sha, "prs": {}, "base_conflicts": []}
    for number in numbers:
        paths = _conflict_paths(base_sha, heads[number])
        if paths is None:
            raise RuntimeError(
                f"could not classify PR #{number} against {base} - "
                "the merge question was not answered"
            )
        report["prs"][number] = {"paths": paths, "dirtied": []}
        if paths:
            report["base_conflicts"].append(number)
    for i, a in enumerate(numbers):
        for b in numbers[i + 1 :]:
            paths = _conflict_paths(heads[a], heads[b])
            if paths is None:
                raise RuntimeError(
                    f"could not classify #{a} against #{b} - "
                    "the merge question was not answered"
                )
            if paths:
                report["prs"][a]["dirtied"].append({"pr": b, "paths": paths})
                report["prs"][b]["dirtied"].append({"pr": a, "paths": paths})
    return report


def _print_report(report: dict) -> None:
    prs = report["prs"]
    total = len(prs)
    pairs = total * (total - 1) // 2
    conflicting = sum(len(v["dirtied"]) for v in prs.values()) // 2
    print(f"base {report['base']}, {total} open PR(s), {conflicting} of {pairs} pairs conflict")
    if report["base_conflicts"]:
        print(
            "  conflicts with the base already: "
            + ", ".join(f"#{n}" for n in report["base_conflicts"])
        )
    print()
    for number in sorted(prs):
        entry = prs[number]
        if entry["paths"]:
            print(f"  #{number}: CONFLICTS with the base on {', '.join(entry['paths'])}")
            continue
        dirtied = entry["dirtied"]
        if not dirtied:
            print(f"  #{number}: mergeable, and merging it dirties nothing else")
            continue
        counts: dict[str, int] = {}
        for item in dirtied:
            for path in item["paths"]:
                counts[path] = counts.get(path, 0) + 1
        shared = ", ".join(f"{path} ({n})" for path, n in sorted(counts.items()))
        print(
            f"  #{number}: mergeable, but dirties {len(dirtied)} other PR(s) on {shared}"
            f" - {' '.join('#' + str(i['pr']) for i in dirtied)}"
        )
    print()
    print("Merging a PR costs one resolution per later PR it dirties, and each")
    print("resolution push voids that PR's votes. Cheapest-first is not always")
    print("most-valuable-first; choose deliberately.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Forecast which open PRs each merge would dirty, by asking git "
            "merge-tree (never edits the working tree)."
        ),
    )
    parser.add_argument("prs", nargs="*", type=int, help="PR numbers (default: all open)")
    parser.add_argument("--base", default=None, help="base ref (default: origin/master)")
    parser.add_argument("--repo", default="argszero/emrg", help="GitHub owner/repo")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    base = args.base
    if base is None:
        proc = _run(["git", "fetch", "--quiet", "origin", "master"])
        if proc.returncode != 0:
            print(
                f"could not fetch master: {proc.stderr.strip() or 'unknown error'}",
                file=sys.stderr,
            )
            return 2
        base = "FETCH_HEAD"

    try:
        numbers = sorted(args.prs) if args.prs else _open_pr_numbers(args.repo)
        report = forecast(base, numbers, args.repo)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_report(report)
    return 1 if report["base_conflicts"] else 0


if __name__ == "__main__":
    sys.exit(main())
