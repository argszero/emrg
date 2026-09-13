#!/usr/bin/env python3
"""Check every *ordered pair* of PRs for a clean merge that lands a failing tree.

The question this exists for
----------------------------
The sibling gates answer about one PR (`check-vote-count.py`, `check-merge-freshness.py`)
or about one *plan* (`check-merge-sequence.py`). In a queue of near-identical PRs the
question that decides what to do next is neither: **is any pair of these silently
dangerous together?** - where "silently" means git reports the merge clean and the
resulting tree fails the repo's own guards.

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

Exit codes
----------
    0  every ordered pair was answered and none merges cleanly into a failing tree
       (a pair blocked by a conflict is *answered*: the pair cannot land, so it cannot
       land badly - that is why this is 0 here and 3 in `check-merge-sequence.py`, where
       a conflict leaves later steps of a chain unmeasured)
    1  at least one ordered pair merges cleanly and lands a tree that fails the guards
    2  the question could not be answered (git/gh/guard failure) - fail loud; never
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
        base = seq._rev_parse(args.base)
        # Deduplicated and ascending: a repeated number would otherwise pair a PR with
        # itself's twin and buy the same answer twice, and the ordering makes the output
        # diffable between runs.
        numbers = sorted(set(args.prs or seq._open_pr_numbers(args.repo)))
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    pairs = [(a, b) for a in numbers for b in numbers if a != b]
    print(f"base {base[:8]} ({args.base})")
    print(
        f"pairs: {len(numbers)} PR(s) -> {len(pairs)} ordered pair(s), "
        f"each measured as {args.base} -> A -> B"
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
                    first_step[a] = seq._merge_commit(base, seq._fetch_head(a))
                landed_a = first_step[a]
                if landed_a is None:
                    blocked += 1
                    continue
                landed_b = seq._merge_commit(landed_a, seq._fetch_head(b))
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
        f"({clean_healthy} clean and healthy, {blocked} blocked by a conflict)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
