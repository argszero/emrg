#!/usr/bin/env python3
"""Forecast which open PRs each merge would dirty, before choosing an order.

The class this exists for
------------------------
On 2026-09-11, eleven open PRs were each individually `MERGEABLE/CLEAN` against
master and each independently green in CI. Every one of them was also ahead of
master, so **any** one of them could be merged. Two cycles derived by hand what
that meant:

* a contributor review noted "the first of them to merge leaves the other three
  conflicting on that one line";
* this repo's own cycle then measured the full matrix with a throwaway
  `git merge-tree` loop and got **51 of 55 pairs conflicting**, every single one on
  `Agent.md` alone - one line, the documented Python test count.

The friction is not the fix, which is mechanical
(`check-doc-count.py --resolve-conflict` re-measures the merged tree and never
picks a side). It is that the *cost* was invisible until afterwards: merging one
ready PR made the other ten `CONFLICTING/DIRTY`, which meant no CI (a dirty PR
gets no `pull_request` run), no merge, and - because resolving requires a push -
**every vote on them void**. Three PRs that were one vote from landing went back
to 0/3. That is a real cost paid out of the 3-consecutive-vote rule, and it was
paid unknowingly.

Why measure instead of reason about it
--------------------------------------
The intuitive account of the cascade ("they all touch the count line") is *nearly*
right and unusable as a rule: on the same day, four of the 55 pairs were clean
despite sharing `Agent.md`, because whether two edits to one file conflict depends
on how close they land. Pairwise conflict is a property of the trees, not of the
file lists, so it is asked of `git merge-tree` rather than inferred.

What was measured
-----------------
`git merge-tree --write-tree <a> <b>` is run per pair. **The merged tree's name on
the first line of its output is what answers the question; the exit code does not.**
A clean merge names that OID and exits 0; a conflict names the same OID, then the
stage block (one `100644 <blob> <stage>\t<path>` line per side per conflicted path,
stages 1/2/3) and exits 1. But the code does not separate those two cases from
"the question was not answered": a failure to merge the two *inputs* exits 1 with
empty output, and `git merge-tree --write-tree --quiet` - a documented flag that
suppresses exactly the tree name - exits **0 with empty output** for a clean merge
(both measured 2026-09-14 in a scratch repo, `cyc20260914-055701`). So a report
that names no tree is read as *not answered* whatever code came with it, and the
paths are only read out of a report that did name one - which is what makes `[]`
mean an *evidenced* clean merge rather than an inference. The report names **which**
file collides, not merely that something did.

This deliberately does **not** attempt the merge or touch the working tree: the
question is "what would happen", and answering it must not itself dirty the
checkout - a forecast that has to be cleaned up is worse than no forecast.

The base is resolved to a commit **before** any PR head is fetched
------------------------------------------------------------------
Found by running this tool against the live queue on the day it was written
(`cyc20260911-225712`). It fetched master into `FETCH_HEAD` and passed the *name*
`FETCH_HEAD` as the base - but `git fetch refs/pull/<N>/head` rewrites
`FETCH_HEAD` too, so by the time each pair was measured the base had been
repointed at the most recently fetched PR head. The output was entirely
plausible: #1152 reported as the only PR mergeable against "master", everything
else conflicting. It was also nonsense - the base was #1152's own head, which is
why the two "sides" were identical there.

The tell was the count line: the run printed nothing about master, but master
really sat at 1490 while the run behaved as if the base said 1503. So the rule is
that a **mutable ref name must never reach `merge-tree`**: the base is resolved
with `rev-parse` first, and the test
`test_no_mutable_ref_name_reaches_merge_tree` asserts that invariant over the
argv the tool builds, which is what would have caught this.

An explicit `--base origin/master` names two refs, and git picks one of them
--------------------------------------------------------------------------
Resolving the base to a commit is only half the rule: the *name* is ambiguous, and
`rev-parse` consults `refs/heads/<name>` **before** `refs/remotes/<name>`. Git
itself creates a local branch called `origin/master` when a fetch destination is
written unqualified, so one stray branch of that name replaces the remote-tracking
ref and every number below is then a true answer about a base the caller never
named. The printed SHA is what exposes it; the name alone does not.

Measured `cyc20260913-234157` in a scratch clone (real git, no network) whose
`refs/remotes/origin/master` sat at `5e45e3d` with a stray `refs/heads/origin/master`
at `2f9c552`:

    uv run --no-sync python3 scripts/check-merge-order.py 1196 --base origin/master
    base 2f9c552403f30882da7856db4f14702f4df508b9, 1 open PR(s), 0 of 0 pairs conflict

i.e. the stray, reported under the caller's name. So an explicit base that names a
remote-tracking ref is resolved **by full name** before anything is measured, and a
name that denotes *only* a local branch is refused rather than measured (exit 2).
The default path is unaffected: it fetches master into `FETCH_HEAD`, which is
neither ambiguous nor remote-tracking, and is left exactly as it is.

Resolving the name is only half of that; the other half is *when* the commit behind
it is read
------------------------------------------------------------------------------
A ref that names the right thing can still hold the wrong commit, and an explicit
base used to be read at whatever moment this checkout last fetched - unlike the
default path, which fetches. So the tool answered `base -> PR` from two different
times: every head below is fetched as it is now, while the base could be days old,
and the forecast (which PRs conflict with the base, and which PRs dirty which) was
computed against a tree the caller never named.

Measured `cyc20260914-014536` in a hermetic clone whose `refs/remotes/origin/master`
sat one commit behind the remote it was cloned from - the normal state of a checkout
that has not fetched - with the commit dates pinned so both shas reproduce:

    before this change   base 450c013804b6d98d67f98dbe1a4b25b5b9894f9a   (the stale ref)
    after                base fd8cb9d103ce7c8eaa267a73566ee07e3ef88bb2   (the remote's tip)

The refresh is the sibling's `_refresh_base` (called, not copied), on the ref the
caller named: it runs after the refusal above, so a stray local branch is refused
rather than fetched into existence, and before anything is measured. A base that
cannot be refreshed is exit 2 - a forecast about a base nobody verified is exactly
the answer this gate exists to refuse.

The order it recommends
-----------------------
A merge costs one resolution per *later* PR it dirties. So the PR that dirties the
fewest others is the cheapest first move, and the shape to look for is a set of PRs
that conflict with everything (usually a shared line everyone edits) plus a few that
conflict with nothing. The tool prints the count per PR and names the collision
sets; the ordering decision stays with the cycle, because "cheapest first" is not
always "most valuable first" - on 2026-09-11 the right first move was the CI-reach
fix (#1149), which conflicted with everything but unblocked CI for every stacked PR
behind it.

Usage
-----
    uv run --no-sync python3 scripts/check-merge-order.py [PR ...]
    uv run --no-sync python3 scripts/check-merge-order.py --json
    uv run --no-sync python3 scripts/check-merge-order.py --base <ref>

With no PR numbers, every open PR is used. Heads are resolved by fetching
`refs/pull/<N>/head` into a temporary ref, so the measurement uses each PR's real
head rather than whatever a local branch of a similar name happens to point at.

An explicit `--base` that names a remote-tracking ref (`origin/<branch>`) is taken
by its **full name**, so a stray local branch of the same name cannot stand in for
it, and it is **refreshed** from the remote before it is read, so the base and the
heads are taken at the same moment; a name that denotes only a local branch is
refused rather than measured, and a base that cannot be refreshed is exit 2. The
default (no `--base`) is unaffected - it fetches master itself.

Exit codes
----------
    0  measurement made (which is not an endorsement of any order)
    1  at least one PR conflicts with the base - not mergeable as it stands
    2  the measurement could not be made (gh/git failed, unparseable output, a base
       name that denotes only a local branch, or a base that could not be
       refreshed) - fail loud; never report an order for a question that was not
       answered

`gh` and network access to GitHub are required to list PRs and fetch their heads;
there is no offline mode.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

# `100644 <blob> <stage>\t<path>` - the conflict block merge-tree writes first, one
# line per side per conflicted path (stage 1/2/3 are base/ours/theirs) - is matched
# by `health._conflict_block_paths` below rather than by a second regex here: that
# reading is one rule with one implementation, and this tool asks the owner of it.

# The sibling tools whose rules this one asks for, loaded from their files rather than
# imported by name: the scripts in this directory are not importable modules
# (hyphenated names, no package), and this is the same loader the test suite already
# uses for them. Taken from the sibling rather than copied so each *rule* has one
# implementation - a base name's meaning must not differ between the gates that ask
# about it, and neither must a conflicted path's name.
#
# * `check-merge-sequence.py` owns base resolution (`_qualify_ref`, `_refresh_base`).
# * `check-merge-tree-health.py` owns the conflicted-path *reading* - the shape match
#   plus the decoding of git's path quoting (`_conflict_block_paths`), the change
#   #1212 landed and #1215 delegated to.
#
# Both dependencies are one-way (neither sibling loads this file), so loading them
# eagerly here cannot recurse.
_SIBLING = Path(__file__).resolve().parent / "check-merge-sequence.py"
_PATHS_SIBLING = Path(__file__).resolve().parent / "check-merge-tree-health.py"


def _load_sibling(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - file is in this repo
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seq = _load_sibling(_SIBLING, "check_merge_sequence")
health = _load_sibling(_PATHS_SIBLING, "check_merge_tree_health")


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command with the locale independent of the environment."""
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")


