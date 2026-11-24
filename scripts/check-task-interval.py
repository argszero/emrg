#!/usr/bin/env python3
"""The pace a task block states, against the round-up the competition template requires.

Usage
-----
    uv run --no-sync python3 scripts/check-task-interval.py
    uv run --no-sync python3 scripts/check-task-interval.py --tasks <PATH>
    uv run --no-sync python3 scripts/check-task-interval.py --tasks <PATH> --quiet

Why this exists
---------------
A competition task's `extra_prompt` states a submission allowance as `n` and then a pace in
seconds as `interval`. `emrg/server/competition_prompt.md` fixes what that pace must be:
`86400 / (n + 1)`, rounded **up**, with the reason given in its own words - a gap shorter
than the division allows spends the attempt the ledger is trying to keep.

Nothing measured it. The reading was done by eye, once per cycle, and it answered wrongly
twice in a single cycle (issue #3171):

* **The first `interval = N` in a block is not the block's answer.** A block states its
  no-allowance fallback (`interval = 86400`) *before* the value it actually computed, so a
  reading that takes the first match reports the fallback and calls it the block's pace.
  The block's own value is the **last** one it states, and that is what this file reads.
* **Counting the word `floor(` is not counting the defect.** On the host this was written
  for, 19 of 39 blocks write `floor(86400 / (n + 1))` and it is tempting to report those as
  the ones that round down. It is false both ways: 17 of the 19 have an `n` that divides
  86400 evenly, so the word changes nothing for them, while **all four blocks that really
  do land one second short never write the word at all** - they state the truncated number
  by hand. The word and the slip are disjoint sets, so a report built on the word names the
  wrong blocks and misses the right ones.

What it distinguishes, and why the classes are four and not two
--------------------------------------------------------------
A stated value that is not the formula's result is **not automatically a fault**. A host
order may cap the pace below or above it - on this host one block is throttled to two runs
a day and another to one run every two days - and the template's precedence rule governs
that case rather than the arithmetic. So the value is compared with the division's two
neighbours rather than with a single expected number:

* ``divides evenly`` - the division is exact **and the block states it**, so the rounding
  question never arises and the word ``floor`` is inert. The block's own pace is read here
  like everywhere else: an exact division it does not state is not agreement, and it falls
  to the last class below.
* ``rounded up`` - not exact, and the block states the ceiling, which is what the template
  asks for.
* ``one second short`` - not exact, and the block states the **floor**: this is the defect
  the template's own sentence is about, and it is the only class that fails.
* ``a pace from another source`` - neither neighbour, so the number did not come from this
  formula at all (a host cap, an allowance read from somewhere else, or a value stated
  against a division that happens to be exact). Reported and counted, never called
  agreement and never called a defect: which it is depends on an order this file cannot
  read.
* ``not a pacing block`` - states neither an allowance nor a pace (a promote or scouting
  task). It makes no claim to check, so it is named and counted rather than failing the run;
  reading it as unmeasurable would leave exit ``0`` unreachable on a host that runs one.

Exit codes
----------
``0`` nothing is short; ``1`` at least one block states the truncated value; ``2`` the task
table or a block could not be measured. A block this file cannot parse is ``2`` and never a
pass - the rule every guard here applies to a question it cannot answer.

What it reads, and what it never does
-------------------------------------
One file: the task table, the host's own, named before any verdict. It **measures and never
repairs** - the table is rewritten by the scheduler through its own dumper, and a tool that
wrote to it would be editing the file this reading exists to describe.

The file is asked for **wherever the home-root migration left it** - `~/.emrg/global/tasks.yml`
once it has run, `~/.emrg/tasks.yml` before - because the daemon reads it from `config_dir()`,
which answers the former on a migrated host. Resolving the default is not a convenience: a
constant spelled the pre-migration way made every run on this host answer exit ``2`` with "No
such file", so the pace was never measured at all (issue #3181).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

EXIT_CLEAN = 0
EXIT_SHORT = 1
EXIT_UNMEASURABLE = 2

#: The task table spelled the way it was **before** the home-root migration. Not the default
#: subject on its own: :func:`resolve_tasks_path` turns it into whichever spelling the host
#: actually has, which is what the daemon reads.
PRE_SPLIT_TASKS = "~/.emrg/tasks.yml"

#: The home-level root, and the directory the root migration puts everything EMRG owns under.
#: Both are spelled here because this script runs stdlib-only apart from `yaml`, so it cannot
#: ask `emrg.config` or `emrg.home_layout` where a file lives. `check-issue-links.py`,
#: `check-memory-rotation.py` and `check-extra-prompt-rotation.py` carry the same pair and the
#: same three functions below for the same reason, and
#: `tests/test_check_issue_links.py::test_every_script_that_resolves_a_split_default_answers_the_same_way`
#: holds every copy to one set of answers rather than trusting them to stay in step.
ROOT_PREFIX = "~/.emrg/"
GLOBAL_PREFIX = "~/.emrg/global/"


def post_split(path: str) -> str:
    """Where ``path`` lives once the root migration has run.

    The migration moves every entry EMRG owns from ``~/.emrg/<name>`` to
    ``~/.emrg/global/<name>``, keeping the relative path, so the transformation is the one
    prefix and nothing else. :data:`ROOT_PREFIX` itself is untouched: it is the root, not an
    entry under it, which is why a path that does not start with it comes back as it was.
    """
    if not path.startswith(ROOT_PREFIX):
        return path
    return GLOBAL_PREFIX + path[len(ROOT_PREFIX):]


def prefer_existing(pre_split: str, post_split_path: str) -> str:
    """The one of the two that is on disk, preferring the migrated path."""
    try:
        return post_split_path if Path(post_split_path).expanduser().exists() else pre_split
    except OSError:
        return pre_split


def resolve_default(path: str) -> str:
    """``path``'s post-migration twin when that file exists, and ``path`` otherwise.

    Resolved against the **file**, not against the ``global/`` directory: a host can have the
    directory without the table in it, and the directory is then the wrong thing to ask.
    """
    return prefer_existing(path, post_split(path))


def resolve_tasks_path(override=None) -> Path:
    """The task table: ``--tasks`` when given, else the host's own wherever it is.

    Read **per call**, so the answer follows the host it is asked about rather than the state
    of the tree when this module was imported. An override is taken as given - a caller naming
    a path is measuring that path.
    """
    if override is not None:
        return Path(os.path.expanduser(str(override)))
    return Path(resolve_default(PRE_SPLIT_TASKS)).expanduser()

#: The value the template divides: one day, in seconds.
DAY = 86400

#: A block states its allowance as `n = <int>` and its pace as `interval = <int>`, in
#: running text in either language, so both are found by value rather than by line.
_N_RE = re.compile(r"n\s*=\s*(\d+)")
_INTERVAL_RE = re.compile(r"interval\s*=\s*(\d+)")

#: The classes. Only `SHORT` fails; `OTHER` is a pace this formula did not produce, and
#: `OUT_OF_SCOPE` is a block that makes no pacing claim at all - a promote or scouting task
#: states neither an allowance nor a pace, and reading that as "could not measure" would
#: make a clean verdict unreachable on a host that legitimately runs both kinds.
EVEN = "divides evenly"
UP = "rounded up"
SHORT = "one second short"
OTHER = "a pace from another source"
OUT_OF_SCOPE = "not a pacing block"
UNREADABLE = "could not measure"


def block_of(record: dict) -> str:
    """The block a record renders, by the precedence the reader applies.

    `config.extra_prompt` is canonical; a block written at the record's top level renders
    too, but only when `config` carries none. Measured on this checkout: config-only answers
    the config block, top-level-only answers the top-level block, both answer the config
    block. So a reading that looked only at `config` would miss a block that renders.
    """
    config = record.get("config") or {}
    block = config.get("extra_prompt") or ""
    if block.strip():
        return block
    return record.get("extra_prompt") or ""


def stated_allowance(block: str) -> int | None:
    """The `n` the block states, or `None` when it states none."""
    found = _N_RE.search(block)
    return int(found.group(1)) if found else None


def stated_interval(block: str) -> int | None:
    """The pace the block states - its **last** one, which is the value it computed.

    The first is the no-allowance fallback for blocks that carry one, and reading it as the
    answer is the first of the two failures this file exists to prevent.
    """
    found = _INTERVAL_RE.findall(block)
    return int(found[-1]) if found else None


def classify(block: str) -> tuple[int | None, int | None, int | None, str, str]:
    """`(n, stated, expected, verdict, why)` for one block.

    `expected` is the division's **ceiling**, the value the template asks for; `n` and
    `stated` are `None` when the block does not say.
    """
    n = stated_allowance(block)
    stated = stated_interval(block)
    if stated is None:
        if n is None:
            return None, None, None, OUT_OF_SCOPE, "it states neither an allowance nor a pace"
        return n, None, None, UNREADABLE, "it states an allowance and no pace"

    if n is None:
        # No allowance stated: the template's own no-allowance branch is a full day, and a
        # block that says so states 86400. Any other number came from somewhere else.
        if stated == DAY:
            return None, stated, DAY, EVEN, "no allowance stated, paced at one day"
        return None, stated, DAY, OTHER, "no allowance stated, and not a day either"

    floor_value, remainder = divmod(DAY, n + 1)
    expected = floor_value + 1 if remainder else floor_value
    if remainder == 0:
        # An exact division settles the *rounding* question, not the pace. The block still
        # states a number, and a stated value that differs from the division is a pace this
        # formula did not produce - reading the exactness as agreement would report a gap
        # half the allowance as clean.
        if stated == expected:
            return n, stated, expected, EVEN, "the division is exact and the block states it"
        return n, stated, expected, OTHER, "the division is exact and the block states another pace"
    if stated == expected:
        return n, stated, expected, UP, "the division is not exact and the value is its ceiling"
    if stated == floor_value:
        return n, stated, expected, SHORT, "the division is not exact and the value is its floor"
    return n, stated, expected, OTHER, "neither neighbour of the division"


def rows(tasks_path: str) -> tuple[list[tuple[str, int | None, int | None, int | None, str, str]], str]:
    """Every block's reading, or a single reason the table could not be measured."""
    import yaml  # imported here so `--help` answers on an interpreter without it

    path = Path(tasks_path).expanduser()
    try:
        records = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [], f"{path}: {exc}"
    except yaml.YAMLError as exc:  # a table that does not parse is not a table with no blocks
        return [], f"{path}: {exc.__class__.__name__}"
    if not isinstance(records, list):
        return [], f"{path}: not a list of task records"

    out = []
    for record in records:
        if not isinstance(record, dict):
            continue
        block = block_of(record)
        if not block.strip():
            continue
        name = str(record.get("name") or "<unnamed>")
        n, stated, expected, verdict, why = classify(block)
        out.append((name, n, stated, expected, verdict, why))
    return out, ""


