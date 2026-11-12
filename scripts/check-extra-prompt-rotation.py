#!/usr/bin/env python3
"""Which task `extra_prompt` blocks moved since the rotation table classified them.

Usage
-----
    uv run --no-sync python3 scripts/check-extra-prompt-rotation.py
    uv run --no-sync python3 scripts/check-extra-prompt-rotation.py --all
    uv run --no-sync python3 scripts/check-extra-prompt-rotation.py --tasks <PATH> --table <PATH>
    uv run --no-sync python3 scripts/check-extra-prompt-rotation.py --init > <table>

Why this exists
---------------
The evolution prompt's section 1.7 rotates one task's `extra_prompt` per cycle and classifies the
blocks it finds, and the classification costs a cycle's round: `ios-app-task` alone is 6,327
characters that have to be read against a 381-line template before anything can be said about
them. The rotation's own clock is therefore the **length list** - a block whose length is
unchanged has not been re-read, so there is nothing to classify.

That rule was prose, and it decayed three times. A cycle read the lengths, saw them unchanged,
opened a body anyway and re-derived a row the table already carried; `cyc20261112-000449` and
`cyc20261112-051604` recorded the same waste, the second one after the rule had been written
down as a cure, and this cycle (`cyc20261112-092741`) did it a third time - reading 704
characters of `feishu-inbox` to reach the identical "0 general-method gaps, three carriers"
that the table's row already stated. Prose asking a reader to compare eight numbers by eye is
not a rule; comparing them is what this file does.

It is the same shape `check-memory-rotation.py` answers for section 1.5: a step every cycle takes,
whose hand-written version answered wrongly while reading as clean.

What it reads, and what it prints first
--------------------------------------
Two subjects, both named before any verdict:

* the **task table** (`--tasks`, default `~/.emrg/tasks.yml`) - the records whose
  `extra_prompt` may render;
* the **rotation table** (`--table`, default
  `.emrg/memory/task-extra-prompt-rotation.md`) - one Markdown row per classified block,
  carrying the block's length as it stood when that row was written.

Both carriers of the field are read, in the precedence the prompt states: `config.extra_prompt`
renders, and a record whose `config` carries none renders a top-level `extra_prompt` instead. A
record carrying both renders the `config` copy and is reported as carrying a second one, because
that is the copy a classification would silently miss (issue #2110). A block is never invisible
for sitting at the top level: `feishu-inbox`'s eight siblings on this host all render from
`config`, and a reader that looked only at top level would have reported "no blocks at all" -
which is a reading this file's own development produced, and why the two carriers are read in
one place.

Per block:

    unchanged      renders a non-empty block and its length is the row's number
    duplicate      as `unchanged`, but the record carries a second copy the precedence hides
    changed        renders a non-empty block and its length is not that number
    unclassified   renders a non-empty block and no row names its task
    gone           a row names it and it renders nothing (or its task is not in the table)

`changed`, `unclassified` and `gone` are the states that ask for a cycle's work; `unchanged`
is the ordinary answer and costs nothing. A `gone` row is not tidiness: a block deleted from
`tasks.yml` and left in the table keeps a classification alive for text that no longer renders.

**`duplicate` is printed without `--all` even though its length is unchanged**, because what it
reports is not a reading that moved but a record carrying two copies of a field where one
renders - the copy a classification would silently miss (issue #2110). It does not fail the
reading: the rendered text is exactly what the row classified, so there is nothing to
re-classify. Its remedy is on the installation's side, not the rotation's.

Exit codes
----------
    0  every non-empty block has a row and its length is unchanged - the section 1.7 reading
    1  a block moved, a block has no row, or a row's block is gone
    2  nothing was measured: the task table, the rotation table, or the YAML inside it could
       not be read, or the table carries no row this file can parse

`0` is reserved for a reading that covered every block, and a question the file cannot answer
is **2**, never `0` - the prompt's own rule for every reading, and the reason a missing table
cannot look like a clean rotation.

Bootstrapping a host that has no table
-------------------------------------
The rotation table is an installation's own memory, and nothing creates it: a checkout that has
never run a cycle has no row for anything, so the first reading on it answers **2**. Naming the
missing path without a next step is the dead end this file exists to remove, so **`--init` prints
a skeleton** - the table header and one row per block that renders, carrying that block's length
as it stands - for the caller to write to the table's path and then classify. It is printed and
never written: recording what a block turned out to be is a reader's judgement, and a tool that
filled in its own rows would be recording classifications nobody made.

What it does not do
-------------------
It **measures and never repairs**: it does not touch `~/.emrg/tasks.yml`, which is the host's
file, and it does not rewrite the rotation table, because recording a classification needs a
reader's judgement about what the block turned out to be. It prints the lengths; the cycle
decides what a moved one means.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The home of the rotation table this repository writes. It is the evolution agent's own
#: memory, which is untracked state rather than source - a checkout that has never run a
#: cycle does not have it, and that is answered as exit **2** rather than as a clean rotation.
DEFAULT_TABLE = REPO_ROOT / ".emrg" / "memory" / "task-extra-prompt-rotation.md"

#: The default task table, which is this host's. `--tasks` exists so the reading can be taken
#: on a fixture, and the default is never exercised by a test.
DEFAULT_TASKS = Path("~/.emrg/tasks.yml")

#: One row of the rotation table: `| `task` | 6327 | class | evidence |`. The length is
#: matched as a plain integer because that is what this file compares; the class and evidence
#: cells are prose for a reader and are not parsed at all.
TABLE_ROW = re.compile(r"^\|\s*`(?P<task>[^`]+)`\s*\|\s*(?P<chars>\d+)\s*\|")


def rendered_blocks(records):
    """`{task: (renders, length, both)}` for every record, per the field's precedence.

    `renders` is the block text that actually reaches the prompt, `length` is its length, and
    `both` says the record carries a second copy that the precedence hides. All three are
    returned rather than just the length so the report can name a duplicate carrier instead of
    quietly measuring one of the two.
    """
    blocks = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get("name")
        if not isinstance(name, str):
            continue
        config = record.get("config") or {}
        from_config = (config.get("extra_prompt") if isinstance(config, dict) else "") or ""
        from_top = record.get("extra_prompt") or ""
        renders = from_config if from_config else from_top
        blocks[name] = (renders, len(renders), bool(from_config and from_top))
    return blocks


def table_rows(text):
    """`{task: chars}` from the rotation table's rows, in the order they are written."""
    rows = {}
    for line in text.splitlines():
        match = TABLE_ROW.match(line)
        if match:
            rows[match.group("task")] = int(match.group("chars"))
    return rows


