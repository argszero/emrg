#!/usr/bin/env python3
"""Check that a *sequence* of merges lands a tree the repo's guards accept.

The class this exists for
------------------------
The sibling gates answer questions about one PR, or about one merge:

* `check-vote-count.py`       - do enough votes still apply to this head?
* `check-pr-base.py`          - can a merge here still reach master? (#1152)
* `check-merge-freshness.py`  - is the green verdict about the tree that merges?
* `check-merge-order.py`      - which other PRs would a merge here dirty?
* `check-merge-tree-health.py`- does *this* PR's merge land a healthy tree? (#1155)

None of them answers the question the queue is actually stuck on: **given a
plan — "merge these PRs in this order" — does every step still land a tree the
repo's guards accept?** Health is a property of each *step*, and a step's
input is the tree the previous step produced, so it is not derivable from any
per-PR fact.

Measured 2026-09-12 (`cyc20260912-174026`) on master `02e43c8`, on the queue as
it actually stood:

    master + #1167                 -> CLEAN, guard OK (count-line era)      ok
    master + #1166                 -> CLEAN, guard OK (count-line era)      ok
    master + #1167 then #1166      -> CLEAN, documents 1541, collects 1560  GUARD FAILS

Both PRs set the count line to `(1541)`, so the second merge writes a line that
is already equal on both sides: git keeps one copy, reports no conflict, and the
stale number rides into master. The guard goes red one minute after the merge,
on master, where nobody is looking.

Why "fewest conflicts" is not a safe heuristic
----------------------------------------------
Two PRs with *different* count values always conflict; two with the *same*
value always merge silently. So on this shared derived line the danger is
inversely related to the signal:

    #1168 (1530) merged first, then:
        #1166 -> CONFLICT on Agent.md      <- forces a human to look
        #1153 -> CONFLICT on Agent.md
        #1167 -> CONFLICT on Agent.md

Measured on the same tree. A conflict is the *safe* outcome — it makes someone
stop — while `check-merge-order.py` ranks a pair by how little it dirties
others, which is precisely the clean-and-silent case. This tool exists so that
"the cheapest order" is not chosen on a misread signal.

Why the per-PR health check cannot substitute
---------------------------------------------
`check-merge-tree-health.py` is correct for what it asks, but in this queue it
degenerates, for a structural reason: every open head contains master's tip, so
`merge-tree master head` is a real merge whose result equals the *branch* tree.
The guard then answers "is this branch self-consistent?", which a branch under
review always is. Run against this queue it reported all five clean PRs
`HEALTHY` — each printing its own value — including the pair that is unsafe
together. Per-step health is not a substitute for sequence health.

Usage
-----
    # check the whole plan: master, then these PRs in this order
    uv run --no-sync python3 scripts/check-merge-sequence.py 1168 1167 1166

    # the default plan: open PRs, ascending, each merged onto the tree built so
    # far (a starting point, not a recommendation - see "The default plan" below)
    uv run --no-sync python3 scripts/check-merge-sequence.py

    # the literal every-open-PR plan, conflicting steps included
    uv run --no-sync python3 scripts/check-merge-sequence.py --all

    # start from a ref other than master
    uv run --no-sync python3 scripts/check-merge-sequence.py --base origin/master 1153

Each step is reported as one of:

    OK        the merge is clean and the resulting tree passes the guards
    DANGER    the merge is clean but the resulting tree FAILS the guards
              (the silent case this tool exists for — git would not have told you)
    conflict  the merge conflicts, so no tree is produced and none is judged
              (not a failure: it is a question for a human / check-merge-order)
              - but a step's input is the previous step's tree, so the plan stops
              here and the steps *after* it are never measured (exit 3)

Exit codes
----------
    0  every step of the plan was measured and landed a tree that passes the guards
    1  at least one clean step landed a tree that FAILS them (the finding)
    2  the question could not be answered (git/gh/guard failure, an empty open-PR
       list, or a default plan with no mergeable PR in it) - fail loud, never report
       health that was not measured
    3  the plan stopped at a conflict, so only a prefix was measured and the rest
       is unmeasured - "not measured" must not be spelled 0

The default plan, and why it is not "every open PR"
----------------------------------------------------
Measured 2026-09-13 (`cyc20260913-120524`): **13 of the 14 open PRs conflicted with
the base**, so the literal default - every open PR, ascending - stopped at step 1
and judged nothing (`plan stopped at a conflict, 0 of 13 steps measured`, exit 3).
That invocation answers nothing, and it cannot answer the question this tool exists
for: a danger pair is two PRs that are each clean, and a plan that cannot get past
step 1 never reaches the second one. Every pair found so far was found by naming
both PRs explicitly.

The default plan is therefore built by walking the open PRs and merging each one
onto **the tree built so far**, keeping the steps that merge and naming the ones
that do not:

    plan source: open PRs that can be merged in this order (8 of 13); excluded as conflicting: #1152 #1153 #1170

The first revision of this filter tested each candidate against `base` alone, and
that is not the same question the loop asks: a step's input is the previous step's
tree. Measured on this repo's live queue (`cyc20260913-144807`), with 13 open PRs
of which 11 merge cleanly onto the base:

    plan: #1141 -> #1145 -> #1151 -> #1152 -> #1153 -> ...
    #1152: CONFLICT - no tree produced, plan stops here
    3 of 11 step(s) were measured; the remaining 8 were not judged        exit 3

#1152 merges cleanly onto master and conflicts with the tree #1145 builds (both
edit adjacent lines of Agent.md), so a pairwise-clean set stopped the plan at step
4 - the same "the default invocation answers nothing" failure the filter was added
to fix, one indirection further in. The filter and the loop disagreed about what
they were measuring; now they measure the same thing.

This is the ascending greedy plan, and it is not necessarily the *largest*
achievable set: skipping an early PR could in principle admit two later ones. What
it guarantees is that every step it plans was measured, and that the ones it left
out are named with the reason. On the queue above it is also maximal (8 of 13,
matching the largest pairwise-clean subset), but that is a property of that queue,
not of the algorithm. Because every planned step can be taken, the "stopped at a
conflict" exit is no longer reachable from the default plan; it remains for `--all`
and for positional numbers, which are taken exactly as given.

Nothing is hidden: an excluded PR is named, `--all` gives the literal every-open-PR
plan, and positional PR numbers are always taken exactly as given. If no open PR
merges cleanly the plan is empty, and that is 2 - the question was not answered -
never a pass over zero steps.

A pass over zero steps is refused at its source, not counted
------------------------------------------------------------
The plan can never be empty, so "all 0 step(s) landed trees that pass the guards"
is unreachable: the default source refuses an empty list itself -

    numbers = [int(line) for line in proc.stdout.split() if line.strip()]
    if not numbers:
        raise MeasurementError("no open PRs reported - nothing to check")

- and `prs` is `nargs="*"`, so `args.prs or _open_pr_numbers(...)` makes an empty
`numbers` impossible: either positional PR numbers were given, or the source
raised. Measured 2026-09-13 (`cyc20260913-114142`) with the real script and an
empty open-PR list (a stub `gh` on PATH printing nothing, exiting 0) on both
master `2017d8f` and this branch:

    $ check-merge-sequence.py            # stub gh: prints nothing, rc 0
    could not measure: no open PRs reported - nothing to check
    --- exit code: 2 ---

Correcting this branch's own first revision: it documented this state as a
*measured* rc-0 pass, and added a check in `main` to convert it to 2. The rc-0
shape is reproducible only by replacing the refusal -

    mod._open_pr_numbers = lambda repo: []      # the guard removed
    # "all 0 step(s) landed trees that pass the guards", rc 0

- which is what produced that reading, and the same substitution its test made.
The reading was therefore about the stub, not about the program, and the branch
was dead code. The property it wanted ("zero measured steps is never a verdict")
already holds here, at the source; what was missing is a test for that refusal,
which is now `test_an_empty_open_pr_list_is_refused_at_its_source`.

Why "stopped" is 3 and not 0 or 1
---------------------------------
A conflict is not a finding - nothing was judged wrong - so it must not be 1. But
it is not health either: 0 is defined above as *every* step measured and passing,
and a plan that stopped at its first step measured nothing at all. Measured on
this repo's live queue (`cyc20260913-084752`): the default invocation - every open
PR ascending - stopped at step 1 (`#1136` conflicts) and exited **0** having
measured no tree, which as a verdict is indistinguishable from a fully verified
plan. A caller testing the exit code reads "the plan is fine" for a plan that was
never checked.

The states have different remedies, which is why they get different codes (the
same reason `check-vote-count.py` separates `BLOCKED` from `SHORT`): resolve the
conflict and re-plan, versus re-order a step that is dangerous, versus retry a
measurement that failed. A caller that wants "nothing was found wrong" can test
`rc in (0, 3)` and still gets an explicit number to read; a caller that treats 0
as "verified" now cannot be told the wrong thing.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# The guard is judged by its exit code, but its own report line names the
# numbers, so it is captured and quoted rather than re-derived. Same constants
# as check-merge-tree-health.py: both tools judge the same guard, and two
# spellings of its report would be one spelling too many.
GUARD = "scripts/check-doc-count.py"
# The guard's report shape changed on 2026-09-13 (#1158): it no longer compares a
# stored count against the collection, it reports files that store one at all, and
# the count is measured on demand. Both regexes are kept so the step report names
# what the guard found instead of a bare "guard OK".
COUNT_IN_REPORT = re.compile(r"FAIL: (\d+) tracked file\(s\) state")
OK_IN_REPORT = re.compile(r"OK: no tracked file states the Python test count")

# The document whose count line the guard reads, and the one command that repairs
# it after a merge (measured on the merged tree, never chosen). Both are printed
# in the empty-plan refusal, and only for the path they apply to: advice for a
# conflict in some other file would be advice that does not run.
COUNT_LINE_DOC = "Agent.md"
RESOLVER = "uv run --no-sync python3 scripts/check-doc-count.py --resolve-conflict"


class MeasurementError(Exception):
    """The question could not be answered. Never a verdict."""


def _run(argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command with the decoding pinned.

    `encoding`/`errors` are pinned for the reason recorded in
    `check-doc-count.py`: a locale mismatch leaves `stdout` as `None` after the
    reader thread swallows the decode error, and the `None` surfaces later as a
    bare `TypeError` past every handler.
    """
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _ref_exists(name: str) -> bool:
    """Is `name` a ref, given it fully?"""
    return _run(["git", "show-ref", "--verify", "--quiet", name]).returncode == 0


