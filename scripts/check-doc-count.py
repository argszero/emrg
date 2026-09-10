#!/usr/bin/env python3
"""Sync Agent.md's documented Python test count with the real collection.

Usage
-----
    uv run --no-sync python3 scripts/check-doc-count.py           # report drift, exit 1
    uv run --no-sync python3 scripts/check-doc-count.py --write   # rewrite Agent.md
    uv run --no-sync python3 scripts/check-doc-count.py --dry-run # show the change

`--write` and `--dry-run` are mutually exclusive: one repairs, the other must not
write, so the pair is rejected outright rather than silently resolved in favour
of one of them (measured before this was enforced: `--write --dry-run` printed
the dry-run line, wrote nothing, and exited 0 - the requested action was dropped
without a word).

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

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "Agent.md"

# The anchor `tests/test_doc_counts.py::test_python_count_matches_docs` keys on.
# tests/test_check_doc_count.py extracts that guard's own pattern and asserts the
# two agree on the real Agent.md, so they cannot drift apart unnoticed.
COUNT_LINE = re.compile(r"(?P<head>uv run pytest tests/ -v` \()(?P<count>\d+)(?P<tail>\))")

COLLECTED = re.compile(r"(\d+) tests? collected")

# The one spelling of "run this tool" that every hint in this repo prints: this
# module's two hints, the pytest guard's failure message, and Agent.md's doc
# line. Measured 2026-09-10 (cyc20260910-191242) in the main clone: this form
# exits 0, while the bare `python3` form the drift hint used to print exits 2
# without measuring anything - the host's `python3` has no pytest, so a hint
# spelled that way sends the reader straight into a second failure. A hint is
# only worth printing if it runs; keep the spelling here and let
# tests/test_check_doc_count.py prove the other sites agree with it.
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
        raise DocCountError(
            f"pytest --collect-only failed (rc={proc.returncode}):\n"
            + (proc.stdout[-2000:] + proc.stderr[-2000:]).strip()
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
    args = parser.parse_args(argv)

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
