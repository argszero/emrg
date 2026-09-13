#!/usr/bin/env python3
"""Run the repository's own test suite on the tree a *plan* would land.

The question this exists for
----------------------------
The other gates in this family answer a question about *one* PR, or about *one*
merge: are the votes current, can the base reach master, is the CI verdict fresh,
which other PRs would this dirty, does merging this PR alone produce a tree the
guards accept. None of them answers the question that decides whether master is
healthy a minute after a *plan* lands: **does the tree those PRs produce together
pass the repository's own tests?**

Per-PR CI cannot answer it either, and not by accident: a `pull_request` run
builds `Merge <head> into <merge-base>`, so a PR's CI contains master and that PR
and nothing else. A rule that arrives in one PR and the code it rejects in
another are invisible to every per-PR signal - each side is green, and the tree
that reaches master is red.

Measured instance (cycle cyc20260913-154837)
--------------------------------------------
Seven open PRs, each `MERGEABLE`, each CI double-green. Every ordered pair merged
cleanly (`git merge-tree --write-tree --merge-base=...`: 21/21 clean), and the
doc-count guard accepted every step of the resulting plan. The tree the seven
produce together, however, failed the suite:

    1 failed, 1738 passed
    FAILED tests/test_script_decode_is_locale_independent.py::test_every_text_mode_subprocess_pins_its_encoding
       tests/test_check_merge_sequence.py:318 subprocess.run(..., text=True) has no encoding= ...

The guard shipped in #1136 rejects three calls shipped in #1172. Neither PR's CI
can see the other's files. That is why this check takes *several* PRs, judges the
tree they produce together, and runs the suite rather than one guard script: one
guard script is exactly what was green while the tree was red.

Why a worktree and not `git archive`
------------------------------------
The tree is materialised with `git worktree add --detach`, never extracted from an
archive. Measured: an archive has no `.git`, this repo's tests resolve paths
through git, and an archive harness therefore reported 8 failures on *master's own
tree* as well - identical on every input, a device measuring itself instead of the
plan. A real worktree distinguishes the two states (control 1600 passed, planned
tree 1643 passed, same harness).

Why the interpreter that is running this script
-----------------------------------------------
A freshly added worktree has no populated `.venv` - `uv run` inside one creates an
empty environment, measured repeatedly in this repo - so the suite is run with
`sys.executable`, i.e. the interpreter running this tool (under
`uv run --no-sync`, the project environment), with the worktree as cwd.

Usage
-----
    uv run --no-sync python3 scripts/check-merge-plan-suite.py 1136 1152 1185
    uv run --no-sync python3 scripts/check-merge-plan-suite.py      # all open, ascending
    uv run --no-sync python3 scripts/check-merge-plan-suite.py --steps 1187 1188

`--steps` judges every intermediate tree instead of only the final one, which is
the other half of the same question and the open half of issue #1161: a plan whose
last PR fixes what an earlier PR broke is green at the end and red on the way, and
each of those in-between trees is master's tree for a while when the plan is landed
one PR at a time. It costs one suite run per step, hence opt-in.

Exit codes
----------
    0  the plan's final tree was built and its suite passed (with `--steps`: every
       step's tree passed)
    1  the plan's final tree was built and its suite FAILED - the finding (with
       `--steps`: at least one step's tree failed, and the step is named)
    2  the question could not be answered (git/gh failure, or the suite could not
       be run at all): fail loud, never report health that was not measured
    3  the plan has no final tree - a step conflicts. Not a health verdict: there
       is no tree to judge. That question belongs to `check-merge-sequence.py`.

The plan and the tree that was measured are named in the output. "Which tree
answered?" is the defect this family exists to remove.

What a worktree run is not
--------------------------
The suite runs in a worktree of the tree under test, so it runs the suite a *fresh
clone* of that tree would run: the tree's **tracked** files and nothing else. One
test is environment-dependent, and it is skipped here but passes in a developer's
checkout - `tests/test_check_node_test_count.py` skips itself with "no node_modules
... cannot ask the runners", since `node_modules/` is untracked. Measured
(`cyc20260913-203027`, master `947377b`): a worktree of that tree reported
`1761 passed, 2 skipped` while the same tree in a populated checkout reported
`1771 passed, 1 skipped` after nine new tests - the two extra numbers are this skip
and those tests, not a difference in the trees. Compare worktree runs with worktree
runs, and never read a skip/pass delta between the two harnesses as a regression.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TIP_REF = "refs/emrg-plan-suite/tip"
SUITE = ["-m", "pytest", "tests/", "-q", "--no-header"]


class MeasurementError(Exception):
    """The question could not be answered. Never a verdict."""


class PlanConflict(Exception):
    """A step conflicts, so the plan has no final tree. Never a verdict."""

    def __init__(self, step: int, number: int, paths: list[str]) -> None:
        super().__init__(
            f"step {step} (#{number}) conflicts on {', '.join(sorted(set(paths)))}"
        )
        self.step = step
        self.number = number
        self.paths = paths


def _run(
    argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command with the decoding pinned.

    `encoding`/`errors` are pinned for the reason recorded in
    `check-doc-count.py`: a locale mismatch leaves `stdout` as `None` after the
    reader thread swallows the decode error, and the `None` surfaces later as a
    bare `TypeError` past every handler.
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


# The date carried by every synthetic plan commit. Pinned, not read from the
# clock: a fold must be a function of its inputs, and a commit's sha includes its
# committer date, so an unpinned fold produced a *different* sha for the *same*
# plan whenever two folds straddled a second boundary. Measured
# (cyc20260913-194108, Windows CI run 34754517824 on #1190): the test comparing
# `build_plan_tip` against the last step tree failed on exactly that - the two
# folds differed and nothing was wrong with either tree. It also makes a `--steps`
# tree sha comparable between runs, which is the point of printing one.
PLAN_COMMIT_DATE = "2000-01-01T00:00:00 +0000"


def _commit_env() -> dict[str, str]:
    """Author/committer for the synthetic plan commits, independent of git config.

    Identity *and* date are pinned, so the same plan folds to the same commits on
    every machine and at every speed (see `PLAN_COMMIT_DATE`).
    """
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "emrg-plan-suite",
        "GIT_AUTHOR_EMAIL": "plan-suite@emrg.invalid",
        "GIT_COMMITTER_NAME": "emrg-plan-suite",
        "GIT_COMMITTER_EMAIL": "plan-suite@emrg.invalid",
        "GIT_AUTHOR_DATE": PLAN_COMMIT_DATE,
        "GIT_COMMITTER_DATE": PLAN_COMMIT_DATE,
    }


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
        raise MeasurementError("no open PRs reported - nothing to judge")
    return sorted(numbers)


def _fetch_head(number: int) -> str:
    """Fetch a PR's real head into a temp ref and return the ref name.

    The refspec is forced (`+`): PR heads here are routinely re-pushed to a commit
    that is not a descendant of the previous one (every conflict resolution does),
    and a rejected fetch would leave the *stale* ref in place, so the plan would
    silently be built from a tree that is no longer the PR.
    """
    ref = f"refs/emrg-plan-suite/pr{number}"
    proc = _run(["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise MeasurementError(f"could not fetch PR #{number}: {detail}")
    return _rev_parse(ref)


def _merge_tree(ours: str, theirs: str) -> tuple[str | None, list[str]]:
    """The tree of the clean merge of two commits, or the conflicted paths.

    **The exit code is not the signal; the output is.** Measured (`git merge-tree
    --write-tree`, this repo): a genuine conflict exits 1 and prints the merged
    tree's OID on the first line of stdout, but so does a failure to merge the two
    *inputs* - an unknown ref, or an object that dereferences to a blob - which
    exits 1 with **empty stdout** and a diagnostic on stderr. Reading only the exit
    code therefore reports "I could not merge these two inputs" as a plan conflict,
    with an empty path list, and sends the caller off to resolve a conflict that
    does not exist. A clean merge always prints its tree OID too, so an empty stdout
    is never an answer, whatever the code says.
    """
    proc = _run(["git", "merge-tree", "--write-tree", ours, theirs])
    lines = proc.stdout.splitlines()
    if proc.returncode == 0:
        if not lines or not _is_object_name(lines[0].strip()):
            raise MeasurementError(
                "merge-tree reported success without naming the merged tree: "
                + _diagnosis(proc)
            )
        return lines[0].strip(), []
    if proc.returncode != 1:
        raise MeasurementError("merge-tree failed: " + _diagnosis(proc))
    if not lines or not _is_object_name(lines[0].strip()):
        raise MeasurementError(
            "merge-tree exited 1 without naming a merged tree (a failure to merge "
            "the inputs, not a conflict): " + _diagnosis(proc)
        )
    return None, [
        line.split("\t", 1)[1] for line in lines if "\t" in line
    ]


def _is_object_name(line: str) -> bool:
    """Whether a line is a bare object name (the merged tree's).

    Both the SHA-1 (40 hex) and SHA-256 (64 hex) object formats are accepted: the
    question is the *shape* of the answer, which must not depend on the object
    format of whichever clone happens to run this.
    """
    return bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", line))


def _diagnosis(proc: subprocess.CompletedProcess[str]) -> str:
    """What git said, from both streams - a failure must not report itself as empty."""
    detail = (proc.stdout[-500:] + proc.stderr[-500:]).strip()
    return detail or f"no output (exit {proc.returncode})"


def _commit_tree(tree: str, parents: list[str], message: str) -> str:
    """Create a commit for a merged tree, so the next step can merge onto it."""
    argv = ["git", "commit-tree", tree]
    for parent in parents:
        argv += ["-p", parent]
    argv += ["-m", message]
    proc = _run(argv, env=_commit_env())
    if proc.returncode != 0:
        raise MeasurementError(f"commit-tree failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def build_plan_steps(base: str, heads: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """Fold the plan, keeping every intermediate commit: (step, PR, commit).

    The fold is the same shape as landing the plan with squash merges - each head
    is merged onto the tree the previous steps built - and it never touches the
    working tree or a branch, only the object store.

    The intermediate commits are *kept* rather than discarded (they used to be a
    local variable): a plan can produce a final tree that passes while a step on
    the way is red, and the second half of issue #1161 is precisely that no gate
    looked at the steps. Merging each head onto the accumulated commit is what
    makes the step commits the trees that would exist if the plan were landed one
    PR at a time, so they are the right thing to judge with `--steps`.
    """
    accumulated = base
    steps: list[tuple[int, int, str]] = []
    for step, (number, head) in enumerate(heads, start=1):
        tree, paths = _merge_tree(accumulated, head)
        if tree is None:
            raise PlanConflict(step, number, paths)
        accumulated = _commit_tree(
            tree, [accumulated, head], f"plan step {step}: #{number}"
        )
        steps.append((step, number, accumulated))
    return steps


def build_plan_tip(base: str, heads: list[tuple[int, str]]) -> str:
    """The final commit of the plan (the whole plan's tree, as one object)."""
    steps = build_plan_steps(base, heads)
    return steps[-1][2] if steps else base


def _suite_verdict(tip: str, scratch: Path) -> tuple[bool, str, str]:
    """Run the repository's suite in a worktree of the planned tree.

    Returns (passed, suite summary, tree sha). The tree sha is returned and
    reported because the family's recurring defect is a verdict about a tree the
    caller was not looking at.
    """
    tree_proc = _run(["git", "rev-parse", f"{tip}^{{tree}}"])
    if tree_proc.returncode != 0:
        raise MeasurementError(f"could not read the planned tree: {tree_proc.stderr}")
    tree_sha = tree_proc.stdout.strip()

    updated = _run(["git", "update-ref", TIP_REF, tip])
    if updated.returncode != 0:
        raise MeasurementError(f"could not mark the plan tip: {updated.stderr.strip()}")
    worktree = scratch / "tree"
    try:
        added = _run(["git", "worktree", "add", "--detach", str(worktree), TIP_REF])
        if added.returncode != 0:
            raise MeasurementError(
                "could not materialise the planned tree: " + added.stderr.strip()
            )
        if not (worktree / "tests").is_dir():
            raise MeasurementError("the planned tree has no tests/ directory")
        proc = _run([sys.executable, *SUITE], cwd=str(worktree))
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            summary = next(
                (line.strip() for line in reversed(out.splitlines()) if line.strip()),
                "suite passed",
            )
            return True, summary, tree_sha
        if proc.returncode == 1:
            failures = [
                line.split(" ", 1)[1].strip()
                for line in out.splitlines()
                if line.startswith("FAILED ")
            ]
            summary = (
                "; ".join(failures[:5])
                if failures
                else "suite FAILED (no per-test line in the output)"
            )
            return False, summary, tree_sha
        # 2 interrupted, 3 internal error, 4 usage error, 5 no tests collected, or
        # pytest missing entirely. None of these is "the suite passed", and a
        # missing pytest is the most likely way to get here by accident.
        raise MeasurementError(
            f"the suite could not be run (rc={proc.returncode}):\n" + out[-1000:].strip()
        )
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)])
        _run(["git", "update-ref", "-d", TIP_REF])


