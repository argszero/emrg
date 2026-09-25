#!/usr/bin/env python3
"""Check that a merge landed the tree its own votes were about.

The class this exists for
-------------------------
Every gate in the merge family asks its question **before** the merge:

* `check-vote-count.py`      - do enough votes still apply to this head?
* `check-merge-freshness.py` - is the green verdict about the tree that merges?
* `check-merge-order.py`     - which other PRs would a merge here dirty?
* `check-merge-tree-health.py` / `check-merge-plan-suite.py`
                             - does the tree a merge (or a plan) would produce pass?

and one of them is asked **at the moment a vote is spent**: `cast-vote.py` refuses a
body whose tree claim is not the tree this merge would land. So the claim is checked,
then the merge happens — and nothing reads the two against each other afterwards. The
gap is not academic, because the landing tree is a function of **(base, head)**, and
the base moves: a parallel cycle merging another PR between the vote and this merge
makes the landed tree a *union* nobody measured, while every signal stayed green —
each PR's CI is about its own branch, and the votes were about a tree that never
landed.

The same shape as `check-merge-tree-health.py`'s origin story (#1133 + #1140: two
individually consistent branches, one inconsistent union, merged cleanly because git
had nothing to conflict about), reached from the other end: there the union was never
measured *before*, here it was measured and then not the one that landed.

What it answers, and from what
------------------------------
Per PR, two readings and no third: the **landed tree** (the merge commit's tree, not
the head's — a head whose tree differs from what landed is exactly the case this is
for) and the **trees this PR's review bodies name**. The reading of "which tree does
this body claim" is `cast-vote.py`'s `named_trees`, reused rather than re-derived:
it is what the pre-merge check uses, so the two ends of one claim cannot drift into
two answers. The review list is `check-vote-count.py`'s paginated read, for the same
reason — a single request returns 30 reviews, and the newest are the ones that count.

Named limits. A body that names no tree is **not** compared: a vote may be about
something other than a landing tree, and that is a legitimate state, so it is reported
as `UNCHECKED` rather than counted against the merge. And `named_trees` resolves each
hex token against *this* clone's object store, so a claim whose tree this checkout
cannot resolve reads as no claim; the tool says `UNCHECKED`, never "agrees".

The four states, and what rc 0 covers
-------------------------------------
    NAMED      a review body names the tree this merge landed
    DIVERGED   the merge landed a tree no review of it names (rc 1)
    UNCHECKED  merged, but no review of it names a tree - compared to nothing
    PENDING    not merged, so there is no merge to audit

The last two are rc 0 and neither is a pass, which is why both are announced on stderr
rather than left to whoever reads the prose: `0` is the value a caller acts on, and
asked about a batch it covers every number in it. `PENDING` was the quieter hole of the
two - a batch of still-open numbers compares nothing at all, and the line under each one
said so only in prose nobody scripting the tool reads.

Exit codes
----------
    0  no divergence: every merged PR landed a tree one of its reviews names, or had
       no tree claim to compare. This is a statement about what diverged and never
       about how much was audited - `UNCHECKED` and `PENDING` are each printed as their
       own state and said not to be a pass on stderr
    1  at least one merged PR landed a tree no review of it names - the votes were
       about a tree that never landed
    2  the question could not be asked (gh failed, the merge commit is not obtainable
       here, a response was not readable) - fail loud, never report "landed" for a
       question that was not answered

`gh` and network access are required for the PR half; the tree half is local git.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent

#: The sibling that owns "which trees does this body claim" - the same function the
#: pre-merge refusal in `cast-vote.py` uses, so the claim and its audit are one rule.
CAST_VOTE = SCRIPTS_DIR / "cast-vote.py"

#: The sibling that owns "every page of a PR's reviews, with the projection applied".
COUNTER = SCRIPTS_DIR / "check-vote-count.py"

PENDING = "PENDING"
NAMED = "NAMED"
DIVERGED = "DIVERGED"
UNCHECKED = "UNCHECKED"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _gh_json(args: list[str]) -> object:
    return json.loads(_gh(args))


def _gh(args: list[str]) -> str:
    """Run `gh` with the program name prepended, capturing both streams.

    The program name is added here so no call site can forget it — the sibling
    tools carry the measurement of what a call site that omitted it did.
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
    return proc.stdout


_LOADED: dict[str, object] = {}


