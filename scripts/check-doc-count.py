#!/usr/bin/env python3
"""Sync Agent.md's documented Python test count with the real collection.

Usage
-----
    uv run --no-sync python3 scripts/check-doc-count.py           # report drift, exit 1
    uv run --no-sync python3 scripts/check-doc-count.py --write   # rewrite Agent.md
    uv run --no-sync python3 scripts/check-doc-count.py --dry-run # show the change
    uv run --no-sync python3 scripts/check-doc-count.py --resolve-conflict

`--write` and `--dry-run` are mutually exclusive: one repairs, the other must not
write, so the pair is rejected outright rather than silently resolved in favour
of one of them (measured before this was enforced: `--write --dry-run` printed
the dry-run line, wrote nothing, and exited 0 - the requested action was dropped
without a word).

`--resolve-conflict` is for the state `--write` cannot act on. During a merge the
count line arrives wrapped in `<<<<<<<`/`=======`/`>>>>>>>` markers, so the doc
holds *two* counts and plain `--write` refuses (rc=2, correctly - it will not
guess which is real). The resolution is not to pick a side: both sides are stale
by construction, which is exactly why they conflicted. This mode removes the
marker block, keeps the surrounding structure, and then writes the number
*measured on the merged tree*, so the value never comes from either side of the
conflict. Measured 2026-09-11: three consecutive unblocks of PRs that had gone
dirty after a merge each needed that strip-markers-then-measure dance by hand.
It refuses when the conflict is anywhere other than the count line, so it
cannot be used as a general "delete the markers" button.

Run it with the project interpreter: the measurement shells out to pytest, so a
bare `python3` that cannot import pytest fails loud with that reason rather than
reporting a bogus count.

Why this exists
---------------
`Agent.md` documents how many Python tests the tree collects, and
`tests/test_doc_counts.py::test_python_count_matches_docs` fails when the doc and
the collection disagree. The guard is correct, but it only *reports*: the number
has to be re-measured by hand whenever tests are added or removed, and every
merge of a test-adding branch conflicts on that single line. On 2026-09-10,
`#1119`, `#1120`, `#1121` and `#1122` each carried a different value and each
merge conflicted on it. A conflicted merge is the worst case: both sides are
stale by construction, so neither value is right and git cannot say which one
to keep. The resolver ends up measuring the merged tree anyway.

This is the host-side half of that CI check, in the same shape as
`scripts/bump-version.py --check` for the version sources: one command that
measures the tree in front of you and, with `--write`, repairs the doc. It
prints the number it measured, so the value never comes from arithmetic or
from memory of what the count "should" be.

`--write` is a strict single-token replacement: the surrounding text is
re-measured before and after and asserted unchanged, so a wrong anchor cannot
corrupt Agent.md.

Scope, stated rather than implied: this tool covers the Python count only. The
GUI and renderer per-file breakdowns on the following lines are guarded by
`tests/test_doc_counts.py` too, but they are not written here - they change
only when GUI tests are added, and measuring them needs the renderer toolchain.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

def _resolve_root() -> Path:
    """The tree to measure: the checkout the caller is *standing in*.

    Derived from the cwd when that is a checkout, not from `__file__`. Measured
    2026-09-11, in exactly the situation this tool exists for: unblocking a PR
    means working in a git worktree, where running the main checkout's copy of
    this script reported `OK: Agent.md documents 1420` while the worktree's own
    `Agent.md` said 1401 - it had read the wrong tree and called it consistent.
    `--write` in that position edits the *other* checkout, which is how a
    confirm-step silently corrupts a tree the caller was not looking at.

    Falling back to the script's own root keeps `python3 scripts/...` working from
    anywhere (the documented invocation), and a mismatch is stated rather than
    silently resolved: if the cwd is a checkout that is not this script's root,
    the caller gets told which tree answered, because "which tree did you measure"
    is the one thing this tool must never leave ambiguous.
    """
    here = Path(__file__).resolve().parent.parent
    cwd = Path.cwd()
    if (cwd / "Agent.md").is_file() and (cwd / "scripts").is_dir():
        return cwd
    return here


REPO_ROOT = _resolve_root()
DOC = REPO_ROOT / "Agent.md"

# The anchor `tests/test_doc_counts.py::test_python_count_matches_docs` keys on.
# tests/test_check_doc_count.py extracts that guard's own pattern and asserts the
# two agree on the real Agent.md, so they cannot drift apart unnoticed.
COUNT_LINE = re.compile(r"(?P<head>uv run pytest tests/ -v` \()(?P<count>\d+)(?P<tail>\))")

COLLECTED = re.compile(r"(\d+) tests? collected")

# A full conflict block: `<<<<<<< label`, both sides, `>>>>>>> label`. Kept as one
# pattern with named sides so the resolver can be driven without a real merge.
CONFLICT_BLOCK = re.compile(
    r"^<<<<<<<[^\n]*\n(?P<ours>.*?)^=======\n(?P<theirs>.*?)^>>>>>>>[^\n]*\n",
    re.S | re.M,
)

# `merge.conflictStyle = diff3` (and `zdiff3`) inserts a `||||||| <base>` section
# between the two sides - measured 2026-09-11 on this machine with a real
# `git merge`: the base line is a third copy of the conflicted line. The subtle
# part, also measured: `CONFLICT_BLOCK` does *not* simply fail to match this
# layout - it matches while swallowing the base line into `ours`, so the two
# captured sides are `"<ours>\n||||||| <base>"` and `"<theirs>"`. The block is
# therefore misread as a content conflict: the reader is told the sides "differ
# by more than the count", which is false (all three differ only in the number)
# and points them at hand-picking a side, the one repair this tool exists to
# prevent. Detecting the layout *before* matching turns that into an accurate
# refusal. Do not reorder these two checks.
CONFLICT_BASE_SECTION = re.compile(r"^\|\|\|\|\|\|\|[^\n]*\n", re.M)

# The one spelling of "run this tool" that every hint in this repo prints: this
# module's two hints, the pytest guard's failure message, and Agent.md's doc
# line. Measured 2026-09-10 (cyc20260910-191242) in the main clone: this form
# exits 0, while the bare `python3` form the drift hint used to print exits 2
# without measuring anything - the host's `python3` has no pytest, so a hint
# spelled that way sends the reader straight into a second failure. A hint is
# only worth printing if it runs; keep the spelling here and let
# tests/test_check_doc_count.py prove the other sites agree with it.
#
# Scope of that measurement, added 2026-09-13 (`cyc20260913-122923`) after
# walking into it: "this form exits 0" is a property of a **synced** checkout.
# The measurement above was taken in the main clone, which is synced. In a fresh
# worktree the same spelling exits 2 without measuring anything, because
# `uv run --no-sync` has created an empty `.venv` there and both `python` and
# `python3` resolve to it - so the spelling is necessary but not sufficient, and
# the hint it appears in must not be printed when the environment, not the
# interpreter choice, is what is missing. See the pytest-missing branch in
# `measured_count`.
INVOCATION = "uv run --no-sync python3 scripts/check-doc-count.py"


class DocCountError(Exception):
    """The doc or the tree is not in a shape this tool can act on."""


def measured_count() -> int:
    """How many tests pytest actually collects in this tree.

    `encoding="utf-8"` / `errors="replace"` rather than the locale codec: the
    identical defect was measured in `scripts/check-node-test-count.py` (issue
    #1132, where a locale mismatch left `proc.stdout` as `None` after the decode
    error was swallowed by subprocess's reader thread, and the concatenation
    below raised a bare `TypeError` past every handler). Any collected id or
    warning carrying a non-ASCII byte would do the same here on a cp936 host, so
    the decoding is pinned before that can happen. `push-branch-from-api.py`
    records the same lesson at its line 15.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.stdout is None or proc.stderr is None:
        raise DocCountError(
            "pytest --collect-only produced no readable output "
            "(its output could not be decoded)"
        )
    if proc.returncode != 0:
        detail = (proc.stdout[-2000:] + proc.stderr[-2000:]).strip()
        # Two causes, two remedies - and the second one is *not* "use the right
        # interpreter". Measured 2026-09-13 (`cyc20260913-122923`) in a fresh
        # review worktree: `uv run --no-sync` there had produced an empty `.venv`
        # (`site-packages` holding only `_virtualenv.pth` and `_virtualenv.py`),
        # and `python` and `python3` both resolve to it - so the invocation this
        # tool prints as its own remedy failed with byte-identical output, rc 2,
        # sending the reader in a circle. Reading that output as a wrong
        # interpreter is the misdiagnosis: there is no interpreter in this
        # checkout that has pytest, so advising a different one cannot help.
        if "No module named pytest" in detail:
            raise DocCountError(
                "pytest is not installed in the interpreter running this tool "
                f"({sys.executable}), so no test was collected:\n"
                + detail
                + "\n\nThis is an unsynced checkout, not a wrong-interpreter "
                "problem - a fresh worktree or clone gets an empty `.venv`, and "
                "`python` and `python3` both resolve to it, so re-running "
                f"`{INVOCATION}` here fails identically. Run `uv sync` in this "
                "checkout first, or run this tool from a checkout whose "
                "environment is already synced."
            )
        raise DocCountError(
            f"pytest --collect-only failed (rc={proc.returncode}):\n"
            + detail
            + "\n\nhint: run this with the project interpreter, e.g."
            f" `{INVOCATION}`"
        )
    match = COLLECTED.search(proc.stdout)
    if not match:
        raise DocCountError(
            "could not parse a collected count from pytest output:\n"
            + proc.stdout[-2000:].strip()
        )
    return int(match.group(1))