def _qualify_ref(ref: str) -> str:
    """Expand a short remote-tracking name to its fully-qualified form.

    `--base` defaults to the short spelling `origin/master`, and that spelling is
    ambiguous: git resolves a bare name by a precedence list, and
    `refs/heads/<name>` is consulted *before* `refs/remotes/<name>`. A local
    branch called `origin/master` - which git itself creates when a fetch
    destination is written unqualified, the trap `_refresh_base` describes -
    therefore shadows the remote-tracking ref, and every measurement after that
    answers about the wrong tree. Reproduced on this repo (`cyc20260913-072845`):
    with a local `refs/heads/origin/master` left at `02e43c8` while the real
    remote-tracking ref was `245125e`, the tool printed

        base 02e43c82 (origin/master)

    and measured that two-cycle-old tree. As with a stale base, the defect is not
    the age of the commit but the *identity* of the ref: the header named master
    and the code used something else.

    So the name is not resolved by guessing better - the remote-tracking ref is
    looked up **by its full name**, where precedence does not apply. A short name
    that denotes only a local branch is refused rather than measured: it is not
    the remote master, whatever it is called.

    The shadow case *warns* instead of failing because the qualified lookup makes
    the answer right either way; refusing would block a checkout for a stray ref
    that no longer affects this tool's verdict. The warning is not cosmetic - the
    same stray branch silently misleads every other short-name reader, including
    `git checkout origin/master`.
    """
    if not ref.startswith("origin/") or ref.count("/") != 1:
        return ref
    qualified = f"refs/remotes/{ref}"
    if _ref_exists(qualified):
        if _ref_exists(f"refs/heads/{ref}"):
            print(
                f"warning: {ref} is ambiguous - a local branch shadows it; "
                f"measuring {qualified}. Delete the shadow: "
                f"git branch -D {ref}",
                file=sys.stderr,
            )
        return qualified
    if _ref_exists(f"refs/heads/{ref}"):
        raise MeasurementError(
            f"{ref!r} is ambiguous and denotes only the local branch "
            f"refs/heads/{ref}: no {qualified} exists"
        )
    return ref


