#!/usr/bin/env python3
"""Every open PR number, ascending -- one question, one reader.

Why this module exists
----------------------
Six places needed this list, and each carried its own copy of the same four lines:
`check-merge-landing-diff.py`, `check-merge-order.py`, `check-merge-plan-suite.py`,
`check-merge-sequence.py` (which `check-merge-pairs.py` borrows) and
`check-merge-tree-health.py`. A copy of a rule is a copy of its bugs, and all six
copies had one, because they had all been copied from each other:

    proc = _run([... "pr", "list", ..., "--jq", ".[].number"])
    if proc.returncode != 0:
        raise MeasurementError(f"gh pr list failed: {proc.stderr.strip()}")
    numbers = [int(line) for line in proc.stdout.split() if line.strip()]
    if not numbers:
        raise MeasurementError("no open PRs reported - nothing to check")

Two of the three answers `gh` can give are handled. The third -- **output that is not
a list of numbers** -- reaches `int()`, which raises a bare `ValueError`. Measured
2026-10-03 (`cyc20261003-023102`) with a stand-in `gh` on `PATH` and no positional
arguments, so each gate's default source ran:

    $ GH_PAYLOAD='oops not a number' python3 scripts/check-merge-order.py
    ValueError: invalid literal for int() with base 10: 'oops'
    ... exit code: 1

    $ GH_PAYLOAD='[]' python3 scripts/check-merge-plan-suite.py
    ValueError: invalid literal for int() with base 10: '[]'
    ... exit code: 1

Exit **1** is each of those gates' code for *the finding*, and no finding was
measured:

    check-merge-order.py        1  at least one PR conflicts with the base
    check-merge-tree-health.py  1  at least one clean merge produced a tree that
                                   FAILS the guard
    check-merge-sequence.py     1  at least one clean step landed a tree that FAILS it
    check-merge-landing-diff.py 1  at least one path reads backwards
    check-merge-plan-suite.py   1  the plan's final tree was built and its suite FAILED

Each of their own exit tables names this exact case and gives it **2**:

    check-merge-order.py        2  the measurement could not be made (gh/git failed,
                                   **unparseable output**, ...)
    check-merge-tree-health.py  2  the question could not be answered (gh/git/guard failure)
    check-merge-sequence.py     2  the question could not be answered (git/gh/guard failure, ...)
    check-merge-landing-diff.py 2  the question could not be answered (git/gh failure)
    check-merge-plan-suite.py   2  the question could not be answered (git/gh failure, ...)

so the prose named the case the code missed, six times, in six files that could not
see each other's copy. The family has spent five PRs (#1210, #1212, #1213, #1215,
#1216) repairing one defect five times for the same reason; `merge_tree.py` was that
collapse, and `tests/test_merge_tree_is_the_only_reading.py` is the rule that keeps
it collapsed. This is the same collapse for the sixth fact the family reads.

Only the ascending form lives here
----------------------------------
`review-queue.py::open_prs` asks `gh` the same question but publishes a *different
answer* -- "every open PR number, in the order GitHub lists them (newest first)" --
and its report is printed in that order. It is not folded in here, because an
ascending reader would silently reorder that report, and it does not need to be: it
reads the JSON list rather than the `--jq` lines and refuses a shape it cannot use
(`cyc20261003-015225`). The rule this module carries is about the *ascending numbers
the merge gates share*.

What this reads
---------------
`gh pr list --state open --json number --jq '.[].number'` -- the projection the six
gates already used, kept rather than replaced by a plain `--json number`, because the
two forms answer a stub differently on the input an existing test pins: a `gh` that
prints **nothing** is "no open PRs" under the `--jq` form (an empty list prints
nothing) and *malformed JSON* under the plain one.
`tests/test_check_merge_sequence.py::test_an_empty_open_pr_list_is_refused_at_its_source`
measures the first, and a reader that changed the wire shape under it would be
measuring a different thing.

This module has no exit codes. It answers with a list or raises `MeasurementError`,
and each caller keeps its own table -- which is the point: the caller's table says
what an unreadable list must become, and this module is what makes that reachable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

# The shared module's directory, so `import merge_tree` works however this file is
# loaded: as `python3 scripts/pr_numbers.py` it is already `sys.path[0]`, but the test
# suite loads these tools by file path (`spec_from_file_location`), where it is not.
# `merge_tree.py` is a module of this repo, not a dependency.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_tree  # noqa: E402  (needs the path above)

# The question could not be answered. Never a verdict. An *alias*, not a subclass:
# a gate that catches this name has to catch what the shared module raises too.
MeasurementError = merge_tree.MeasurementError

#: How many of the queue's rows are read. One number for every caller: two limits
#: would be two answers to "how much of the queue counts", and the gates' exit tables
#: describe the same subject.
LIMIT = 100

#: The projection, named once. `--jq` is what turns the JSON array into one number per
#: line; a second spelling of it anywhere in this family would be a second home for
#: the same question, which is the defect this module was created to end.
JQ = ".[].number"


def open_pr_numbers(
    repo: str,
    run: Callable[..., subprocess.CompletedProcess],
) -> list[int]:
    """Every open PR number, ascending.

    Three answers, and only two of them are a list of numbers:

    * `gh` refused the call -- its own message is carried, and the caller turns it
      into exit 2;
    * `gh` answered with lines that are not numbers -- the lines are quoted, because a
      reader who cannot see what came back cannot tell a proxy's error page from a
      `gh` that is really `pr`;
    * `gh` printed nothing -- an empty queue, which the callers refuse as well (their
      default source is `args.prs or _open_pr_numbers(...)`, so an empty list must not
      be able to become "a plan of zero steps measured").

    `run` is the caller's runner, passed in rather than owned here, exactly as
    `merge_tree` takes it: `encoding`/`errors` are pinned per caller (`check-doc-count.py`
    records why -- a locale mismatch leaves `stdout` as `None` after the reader thread
    swallows the decode error, and the `None` surfaces later as a bare `TypeError` past
    every handler), and a runner owned here would be a second place that decision lives.
    """
    argv = [
        "gh",
        "pr",
        "list",
        "-R",
        repo,
        "--limit",
        str(LIMIT),
        "--state",
        "open",
        "--json",
        "number",
        "--jq",
        JQ,
    ]
    proc = run(argv)
    if proc.returncode != 0:
        raise MeasurementError(f"gh pr list failed: {proc.stderr.strip()}")
    numbers: list[int] = []
    for token in proc.stdout.split():
        try:
            numbers.append(int(token))
        except ValueError:
            # The whole output, not just the first bad token: a `gh` that answered an
            # error page has its first line here, and one that answered a JSON document
            # has its opening characters. Truncated, because this travels in a message.
            raise MeasurementError(
                f"`gh pr list --state open` answered {proc.stdout.strip()[:200]!r}, which "
                "is not a list of PR numbers, so the queue could not be read - this is "
                "not an empty queue (that answer is `gh` printing nothing)"
            ) from None
    if not numbers:
        raise MeasurementError("no open PRs reported - nothing to check")
    return sorted(numbers)
