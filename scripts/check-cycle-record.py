#!/usr/bin/env python3
"""A cycle's record where it lands: the snapshot header, and the block it displaced.

Usage
-----
    uv run --no-sync python3 scripts/check-cycle-record.py
    uv run --no-sync python3 scripts/check-cycle-record.py --memory-root <DIR>
    uv run --no-sync python3 scripts/check-cycle-record.py --state <PATH> --archive <PATH>

Why this exists
---------------
Every cycle ends by moving one block out of the state file into the archive, and then
writing a snapshot of its own in the state file's place. The block's first line is its
identity::

    ## Current state (snapshot cyc20261112-220426, 2026-11-12) - re-derive, never quote

Nothing read that line. `grep -rn 'Current state' scripts/ tests/` answered **0 hits** on
2026-11-12, including the two guards whose subject is this project's memory: the rotation
guard reads the bookmark and `check-memory-index.py` an index, and neither opens the state
file or the archive. So a header naming no cycle passed every guard, and one did -- a cycle
wrote `snapshot cyc cyc20261112-220426`, the prefix and the id run together, and the
rotation guard, the index guard and the hand-written archive de-duplication all answered 0.

That is not cosmetic. The subject cell is how a block is *found*: the reading that settled
whether a dead cycle's record had reached its file was a search for its id in the archive,
and a header naming no cycle is invisible to exactly that search. The archive is
append-only, so a malformed header is permanent once it lands.

The admitted forms are measured, not assumed. The archive held **250** block headers when
this tool was written: **249** of the form `snapshot cyc<8 digits>-<6 digits>,` and **one**
`snapshot 2026-10-26T20:45+08:00, host turn - no scheduler cycle` -- a snapshot taken in a
host conversation rather than by a dispatched cycle. A rule admitting only cycle ids would
red on that block, so both forms are admitted and that count is stated here rather than
re-derived at each run.

The second reading is the append's own premise: **the state file's block must not already
be in the archive**. When it is, the append reached the archive and the replacement did
not, so the state file still holds a block that is filed twice.

Report
------
The first line names the checkout that answered, `tree: <path>`, before any verdict -- the
convention every guard in this family follows, and the reason is that a line following the
caller's cwd would make one report true of two different trees. The two lines after it name
the files actually read; `--memory-root` moves the subject and leaves the tree line naming
this guard's own checkout, so nothing about what was read is left implicit.

Exit
----
    0  every header names exactly one subject, in an admitted form, and the state file's
       block is not yet archived
    1  a header naming none or several, a subject cell that is not the id it names, a
       missing state header, or a block filed twice
    2  a file could not be read, reported as `could not measure: ...` -- never a pass
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

#: The state file and its archive, by the names this host keeps them under. Both live in
#: the memory root beside the index and the bookmark, and both are gitignored: the reading
#: is a host-side one, so the names are data here rather than a path a CI leg could build.
STATE_NAME = "evolution-nte-steady-state-since-v0.2.73.md"
ARCHIVE_NAME = "cycle-archive-20260913-state.md"

#: The block header, and the forms its subject cell may take. The subject is the text
#: between `snapshot ` and the comma that opens the date, and it is read from the line
#: rather than from the block, because a header is what names the block a reader has to
#: find.
HEADER = re.compile(r"^## Current state \(snapshot (?P<subject>[^,)]+),")
CYCLE_ID = re.compile(r"cyc\d{8}-\d{6}")
FULL_CYCLE = re.compile(r"cyc\d{8}-\d{6}\Z")
#: The one exception, and it is stated rather than inferred from "not a cycle id": a
#: snapshot written in a host conversation carries the instant it was taken.
HOST_TURN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?[+-]\d{2}:\d{2}\Z")


def checkout_root() -> Path:
    """The checkout this script ships inside, derived from its own file.

    Derived from `<root>/scripts/check-cycle-record.py` rather than written as this host's
    home path, so a copy of the tree measures its own memory instead of naming a directory
    that belongs to one installation -- and so the report's `tree:` line names this
    checkout whatever the caller's cwd. A line that followed the cwd would make the same
    report true of two different trees.
    """
    return Path(__file__).resolve().parent.parent


def default_memory_root() -> Path:
    """The memory root of the agent this script ships inside."""
    return checkout_root() / ".emrg" / "memory"


def read_lines(path: Path) -> str:
    """The file's text.

    :raises OSError: the file is not there or cannot be opened.
    :raises UnicodeDecodeError: the bytes are not UTF-8 text -- the third shape of an
        unreadable file, which is neither an `OSError` nor a missing path.
    """
    return path.read_text(encoding="utf-8")


def header_faults(text: str) -> list[tuple[int, str, str]]:
    """Every block header in `text` whose **subject cell** does not name one admitted form.

    The reading is the subject cell and not the line, which is a distinction the archive
    itself taught: seven blocks there carry a `- displaced by cyc<id>` suffix on the header
    line, so the line names two cycles -- the block's own and the one that displaced it.
    Only the subject cell is the block's identity; the suffix says who filed it.

    :returns: one `(line number, reason, header)` per finding, in file order.
    """
    faults: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = HEADER.match(line)
        if match is None:
            continue
        subject = match.group("subject").strip()
        ids = CYCLE_ID.findall(subject)
        if len(ids) > 1:
            faults.append(
                (
                    number,
                    f"its subject cell names {len(ids)} cycle ids ({', '.join(ids)})",
                    line,
                )
            )
        elif len(ids) == 1 and FULL_CYCLE.match(subject) is None:
            faults.append(
                (
                    number,
                    f"reads its subject as {subject!r}, which is not the cycle id it "
                    "carries",
                    line,
                )
            )
        elif not ids and HOST_TURN.match(subject) is None:
            faults.append(
                (
                    number,
                    f"names no cycle id and no host-turn stamp -- subject {subject!r}",
                    line,
                )
            )
    return faults


def snapshot_block(text: str) -> Optional[str]:
    """The state file's snapshot block: its header through to the end of the file.

    :returns: the block, or `None` when the file opens no snapshot header at all.
    """
    lines = text.splitlines()
    for number, line in enumerate(lines):
        if HEADER.match(line) is not None:
            return "\n".join(lines[number:]).rstrip("\n")
    return None


def main(argv: Optional[list[str]] = None) -> int:
    """Check the record's headers, and that the state block is not filed twice.

    :param argv: the command line, defaults to `sys.argv[1:]`.
    :returns: 0 clean, 1 a finding, 2 the files could not be read.
    """
    parser = argparse.ArgumentParser(
        description="Check a cycle record's snapshot headers and its displaced block."
    )
    parser.add_argument(
        "--memory-root",
        type=Path,
        default=None,
        help="the directory holding the state file and its archive (default: this tree's)",
    )
    parser.add_argument("--state", type=Path, default=None, help="the state file")
    parser.add_argument("--archive", type=Path, default=None, help="the archive")
    args = parser.parse_args(argv)

    root = args.memory_root if args.memory_root is not None else default_memory_root()
    state = args.state if args.state is not None else root / STATE_NAME
    archive = args.archive if args.archive is not None else root / ARCHIVE_NAME

    print(f"tree: {checkout_root()}")
    print(f"state:   {state}")
    print(f"archive: {archive}")

    texts: dict[str, str] = {}
    for label, path in (("state", state), ("archive", archive)):
        try:
            texts[label] = read_lines(path)
        except (OSError, UnicodeDecodeError) as exc:
            print(
                f"could not measure: the {label} file could not be read "
                f"({exc.__class__.__name__}: {exc}) -- this is not a pass, it is a question "
                "this run could not ask"
            )
            return 2

    findings: list[str] = []

    state_faults = header_faults(texts["state"])
    for number, reason, line in state_faults:
        findings.append(f"{state}:{number}: {reason}\n    {line}")
    block = snapshot_block(texts["state"])
    state_headers = sum(1 for line in texts["state"].splitlines() if HEADER.match(line))
    if block is None:
        findings.append(f"{state}: opens no `## Current state (snapshot ...` header at all")
    elif state_headers != 1:
        findings.append(
            f"{state}: opens {state_headers} snapshot headers -- a state file carries the "
            "one current state, so every extra header is a block nothing displaced"
        )

    archive_faults = header_faults(texts["archive"])
    for number, reason, line in archive_faults:
        findings.append(f"{archive}:{number}: {reason}\n    {line}")

    if block is not None and block in texts["archive"].rstrip("\n"):
        findings.append(
            f"{archive}: already holds the block the state file carries -- the append "
            "reached the archive and the replacement did not"
        )

    headers = sum(1 for line in texts["archive"].splitlines() if HEADER.match(line))

    if findings:
        for finding in findings:
            print(f"FAULT: {finding}")
        print(
            f"{len(findings)} finding(s): a block's header is its identity, so one naming "
            "no cycle id or several is a block no reading can find, and a state block "
            "already in the archive is a record filed twice"
        )
        return 1

    print(
        f"OK: the state file opens {state_headers} snapshot header and the archive's "
        f"{headers} block header(s) each name exactly one subject -- a cycle id in the "
        "form `cyc<8 digits>-<6 digits>` or a host-turn stamp -- and the state file's block "
        "is not yet in the archive"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
