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
reach master"**:

* the base branch's head is **on** master (or is master) -> yes; nothing to do.
  `OK`.
* the base branch is the head of an **open PR** -> it is still in flight. A merge
  into it goes into the live parent branch and rides along when the parent lands,
  so this is not a dead end - but it is not a guarantee either, because the parent
  can still be *closed* unmerged. Reported as `LIVE`, and **not** a failure.
* the base branch no longer exists, or its head is not on master and no open PR
  is named by it -> **no**. This is the squash-merge shape: the parent PR landed
  as a squash, so its branch was never an ancestor of master and merging into it
  lands nothing. `DEAD`.

The live case is what keeps the check from being noise, and it cannot be
recognised from the compare status - a parent that is in flight *necessarily* has
commits master does not, so its head scores `ahead`, or `diverged` as soon as
master moves for any unrelated reason. Both are byte-identical to a squash-merged
branch's status (measured 2026-09-11: all five open PR branches here scored
`ahead` only because master had not advanced since they were cut). The decidable
input is whether an open PR's head *is* that branch - which `_open_prs` already
fetches and this check previously ignored.

Exit codes
----------
0  no open PR's base is a dead end (bases either reach master, or are an open
   PR's head - the latter reported as LIVE)
1  at least one open PR is based on a dead end - retarget it before merging
2  the question could not be answered (gh failed, response unparseable, a
   requested PR number is not among the open PRs, or no repository could be named
   to measure - see "Which repository answered" below)

Which repository answered
-------------------------
Every verdict here is about exactly one repository, and the first line of every report
names it. `master` and the branch list are that repository's, so a reader who cannot see
which one was read cannot tell whose bases were classified.

`--repo` names it. Without `--repo` the repository used to be left to gh's own
`{owner}/{repo}` placeholder, which gh resolves from the remotes of the directory this
runs in - correct on an ordinary clone, and wrong on a checkout whose only GitHub remote
is a fork. A fork holds a **copy** of `master` and the branches it had when it was forked,
so the gate answered about the fork and said nothing about it. Measured 2026-10-29
(`cyc20261029-082021`) on master `7e76668`: the placeholder resolved to `pm25coder/emrg`
with 50 branches against the upstream's 1159, so 1158 upstream bases were reported as "no
longer exists on the remote"; and the upstream master head `7e766683` - the commit this
very file is committed in - read as *not on master*, flipping `_ref_is_on_master` from
True to False. Same commit, same question, two answers, and no line of output naming the
repository that gave either. So the placeholder never reaches gh as a path now: a fork is
refused by name, and any other resolution is reported before the verdicts.

What the count covers
---------------------
Every exit code above is about the PRs this run **looked at**, and those are the numbers
the caller gave (or all open PRs when none were given). The summary line says which:
`N of the M open PR(s)` under the default, `N of the M named PR(s)` when numbers were
passed. Measured 2026-09-26 (`cyc20260926-023125`): asked about a single number, the line
read `1 open PR(s) are based on a branch that cannot reach master` - a count of the
caller's list, in the vocabulary of a fact about the repository. The per-PR lines above it
were always per-PR; only the summary generalised, and it is the line a reader quotes.

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

#: gh's placeholder for "the repository of the directory this runs in". Spelled once,
#: because it is the one token that must never again reach a `repos/...` path: gh resolves
#: it from the cwd's remotes, and on a checkout whose only GitHub remote is a fork that
#: resolution *is* the fork. `_repo_for_the_question` is where it is resolved and refused.
PLACEHOLDER = "{owner}/{repo}"


