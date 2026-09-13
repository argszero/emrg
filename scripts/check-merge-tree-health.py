#!/usr/bin/env python3
"""Check that *merging a PR* produces a tree that passes the repo's own guards.

The class this exists for
------------------------
This repo's merge gates all answer a question *about a PR*:

* `check-vote-count.py`  - do enough votes still apply to this head?
* `check-pr-base.py`     - can a merge here still reach master?
* `check-merge-freshness.py` - is the green verdict about the tree that merges?
* `check-merge-order.py` - which other PRs would a merge here dirty?

None of them answers the question that decides whether master is healthy one
minute after the merge: **does the tree produced by merging this PR pass the
guards the repo enforces on master?** That question is not about the branch - a
branch is routinely self-consistent - it is about the *union* of the branch and
master, and it can be answered only by building that union and asking it.

Measured 2026-09-12 (`cyc20260912-040220`), while draining a queue of eleven
green PRs. #1133 and #1140 each added tests and each rewrote Agent.md's
documented Python count to the value true *for its own branch*:

    #1133 (own tree)  : documents 1500   collects 1500   consistent
    #1140 (own tree)  : documents 1500   collects 1500   consistent
    merged 1133+1140  : documents 1500   collects 1506   FAIL

Both sides set the count line to the same number, so git merged it with *no
conflict* and kept one copy. The conflict-free merge is the dangerous one: a
conflict forces someone to look, while a clean merge of the same line looks like
nothing happened. `check-merge-order.py` reports that pair as conflicting with
the fewest others, so "cheapest first" actively recommends the merge that lands
an inconsistent tree.

This is the same failure #1137 suffered (see `check-merge-freshness.py`), reached
by a different route: not a stale CI verdict, but a pair of individually correct
counts that are both wrong together.

Why the guard is run instead of modelled
----------------------------------------
The count cannot be predicted from the two sides. It is not `max(a, b)` and not
`a + b`: the count is whatever the merged tree collects, and collection depends
on imports, conftest and parametrisation. So the merged tree is built (in a
scratch dir; the working tree is never touched) and the tree's **own**
`scripts/check-doc-count.py` is run there - the same guard CI runs, at the same
path, reading its own tree (which it names, since #1140). Modelling the guard
would only move the guess one level up.

The uncommitted-repair trap
---------------------------
Measured the same cycle, and the reason the first attempt at #1140 failed CI:
the guard prints `Fix with: ... --write`, and `--write` edits the file **in the
tree the tool resolved**. Running it and then committing *the merge* rather than
*the merge plus the edit* pushes a head that still documents the old count, and
CI's own guard then fails on the pushed commit. This tool reports the tree
verdict for the *committed* head it was asked about, and says so, because the
pre-push question is "does the commit I am about to merge pass?" - not "does my
working tree pass?".

Usage
-----
    uv run --no-sync python3 scripts/check-merge-tree-health.py [PR ...]
    uv run --no-sync python3 scripts/check-merge-tree-health.py --base <ref>

With no PR numbers, every open PR is checked. Heads come from
`refs/pull/<N>/head`, so the check is about each PR's real head rather than a
local branch of a similar name.

Exit codes
----------
    0  every clean merge produced a tree that passes the repo's guards
    1  at least one clean merge produced a tree that FAILS them (the finding)
    2  the question could not be answered (gh/git/guard failure) - fail loud,
       never report health that was not measured

Conflicting PRs are reported as CONFLICT and are not a failure of this check:
they cannot be merged as they stand, so there is no merged tree to judge. That
question belongs to `check-merge-order.py`.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# The guard is judged by its exit code, but its own report line is what names the
# numbers, so it is captured and quoted rather than re-derived.
GUARD = "scripts/check-doc-count.py"
COUNT_IN_REPORT = re.compile(r"documents (\d+).*?but (\d+) are collected")
OK_IN_REPORT = re.compile(r"OK: .*?documents (\d+)")


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


def _rev_parse(ref: str) -> str:
    """Resolve a ref to a commit SHA.

    Every ref that reaches `merge-tree` goes through here first. A *name* is
    mutable in a way a SHA is not - `fetch` rewrites `FETCH_HEAD`, so passing the
    name would answer about whatever the last fetch happened to leave behind.
    That defect was measured in `check-merge-order.py`, which is why the rule is
    asserted rather than remembered.
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
        raise MeasurementError("no open PRs reported - nothing to check")
    return sorted(numbers)