def classify(blocks, rows):
    """One `(state, task, detail)` per block and per orphan row, in a stable order.

    The states are the four the docstring names. A block that renders nothing is left out
    entirely - it has no text to classify - while a row whose block renders nothing is
    reported, because that row is the one still claiming a classification.
    """
    findings = []
    for task, (renders, length, both) in blocks.items():
        if not renders:
            continue
        if task not in rows:
            findings.append(("unclassified", task, f"{length} chars, no table row"))
            continue
        if rows[task] != length:
            findings.append(
                ("changed", task, f"table says {rows[task]}, block is {length}")
            )
            continue
        if both:
            findings.append(
                (
                    "duplicate",
                    task,
                    f"{length} chars unchanged, but config and top level both carry a copy "
                    f"and only the config one renders",
                )
            )
            continue
        findings.append(("unchanged", task, f"{length} chars, unchanged"))
    for task, chars in rows.items():
        renders = blocks.get(task, ("", 0, False))[0]
        if not renders:
            findings.append(("gone", task, f"table says {chars} chars, nothing renders"))
    return findings


def _load_tasks(path):
    """The task records, or `None` when they cannot be read - a caller's exit 2.

    `yaml` is imported here rather than at module scope so a missing dependency is a reported
    failure of this reading instead of an import error before the subject is ever named.
    """
    try:
        import yaml
    except ImportError:
        print("cannot measure: PyYAML is not importable here", file=sys.stderr)
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, UnicodeDecodeError) as error:
        print(f"cannot measure: {path} is unreadable ({error})", file=sys.stderr)
        return None
    except Exception as error:  # the format's own parse error, which is not an OSError
        print(f"cannot measure: {path} does not parse ({error})", file=sys.stderr)
        return None
    if data is None:
        print(f"cannot measure: {path} holds no records", file=sys.stderr)
        return None
    if not isinstance(data, list):
        print(
            f"cannot measure: {path} holds a {type(data).__name__}, not a list of records",
            file=sys.stderr,
        )
        return None
    return data