def _rev_parse(ref: str) -> str:
    """Resolve a ref to a commit SHA.

    Every ref used in a merge question goes through here first. A *name* is
    mutable in a way a SHA is not: `FETCH_HEAD` is rewritten by any `git fetch`,
    so passing the name to `merge-tree` answers about whatever the fetch happened
    to leave behind. That is not hypothetical - it is the defect this tool shipped
    with, and the reason the invariant is asserted in the tests.
    """
    proc = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"])
    if proc.returncode != 0:
        raise RuntimeError(f"could not resolve {ref!r} to a commit: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _open_pr_numbers(repo: str) -> list[int]:
    """The open PR numbers, ascending - the default subject of the forecast."""
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
        raise RuntimeError(f"gh pr list failed: {proc.stderr.strip()}")
    numbers = [int(line) for line in proc.stdout.split() if line.strip()]
    if not numbers:
        raise RuntimeError("no open PRs reported - nothing to forecast")
    return sorted(numbers)


def _fetch_head(repo: str, number: int) -> str:
    """Fetch a PR's real head into a temp ref and return the ref name.

    By ref, not by local branch name: a local branch called `pr<N>` may point at a
    stale commit, and the whole value of this tool is that it measures the trees
    that would actually merge.

    The refspec is **forced** (`+`), and must be. A PR head is routinely re-pushed
    to a commit that is not a descendant of the previous one - every conflict
    resolution in this repo pushes a new head over the old - so the second run of
    this tool against a branch whose head moved would otherwise be rejected:

        ! [rejected]  pull/1148/head -> refs/emrg-forecast/pr1148  (non-fast-forward)

    The fetch then exits 1 **and leaves the stale ref in place**, so the failure is
    not merely noisy: the ref the run would have measured is still the *old* head,
    i.e. the tool would answer about a tree that is no longer the PR. Reproduced
    against a real pair of divergent heads (`cyc20260911-235001`); with the `+` the
    same two fetches both succeed and the ref ends at the true head.
    """
    ref = f"refs/emrg-forecast/pr{number}"
    proc = _run(
        ["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"]
    )
    if proc.returncode != 0:
        # Not `--quiet`: it suppresses the rejection diagnostic as well, which is
        # how this surfaced as an undiagnosable "unknown error" with empty stderr.
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"could not fetch PR #{number}: {detail}")
    return ref