def _fetch_head(number: int) -> str:
    """Fetch a PR's real head into a temp ref and return the ref name.

    The refspec is forced (`+`): a PR head is routinely re-pushed to a commit
    that is not a descendant of the previous one (every conflict resolution in
    this repo does), and a rejected fetch leaves the *stale* ref in place, so the
    check would silently answer about a tree that is no longer the PR.
    """
    ref = f"refs/emrg-tree-health/pr{number}"
    proc = _run(["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise MeasurementError(f"could not fetch PR #{number}: {detail}")
    return ref


def _merge_tree_paths(a: str, b: str, cwd: str | None = None) -> list[str] | None:
    """Conflicted paths when `a` and `b` are merged; None if unmeasurable.

    An empty list means the merge is clean, which is distinct from None (the
    question was not answered). Anything other than rc 0/1 is treated as a
    failure to measure, never as a conflict.
    """
    proc = _run(["git", "merge-tree", "--write-tree", a, b], cwd=cwd)
    if proc.returncode == 0:
        return []
    if proc.returncode != 1:
        return None
    return [
        line.split("\t", 1)[1]
        for line in proc.stdout.splitlines()
        if "\t" in line
    ]


def _merged_tree_sha(a: str, b: str, cwd: str | None = None) -> str:
    """The tree sha of the clean merge of `a` and `b`."""
    proc = _run(["git", "merge-tree", "--write-tree", a, b], cwd=cwd)
    if proc.returncode != 0:
        raise MeasurementError(
            "merge-tree did not produce a tree for a merge reported clean: "
            + (proc.stdout[-500:] + proc.stderr[-500:]).strip()
        )
    return proc.stdout.strip().splitlines()[0].strip()


def _git_cwd() -> str | None:
    """The repository to read objects from.

    `git archive <tree>` is answered by the repository the process is standing
    in, and the canonical invocation is from the checkout root, so the default is
    simply the cwd. It is resolved explicitly so the extraction cannot be
    answered by a different repository when the tool is invoked from elsewhere -
    the whole family of tools here has been bitten by "which tree answered?".
    """
    proc = _run(["git", "rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _guard_verdict(tree_sha: str, workdir: Path, cwd: str | None = None) -> tuple[bool, str]:
    """Run the extracted tree's own guard on the extracted tree.

    Returns (passed, one-line report). A guard that cannot be run at all (no
    script in the tree, no interpreter able to collect) is a measurement error,
    not a pass - the failure direction matters, because "I could not check"
    reported as healthy is how a broken tree reaches master.
    """
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    archive = subprocess.Popen(
        ["git", "archive", tree_sha],
        stdout=subprocess.PIPE,
        cwd=cwd,
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
        detail = f"documents {m.group(1)}" if m else "guard reported OK"
        return True, f"guard OK ({detail})"
    if proc.returncode == 1:
        m = COUNT_IN_REPORT.search(out)
        detail = (
            f"documents {m.group(1)} but {m.group(2)} are collected"
            if m
            else out.strip().splitlines()[-1] if out.strip() else "guard FAILED"
        )
        return False, f"guard FAIL ({detail})"
    # Exit 2 from the guard means it could not measure the tree itself. Quoting
    # it is not enough to call the tree healthy.
    raise MeasurementError(
        f"the merged tree's guard could not run (rc={proc.returncode}):\n"
        + out[-1000:].strip()
    )


def check_pr(
    number: int, base: str, workdir: Path, cwd: str | None = None
) -> tuple[str, str]:
    """Verdict for one PR: (state, report).

    state is one of 'healthy', 'unhealthy', 'conflict'.

    A conflict is not a health verdict: the merge cannot be made as it stands, so
    there is no merged tree to judge. Reporting it as unhealthy would be a
    verdict about a tree that does not exist.
    """
    head = _rev_parse(_fetch_head(number))
    paths = _merge_tree_paths(base, head, cwd=cwd)
    if paths is None:
        raise MeasurementError(f"merge-tree failed for PR #{number}")
    if paths:
        return "conflict", f"conflicts on {', '.join(sorted(set(paths)))}"
    tree = _merged_tree_sha(base, head, cwd=cwd)
    passed, report = _guard_verdict(tree, workdir, cwd=cwd)
    return ("healthy" if passed else "unhealthy"), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check that merging a PR produces a tree that passes the repo's guards."
        )
    )
    parser.add_argument("prs", nargs="*", type=int, help="PR numbers (default: all open)")
    parser.add_argument("--repo", default="argszero/emrg", help="owner/name")
    parser.add_argument("--base", default="origin/master", help="the ref to merge onto")
    args = parser.parse_args(argv)

    try:
        base = _rev_parse(args.base)
        numbers = args.prs or _open_pr_numbers(args.repo)
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    # The tree to read objects from: the checkout the caller is standing in. Said
    # out loud, because a tool answering "is this merge healthy" must not leave
    # "which repository answered" ambiguous - the defect the sibling tools here
    # were built to remove.
    cwd = _git_cwd()
    print(f"repo: {cwd}")
    print(f"base {base[:8]}, {len(numbers)} PR(s) checked against {args.base}")

    healthy: list[int] = []
    unhealthy: list[int] = []
    conflicts: list[int] = []
    with tempfile.TemporaryDirectory(prefix="emrg-tree-health-") as tmp:
        workdir = Path(tmp) / "tree"
        for number in numbers:
            try:
                state, report = check_pr(number, base, workdir, cwd=cwd)
            except MeasurementError as exc:
                print(f"  #{number}: could not measure: {exc}", file=sys.stderr)
                return 2
            print(f"  #{number}: {state.upper()} - {report}")
            if state == "healthy":
                healthy.append(number)
            elif state == "unhealthy":
                unhealthy.append(number)
            else:
                conflicts.append(number)

    print(
        f"\nclean+healthy: {healthy}\n"
        f"clean but FAILS the tree's guards: {unhealthy}\n"
        f"conflicts (not judged here): {conflicts}"
    )
    if unhealthy:
        print(
            "\nAn unhealthy entry merges without conflict but lands a tree its own "
            "guards reject - the count line was rewritten on both sides. Resolve it "
            "on the merged tree (measure, never pick a side), commit that edit, and "
            "push: pushing voids the votes, so the repair is not free."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
