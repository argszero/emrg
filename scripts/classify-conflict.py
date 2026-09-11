#!/usr/bin/env python3
"""Classify merge-conflict blocks and say what the correct resolution is.

Why this exists (measured, cycle `cyc20260911-112155`): while unblocking the PR
queue after a merge, two conflicts of the *same shape* — "both sides edited this
file" — needed **opposite** resolutions, and telling them apart took several
rounds of hand measurement (`comm`, `diff`, counting `def test_` by hand):

* **#1136**: the branch carried an unmerged *duplicate* of #1134's tests (it had
  been built on #1134, which was then squash-merged, so git could no longer see
  the common ancestry). Master's copy was a strict superset — measured, zero
  lines existed only in the branch's copy — so **taking master's side** was
  correct, and it removed a duplicate rather than losing coverage.
* **#1140**: the sides were **disjoint** additions (this branch's
  `_resolve_root()` probes vs master's argv/decode probes, for the same test
  files). Either side-pick would have silently dropped four of the branch's own
  probes; the only correct resolution is to **keep both**.

A blanket `--theirs` is right for the first and wrong for the second. The
distinction is measurable, so it should not have to be re-derived by hand in
every cycle that runs this cascade — which, while the queue is deep, is every
cycle.

This tool is a **decision aid, not an automatic resolver**. It never edits a
file: it classifies each conflict block and prints the resolution the evidence
supports, so the class is explicit and reviewable instead of inferred. The one
case it cannot decide (`overlapping`) is the one where a human must read both
sides.

Usage:
    python3 scripts/classify-conflict.py <file> [<file> ...]
    python3 scripts/classify-conflict.py --all        # every unmerged path

Exit codes:
    0  every block classified, none needs a human decision
    1  at least one block is `overlapping` (human must decide)
    2  usage error / no conflict blocks found
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# A conflict block. Deliberately the same shape as `check-doc-count.py`'s
# CONFLICT_BLOCK: the opening marker, then ours, then the separator, then
# theirs, then the closing marker. `(?!<<<<<<<)`-style guards are unnecessary
# here because we scan one block at a time with a non-greedy body.
CONFLICT_BLOCK = re.compile(
    r"^<{7} (?P<ours_label>.*?)\n(?P<ours>.*?)^={7}\n(?P<theirs>.*?)^>{7} (?P<theirs_label>.*?)$",
    re.MULTILINE | re.DOTALL,
)

# A single line that differs from another only by an integer. The repo's
# Agent.md count lines are the recurring case ("... (1401) — import check: ...").
_NUMBER = re.compile(r"\d+")

# Symbols a hunk *declares*. This is the axis that decides duplicate-vs-disjoint,
# and it is not the same as "which text lines are shared": my first version
# compared content lines and classified the two real #1140 conflicts as
# `overlapping` because both sides happened to contain `    """` and `    )` -
# structural boilerplate, not work. Measured on the same blocks, the declared
# names are fully disjoint, which is the property that actually matters.
_SYMBOL = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", re.MULTILINE)

# Classification labels.
IDENTICAL = "identical"
DUPLICATE = "duplicate"
DISJOINT = "disjoint"
COUNT_LINE = "count-line"
OVERLAPPING = "overlapping"


def _content_lines(text: str) -> list[str]:
    """The lines that carry content: trailing whitespace kept out, blanks out.

    Blank lines are omitted because indentation/blank-line churn between two
    sides is not evidence of either duplication or of a conflict; comparing
    them would classify identical code as `overlapping`.
    """
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def _differ_only_by_number(ours: list[str], theirs: list[str]) -> bool:
    """True when both sides are one line each and differ only in an integer."""
    if len(ours) != 1 or len(theirs) != 1:
        return False
    a, b = ours[0], theirs[0]
    if a == b:
        return False
    return _NUMBER.sub("#", a) == _NUMBER.sub("#", b)


def _symbols(text: str) -> set[str]:
    """Names of the functions/classes a hunk declares (empty for non-code)."""
    return set(_SYMBOL.findall(text))


def classify(ours_text: str, theirs_text: str) -> tuple[str, str]:
    """Return (label, recommendation) for one conflict block.

    The `duplicate` test is a *subset* test over declared symbols, in both
    directions, so the recommendation can name which side is the superset. That
    is the measurement that decided #1136: it is not "one side is shorter", it
    is "every name one side declares is present in the other".

    When a hunk declares no symbols (a doc, a config, a prose line) the axes fall
    back to content lines, which is all there is to compare.
    """
    ours = _content_lines(ours_text)
    theirs = _content_lines(theirs_text)

    if ours == theirs:
        return IDENTICAL, "both sides are byte-identical; take either"

    if _differ_only_by_number(ours, theirs):
        return (
            COUNT_LINE,
            "differs only by a number - MEASURE on the merged tree, never pick a "
            "side (both sides are stale by construction); for Agent.md's test "
            "count use `check-doc-count.py --resolve-conflict`",
        )

    ours_syms, theirs_syms = _symbols(ours_text), _symbols(theirs_text)

    if ours_syms or theirs_syms:
        # Declared symbols are the evidence; shared boilerplate is not.
        only_ours = ours_syms - theirs_syms
        only_theirs = theirs_syms - ours_syms
        if not only_ours and not only_theirs:
            return (
                OVERLAPPING,
                "both sides declare the SAME names - a human must read both sides "
                "and reconcile the bodies (this is a real edit collision)",
            )
        if not only_theirs:
            return (
                DUPLICATE,
                "every name theirs declares is already ours - take OURS; this is "
                "the unmerged-duplicate shape from a squash-merged base, not lost "
                "work",
            )
        if not only_ours:
            return (
                DUPLICATE,
                "every name ours declares is already theirs - take THEIRS; this is "
                "the unmerged-duplicate shape from a squash-merged base, not lost "
                "work",
            )
        return (
            DISJOINT,
            f"the sides declare different symbols (ours only: "
            f"{len(only_ours)}, theirs only: {len(only_theirs)}) - KEEP BOTH "
            f"(concatenate); either side-pick silently drops one side's work",
        )

    # No declarations to compare: fall back to content lines.
    ours_set, theirs_set = set(ours), set(theirs)
    if not ours_set & theirs_set:
        return (
            DISJOINT,
            "the two sides share no content line - KEEP BOTH (concatenate)",
        )
    if ours_set < theirs_set:
        return (DUPLICATE, "theirs is a strict superset - take THEIRS")
    if theirs_set < ours_set:
        return (DUPLICATE, "ours is a strict superset - take OURS")

    both = ours_set & theirs_set
    return (
        OVERLAPPING,
        f"sides partially overlap ({len(both)} shared line(s)) - a human must read "
        "both sides; this is the one case no rule can decide",
    )


def conflicts_in(text: str) -> list[tuple[str, str, str]]:
    """All conflict blocks in `text` as (ours, theirs, theirs_label)."""
    return [
        (m.group("ours"), m.group("theirs"), m.group("theirs_label"))
        for m in CONFLICT_BLOCK.finditer(text)
    ]


def _unmerged_paths() -> list[str]:
    """Paths git currently reports as unmerged, in index order."""
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [line for line in (proc.stdout or "").splitlines() if line.strip()]


def _report(path: str, text: str) -> list[str]:
    """Classify one file; return the labels found (for the exit code)."""
    blocks = conflicts_in(text)
    if not blocks:
        print(f"{path}: no conflict blocks")
        return []

    labels: list[str] = []
    for i, (ours, theirs, label) in enumerate(blocks, 1):
        kind, advice = classify(ours, theirs)
        labels.append(kind)
        print(f"{path}: block {i}/{len(blocks)} -> {kind}")
        print(f"    ours   {len(_content_lines(ours))} content line(s)   (HEAD)")
        print(f"    theirs {len(_content_lines(theirs))} content line(s)   ({label})")
        print(f"    {advice}")
    return labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Classify merge-conflict blocks and report the resolution the "
            "evidence supports (never edits files)."
        ),
    )
    parser.add_argument("paths", nargs="*", help="conflicted files to classify")
    parser.add_argument(
        "--all",
        action="store_true",
        help="classify every path git reports as unmerged",
    )
    args = parser.parse_args(argv)

    paths = list(args.paths)
    if args.all:
        paths.extend(_unmerged_paths())
    # de-duplicate, preserving order
    seen: set[str] = set()
    paths = [p for p in paths if not (p in seen or seen.add(p))]

    if not paths:
        print(
            "error: no paths given (pass files, or --all for every unmerged path)",
            file=sys.stderr,
        )
        return 2

    all_labels: list[str] = []
    for path in paths:
        p = Path(path)
        if not p.is_file():
            print(f"{path}: not a file", file=sys.stderr)
            return 2
        all_labels.extend(_report(path, p.read_text(encoding="utf-8", errors="replace")))

    if not all_labels:
        print("error: no conflict blocks found in any given path", file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    for label in all_labels:
        counts[label] = counts.get(label, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"\nsummary: {summary}")

    if OVERLAPPING in counts:
        print(
            "at least one block needs a human decision (overlapping) - this tool "
            "does not guess"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