def _rev_parse(ref: str) -> str:
    """Resolve a ref to a commit SHA.

    Every ref that reaches `merge-tree` goes through here first. A *name* is
    mutable in a way a SHA is not - `fetch` rewrites `FETCH_HEAD`, so passing a
    name would answer about whatever the last fetch happened to leave behind.
    That defect was measured in `check-merge-order.py`, and it bit this cycle's
    own probe: a harness loop that re-fetched left `FETCH_HEAD` pointing at the
    wrong PR, so "master" was silently one of the subjects.

    A name is also *ambiguous* in a way a SHA is not, which is the second half of
    the same defect: `_qualify_ref` runs first so the commit returned is the one
    the caller's name denotes, not the one git's precedence rules would pick.
    """
    proc = _run(["git", "rev-parse", "--verify", f"{_qualify_ref(ref)}^{{commit}}"])
    if proc.returncode != 0:
        raise MeasurementError(
            f"could not resolve {ref!r} to a commit: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _plan_from_open_prs(
    repo: str, base: str, include_conflicting: bool
) -> tuple[list[int], str]:
    """The default plan: open PRs, ascending, each merged onto the tree built so far.

    Two measurements shaped this, and the second corrected the first.

    Measured 2026-09-13 (`cyc20260913-120524`, on the queue as it stood): **13 of 14
    open PRs conflicted with the base**, so the literal default - every open PR,
    ascending - stopped at its first step and judged nothing at all (`plan stopped
    at a conflict, 0 of 13 steps measured`, exit 3). That is exactly the invocation a
    reader reaches for first, and it answers nothing. Worse, it cannot answer the
    question this tool exists for: a danger pair is two PRs that are each clean, and
    a plan that cannot advance past step 1 never sees the second one. Every such pair
    found so far (#1173<->#1174, #1174<->#1176, #1176<->#1178) was found by naming
    *both* PRs explicitly.

    Measured 2026-09-13 (`cyc20260913-144807`, the other measurement): filtering the
    candidates to "merges cleanly onto `base`" fixed the all-conflicting queue and
    left a subtler form of the same failure. On that day's queue - 13 open PRs, **11
    of which merge cleanly onto the base** - the default plan still stopped at step 4
    (`#1152: CONFLICT`, `3 of 11 step(s) were measured`, exit 3), because **a step's
    input is the tree the previous step produced, while the filter tested each
    candidate against `base` alone**. #1152 merges cleanly onto master and conflicts
    with the tree #1145 builds. A pairwise-clean set is not a sequence; the filter
    and the loop disagreed about what they were measuring.

    So the plan is built by walking the candidates in ascending order and merging
    each one onto the tree built so far, keeping the steps that merge and naming the
    ones that do not. Every step of the returned plan can be taken, which is what
    makes "every step was measured" reachable from the default at all.

    This is the ascending greedy plan, not necessarily the *largest* achievable set:
    skipping an early PR could in principle admit two later ones. What it guarantees
    is that every planned step was measured and that the exclusions are named. On the
    queue above it happens to be maximal too (8 of 13), but that is a property of
    that queue, not of the algorithm.

    The excluded numbers are **named in the note** rather than dropped, because a
    plan that hides its own omissions is the defect this whole file is about. Use
    `--all` for the literal "every open PR" plan.
    """
    numbers = _open_pr_numbers(repo)
    if include_conflicting:
        return numbers, f"plan source: every open PR (--all), {len(numbers)} in total"
    planned: list[int] = []
    excluded: list[int] = []
    current = base
    excluded_heads: dict[int, str] = {}
    for number in numbers:
        head = _fetch_head(number)
        merged = _merge_commit(current, head)
        if merged is None:
            # Not taken, and not judged: the next candidate is tested against the
            # same tree this one was, so one bad step does not abandon the queue.
            # The head is kept so the empty-plan refusal can name *which paths*
            # conflicted (see `_conflict_summary`).
            excluded.append(number)
            excluded_heads[number] = head
            continue
        planned.append(number)
        current = merged
    if not planned:
        raise MeasurementError(
            f"all {len(numbers)} open PR(s) conflict with {base[:8]}, so the default "
            f"plan is empty and no step could be measured. This is not a verdict "
            f"about any tree."
            + _conflict_summary(base, excluded, excluded_heads)
            + f" Naming a PR explicitly does not clear a conflict - it is a conflict "
            f"wherever it is planned, and `--all` plans them all and stops at the "
            f"first."
        )
    note = (
        f"plan source: open PRs that can be merged in this order "
        f"({len(planned)} of {len(numbers)}"
    )
    if excluded:
        note += "); excluded as conflicting: " + " ".join(f"#{n}" for n in excluded)
    else:
        note += "; none excluded)"
    return planned, note


