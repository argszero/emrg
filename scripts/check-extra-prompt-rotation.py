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
them. The rotation's own clock is therefore a **fingerprint** - the block's length and the
sha256 of its text - so a block that has not moved is not re-read and there is nothing to
classify.

That rule was prose, and it decayed three times. A cycle read the lengths, saw them unchanged,
opened a body anyway and re-derived a row the table already carried; `cyc20261112-000449` and
`cyc20261112-051604` recorded the same waste, the second one after the rule had been written
down as a cure, and this cycle (`cyc20261112-092741`) did it a third time - reading 704
characters of `feishu-inbox` to reach the identical "0 general-method gaps, three carriers"
that the table's row already stated. Prose asking a reader to compare eight numbers by eye is
not a rule; comparing them is what this file does.

It is the same shape `check-memory-rotation.py` answers for section 1.5: a step every cycle takes,
whose hand-written version answered wrongly while reading as clean.

A length is a proxy for the text, and a guard that measures a proxy cannot see an edit that keeps
the proxy. A block rewritten to the same number of characters read `unchanged` here, so it was
never re-classified and the rotation quietly stopped covering it. So a row carries the sha256 of
the text it classified, computed from the block itself and from nothing a reader wrote down, and
a row written before those digests existed is reported as **unable to answer** rather than as
unchanged - the rule this file already applies to every question it cannot measure.

What it reads, and what it prints first
--------------------------------------
Two subjects, both named before any verdict:

* the **task table** (`--tasks`, default the host's own store: `~/.emrg/global/tasks.yml`
  once the home-root migration has run, `~/.emrg/tasks.yml` before it) - the records whose
  `extra_prompt` may render;
* the **rotation table** (`--table`, default `.emrg/memory/task-extra-prompt-rotation.md`,
  or `.emrg/memory/ref/task-extra-prompt-rotation.md` once the memory store has migrated its
  detail files into `ref/`) - one Markdown row per classified block,
  carrying the block's length and the sha256 of its text as they stood when that row was
  written.

