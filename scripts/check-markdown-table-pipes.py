#!/usr/bin/env python3
"""CI check: no markdown table cell hides an unescaped `|` inside a code span.

The defect this exists for
--------------------------
GFM splits a table row on its pipes, and a **code span does not protect one**: a
`|` inside a code span still ends the cell. The escape that works is `\\|`, and it
works inside a code span too. So a row like

    | `/skills` | List loaded skills; `/skills available|install|update` |

parses as **four** cells where its header has two, and github.com breaks the
table at that row -- while the raw source still reads as two tidy columns, which
is what makes it invisible to anyone reading the file.

Measured 2026-10-16 (`cyc20261016-183442`) over this checkout's tracked markdown:
17 files, exactly one site -- `README.md`, whose `/skills` row had drifted from
the escaped form the repository already keeps in `README.cn.md`
(`` `/skills available\\|install\\|update` ``). Issue #2324.

What is in scope, and what is not
---------------------------------
The subject is a **tracked markdown file** and the defect is one shape: an
unescaped pipe appearing **inside a code span** on a line that is part of a GFM
table (its header, its delimiter row, or one of its body rows). Lines inside a
fenced code block are not table rows and are skipped.

Two neighbouring shapes are deliberately **not** this rule's subject, and a
finding they would have produced is not reported:

* an unescaped `|` in a cell's *plain text* -- that also splits a cell, but the
  text it sits in is exactly what a reader sees, so it is not the invisible case
  this guard exists for;
* a pipe in prose outside any table -- harmless, and measured to be common here
  (8 lines across 4 tracked files: `DEVELOPMENT.md`, `emrg/server/*_prompt.md`).

Tracking the wider rule would need the row's cell count to be compared with its
header's; that is a different reading, and no tracked file needs it today.

Scope of the file list, stated with its measurement
---------------------------------------------------
The files come from `git ls-files` in a checkout and from a walk when the tree
carries no `.git` (a `git archive` export, which holds only tracked content, so
the two paths see the same files there; the export path is what
`scripts/check-merge-sequence.py` runs when it judges a merged tree). Excluding
the `.git` walk matters: an untracked scratch file must not be able to red the
rule, and the `.emrg/` state directory is gitignored, so a checkout with the
agent's own files under it is scanned as its tracked set and nothing else.

Exit codes
----------
    0  no such pipe in the tree it read (the file count is named in the verdict)
    1  `FAIL` -- a table row carries an unescaped pipe inside a code span
    2  `could not measure` -- the root is not a directory, the file listing
       failed, no markdown file was read at all, or a file could not be decoded

`2` is not a pass: `0` means *measured and clean*, and a scan that read no file
has measured nothing (the family's rule; `check_unbound_reads.py` and
`check-citation-resolves.py` state it for their own subjects).

Run it with the project interpreter, from anywhere:

    uv run --no-sync python3 scripts/check-markdown-table-pipes.py
    uv run --no-sync python3 scripts/check-markdown-table-pipes.py --root <tree>
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: A line is a fence marker when it is indented at most three spaces and opens
#: with three or more backticks or tildes (CommonMark). The marker's character and
#: length are what pair it with its closer.
_FENCE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")

#: A table's delimiter row is one where *every* cell is dashes, optionally with
#: one leading or trailing colon (`---`, `:--`, `--:`, `:-:`).
_DELIM_CELL = re.compile(r"^:?-+:?$")

#: An unescaped pipe: one not preceded by a backslash. That is the whole escape
#: GFM offers inside a table row, and it is honoured inside a code span too.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")

#: A backtick run, the unit a code span is delimited by.
_BACKTICK_RUN = re.compile(r"`+")

#: Directories a *non-checkout* tree may still carry from a build, skipped by the
#: walk fallback so an export's walk sees the same files its tracked listing
#: would. Stated rather than silent, because "which files did you read" is the one
#: thing this rule must not answer vaguely.
_SKIPPED_DIRS = frozenset(
    {".git", ".emrg", ".venv", "venv", "node_modules", "build", "dist", "__pycache__"}
)


class MarkdownTableError(Exception):
    """The tree is not in a shape this tool can measure."""


def _resolve_root() -> Path:
    """The tree to read: the checkout the caller is *standing in*, else our own.

    Derived from the cwd when that is a checkout, not from `__file__` alone, for
    the reason `check-doc-count.py` and `check_unbound_reads.py` record at length:
    the guard is used from git worktrees, where a script-root rule reads the main
    checkout and prints a confident verdict about a tree the caller is not
    looking at. Launched from anywhere that is not a checkout, it falls back to
    the tree its own file lives in -- still *a* tree, named on the report's first
    line, never guessed at.
    """
    here = Path(__file__).resolve().parent.parent
    cwd = Path.cwd()
    if (cwd / "README.md").is_file() and (cwd / "scripts").is_dir():
        return cwd
    return here


def code_span_bodies(line: str) -> list[str]:
    """The text inside each code span on `line`, in order.

    Backtick runs pair the CommonMark way: an opening run of N backticks is closed
    by the next run of **exactly** N, so a double-backtick run inside a
    single-backtick span does not close it, and an unclosed run is literal (the
    scan moves past it and keeps looking).

    Pure, and over one line rather than a document, so the rule can be *driven*
    against a synthetic span in a test -- a rule only ever pointed at the real
    tree is not known to discriminate.
    """
    runs = [(m.start(), m.end(), m.end() - m.start()) for m in _BACKTICK_RUN.finditer(line)]
    bodies: list[str] = []
    i = 0
    while i < len(runs):
        _, opener_end, opener_len = runs[i]
        j = i + 1
        while j < len(runs) and runs[j][2] != opener_len:
            j += 1
        if j == len(runs):
            i += 1
            continue
        bodies.append(line[opener_end : runs[j][0]])
        i = j + 1
    return bodies


def _fence_mask(lines: list[str]) -> list[bool]:
    """True for every line inside a fenced code block, the fence lines included.

    A fenced block is not a table, so this is what keeps a documentation example
    of a broken table from being reported as one.
    """
    mask = [False] * len(lines)
    opener: str | None = None
    for i, line in enumerate(lines):
        match = _FENCE.match(line)
        if opener is None:
            if match:
                opener = match.group("marker")
                mask[i] = True
            continue
        mask[i] = True
        if (
            match
            and match.group("marker")[0] == opener[0]
            and len(match.group("marker")) >= len(opener)
        ):
            opener = None
    return mask


def _is_delimiter_row(line: str) -> bool:
    """Whether `line` is a table's delimiter row (`|---|---|`)."""
    if "|" not in line:
        return False
    cells = _UNESCAPED_PIPE.split(line)
    # A leading and a trailing pipe are the row's own edges, not empty cells.
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    if not cells:
        return False
    return all(_DELIM_CELL.match(cell.strip()) for cell in cells)