def _conflict_paths(a: str, b: str) -> list[str]:
    """The paths `git merge-tree` reports as conflicted between `a` and `b`.

    Reporting only. An empty list means git said nothing this parser recognises as
    a path, so no caller may read "no paths" as "no conflict" - `_merge_commit`
    is what decides whether a merge conflicts; this only describes one.
    """
    proc = _run(["git", "merge-tree", "--write-tree", a, b])
    paths: list[str] = []
    for line in (proc.stdout + proc.stderr).splitlines():
        if not line.startswith("CONFLICT"):
            continue
        match = re.search(r"Merge conflict in (.+?)\s*$", line)
        paths.append(match.group(1) if match else line.strip())
    return sorted(set(paths))


def _conflict_summary(base: str, excluded: list[int], heads: dict[int, str]) -> str:
    """Which paths the excluded PRs conflict in, and what clears that here.

    Why this exists, measured 2026-09-13 (`cyc20260913-125509`) on the queue as it
    stood immediately after a merge moved the derived count: **every one of the 13
    open PRs conflicted, all of them in `Agent.md`, 10 of them in that file alone**
    - and the remedies this refusal used to offer ("pass PR numbers explicitly, or
    use --all") resolve neither, because an explicitly named conflicting PR is
    still a conflict. A refusal that names no working way out is the same defect as
    a hint that cannot run, so the paths are counted here rather than asserted in
    prose, and the remedy is printed only for the path that has one.
    """
    counts: dict[str, int] = {}
    for number in excluded:
        for path in _conflict_paths(base, heads[number]):
            counts[path] = counts.get(path, 0) + 1
    if not counts:
        return ""
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    out = (
        f" Conflicting paths over those {len(excluded)} PR(s): "
        + ", ".join(f"{path} x{n}" for path, n in ranked[:4])
        + "."
    )
    if counts.get(COUNT_LINE_DOC):
        out += (
            f" {COUNT_LINE_DOC} carries the derived Python test count that {GUARD}"
            f" measures, and two PRs that both rewrote it cannot be merged together"
            f" - the way out is to merge the base in, re-measure the line on the"
            f" merged tree (`{RESOLVER}`), and push; the push re-plans the PR."
        )
    return out
