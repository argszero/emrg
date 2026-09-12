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

    master + #1167                 -> CLEAN, documents 1541, collects 1541  ok
    master + #1166                 -> CLEAN, documents 1541, collects 1541  ok
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

    # plan every open PR, ascending (a default, not a recommendation)
    uv run --no-sync python3 scripts/check-merge-sequence.py

    # start from a ref other than master
    uv run --no-sync python3 scripts/check-merge-sequence.py --base origin/master 1153

Each step is reported as one of:

    OK        the merge is clean and the resulting tree passes the guards
    DANGER    the merge is clean but the resulting tree FAILS the guards
              (the silent case this tool exists for — git would not have told you)
    conflict  the merge conflicts, so no tree is produced and none is judged
              (not a failure: it is a question for a human / check-merge-order)

Exit codes
----------
    0  every clean step landed a tree that passes the guards
    1  at least one clean step landed a tree that FAILS them (the finding)
    2  the question could not be answered (git/gh/guard failure) - fail loud,
       never report health that was not measured
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
    mutable in a way a SHA is not - `fetch` rewrites `FETCH_HEAD`, so passing a
    name would answer about whatever the last fetch happened to leave behind.
    That defect was measured in `check-merge-order.py`, and it bit this cycle's
    own probe: a harness loop that re-fetched left `FETCH_HEAD` pointing at the
    wrong PR, so "master" was silently one of the subjects.
    """
    proc = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"])
    if proc.returncode != 0:
        raise MeasurementError(
            f"could not resolve {ref!r} to a commit: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _open_pr_numbers(repo: str) -> list[int]:
    """The open PR numbers, ascending - the default plan."""
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
        return True, f"documents {m.group(1)}" if m else "guard OK"
    if proc.returncode == 1:
        m = COUNT_IN_REPORT.search(out)
        detail = (
            f"documents {m.group(1)} but {m.group(2)} are collected"
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
    parser.add_argument("--repo", default="argszero/emrg", help="owner/name")
    parser.add_argument("--base", default="origin/master", help="the ref to merge onto")
    args = parser.parse_args(argv)

    try:
        base = _rev_parse(args.base)
        numbers = args.prs or _open_pr_numbers(args.repo)
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    print(f"base {base[:8]} ({args.base})")
    print(f"plan: {' -> '.join('#' + str(n) for n in numbers)}")

    dangers: list[int] = []
    conflicts: list[int] = []
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
        print(f"plan stopped at conflicting step(s): {conflicts} - not a health verdict")
        return 0
    print(f"all {len(numbers)} step(s) landed trees that pass the guards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