def table_rows(lines: list[str], mask: list[bool]) -> set[int]:
    """Zero-based line indices that are cells of a GFM table.

    A table is a delimiter row with a non-blank header above it, followed by
    further non-blank lines that carry a pipe; a blank line ends it (GFM's own
    rule). Header, delimiter and body rows are all included, because a code span
    in any of them is rendered as a cell.
    """
    rows: set[int] = set()
    i = 0
    while i < len(lines):
        if mask[i] or not _is_delimiter_row(lines[i]):
            i += 1
            continue
        if i == 0 or mask[i - 1] or not lines[i - 1].strip():
            i += 1
            continue
        rows.add(i - 1)
        rows.add(i)
        j = i + 1
        while j < len(lines) and not mask[j] and lines[j].strip() and "|" in lines[j]:
            rows.add(j)
            j += 1
        i = j
    return rows


def findings_in_text(text: str) -> list[tuple[int, str, str]]:
    """Every defect in `text`, as (line number, the code span, the whole line).

    Takes text rather than a path so the rule can be driven against a synthetic
    document built by a test, in both directions, next to the real tree's files.
    """
    lines = text.splitlines()
    mask = _fence_mask(lines)
    rows = table_rows(lines, mask)
    findings: list[tuple[int, str, str]] = []
    for index in sorted(rows):
        for body in code_span_bodies(lines[index]):
            if _UNESCAPED_PIPE.search(body):
                findings.append((index + 1, body, lines[index]))
    return findings