def _refresh_base(base: str) -> None:
    """Bring the base up to date when it names a remote-tracking branch.

    Every PR head is fetched from the network; the base was not, so a checkout
    whose `origin/master` is behind answered a different question than the one
    asked, and printed the stale commit as if it were master (`cyc20260912-203927`).
    Measured on this repo with `origin/master` left two commits behind:

        base 02e43c82 (origin/master)   <- 02e43c8 is NOT master; 3dbc2f1 is

    and the mislabelled base changes the verdict: over 25 plans, 16 differed
    between a stale and a fresh base, e.g. the plan below reads as 2 DANGER steps
    against the stale base and as a conflict (safe, no tree produced) against the
    live one. A gate that answers about the wrong tree is the failure this file
    already documents for `__file__`-relative tools; the base is the same trap in
    the time dimension.

    Only `origin/<branch>` is refreshed: any other ref is taken literally, and a
    SHA is immutable by construction. A fetch failure is a measurement error -
    the caller must not silently continue against a base it could not verify.

    The destination is written **fully qualified**. A bare `origin/master` as a
    fetch destination is ambiguous, and git resolves it by creating a *local
    branch* named `origin/master` (`refs/heads/origin/master`) - so the first
    version of this fix silently littered the checkout with a ref that shadows
    the remote-tracking one and makes every later `origin/master` ambiguous
    (`cyc20260912-203927`, caught by git's own "refname is ambiguous" warning).
    """
    if ":" in base or not base.startswith("origin/"):
        return
    branch = base[len("origin/"):]
    dest = f"refs/remotes/origin/{branch}"
    proc = _run(["git", "fetch", "--quiet", "origin", f"+refs/heads/{branch}:{dest}"])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise MeasurementError(f"could not refresh {base}: {detail}")