def documented_count(text: str) -> int:
    """The Python test count Agent.md claims. Fails loud unless it is unambiguous."""
    matches = list(COUNT_LINE.finditer(text))
    if not matches:
        raise DocCountError(
            f"no documented Python count found in {DOC.name} "
            "(expected the anchor: uv run pytest tests/ -v` (NNNN))"
        )
    if len(matches) > 1:
        raise DocCountError(
            f"{len(matches)} documented Python counts found in {DOC.name}; "
            "exactly one is expected, and this tool will not guess which one is real"
        )
    return int(matches[0].group("count"))


def resolve_conflict(text: str) -> str:
    """Strip the count line's conflict block, leaving the structure, not a side.

    Refuses unless the conflict is *exactly* the count line: if any other part of
    the document is conflicted, resolving here would silently drop whichever
    lines lost - and git, not this tool, is what should decide that. The check is
    therefore structural: after removing the block, the document must be free of
    conflict markers, and the two sides must be the same line modulo the number.

    Returns the unresolved text (markers gone, count still ambiguous) so the
    caller can print a real `documented -> measured` transition - resolving and
    measuring are separate steps because the number must come from the tree, not
    from either side.
    """
    if CONFLICT_BASE_SECTION.search(text):
        raise DocCountError(
            "the conflict uses the diff3 layout (`||||||| <base>`), which this "
            "tool does not parse: it cannot tell whether the conflict is the "
            "count line, so it must not resolve it. Re-merge with git's "
            "default layout (`git config merge.conflictStyle merge` and "
            "recreate the conflict), or resolve by hand"
        )
    matches = list(CONFLICT_BLOCK.finditer(text))
    if not matches:
        raise DocCountError(
            f"no conflict block found in {DOC.name}; nothing to resolve "
            "(use --write for a plain drift)"
        )
    if len(matches) > 1:
        raise DocCountError(
            f"{len(matches)} conflict blocks found in {DOC.name}; this tool only "
            "resolves the count line - resolve the others by hand"
        )

    block = matches[0]
    ours, theirs = block.group("ours"), block.group("theirs")
    if COUNT_LINE.search(ours) is None or COUNT_LINE.search(theirs) is None:
        raise DocCountError(
            "the conflict is not the count line; this tool only resolves that "
            "line - resolve this one by hand"
        )

    # The two sides must be the same line but for the number, or this is a
    # conflict about content and picking either side would be a real choice.
    def masked(side: str) -> str:
        return COUNT_LINE.sub(lambda m: m.group("head") + "#" + m.group("tail"), side)

    if masked(ours) != masked(theirs):
        raise DocCountError(
            "the conflicted line differs by more than the count, so this is a "
            "content conflict and the tool must not choose a side (the block "
            "either changes text other than the number, or spans more than the "
            "one count line)"
        )

    resolved = text[: block.start()] + ours + text[block.end() :]
    if CONFLICT_BLOCK.search(resolved) or ">>>>>>>" in resolved:
        raise DocCountError(
            "conflict markers remain outside the count line; resolve by hand"
        )
    return resolved


