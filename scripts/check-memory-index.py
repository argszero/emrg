#!/usr/bin/env python3
"""A memory index, measured against the rule it answers to: two numbers, and its rows' links.

Usage
-----
    uv run --no-sync python3 scripts/check-memory-index.py                 # the indexes the tree carries
    uv run --no-sync python3 scripts/check-memory-index.py <PATH ...>      # named indexes, wherever they are

Why this exists
---------------
The index rule EMRG's cycles obey is two numbers and one place to act:

    No archiving step and no row cap - the rule is the file's LINE COUNT. An
    index past 100 lines is compacted in place, by yourself ... shorten a row
    past INDEX_TITLE_MAX_CHARS (512) to one line.

(`emrg/server/evolution_prompt.md` §6, in this checkout.) Nothing reads those
numbers off an index. The two mechanisms that do look at one are both elsewhere,
and each covers a different file: the reflection round's compaction note counts
the session cwd's project index and the session's own index
(`EmrgServer._maybe_reflect_memory`), while the store's write-time advisory fires
only on a **store write** - its own comment in `emrg/memory.py` names what that
leaves out, "an index the agent edits with `write`/`edit`, which is how the memory
instructions tell it to maintain `MEMORY.md`, bypasses this method at either
scope".

So the index a cycle writes is measured by nobody, and the cost is measured
rather than argued. On 2026-09-26 this host's evolution index stood at **206
lines / 199,316 bytes / 194 rows, 48 of them over 512 chars (longest 4,280)** and
left that state because an agent decided to compact it, not because anything told
it to - issue #1606 records both the arithmetic and the compaction. This tool is
that rule's reading: it says whether an index is over either number, so "is this
index within its rule" is a command instead of a hand-rolled probe.

What a row is, and why by shape
-------------------------------
A row is a line whose first two characters are ``- ``. That is measured by shape
rather than by the store's entry grammar, and the difference is not academic:
this host's evolution index read 75 lines on 2026-09-26, **67** of them list
lines, of which `MemoryIndex.from_text` recognises only **21**. The other 46 are
pointer lines an agent's compaction wrote, each carrying several `[id](file.md)`
references on one line. Those are rows the rule binds, the prompt pays for, and
every store mechanism is blind to - so a reading that used the parser's grammar
would exempt exactly the rows only an agent writes.

Three readings, each from its own source
----------------------------------------
The line **cap** is `MEMORY_INDEX_ROW_CAP` and the row **bound** is
`INDEX_TITLE_MAX_CHARS`, both imported here rather than spelled again: they are
the two constants the prompt's rule is pinned to
(`tests/test_evolution_prompt_index_rule.py` asserts the prompt states the value
these two carry). The line count is `len(text.splitlines())`, the same expression
the daemon's compaction note counts with, so the number printed and the number a
trigger acts on are one reading. The row bound is counted in **characters**, which
is the unit the rule names and the unit the embed budget spends (the store's
*advisory* compares the file's bytes, a deliberately different reading - one CJK
index fires one and not the other). The row **links** are the third reading, and
its source is not this rule's text but the resolution rule three other carriers
already assert (named under *Scope*): the index's rows are the one carrier none of
them read.

Scope, named rather than implied
--------------------------------
* This tool **measures; it never repairs.** Compaction needs a reader's judgement
  (merging rows, shortening one without losing a fact), and the rule already says
  who does it: the agent, in place.
* Its default subject is the tree this shell stands in. The index a cycle writes
  may live outside that tree - on this host it is the evolution root, which is
  issue #1606's measured divergence - so the path form is not a convenience but
  the way to reach it.
* A row's target file **is** checked, and this is the third reading: a row link
  that resolves to no file beside the index. The rule is the one this project
  already asserts in three other carriers - `tests/test_project_context_paths.py`
  for the paths `Agent.md`/`MANIFESTO.md` name, `check-citation-resolves.py` for
  test node ids, `check-rant-citations.py` for the files a rant names - and the
  index's own rows are the carrier none of them reads. Measured 2026-09-26 before
  it was added: 245 links across this host's three live indexes, **0** unresolved,
  so the value is the next rename, not a repair (the shape
  `test_project_context_paths.py` was written in).
* The character bound applies to **rows**. A title, a `>` note or a paragraph past
  512 chars is not a row and is not reported - the rule's subject is rows, which is
  what `100 x 512` bounds, and the renderer that writes them bounds the same thing.
* An index with no rows (a title and prose only) is measured, not failed: it has
  nothing for the bound to bind and nothing to resolve.

What the resolution reading does not cover
------------------------------------------
* **Which** link is the row's own. A row carrying several `[x](file.md)`
  references is checked for all of them, because the rule is "a row may not point
  at nothing" and each link is a thing a reader can follow.
* A target that exists but is not a *detail file* (a directory, an unrelated
  file) resolves, and is not reported. "This row points somewhere that holds what
  the row claims" is a different rule with a different remedy.
* A target with a URL-escape spelling (`%20`) is resolved as written: this tool
  does not guess a second spelling of a name the author wrote, and
  `check-citation-resolves.py`'s own limit section is the precedent for saying so
  rather than half-reading it.

Which tree answered
-------------------
The first line is `tree: <resolved root>` before any verdict, the convention every
guard in this family carries: without it the same report would be true of this
checkout, of a worktree, and of a directory a test built. Its default subject is
derived from that root, so the two lines answer about one tree.

Exit codes
----------
``0``  every index read is within both numbers, and every row link resolves.
``1``  at least one index is over a number the rule names (the line cap, or a row
       past the bound), or carries a row link that resolves to no file; each
       finding is printed with the line it is on.
``2``  nothing could be measured: no index under the tree, or a named index could
       not be read. An unreadable subject makes the reading incomplete, which is
       reported as such even when the other indexes were read - a partial reading
       is never printed as a pass.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple, Optional


def _resolve_root() -> Path:
    """The tree whose indexes answer when no path is named.

    Derived from the cwd when the caller is *standing in* a checkout, the shape
    `check-doc-count.py` uses and for its reason: unblocking a PR means working in
    a git worktree, and the indexes under *that* tree are the ones the caller
    means. Falling back to this file's own root keeps the documented invocation
    working from anywhere (the classification in
    `tests/test_a_tree_reading_guard_names_its_tree.py` runs it from a directory
    that is not a checkout and requires the same answer), and the `tree:` line
    states which one answered either way.

    :returns: the checkout to measure by default.
    """
    here = Path(__file__).resolve().parent.parent
    cwd = Path.cwd()
    if (cwd / "Agent.md").is_file() and (cwd / "scripts").is_dir():
        return cwd
    return here


REPO_ROOT = _resolve_root()

# The two numbers come from the modules that act on them, so this file holds no
# second copy of either: `emrg.memory` is the writer (its truncation applies the
# 512), and `emrg.server.daemon` is the trigger (its compaction note fires past
# the 100). Imported rather than read out of the source text, because a literal
# here would be one more spelling to keep true - and the pair is already pinned by
# `tests/test_evolution_prompt_index_rule.py` against the prompt's own wording.
#
# The tool's own tree goes to the front of `sys.path` first, unconditionally, and
# that is load-bearing rather than tidy: this environment's `PYTHONPATH` carries
# the **installed** package ahead of the checkout (measured 2026-09-26:
# `PYTHONPATH=/Users/.../install/source:/Users/.../install/lib:`, so `import emrg`
# resolves to the installed 0.3.1 from this very directory), and the installed
# copy is a version behind the tree this script ships in - it has no
# `MEMORY_INDEX_ROW_CAP` at all. Without the insert, the two numbers would be read
# from a tree other than the one the first line names, which is the defect the
# `tree:` line exists to prevent, one level down. The check after the import
# holds that: a number that resolves *outside* the named tree is reported as
# unmeasurable rather than printed as the tree's own.
#
# The import is also the one thing here that needs the project interpreter, so its
# failure is carried to `main()` as data instead of raised at import time: a bare
# `python3` that cannot import the package must still print its `tree:` line and
# say what is missing, not die before naming the tree or a reason. The same shape
# `check-doc-count.py` documents for its `--measure` path.
sys.path.insert(0, str(REPO_ROOT))

THRESHOLD_ERROR = ""
THRESHOLD_SOURCE = ""
try:
    import emrg.memory as _memory_module
    import emrg.server.daemon as _daemon_module

    INDEX_TITLE_MAX_CHARS = _memory_module.INDEX_TITLE_MAX_CHARS
    MEMORY_INDEX_ROW_CAP = _daemon_module.MEMORY_INDEX_ROW_CAP
    THRESHOLD_SOURCE = str(Path(_memory_module.__file__).resolve())
    if not Path(THRESHOLD_SOURCE).is_relative_to(REPO_ROOT):
        THRESHOLD_ERROR = (
            f"the rule's numbers came from {THRESHOLD_SOURCE}, which is not under "
            f"{REPO_ROOT}"
        )
except Exception as exc:  # noqa: BLE001 - reported by main(), never swallowed
    INDEX_TITLE_MAX_CHARS = 0
    MEMORY_INDEX_ROW_CAP = 0
    THRESHOLD_ERROR = f"{type(exc).__name__}: {exc}"

#: What makes a line a row. Deliberately the whole predicate, so "why is that
#: line counted" has an answer a reader can apply to a file by eye (see the
#: docstring's "What a row is, and why by shape").
ROW_PREFIX = "- "

#: A row's detail-file link: `](target)`. The same shape the store renders
#: (`MemoryIndex._render_entry`) and the one the memory instructions tell an
#: agent to write, so a row that carries several links is read as several.
LINK = re.compile(r"\]\(([^)]+)\)")

#: Targets that are not a file in the index's directory, and so are excluded
#: from the resolution reading rather than reported as missing: an external URL
#: is not this tree's to resolve, and a bare `#anchor` names a heading in the
#: index itself. Named here as the whole exemption list, because an exemption
#: that grows silently is how a check becomes vacuous.
NON_FILE_TARGET = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|#)", re.IGNORECASE)

RUNNER = "uv run --no-sync python3"


class Reading(NamedTuple):
    """One index's readings, every one of them taken from the file at `path`.

    A `NamedTuple` rather than a dataclass: this module is loaded by path in its
    tests (`spec_from_file_location`, the shape `test_rant_citations.py` uses),
    and a dataclass built outside `sys.modules` raises in `dataclasses` itself
    when a string annotation is resolved - the trap `check-citation-resolves.py`
    records for whoever adds the next test.

    :param path: the index that answered.
    :param lines: its line count, `splitlines()` - the cap's subject.
    :param row_lines: the 1-based file line number of each row.
    :param row_lengths: each row's length in characters, in the same order.
    :param row_targets: every file link a row carries, as
        `(file line number, target)`, in file order - the resolution reading's
        subject. Excludes what `NON_FILE_TARGET` names.
    """

    path: Path
    lines: int
    row_lines: tuple[int, ...]
    row_lengths: tuple[int, ...]
    row_targets: tuple[tuple[int, str], ...] = ()

    @property
    def rows(self) -> int:
        """How many rows the file holds."""
        return len(self.row_lengths)

    @property
    def longest(self) -> int:
        """The longest row's length in characters, or 0 when there is none."""
        return max(self.row_lengths, default=0)

    def over(self, bound: int) -> list[tuple[int, int]]:
        """The rows past `bound`, as `(file line number, length)`.

        :param bound: the per-row character bound.
        :returns: one pair per offending row, in file order.
        """
        return [
            (line, length)
            for line, length in zip(self.row_lines, self.row_lengths)
            if length > bound
        ]

    def unresolved(self) -> list[tuple[int, str]]:
        """The row links that name no file, as `(file line number, target)`.

        Resolved against the index's own directory, which is what a relative
        target in one of these files means: the rows are written next to the
        detail files they name, so `- [x](cycle-20260926-140150.md)` is a
        sibling of the index, not of the caller's cwd. A target that is still
        missing once resolved is reported - a row whose file is gone is a
        pointer the reader follows into nothing, and the index is the only map
        of the store.

        Resolution is `os.path.exists` on the joined path: a symlink that
        resolves is a file for this purpose, and a broken one is not (a broken
        symlink is exactly the state a reader cannot follow).

        :returns: one pair per unresolved link, in file order.
        """
        base = self.path.parent
        out: list[tuple[int, str]] = []
        for line, target in self.row_targets:
            if not os.path.exists(os.path.join(base, target)):
                out.append((line, target))
        return out