def _open_pr_numbers(repo: str) -> list[int]:
    """Every open PR number, ascending - the source the default plan is drawn from."""
    proc = _run(
        [
            "gh", "pr", "list", "-R", repo,
            "--limit", "100", "--state", "open",
            "--json", "number", "--jq", ".[].number",
        ]
    )
    if proc.returncode != 0:
        raise MeasurementError(f"gh pr list failed: {proc.stderr.strip()}")
    numbers = [int(line) for line in proc.stdout.split() if line.strip()]
    if not numbers:
        raise MeasurementError("no open PRs reported - nothing to check")
    return sorted(numbers)


def _fetch_head(number: int) -> str:
    """Fetch a PR's real head into a temp ref and return the ref name.

    The refspec is forced (`+`): a PR head is routinely re-pushed to a commit
    that is not a descendant of the previous one (every conflict resolution in
    this repo does), and a rejected fetch would leave the *stale* ref in place,
    so the check would silently answer about a tree that is no longer the PR.
    """
    ref = f"refs/emrg-merge-seq/pr{number}"
    proc = _run(["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise MeasurementError(f"could not fetch PR #{number}: {detail}")
    return _rev_parse(ref)


def _merge_commit(a: str, b: str) -> str | None:
    """Materialise the merge of commits `a` and `b` as a commit, or None if it conflicts.

    `merge-tree --write-tree` yields a tree sha, but a *tree* cannot be merged
    again - the next step needs a commit to be one side of the merge. So the
    tree is wrapped in a real merge commit via `commit-tree`, with both parents
    recorded, which is exactly what a merge would have produced. Nothing is
    written to the working tree, so a check can never leave the checkout dirty
    (the uncommitted-repair trap recorded in `check-merge-tree-health.py`).

    A non-zero/one exit is a measurement failure, never a conflict: reporting
    "conflict" for a git error would turn an unanswered question into a
    reassuring one.
    """
    proc = _run(["git", "merge-tree", "--write-tree", a, b])
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        raise MeasurementError(
            "merge-tree failed: " + (proc.stdout[-400:] + proc.stderr[-400:]).strip()
        )
    tree = proc.stdout.strip().splitlines()[0].strip()
    commit = _run(
        ["git", "commit-tree", tree, "-p", a, "-p", b, "-m", f"merge {b[:8]} into {a[:8]}"]
    )
    if commit.returncode != 0:
        raise MeasurementError(f"commit-tree failed: {commit.stderr.strip()}")
    return commit.stdout.strip()


def _guard_verdict(tree_sha: str, workdir: Path) -> tuple[bool, str]:
    """Run the extracted tree's own guard on the extracted tree.

    The tree's *own* copy is run, at the same path CI uses, reading its own tree
    - the guard cannot be modelled here, because the count is whatever the tree
    collects (it depends on imports, conftest and parametrisation).

    A guard that cannot run at all is a measurement error, not a pass: "I could
    not check" reported as healthy is how a broken tree reaches master.
    """
    if workdir.exists():
        import shutil

        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    archive = subprocess.Popen(
        ["git", "archive", tree_sha],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
        tar.extractall(workdir, filter="data")
    if archive.wait() != 0:
        raise MeasurementError(f"git archive failed for tree {tree_sha[:8]}")

    script = workdir / GUARD
    if not script.is_file():
        raise MeasurementError(f"{GUARD} is not present in the merged tree")

    proc = _run([sys.executable, str(script)], cwd=str(workdir))
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        m = OK_IN_REPORT.search(out)
        return True, "no stored count" if m else "guard OK"
    if proc.returncode == 1:
        m = COUNT_IN_REPORT.search(out)
        detail = (
            f"{m.group(1)} tracked file(s) state the test count"
            if m
            else (out.strip().splitlines() or ["guard FAILED"])[-1]
        )
        return False, detail
    raise MeasurementError(
        f"the merged tree's guard could not run (rc={proc.returncode}):\n"
        + out[-1000:].strip()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that a sequence of merges lands trees the guards accept."
    )
    parser.add_argument(
        "prs", nargs="*", type=int,
        help="PR numbers, in the order they would be merged (default: all open, ascending)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help=(
            "plan every open PR, including ones that conflict with the base "
            "(default: only the open PRs that can be merged in ascending order, each "
            "onto the tree the steps before it build)"
        ),
    )
    parser.add_argument("--repo", default="argszero/emrg", help="owner/name")
    parser.add_argument("--base", default="origin/master", help="the ref to merge onto")
    args = parser.parse_args(argv)

    try:
        _refresh_base(args.base)
        # The header must name the ref actually measured, not the spelling that
        # was typed: those differ whenever a short name is ambiguous, and a header
        # that reports `origin/master` for a commit that is not master is how the
        # wrong-tree defect stays invisible (`cyc20260913-072845`).
        base_ref = _qualify_ref(args.base)
        base = _rev_parse(base_ref)
        # One plan source, not two: an explicit list is used verbatim; otherwise the
        # default plan is computed (and says what it left out).
        note: str | None = None
        if args.prs:
            numbers = args.prs
        else:
            numbers, note = _plan_from_open_prs(args.repo, base, args.all)
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    print(f"base {base[:8]} ({base_ref})")
    if note is not None:
        # Always printed when the plan was chosen for the reader: which PRs it
        # considered, and which it left out, is part of the answer.
        print(note)
    print(f"plan: {' -> '.join('#' + str(n) for n in numbers)}")

    dangers: list[int] = []
    conflicts: list[int] = []
    measured = 0
    with tempfile.TemporaryDirectory(prefix="emrg-merge-seq-") as tmp:
        workdir = Path(tmp) / "tree"
        current = base
        for number in numbers:
            try:
                head = _fetch_head(number)
                merged = _merge_commit(current, head)
            except MeasurementError as exc:
                print(f"  #{number}: could not measure: {exc}", file=sys.stderr)
                return 2
            if merged is None:
                # The plan cannot proceed past this step. Say so, and stop: the
                # remaining steps would be measured against a tree that cannot
                # exist, which is how a plan check turns into fiction.
                print(f"  #{number}: CONFLICT - no tree produced, plan stops here")
                conflicts.append(number)
                break
            try:
                ok, report = _guard_verdict(
                    _run(["git", "rev-parse", f"{merged}^{{tree}}"]).stdout.strip(),
                    workdir,
                )
            except MeasurementError as exc:
                print(f"  #{number}: could not measure: {exc}", file=sys.stderr)
                return 2
            if ok:
                print(f"  #{number}: OK - {report}")
            else:
                print(f"  #{number}: DANGER - clean merge, but the tree FAILS: {report}")
                dangers.append(number)
            current = merged
            measured += 1

    print()
    if dangers:
        print(
            f"DANGEROUS STEPS: {dangers}\n"
            "git reported these merges CLEAN and no CI run covers them: the tree "
            "they produce fails the repo's own guards. Re-order the plan, or "
            "re-measure the derived value on the merged tree before pushing."
        )
        return 1
    if conflicts:
        # A break leaves `measured < len(numbers)`, so the sizes are the honest
        # report: the conflicting step produced no tree either, so it counts as
        # unjudged alongside the steps behind it.
        print(
            f"plan stopped at conflicting step(s): {conflicts} - not a health verdict\n"
            f"{measured} of {len(numbers)} step(s) were measured; the remaining "
            f"{len(numbers) - measured} were not judged, so nothing here says the "
            f"plan is safe. Resolve the conflict (which re-plans it with a new head) "
            f"and run this again. Not exit 0: 0 means every step was measured and "
            f"healthy."
        )
        return 3
    print(f"all {len(numbers)} step(s) landed trees that pass the guards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