def _judge_every_step(base: str, heads: list[tuple[int, str]]) -> int:
    """Run the suite on each intermediate tree, not only on the final one.

    Issue #1161's open half: the tool judged the plan's *final* tree, so a rule
    that breaks only at an intermediate step was invisible to it. That gap has a
    shape, and it is not exotic - a plan whose last PR is the one that fixes what
    an earlier PR broke is *green at the end and red on the way*, and the trees in
    between are the ones a caller who lands the plan step by step will actually
    have on master, one at a time, with CI reporting green for each.

    Cost is one suite run per step (`~55s` here), which is why it is opt-in: the
    final tree remains the default question, since that is what decides whether
    master is healthy a minute after the whole plan lands.
    """
    try:
        steps = build_plan_steps(base, heads)
    except PlanConflict as exc:
        print(f"no final tree: {exc}", file=sys.stderr)
        print(
            "\nA step of the plan conflicts, so the plan has no final tree to judge. "
            "That is not a health verdict - resolve the conflict (and re-push) or "
            "reorder with check-merge-sequence.py.",
            file=sys.stderr,
        )
        return 3
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    red: list[tuple[int, int, str]] = []
    for step, number, commit in steps:
        try:
            with tempfile.TemporaryDirectory(prefix="emrg-plan-step-") as tmp:
                passed, summary, tree_sha = _suite_verdict(commit, Path(tmp))
        except MeasurementError as exc:
            print(f"could not measure step {step} (#{number}): {exc}", file=sys.stderr)
            return 2
        state = "OK" if passed else "FAILED"
        print(f"step {step} (#{number}) tree {tree_sha[:12]} suite {state}: {summary}")
        if not passed:
            red.append((step, number, summary))

    if not red:
        print(f"every step healthy ({len(steps)} suite run(s))")
        return 0
    detail = "; ".join(f"step {step} (#{number}): {summary}" for step, number, summary in red)
    print(f"suite FAILED at {len(red)} of {len(steps)} step(s): {detail}")
    print(
        "\nThe plan's final tree is not what this reports on: a step's tree is. Each "
        "of these would be master's tree for a while if the plan is landed in order, "
        "so fix it on the PR that owns the step (a push voids its votes). Landing the "
        "whole plan at once would land the final tree, which is judged by default.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the repository's test suite on the tree a plan of PRs would land."
        )
    )
    parser.add_argument(
        "prs", nargs="*", type=int, help="PR numbers in landing order (default: all open)"
    )
    parser.add_argument("--repo", default="argszero/emrg", help="owner/name")
    parser.add_argument(
        "--base", default="origin/master", help="the ref to plan onto"
    )
    parser.add_argument(
        "--steps",
        action="store_true",
        help=(
            "judge every intermediate tree of the plan, not only the final one "
            "(one suite run per step; issue #1161's open half)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        base = _rev_parse(args.base)
        numbers = args.prs or _open_pr_numbers(args.repo)
        heads = [(number, _fetch_head(number)) for number in numbers]
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    print(f"base {base[:8]} ({args.base}), {len(numbers)} PR(s) planned")

    try:
        tip = build_plan_tip(base, heads)
    except PlanConflict as exc:
        print(f"no final tree: {exc}", file=sys.stderr)
        print(
            "\nA step of the plan conflicts, so the plan has no final tree to judge. "
            "That is not a health verdict - resolve the conflict (and re-push) or "
            "reorder with check-merge-sequence.py.",
            file=sys.stderr,
        )
        return 3
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    print("plan: " + " -> ".join(f"#{number}" for number, _ in heads))
    if args.steps:
        return _judge_every_step(base, heads)
    try:
        with tempfile.TemporaryDirectory(prefix="emrg-plan-suite-") as tmp:
            passed, summary, tree_sha = _suite_verdict(tip, Path(tmp))
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    print(f"final tree {tree_sha[:12]} ({tree_sha})")
    if passed:
        print(f"suite OK: {summary}")
        return 0
    print(f"suite FAILED: {summary}")
    print(
        "\nThe plan's steps are individually clean and the per-PR signals are green, "
        "but the tree they produce together fails the suite. Fix it on the merged "
        "tree and re-push the PR that owns the failure (a push voids its votes).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