def _is_object_name(line: str) -> bool:
    """Whether a line is a bare object name (the merged tree's).

    Both the SHA-1 (40 hex) and SHA-256 (64 hex) object formats are accepted: the
    question is the *shape* of the answer, which must not depend on the object
    format of whichever clone happens to run this. The same rule and the same
    spelling as `check-merge-plan-suite.py`, `check-merge-landing-diff.py` and
    `check-merge-tree-health.py`, each of which reads its merge verdict this way.
    It is spelled here because the sibling this tool loads (`seq`) does not carry
    it on master - the rule reaches `seq` in #1207 - and once that has landed this
    copy should come from there, the way *base* resolution already does.
    """
    return bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", line))


def _conflict_paths(a: str, b: str) -> list[str] | None:
    """Paths that conflict when `a` and `b` are merged; None if the merge is not answered.

    An empty list means the merge is clean - distinct from None, which means the
    question was not answered (a bad ref, a git that rejects `--write-tree`, or a
    report that names no merged tree).

    **The exit code is not the answer; the name on the first line is.** Both of the
    answers this function can give have to be evidenced by `merge-tree`'s report,
    because neither exit code identifies one: a clean merge names its tree *and*
    `--quiet` exits 0 printing nothing, while a genuine conflict names the tree
    and a failure to merge the two inputs exits 1 printing nothing (measured
    2026-09-14 in a scratch repo, `cyc20260914-055701`). `rc == 0` alone therefore
    is not evidence that a merge was produced: reading `[]` out of it hands the
    caller the *clean* answer over a merge the tool never saw, and `[]` is the one
    answer here that nothing downstream re-checks - `forecast` reports such a PR as
    conflicting with nothing, so it can be recommended in an order it cannot take.
    An unevidenced answer in the other direction was already refused (`paths or
    None`), so the asymmetry was the whole defect: the *reassuring* answer was the
    half that could be invented.

    **The paths are decoded, not passed through as git wrote them.** This function
    used to match the stage block's shape itself and return the text after the tab,
    which is git's *spelling* of the name rather than the name: with the default
    `core.quotePath=true`, a path holding a non-ASCII byte, a quote, a backslash or a
    control byte arrives C-quoted. Measured 2026-09-14 (`cyc20260914-104220`, git
    2.50.1, scratch repos), a conflict in `中文.txt` was reported here as
    `'"\\344\\270\\255\\346\\226\\207.txt"'` - and `(repo / that).exists()` was
    measured `False`, while the sibling's decoded reading answers `中文.txt`
    (`exists? → True`). Those names are not decoration: `forecast` prints them
    ("CONFLICTS with the base on ...", "dirties ... on ... (n)") and attributes the
    cascade through them, so a quoted spelling tells the caller to resolve a file
    that does not exist.

    The reading is the sibling's rather than a fifth copy of the rule
    (`check-merge-tree-health.py::_conflict_block_paths`, which matches the shape and
    decodes the quoting; #1212 landed it and #1215 delegated the sequence tool to it).
    This function keeps its own two contract bits on top: the paths are deduplicated
    here (the sibling returns one entry per stage line, three for a content conflict),
    and an empty answer stays `None` - "not answered" - rather than becoming the
    clean answer above.
    """
    proc = _run(["git", "merge-tree", "--write-tree", a, b])
    lines = proc.stdout.splitlines()
    named = lines[0].strip() if lines else ""
    if not _is_object_name(named):
        # No merged tree was named, so there is nothing here to have an opinion
        # about - whatever the exit code says.
        return None
    if proc.returncode == 0:
        # `[]` is the clean answer, and the named tree is what makes it evidenced.
        return []
    # A conflict exits 1 with the block on stdout; anything else is a failure to
    # measure rather than a conflict to report.
    if proc.returncode != 1:
        return None
    paths: list[str] = []
    for path in health._conflict_block_paths(lines):
        if path not in paths:
            paths.append(path)
    return paths or None


