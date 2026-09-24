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

What a stale verdict costs, and which remedy is the cheap one
-------------------------------------------------------------
A stale verdict has two remedies and their prices are not interchangeable:

* **Refresh the branch** - re-merge master into it and push the merge, so CI
  judges the real merged tree. The head has to move for CI's merge base to move,
  so this is the only way to a *pull_request* verdict about the current master.
  It also **moves the head**, and `check-vote-count.py` voids every vote that
  predates a head push: the refresh is paid for with the review the branch has
  accumulated.

  The merge, not a rebase: a rebase rewrites commits the remote already holds, so
  `git push` refuses it as non-fast-forward and the only way to publish one is the
  force-push this project forbids, an overwritten remote commit being often
  unrecoverable - measured 2026-09-19 (`cyc20260919-065231`) on `#1404`, whose
  rebase was rejected exactly that way while merging master in and pushing the
  same tree moved the head cleanly. Every refresh in this repo's history is that
  merge. The three carriers of this route - this paragraph, the per-PR remedy and
  the header printed above it - are pinned by
  `tests/test_check_merge_freshness.py::test_no_carrier_offers_a_rebase_as_the_refresh_route`.
* **Measure the landing tree** - build the tree this merge would produce against
  current master and run the guards on it (`check-merge-plan-suite.py <PR>`).
  The head does not move, so the count does not change. It is a local reading
  rather than a CI verdict, and it is the tree the merge actually lands.

  The reading is recorded as a **review** (`gh pr review --comment`), not as a
  plain comment: reviews are the only channel `check-vote-count.py` reads, so a
  review cast after the head push is a vote even when the head is ancestry-stale,
  while a plain comment carries the reading but no vote. Measured 2026-09-14
  (`cyc20260914-040021`): `#1200`'s 2nd vote was a review on a stale head and the
  counter read `2/3`; the cycle before, `#1199` and `#1201` each took their
  deciding 3rd vote that way and merged. Naming the comment as the vehicle - which
  this remedy did until then - reads as "do not vote here", and a stale PR whose
  only route to the threshold is the landing-tree vote would then never reach it.

  Being a review is necessary and not sufficient. The counter reads the *voting
  cycle* out of the body, so a review whose body carries no cycle id is excluded
  from the run: measured 2026-09-16 (`cyc20260916-020149`) the remedy above was
  followed exactly - `gh pr review 1255 --comment --body-file review1255.md`,
  rc 0, no output - and the count did not move (`VOID (no cycle id) - no cycle id
  in the vote body`) on both `#1255` and `#1258`, two votes spent invisibly in one
  run. Neither signal at the call site says so: `gh` prints nothing on success,
  and the voiding is only visible to a reader who re-runs the counter afterwards.
  So the remedy names `scripts/cast-vote.py`, which refuses to post a body the
  counter cannot attribute (none, or more than one, cycle id) and then reads the
  count back rather than assuming the POST worked - the loss was silent on both
  sides, so the fix had to be a check on both sides.

Measured 2026-09-14 (`cyc20260914-010711`, master `abe6f8b`), the whole queue
stale: `#1197` 2 valid votes, `#1198` 1, `#1199` and `#1200` none. The blanket
advice this tool printed until then - "re-merge master into each stale branch" -
would have voided three cycles of review on `#1197`/`#1198` to buy nothing those
branches did not already have: the votes were still about the head, and the tree
the merge lands was measurable without moving it.

So the remedy is now per PR and priced by that count. A stale verdict that is
*not* ancestry-shaped (no run for the head, a run still going, a run that
concluded non-success) does not have a refresh for a remedy at all, and is now
named with the remedy that does fit it instead of a rebase that would not answer
its question. The count is read from the sibling tool that owns it (one extra
`gh` query per ancestry-stale PR, and none otherwise); when it cannot be read the
line says so and prices the refresh pessimistically, rather than reporting `0`,
which is the direction that quietly spends votes.

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
import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = "argszero/emrg"