def _cell(value: int | None) -> str:
    return "-" if value is None else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare each task block's stated interval with the template's round-up.",
    )
    parser.add_argument(
        "--tasks",
        default=None,
        help="the task table (default: the host's own, wherever the root migration left it)",
    )
    parser.add_argument("--quiet", action="store_true", help="print only the blocks that are not exact")
    args = parser.parse_args(argv)

    tasks_path = resolve_tasks_path(args.tasks)
    print(f"tasks: {tasks_path}")

    readings, failure = rows(str(tasks_path))
    if failure:
        print(f"  {failure}")
        print("could not measure: the task table was not read, which is not a clean result")
        return EXIT_UNMEASURABLE

    print(f"  {len(readings)} block(s) render a task supplement")
    print(f"  {'task':38} {'n':>3} {'stated':>7} {'round-up':>8}  verdict")

    shown = [r for r in readings if r[4] != EVEN] if args.quiet else readings
    for name, n, stated, expected, verdict, why in shown:
        print(f"  {name:38} {_cell(n):>3} {_cell(stated):>7} {_cell(expected):>8}  {verdict}")
        if verdict in (SHORT, OTHER, UNREADABLE):
            print(f"  {'':38} {'':3} {'':7} {'':8}  ^ {why}")

    counts = {v: sum(1 for r in readings if r[4] == v)
              for v in (EVEN, UP, SHORT, OTHER, OUT_OF_SCOPE, UNREADABLE)}
    print(
        f"blocks: {counts[EVEN]} divide evenly, {counts[UP]} rounded up, "
        f"{counts[SHORT]} one second short, {counts[OTHER]} from another source, "
        f"{counts[OUT_OF_SCOPE]} not a pacing block, {counts[UNREADABLE]} could not measure"
    )

    if counts[UNREADABLE]:
        print(
            "a block this file cannot parse is not a block that agrees: name its allowance and "
            "its pace, or read the table's shape again before treating the rest as clean"
        )
        return EXIT_UNMEASURABLE
    if counts[SHORT]:
        print(
            "the template rounds the interval UP, because a gap shorter than the division allows "
            f"spends the attempt the ledger is keeping: {counts[SHORT]} block(s) state its floor"
        )
        return EXIT_SHORT
    print(
        "no block states the interval's floor: every non-exact division is rounded up, and every "
        "other value is a pace this formula did not produce"
    )
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
