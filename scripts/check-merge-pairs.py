#!/usr/bin/env python3
"""Check every *ordered pair* of PRs for a clean merge that lands a failing tree.

The question this exists for
----------------------------
The sibling gates answer about one PR (`check-vote-count.py`, `check-merge-freshness.py`)
or about one *plan* (`check-merge-sequence.py`). In a queue of near-identical PRs the
question that decides what to do next is neither: **is any pair of these silently
dangerous together?** - where "silently" means git reports the merge clean and the
resulting tree fails the repo's own guard, `scripts/check-doc-count.py` (`seq.GUARD`).

One guard, not the suite
------------------------
That verdict is one guard's, so "no ordered pair merges cleanly into a failing tree" is a
statement about the derived-count class and nothing else: **a pair tree can pass here and
fail the repository's own tests.** Measured 2026-09-19 (`cyc20260919-212912`), on two open
heads merged and judged by this tool's own `_guard_verdict` as `master + #1432@459438e9 +
#1435@8538badc` (tree `510559ec5f50`): HEALTHY, `no stored count` - while that tree's own
`tests/test_bash_tool_unlink_remover.py` failed, 1 of the 53 tests collected from the two
files the PRs change, because one PR's test pinned a spelling the other PR changed. A later commit on #1432's branch aligned that
row and the pair is green (the same heads land tree `f00f2e9d`). So a clean pair is not yet
"the pair is safe to land": the suite question is `check-merge-plan-suite.py A B`, which
builds the tree those heads produce and runs the tests on it - measured green on the pair
above, 3859 passed and 22 skipped. This tool answers the count-line class, that one answers
the suite, and neither answers the other's question.

Measured 2026-09-13 (`cyc20260913-091152`) on the six PRs that were `MERGEABLE`/`CLEAN`
at the time, all 15 pairs in both orders (30 measurements of `master -> A -> B`):

    1173 -> 1174:  DANGER - clean merge, but the tree FAILS: documents 1564 but 1566 are collected
    1174 -> 1173:  DANGER - clean merge, but the tree FAILS: documents 1564 but 1566 are collected

    the other 14 ordered pairs: CONFLICT - git blocks them, which is the safe outcome

`#1173` and `#1174` had each been re-measured against the same master, so both wrote
`1564`; git keeps one copy of the line with no conflict, while the merged tree collects
`1566` (both PRs add two tests). Every other signal said the pair was fine: both PRs were
`MERGEABLE`/`CLEAN`, both double-green, and each was individually safe
(`check-merge-sequence.py` reported `OK - documents 1564` for each on its own). Merging
them in sequence would have looked routine twice and left master red.

Why a plan is not enough
------------------------
`check-merge-sequence.py` with no arguments plans every open PR in ascending order and
stops at the first conflict, because a step's input is the previous step's tree. On the
queue above that is step 1, so the dangerous pair - at positions 11 and 12 - was never
reached: the tool measured `0 of 13 step(s)`. Stopping is right for a plan; it also means
the DANGER search is blind exactly when the queue is conflict-heavy, which is the normal
state of this queue. Pairs do not depend on the plan surviving, so this tool measures them
directly.

Why measurement and not a heuristic
-----------------------------------
The tempting shortcut is to compare the PRs' count lines and flag equal values. That would
have found this instance, and would be wrong as a rule: the property is "a derived fact
merged silently", and the count line is only today's instance of it (the same file family
has already had the duplicate-content, count-rebreakdown and locale-decode variants).
Reading the guards' verdict on the actually-merged tree is what the sibling tools settled
on, and it does not need the derived fact to be a count. The primitives are imported from
`check-merge-sequence.py` rather than copied, so "materialise a merge" and "judge a tree"
keep one spelling.

Cost
----
Quadratic: `m` PRs give `m * (m - 1)` ordered pairs, each needing a guard run over the
merged tree (~2 s), plus one merge of `master + A` per A (cached, not recomputed per pair).
Six PRs is about a minute; thirteen is about ten. Pass the PR numbers you care about, which
is usually the mergeable ones - not all of them.

Usage
-----
    # scan the pairs you are choosing between
    python3 scripts/check-merge-pairs.py 1142 1166 1170 1172 1173 1174

    # every open PR (quadratic: the cost is printed before the work starts)
    python3 scripts/check-merge-pairs.py

    # start from a ref other than master
    python3 scripts/check-merge-pairs.py --base origin/master 1173 1174

A `--base` naming a remote-tracking ref is resolved **by full name** (`refs/remotes/…`),
and a name that denotes only a same-named local branch is refused: `git rev-parse
origin/master` would otherwise pick up a stray local `origin/master` branch and every
verdict below would be about a base the caller never named. The printed base line names
the ref actually measured. Plain branch names, tags and SHAs are passed through unchanged.

Naming the ref correctly is only half of that defect; the other half is *when* the name
was read. That ref is therefore **refreshed** before it is read, by the sibling's own
`_refresh_base` (imported, not copied, for the reason above) - the same call
`check-merge-tree-health.py`, `check-merge-sequence.py` and `check-merge-landing-diff.py`
make. Measured in a hermetic clone whose `refs/remotes/origin/master` sat one commit
behind the remote (`cyc20260914-000319`), same state, same fakes, only that call toggled.
It is pinned as the real-git arm in `tests/test_check_merge_pairs.py`, and the commit
dates there are fixed, so both shas reproduce on every run:

    without the refresh:  base 450c0138 (refs/remotes/origin/master)   <- the stale commit
    with it:              base fd8cb9d1 (refs/remotes/origin/master)   <- the remote's tip

Every verdict this tool prints is `base -> A -> B`, so a stale base makes the whole
measurement - and the `master + A` cached first step under it - about a tree the caller
did not name. A base that cannot be refreshed is exit 2, never an answer.

Exit codes
----------
    0  every ordered pair was answered and none merges cleanly into a failing tree
       (a pair blocked by a conflict is *answered*: the pair cannot land, so it cannot
       land badly - that is why this is 0 here and 3 in `check-merge-sequence.py`, where
       a conflict leaves later steps of a chain unmeasured)
    1  at least one ordered pair merges cleanly and lands a tree that fails the guards
    2  the question could not be answered (git/gh/guard failure, or a requested PR
       number whose head cannot be fetched) - fail loud; never
       report "no dangerous pair" about pairs that were not measured
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
from pathlib import Path

# The sibling tool's primitives, loaded from its file rather than imported by name: the
# scripts in this directory are not importable modules (hyphenated names, no package),
# and this is the same loader the test suite already uses for them.
_SCRIPT = Path(__file__).resolve().parent / "check-merge-sequence.py"


def _load_sibling():
    spec = importlib.util.spec_from_file_location("check_merge_sequence", _SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - file is in this repo
        raise RuntimeError(f"could not load {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seq = _load_sibling()

MeasurementError = seq.MeasurementError


def _resolve_base(ref: str) -> str:
    """The ref actually to measure, resolved by full name - or a refusal.

    `git rev-parse origin/master` consults `refs/heads/origin/master` **before**
    `refs/remotes/origin/master`, so one stray local branch of that name silently
    replaces the base and every verdict below is then about a merge nobody asked
    for. Measured 2026-09-13 (`cyc20260913-102231`), with master at `5f0ee34`: after
    `git branch origin/master 633a777`, this tool printed

        base 633a7779 (origin/master)
        #1173 -> #1174: DANGER - clean merge, but the tree FAILS: documents 1564 ...

    i.e. the historical pair and a two-cycle-old base - a *true* answer about a base
    the caller never named, which is the failure shape this whole tool family is
    about. (The printed SHA is what exposes it; the ref name alone does not.)

    So a remote-tracking name is resolved **by full name**, bypassing the short-name
    search entirely, and a name that denotes *only* a local branch is refused rather
    than measured. Plain branch names, tags and SHAs are passed through untouched,
    since for those the short name is what the caller meant.

    Resolving it is the half that names the ref; the other half is *when* its commit is
    read, and that is `seq._refresh_base`, called by `main` on the ref this returns. The
    two are separate on purpose: a caller that asks for the fully-qualified ref still
    gets it read at whatever moment the checkout last fetched, which is the same
    wrong-tree defect one dimension over (`cyc20260914-000319`).
    """
    if ref.startswith("refs/") or "/" not in ref:
        return ref
    remote = f"refs/remotes/{ref}"
    try:
        seq._rev_parse(remote)
    except MeasurementError:
        local = f"refs/heads/{ref}"
        try:
            seq._rev_parse(local)
        except MeasurementError:
            return ref  # neither form exists: let the caller's own error explain it
        raise MeasurementError(
            f"--base {ref!r} denotes only the local branch {local!r}; there is no "
            f"remote-tracking {remote!r}, so the tree this would measure is a local "
            f"branch that merely looks like a remote, not the base you named. Fetch "
            f"the remote or pass the fully-qualified ref you mean"
        )
    return remote


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check every ordered pair of PRs for a clean merge that lands a tree the "
            "repo's guards reject."
        )
    )
    parser.add_argument(
        "prs",
        nargs="*",
        type=int,
        help="PR numbers to scan (default: every open PR - quadratic, cost is printed)",
    )
    parser.add_argument("--repo", default="argszero/emrg", help="owner/name")
    parser.add_argument("--base", default="origin/master", help="the ref to merge onto")
    args = parser.parse_args(argv)

    try:
        base_ref = _resolve_base(args.base)
        # Refreshed *after* it is resolved to the ref the caller named, and before it is
        # read: every PR head is fetched below, so the pairs are answered about the heads
        # as they are now, while an unrefreshed base answers about the moment this
        # checkout last fetched - the two halves of one question taken at two times.
        # `base_ref` is passed, not `args.base`: resolving first keeps the refusal below
        # intact (a short name that denotes only a stray local branch is refused, never
        # refreshed into existence), and a resolved remote-tracking ref is a spelling the
        # sibling refreshes. A SHA, tag or local branch is returned untouched by it.
        seq._refresh_base(base_ref)
        base = seq._rev_parse(base_ref)
        # Deduplicated and ascending: a repeated number would otherwise pair a PR with
        # itself's twin and buy the same answer twice, and the ordering makes the output
        # diffable between runs.
        numbers = sorted(set(args.prs or seq._open_pr_numbers(args.repo)))
        # A number that is not an open PR (closed, merged, or mistyped) fetches no
        # head. It used to be taken verbatim: a lone number produces no pairs at all,
        # so the loop below never ran, the summary line claimed "1 PR(s) -> 0 ordered
        # pair(s)" about a PR that does not exist, and the run exited 0 - "no
        # dangerous pair" about a PR that was never measured, which is the reading the
        # exit-code contract above rules out. Resolving each head once, here, makes
        # such a number fail loud as rc 2. The resolved heads are reused below, so
        # this costs no extra fetches - and every pair is now measured against one
        # snapshot of each head rather than a fresh fetch per pair.
        heads = {n: seq._fetch_head(n) for n in numbers}
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    pairs = [(a, b) for a in numbers for b in numbers if a != b]
    print(f"base {base[:8]} ({base_ref})")
    print(
        f"pairs: {len(numbers)} PR(s) -> {len(pairs)} ordered pair(s), "
        f"each measured as {base_ref} -> A -> B, judged by {seq.GUARD}"
    )

    # `master + A` is the same merge for every B, so it is materialised once per A rather
    # than once per pair (m merges instead of m * (m - 1)). None means A cannot land onto
    # the base at all, in which case no pair starting with A is reachable today.
    first_step: dict[int, str | None] = {}
    dangers: list[tuple[int, int]] = []
    clean_healthy = 0
    blocked = 0

    with tempfile.TemporaryDirectory(prefix="emrg-merge-pairs-") as tmp:
        workdir = Path(tmp) / "tree"
        for a, b in pairs:
            try:
                if a not in first_step:
                    first_step[a] = seq._merge_commit(base, heads[a])
                landed_a = first_step[a]
                if landed_a is None:
                    blocked += 1
                    continue
                landed_b = seq._merge_commit(landed_a, heads[b])
                if landed_b is None:
                    blocked += 1
                    continue
                ok, report = seq._guard_verdict(
                    seq._run(["git", "rev-parse", f"{landed_b}^{{tree}}"]).stdout.strip(),
                    workdir,
                )
            except MeasurementError as exc:
                print(f"  #{a} -> #{b}: could not measure: {exc}", file=sys.stderr)
                return 2
            if ok:
                clean_healthy += 1
            else:
                dangers.append((a, b))
                print(f"  #{a} -> #{b}: DANGER - clean merge, but the tree FAILS: {report}")

    print()
    if dangers:
        print(
            "DANGEROUS PAIRS: "
            + ", ".join(f"#{a} -> #{b}" for a, b in dangers)
            + "\ngit reports these merges CLEAN and each PR is individually safe: only the "
            "pair is not. Landing one of them first is what makes the other dangerous (a "
            "re-push re-measures it against the new base), so pick one, land it, and "
            "re-run this before landing the next."
        )
        return 1
    print(
        f"no ordered pair merges cleanly into a failing tree "
        f"({clean_healthy} clean and healthy, {blocked} blocked by a conflict; "
        f"judged by {seq.GUARD} alone - the suite is check-merge-plan-suite.py's question)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
