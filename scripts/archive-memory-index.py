#!/usr/bin/env python3
"""Trim a memory index to its row cap by moving the OLDEST rows to an archive.

The class this exists for
-------------------------
Every evolution cycle curates two memory indexes (the evolution project's and
the session's), and the protocol is mechanical: keep at most 50 cycle rows,
append the rows that fall off to `cycle-archive-<YYYYMMDD>.md`, never delete a
detail file, never reference the archive from the index. It has been done by
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

Exit codes
----------
``0``  the index is within its cap (nothing to move), or the move was made and
       verified. ``1``  ``--check`` found a row-rule violation (too many rows, a
       row over the 512-char cap, or a duplicate target). ``2``  the question
       could not be answered, or the move did not verify - a measurement error is
       never reported as a healthy index.

Usage
-----
    uv run --no-sync python scripts/archive-memory-index.py <path/to/MEMORY.md>
    uv run --no-sync python scripts/archive-memory-index.py <index> --check
    uv run --no-sync python scripts/archive-memory-index.py <index> --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

#: One index row: a markdown link whose text is the title and whose target is a
#: detail file. Only the target is load-bearing here, so the link text is free.
ROW_RE = re.compile(r"^\s*-\s+\[[^\]]*\]\((?P<target>[^)\s]+)\)")

#: A cycle record row, whose target carries the cycle id the ordering uses.
CYCLE_RE = re.compile(r"^cycle-(?P<stamp>\d{8}-\d{6})\.md$")

#: The protocol's per-row cap, in **characters** (the index is embedded in the
#: system prompt, so a byte count would under-report CJK rows).
ROW_MAX_CHARS = 512

DEFAULT_CAP = 50


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
        cycle = CYCLE_RE.match(target)
        rows.append(
            Row(
                lineno=lineno,
                text=line,
                target=target,
                stamp=cycle.group("stamp") if cycle else None,
            )
        )
    return rows


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
    """The row rules `--check` enforces (read-only)."""
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


def apply_plan(index_path: Path, archive_path: Path, plan: Plan) -> None:
    """Write the archive first, then the index.

    Writing the archive first is the safe order for a move: a failure between the
    two leaves a row in *both* files (visible, recoverable) rather than in
    neither. The post-write check below restores both texts either way.
    """
    if plan.archive_after != plan.archive_before:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_text(plan.archive_after, encoding="utf-8")
    index_path.write_text(plan.index_after, encoding="utf-8")


def restore(index_path: Path, archive_path: Path, plan: Plan) -> list[str]:
    """Put both files back exactly as they were; report anything that failed."""
    failures: list[str] = []
    try:
        index_path.write_text(plan.index_before, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - a failing restore is reported loudly
        failures.append(f"could not restore the index: {exc}")
    try:
        if plan.archive_before:
            archive_path.write_text(plan.archive_before, encoding="utf-8")
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

    archive_path: Path = args.archive or index_path.with_name(
        f"cycle-archive-{date.today():%Y%m%d}.md"
    )

    if args.check:
        problems = check_rules(index_path, args.cap)
        rows = parse_rows(index_path.read_text(encoding="utf-8"))
        cycle_rows = [row for row in rows if row.is_cycle]
        print(f"index: {index_path}")
        print(f"{len(cycle_rows)} cycle row(s) of {len(rows)} row(s), cap {args.cap}")
        if problems:
            for problem in problems:
                print(f"VIOLATION: {problem}")
            return 1
        print("OK: the index respects the row rules")
        return 0

    plan = build_plan(index_path, archive_path, args.cap, f"{date.today():%Y-%m-%d}")
    problems = verify_plan(plan, args.cap)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2

    if not plan.moved:
        print(f"index: {index_path}")
        print(f"nothing to move: the index is within its {args.cap}-row cap")
        return 0

    print(f"index: {index_path}")
    print(f"archive: {archive_path}")
    for row in plan.moved:
        print(f"move: {row.target}")

    if args.dry_run:
        print(f"dry run: {len(plan.moved)} row(s) would move, nothing written")
        return 0

    try:
        apply_plan(index_path, archive_path, plan)
    except OSError as exc:
        failures = restore(index_path, archive_path, plan)
        print(f"error: writing failed: {exc}", file=sys.stderr)
        for failure in failures:
            print(f"error: {failure}", file=sys.stderr)
        return 2

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