def _load_table(path):
    """The rotation table's text, or `None` when it cannot be read - a caller's exit 2."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"cannot measure: {path} is unreadable ({error})", file=sys.stderr)
        return None


def init_table(blocks):
    """The rotation table's skeleton, one row per block that renders.

    The lengths are this host's, which is the point: the skeleton is only useful for the task
    table it was printed from, and a reader classifies each row against the block it names.
    """
    rows = sorted(
        (task, length) for task, (renders, length, _both) in blocks.items() if renders
    )
    lines = [
        "# The section 1.7 rotation table.",
        "#",
        "# One row per task `extra_prompt` block that renders. The number is that block's length",
        "# when the row was written; a block whose length is unchanged has not moved, so there is",
        "# nothing to classify.",
        "#",
        "# Replace `**unclassified**` with what the block turned out to be: `**already in",
        "# template**`, `**instance configuration**`, or `**missing from template**`.",
        "",
        "| task | chars | class | evidence |",
        "|---|---|---|---|",
    ]
    for task, length in rows:
        lines.append(f"| `{task}` | {length} | **unclassified** | printed by --init |")
    return "\n".join(lines) + "\n"


def _print_init(records, tasks_path, table_path):
    """`--init`: print the skeleton for this task table. Exit 2 when nothing renders."""
    blocks = rendered_blocks(records)
    if not any(renders for renders, _length, _both in blocks.values()):
        print(
            f"cannot measure: {tasks_path} carries no record with a rendering extra_prompt",
            file=sys.stderr,
        )
        return 2
    sys.stdout.write(init_table(blocks))
    print(f"# write this to {table_path}", file=sys.stderr)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Report which task extra_prompt blocks moved since the section 1.7 rotation table "
            "classified them. Exit 0 = nothing moved, 1 = something needs classifying, "
            "2 = the reading could not be taken."
        )
    )
    parser.add_argument(
        "--tasks",
        type=Path,
        default=DEFAULT_TASKS,
        help="the task table to read (default: %(default)s)",
    )
    parser.add_argument(
        "--table",
        type=Path,
        default=DEFAULT_TABLE,
        help="the section 1.7 rotation table (default: %(default)s)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="list every block, including the unchanged ones",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help=(
            "print a skeleton rotation table for this task table, one row per block that "
            "renders, to be written to the table's path and classified"
        ),
    )
    args = parser.parse_args(argv)

    tasks_path = Path(os.path.expanduser(str(args.tasks)))
    table_path = Path(os.path.expanduser(str(args.table)))

    records = _load_tasks(tasks_path)
    if records is None:
        return 2

    if args.init:
        return _print_init(records, tasks_path, table_path)

    text = _load_table(table_path)
    if text is None:
        print(
            "the rotation table is this host's memory and nothing creates it: run `--init` to "
            f"print a skeleton for every block that renders, write it to {table_path}, then "
            "classify its rows",
            file=sys.stderr,
        )
        return 2

    rows = table_rows(text)
    if not rows:
        print(
            f"cannot measure: {table_path} carries no row this file can parse "
            f"(expected `| `<task>` | <chars> | ... |`)",
            file=sys.stderr,
        )
        return 2

    blocks = rendered_blocks(records)
    findings = classify(blocks, rows)

    print(f"tasks: {tasks_path} ({len(records)} record(s))")
    print(f"table: {table_path} ({len(rows)} row(s))")

    rendered = [f for f in findings if f[0] != "gone"]
    unchanged = [f for f in findings if f[0] == "unchanged"]
    duplicate = [f for f in findings if f[0] == "duplicate"]
    changed = [f for f in findings if f[0] in ("changed", "unclassified")]
    gone = [f for f in findings if f[0] == "gone"]

    for state, task, detail in findings:
        if state == "unchanged" and not args.all:
            continue
        print(f"  {state:13} {task:32} {detail}")

    print(
        f"blocks: {len(rendered)} render, {len(unchanged)} unchanged, "
        f"{len(duplicate)} duplicate, {len(changed)} need classifying, "
        f"{len(gone)} row(s) with nothing to render"
    )

    if changed or gone:
        print(
            "classify the block(s) above in the rotation table before re-reading any "
            "unchanged one - a block whose length is unchanged has not moved"
        )
        return 1
    print("every block renders unchanged text: the section 1.7 reading is the length list itself")
    return 0


if __name__ == "__main__":
    sys.exit(main())