def forecast(base: str, numbers: list[int], repo: str) -> dict:
    """Per-PR: does it conflict with the base, and which PRs would it dirty."""
    # Resolve the base first: fetching the PR heads below rewrites FETCH_HEAD, so a
    # name held across them would silently become the last head fetched.
    base_sha = _rev_parse(base)
    heads = {number: _rev_parse(_fetch_head(repo, number)) for number in numbers}
    report: dict = {"base": base_sha, "prs": {}, "base_conflicts": []}
    for number in numbers:
        paths = _conflict_paths(base_sha, heads[number])
        if paths is None:
            raise RuntimeError(
                f"could not classify PR #{number} against {base} - "
                "the merge question was not answered"
            )
        report["prs"][number] = {"paths": paths, "dirtied": []}
        if paths:
            report["base_conflicts"].append(number)
    for i, a in enumerate(numbers):
        for b in numbers[i + 1 :]:
            paths = _conflict_paths(heads[a], heads[b])
            if paths is None:
                raise RuntimeError(
                    f"could not classify #{a} against #{b} - "
                    "the merge question was not answered"
                )
            if paths:
                report["prs"][a]["dirtied"].append({"pr": b, "paths": paths})
                report["prs"][b]["dirtied"].append({"pr": a, "paths": paths})
    return report