def measure(path: Path) -> Reading:
    """Read one index and count what the rule counts.

    :param path: the index file to read.
    :returns: its readings.
    :raises OSError: the file could not be read (the caller reports it as
        unmeasurable rather than as a clean index).
    :raises UnicodeDecodeError: it is not UTF-8 text, the same failure the
        store's own reader names rather than guesses through.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    row_lines: list[int] = []
    row_lengths: list[int] = []
    row_targets: list[tuple[int, str]] = []
    for number, line in enumerate(lines, 1):
        if line.startswith(ROW_PREFIX):
            row_lines.append(number)
            row_lengths.append(len(line))
            for target in LINK.findall(line):
                if not NON_FILE_TARGET.match(target):
                    row_targets.append((number, target))
    return Reading(
        path, len(lines), tuple(row_lines), tuple(row_lengths), tuple(row_targets)
    )


def default_indexes(root: Path) -> list[Path]:
    """The indexes the tree at `root` carries, in a stable order.

    Both levels the memory instructions name for a tree: the project's index and
    one per session under it. Derived by *globbing the filesystem*, never by
    constructing a store - `MemoryStore.__init__` mkdirs the directory it is
    handed, and a reading with a side effect is how a count comes to create the
    thing it counts (the reason `_maybe_reflect_memory` spells its project path
    out rather than asking a store for it).

    :param root: the tree to look in.
    :returns: the index files that exist, empty when the tree carries none.
    """
    candidates = [root / ".emrg" / "memory" / "MEMORY.md"]
    candidates.extend(sorted((root / ".emrg" / "sessions").glob("*/memory/MEMORY.md")))
    return [path for path in candidates if path.is_file()]


def _report(reading: Reading) -> list[str]:
    """The verdict lines for one index, before they are printed.

    :param reading: the index's readings.
    :returns: its lines, the first one naming the file.
    """
    out = [str(reading.path)]
    if reading.lines > MEMORY_INDEX_ROW_CAP:
        out.append(
            f"  lines {reading.lines} of {MEMORY_INDEX_ROW_CAP} - over by "
            f"{reading.lines - MEMORY_INDEX_ROW_CAP}"
        )
    else:
        out.append(f"  lines {reading.lines} of {MEMORY_INDEX_ROW_CAP} - within")
    over = reading.over(INDEX_TITLE_MAX_CHARS)
    out.append(
        f"  rows {reading.rows}, longest {reading.longest} chars, "
        f"over {INDEX_TITLE_MAX_CHARS}: {len(over)}"
    )
    for number, length in over:
        out.append(
            f"  row at line {number} is {length} chars, "
            f"over the {INDEX_TITLE_MAX_CHARS} bound"
        )
    unresolved = reading.unresolved()
    out.append(
        f"  row links {len(reading.row_targets)}, unresolved: {len(unresolved)}"
    )
    for number, target in unresolved:
        out.append(
            f"  row at line {number} names {target}, "
            f"which is not beside {reading.path.name}"
        )
    return out


def main(argv: Optional[list[str]] = None) -> int:
    """Measure each index and report against the rule's numbers and its rows' links.

    :param argv: the command line, defaults to `sys.argv[1:]`.
    :returns: the exit code the docstring states.
    """
    # A merged reader must see the `tree:` line before any verdict, and this
    # family's docstrings promise that order. stdout is block-buffered when it is
    # a pipe (how a cycle reads a report: `2>&1 | tail`) while stderr is not, so
    # without this every stderr line overtakes the tree line - measured 2026-09-26
    # on the sibling guards (`2>&1 | cat -n` puts the verdict first). The remedy
    # is one line at each gate rather than a shared module, because these are
    # independent tools that otherwise share nothing.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description=(
            "Measure a MEMORY.md against the rule for an index: "
            + (
                f"{MEMORY_INDEX_ROW_CAP} lines, {INDEX_TITLE_MAX_CHARS} chars per row"
                if not THRESHOLD_ERROR
                else "the rule's numbers are unreadable from this interpreter, so "
                "every run reports that instead of a count"
            )
            + ", and every row link resolving to a file beside it."
        ),
        epilog=(
            f"Exit 0: every index is within both numbers and every row link "
            f"resolves. Exit 1: at least one is over one of them, or points at a "
            f"file that is not there. Exit 2: nothing could be measured (no index "
            f"under the tree, or a named index could not be read). Example: "
            f"{RUNNER} scripts/check-memory-index.py "
            "~/some/tree/.emrg/memory/MEMORY.md"
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help=(
            "index file(s) to measure (default: the indexes under the tree this "
            "shell stands in)"
        ),
    )
    args = parser.parse_args(argv)

    # Before anything else, and before any branch: which tree answered.
    print(f"tree: {REPO_ROOT}")

    if THRESHOLD_ERROR:
        print(
            "could not measure: the rule's two numbers could not be imported from "
            f"this checkout ({THRESHOLD_ERROR}) - run this with the project "
            f"interpreter ({RUNNER} scripts/check-memory-index.py)",
            file=sys.stderr,
        )
        return 2

    if args.paths:
        # Resolved, like the `tree:` line and for the same reason: a report read
        # from another directory has to name the file it read, not the spelling
        # that happened to work from the caller's cwd. `resolve()` does not need
        # the file to exist, so a missing one is reported by its absolute path.
        subjects = [Path(raw).resolve() for raw in args.paths]
    else:
        subjects = default_indexes(REPO_ROOT)
        if not subjects:
            print(
                f"could not measure: no memory index under {REPO_ROOT} - looked "
                "for .emrg/memory/MEMORY.md and .emrg/sessions/*/memory/MEMORY.md; "
                "name the index with a path when it lives outside this tree",
                file=sys.stderr,
            )
            return 2

    readings: list[Reading] = []
    unreadable: list[str] = []
    for path in subjects:
        try:
            readings.append(measure(path))
        except (OSError, UnicodeDecodeError) as exc:
            # Named, not summarised: the two failures a reader can act on are "no
            # such file" and "not UTF-8", and both have to survive to the report.
            unreadable.append(f"{path}: {type(exc).__name__}: {exc}")

    for reading in readings:
        for line in _report(reading):
            print(line)

    if unreadable:
        for line in unreadable:
            print(f"could not measure: {line}", file=sys.stderr)
        print(
            f"could not measure: {len(unreadable)} of {len(subjects)} index(es) "
            "could not be read, so this reading is incomplete",
            file=sys.stderr,
        )
        return 2

    findings = [
        reading
        for reading in readings
        if reading.lines > MEMORY_INDEX_ROW_CAP
        or reading.over(INDEX_TITLE_MAX_CHARS)
        or reading.unresolved()
    ]
    if not findings:
        print(
            f"OK: {len(readings)} index(es) within the two numbers the rule names "
            f"({MEMORY_INDEX_ROW_CAP} lines, {INDEX_TITLE_MAX_CHARS} chars per row), "
            "and every row link resolves"
        )
        return 0

    print(
        f"{len(findings)} of {len(readings)} index(es) over a number the rule names, "
        "or carrying a row link that resolves nowhere; the rule is compacted in "
        "place, by the agent itself, and a stale row is rewritten or dropped when "
        "the detail file is gone"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
