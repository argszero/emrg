#!/usr/bin/env python3
"""Check that a rebuilt patch did not silently drop a file the previous one had.

Why this exists
---------------
A patch that drops a test file still satisfies every check a maintainer would
naturally run: it *applies* cleanly, and the suite is *green* - because the
missing file is simply not there to fail. This repo hit exactly that: a fix was
published whose only test file had been dropped while the patch was rebuilt from
a working tree that contained just the two files being edited that cycle. The
code survived (it lives in the file that descended from the previous version),
nothing failed, and a reader comparing the two patches' file lists could see the
loss immediately.

So the free check is a set comparison: list the files each patch touches, and
report any file present in a previous patch and absent from a later one. A
disappearance is either deliberate (and then it should be stated in the publish
text) or it is the defect.

Usage
-----
    python3 scripts/check-patch-files.py old.patch new.patch
    python3 scripts/check-patch-files.py a.patch b.patch c.patch    # each vs the prior
    python3 scripts/check-patch-files.py --list a.patch b.patch

Exit codes
----------
0  every file a previous patch carried is still carried by the next one
1  a file disappeared from the patch (named, with its hunk count)
2  the question could not be answered - no patches given, a patch is unreadable,
   a single patch without --list (there is nothing to compare it against), or a
   patch that parsed to zero files. The last one matters: a parser that matched
   nothing would report "nothing wrong" for every input, so it must never be
   reported as a pass.

The tool reads the patch files it is given, not the working tree; its first
output line names every patch it opened, so a reader can tell what was measured.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

DIFF_HEADER = "diff --git "
HUNK_MARKER = "@@ "
NEW_FILE_MODE = "new file mode "
DELETED_FILE_MODE = "deleted file mode "

DESCRIPTION = (
    "Compare the file lists of two or more patches and fail if a file carried by "
    "an earlier patch is missing from a later one."
)


@dataclass(frozen=True)
class Entry:
    """One file a patch touches."""

    path: str
    hunks: int
    is_new: bool
    is_deleted: bool

    def label(self) -> str:
        kind = "new file" if self.is_new else ("deletes" if self.is_deleted else "edits")
        return f"{kind} {self.path}" + (f" ({self.hunks} hunks)" if self.hunks else "")


def _path_from_diff_header(line: str) -> str | None:
    """The post-image path of a `diff --git` header, or None if unparseable.

    ``shlex`` rather than a regex: git quotes a path containing whitespace, so
    splitting on spaces silently truncates exactly the paths a regex is most
    likely to mis-handle.
    """
    rest = line[len(DIFF_HEADER):]
    try:
        parts = shlex.split(rest)
    except ValueError:
        parts = rest.split()
    if len(parts) < 2:
        return None
    target = parts[1]
    return target[2:] if target.startswith("b/") else target


def parse_patch(text: str) -> dict[str, Entry]:
    """{path: Entry} for every file the patch touches, in the order git wrote them."""
    entries: dict[str, Entry] = {}
    current: str | None = None
    hunks = 0
    is_new = is_deleted = False

    def flush() -> None:
        if current is not None:
            entries[current] = Entry(current, hunks, is_new, is_deleted)

    for line in text.splitlines():
        if line.startswith(DIFF_HEADER):
            flush()
            current = _path_from_diff_header(line)
            hunks, is_new, is_deleted = 0, False, False
            continue
        if current is None:
            continue
        if line.startswith(HUNK_MARKER):
            hunks += 1
        elif line.startswith(NEW_FILE_MODE):
            is_new = True
        elif line.startswith(DELETED_FILE_MODE):
            is_deleted = True
    flush()
    return entries


def _describe(label: str, entries: dict[str, Entry]) -> None:
    print(f"  {label}: {len(entries)} file(s)")
    for entry in entries.values():
        print(f"      {entry.label()}")


def _load(path: str) -> tuple[dict[str, Entry], str] | None:
    target = Path(path)
    if not target.is_file():
        print(f"unmeasurable: no such patch file: {path}")
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # unreadable file (permissions, a directory, ...)
        print(f"unmeasurable: cannot read {path}: {exc}")
        return None
    return parse_patch(text), text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("patches", nargs="*", help="patch files, oldest first")
    parser.add_argument(
        "--list",
        action="store_true",
        help="print each patch's file set and stop (no comparison)",
    )
    args = parser.parse_args(argv)

    if not args.patches:
        parser.print_help()
        return 2
    if len(args.patches) == 1 and not args.list:
        print(
            "unmeasurable: one patch given and nothing to compare it against; "
            f"pass a second patch or use --list ({args.patches[0]})"
        )
        return 2

    print("patches read:")
    for path in args.patches:
        print(f"    {path}")

    loaded: list[tuple[str, dict[str, Entry]]] = []
    for path in args.patches:
        result = _load(path)
        if result is None:
            return 2
        entries = result[0]
        if not entries:
            # The failure this guard must not have: a parser that matched
            # nothing agrees with every input, so "no file disappeared" would
            # be a verdict about the parser, not about the patches.
            print(
                f"unmeasurable: {path} parsed to 0 files - it is empty, not a diff, "
                "or in a format this tool does not read; reporting 'nothing wrong' "
                "here would be a verdict about the parser"
            )
            return 2
        loaded.append((path, entries))

    if args.list:
        for path, entries in loaded:
            print()
            _describe(Path(path).name, entries)
        return 0

    dropped_any = False
    for (prev_path, prev), (cur_path, cur) in zip(loaded, loaded[1:]):
        lost = [prev[f] for f in prev if f not in cur]
        gained = [cur[f] for f in cur if f not in prev]
        print()
        print(f"{Path(prev_path).name}  ->  {Path(cur_path).name}")
        _describe("previous", prev)
        _describe("current ", cur)
        if gained:
            print(f"  gained: {[e.path for e in gained]}")
        if lost:
            dropped_any = True
            print(f"  FAIL: {len(lost)} file(s) dropped")
            for entry in lost:
                print(f"      {entry.label()}")
            print(
                "      A dropped file is either deliberate (say so where the patch "
                "is published) or a rebuild defect: the patch still applies and the "
                "suite is still green, because the file is absent."
            )
        else:
            print("  OK: no file the previous patch carried is missing from this one")
    return 1 if dropped_any else 0


if __name__ == "__main__":
    sys.exit(main())
