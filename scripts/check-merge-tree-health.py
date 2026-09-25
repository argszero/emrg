#!/usr/bin/env python3
"""Check that *merging a PR* produces a tree the repository's own guard accepts.

The class this exists for
------------------------
This repo's merge gates all answer a question *about a PR*:

* `check-vote-count.py`  - do enough votes still apply to this head?
* `check-pr-base.py`     - can a merge here still reach master?
* `check-merge-freshness.py` - is the green verdict about the tree that merges?
* `check-merge-order.py` - which other PRs would a merge here dirty?

None of them answers the question that decides whether master is healthy one
minute after the merge: **does the tree produced by merging this PR pass the
repository's own guard (`scripts/check-doc-count.py`, `GUARD`)?** That question is
not about the branch - a branch is routinely self-consistent - it is about the
*union* of the branch and master, and it can be answered only by building that union
and asking it.

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

Which question this answers, and which it does not (measured 2026-09-25)
-----------------------------------------------------------------------
Two things a reader of a verdict needs, and this gate used to leave both to
inference (`cyc20260925-200139`):

* **the verdict is one guard's.** The summary used to call it "the tree's guards",
  and the unhealthy note asserted a cause - "the count line was rewritten on both
  sides" - that #1603 retired on this date: the count is measured now, never
  stored, so no clean union can produce that line. The note states what was
  measured (the tree's own copy of `GUARD` rejected it) and keeps the repair,
  which is still the only one: measure the merged tree, never pick a side, and
  remember that a push voids the votes.
* **the class it was built for is not this gate's to catch any more**, for the same
  reason: a rule every tracked file already satisfies cannot be broken by a union.
  The class itself is alive - measured the same day, `#1618` + `#1619` are each
  green while their union (`53780d3db66e`) fails
  `tests/test_a_tree_reading_guard_names_its_tree.py::test_every_guard_in_the_family_is_classified`,
  a rule whose subject is the *set* of `scripts/check*.py`, which no cheap per-PR
  gate can read. That question belongs to `check-merge-plan-suite.py` (the plan's
  final tree passes the suite; `--steps` judges every intermediate tree too, issue
  #1161), and the report prints the sibling tools' own sentence for it - one
  spelling of one fact - rather than leaving the reader to infer it from a label.

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

A `--base` written as a remote-tracking ref (either `origin/<branch>` or
`refs/remotes/origin/<branch>`) is **fetched before it is measured**, so the base
half of the question is as current as the heads half - without it a clean merge is
judged against a stale base and the tree it produces is not the tree that lands
(measured: 3 of the 3 clean merges over the head refs saved in this clone landed a
different tree). The base is then resolved by its **full name**, so a stray local
branch called `origin/master` cannot shadow the remote ref; a name denoting only a
local branch is refused rather than measured. A SHA, a local branch or a written
refspec is taken literally. See `_refresh_base` for the measurements.

Exit codes
----------
    0  every clean merge produced a tree that passes the repository's own guard,
       `scripts/check-doc-count.py` (`GUARD`)
    1  at least one clean merge produced a tree that FAILS it (the finding)
    2  the question could not be answered (gh/git/guard failure) - fail loud,
       never report health that was not measured

Conflicting PRs are reported as CONFLICT and are not a failure of this check:
they cannot be merged as they stand, so there is no merged tree to judge. That
question belongs to `check-merge-order.py`. The files such a refusal names are the
real ones - read from the report's stage block and decoded out of git's path
quoting, both of which are `merge_tree.py`'s now - so `conflicts on ...` names a
file that can be opened, and not, as it used to, a name no resolver has.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# The shared module's directory, so `import merge_tree` works however this file is
# loaded: as `python3 scripts/check-merge-tree-health.py` it is already `sys.path[0]`,
# but the test suite loads these tools by file path (`spec_from_file_location`), where
# it is not. `merge_tree.py` is a module of this repo, not a dependency.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_tree  # noqa: E402  (needs the path above)

# The guard is judged by its exit code, but its own report line is what names the
# numbers, so it is captured and quoted rather than re-derived. Same constants as
# check-merge-sequence.py, which asks the same guard the same question: two
# spellings of one report would be one spelling too many. These two carried the
# pre-#1158 wording (`documents N but M are collected`) after the guard stopped
# printing it (2026-09-13), so a real finding was reported as the last line of the
# report - the trailing "Measure it with:" hint - instead of as the finding itself.
GUARD = "scripts/check-doc-count.py"
COUNT_IN_REPORT = re.compile(r"FAIL: (\d+) tracked file\(s\) state")
OK_IN_REPORT = re.compile(r"OK: no tracked file states the Python test count")

# The guard's own failure line, matched by its first characters at line start.
# Python exits 1 for an unhandled exception too, so the exit code alone cannot
# tell "this tree breaks the rule" from "this guard never reached a verdict": only
# this line can, and the guard prints it exactly when it finds something.
FAILURE_LINE = re.compile(r"^FAIL: ", re.MULTILINE)


# The question could not be answered. Never a verdict. An *alias*, not a subclass:
# a gate that catches this name has to catch what the shared module raises too.
MeasurementError = merge_tree.MeasurementError


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


def _refresh_base(base: str) -> None:
    """Bring the base up to date when it names a remote-tracking branch.

    Every PR head is fetched from the network, so this tool always answers about
    the heads as they are *now*. The base was not, and a base that is read from
    whatever the local ref happens to hold makes the two halves of one question
    come from different points in time - the wrong-tree defect this file's
    `_rev_parse` docstring already records one level down, with the merge tree
    being the thing that differs rather than just the header.

    Measured on this repo (`cyc20260913-231848`) over the 25 PR head refs saved in
    the clone, folding `merge-tree --write-tree <base> <head>` against master's
    previous commit and against master: **22 conflict against both bases** (safe -
    no tree is produced), and of the 3 that merge cleanly **all 3 land a different
    tree** - the base's own advance flows into the tree being judged, so the gate
    answers about a tree nobody will land. Here the difference was master's version
    bump (8 files, all 8 version declarations agreeing in each tree), and the
    doc-count guard this tool runs happened to pass in both, so **no verdict flip
    was observed** in these three: what is measured is the wrong tree, not a wrong
    verdict. The sibling tools measured verdict flips too, which is why they
    refresh.

    A **remote-tracking** ref is refreshed, in either spelling the caller may write
    it: `origin/<branch>` and `refs/remotes/origin/<branch>` are the same mutable
    ref, and the fully-qualified spelling is the one `check-merge-pairs.py`'s
    refusal text tells callers to use. A `<remote>/HEAD` spelling is resolved
    through its symref first (a fetch *into* a symref cannot be locked - git
    refuses and leaves it unchanged), and **whether a ref is a symref is decided by
    git, not by its name**: `<branch>/HEAD` is a legal branch name, so
    `refs/remotes/origin/feature/HEAD` is an ordinary remote-tracking branch that
    merely ends in `/HEAD`.

    Anything else is taken literally: a SHA is immutable by construction, a local
    branch is not the remote ref whatever it is called, and a refspec the caller
    already wrote (`origin/x:dest`) is passed to git as given. The destination is
    written **fully qualified**, because a bare `origin/master` as a fetch
    destination makes git create a *local branch* of that name, which then shadows
    the remote-tracking ref.

    A fetch failure, or a `<remote>/HEAD` spelling whose symref leads outside
    `origin`'s tracking refs, is a measurement error: the caller must not silently
    continue against a base it could not verify.
    """
    if ":" in base:
        return
    if base.startswith("origin/"):
        dest = f"refs/remotes/origin/{base[len('origin/'):]}"
    elif base.startswith("refs/remotes/origin/"):
        dest = base
    else:
        return
    if dest.endswith("/HEAD"):
        link = _run(["git", "symbolic-ref", "--quiet", dest])
        if link.returncode == 0:
            target = link.stdout.strip()
            if not target.startswith("refs/remotes/origin/"):
                raise MeasurementError(
                    f"could not refresh {base}: {dest} is a symbolic ref to "
                    f"{target}, which is not a remote-tracking branch of origin"
                )
            dest = target
    branch = dest[len("refs/remotes/origin/"):]
    proc = _run(["git", "fetch", "--quiet", "origin", f"+refs/heads/{branch}:{dest}"])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise MeasurementError(f"could not refresh {base}: {detail}")


def _qualify_ref(ref: str) -> str:
    """Expand a short remote-tracking name to its fully-qualified form.

    `--base` defaults to the short spelling `origin/master`, and that spelling is
    ambiguous: git resolves a bare name by a precedence list, and
    `refs/heads/<name>` is consulted *before* `refs/remotes/<name>`. A local branch
    called `origin/master` - which git itself creates when a fetch destination is
    written unqualified, the trap `_refresh_base` describes - therefore shadows the
    remote-tracking ref, and every measurement after that answers about the wrong
    tree. Measured on this repo (`cyc20260913-072845`) with a stray
    `refs/heads/origin/master` at `02e43c8` while the real remote-tracking ref was
    `245125e`, the sibling tool printed the two-cycle-old commit **as**
    `origin/master`. The name is therefore looked up **by its full name**, where
    precedence does not apply; a short name that denotes only a local branch is
    refused rather than measured, and a name that a stray shadows is reported so
    the stray can be removed.

    The shadow case *warns* instead of failing because the qualified lookup makes
    the answer right either way; refusing would block a checkout for a stray ref
    that no longer affects this tool's verdict.
    """
    if not ref.startswith("origin/") or ref.count("/") != 1:
        return ref
    qualified = f"refs/remotes/{ref}"
    if _ref_exists(qualified):
        if _ref_exists(f"refs/heads/{ref}"):
            print(
                f"warning: {ref} is ambiguous - a local branch shadows it; "
                f"measuring {qualified}. Delete the shadow: git branch -D {ref}",
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
    mutable in a way a SHA is not - `fetch` rewrites `FETCH_HEAD`, so passing the
    name would answer about whatever the last fetch happened to leave behind.
    That defect was measured in `check-merge-order.py`, which is why the rule is
    asserted rather than remembered.

    A name is also *ambiguous* in a way a SHA is not: `_qualify_ref` runs first, so
    the commit returned is the one the caller's name denotes rather than the one
    git's precedence rules would pick.
    """
    proc = _run(["git", "rev-parse", "--verify", f"{_qualify_ref(ref)}^{{commit}}"])
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
    """Fetch a PR's real head into a temp ref and return its commit SHA.

    The refspec is forced (`+`): a PR head is routinely re-pushed to a commit
    that is not a descendant of the previous one (every conflict resolution in
    this repo does), and a rejected fetch leaves the *stale* ref in place, so the
    check would silently answer about a tree that is no longer the PR.

    The parked ref is dropped before this returns (`merge_tree.drop_ref`): the
    caller wants the commit - it rev-parses what comes back either way - and a ref
    left behind pins the head's objects for the life of the clone (measured
    2026-09-17: this gate had 40 of them resident).
    """
    ref = f"refs/emrg-tree-health/pr{number}"
    proc = _run(["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise MeasurementError(f"could not fetch PR #{number}: {detail}")
    sha = _rev_parse(ref)
    merge_tree.drop_ref(ref, run=_run)
    return sha
def _merge_tree_paths(
    a: str, b: str, cwd: str | None = None
) -> tuple[list[str] | None, str]:
    """`(conflicted paths, what git said)`; the paths are None if unmeasurable.

    An empty list means the merge is clean, which is distinct from None (the
    question was not answered).

    The measurement is `merge_tree.fold`'s, and so is the reading of the paths -
    **the exit code is not the signal; the named tree is**, and the paths come from
    the report's stage block decoded, not as git spelled them. Measured here on
    2026-09-14 (`cyc20260914-050817`): `git merge-tree --write-tree <commit>
    <blob>` exits 1 with **empty stdout**, which the `rc == 1` branch this function
    used to have turned into `[]` - the *clean-merge* answer, over a merge nobody
    made. What stays here is the mapping to this tool's three answers: a conflict
    whose paths the report does not name is `None` ("not answered"), never `[]`.

    The pair exists for the same reason it does in `check-merge-order.py` (issue
    #1559): `check_pr` raises when the answer is None, and it raised with a literal
    - "merge-tree failed for PR #N" - so `Fold.diagnosis` was computed for this gate
    and thrown away. That is the gate whose whole job is to say whether a merge can
    be judged, and its one failure sentence read as a fact about the *PR*: in a
    shallow clone the same refusal is a fact about the checkout, with the repair
    (`git fetch --unshallow`) in git's words.
    """
    answer = merge_tree.fold(a, b, run=_run, cwd=cwd)
    if answer.verdict == "clean":
        return [], answer.diagnosis
    if answer.verdict != "conflict":
        return None, answer.diagnosis
    return list(answer.paths) or None, answer.diagnosis


def _merged_tree_sha(a: str, b: str, cwd: str | None = None) -> str:
    """The tree sha of the clean merge of `a` and `b`.

    The rule is `merge_tree.merged_tree_sha`'s - the named OID, never the exit code,
    and `MeasurementError` rather than a falsy value, because an unhandled exception
    leaves this tool as exit 1 - the code that means "a clean merge landed an
    unhealthy tree", i.e. a crash reported as a finding about a tree nobody measured.
    What is this tool's is the repository the question is asked in (`cwd`), and the
    runner that pins its decoding.
    """
    return merge_tree.merged_tree_sha(a, b, run=_run, cwd=cwd)


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
    reported as healthy is how a broken tree reaches master. The same holds one
    exit code over: a guard that exits 1 *without* printing its own failure line
    never reached a verdict, so a crash is not a finding either.
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
        # Same convention as the sibling: the report names the guard's own line
        # when it is recognised, and says no more than it can when it is not.
        m = OK_IN_REPORT.search(out)
        return True, "guard OK (no stored count)" if m else "guard OK"
    if proc.returncode == 1:
        # A red verdict has to be *evidenced by the guard's own report*. Python
        # exits 1 for an unhandled exception as well as for a deliberate `exit(1)`,
        # so the code alone cannot tell a tree that breaks the rule from a guard
        # that never reached a verdict - and the second, reported as the first, is
        # a finding about a tree that was never measured. Measured 2026-09-14
        # (`cyc20260914-033026`): a guard that cannot import its own dependencies
        # prints a traceback and exits 1, which this branch used to report as
        # `guard FAIL (1 tracked file(s) state the test count)`.
        #
        # This is the same rule the sibling reader in check-merge-sequence.py
        # already applies to the same guard (`_base_states_a_count`), and the same
        # one its `_guard_verdict` now applies: rc == 1 *and* the guard's line.
        if FAILURE_LINE.search(out) is None:
            raise MeasurementError(
                "the merged tree's guard exited 1 without reporting a finding, so it "
                f"crashed instead of reaching a verdict: ran {GUARD} from the tree at "
                f"{workdir}, with {sys.executable} - the code a bare interpreter and an "
                f"unimportable tree both produce. Re-run it there:\n"
                + out[-1000:].strip()
            )
        m = COUNT_IN_REPORT.search(out)
        if m:
            detail = f"{m.group(1)} tracked file(s) state the test count"
        else:
            # A finding in the same shape but another wording: quote the guard's
            # own line, never the tail of its advice.
            detail = FAILURE_LINE.split(out, maxsplit=1)[1].splitlines()[0].strip()
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
    paths, diagnosis = _merge_tree_paths(base, head, cwd=cwd)
    if paths is None:
        raise MeasurementError(f"merge-tree failed for PR #{number}: {diagnosis}")
    if paths:
        return "conflict", f"conflicts on {', '.join(sorted(set(paths)))}"
    tree = _merged_tree_sha(base, head, cwd=cwd)
    passed, report = _guard_verdict(tree, workdir, cwd=cwd)
    return ("healthy" if passed else "unhealthy"), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            f"Check that merging a PR produces a tree {GUARD} passes - that guard "
            "alone; the suite is check-merge-plan-suite.py's question."
        )
    )
    parser.add_argument("prs", nargs="*", type=int, help="PR numbers (default: all open)")
    parser.add_argument("--repo", default="argszero/emrg", help="owner/name")
    parser.add_argument("--base", default="origin/master", help="the ref to merge onto")
    args = parser.parse_args(argv)

    try:
        _refresh_base(args.base)
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
        f"\nclean, and {GUARD} passed: {healthy}\n"
        f"clean but FAILS {GUARD}: {unhealthy}\n"
        f"conflicts (not judged here): {conflicts}\n"
        f"(judged by {GUARD} alone - the suite is check-merge-plan-suite.py's question)"
    )
    if unhealthy:
        print(
            "\nAn unhealthy entry merges without conflict but lands a tree whose own "
            f"copy of {GUARD} rejects it. Resolve it on the merged tree (measure, never "
            "pick a side), commit that edit, and push: pushing voids the votes, so the "
            "repair is not free."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
