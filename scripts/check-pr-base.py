#!/usr/bin/env python3
"""Report PRs whose base branch is not on the path to master.

The class this exists for
------------------------
A PR whose `base` is another *feature branch* passes every check this repo has:

* CI is green (a stacked PR runs against its base, and #1149 now gives it runs at
  all);
* the vote helper counts its LGTMs normally - nothing in it looks at the base;
* `gh pr merge` succeeds.

And it still lands **nothing on master**. `gh pr merge` merges into the PR's
*base branch*, which for a stacked PR is the parent *branch* - and this repo
squash-merges its PRs, so the parent branch is never an ancestor of master
afterwards. The merge commit goes to the parent branch, master never sees the
change, and the PR is marked MERGED.

Measured 2026-09-11 (`cyc20260911-210746`): #1148 was based on
`feature/conflict-classifier-multiline-count`, the branch of #1147. #1147 was
squash-merged as `25904b6`, so that base branch is not an ancestor of master and
its content on master came from the squash - not from the branch. #1148 carried
the *fix* to the classifier master had just received; had it been approved as it
stood, the fix would have been merged into a dead branch, master's classifier
would have kept answering `KEEP BOTH (concatenate)` on the very blocks the PR
fixes, and the queue would have shown a MERGED PR with three green votes that
changed nothing. The condition is invisible in every dashboard: the PR is
MERGEABLE, CI is green, and the votes count.

Why the base being non-master is not automatically fatal
--------------------------------------------------------
A stacked PR is a legitimate, useful workflow *while the parent is in flight* -
that is the whole point of #1149, which made CI run for non-master bases. The
question is not "is the base master", it is **"can a merge into this base still
reach master"**, and that is decidable:

* the base branch's head is **on** master (or is master) -> yes; the stacked PR is
  fine and will arrive when the parent lands. `OK`.
* the base branch no longer exists, or its head is not on master -> **no**; the PR
  is based on a dead end. `DEAD`.
* the parent PR was already merged -> nothing can carry the child over. `DEAD`.

Distinguishing these is what keeps the check from being noise, since a stacked
PR on a live parent is normal and is the state #1149 exists to serve.

Exit codes
----------
0  every open PR's base can still reach master (or is master)
1  at least one open PR is based on a dead end - retarget it before merging
2  the question could not be answered (gh failed, response unparseable)

Never reports a count it could not obtain: a check that guesses "OK" when it
could not read the state is worse than no check, because the failure it hides is
exactly the silent one.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

MASTER = "master"


def _gh(*args: str) -> str:
    """Run `gh`, failing loud rather than guessing."""
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh failed (rc={proc.returncode}): gh {' '.join(args)}\n"
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def _gh_json(*args: str) -> object:
    return json.loads(_gh(*args))


def _open_prs(repo: str | None) -> list[dict]:
    """Every open PR with the fields this check needs."""
    args = [
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,baseRefName,headRefName,state",
    ]
    if repo:
        args += ["-R", repo]
    payload = _gh_json(*args)
    if not isinstance(payload, list):
        raise RuntimeError(f"expected a list of PRs, got {type(payload).__name__}")
    for pr in payload:
        for field in ("number", "baseRefName"):
            if field not in pr:
                raise RuntimeError(f"PR entry missing {field!r}: {pr}")
    return payload


def _repo_path(repo: str | None) -> str:
    """`owner/name` - the caller's, or gh's placeholder for the current repo."""
    return repo if repo else "{owner}/{repo}"


def _branch_heads(repo: str | None) -> dict[str, str]:
    """branch name -> head sha, for every branch on the remote."""
    proc = subprocess.run(
        ["gh", "api", f"repos/{_repo_path(repo)}/branches", "--paginate"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh failed (rc={proc.returncode}): listing branches\n"
            f"{proc.stderr.strip()}"
        )
    # --paginate concatenates page arrays, which is not valid JSON as a whole;
    # the endpoint returns an array per page, so parse per document if it parses,
    # else fall back to line-delimited objects.
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    if not isinstance(payload, list):
        raise RuntimeError("branch listing was not a list")
    heads = {}
    for entry in payload:
        name = entry.get("name")
        sha = (entry.get("commit") or {}).get("sha")
        if name and sha:
            heads[name] = sha
    return heads


def _ref_is_on_master(sha: str, repo: str | None) -> bool:
    """True when `sha` is an ancestor of (or equal to) master's head."""
    data = _gh_json("api", f"repos/{_repo_path(repo)}/compare/{MASTER}...{sha}")
    status = data.get("status")
    if status is None:
        raise RuntimeError(f"compare response has no status: {data}")
    # `identical` and `behind` mean the commit is on master's history; `ahead`
    # and `diverged` mean it is not.
    return status in ("identical", "behind")


def classify_base(base: str, branches: dict[str, str], repo: str | None) -> tuple[str, str]:
    """`(verdict, why)` for one PR's base branch."""
    if base == MASTER:
        return ("OK", "base is master")
    if base not in branches:
        return (
            "DEAD",
            f"base branch {base!r} no longer exists on the remote, so a merge "
            "would have nowhere to land and could never reach master",
        )
    sha = branches[base]
    if _ref_is_on_master(sha, repo):
        return ("OK", f"base {base!r} ({sha[:8]}) is already on master")
    return (
        "DEAD",
        f"base {base!r} ({sha[:8]}) is not on master - it is a dead end. This is "
        "the squash-merge shape: if the parent PR was squash-merged, the branch "
        "was never an ancestor of master and merging into it lands nothing. "
        "Retarget this PR to master before approving it",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report PRs whose base branch cannot reach master."
    )
    parser.add_argument("--repo", help="owner/name (default: the current repo)")
    parser.add_argument(
        "prs", nargs="*", type=int, help="only these PR numbers (default: all open)"
    )
    args = parser.parse_args(argv)

    try:
        prs = _open_prs(args.repo)
        branches = _branch_heads(args.repo)
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"cannot determine PR bases: {exc}", file=sys.stderr)
        return 2

    if args.prs:
        wanted = set(args.prs)
        prs = [p for p in prs if p["number"] in wanted]

    dead = 0
    for pr in sorted(prs, key=lambda p: p["number"]):
        try:
            verdict, why = classify_base(pr["baseRefName"], branches, args.repo)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print(f"#{pr['number']} UNKNOWN base={pr['baseRefName']!r}: {exc}")
            return 2
        mark = "OK   " if verdict == "OK" else "DEAD "
        print(f"#{pr['number']} {mark} base={pr['baseRefName']!r} - {why}")
        if verdict == "DEAD":
            dead += 1

    if dead:
        print(
            f"\n{dead} open PR(s) are based on a branch that cannot reach master. "
            f"Retarget with: gh api -X PATCH repos/<owner>/<repo>/pulls/<N> -f base=master"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