Both carriers of the field are read, in the precedence the prompt states: `config.extra_prompt`
renders, and a record whose `config` carries none renders a top-level `extra_prompt` instead. A
record carrying both renders the `config` copy and is reported as carrying a second one, because
that is the copy a classification would silently miss (issue #2110). A block is never invisible
for sitting at the top level: `feishu-inbox`'s eight siblings on this host all render from
`config`, and a reader that looked only at top level would have reported "no blocks at all" -
which is a reading this file's own development produced, and why the two carriers are read in
one place.

Per block:

    unchanged      renders a non-empty block and the row's length and sha256 are both its own
    duplicate      as `unchanged`, but the record carries a second copy the precedence hides
    changed        renders a non-empty block and its length is not that number
    rewritten      the length is the row's number and the sha256 is not: a same-length edit
    placeholder    as `unchanged`, but the row's class cell is still `--init`'s
                   `**unclassified**`, so the row records a fingerprint and no classification
    undigested     the row carries no sha256, so it cannot answer the question at all
    unclassified   renders a non-empty block and no row names its task
    gone           a row names it and it renders nothing (or its task is not in the table)

`changed`, `rewritten`, `placeholder`, `unclassified` and `gone` ask for a **reading** - a block
has to be opened and classified, or a row whose block is gone has to be retired. `undigested`
asks for a **row edit** and no reading at all: the row already says what that block turned out to
be, and what it lacks is the digest the output prints beside it. The two are counted apart for
that reason, and each is given its own remedy. `unchanged` is the ordinary answer and costs
nothing.
A `gone` row is not tidiness: a block deleted from
`tasks.yml` and left in the table keeps a classification alive for text that no longer renders.

**`placeholder` is a positive marker, and its absence is not a claim.** The only value this file
recognises as "nobody classified this" is the exact string `--init` prints, because that string
is written by the tool itself and cannot be a reader's paraphrase. A row whose class cell is
empty - a table written before the column existed, or a cell someone cleared - keeps the reading
it had: this instrument can see a marker, and it cannot see the absence of one, so reporting an
empty cell as unclassified would be inventing a fact from silence.

**`duplicate` is printed without `--all` even though its length is unchanged**, because what it
reports is not a reading that moved but a record carrying two copies of a field where one
renders - the copy a classification would silently miss (issue #2110). It does not fail the
reading: the rendered text is exactly what the row classified, so there is nothing to
re-classify. Its remedy is on the installation's side, not the rotation's.

Exit codes
----------
    0  every non-empty block has a row and its length and sha256 are unchanged - the section
       1.7 reading
    1  a block moved, a block's text moved under an unchanged length, a row carries no sha256
       to compare, a block has no row, a row's class cell is still `--init`'s placeholder, or a
       row's block is gone
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
and its sha256 as they stand - for the caller to write to the table's path and then classify. It
is printed and
never written: recording what a block turned out to be is a reader's judgement, and a tool that
filled in its own rows would be recording classifications nobody made.

What it does not do
-------------------
It **measures and never repairs**: it does not touch `~/.emrg/global/tasks.yml`, which is the host's
file, and it does not rewrite the rotation table, because recording a classification needs a
reader's judgement about what the block turned out to be. It prints the lengths and the digests;
the cycle decides what a moved one means.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

#: This file's own tree, put on `sys.path` before the one import it takes from the package.
#: A `scripts/*.py` file gets `sys.path[0] = scripts/`, so without this insert `emrg` resolves
#: from whatever the host has **installed** - a released copy, therefore older than the tree
#: this file ships in - and the import below raises `ImportError` on every host running a
#: release, which is every host. Measured 2026-11-22: `python3 -c 'import emrg.memory'` from
#: the repo root answers the checkout while this script's own entry point answered
#: `/Users/…/.emrg/install/source/emrg/memory.py`. `check-memory-rotation.py` and
#: `find-host-message.py` already carry this insert for the same reason.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# The directory name the store keeps its detail files under, imported rather than re-spelled:
# the rule is stated once in `emrg.memory`, and a second spelling of it is what issue #2559 was.
from emrg.memory import MEMORY_REF_DIRNAME  # noqa: E402

#: The rotation table's name. It is the evolution agent's own memory, which is untracked state
#: rather than source - a checkout that has never run a cycle does not have it, and that is
#: answered as exit **2** rather than as a clean rotation.
TABLE_NAME = "task-extra-prompt-rotation.md"


def default_table() -> Path:
    """The rotation table inside this repository's memory store: `ref/` when it is there.

    The store keeps every detail file in `ref/` and leaves only the index at the top
    (`emrg.memory.MEMORY_REF_DIRNAME`), and `MemoryStore.migrate_flat_layout()` moves a store
    that predates that layout the first time it is constructed. This table is a detail file, so
    it walks the rule the store walks - the same one `check-memory-index.py` resolves its rows
    against and `check-cycle-record.py` resolves its two subjects against.

    Measured 2026-11-22 (`cyc20261122-142416`): while the table sat at its pre-migration path
    this reading answered `8 unchanged`, and the moment the store was migrated it answered
    exit 2, `cannot measure: ... is unreadable ([Errno 2] ...)` - for a file that was present
    one directory down. Step 1.7 of a cycle then cannot be taken at all, and the answer it
    prints reads like a missing rotation table rather than a wrong path.

    Resolved at call time, never at import: `REPO_ROOT` is the subject this reads, and a
    fixture that moves it must move the answer with it. `--table` still overrides the whole
    question, which is how a reading is taken on a fixture.
    """
    memory_root = REPO_ROOT / ".emrg" / "memory"
    ref = memory_root / MEMORY_REF_DIRNAME
    return (ref if ref.is_dir() else memory_root) / TABLE_NAME

#: The default task table, which is this host's. `--tasks` exists so the reading can be taken
#: on a fixture, and the default is never exercised by a test.
#:
#: **Which path that is under the host's root is two paths, and the window between them is not
#: hypothetical.** `emrg/home_layout.py` moves everything EMRG owns into `global/`, and the call
#: that performs the move sits in the daemon's own entry point - so a checkout, and every host
#: between one release and the next, keeps its task table at the pre-split path while this
#: script already asked for the migrated one. Measured on this host before the fix, with
#: `~/.emrg/tasks.yml` present and readable and no `~/.emrg/global/` directory at all:
#:
#:     cannot measure: /Users/.../.emrg/global/tasks.yml is unreadable ([Errno 2] ...)
#:
#: - so the whole task-configuration rotation answered "cannot measure" for the length of that
#: window, which reads as "nothing to classify" rather than as a broken instrument (issue
#: #3145). :func:`resolve_tasks_path` is what makes the default ask the file that is there.
PRE_SPLIT_TASKS = "~/.emrg/tasks.yml"

#: The home-level root, and the directory the root migration puts everything EMRG owns under.
#: Both are spelled here because this script runs **without the package** - it is stdlib-only,
#: so it cannot ask `emrg.config` or `emrg.home_layout` where a file lives, and the migration
#: itself lives in the package. `scripts/check-issue-links.py` and
#: `scripts/check-memory-rotation.py` carry the same pair and the same three functions below for
#: the same window, and `tests/test_check_issue_links.py` holds every copy to one set of answers
#: rather than trusting them to stay in step.
ROOT_PREFIX = "~/.emrg/"
GLOBAL_PREFIX = "~/.emrg/global/"


def post_split(path: str) -> str:
    """Where ``path`` lives once the root migration has run.

    The migration moves every entry EMRG owns from ``~/.emrg/<name>`` to
    ``~/.emrg/global/<name>``, keeping the relative path, so the transformation is the one
    prefix and nothing else. :data:`ROOT_PREFIX` itself is untouched: it is the root, not an
    entry under it, which is why a path that does not start with it comes back as it was
    rather than gaining a prefix it never had.
    """
    if not path.startswith(ROOT_PREFIX):
        return path
    return GLOBAL_PREFIX + path[len(ROOT_PREFIX):]


def prefer_existing(pre_split: str, post_split_path: str) -> str:
    """The one of the two that is on disk, preferring the migrated path.

    Split out from :func:`resolve_default` so the choice can be held still by a test: the two
    candidates it takes are ordinary paths, and a fixture can build either without the host's
    own root being involved.
    """
    try:
        return post_split_path if Path(post_split_path).expanduser().exists() else pre_split
    except OSError:
        return pre_split


def resolve_default(path: str) -> str:
    """``path``'s post-migration twin when that file exists, and ``path`` otherwise.

    Resolved against the **file**, not against the ``global/`` directory: a migration that
    stopped part way (it is all-or-nothing, but a host can also have created ``global/`` from
    some other start) leaves the directory present and this file absent, and the directory is
    then the wrong thing to ask.
    """
    return prefer_existing(path, post_split(path))


#: The default task table is **resolved when it is read**, inside
#: :func:`resolve_tasks_path`, and there is deliberately no module-level constant holding the
#: answer: a value frozen at import is one answer for the life of a process, it cannot be asked
#: about a fixture's home, and `argparse`'s `default=` is evaluated at import exactly like one.


def resolve_tasks_path(override=None) -> Path:
    """The task table: ``--tasks`` when given, else the host's own store wherever it is.

    Read **per call**, so the answer follows the host it is asked about rather than the state
    of the tree when this module was imported. An override is taken as given - a caller naming
    a path is measuring that path, and resolving its post-split twin would answer a file the
    caller did not name.
    """
    if override is not None:
        return Path(os.path.expanduser(str(override)))
    return Path(resolve_default(PRE_SPLIT_TASKS)).expanduser()

#: One row of the rotation table: `| `task` | 6327 | <sha256> | class | evidence |`. The length is
#: matched as a plain integer and the digest as 64 hex characters, because those two are what
#: this file compares. The digest cell is optional so a table written before it existed still
#: parses - and that row is then reported as `undigested`, never as `unchanged`.
#:
#: `rest` is everything after the digest, kept only so the **class** cell can be read out of it.
#: The evidence cell stays prose: what a reader wrote as evidence answers whether *they* read the
#: block, which is what the row claims, and a file that re-read their prose would be re-deciding
#: their reading. The class cell is different - it is the one cell whose value this file has to
#: understand, because `--init` writes a value there that is not a classification at all.
TABLE_ROW = re.compile(
    r"^\|\s*`(?P<task>[^`]+)`\s*\|\s*(?P<chars>\d+)\s*\|"
    r"(?:\s*(?P<sha>[0-9a-fA-F]{64})\s*\|(?P<rest>[^\n]*))?"
)

#: What `--init` writes in the class cell of every row it prints, and what `init_table()` tells
#: the reader to replace. One spelling, used by the writer that emits it and the reader that
#: recognises it, so a row written by the tool can never read to the tool as a classification.
#: Measured 2026-11-20 (cycle cyc20261120-235036) on this host: 12 of the table's 25 rows still
#: carried it while the guard reported every one of them `unchanged` and exited 0.
PLACEHOLDER_CLASS = "**unclassified**"


def digest(text):
    """The sha256 of a block's rendered text: the row's second, and stronger, fingerprint.

    Computed from the block and from nothing a reader wrote down. That is what lets an edit which
    keeps the length show up at all - a length is a proxy for the text, and a guard measuring a
    proxy cannot see an edit that keeps the proxy.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    """`{task: (chars, sha256 or None, class cell)}` from the rotation table's rows.

    A `None` digest is a row that cannot say what text it classified - either a table written
    before the column existed or a cell left empty - and it is carried rather than defaulted, so
    `classify` can report the row instead of answering for it.

    The class cell is carried as written, or `""` when the row carries none. It is not
    normalised: `PLACEHOLDER_CLASS` is compared as an exact string, because the one thing this
    file must recognise is the value its own `--init` prints, and a matcher loose enough to
    catch a paraphrase would also catch a reader's classification that quotes it.
    """
    rows = {}
    for line in text.splitlines():
        match = TABLE_ROW.match(line)
        if match:
            sha = match.group("sha")
            rest = match.group("rest") or ""
            rows[match.group("task")] = (
                int(match.group("chars")),
                sha.lower() if sha else None,
                rest.split("|")[0].strip(),
            )
    return rows


def classify(blocks, rows):
    """One `(state, task, detail)` per block and per orphan row, in a stable order.

    The states are the ones the docstring names. A block that renders nothing is left out
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
        chars, sha, class_cell = rows[task]
        if chars != length:
            findings.append(("changed", task, f"table says {chars}, block is {length}"))
            continue
        if sha is None:
            findings.append(
                (
                    "undigested",
                    task,
                    f"{length} chars, but the row carries no sha256, so a same-length edit is "
                    f"invisible to it: {digest(renders)} is this block's",
                )
            )
            continue
        if sha != digest(renders):
            findings.append(
                (
                    "rewritten",
                    task,
                    f"{length} chars unchanged, but the text moved: table says {sha}, "
                    f"block is {digest(renders)}",
                )
            )
            continue
        # Before `duplicate`, because the two ask different questions: `duplicate` is a fact
        # about the installation's task table, while this is the row still saying nobody read
        # the block. A reader told only about the duplicate would leave the row unclassified.
        if class_cell == PLACEHOLDER_CLASS:
            findings.append(
                (
                    "placeholder",
                    task,
                    f"{length} chars match the row, but its class cell is still `--init`'s "
                    f"placeholder: the numbers say the block has not moved, and they say "
                    f"nothing about whether anyone decided what it turned out to be",
                )
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
    for task, (chars, _sha, _class_cell) in rows.items():
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

    The lengths and digests are this host's, which is the point: the skeleton is only useful for
    the task table it was printed from, and a reader classifies each row against the block it
    names.
    """
    rows = sorted(
        (task, length, digest(renders))
        for task, (renders, length, _both) in blocks.items()
        if renders
    )
    lines = [
        "# The section 1.7 rotation table.",
        "#",
        "# One row per task `extra_prompt` block that renders. The number and the digest are that",
        "# block's when the row was written; a block whose length *and* sha256 are unchanged has",
        "# not moved, so there is nothing to classify.",
        "#",
        "# Replace `**unclassified**` with what the block turned out to be: `**already in",
        "# template**`, `**instance configuration**`, or `**missing from template**`.",
        "",
        "| task | chars | sha256 | class | evidence |",
        "|---|---|---|---|---|",
    ]
    for task, length, sha in rows:
        lines.append(
            f"| `{task}` | {length} | {sha} | {PLACEHOLDER_CLASS} | printed by --init |"
        )
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
        default=None,
        help=(
            "the task table to read (default: the host's own store - "
            "~/.emrg/global/tasks.yml once the home-root migration has run, "
            "~/.emrg/tasks.yml before it)"
        ),
    )
    parser.add_argument(
        "--table",
        type=Path,
        default=None,
        help=(
            "the section 1.7 rotation table (default: this checkout's memory store, which is "
            ".emrg/memory/ref/ once the store has migrated its detail files there, and "
            ".emrg/memory/ before it has)"
        ),
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
            "renders, carrying its length and sha256, to be written to the table's path and "
            "classified"
        ),
    )
    args = parser.parse_args(argv)

    tasks_path = resolve_tasks_path(args.tasks)
    table_path = (
        Path(os.path.expanduser(str(args.table))) if args.table is not None else default_table()
    )

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
    changed = [
        f
        for f in findings
        if f[0] in ("changed", "unclassified", "rewritten", "placeholder")
    ]
    # The remedy differs from `changed`'s even though the action is the same: a `changed` row was
    # classified once and has drifted, so re-reading it is the cost of the drift. A `placeholder`
    # row was never classified at all, so its unchanged numbers are not evidence of anything.
    placeholder = [f for f in findings if f[0] == "placeholder"]
    # `undigested` is the one state here that asks for a *row edit* rather than a reading: the
    # row already says what the block turned out to be, and what it lacks is the digest. Counting
    # it with the states that need a classification made the tally say `22 need classifying`
    # over a table whose rows were all classified, and the sentence below then told the reader
    # to re-classify them — which is precisely the re-reading this clock exists to prevent.
    undigested = [f for f in findings if f[0] == "undigested"]
    gone = [f for f in findings if f[0] == "gone"]

    for state, task, detail in findings:
        if state == "unchanged" and not args.all:
            continue
        print(f"  {state:13} {task:32} {detail}")

    print(
        f"blocks: {len(rendered)} render, {len(unchanged)} unchanged, "
        f"{len(duplicate)} duplicate, {len(changed)} need classifying, "
        f"{len(undigested)} need a digest, "
        f"{len(gone)} row(s) with nothing to render"
    )

    if changed or undigested or gone:
        if changed:
            print(
                "classify the block(s) above in the rotation table before re-reading any "
                "unchanged one - a block whose length and sha256 are unchanged has not moved"
            )
        if placeholder:
            print(
                f"a row whose class cell is still `{PLACEHOLDER_CLASS}` holds a fingerprint and "
                "no classification: `--init` wrote it and nobody decided what that block turned "
                "out to be, so its unchanged numbers are not evidence that it was read. Replace "
                "the placeholder with what the block is, and leave its length and digest alone"
            )
        if undigested:
            print(
                "add the sha256 printed beside each undigested row above to that row's digest "
                "cell, keeping its class and evidence - those rows are classified already, and "
                "re-classifying them is the re-reading this clock exists to save"
            )
        return 1
    print("every block renders unchanged text: the section 1.7 reading is the fingerprint list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
