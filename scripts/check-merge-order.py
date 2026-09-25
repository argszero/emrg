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
with `rev-parse` first, and
`tests/test_check_merge_order.py::TestNoMutableRefNameReachesMergeTree::test_the_shipped_source_passes_only_commits_to_merge_tree`
asserts that invariant over the calls the tool builds, which is what would have
caught this.

The class segment is not decoration: the name this line carried before
(`test_no_mutable_ref_name_reaches_merge_tree`) never existed, and the one that does
sits inside `TestNoMutableRefNameReachesMergeTree`, so a node id without that segment
collects nothing - running it answers `ERROR: not found`. A citation that cannot be
run is the defect this file is about; `scripts/check-citation-resolves.py` holds the
rule.

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

(That quoted header has a second defect of its own, found later and fixed separately:
`1 open PR(s)` counts the number the caller *named* (1196), not the repository's open
PR(s) - see "What the header counts" below. The line is left as it was measured.)

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

What a merge *lands*, not only what it dirties
----------------------------------------------
Conflict is one way a merge reaches past its own PR. The other is **containment**, and
this tool looked for neither until now: a PR's head can already *contain* another open
PR's head, which makes the upper PR's landing change that PR's change plus this one's,
and leaves the lower PR with nothing to land once the upper one merges. Nothing in the
family caught it. Measured 2026-09-25 (`cyc20260925-165507`) on this queue: `#1614`'s
head `d2d10064` contains `#1611`'s `cf5fa701`, because the cycle that wrote it branched
from the branch it had just been standing on while resolving `#1611`'s conflict. Both
read `MERGEABLE/CLEAN`, both were green, this tool printed `0 of 10 pairs conflict` and
`merging it dirties nothing else` for each, and `check-merge-landing-diff.py 1614` showed
`#1611`'s five files inside `#1614`'s landing change. `check-pr-base.py` does not see it
either, and cannot: that gate reads the base a PR **declares**, and `#1614` declares
`master`.

The relation is one `git merge-base --is-ancestor` per ordered pair, on commits the run
has already fetched for the conflict measurement, so it costs no network and no new
ref. Like `check-pr-base.py`'s `LIVE`, a stack is **reported, not failed**: stacking is a
legitimate way to carry work, and what a queue needs from it is the order - land the
lower PR first, so each lands its own change and neither rides on the other's review.
The exit code is unchanged by this section.

Usage
-----
    uv run --no-sync python3 scripts/check-merge-order.py [PR ...]
    uv run --no-sync python3 scripts/check-merge-order.py --json
    uv run --no-sync python3 scripts/check-merge-order.py --base <ref>

With no PR numbers, every open PR is used. Heads are resolved by fetching
`refs/pull/<N>/head` into a temporary ref, so the measurement uses each PR's real
head rather than whatever a local branch of a similar name happens to point at.

What the header counts
----------------------
The numbers printed are the **selection's**, and the header says which one it is:
`N open PR(s)` only when the caller named none - the case where the selection *is*
the open set - and `N named PR(s)` when the caller supplied them. The distinction is
not cosmetic. Measured 2026-09-26 (`cyc20260926-015635`) with three PRs open:

    uv run --no-sync python3 scripts/check-merge-order.py 1627
    base 3d9e4557..., 1 open PR(s), 0 of 0 pairs conflict

    #1627: mergeable, and merging it dirties nothing else

