#!/usr/bin/env python3
"""Every timestamp in a memory file's frontmatter must have happened by now.

Reads the tree it stands in and says so in its first line: the files it scans are
found relative to this file's own repository root, not to the caller's cwd.

Usage
-----
    uv run --no-sync python3 scripts/check-memory-timestamps.py            # the rule
    uv run --no-sync python3 scripts/check-memory-timestamps.py --help

The class this exists for
-------------------------
A memory file opens with YAML frontmatter, and this repository's convention puts the
dates it records there (``event_at`` - when the event happened, ``created_at`` /
``updated_at`` - when the file was written). Those are **measurements**: the moment is
something a writer reads off a clock. Writing one from memory instead produces a value
no clock can have reached yet, and the value looks exactly like a correct one - which is
why this is a class of defect rather than a slip.

Measured 2026-10-01 on this host, while this tool was being written: three files under
``.emrg/memory/`` carried ``updated_at`` in the future - one by 10 minutes, one by 3 -
written by an earlier evolution cycle in the same session, each having invented a
plausible-looking time instead of reading one. Two earlier cycles had done the same
(six records over the day, the first noticed only by accident). Nothing reads those
fields, so nothing noticed; the frontmatter is prose that happens to parse.

What is read, and what is deliberately not
------------------------------------------
The **frontmatter block only** - the text between the leading ``---`` fence and the next
one - and inside it every ``key: value`` whose value parses as an ISO date-time. Any key,
not a fixed list: the convention's three names are not the whole of it (a record may carry
``completed``), and a rule that named fields would let a fourth one through.

* **A file with no frontmatter is not a finding.** ``MEMORY.md`` is a title and rows, with
  no block at all, and the memory instructions require that shape of the index. The
  absence of a block is not a false date.
* **A value that is not a date-time is not a finding** - ``id: pending001``, a status, a
  scope. Only what parses is compared, and anything else is left to its own reader.
* **The body is not read.** A date in the prose is part of a sentence (a citation of a
  commit, a description of a rule), not a claim about when this file was written.
* **Whether the moment is *right* is out of scope.** This answers "is it in the future",
  a question a clock settles. Whether ``event_at`` truthfully names when the event
  happened is a reader's judgement, and no tool here can make it.

The tolerance, and why it is stated
-----------------------------------
``CLOCK_SKEW_TOLERANCE_SECONDS`` is 120. Writer and reader share one host clock, so the
edge is not a real skew - it is there so that a stamp taken a second before a file is
written, on a machine whose clock is being corrected by NTP, is not reported as a
fabrication. 120 rather than 0 so the guard has no hairline; 120 rather than an hour so a
value invented "roughly now, a bit ahead" is still caught. The number is a decision, and
it is written where its reader can see it rather than left implicit in a comparison.

Which tree answered
-------------------
The first line is ``tree: <resolved root>`` before any verdict, the convention every guard
in this family carries. Its subject is the tree it is run against (defaulting to the
checkout this file lives in), so the same report is not silently true of another.

Exit codes
----------
``0``  every timestamp in every memory file's frontmatter is at or before now, allowing
       the tolerance above.
``1``  at least one is in the future; each is printed with its file, its field, the value
       written, and how far ahead of the clock it is.
``2``  nothing could be measured: no memory file under the tree, or every named path
       unreadable - which would make a green verdict a reading over an empty set.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path
from typing import NamedTuple, Optional

#: How far ahead of the clock a stamp may be and still pass. See the docstring: not a
#: real skew (writer and reader share one host clock), but the margin that keeps a
#: stamp taken a moment before the write from being reported as a fabrication.
CLOCK_SKEW_TOLERANCE_SECONDS = 120

RUNNER = "uv run --no-sync python3"

#: The frontmatter block: the leading `---` fence, its content, and the closing fence.
#: Anchored at the start of the file, because that is where the format puts it - a later
#: `---` is a horizontal rule in the body, not a second block.
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)

#: One `key: value` line of that block whose value begins with an ISO date. The parse is
#: what decides whether the value is a date-time (below), not this pattern: a pattern that
#: tried to spell the whole format would disagree with `fromisoformat` about some form.
_KEY_VALUE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(\S+)[ \t]*$", re.MULTILINE)


class Stamp(NamedTuple):
    """One dated field of one memory file.

    :param path: the file it was read from.
    :param line: its 1-based line in that file, so a finding can be opened.
    :param field: the frontmatter key.
    :param value: the text as written, which is what a reader has to repair.
    :param when: the value parsed.
    """

    path: Path
    line: int
    field: str
    value: str
    when: datetime.datetime


def memory_files(root: Path) -> list[Path]:
    """The memory files the tree at `root` carries, in a stable order.

    Both levels the memory instructions name for a tree: the project's memory directory
    and one per session under it - the same two `scripts/check-memory-index.py` reads,
    one level wider (every `.md` file there, not only the index), because the frontmatter
    this tool reads lives in the detail files the index points at.

    Derived by *globbing the filesystem*, never by constructing a store:
    `MemoryStore.__init__` mkdirs the directory it is handed, and a reading with a side
    effect is how a count comes to create the thing it counts.

    :param root: the tree to look in.
    :returns: the `.md` files that exist, empty when it carries no memory directory.
    """
    candidates = list((root / ".emrg" / "memory").glob("*.md"))
    candidates.extend(sorted((root / ".emrg" / "sessions").glob("*/memory/*.md")))
    return sorted({path for path in candidates if path.is_file()})


def stamps_in(path: Path) -> list[Stamp]:
    """Every dated frontmatter field of `path`, in file order.

    :param path: a memory file.
    :returns: the stamps; empty when the file has no frontmatter or no dated field.
    :raises OSError: the file could not be read.
    :raises UnicodeDecodeError: it is not UTF-8 text.
    """
    text = path.read_text(encoding="utf-8")
    block = _FRONTMATTER.match(text)
    if not block:
        return []
    out: list[Stamp] = []
    # Line numbers are counted from the block's own start, so `line` is a line of the
    # file: the opening fence is line 1 and the content begins on line 2.
    for offset, match in enumerate(_KEY_VALUE.finditer(block.group(1))):
        value = match.group(2)
        try:
            when = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        line = block.group(1)[: match.start()].count("\n") + 2
        out.append(Stamp(path, line, match.group(1), value, when))
    return out


def ahead_by(stamp: Stamp, now: datetime.datetime) -> float:
    """How many seconds ahead of `now` a stamp is; negative when it is in the past.

    A stamp that names no zone is read as local time, which is what a hand-written
    frontmatter means and what `datetime.now()` returns - so the two are comparable
    and a naive stamp is not silently treated as UTC.

    :param stamp: the dated field.
    :param now: the reading of the clock.
    :returns: seconds, signed.
    """
    when = stamp.when
    if when.tzinfo is None and now.tzinfo is not None:
        return (when - now.replace(tzinfo=None)).total_seconds()
    if when.tzinfo is not None and now.tzinfo is None:
        return (when - now.astimezone(when.tzinfo)).total_seconds()
    return (when - now).total_seconds()


def _fmt(seconds: float) -> str:
    """A span of seconds as the unit a reader can act on."""
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f}min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def main(argv: Optional[list[str]] = None) -> int:
    """Read every memory file's frontmatter and compare its stamps with the clock.

    :param argv: the command line, defaults to `sys.argv[1:]`.
    :returns: the exit code the docstring states.
    """
    # A merged reader must see the `tree:` line before any verdict, and this family's
    # docstrings promise that order. stdout is block-buffered when it is a pipe (how a
    # cycle reads a report: `2>&1 | tail`) while stderr is not, so without this every
    # stderr line overtakes the tree line - measured 2026-09-26 on the sibling guards.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description=(
            "Every timestamp in a memory file's frontmatter must have happened by now - "
            f"allowing {CLOCK_SKEW_TOLERANCE_SECONDS}s of clock slack. A stamp in the "
            "future was written from memory rather than read off a clock, and nothing "
            "else in this tree notices one."
        ),
        epilog=(
            "Exit 0: every stamp is at or before now. Exit 1: at least one is in the "
            "future, printed with its file, field and value. Exit 2: nothing could be "
            "measured (no memory file under the tree, or a named path unreadable). "
            f"Example: {RUNNER} scripts/check-memory-timestamps.py"
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help=(
            "memory file(s) to read (default: the memory directories under the tree "
            "this file lives in)"
        ),
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    print(f"tree: {root}")

    if args.paths:
        subjects: list[Path] = []
        for raw in args.paths:
            path = Path(raw)
            subjects.extend(
                sorted(p for p in path.glob("*.md")) if path.is_dir()
                else [path]
            )
    else:
        subjects = memory_files(root)

    if not subjects:
        print(
            "could not measure: no memory file under "
            f"{root / '.emrg' / 'memory'} or {root / '.emrg' / 'sessions'}/*/memory, so "
            "a green verdict would be a reading over an empty set",
            file=sys.stderr,
        )
        return 2

    now = datetime.datetime.now(datetime.timezone.utc).astimezone()
    read = 0
    findings: list[tuple[Stamp, float]] = []
    for path in subjects:
        try:
            stamps = stamps_in(path)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"could not measure: {path} ({type(exc).__name__}: {exc})", file=sys.stderr)
            return 2
        read += 1
        for stamp in stamps:
            ahead = ahead_by(stamp, now)
            if ahead > CLOCK_SKEW_TOLERANCE_SECONDS:
                findings.append((stamp, ahead))

    if not findings:
        print(
            f"OK: {read} memory file(s) read, every frontmatter timestamp at or before "
            f"now (tolerance {CLOCK_SKEW_TOLERANCE_SECONDS}s)"
        )
        return 0

    for stamp, ahead in findings:
        print(
            f"{stamp.path}:{stamp.line} {stamp.field} is in the future: "
            f"{stamp.value!r}, {_fmt(ahead)} ahead of this reading"
        )
    print(
        f"{len(findings)} timestamp(s) ahead of the clock in {read} memory file(s) read; "
        "a stamp is a reading of a clock, so the remedy is to write the measured time "
        "(the writer's own `date`), not a plausible one"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
