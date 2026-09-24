#!/usr/bin/env python3
"""Trim a memory index to its row cap by moving the OLDEST rows to an archive.

The class this exists for
-------------------------
An evolution cycle used to curate two memory indexes (the evolution project's
and the session's); that per-cycle mandate was removed (rant 2026-09-14T20:14:56),
so an index is now maintained on demand. When it is maintained, the protocol is
mechanical: keep at most 50 cycle rows, append the rows that fall off to
`cycle-archive-<YYYYMMDD>.md`, never delete a detail file, never reference the
archive from the index. It has been done by
hand-written scripts, and the hand-written scripts have failed twice:

* an empty shell variable turned ``sed -i '' "${n}d"`` into the sed script ``d``,
  which deleted **every line** of the index (the file was rebuilt from the cycle
  records on disk, which is why the detail files are never deleted);
* an off-by-one archived the **newest** row instead of the oldest, so the index
  lost the row it had just gained and the archive gained a row that was still
  current.

Both are the same shape: the operation is a *move*, and a move done by position
is one edit away from a delete or from the wrong end. So this tool makes the two
things a hand-written one-liner cannot guarantee explicit:

* **Rows are chosen by the timestamp parsed from ``cycle-<ts>.md``**, never by
  where the row sits in the file. "Oldest" is a property of the data, so an index
  written in any order still gives the same answer.
* **The move is checked to conserve every row.** The multiset of row lines in
  (index + archive) before must equal the multiset after: a row can neither be
  lost nor invented. Non-row lines (headings, the protocol notes) are asserted
  unchanged, and the archive is asserted to be append-only.
* **The check runs against the files on disk after writing**, not against the
  plan, and if it fails the index is restored verbatim and a freshly created
  archive is removed. The failure mode is "nothing happened", never "half a
  move".

The archive is the backup for exactly this operation, so an archiver that moves
the wrong end destroys the redundancy it is writing.

A third writer, and why the conservation check cannot see it
------------------------------------------------------------
The index is shared: every task's cycles append their rows to the same file, and
that file is the cross-task knowledge carrier the daemon embeds in every system
prompt. So a run of this tool is never the only writer of it. The check above
compares two snapshots this process took itself, which means a row another
writer appends *between* this run's read and its write is in neither snapshot:
from the accounting's point of view it was never there, the verdict is ``OK``,
and ``Plan.index_after`` - built from the older text - drops it without a trace.

Two things make that window safe, and neither is a lock:

* **The write is a compare-and-swap on content.** A plan carries the texts it
  read, so immediately before writing, both files are read back and compared
  against those texts. If either moved, the plan is discarded and rebuilt from
  what is on disk *now* - so the row that arrived is planned for rather than
  planned over - and if the files keep moving the run refuses and writes
  nothing. A lock would need every writer to take it; the writers here are
  separate processes belonging to separate tasks, so the check is on the bytes.
* **The write is a rename.** Both files are written to a temporary file beside
  the target and then ``os.replace``d, so an index that dies mid-write is either
  the old file or the new one, never a truncated prefix of the new one - a
  truncation no post-write restore can undo.

The retry is bounded (``MOVE_ATTEMPTS``): a refusal with an exit code is the
honest answer when the file is being rewritten faster than one run can plan.

What a row is, and what happens to a line that is almost one
------------------------------------------------------------
A row is a markdown link (`- [title](cycle-<ts>.md)`); the id is taken from the
target's **basename**, so a row that links the same detail file through a path is
still a cycle row. But an index can also *hold* a cycle row written in a shape
this parser does not read - a table row, a bare text row. Those lines are not
silently skipped: they are the reason the tool answers nothing at all. A move that
took the rows it *could* see would leave the cap violated while printing `OK`, and
a `--check` alongside it would print a count taken over a subset (measured
2026-09-14: 52 cycle rows written as a table reported as `0 cycle row(s) of 0
row(s)` with `OK`, and the trim answered `nothing to move` for a 52-row index).
Both modes now exit 2 and name the lines. Prose that merely mentions a cycle id is
not a row and is left alone.

The same false healthy verdict has a second shape, and it is the counted one: an
index whose rows are **all** in a shape this parser does not read *and* which names
no cycle, so nothing above can see it - a project index written as
`| id | title | type | status | updated |`. There the tool used to answer
`0 cycle row(s) of 0 row(s)` plus `OK` while the rows ran to six times the per-row
cap (measured 2026-09-24: 161 row-like lines, the longest 3,141 chars, no cycle
ids), and the trim answered `nothing to move` for the same file. A count over the
empty set is not a clean index, so a file with row-like lines and **no** row of the
shape this tool reads is refused by both modes as well (`report_foreign_format`).

The boundary is deliberate: the refusal fires only when *no* row parses. A mixed
index - readable rows beside rows of another shape that name no cycle - still
answers from what it can read, which is why the two indexes embedded on the host
that carry such lines (a table row here, a prose bullet there) keep their verdicts.
That residual gap is a count taken over a subset, and it is the honest cost of not
refusing files whose readable rows are perfectly manageable.

Exit codes
----------
``0``  the index is within its cap (nothing to move), or the move was made and
       verified. ``1``  ``--check`` found a row-rule violation in an index it could
       read (too many cycle rows, a row over the per-row cap, a duplicate target).
       ``2``  the question could not be answered - no index at that path, an index
       that could not be read, an index holding lines that name a cycle but are not
       rows this tool can read (so which rows are cycle rows is unknowable), an index
       whose row-like lines contain no row this tool can read at all (so the row rules
       have nothing to be asserted about), or a move that did not verify. A
       measurement error is never reported as a healthy index, and never as a rule
       violation.

Usage
-----
    uv run --no-sync python scripts/archive-memory-index.py <path/to/MEMORY.md>
    uv run --no-sync python scripts/archive-memory-index.py <index> --check
    uv run --no-sync python scripts/archive-memory-index.py <index> --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

# The per-row cap below is the memory store's number, so read it from the store
# rather than spelling a second copy that can drift from what the store actually
# truncates at (rant 2026-09-14T13:23:04). The repo root goes on the path so this
# tool measures the tree it stands in, however it is loaded: as
# `python3 scripts/archive-memory-index.py` the root is not `sys.path[0]`, and the
# test suite loads this file by path (`spec_from_file_location`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from emrg.memory import INDEX_TITLE_MAX_CHARS  # noqa: E402  (needs the path above)

#: One index row: a markdown link whose text is the title and whose target is a
#: detail file. Only the target is load-bearing here, so the link text is free.
ROW_RE = re.compile(r"^\s*-\s+\[[^\]]*\]\((?P<target>[^)\s]+)\)")

#: A cycle record row, whose target carries the cycle id the ordering uses. The
#: test is on the target's **basename**: `cycle-<ts>.md` is the same detail file
#: whether a row names it from beside the index or through a path
#: (`memory/cycle-<ts>.md`), and where a file sits is not what its id means.
CYCLE_RE = re.compile(r"cycle-(?P<stamp>\d{8}-\d{6})\.md$")

#: A cycle id, wherever it appears - used to notice a row the parser cannot read.
CYCLE_ID_IN_LINE = re.compile(r"\bcyc\d{8}-\d{6}\b")

#: The shapes a *row* is written in: a list item, an ordered item, or a table row.
#: Prose that merely mentions a cycle id (a `>` note explaining the protocol, a
#: heading) is not a row and must not be mistaken for one.
ROW_LIKE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|\|)")

#: The store's per-row cap for an index line, in **characters** (the index is
#: embedded in the system prompt, so a byte count would under-report CJK rows).
#: One owner: this is `emrg.memory`'s constant, not a number re-spelled here.
ROW_MAX_CHARS = INDEX_TITLE_MAX_CHARS

DEFAULT_CAP = 50

#: How many times a run re-plans before it refuses. One is the common case; a
#: second is needed only when another writer touched a file between the read and
#: the compare; a file that moves on every attempt is not one this run may write.
MOVE_ATTEMPTS = 3


@dataclass(frozen=True)
class Row:
    """A row line, its 0-based line number, and the cycle id if it has one."""

    lineno: int
    text: str
    target: str
    stamp: str | None

    @property
    def is_cycle(self) -> bool:
        return self.stamp is not None


@dataclass(frozen=True)
class Plan:
    """What the index and the archive would look like after the move."""

    index_before: str
    archive_before: str
    index_after: str
    archive_after: str
    moved: tuple[Row, ...]


def parse_rows(text: str) -> list[Row]:
    """Every row line in `text`, in file order, with its cycle id when it has one."""
    rows: list[Row] = []
    for lineno, line in enumerate(text.splitlines()):
        match = ROW_RE.match(line)
        if not match:
            continue
        target = match.group("target")
        cycle = CYCLE_RE.search(target)
        rows.append(
            Row(
                lineno=lineno,
                text=line,
                target=target,
                stamp=cycle.group("stamp") if cycle else None,
            )
        )
    return rows


def unreadable_rows(text: str) -> list[tuple[int, str]]:
    """Row-like lines that name a cycle but are not rows this tool can read.

    Returns `(line number, line)` pairs, numbered from 1 the way an editor counts
    (``Row.lineno`` is an internal 0-based index and is never shown to a reader).

    Why this exists: a *move* whose source set is wrong is worse than a refusal.
    A cycle row written as a table (`| cyc... | cycle-....md |`) or as plain text
    (`- cyc... - cycle-....md`) is not a markdown link, so `parse_rows` does not
    see it at all - the tool would count the rows it *can* read, report `OK`, and
    print `nothing to move` for an index that is over its cap. That is a healthy
    verdict on a question it did not answer, which is exactly what exit code 2 is
    reserved for. Measured before this rule (`cyc20260914-042726`): 52 cycle rows
    written as a table gave `0 cycle row(s) of 0 row(s)` and `OK: the index
    respects the row rules`.

    Only row-like lines count - a `>` note or a heading may mention a cycle id
    without being a row (the session index's own header does), and a non-cycle
    *link* row may quote one in its text (the source-project index's single row
    does). Those are notes and rows, not unreadable rows.
    """
    readable = {row.lineno for row in parse_rows(text)}
    return [
        (lineno, line)
        for lineno, line in enumerate(text.splitlines(), start=1)
        if lineno - 1 not in readable
        and ROW_LIKE.match(line)
        and CYCLE_ID_IN_LINE.search(line)
    ]


def row_like_lines(text: str) -> list[tuple[int, str]]:
    """Every row-like line in `text`, numbered as an editor counts.

    `ROW_LIKE` is a *shape* test, not a parse: it answers whether the file contains
    rows at all, which is the one question `parse_rows` returning nothing leaves
    open. A file with row-like lines and none of them readable is a file the row
    rules cannot be asserted about - see `report_foreign_format`.
    """
    return [
        (lineno, line)
        for lineno, line in enumerate(text.splitlines(), start=1)
        if ROW_LIKE.match(line)
    ]


def row_texts(text: str) -> list[str]:
    """The row lines of `text`, for multiset conservation checks."""
    return [row.text for row in parse_rows(text)]


def non_row_lines(text: str) -> list[str]:
    """The lines of `text` that are not rows (headings, notes, blank lines)."""
    row_numbers = {row.lineno for row in parse_rows(text)}
    return [
        line for lineno, line in enumerate(text.splitlines()) if lineno not in row_numbers
    ]


def archive_header(index: Path, cap: int, today: str) -> str:
    """The heading a freshly created archive starts with.

    Append-only in spirit: once the file exists its text is never rewritten, so
    this only decides what the first row of a new archive sits under.
    """
    return (
        f"# cycle index archive ({today})\n"
        "\n"
        f"Rows moved out of {index.name} by the {cap}-row cap. Append-only; detail "
        "files\n(`cycle-*.md`) are never deleted. Never referenced from "
        f"{index.name}.\n"
        "\n"
    )


def build_plan(index_path: Path, archive_path: Path, cap: int, today: str) -> Plan:
    """Plan the move: the oldest cycle rows out, everything else untouched."""
    index_text = index_path.read_text(encoding="utf-8")
    archive_exists = archive_path.exists()
    archive_text = archive_path.read_text(encoding="utf-8") if archive_exists else ""

    rows = parse_rows(index_text)
    cycle_rows = [row for row in rows if row.is_cycle]
    excess = len(cycle_rows) - cap
    if excess <= 0:
        return Plan(index_text, archive_text, index_text, archive_text, ())

    # Ordered by the cycle id, not by file position: see the module docstring for
    # the incident (and the wrong end) this replaces.
    moved = tuple(sorted(cycle_rows, key=lambda row: row.stamp)[:excess])
    dropped = {row.lineno for row in moved}
    kept = [
        line
        for lineno, line in enumerate(index_text.splitlines(keepends=True))
        if lineno not in dropped
    ]
    index_after = "".join(kept)

    archive_before = archive_text
    if not archive_exists:
        archive_text = archive_header(index_path, cap, today)
    if archive_text:
        if not archive_text.endswith("\n"):
            archive_text += "\n"
        if archive_exists and not archive_text.endswith("\n\n"):
            # One blank line before the appended rows, so a later reader can see
            # where this run starts. Only for an existing archive: a fresh header
            # already ends with one.
            archive_text += "\n"
    archive_after = archive_text + "".join(row.text + "\n" for row in moved)

    return Plan(index_text, archive_before, index_after, archive_after, moved)


def verify_plan(plan: Plan, cap: int) -> list[str]:
    """Every property the move claims, checked against the texts it plans.

    Called with the *planned* texts before writing and with the texts read back
    from disk afterwards, so the same rules answer "would this be right?" and
    "is this right?".
    """
    problems: list[str] = []

    before = Counter(row_texts(plan.index_before) + row_texts(plan.archive_before))
    after = Counter(row_texts(plan.index_after) + row_texts(plan.archive_after))
    if before != after:
        lost = sorted((before - after).elements())
        gained = sorted((after - before).elements())
        problems.append(
            f"rows not conserved: lost={len(lost)} {lost[:3]} gained={len(gained)} "
            f"{gained[:3]}"
        )

    if non_row_lines(plan.index_after) != non_row_lines(plan.index_before):
        problems.append("the index's non-row lines changed (headings or notes)")

    before_notes = non_row_lines(plan.archive_before)
    after_notes = non_row_lines(plan.archive_after)
    if after_notes[: len(before_notes)] != before_notes:
        problems.append("the archive's existing non-row lines were rewritten")

    kept_cycle_rows = [row for row in parse_rows(plan.index_after) if row.is_cycle]
    if len(kept_cycle_rows) > cap:
        problems.append(
            f"the index still holds {len(kept_cycle_rows)} cycle rows, over the cap {cap}"
        )

    for row in plan.moved:
        if row.text not in row_texts(plan.archive_after):
            problems.append(f"a moved row is missing from the archive: {row.target}")
            break

    suffix = row_texts(plan.archive_after)[-len(plan.moved) :] if plan.moved else []
    if plan.moved and suffix != [row.text for row in plan.moved]:
        problems.append("the moved rows are not the archive's last rows, in order")

    return problems


def check_rules(index_path: Path, cap: int) -> list[str]:
    """The row rules `--check` enforces (read-only).

    Rows this parser cannot read are not a rule `--check` can test - `main` refuses
    before either mode runs, because a rule list printed beside a count taken over
    the rows it *could* see is the false OK this tool exists to prevent. What is
    left here is what the protocol states about rows that are readable.
    """
    text = index_path.read_text(encoding="utf-8")
    rows = parse_rows(text)
    problems: list[str] = []

    cycle_rows = [row for row in rows if row.is_cycle]
    if len(cycle_rows) > cap:
        problems.append(
            f"{len(cycle_rows)} cycle rows, over the cap {cap} - run without --check "
            "to archive the oldest ones"
        )

    long_rows = [row for row in rows if len(row.text) > ROW_MAX_CHARS]
    if long_rows:
        problems.append(
            f"{len(long_rows)} row(s) over {ROW_MAX_CHARS} chars, longest "
            f"{max(len(row.text) for row in long_rows)}"
        )

    targets = Counter(row.target for row in rows)
    duplicates = sorted(target for target, count in targets.items() if count > 1)
    if duplicates:
        problems.append(f"duplicate row target(s): {duplicates}")

    return problems


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a reader sees the old file or the new one.

    ``Path.write_text`` truncates and then writes, so a process that dies in
    between leaves a prefix of the new index where the index was - a loss no
    post-write restore can reach, because the process that would run it is gone.
    A temporary file beside the target plus ``os.replace`` (a rename, same
    filesystem) removes that state: the index is never the file being written.

    The temporary name is predictable on purpose: a leaked one is obvious and
    cleaning it is a single ``rm``. It is also removed on every exit path here,
    including the failure one, so only ``SIGKILL`` between the open and the
    replace can leave it.
    """
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        # No `newline=`: this must write the bytes `Path.write_text` writes.
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def changed_since_planned(index_path: Path, archive_path: Path, plan: Plan) -> list[str]:
    """Which of the two files no longer holds the text the plan was built from.

    The plan carries what it read, so this is a compare-and-swap against the
    bytes: a row another task's cycle appended while this run was planning is
    visible here, and the caller re-plans instead of overwriting it. Files that
    cannot be read back are reported as changed for the same reason - a file this
    run cannot read is not one it may overwrite.

    Each file is read on its own, so a failure names **that** file. One `try`
    covering both reads named the index whichever read raised - and appended it
    even when the index read had succeeded - so an archive this run could not read
    back was reported as "the index changed": the operator was sent to a file
    nothing had written, and the unreadable one went unmentioned (issue #1486).
    """
    changed: list[str] = []
    pairs = ((index_path, plan.index_before), (archive_path, plan.archive_before))
    for path, before in pairs:
        try:
            on_disk = path.read_text(encoding="utf-8") if path.exists() else ""
        except (OSError, UnicodeDecodeError):
            changed.append(str(path))
            continue
        if on_disk != before:
            changed.append(str(path))
    return changed


def announce(index_path: Path, archive_path: Path, plan: Plan) -> None:
    """Print what is about to move, once, after the plan is the one being used."""
    print(f"index: {index_path}")
    print(f"archive: {archive_path}")
    for row in plan.moved:
        print(f"move: {row.target}")


def apply_plan(index_path: Path, archive_path: Path, plan: Plan) -> None:
    """Write the archive first, then the index.

    Writing the archive first is the safe order for a move: a failure between the
    two leaves a row in *both* files (visible, recoverable) rather than in
    neither. The post-write check below restores both texts either way, and both
    writes are renames, so neither file is ever a partial one.
    """
    if plan.archive_after != plan.archive_before:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(archive_path, plan.archive_after)
    atomic_write_text(index_path, plan.index_after)


def restore(index_path: Path, archive_path: Path, plan: Plan) -> list[str]:
    """Put both files back exactly as they were; report anything that failed."""
    failures: list[str] = []
    try:
        atomic_write_text(index_path, plan.index_before)
    except OSError as exc:  # pragma: no cover - a failing restore is reported loudly
        failures.append(f"could not restore the index: {exc}")
    try:
        if plan.archive_before:
            atomic_write_text(archive_path, plan.archive_before)
        else:
            archive_path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover
        failures.append(f"could not restore the archive: {exc}")
    return failures


def measure_on_disk(index_path: Path, archive_path: Path, plan: Plan, cap: int) -> list[str]:
    """Verify what the files actually say now, not what the plan intended."""
    problems: list[str] = []
    try:
        actual_index = index_path.read_text(encoding="utf-8")
        actual_archive = (
            archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
        )
    except OSError as exc:
        return [f"could not read the files back: {exc}"]

    if actual_index != plan.index_after:
        problems.append("the index on disk is not what the plan wrote")
    if actual_archive != plan.archive_after:
        problems.append("the archive on disk is not what the plan wrote")
    problems.extend(
        verify_plan(
            replace(plan, index_after=actual_index, archive_after=actual_archive), cap
        )
    )
    return problems


def report_unreadable(index_path: Path, unreadable: list[tuple[int, str]]) -> None:
    """Name the lines that block the question, numbered as an editor counts them."""
    print(
        f"error: {len(unreadable)} row-like line(s) in {index_path} name a cycle but "
        "are not markdown link rows, so which rows are cycle rows cannot be answered:",
        file=sys.stderr,
    )
    for lineno, line in unreadable[:20]:
        print(f"  {index_path}:{lineno}: {line.strip()[:120]}", file=sys.stderr)
    if len(unreadable) > 20:
        print(f"  ... and {len(unreadable) - 20} more", file=sys.stderr)


def report_foreign_format(index_path: Path, row_like: list[tuple[int, str]]) -> None:
    """Name the rows in a shape this parser does not read, when there are no others.

    The verdict is deliberately about what the tool *did* with this file, not about
    the file being wrong: an index in another format may be perfectly good, and this
    tool has no opinion on it. What it may not do is call it clean, because the row
    rules were applied to nothing.
    """
    print(
        f"error: {index_path} has {len(row_like)} row-like line(s) and not one row of "
        "the shape this tool reads (`- [title](target)`), so the row rules have "
        "nothing to be asserted about:",
        file=sys.stderr,
    )
    for lineno, line in row_like[:20]:
        print(f"  {index_path}:{lineno}: {line.strip()[:120]}", file=sys.stderr)
    if len(row_like) > 20:
        print(f"  ... and {len(row_like) - 20} more", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("index", type=Path, help="path to the memory index (MEMORY.md)")
    parser.add_argument(
        "--cap",
        type=int,
        default=DEFAULT_CAP,
        help=f"cycle rows kept in the index (default: {DEFAULT_CAP})",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=None,
        help="archive file to append to (default: cycle-archive-<today>.md beside "
        "the index)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="read-only: report row-rule violations, exit 1 if there are any",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without writing anything",
    )
    args = parser.parse_args(argv)

    index_path: Path = args.index
    if not index_path.is_file():
        print(f"error: no index at {index_path}", file=sys.stderr)
        return 2

    # A read that fails is a measurement that did not happen: an escaping traceback
    # would exit 1, which this tool's own contract reads as "the index violates the
    # row rules" - the same false verdict in a different costume.
    try:
        index_text = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: could not read {index_path}: {exc}", file=sys.stderr)
        return 2

    # Refuse before either mode answers: a count printed over rows this tool cannot
    # classify is the verdict this tool exists to prevent (measured 2026-09-14: 52
    # cycle rows written as a table read as `0 cycle row(s) of 0 row(s)`, exit 0), and
    # a move over a *subset* of the rows would leave the cap violated while reporting
    # success. Nothing is answered, nothing is moved.
    unreadable = unreadable_rows(index_text)
    if unreadable:
        report_unreadable(index_path, unreadable)
        return 2

    # The counted shape of the same defect, which `unreadable_rows` cannot see
    # because such a file names no cycle: rows in a shape this parser does not read,
    # and no readable row to apply the rules to. `0 cycle row(s) of 0 row(s)` plus
    # `OK` is a healthy verdict on a question that was never answered (measured
    # 2026-09-24 on a table-format project index: 161 row-like lines, the longest
    # 3,141 chars, `--check` rc 0), and the trim's `nothing to move` is the same
    # claim from the other side. Refused here, before either mode answers.
    rows = parse_rows(index_text)
    if not rows:
        row_like = row_like_lines(index_text)
        if row_like:
            report_foreign_format(index_path, row_like)
            return 2

    archive_path: Path = args.archive or index_path.with_name(
        f"cycle-archive-{date.today():%Y%m%d}.md"
    )

    if args.check:
        problems = check_rules(index_path, args.cap)
        cycle_rows = [row for row in rows if row.is_cycle]
        print(f"index: {index_path}")
        print(f"{len(cycle_rows)} cycle row(s) of {len(rows)} row(s), cap {args.cap}")
        if problems:
            for problem in problems:
                print(f"VIOLATION: {problem}")
            return 1
        print("OK: the index respects the row rules")
        return 0

    today = f"{date.today():%Y-%m-%d}"
    for attempt in range(1, MOVE_ATTEMPTS + 1):
        plan = build_plan(index_path, archive_path, args.cap, today)
        problems = verify_plan(plan, args.cap)
        if problems:
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            return 2

        if not plan.moved:
            print(f"index: {index_path}")
            print(f"nothing to move: the index is within its {args.cap}-row cap")
            return 0

        if args.dry_run:
            announce(index_path, archive_path, plan)
            print(f"dry run: {len(plan.moved)} row(s) would move, nothing written")
            return 0

        # The compare half of the compare-and-swap: another task's cycle appends
        # to this index continuously, so a plan built from text that has since
        # moved is a plan that would overwrite the other writer's rows.
        changed = changed_since_planned(index_path, archive_path, plan)
        if changed:
            if attempt < MOVE_ATTEMPTS:
                continue
            print(
                f"error: the files changed (or could not be read back) on each of "
                f"{MOVE_ATTEMPTS} attempts, so this run refuses to write (nothing "
                "written, nothing moved); re-run it once the other writer has finished:",
                file=sys.stderr,
            )
            for name in changed:
                print(f"  {name}", file=sys.stderr)
            return 2

        announce(index_path, archive_path, plan)
        try:
            apply_plan(index_path, archive_path, plan)
        except OSError as exc:
            failures = restore(index_path, archive_path, plan)
            print(f"error: writing failed: {exc}", file=sys.stderr)
            for failure in failures:
                print(f"error: {failure}", file=sys.stderr)
            return 2
        break

    problems = measure_on_disk(index_path, archive_path, plan, args.cap)
    if problems:
        failures = restore(index_path, archive_path, plan)
        print("error: the move did not verify, the files were restored:", file=sys.stderr)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        for failure in failures:
            print(f"error: {failure}", file=sys.stderr)
        return 2

    print(f"moved {len(plan.moved)} row(s); verified conserved and append-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