def _print_report(report: dict) -> None:
    prs = report["prs"]
    total = len(prs)
    pairs = total * (total - 1) // 2
    conflicting = sum(len(v["dirtied"]) for v in prs.values()) // 2
    print(f"base {report['base']}, {total} open PR(s), {conflicting} of {pairs} pairs conflict")
    if report["base_conflicts"]:
        print(
            "  conflicts with the base already: "
            + ", ".join(f"#{n}" for n in report["base_conflicts"])
        )
    print()
    for number in sorted(prs):
        entry = prs[number]
        if entry["paths"]:
            print(f"  #{number}: CONFLICTS with the base on {', '.join(entry['paths'])}")
            continue
        dirtied = entry["dirtied"]
        if not dirtied:
            print(f"  #{number}: mergeable, and merging it dirties nothing else")
            continue
        counts: dict[str, int] = {}
        for item in dirtied:
            for path in item["paths"]:
                counts[path] = counts.get(path, 0) + 1
        shared = ", ".join(f"{path} ({n})" for path, n in sorted(counts.items()))
        print(
            f"  #{number}: mergeable, but dirties {len(dirtied)} other PR(s) on {shared}"
            f" - {' '.join('#' + str(i['pr']) for i in dirtied)}"
        )
    print()
    print("Merging a PR costs one resolution per later PR it dirties, and each")
    print("resolution push voids that PR's votes. Cheapest-first is not always")
    print("most-valuable-first; choose deliberately.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Forecast which open PRs each merge would dirty, by asking git "
            "merge-tree (never edits the working tree)."
        ),
    )
    parser.add_argument("prs", nargs="*", type=int, help="PR numbers (default: all open)")
    parser.add_argument(
        "--base",
        default=None,
        help="base ref (default: origin/master); a remote-tracking name is taken by full name",
    )
    parser.add_argument("--repo", default="argszero/emrg", help="GitHub owner/repo")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    base = args.base
    if base is None:
        proc = _run(["git", "fetch", "--quiet", "origin", "master"])
        if proc.returncode != 0:
            print(
                f"could not fetch master: {proc.stderr.strip() or 'unknown error'}",
                file=sys.stderr,
            )
            return 2
        base = "FETCH_HEAD"
    else:
        # An explicit `--base origin/master` is ambiguous: `rev-parse` consults
        # `refs/heads/origin/master` *before* the remote-tracking ref, so one stray
        # local branch of that name replaces the base and the whole forecast is
        # about a tree nobody named. Resolved by full name (or refused), by the
        # sibling that already owns the rule - not by a second copy of it here.
        try:
            base = seq._qualify_ref(base)
        except seq.MeasurementError as exc:
            print(f"could not measure: {exc}", file=sys.stderr)
            return 2
        # Naming the ref correctly is half the rule; *when* its commit is read is the
        # other half. The heads below are fetched as they are now, so a base read at
        # whatever moment this checkout last fetched would answer `base -> PR` from
        # two different times - and the whole forecast, base conflicts included, would
        # be about a tree the caller did not name. Refreshed here, after the refusal
        # above (a stray local branch is refused, never fetched into existence) and
        # before anything is measured. A SHA, tag or local branch is left untouched by
        # the sibling; a base that cannot be refreshed is exit 2, never answered from.
        try:
            seq._refresh_base(base)
        except seq.MeasurementError as exc:
            print(f"could not measure: {exc}", file=sys.stderr)
            return 2

    try:
        numbers = sorted(args.prs) if args.prs else _open_pr_numbers(args.repo)
        report = forecast(base, numbers, args.repo)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_report(report)
    return 1 if report["base_conflicts"] else 0


if __name__ == "__main__":
    sys.exit(main())
