#!/usr/bin/env python3
"""Show the change merging a PR lands, and what its diff-to-master is *not*.

The class this exists for
-------------------------
The gates in this family ask, in the order a queue is drained: do the votes still
apply (`check-vote-count.py`), can a merge here reach master (`check-pr-base.py`),
is the green verdict about the tree that merges (`check-merge-freshness.py`),
whom would this dirty (`check-merge-order.py`), does the landed tree pass the
guards (`check-merge-tree-health.py`), does a whole plan's tree pass the suite
(`check-merge-plan-suite.py`).

None of them answers the question a review *starts* with: **what change does
merging this PR make?** The obvious substitute - `git diff master <head>` - is not
that change. It is the landing change **plus** every commit master gained after
the branch point, with the branch's older copy of those lines shown as a deletion.
Where the two are mixed, the diff reads as a PR that undoes work it never touched,
and reviewing a diff that is not the change is how a reviewer reaches the wrong
conclusion about a correct PR.

Measured instance (cycle `cyc20260913-203027`, while reviewing #1190)
--------------------------------------------------------------------
#1190's head sat one commit behind master: #1189 had rewritten
`scripts/classify-conflict.py` after #1190's branch point.

    git diff master 967a57a            -> 5 paths
    landing (merge-tree 9b755ec 967a57a = a290a46c63e8) -> 2 paths

The other three - `scripts/classify-conflict.py`, `tests/test_classify_conflict.py`
and `Agent.md` - are #1189's own additions, and the two-tree diff prints them as
276 removed lines: it reads as "this PR reverts the mid-line conflict classifier".
It does not. In the landed tree those three blobs are byte-identical to master's,
which is the fact that decided the review, and it is printed by this tool instead
of having to be reconstructed by hand.

Frequency, measured in this clone over the 24 PR-head refs still present
(`refs/drain/*`, `refs/cdrain/*`, `refs/tmp/*`): **24 of 24** read backwards on at
least one path, 1001 paths in total. Those heads are old, so the count overstates
a typical queue - but a head at 2/3 routinely waits while other PRs land, and the
condition is simply "the base moved", which is the normal state of a queued PR.

Why the landing tree and not a model
------------------------------------
The landing tree is `git merge-tree --write-tree <base> <head>` - the same fold the
sibling tools use - materialised as a commit (identity and date pinned, for the
reason `check-merge-plan-suite.py` records) so that both diffs are
commit-to-commit. Nothing is inferred from the two sides: the printed change is
`git diff` of a tree the merge actually produces.

Exit codes
----------
    0  nothing reads backwards: every path `diff(base, head)` lists is a path the
       landing changes
    1  at least one path reads backwards, and it is named. This is a *reading*
       hazard, not a defect in the PR and not a blocker - the change the PR really
       lands is printed above it
    2  the question could not be answered (git/gh failure): fail loud, never report
       health that was not measured
    3  the merge conflicts, so there is no landing tree to diff. Not a verdict:
       that question belongs to `check-merge-sequence.py`
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

# Pinned, not read from the clock: a commit's sha contains its committer date, so
# an unpinned synthetic commit is not a function of its inputs. The same constant
# and the same reasoning as the two folds in this family, so they cannot drift.
PLAN_COMMIT_DATE = "2000-01-01T00:00:00 +0000"


class MeasurementError(Exception):
    """The question could not be answered. Never a verdict."""


class Conflict(MeasurementError):
    """The merge conflicts, so there is no landing tree - not a verdict either.

    A subclass rather than a message test: "no landing tree" has its own exit code
    (3) and its own owner (`check-merge-sequence.py`), and a caller that had to
    pattern-match prose to tell it from a measurement failure is how the two get
    conflated.
    """


def _run(
    argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command with the decoding pinned.

    `encoding`/`errors` are pinned for the reason recorded in `check-doc-count.py`:
    a locale mismatch leaves `stdout` as `None` after the reader thread swallows
    the decode error, and that `None` surfaces later as a bare `TypeError` past
    every handler.
    """
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _rev_parse(ref: str) -> str:
    """Resolve a ref to a commit SHA.

    Every ref that reaches `merge-tree` goes through here first: a *name* is
    mutable in a way a SHA is not (`fetch` rewrites `FETCH_HEAD`, and a local
    branch of the same name shadows `origin/master`), and this family has already
    answered about the wrong tree because of it.
    """
    proc = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"])
    if proc.returncode != 0:
        raise MeasurementError(
            f"could not resolve {ref!r} to a commit: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _qualify_ref(ref: str) -> str:
    """The full name a short ref resolves to, so the header names what was measured.

    A header that reports `origin/master` for a commit that is not master is how the
    wrong-tree defect stays invisible: the spelling typed and the ref measured differ
    whenever a short name is ambiguous (a local `origin/master` branch shadows
    `refs/remotes/origin/master`). A SHA has no symbolic name, so it is returned as is.
    """
    proc = _run(["git", "rev-parse", "--symbolic-full-name", ref])
    name = proc.stdout.strip()
    return name if proc.returncode == 0 and name else ref


def _open_pr_numbers(repo: str) -> list[int]:
    """The open PR numbers, ascending - the default subject of the check."""
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
        raise MeasurementError(f"gh pr list failed: {proc.stderr.strip()}")
    numbers = [int(line) for line in proc.stdout.split() if line.strip()]
    if not numbers:
        raise MeasurementError("no open PRs reported - nothing to check")
    return sorted(numbers)


def _fetch_head(number: int) -> str:
    """Fetch a PR's real head into a temp ref and return its SHA.

    The refspec is forced (`+`): PR heads here are routinely re-pushed to a commit
    that is not a descendant of the previous one (every conflict resolution does),
    and a rejected fetch would leave the *stale* ref in place, so the reading would
    be about a head that is no longer the PR.
    """
    ref = f"refs/emrg-landing-diff/pr{number}"
    proc = _run(["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise MeasurementError(f"could not fetch PR #{number}: {detail}")
    return _rev_parse(ref)


def _is_object_name(line: str) -> bool:
    """Whether a line is a bare object name (the merged tree's).

    Both object formats are accepted: the question is the *shape* of the answer,
    which must not depend on the object format of whichever clone runs this.
    """
    return bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", line))


def _diagnosis(proc: subprocess.CompletedProcess[str]) -> str:
    """What git said, from both streams - a failure must not report itself as empty."""
    detail = (proc.stdout[-500:] + proc.stderr[-500:]).strip()
    return detail or f"no output (exit {proc.returncode})"


def _merge_tree(base: str, head: str) -> str | None:
    """The tree landing `head` on `base` produces, or None when it conflicts.

    The exit code is not the signal, the output is (the same measurement
    `check-merge-plan-suite.py` records): a genuine conflict exits 1 *and* names
    the merged tree on the first line, while a failure to merge the two *inputs*
    exits 1 with empty stdout - reading only the code would report "conflict" for
    an unanswered question.
    """
    proc = _run(["git", "merge-tree", "--write-tree", base, head])
    lines = proc.stdout.splitlines()
    first = lines[0].strip() if lines else ""
    if not _is_object_name(first):
        raise MeasurementError(
            "merge-tree did not name a merged tree: " + _diagnosis(proc)
        )
    return first if proc.returncode == 0 else None


def _commit_env() -> dict[str, str]:
    """Author/committer for the synthetic landing commit, independent of git config.

    Measured in this family (`cyc20260913-200715`): with no ambient identity and
    `user.useConfigOnly = true`, `git commit-tree` refuses and the tool reports a
    *false* "could not measure" about a question the machine's git config has no
    bearing on. The date is pinned for the reason in `PLAN_COMMIT_DATE`.
    """
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "emrg-landing-diff",
        "GIT_AUTHOR_EMAIL": "landing-diff@emrg.invalid",
        "GIT_COMMITTER_NAME": "emrg-landing-diff",
        "GIT_COMMITTER_EMAIL": "landing-diff@emrg.invalid",
        "GIT_AUTHOR_DATE": PLAN_COMMIT_DATE,
        "GIT_COMMITTER_DATE": PLAN_COMMIT_DATE,
    }


def _commit_tree(tree: str, parents: list[str], message: str) -> str:
    """Create a commit for a merged tree, so both diffs are commit-to-commit."""
    argv = ["git", "commit-tree", tree]
    for parent in parents:
        argv += ["-p", parent]
    argv += ["-m", message]
    proc = _run(argv, env=_commit_env())
    if proc.returncode != 0:
        raise MeasurementError(f"commit-tree failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _changed_paths(a: str, b: str) -> list[tuple[str, str]]:
    """`git diff --name-status a b`, as (status, path) pairs, in git's order."""
    proc = _run(["git", "diff", "--name-status", "--no-renames", a, b])
    if proc.returncode != 0:
        raise MeasurementError(
            f"could not diff {a[:8]}..{b[:8]}: {_diagnosis(proc)}"
        )
    changed: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) >= 2:
            changed.append((fields[0], fields[-1]))
    return changed


def _behind_by(base: str, head: str) -> int:
    """How many commits of `base` the head does not contain - the reason for all this."""
    proc = _run(["git", "rev-list", "--count", f"{head}..{base}"])
    if proc.returncode != 0:
        raise MeasurementError(
            f"could not count {head[:8]}..{base[:8]}: {_diagnosis(proc)}"
        )
    try:
        return int(proc.stdout.strip() or "0")
    except ValueError as exc:
        raise MeasurementError(
            f"rev-list --count said {proc.stdout.strip()!r}"
        ) from exc


def landing_reading(
    base: str, head: str
) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """The measurement, without the report.

    Returns `(landing_tree, landed, apparent, backwards)`:

    * `landed`    - what merging the head on the base changes (the reviewable change)
    * `apparent`  - what `diff(base, head)` lists (the reading to distrust)
    * `backwards` - paths in `apparent` that the landing does not change, i.e. the
      base's own later commits, shown there as reversals this PR does not make

    Raises `Conflict` when the merge conflicts (no landing tree to diff) and
    `MeasurementError` when git failed: neither is a reading, and neither is a
    verdict.
    """
    # Resolved here, not only by the caller: a *name* is mutable (`fetch` rewrites
    # `FETCH_HEAD`; a local branch shadows `origin/master`), so no entry point is
    # allowed to hand one to `merge-tree`.
    base = _rev_parse(base)
    head = _rev_parse(head)
    tree = _merge_tree(base, head)
    if tree is None:
        raise Conflict(
            "the merge conflicts, so there is no landing tree to diff "
            "(that question belongs to check-merge-sequence.py)"
        )
    landing = _commit_tree(tree, [base, head], f"landing {head[:8]} on {base[:8]}")
    landed = _changed_paths(base, landing)
    apparent = _changed_paths(base, head)
    landed_paths = {path for _, path in landed}
    backwards = [(status, path) for status, path in apparent if path not in landed_paths]
    return tree, landed, apparent, backwards


def check_pr(number: int, base: str) -> tuple[str, str]:
    """One PR's landing change, plus the paths of `diff(base, head)` it does not land.

    Returns `(state, report)` with state in `{"clean", "backwards", "conflict"}`.
    """
    head = _fetch_head(number)
    try:
        tree, landed, apparent, backwards = landing_reading(base, head)
    except Conflict as exc:
        return "conflict", f"  #{number}: {exc}"
    behind = _behind_by(base, head)

    lines = [
        f"  #{number} landing tree {tree[:12]} - merging it changes {len(landed)} "
        f"path(s) on the base:"
    ]
    lines += [f"    {status}\t{path}" for status, path in landed]
    if not landed:
        lines.append("    (nothing: this head adds no change to the base)")
    if backwards:
        lines.append(
            f"  reads backwards: {len(backwards)} of the {len(apparent)} path(s) in "
            f"diff(base, head) are the base's own later changes ({behind} commit(s) "
            f"the head does not contain), shown there as reversals this PR does not make:"
        )
        lines += [f"    {status}\t{path}" for status, path in backwards]
        return "backwards", "\n".join(lines)
    lines.append(
        f"  diff(base, head) lists {len(apparent)} path(s), and every one of them is "
        f"landed ({behind} commit(s) of the base not in the head)"
    )
    return "clean", "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Show the change merging a PR lands, next to the diff-to-master reading "
            "that is not that change."
        )
    )
    parser.add_argument(
        "prs", nargs="*", type=int, help="PR numbers (default: all open, ascending)"
    )
    parser.add_argument("--repo", default="argszero/emrg", help="owner/name")
    parser.add_argument("--base", default="origin/master", help="the ref to land on")
    args = parser.parse_args(argv)

    try:
        base = _rev_parse(args.base)
        numbers = args.prs or _open_pr_numbers(args.repo)
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    print(f"base {base[:8]} ({_qualify_ref(args.base)}), {len(numbers)} PR(s) checked")
    states: list[tuple[int, str]] = []
    for number in numbers:
        try:
            state, report = check_pr(number, base)
        except MeasurementError as exc:
            print(f"  #{number}: could not measure: {exc}", file=sys.stderr)
            return 2
        print(report)
        states.append((number, state))

    conflicts = [number for number, state in states if state == "conflict"]
    backwards = [number for number, state in states if state == "backwards"]
    if conflicts:
        print(
            "\nNo landing tree for "
            + ", ".join(f"#{number}" for number in conflicts)
            + ": the merge conflicts, so this tool has nothing to diff. That is not a "
            "health verdict - the sequence question is check-merge-sequence.py's.",
            file=sys.stderr,
        )
        return 3
    if backwards:
        print(
            "\n"
            + ", ".join(f"#{number}" for number in backwards)
            + " reads backwards: reading diff(master, head) as the change this PR lands "
            "would review work the PR never touched. The landing change is printed above "
            "it and is what merges. This is a reading hazard, not a defect in the PR.",
            file=sys.stderr,
        )
        return 1
    print("\nno path reads backwards: diff(master, head) is the change this lands")
    return 0


if __name__ == "__main__":
    sys.exit(main())