Three PRs were open and this PR dirtied nothing - a true sentence about the caller's
one-number list, printed in the vocabulary of a fact about the repository, and read
by the cycle that ran it (this one) as the queue-wide all-clear the merge gate wants.
The per-PR verdict carries the same bound: under the default `dirties nothing else`
means every open PR, and under a named list it says `among the N PR(s) named`, because
that is what was compared. A named list of fewer than two has nothing to compare at
all - no pair conflicts and no other PR to dirty - so it says so on stderr, with the
exit code unchanged: the measurement was made, and what went unanswered is a different
question (the shape `check-merge-landed.py`'s `UNCHECKED`/`PENDING` notes use).

Since the selection is the caller's, so is the fix: pass the other open numbers, or
none at all.

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
import subprocess
import sys
from pathlib import Path

# The shared module's directory, so `import merge_tree` works however this file is
# loaded: as `python3 scripts/check-merge-order.py` it is already `sys.path[0]`, but
# the test suite loads these tools by file path (`spec_from_file_location`), where it
# is not. `merge_tree.py` is a module of this repo, not a dependency.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_tree  # noqa: E402  (needs the path above)

# The sibling whose *base-resolution* rules this one asks for, loaded from its file
# rather than imported by name: the scripts in this directory are not importable
# modules (hyphenated names, no package), and this is the same loader the test suite
# already uses for them. Taken from the sibling rather than copied so the rule has one
# implementation - a base name's meaning must not differ between the gates that ask
# about it.
#
# `check-merge-sequence.py` owns base resolution (`_qualify_ref`, `_refresh_base`).
# The conflicted-path *reading* is not asked of a sibling any more: it lives in
# `merge_tree.py`, with the measurement it belongs to (see that module's docstring for
# the five copies this replaced).
#
# The dependency is one-way (that sibling does not load this file), so loading it
# eagerly here cannot recurse.
_SIBLING = Path(__file__).resolve().parent / "check-merge-sequence.py"


def _load_sibling(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - file is in this repo
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seq = _load_sibling(_SIBLING, "check_merge_sequence")


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
    """Fetch a PR's real head into a temp ref and return its commit SHA.

    By ref, not by local branch name: a local branch called `pr<N>` may point at a
    stale commit, and the whole value of this tool is that it measures the trees
    that would actually merge. The ref is dropped again before this returns —
    `merge_tree.drop_ref` owns the why.

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
    # The commit, and the ref dropped again as soon as it is read: the name is
    # mutable in a way a SHA is not, so nothing downstream wants it back, and a ref
    # left behind pins that head's objects forever (measured 2026-09-17: this gate
    # alone had 105 of them resident). The sha is what `merge-tree` is asked with.
    sha = _rev_parse(ref)
    merge_tree.drop_ref(ref, run=_run)
    return sha
def _conflict_paths(a: str, b: str) -> tuple[list[str] | None, str]:
    """`(paths that conflict, what git said)`; the paths are None if not answered.

    An empty list means the merge is clean - distinct from None, which means the
    question was not answered (a bad ref, a git that rejects `--write-tree`, or a
    report that names no merged tree).

    **The second element is why this returns a pair.** Both callers below raise when
    the answer is None, and they used to raise with a literal - "the merge question
    was not answered" - so everything `merge_tree.Fold.diagnosis` learns was computed
    for this gate and dropped (issue #1559). What that costs is not hypothetical: the
    fold spends an extra git call to find out whether an unanswered merge is an
    unrelated-history refusal *in a shallow clone*, the one fact that separates "the
    PRs are at fault" from "this checkout cut their common ancestor off", and the
    repair (`git fetch --unshallow`) is in git's words rather than in the literal. It
    is carried as a value rather than fetched at the raise site because a second
    `merge_tree.fold` would be a second measurement of a question already asked.
    `check-merge-plan-suite.py::_merge_tree` needs no such carrier: its raises sit in
    the fold's own frame, so `answer.diagnosis` is simply in scope. Here the raise is
    one frame up, in `forecast`, so the one field that frame needs is what the pair
    carries - not the whole `Fold`, whose other fields are this tool's mapping and
    must not leak to a caller that would then re-derive them.

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

    Both facts - the named tree and the decoded stage-block paths - are
    `merge_tree.fold`'s, measured once for every gate that needs them (that
    module's docstring carries the arm table: prose reports a real name but only
    for some conflict kinds, the block always names the paths but quotes them, and
    `unquote_path` is what makes the block's answer a file one can open). What is
    this function's own is the *mapping* to a caller's three answers: `"clean"` to
    `[]`, anything unmeasured to `None`, a conflict to its paths or `None` (a
    conflict whose paths the report does not name is not the clean answer), and
    the paths deduplicated - the fold returns one entry per stage line.
    """
    answer = merge_tree.fold(a, b, run=_run)
    if answer.verdict == "clean":
        return [], answer.diagnosis
    if answer.verdict != "conflict":
        return None, answer.diagnosis
    paths: list[str] = []
    for path in answer.paths:
        if path not in paths:
            paths.append(path)
    return paths or None, answer.diagnosis


def _is_ancestor(earlier: str, later: str) -> bool:
    """True when commit `earlier` is an ancestor of (or equal to) commit `later`.

    `git merge-base --is-ancestor` answers with its exit code and prints nothing: 0 for
    yes, **1 for no**, and anything above that is git declining to answer at all (a
    missing object, an unusable repository, a shallow clone without the common
    ancestor). The third case is why this raises instead of returning False: "not 0" is
    not "no", and independence is the reassuring answer here - the same asymmetry
    `merge_tree.fold` refuses for merges, where a failure to merge the two *inputs* also
    exits 1 with no output and must not be read as a conflict. A pair this checkout
    cannot ask about is reported as unmeasured, by exiting 2, never as unrelated.
    """
    proc = _run(["git", "merge-base", "--is-ancestor", earlier, later])
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise RuntimeError(
        f"could not ask whether {earlier[:8]} is an ancestor of {later[:8]}: "
        f"{proc.stderr.strip() or f'git exited {proc.returncode}'}"
    )


def forecast(base: str, numbers: list[int], repo: str) -> dict:
    """Per-PR: does it conflict with the base, what would it dirty, what does it contain.

    Two questions per pair, asked in one pass so they cannot be about different pairs:
    the merge (conflict, `merge_tree`'s) and the ancestry (containment, `_is_ancestor`'s).
    """
    # Resolve the base first: fetching the PR heads below rewrites FETCH_HEAD, so a
    # name held across them would silently become the last head fetched.
    base_sha = _rev_parse(base)
    heads = {number: _rev_parse(_fetch_head(repo, number)) for number in numbers}
    report: dict = {
        "base": base_sha,
        "prs": {},
        "base_conflicts": [],
        "contains": {},
        "identical": [],
    }
    for number in numbers:
        paths, diagnosis = _conflict_paths(base_sha, heads[number])
        if paths is None:
            raise RuntimeError(
                f"could not classify PR #{number} against {base} - "
                f"the merge question was not answered: {diagnosis}"
            )
        report["prs"][number] = {"paths": paths, "dirtied": []}
        if paths:
            report["base_conflicts"].append(number)
    for i, a in enumerate(numbers):
        for b in numbers[i + 1 :]:
            paths, diagnosis = _conflict_paths(heads[a], heads[b])
            if paths is None:
                raise RuntimeError(
                    f"could not classify #{a} against #{b} - "
                    f"the merge question was not answered: {diagnosis}"
                )
            if paths:
                report["prs"][a]["dirtied"].append({"pr": b, "paths": paths})
                report["prs"][b]["dirtied"].append({"pr": a, "paths": paths})
            # Equal heads are the degenerate case and must not reach the branch below:
            # a commit is an ancestor of itself, so both directions answer yes and the
            # pair would be printed as "one contains the other" - true, and useless.
            # Two open PRs sharing a head is its own fact, reported as such.
            if heads[a] == heads[b]:
                report["identical"].append({"a": a, "b": b, "head": heads[a]})
            elif _is_ancestor(heads[a], heads[b]):
                report["contains"].setdefault(b, []).append(a)
            elif _is_ancestor(heads[b], heads[a]):
                report["contains"].setdefault(a, []).append(b)
    return report


def _print_report(report: dict, *, selection: str) -> None:
    """Print the forecast, naming the set it is about.

    `selection` is where the numbers came from - `"open"` when the caller named none, so
    every open PR was used, `"named"` when the caller supplied the numbers. It is
    required rather than defaulted, because the default would be the very claim this
    argument exists to stop: measured 2026-09-26 (`cyc20260926-015635`) with three PRs
    open, `check-merge-order.py 1627` printed `1 open PR(s), 0 of 0 pairs conflict` and
    `mergeable, and merging it dirties nothing else` - a count of the caller's list
    reported as a fact about the repository, and a verdict about "everybody else" said
    about a set with nobody else in it.
    """
    prs = report["prs"]
    total = len(prs)
    pairs = total * (total - 1) // 2
    conflicting = sum(len(v["dirtied"]) for v in prs.values()) // 2
    where = "open" if selection == "open" else "named"
    print(f"base {report['base']}, {total} {where} PR(s), {conflicting} of {pairs} pairs conflict")
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
            # "nothing else" is a claim about a set, so the set is named. Under the
            # default it is every open PR and needs no qualifier; a named list is not
            # the open set, and with one number in it there is no "else" at all.
            bound = "" if selection == "open" else f" among the {total} PR(s) named"
            print(f"  #{number}: mergeable, and merging it dirties nothing else{bound}")
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
    stacked = report.get("contains") or {}
    identical = report.get("identical") or []
    if stacked or identical:
        print("What a merge would land, beyond its own change (the base field does not")
        print("declare this, and no other gate reads it):")
        for upper in sorted(stacked):
            lowers = sorted(stacked[upper])
            names = ", ".join(f"#{n}" for n in lowers)
            print(
                f"  #{upper} is stacked on {names}: its head already contains the "
                f"commits of {'those PRs' if len(lowers) > 1 else 'that PR'}, so a "
                f"merge of #{upper} lands them too - and leaves "
                f"{'them' if len(lowers) > 1 else 'it'} with nothing left to land on "
                "its own review."
            )
            order = " -> ".join(f"#{n}" for n in lowers) + f" -> #{upper}"
            print(f"    land in this order: {order}")
        for pair in identical:
            print(
                f"  #{pair['a']} and #{pair['b']} have the same head commit "
                f"({pair['head'][:8]}) - a merge of either lands it"
            )
        print()
    print("Merging a PR costs one resolution per later PR it dirties, and each")
    print("resolution push voids that PR's votes. Cheapest-first is not always")
    print("most-valuable-first; choose deliberately.")
    if selection != "open" and total < 2:
        # rc is unchanged: the measurement was made, and what was not answered is a
        # different question. A vacuous all-clear and a real one must not print the
        # same thing, which is the whole point of saying it here rather than in a
        # docstring nobody runs.
        print(
            f"\n{total} PR(s) named: this forecast compares the numbers it was given "
            "against each other, so with fewer than two there is no other PR to dirty "
            "and no pair to conflict - the line above is about no other PR, not about "
            "the open queue. Pass the other open numbers, or none at all to use every "
            "open PR.",
            file=sys.stderr,
        )


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

    selection = "named" if args.prs else "open"
    if args.json:
        report["selection"] = selection
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_report(report, selection=selection)
    return 1 if report["base_conflicts"] else 0


if __name__ == "__main__":
    sys.exit(main())