def _gh(*args: str) -> str:
    """Run `gh`, failing loud rather than guessing.

    A **missing** `gh` is a failure of the same kind as a failing one, and must
    take the same exit path. It is not a hypothetical: on the Windows CI runner
    `gh` is not installed, so the test that drives this tool against a bogus repo
    raised `FileNotFoundError` out of `subprocess.run` - an uncaught exception,
    which CPython reports as **rc 1**, i.e. "a PR is on a dead end". That is the
    exact wrong answer this tool exists to prevent: a caller reading rc 1 would
    go looking for a PR to retarget that does not exist, while the real state is
    "could not look". Caught by CI (run 34605389233), not by review.
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"gh is not available on this machine ({exc}); cannot determine PR bases"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh failed (rc={proc.returncode}): gh {' '.join(args)}\n"
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def _gh_json(*args: str) -> object:
    return json.loads(_gh(*args))


def _open_prs(repo: str) -> list[dict]:
    """Every open PR **in `repo`**, with the fields this check needs.

    `repo` is always a concrete `owner/name` - the caller named it, or
    `_repo_for_the_question` resolved it. The `if repo:` that used to be here was the
    second half of the old defect: omitting `-R` is what made gh answer about whatever
    repository the checkout resolved to, and leaving the omissible branch in place would
    keep that answer one forgotten argument away.
    """
    args = [
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,baseRefName,headRefName,state",
        "-R",
        repo,
    ]
    payload = _gh_json(*args)
    if not isinstance(payload, list):
        raise RuntimeError(f"expected a list of PRs, got {type(payload).__name__}")
    for pr in payload:
        for field in ("number", "baseRefName"):
            if field not in pr:
                raise RuntimeError(f"PR entry missing {field!r}: {pr}")
    return payload


class Unmeasurable(RuntimeError):
    """The question cannot be answered, and refusing is the answer.

    The same exit path as any other `RuntimeError` here (rc 2), and named apart so a
    reader of the refusal can see which of the two it was: this one means the repository
    to measure could not be named, not that gh failed.
    """


def _repo_for_the_question(repo: str | None) -> str:
    """The concrete `owner/name` this run reads - resolved, never a placeholder.

    `--repo` is the caller naming a repository, which is not a guess. Without it, the
    repository used to be gh's placeholder and gh resolved it from the remotes of the
    checkout - on which a fork is the answer, and a fork is the wrong repository for this
    question for two measured reasons: its `master` is a copy that lags, and its branch
    list is a snapshot taken at fork time. Measured 2026-10-29 (`cyc20261029-082021`) on
    master `7e76668`: the placeholder resolved to `pm25coder/emrg` (50 branches against
    the upstream's 1159), and the upstream master head `7e766683` itself read as *not on
    master* - the same commit answering False here and True under `--repo argszero/emrg`.

    So a fork is refused **by name**, together with the repository it forks, and the
    remedy is spelled out; a non-fork resolution is returned so `main` can print it before
    any verdict. The non-fork half is load-bearing: an ordinary clone, whose remote is the
    repository itself, must keep working without new flags.
    """
    if repo:
        return repo
    meta = _gh_json("api", f"repos/{PLACEHOLDER}")
    if not isinstance(meta, dict):
        raise Unmeasurable(
            f"gh answered {type(meta).__name__} for {PLACEHOLDER}, not a repository - "
            f"there is no repository to measure"
        )
    resolved = meta.get("full_name")
    if not resolved:
        raise Unmeasurable(
            f"gh did not name the repository it resolved from {PLACEHOLDER}: {meta!r}"
        )
    parent = (meta.get("parent") or {}).get("full_name")
    if parent:
        raise Unmeasurable(
            f"refusing to measure: with no --repo, gh resolved this checkout to "
            f"{resolved}, which is a fork of {parent}. The base a PR declares and the "
            f"master it has to reach both belong to one repository, and a fork holds a "
            f"copy of master and the branches it had when it was forked, so a verdict "
            f"read here is a verdict about {resolved}, not about {parent}. Name the "
            f"repository to measure: --repo {parent}"
        )
    return resolved


def _branch_heads(repo: str) -> dict[str, str]:
    """branch name -> head sha, for every branch of `repo`."""
    # `_gh` (not a bare subprocess.run) so a missing `gh` raises the same
    # RuntimeError as a failing one and takes the rc-2 path.
    out = _gh("api", f"repos/{repo}/branches", "--paginate")
    # --paginate concatenates page arrays, which is not valid JSON as a whole;
    # the endpoint returns an array per page, so parse per document if it parses,
    # else fall back to line-delimited objects.
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        payload = [json.loads(line) for line in out.splitlines() if line.strip()]
    if not isinstance(payload, list):
        raise RuntimeError("branch listing was not a list")
    heads = {}
    for entry in payload:
        name = entry.get("name")
        sha = (entry.get("commit") or {}).get("sha")
        if name and sha:
            heads[name] = sha
    return heads


def _ref_is_on_master(sha: str, repo: str) -> bool:
    """True when `sha` is an ancestor of (or equal to) `repo`'s master head."""
    data = _gh_json("api", f"repos/{repo}/compare/{MASTER}...{sha}")
    status = data.get("status")
    if status is None:
        raise RuntimeError(f"compare response has no status: {data}")
    # `identical` and `behind` mean the commit is on master's history; `ahead`
    # and `diverged` mean it is not.
    return status in ("identical", "behind")


def classify_base(
    base: str, branches: dict[str, str], repo: str, open_heads: set[str] = frozenset()
) -> tuple[str, str]:
    """`(verdict, why)` for one PR's base branch.

    `open_heads` is the set of branches that are the head of an open PR; a base in
    it is a parent still in flight (`LIVE`), which is not a dead end.
    """
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
    if base in open_heads:
        return (
            "LIVE",
            f"base {base!r} ({sha[:8]}) is the head of an open PR, so this is a "
            "stacked PR on a parent still in flight: a merge into it lands on the "
            "live parent branch and arrives when the parent lands. Not a dead end "
            "- but not a guarantee either, since the parent can still be closed "
            "unmerged. Prefer retargeting to master before merging",
        )
    return (
        "DEAD",
        f"base {base!r} ({sha[:8]}) is not on master and is not the head of any "
        "open PR - it is a dead end. This is the squash-merge shape: if the parent "
        "PR was squash-merged, the branch was never an ancestor of master and "
        "merging into it lands nothing. "
        "Retarget this PR to master before approving it",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report PRs whose base branch cannot reach master."
    )
    parser.add_argument(
        "--repo",
        help=(
            "owner/name to measure (default: the repository gh resolves for this "
            "checkout, refused when that repository is a fork)"
        ),
    )
    parser.add_argument(
        "prs", nargs="*", type=int, help="only these PR numbers (default: all open)"
    )
    args = parser.parse_args(argv)

    # Resolved before anything is measured: a refusal that arrived after a verdict would
    # still be a verdict, and the shape this file used to print was a verdict about an
    # unnamed repository.
    try:
        repo = _repo_for_the_question(args.repo)
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"cannot determine PR bases: {exc}", file=sys.stderr)
        return 2

    # Named before any verdict, on every run: `master` and the branch list are this
    # repository's, so the reader has to be able to see whose they were. The line carries
    # no other word on purpose - the tests below assert what the *verdict* lines say, and
    # this one must not fall inside those assertions.
    print(f"repo: {repo}")

    try:
        prs = _open_prs(repo)
        branches = _branch_heads(repo)
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"cannot determine PR bases: {exc}", file=sys.stderr)
        return 2

    # Branches that are the head of an open PR: a base among these is a parent
    # still in flight (`LIVE`), not a dead end. Computed from **every** open PR,
    # before any `prs` filter - the parent of a stacked PR is usually not one of
    # the PRs being asked about, and is precisely the row we must not misjudge.
    open_heads = {p.get("headRefName") for p in prs} - {None, ""}

    if args.prs:
        wanted = set(args.prs)
        # A requested number that is not among the open PRs used to be dropped in
        # silence: the loop below then had nothing to classify, so the tool printed
        # nothing and returned 0 - a clean bill for a PR it never looked at. That is
        # the one thing its own contract forbids (never guess "OK" when the state
        # could not be read), and it is indistinguishable from a pass at the exit
        # code, which is what every caller reads. The number is answered as
        # unmeasurable instead: this tool lists open PRs, so a closed, merged or
        # mistyped number has no base it could classify.
        missing = sorted(wanted - {p["number"] for p in prs})
        if missing:
            print(
                "cannot determine PR bases: "
                + ", ".join(f"#{n}" for n in missing)
                + " is not among the open PRs (closed, merged, or nonexistent) - "
                "this tool classifies open PRs only, and will not report a clean bill "
                "for one it did not look at",
                file=sys.stderr,
            )
            return 2
        prs = [p for p in prs if p["number"] in wanted]

    dead = 0
    for pr in sorted(prs, key=lambda p: p["number"]):
        try:
            verdict, why = classify_base(
                pr["baseRefName"], branches, repo, open_heads
            )
        except (RuntimeError, json.JSONDecodeError) as exc:
            print(f"#{pr['number']} UNKNOWN base={pr['baseRefName']!r}: {exc}")
            return 2
        mark = f"{verdict:<5}"
        print(f"#{pr['number']} {mark} base={pr['baseRefName']!r} - {why}")
        if verdict == "DEAD":
            dead += 1

    if dead:
        # The count is the **selection's**, and the sentence says which one it is: with
        # no numbers given the selection *is* every open PR, and with numbers given it is
        # those. Measured 2026-09-26 (`cyc20260926-023125`) after the sibling
        # `check-merge-order.py` was corrected for the same shape: asked about one number,
        # this sentence read "1 open PR(s) are based on a branch that cannot reach
        # master" - true of the one PR asked about, and a claim about the repository that
        # nobody measured. `of the N` costs three words and makes the subject explicit.
        where = "named" if args.prs else "open"
        print(
            f"\n{dead} of the {len(prs)} {where} PR(s) are based on a branch that cannot "
            "reach master. "
            f"Retarget with: gh api -X PATCH repos/<owner>/<repo>/pulls/<N> -f base=master"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