def patch(text: str, measured: int) -> str:
    """Replace the documented count. Only that number may change."""
    match = COUNT_LINE.search(text)
    if match is None:
        raise DocCountError(f"no documented Python count found in {DOC.name}")
    patched = text[: match.start("count")] + str(measured) + text[match.end("count") :]
    # Guard the edit: everything except the number must be byte-identical.
    mask = COUNT_LINE.sub(lambda m: m.group("head") + "#" + m.group("tail"), text)
    masked_patched = COUNT_LINE.sub(
        lambda m: m.group("head") + "#" + m.group("tail"), patched
    )
    if mask != masked_patched:
        raise DocCountError(
            "refusing to write: the patch would change more than the count"
        )
    return patched


def _resolve_conflict_mode() -> int:
    """`--resolve-conflict`: strip the count line's conflict, then measure.

    The measurement happens *after* the markers are gone, on the tree as it
    stands, so the written number is the merged tree's own - not ours, not
    theirs, not a remembered value.
    """
    try:
        text = DOC.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {DOC}: {exc}", file=sys.stderr)
        return 2

    try:
        resolved = resolve_conflict(text)
    except DocCountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        documented = documented_count(resolved)
        measured = measured_count()
    except DocCountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        patched = patch(resolved, measured)
        DOC.write_text(patched, encoding="utf-8")
    except DocCountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot write {DOC}: {exc}", file=sys.stderr)
        return 2

    print(
        f"resolved {DOC.name}: conflict block removed, "
        f"{documented} -> {measured} (measured on the merged tree)"
    )
    print("Next: uv run --no-sync pytest tests/test_doc_counts.py -q")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage\n-----", 1)[0].strip(),
        epilog=__doc__.split("Usage\n-----", 1)[-1].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write",
        action="store_true",
        help="rewrite Agent.md with the measured count",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="show the change without writing",
    )
    mode.add_argument(
        "--resolve-conflict",
        action="store_true",
        help=(
            "resolve a conflicted count line by measurement: strip the conflict "
            "block and write the count measured on the merged tree (refuses if "
            "the conflict is anywhere other than the count line)"
        ),
    )
    args = parser.parse_args(argv)

    # Say which tree answered. A tool whose whole job is "measure the tree you are
    # about to merge" must not leave "which tree" ambiguous - the 2026-09-11 defect
    # was precisely a confident `OK` about a checkout the caller was not in.
    print(f"tree: {REPO_ROOT}")

    if args.resolve_conflict:
        return _resolve_conflict_mode()

    try:
        text = DOC.read_text(encoding="utf-8")
        documented = documented_count(text)
        measured = measured_count()
    except DocCountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot read {DOC}: {exc}", file=sys.stderr)
        return 2

    if documented == measured:
        print(f"OK: {DOC.name} documents {measured} collected Python tests")
        return 0

    print(
        f"FAIL: {DOC.name} documents {documented} Python tests "
        f"but {measured} are collected"
    )
    if not (args.write or args.dry_run):
        print(f"\nFix with: {INVOCATION} --write")
        return 1

    if args.dry_run:
        print(f"\n(dry run - would set {documented} -> {measured}, no files written)")
        return 0

    try:
        patched = patch(text, measured)
        DOC.write_text(patched, encoding="utf-8")
    except DocCountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot write {DOC}: {exc}", file=sys.stderr)
        return 2

    print(f"\nupdated {DOC.name}: {documented} -> {measured}")
    print("Next: uv run --no-sync pytest tests/test_doc_counts.py -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