def _tracked_markdown(root: Path) -> list[str]:
    """The tracked `.md` files of a checkout, tree-relative and sorted.

    Tracked rather than walked, because the rule is about what the repository
    carries: an untracked scratch file must not be able to red it, and a tracked
    file must not be able to hide behind an ignore rule.
    """
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise MarkdownTableError(
            f"could not list tracked files in {root} "
            f"(git ls-files rc={proc.returncode}): {proc.stderr.strip()}"
        )
    if proc.stdout is None:
        raise MarkdownTableError("git ls-files produced no readable output")
    return sorted(
        name for name in proc.stdout.split("\0") if name and name.endswith(".md")
    )


def _exported_markdown(root: Path) -> list[str]:
    """The `.md` files of a tree with no `.git`, tree-relative and sorted.

    The fallback for a pristine export (`git archive` into a temp directory, the
    tree `scripts/check-merge-sequence.py` judges), where `git ls-files` fails by
    construction. A walk is right exactly there: an export holds only tracked
    content, so walking it and listing its tracked files are the same set.
    """
    found: list[str] = []
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _SKIPPED_DIRS for part in relative.parts):
            continue
        found.append(relative.as_posix())
    return sorted(found)


def markdown_files(root: Path) -> list[str]:
    """The files this rule covers, tree-relative and sorted.

    Two paths, decided by one measurable fact: whether the tree is a git
    checkout. A checkout is always read as *tracked* files; a pristine export is
    walked. A checkout whose listing fails is an error rather than a walk, so a
    broken repository cannot quietly change what "the repository states" means.
    """
    if (root / ".git").exists():
        return _tracked_markdown(root)
    return _exported_markdown(root)


def scan(root: Path) -> tuple[list[tuple[str, int, str, str]], list[str], int]:
    """(findings, files skipped as unreadable, files read).

    The third element is what separates "nothing to report" from "nothing was
    measured": both other lists are empty for a tree that carries no markdown at
    all, and the two are opposite readings. The caller turns a zero into the
    unmeasurable answer and prints the count on the clean verdict, so a reader
    sees the subject the verdict is about instead of having to trust it.
    """
    findings: list[tuple[str, int, str, str]] = []
    skipped: list[str] = []
    read = 0
    for name in markdown_files(root):
        try:
            text = (root / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A markdown file this tool cannot decode cannot be judged, and
            # failing the whole scan over one binary-ish file would make the
            # guard unusable rather than strict. Skipped, not hidden: the count
            # goes in the verdict below.
            skipped.append(name)
            continue
        read += 1
        for lineno, span, line in findings_in_text(text):
            findings.append((name, lineno, span, line))
    return findings, skipped, read


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        help="the tree to read; defaults to the checkout the caller stands in "
        "(or the checkout this script lives in, when the cwd is not one)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing but the verdict line on a clean tree",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else _resolve_root()
    # The verdict goes to stdout first; an unflushed block would let a finding
    # written to stderr overtake the line naming the tree it is about.
    sys.stdout.reconfigure(line_buffering=True)
    print(f"tree: {root}")

    if not root.is_dir():
        print(
            f"could not measure: {root} is not a directory, so no file was read - "
            "`0` says measured and clean, and this is neither",
            file=sys.stderr,
        )
        return 2

    try:
        findings, skipped, read = scan(root)
    except MarkdownTableError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    for name, lineno, span, line in findings:
        print(f"ERROR: {name}:{lineno}  code span {span!r} carries an unescaped `|`")
        print(f"       {line.strip()}")

    if findings:
        print(
            f"ERROR: {len(findings)} table cell(s) carry an unescaped `|` inside a "
            "code span - GFM splits the row on it, so that table breaks at the row. "
            r"Escape each one as `\|`."
        )
        return 1
    if skipped:
        print(
            f"UNMEASURABLE: {len(skipped)} markdown file(s) could not be decoded "
            f"({', '.join(skipped)}), so this is not a clean reading of the tree.",
            file=sys.stderr,
        )
        return 2
    if read == 0:
        print(
            f"could not measure: no markdown file was read under {root}, so nothing "
            "was judged - a reading of nothing is not a clean reading.",
            file=sys.stderr,
        )
        return 2
    if not args.quiet:
        print(
            "OK: no markdown table cell carries an unescaped `|` inside a code span "
            f"({read} file(s) read)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
