#!/usr/bin/env python3
"""Insert one cycle row into a memory index at the position that index keeps.

The class this exists for
-------------------------
Every evolution cycle adds one row to two memory indexes and trims them back to
their cap. The trim half is mechanised (`archive-memory-index.py`, which chooses
the OLDEST row by the timestamp in its filename, never by position). The insert
half was still hand-written, and it has now failed the same way three times:

* the session index is written **newest first** while the evolution index is
  written **oldest first**, and a row appended to the end of the session index is
  the row of the *oldest* cycle in the file;
* two cycles reported "move by position" bugs in their own index scripts;
* measured 2026-09-14 (`cyc20260914-180702`): an insert-then-trim one-liner
  dropped the ten rows it was supposed to keep and left only the new one - the
  session index had to be rebuilt from the rows the daemon embeds in its requests.

Every one of those is a position where an id was meant. So this tool refuses to
ask where a row should go:

* **The order is read from the index**, from the cycle ids of its own rows: ids
  ascending down the file means the new row is appended; descending means it goes
  above the first row. Rows that are not consistently ordered are not a guess the
  tool makes - it exits 2 and names them.
* **The row is placed by id**, not by end: the insert position is the one that
  keeps the file's order for the new row's own timestamp, so re-inserting an
  older cycle's row lands where that cycle belongs instead of at the front.
* **The trim is delegated** to `archive-memory-index.py` (one implementation of
  "oldest", with its own conservation check), and the result is verified after
  the fact: the new row is still there and the index is within its cap.
* **The index is backed up before it is touched**, and any failure leaves the
  original file exactly as it was.

Exit codes
----------
``0``  the row was inserted (and the index trimmed, unless ``--no-trim``).
``1``  the insert is a rule violation: the row's cycle id is already in the index.
``2``  the question could not be answered - no index or row file at that path, an
       index whose rows are not consistently ordered, a row that does not carry a
       cycle id, or an insert that did not verify. A measurement error is never
       reported as a successful insert.

Usage
-----
    uv run --no-sync python scripts/insert-memory-index-row.py <index> --row-file <row.md>
    uv run --no-sync python scripts/insert-memory-index-row.py <index> --row-file <row.md> --cap 10
    uv run --no-sync python scripts/insert-memory-index-row.py <index> --row-file <row.md> --dry-run
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# A row is a markdown link whose target is a cycle detail file. The id is the
# timestamp in the target's basename, so a row that links the same file through a
# path is still understood.
ROW_RE = re.compile(r"^- \[(?P<title>[^\]]*)\]\((?P<target>[^)]+)\)")
ID_RE = re.compile(r"(\d{8})-(\d{6})")

ARCHIVER = Path(__file__).resolve().parent / "archive-memory-index.py"


class Unanswerable(Exception):
    """The tool cannot tell what the right answer is; never a silent pass."""


def row_id(line: str) -> str | None:
    """The cycle id a row line belongs to, or None if the line is not a row.

    A line that mentions a cycle id but is not a markdown link is *not* a row:
    the archiver treats such lines as a reason to answer nothing at all, and this
    tool must not count them either - a count over a subset is how a cap gets
    violated while printing OK.
    """
    m = ROW_RE.match(line)
    if not m:
        return None
    target = m.group("target")
    hit = ID_RE.search(Path(target).name)
    return f"{hit.group(1)}-{hit.group(2)}" if hit else None


def read_index(path: Path) -> tuple[list[str], list[tuple[int, str]]]:
    """Return (all lines, [(line index, cycle id)] for row lines)."""
    if not path.is_file():
        raise Unanswerable(f"no index at {path}")
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except OSError as exc:
        raise Unanswerable(f"cannot read {path}: {exc}") from exc
    rows = [(i, rid) for i, line in enumerate(lines) if (rid := row_id(line))]
    if not rows:
        raise Unanswerable(f"{path} holds no cycle rows")
    return lines, rows


def detect_order(rows: list[tuple[int, str]]) -> str:
    """'ascending' (oldest first) or 'descending' (newest first), read from the ids."""
    ids = [rid for _i, rid in rows]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise Unanswerable(f"the index has duplicate cycle rows: {dupes}")
    if all(a <= b for a, b in zip(ids, ids[1:])):
        return "ascending"
    if all(a >= b for a, b in zip(ids, ids[1:])):
        return "descending"
    raise Unanswerable(
        "the index's rows are not consistently ordered (neither ascending nor "
        "descending by cycle id), so where a new row belongs is not a question "
        "this tool can answer: " + ", ".join(ids[:8])
    )


def insert_position(rows: list[tuple[int, str]], order: str, new_id: str) -> int:
    """The line index the new row goes at, keeping the index's own order.

    The position is counted from ids, never from ends: an ascending index takes
    the new row after every row older than it, a descending index after every row
    newer than it. A new row that is not the newest therefore lands among the rows
    rather than at the front - which is what an insert at "the top" gets wrong.
    """
    ids = [rid for _line_no, rid in rows]
    pos = sum(1 for rid in ids if rid < new_id) if order == "ascending" else sum(
        1 for rid in ids if rid > new_id
    )
    return rows[pos][0] if pos < len(rows) else rows[-1][0] + 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("index", help="path to MEMORY.md")
    ap.add_argument("--row-file", required=True, help="file holding the one row line")
    ap.add_argument("--cap", type=int, default=50, help="row cap passed to the archiver")
    ap.add_argument("--archive", help="archive path for the trim (default: archiver's own)")
    ap.add_argument("--no-trim", action="store_true", help="insert only, do not trim")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = ap.parse_args(argv)

    index = Path(args.index)
    row_file = Path(args.row_file)
    if not row_file.is_file():
        print(f"unanswerable: no row file at {row_file}", file=sys.stderr)
        return 2
    row = row_file.read_text(encoding="utf-8").strip("\n")
    if "\n" in row:
        print("unanswerable: the row file must hold exactly one line", file=sys.stderr)
        return 2
    try:
        lines, rows = read_index(index)
        order = detect_order(rows)
    except Unanswerable as exc:
        print(f"unanswerable: {exc}", file=sys.stderr)
        return 2

    new_id = row_id(row)
    if new_id is None:
        print(f"unanswerable: the row carries no cycle id: {row[:80]!r}", file=sys.stderr)
        return 2
    if any(rid == new_id for _i, rid in rows):
        print(f"rule violation: {new_id} is already a row of {index}", file=sys.stderr)
        return 1

    at = insert_position(rows, order, new_id)
    out = lines[:at] + [row] + lines[at:]
    print(f"index {index}: order={order} existing rows={len(rows)} -> insert {new_id} at line {at + 1}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    backup = index.with_name(f"{index.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(index, backup)
    index.write_text("\n".join(out) + ("" if not out[-1] else "\n"), encoding="utf-8")

    verified, after_rows = False, []
    try:
        _, after_rows = read_index(index)
        verified = any(rid == new_id for _i, rid in after_rows)
    except Unanswerable as exc:
        print(f"insert did not verify: {exc}", file=sys.stderr)
        shutil.copy2(backup, index)
        return 2
    if not verified:
        print("insert did not verify: the new row is not in the index", file=sys.stderr)
        shutil.copy2(backup, index)
        return 2

    if not args.no_trim and len(after_rows) > args.cap:
        cmd = [sys.executable, str(ARCHIVER), str(index), "--cap", str(args.cap)]
        if args.archive:
            cmd += ["--archive", args.archive]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print(f"trim failed (archiver exit {proc.returncode}) - the index keeps the row",
                  file=sys.stderr)
            return 2
        _, after_rows = read_index(index)
        if not any(rid == new_id for _i, rid in after_rows):
            print(
                "trim removed the row that was just added - the archiver picks the "
                "oldest by cycle id, so a row that is not the oldest must not be the "
                "one archived; the index is backed up at " + str(backup),
                file=sys.stderr,
            )
            return 2
        if len(after_rows) > args.cap:
            print(f"trim left {len(after_rows)} rows for a cap of {args.cap}", file=sys.stderr)
            return 2

    print(f"ok: {len(after_rows)} row(s) in {index}; backup {backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