def _load(name: str, path: Path):
    """A sibling script, loaded from its file.

    The scripts in this directory are not importable modules (hyphenated names, no
    package), so this is the same loader `cast-vote.py` and `check-merge-freshness.py`
    use for their own siblings. Registered in `sys.modules` before `exec_module`
    because these modules declare dataclasses, and dataclasses resolves annotations
    through `sys.modules[cls.__module__]` at class-creation time.
    """
    if name not in _LOADED:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:  # pragma: no cover - both are in this repo
            raise RuntimeError(f"could not load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _LOADED[name] = module
    return _LOADED[name]


def cast_vote():
    """The sibling that owns the reading of a body's tree claim."""
    return _load("cast_vote_sibling", CAST_VOTE)


def counter():
    """The sibling that owns the paginated reviews read."""
    return _load("check_vote_count_sibling", COUNTER)


@dataclass
class Verdict:
    pr: int
    state: str
    landed_tree: str | None = None
    claimed: list[str] = field(default_factory=list)
    reading: str = PENDING
    reason: str = ""

    @property
    def diverged(self) -> bool:
        return self.reading == DIVERGED


def _short(sha: str) -> str:
    """One spelling of "print a tree short", shared by every line that prints one."""
    return sha[:12]


def _resolve_commit(sha: str) -> tuple[str | None, str]:
    """The merge commit, fetched if this clone does not have it.

    `(commit, "")` or `(None, why)`. A PR merged by someone else is not necessarily
    in this clone's object store, so the fetch is attempted once before giving up —
    and a failure is reported as unmeasurable, never as agreement.
    """
    if not sha:
        return None, "the merge commit is not named by the API response"
    got = _git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if got.returncode == 0 and got.stdout.strip():
        return got.stdout.strip(), ""
    fetched = _git("fetch", "--quiet", "origin", sha)
    if fetched.returncode != 0:
        return None, (
            f"the merge commit {_short(sha)} is not in this clone and could not be "
            f"fetched ({fetched.stderr.strip() or 'no reason reported'})"
        )
    again = _git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if again.returncode != 0 or not again.stdout.strip():
        return None, f"the merge commit {_short(sha)} was fetched but still does not resolve"
    return again.stdout.strip(), ""


def _tree_of(commit: str) -> tuple[str | None, str]:
    """The tree a commit carries — the thing a merge actually lands."""
    got = _git("rev-parse", f"{commit}^{{tree}}")
    if got.returncode != 0 or not got.stdout.strip():
        return None, f"{_short(commit)} has no readable tree ({got.stderr.strip()})"
    return got.stdout.strip(), ""


def tree_claims(number: int, repo: str) -> tuple[list[str], int]:
    """`(every tree this PR's reviews name, how many bodies named a token that is not one)`.

    Read through the counter's paginated helper (one request returns 30 reviews and
    the newest are the ones that count) and decoded by `cast-vote.py`'s
    `named_trees`, so the claim read here is the claim that was checked there.

    The second element is the honest half of a limit `named_trees` carries by design:
    it resolves each token against *this* clone, so a body naming a tree this checkout
    does not have returns no claim — indistinguishable, in the bare list, from a body
    naming nothing. Those are different situations with different repairs (a shallow
    clone or pruned plan worktrees, versus a vote that was never about a landing tree),
    so they are counted apart and reported apart. The token pattern is asked of the
    sibling that owns it; a second regex here would be a second answer to "which token
    is a claim".
    """
    reviews = counter()._gh_json_paginated(
        [
            "api",
            f"repos/{repo}/pulls/{number}/reviews",
            "--jq",
            ".[] | {at: .submitted_at, body: .body}",
        ]
    )
    claims: list[str] = []
    unresolved = 0
    for r in reviews:
        assert isinstance(r, dict)
        # The projection must have applied: without it the fields arrive under their
        # raw names, `at` reads as "", and the caller-owns-the-filter rule the sibling
        # documents would be broken silently here as well.
        if not r.get("at"):
            raise RuntimeError(
                f"#{number}: a review came back without `submitted_at`, so the caller's "
                "--jq projection did not apply - this tool would read no claim out of a "
                "body that has one"
            )
        body = str(r.get("body") or "")
        found = cast_vote().named_trees(body)
        if not found and cast_vote()._HEX_TOKEN.findall(body):
            unresolved += 1
        for tree in found:
            if tree not in claims:
                claims.append(tree)
    return claims, unresolved


def check_pr(number: int, repo: str) -> Verdict:
    view = _gh_json(
        [
            "api",
            f"repos/{repo}/pulls/{number}",
            "--jq",
            "{state: .state, merged: .merged, merged_at: .merged_at, "
            "merge_commit_sha: .merge_commit_sha, head_sha: .head.sha, base_sha: .base.sha}",
        ]
    )
    if not isinstance(view, dict):
        raise RuntimeError(f"#{number}: the PR response was not an object")
    state = str(view.get("state") or "")
    if not view.get("merged"):
        return Verdict(
            pr=number,
            state=state,
            reading=PENDING,
            reason=f"#{number} is {state} and unmerged, so nothing has landed yet",
        )

    commit, why = _resolve_commit(str(view.get("merge_commit_sha") or ""))
    if commit is None:
        raise RuntimeError(f"#{number}: {why}")
    landed, why = _tree_of(commit)
    if landed is None:
        raise RuntimeError(f"#{number}: {why}")

    claimed, unresolved = tree_claims(number, repo)
    if not claimed:
        return Verdict(
            pr=number,
            state=state,
            landed_tree=landed,
            claimed=[],
            reading=UNCHECKED,
            reason=(
                f"{unresolved} review body/bodies of #{number} name a token this clone "
                "cannot resolve to a tree, so no claim could be read (a shallow clone "
                "or pruned plan worktrees read this way) - the landed tree was "
                "compared to nothing"
                if unresolved
                else f"no review body of #{number} names a tree, so the landed tree was "
                "compared to nothing"
            ),
        )
    if landed in claimed:
        return Verdict(
            pr=number,
            state=state,
            landed_tree=landed,
            claimed=claimed,
            reading=NAMED,
            reason="a review body names the tree this merge landed",
        )
    return Verdict(
        pr=number,
        state=state,
        landed_tree=landed,
        claimed=claimed,
        reading=DIVERGED,
        reason=(
            "this merge landed a tree no review names - master moved between the vote "
            "and the merge, so the landed tree is base x head and the votes were about "
            "a tree that never landed"
        ),
    )


def _report(v: Verdict) -> list[str]:
    lines = [f"#{v.pr} {v.reading} - {v.reason}"]
    if v.landed_tree:
        lines.append(f"    landed tree {_short(v.landed_tree)}")
    for tree in v.claimed:
        lines.append(f"    a review names {_short(tree)}")
    return lines


def _remedy(v: Verdict) -> str:
    """The next action for a state that is not a pass - **one named branch per state**.

    The branches used to end in a fallthrough, so a reading with no branch of its own
    inherited whichever sentence happened to be last: when `PENDING` began to be
    announced it would have been handed the *unchecked* sentence - "a vote that names no
    tree is not a weaker vote" - which is an answer about a vote for a PR that has
    merged nothing. `UNCHECKED` is now named rather than left as that fallthrough, and a
    reading this function has no branch for is refused loudly: a state added later must
    not silently inherit someone else's next action (the class this whole tool exists to
    keep out of a verdict). `NAMED` is a pass, reaches no caller, and is refused here
    rather than given a sentence it would never print.
    """
    if v.reading == DIVERGED:
        return (
            f"  #{v.pr}: write the landed tree ({_short(v.landed_tree or '')}) into the "
            "record, not the one the votes named - master's CI is the first reader the "
            "union it landed has had; re-measure every still-open PR "
            "(`check-merge-plan-suite.py <N>`) before the next merge, since their "
            "readings were taken on the same base that just moved"
        )
    if v.reading == PENDING:
        return (
            f"  #{v.pr}: nothing has landed, so this number was audited against nothing "
            "- re-run this audit after the merge it names, and read no verdict here"
        )
    if v.reading == UNCHECKED:
        return (
            f"  #{v.pr}: a vote that names no tree is not a weaker vote, but nothing "
            "compared this merge to one - `cast-vote.py`'s `tree_claim_refusal` only checks "
            "a claim that was made"
        )
    raise AssertionError(
        f"_remedy has no branch for reading {v.reading!r} - a state with no branch of its "
        "own would inherit the last one's advice, which is what each branch above exists "
        "to prevent; add the branch, or do not call this for a pass"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-merge-landed.py",
        description="Did a merged PR land the tree its votes were about?",
    )
    parser.add_argument("prs", nargs="+", type=int, help="pull request number(s)")
    parser.add_argument("--repo", default="argszero/emrg", help="owner/name (default: argszero/emrg)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    args = parser.parse_args(argv)

    try:
        verdicts = [check_pr(n, args.repo) for n in args.prs]
    except (RuntimeError, KeyError, ValueError, TypeError, AssertionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "pr": v.pr,
                        "state": v.state,
                        "reading": v.reading,
                        "landed_tree": v.landed_tree,
                        "claimed_trees": v.claimed,
                        "reason": v.reason,
                    }
                    for v in verdicts
                ],
                indent=2,
            )
        )
    else:
        for v in verdicts:
            for line in _report(v):
                print(line)

    diverged = [v for v in verdicts if v.diverged]
    unchecked = [v for v in verdicts if v.reading == UNCHECKED]
    pending = [v for v in verdicts if v.reading == PENDING]
    if diverged:
        print(
            "\nA merge that landed a tree nobody voted on is not a merge those votes "
            "authorised: their reading was of (base, head) as it stood when they were "
            "cast, and the base moved. Re-measure each open PR before the next merge:",
            file=sys.stderr,
        )
        for v in diverged:
            print(_remedy(v), file=sys.stderr)
        return 1
    if unchecked:
        print(
            f"\n{len(unchecked)} of {len(verdicts)} merged PR(s) carried no tree claim: "
            "the merge landing the voted tree was **not** checked for them. This is not "
            "a pass - it is a question with nothing to answer it.",
            file=sys.stderr,
        )
        for v in unchecked:
            print(_remedy(v), file=sys.stderr)
    if pending:
        print(
            f"\n{len(pending)} of {len(verdicts)} PR(s) are unmerged: nothing was compared "
            "for them, so exit code 0 does not mean every number given was audited. This is "
            "not a pass - it is an audit of nothing.",
            file=sys.stderr,
        )
        for v in pending:
            print(_remedy(v), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