# The gate this repo merges on, and therefore the number of votes a refresh puts
# at risk. Passed to the sibling counter explicitly rather than defaulted on both
# sides, so a change to the gate has one place to land.
_VOTES_NEEDED = 3

# How long this gate may keep re-asking the counter while GitHub has not computed
# mergeability yet (see `_valid_votes`). A gate a reader is watching may wait; the
# alternative is reporting the price of a refresh as "unavailable" at exactly the
# moment the price decides the action. The counter's own refusal still wins when
# the budget runs out.
_MERGEABILITY_WAIT = 60.0

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

# The four ways a verdict can fail to be current. They were prose in `reason`
# before, which is enough to *report* the state and not enough to choose a
# remedy: the state decides which action the tool may recommend, and only one of
# the four is fixed by a refresh (and that one charges the whole vote count).
_KIND_ANCESTRY = "ancestry"  # the #1137 case: green, but about an older master
_KIND_NO_RUN = "no_run"  # master is an ancestor but nothing ever judged the head
_KIND_RUNNING = "running"  # a run exists and has not concluded
_KIND_FAILING = "failing"  # a run concluded non-success


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
    # Which of the four ways (one of the `_KIND_*` names); "" when fresh.
    stale_kind: str = ""


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
            stale_kind=_KIND_ANCESTRY,
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
            stale_kind=_KIND_NO_RUN,
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
            stale_kind=_KIND_RUNNING,
            reason=f"CI is still {conclusion or 'pending'} on head {head_sha[:8]} - no verdict yet",
        )
    if conclusion != "success":
        return Verdict(
            **common,
            stale=True,
            stale_kind=_KIND_FAILING,
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


# ── the price of a refresh, read from the tool that owns the count ───────────

_SIBLING = Path(__file__).resolve().parent / "check-vote-count.py"
_sibling: object | None = None


def votes_counter():
    """The sibling module that owns "is this vote still about this head?".

    Loaded from its file rather than imported by name: the scripts in this
    directory are not importable modules (hyphenated names, no package), and this
    is the same loader `check-merge-pairs.py` uses for its sibling and both test
    modules use for theirs. Imported rather than reimplemented because a second
    reading of the vote rule is a second answer to "how many votes does this PR
    have", and one number now decides the remedy.
    """
    global _sibling
    if _sibling is None:
        spec = importlib.util.spec_from_file_location("check_vote_count", _SIBLING)
        if spec is None or spec.loader is None:  # pragma: no cover - the file is in this repo
            raise RuntimeError(f"could not load {_SIBLING}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _sibling = module
    return _sibling


@dataclass(frozen=True)
class Price:
    """What a refresh charges this branch: the approvals it voids, and the veto it resets.

    Two quantities, not one, and #1562 is why. `valid_votes` is the counter's
    `valid_count` - the approvals in the trailing run, which is what the gate counts
    toward its threshold. A veto contributes **0** to that number by construction: its
    entire effect is *resetting* the run it lands in. So the one vote that governs
    whether a refresh is free is invisible in the count, and a branch whose only vote
    is a standing veto was handed the sentence "0 valid votes - nothing to void" -
    false in its case, and false in the direction that spends a review the branch
    cannot re-earn, because the same push that voids an approval voids the veto and
    three fresh approvals then satisfy the gate over a defect nobody addressed.

    `vetoes` is that missing quantity: the vetoes *about this head*. Deliberately not
    the counter's parallel `counted` list - its run walk appends `True` for every veto
    unconditionally (`check-vote-count.py`), including one the head push has already
    voided, so `counted` cannot separate a live veto from a spent one. `valid` can: it
    is precisely "about this head, and attributable to one cycle".

    A veto that stands *beside* a satisfied run is not priced here; that branch is
    reached at `valid_votes > 0`, where the sentence is already about votes at risk.
    What this record exists to stop is the one reading that is not merely optimistic
    but blind: calling a state with a veto in it "nothing to void".
    """

    valid_votes: int | None
    vetoes: int = 0
    unread: str = ""

    @property
    def veto_clause(self) -> str:
        """The noun phrase for the standing veto(es), or `""` when none stands."""
        if not self.vetoes:
            return ""
        return f"{self.vetoes} standing veto" + ("" if self.vetoes == 1 else "es")


def _standing_vetoes(verdict: object) -> int:
    """The vetoes in `verdict` that are about its head: `kind == "veto" and valid`.

    `valid` rather than `counted` - see `Price`. Mutation arm for this reading (drop
    `and vote.valid`): a verdict carrying a veto the head push has already voided is
    then priced as a standing one, and the price line stops distinguishing a spent
    veto from a live one.
    """
    return sum(
        1
        for vote in getattr(verdict, "votes", [])
        if getattr(vote, "kind", "") == "veto" and getattr(vote, "valid", False)
    )


def _valid_votes(pr: int) -> Price:
    """What a refresh of this PR would cost, or `Price(None, unread=why it failed)`.

    Advisory, so it degrades instead of failing the run: the freshness verdict
    above is answerable without it, and exiting 2 because a *price* could not be
    read would trade a real answer for a missing one. It never degrades to `0`
    though - zero is the line that says "refresh freely", so reporting it without
    having read it would be the direction that spends votes.

    The read waits (`_MERGEABILITY_WAIT`) for the one transient this gate hit in
    practice (2026-09-16): right after a push or a merge GitHub reports
    mergeability as `UNKNOWN`, and the counter refuses it - which is correct, but
    it made the *price* of a stale branch unreadable exactly when the decision is
    being taken. Advising a refresh without knowing how many votes it voids is the
    one degradation this function exists to avoid.
    """
    try:
        verdict = votes_counter().check_pr(
            pr, _VOTES_NEEDED, mergeability_wait=_MERGEABILITY_WAIT
        )
        return Price(int(verdict.valid_count), _standing_vetoes(verdict))
    except Exception as exc:  # advisory by construction - see the docstring above
        return Price(None, unread=f"{type(exc).__name__}: {exc}".replace("\n", " ")[:200])


def _remedy(pr: int, kind: str, price: Price) -> str:
    """One line: what to do about this verdict, and what it charges.

    The price is attached only where it is actually paid - an ancestry-stale
    verdict is the one a refresh cures. The other three kinds get the action that
    fits them, so the output cannot be read as "rebase and move on" for a head
    whose CI run is merely missing (a refresh is a remedy there too, but the
    expensive one: re-triggering fires a run on the same head and keeps the
    votes).

    The ancestry-stale branch has **three** states, not two: nothing to void, a
    price in approvals, and - the one that went missing - no approvals but a
    standing veto, where the refresh is not free and "nothing to void" is the one
    claim that is false (#1562).
    """
    if kind == _KIND_ANCESTRY:
        if price.valid_votes is None:
            return (
                f"#{pr}: vote count unavailable ({price.unread or 'not read'}) - read it "
                f"before refreshing (`scripts/check-vote-count.py {pr}`): a refresh moves "
                "the head and voids every vote the branch has"
            )
        if price.valid_votes == 0 and not price.vetoes:
            return (
                f"#{pr}: 0 valid votes - nothing to void. Re-merge master into the branch "
                "and push the merge (`git fetch origin master`, `git merge FETCH_HEAD`, "
                "`git push origin <branch>`) so CI judges the real merged tree - the merge, "
                "not a rebase: a rebase of a pushed branch is refused as non-fast-forward, "
                "and publishing one needs the force-push this project forbids"
            )
        if price.valid_votes == 0:
            return (
                f"#{pr}: 0 valid votes, but {price.veto_clause} at this head - the refresh "
                "is NOT free: it moves the head and voids the veto as surely as it voids an "
                "approval, and with no approval in the run the gate is then satisfied by "
                "fresh approvals alone, over the defect the veto named and nothing else "
                "changed. Answer what the veto names first, and refresh only once it is "
                "gone (`git fetch origin master`, `git merge FETCH_HEAD`, "
                "`git push origin <branch>`) - the merge, not a rebase: a rebase of a "
                "pushed branch is refused as non-fast-forward, and publishing one needs "
                "the force-push this project forbids"
            )
        beside = f" (and the {price.veto_clause} standing there)" if price.vetoes else ""
        return (
            f"#{pr}: {price.valid_votes} valid vote(s) at risk - a refresh moves the head, "
            f"and the vote counter voids all {price.valid_votes}{beside}. Measure the tree "
            "this merge would land "
            "instead (`git fetch origin master`, then `scripts/check-merge-plan-suite.py "
            f"{pr}`) and cast the vote on it (`scripts/cast-vote.py {pr} --body-file <path>`), "
            "stating the landing tree the review is about: the head does not move, so the "
            "votes already cast stay valid and this one is counted - reviews are the channel "
            "the counter reads, a plain comment carries the reading but no vote. The body "
            "must carry this cycle's id (`cycYYYYMMDD-HHMMSS`): the counter reads the voting "
            "cycle out of the body and excludes a review without one, and `gh pr review` "
            "prints nothing on success, so such a vote is spent in silence - which is why the "
            "casting is done by `scripts/cast-vote.py`, that refuses a body the counter cannot "
            "attribute and then reads the count back. Refresh only if that tree fails - those "
            "votes were about a tree that can no longer be merged"
        )
    if kind == _KIND_NO_RUN:
        return (
            f"#{pr}: no run for this head - re-trigger CI on the same head (`gh workflow run "
            "test.yml --ref <branch>`, or `scripts/re-trigger-ci.sh <branch>`), which keeps "
            "the votes. A refresh would fire a run too, and cost every vote the branch has"
        )
    if kind == _KIND_RUNNING:
        return (
            f"#{pr}: wait for the run - neither a refresh nor a re-trigger answers a run "
            "that has not concluded"
        )
    return (
        f"#{pr}: fix the failure - a refresh costs every vote the branch has, and does not "
        "make a failing run pass"
    )


def _prices(verdicts: list[Verdict]) -> dict[int, Price]:
    """The vote count for each verdict whose remedy depends on it.

    Only the ancestry-stale kind: the other kinds are told an action that does not
    depend on the count, so reading it for them would be a `gh` query with no
    consumer - and, worse, a query that can only change what the tool prints for a
    verdict whose remedy is already decided.
    """
    return {
        v.pr: _valid_votes(v.pr)
        for v in verdicts
        if v.stale and v.stale_kind == _KIND_ANCESTRY
    }


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

    # After the verdicts, and outside their error path: a failure to read a price
    # is not a failure to answer the question above (see `_valid_votes`).
    prices = _prices(verdicts)

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
                        "stale_kind": v.stale_kind,
                        # Both halves of the price. `valid_votes` alone reads as "0
                        # means free" for a head whose only vote is a standing veto -
                        # the reading #1562 is about - so the veto count is emitted
                        # beside it and a consumer never has to infer it.
                        "valid_votes": prices[v.pr].valid_votes if v.pr in prices else None,
                        "standing_vetoes": prices[v.pr].vetoes if v.pr in prices else None,
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
            "\nA stale verdict has no free remedy: refreshing a branch (re-merge master "
            "in, then push it - a rebase cannot be published without the force-push this "
            "project forbids) moves its head, and `check-vote-count.py` voids every vote "
            "predating a head push. Per stale PR:",
            file=sys.stderr,
        )
        for v in verdicts:
            if not v.stale:
                continue
            price = prices.get(v.pr, Price(None))
            print("  " + _remedy(v.pr, v.stale_kind, price), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
