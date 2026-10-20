#!/usr/bin/env python3
"""Which registered projects' memory moved since their bookmark - step 0 of the rotation.

Usage
-----
    uv run --no-sync python3 scripts/check-memory-rotation.py --bookmark <PATH>
    uv run --no-sync python3 scripts/check-memory-rotation.py --bookmark <PATH> --all
    uv run --no-sync python3 scripts/check-memory-rotation.py --bookmark <PATH> --json

    # the defaults both point at the paths this host uses
    uv run --no-sync python3 scripts/check-memory-rotation.py

Why this exists
---------------
The evolution prompt's §1.5 makes every cycle answer one question before it reads
anything: **which registered projects' memory is newer than that project's
bookmark line, and what is the newest file**. Step 0 is the step that is never
skipped, and nothing in this repository performed it - `check-memory-index.py`
measures an index against the index rule's two numbers and never looks at
`projects.yml`. Every cycle therefore hand-wrote the probe in a `bash` heredoc,
and it answered wrongly in two ways that both read as a clean result.

**Keying.** A probe that keys rows by `projects.yml`'s `name` field cannot
resolve every row the bookmark carries. The bookmark writes a row's key as
whatever disambiguates that project, and this host's bookmark has three forms:
`emrg` (a bare name), `aitokenpool (github)` (a name plus the parenthetical that
separates two projects sharing it) and `gitlab/shaoaq/mem` (a path suffix, for a
project whose name alone would be ambiguous). Measured in the cycle that filed
issue #2408: a name-keyed probe reported **12 of 31** projects as changed or
unvisited, of which **10** were artefacts of the keying - the rows existed, keyed
another way, so ten projects were invisible to the rotation while the reading
said they had nothing to show.

**The comparison.** `find <dir> -newermt '<stamp>'` parses a stamp written
`%Y-%m-%d %H:%M:%S` and answers **0 files** for the ISO `T` form - the shell
form's silent failure, which makes every project read as unchanged for ever and
is why the bookmark states that measurement as a rule. This tool does not hand
the stamp to any external parser: it parses the stamp itself and compares it
against an `os.stat` mtime, so the format is a reading this file can be wrong
about out loud (a stamp that does not parse is exit **2**, never "unchanged").

What it reads, and what it prints first
--------------------------------------
Two subjects, both named before any verdict: the `projects.yml` (the register of
projects this installation tracks) and the **bookmark** (one Markdown-table row
per project: the newest memory modification time already read, and the cycle
that read it). Per project it looks under `.emrg/memory/**/*.md` and under
`.emrg/sessions/*/memory/**/*.md` - the two places the rotation's rule names - and
keeps the newest file strictly newer than that project's stamp.

Each project lands in exactly one state:

    changed          a memory file is newer than its stamp; the newest is printed
    unchanged        nothing newer under either memory root
    no memory root   the project is on disk and holds no memory file at all (a memory
                     directory with nothing in it is nothing to read)
    path missing     the project's registered path is not on disk

The last two are states a row may **carry as a note** instead of a stamp, and a note is a reading
taken on a day rather than a property: both are re-measured on every run, and a note the disk now
contradicts (`row says path missing`, `row says no memory root`) is a fault with the row named.
Repeating a note would hide a project that came back, or one that started writing memory, for
ever - the same stale-past-state defect as comparing a whole second against a sub-second mtime.
    no bookmark row  the project has a line in `projects.yml` and none in the
                     bookmark: it has never been visited, and §1.5 reads it

Exit codes
----------
    0  a reading was taken, every project has a row, and every row belongs to a project
    1  the coverage the bookmark exists to guarantee is broken: a project with no row,
       a row naming no project, a row that matches more than one, or a row whose state
       field is none of the three forms above
    2  nothing was measured: no `projects.yml`, no bookmark, or a stamp that cannot be
       ordered against an mtime - a value that does not parse, or one later than the
       instant of the reading - which is never a pass

What it does not do
-------------------
It **measures and never repairs**: it does not rewrite the bookmark, add a row, or
touch any project's memory. Advancing a row needs a reader's judgement (which
project was actually read, and to what depth), and §1.5 already says who makes it.

`--all` lists every project with its state; the default lists the projects that
moved plus every fault, because the ordinary cycle has two or three of the first
and none of the second, and 31 unchanged lines are 31 lines of noise in a prompt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

#: The one stamp format a bookmark row may carry, and the one the shell form parses.
#: Spelled as a constant because the comparison below is the only reader of it: a
#: value that does not fit is unmeasurable, and this is the string that decides.
STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime:
    """The instant this reading is taken, as one function so a test can pin it.

    It exists to answer one question a stamp cannot answer about itself: **is this row's
    instant in the future?** `newest_memory` keeps the files strictly newer than a row's
    stamp, so a stamp later than the clock keeps *nothing*, every project reads
    `unchanged`, and the report prints "every project is at or before its stamp" with exit
    0 - a clean verdict that never changes, because the row stays in the future until
    somebody edits it (issue #2421). A function rather than a `datetime.now()` call at the
    one site that needs it, so the boundary can be measured instead of raced.
    """
    return datetime.now()

#: The two states a row may carry instead of a stamp. Both are the reader's own note
#: about the project rather than a time, so neither can be ordered against an mtime.
NO_MEMORY_ROOT = "no memory root"
PATH_MISSING_PREFIX = "path missing"

#: A body row's **key cell**: a code span, with an optional disambiguator that may sit
#: inside the span or just after it. Both forms are in this host's bookmark -
#: `aitokenpool (github)` and `` `1` (Downloads/1) `` - and a parser that insisted on one
#: of them dropped the other row in silence: measured in the cycle that filed issue #2408,
#: a span-only pattern read 30 of 31 rows and reported the missing project as never
#: visited.
KEY_CELL = re.compile(r"^`(?P<key>[^`]+)`\s*(?:\((?P<hint>[^)]*)\))?$")

#: What a row's **value cell** carries: a stamp, or one of the two notes. A cell may append
#: a parenthetical - this host writes `path missing (gone from disk)`, another writes the
#: stamp followed by the memory file it was read from - so the note is read from the cell
#: with any trailing parenthetical removed.
CELL_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")

#: A markdown table's separator line, one cell at a time: `---`, `:---`, `---:`, `:---:`.
SEPARATOR_CELL = re.compile(r"^:?-+:?$")

#: What a stamp cell looks like **before** it is parsed: a leading date. The cell is located
#: by this shape and then parsed strictly by `project_state`, so a stamp written in a form
#: this tool cannot order - the ISO `T` form, whose silent failure is one of the reasons the
#: tool exists - is located here and refused there, with the value named in the message.
#: Parsing here instead would answer "this row is unreadable", which is true but says less.
DATE_SHAPED = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}[ T]")

#: A row is **not** a fixed number of cells. §1.5 fixes what a row *states* - the project,
#: the stamp, and the cycle that read it - not how many columns a table lays those out in:
#: this installation writes three (`| `emrg` | stamp | files |`) and another writes four
#: (`| project | path | stamp | read by |`). Measured on the three-cell-anchored pattern an
#: earlier revision carried, the four-column file matched **0 rows**, and the reading that
#: followed was a false statement about live data ("every project is at or before its
#: stamp") rather than an unmeasurable one. So the value cell is found rather than counted:
#: it is the first cell after the key that carries a stamp or a note, whatever its index.

#: A project's memory, in the two places §1.5's rule names.
MEMORY_SUBDIR = (".emrg", "memory")
SESSIONS_SUBDIR = (".emrg", "sessions")

#: Both defaults are this host's paths. They are defaults rather than required
#: arguments because step 0 runs every cycle on this installation; any other
#: layout is reached by naming the two paths, and the report says which it read.
DEFAULT_PROJECTS = "~/.emrg/projects.yml"
DEFAULT_BOOKMARK = "~/.emrg/evolution/emrg/.emrg/memory/project-memory-rotation-bookmark.md"


class Unmeasurable(Exception):
    """A reading could not be taken. Reported as exit 2, never as a pass."""


def load_projects(path: Path) -> list[dict]:
    """The register: one record per tracked project, each with a `name` and a `path`."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - the repository depends on PyYAML
        raise Unmeasurable(f"PyYAML is not importable, so no register can be read: {exc}") from exc
    if not path.is_file():
        raise Unmeasurable(f"no project register at {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unmeasurable(f"the project register at {path} could not be read: {exc}") from exc
    if not isinstance(data, list):
        raise Unmeasurable(f"the project register at {path} is not a list of records")
    records = []
    for record in data:
        if isinstance(record, dict) and record.get("name") is not None:
            # A register is machine-written YAML, so a project whose name looks numeric
            # (`1`, this host has one) arrives as an `int`. Coerced here because the
            # bookmark writes that key as text: left alone, the row is a `str` and the
            # project an `int`, so `==` answers no and the project reads as never visited.
            records.append(dict(record, name=str(record["name"])))
    return records


def _cells(line: str) -> list[str]:
    """A table line's cells, stripped, with the two halves outside its outer pipes gone."""
    return [cell.strip() for cell in line.strip()[1:-1].split("|")]


def _is_table_line(line: str) -> bool:
    """Whether this line is a row of a markdown table: it opens and closes on a pipe."""
    stripped = line.strip()
    return len(stripped) > 2 and stripped.startswith("|") and stripped.endswith("|")


def _value_of(cells: list[str]) -> str | None:
    """The stamp or note a row carries, from wherever its table puts it.

    Found rather than counted, because the column's index belongs to the installation
    writing the bookmark and not to §1.5's rule (see the block above `SEPARATOR_CELL`). A
    cell qualifies when it is one of the two notes or opens on a date, so a path, a count
    or a cycle id in the same row is skipped instead of mistaken for the value. Whether a
    date-shaped cell is a stamp this tool can order is `project_state`'s question.
    """
    for cell in cells:
        for candidate in (CELL_PARENTHETICAL.sub("", cell).strip(), cell):
            if candidate == NO_MEMORY_ROOT or candidate.startswith(PATH_MISSING_PREFIX):
                return candidate
            if DATE_SHAPED.match(candidate):
                return candidate
    return None


def parse_bookmark(path: Path) -> list[dict]:
    """Every body row of the bookmark's table, in file order, as key/value pairs.

    A table this reader cannot read is **not** an empty table, and neither is a file this
    reader cannot read as a table. Both are reported as unmeasurable, because reading
    either as "no row" would report its projects as never visited while their rows sit in
    the file, and that is the defect this tool was written to end (issue #2408). Two
    shapes are refused for that reason (issue #2419):

    * a file that carries **no table line at all** - the shape §1.5's prose describes,
      "one line per project", which says nothing about a markdown table. Measured before
      this arm existed: a `- `alpha` <stamp>` file, a prose file and a table of rows
      written without code spans all read as `0 row(s)` and produced one "never been
      visited" fault per registered project (exit 1), whose remedy is to re-read every
      project rather than to fix the bookmark's shape;
    * a **body** row that names its project in no code span, each offending line named.
      A header is not a body row: markdown decides that by the separator under it, so a
      header is skipped whether or not its first cell is a code span.

    A table that really is empty - a header and no body rows - keeps the honest reading:
    every project never visited, exit 1. That is a true statement about a first-ever
    bookmark, and the guard above must not swallow it; a test pins both directions,
    because those two files produce the same `0 row(s)` line and differ only in shape.
    """
    if not path.is_file():
        raise Unmeasurable(f"no bookmark at {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise Unmeasurable(f"the bookmark at {path} could not be read: {exc}") from exc
    lines = text.splitlines()
    rows = []
    unreadable = []
    keyless = []
    saw_table = False
    for index, line in enumerate(lines):
        if not _is_table_line(line):
            continue
        saw_table = True
        cells = _cells(line)
        if not cells or all(SEPARATOR_CELL.match(cell) for cell in cells if cell):
            continue                      # the header's separator, not a body row
        following = next((l for l in lines[index + 1:] if l.strip()), "")
        if _is_table_line(following) and all(
                SEPARATOR_CELL.match(cell) for cell in _cells(following) if cell):
            continue                      # a header, with or without a code span
        key_cell = KEY_CELL.match(cells[0]) if cells else None
        if key_cell is None:
            keyless.append(f"line {index + 1}: {line.strip()}")
            continue
        value = _value_of(cells[1:])
        if value is None:
            unreadable.append(f"line {index + 1}: {line.strip()}")
            continue
        key = key_cell.group("key")
        hint = key_cell.group("hint")
        rows.append({"key": f"{key} ({hint})" if hint else key, "value": value})
    if not saw_table:
        raise Unmeasurable(
            f"the bookmark at {path} carries no markdown table - {len(lines)} line(s) read "
            f"and not one of them is a `| ... |` row - so there are no rows here to read: a "
            f"bookmark in another shape is not an empty bookmark, and reading it as one "
            f"would report every registered project as never visited"
        )
    if keyless:
        raise Unmeasurable(
            f"the bookmark at {path} carries {len(keyless)} row(s) this reader cannot "
            f"read - a table row names its project in its first cell, and these do not "
            f"name it in a code span - so the reading below would call their projects "
            f"never visited: " + "; ".join(keyless)
        )
    if unreadable:
        raise Unmeasurable(
            f"the bookmark at {path} carries {len(unreadable)} row(s) this reader cannot "
            f"read - a row names its project in a code span and then no stamp and no note "
            f"in any cell - so the reading below would call their projects never visited: "
            + "; ".join(unreadable)
        )
    return rows


def _segments(path: str) -> list[str]:
    """A path's segments, host and platform separators alike, empty ones dropped."""
    return [s for s in re.split(r"[\\/]+", path) if s]


def _key_matches_path(key: str, project_path: str) -> bool:
    """Whether a path-suffix key names this project's path.

    The bookmark's third form is a suffix (`gitlab/shaoaq/mem`), written because the
    project's own name would not identify it. A segment matches when it equals the
    path's own segment or is a prefix of it, so `gitlab` names `gitlab.xinluex.com`
    without the row having to repeat the host's whole domain.
    """
    key_segments = _segments(key)
    path_segments = _segments(project_path)
    if not key_segments or len(key_segments) > len(path_segments):
        return False
    tail = path_segments[-len(key_segments):]
    return all(k == p or p.startswith(k) for k, p in zip(key_segments, tail))


def resolve_rows(rows: list[dict], projects: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    """Match each row to the project it names.

    Three forms, tried in this order and for this reason: a bare `name` first (the
    common case, and it cannot be ambiguous while only one project carries that name);
    then a name with a parenthetical disambiguator, resolved by matching the hint
    against the project's path (`aitokenpool (github)`); then a path suffix
    (`gitlab/shaoaq/mem`). A row that matches no project, or more than one, is
    returned rather than guessed at.
    """
    by_key: dict[str, dict] = {}
    orphans: list[dict] = []
    ambiguous: list[dict] = []
    for row in rows:
        key = row["key"]
        candidates: list[dict] = []
        name, _, hint = key.partition(" (")
        hint = hint.rstrip(")")
        named = [p for p in projects if p["name"] == name]
        if named and not hint:
            candidates = named
        elif named and hint:
            candidates = [p for p in named if hint in (p.get("path") or "")]
            if not candidates:
                candidates = [p for p in named if _key_matches_path(hint, p.get("path") or "")]
        if not candidates:
            candidates = [p for p in projects
                          if _key_matches_path(key, p.get("path") or "")
                          and p["name"] != key]
        if len(candidates) > 1:
            ambiguous.append({"key": key, "projects": [p["name"] for p in candidates],
                              "paths": [p.get("path") for p in candidates]})
            continue
        if not candidates:
            orphans.append(row)
            continue
        by_key[candidates[0].get("path") or candidates[0]["name"]] = row
    return by_key, orphans, ambiguous


def memory_roots(root: Path) -> list[Path]:
    """The project's memory directories: its own, and each of its sessions'."""
    found = []
    own = root.joinpath(*MEMORY_SUBDIR)
    if own.is_dir():
        found.append(own)
    sessions = root.joinpath(*SESSIONS_SUBDIR)
    if sessions.is_dir():
        try:
            names = sorted(sessions.iterdir())
        except OSError:
            names = []
        for entry in names:
            candidate = entry / "memory"
            if candidate.is_dir():
                found.append(candidate)
    return found


def memory_files(root: Path) -> list[Path]:
    """Every `.md` under the project's memory roots - the files the rotation's rule binds.

    A directory is not a reading. Measured in the cycle that filed issue #2408: three
    projects on this host carry an **empty** `.emrg/sessions/*/memory/` directory, and a
    reading that counted the directory as "this project keeps memory" reported all three as
    contradicting their `no memory root` note. Nothing was there to read.
    """
    found = []
    for memory_root in memory_roots(root):
        for dirpath, _dirnames, filenames in os.walk(memory_root):
            for name in filenames:
                if name.endswith(".md"):
                    found.append(Path(dirpath) / name)
    return found


def newest_memory(root: Path, since: int) -> tuple[Path, float] | None:
    """The newest `.md` under the project's memory roots that is strictly newer than `since`.

    `since` is a **whole second**, and an mtime is truncated to one before it is compared.
    The bookmark is written from `stat`'s `%Sm`, which carries no fraction, so a
    sub-second comparison reports the very file a row was written from as newer for ever:
    measured in the cycle that filed issue #2408, **21 of 23** projects read as changed
    against stamps that already named their newest file - `.real` at `10:19:27.829` against
    a `10:19:27` row. Truncating both sides is what makes the reading idempotent, and it
    loses nothing: a file that really moved lands in a later second.
    """
    best: tuple[Path, float] | None = None
    for path in memory_files(root):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if int(mtime) <= since:
            continue
        if best is None or mtime > best[1]:
            best = (path, mtime)
    return best


def project_state(project: dict, row: dict | None) -> dict:
    """One project's line: its state, and the reading behind it."""
    name = project["name"]
    raw_path = project.get("path") or ""
    state = {"name": name, "path": raw_path, "key": row["key"] if row else None}
    if row is None:
        state.update(state="no bookmark row", bookmark=None, newest=None)
        return state
    value = row["value"]
    root = Path(os.path.expanduser(raw_path))
    if value == NO_MEMORY_ROOT:
        # A note is a reading taken on a day, so it is re-measured rather than repeated: a
        # project that has since written a memory root is one the note would hide for ever,
        # which is the same class of stale-past-state defect as the truncated stamp above.
        state.update(state=NO_MEMORY_ROOT if not memory_files(root) else "row says no memory root",
                     bookmark=None, newest=None)
        return state
    if value.startswith(PATH_MISSING_PREFIX):
        state.update(state=PATH_MISSING_PREFIX if not root.is_dir() else "row says path missing",
                     bookmark=None, newest=None)
        return state
    try:
        stamp = datetime.strptime(value, STAMP_FORMAT)
    except ValueError as exc:
        raise Unmeasurable(
            f"the row for {name} carries {value!r}, which is not a {STAMP_FORMAT} stamp "
            f"and cannot be ordered against a file's mtime ({exc})"
        ) from exc
    taken = now()
    if stamp > taken:
        # Ordered, but against nothing real: an mtime cannot be later than what a cycle has
        # read, so every file on disk is older than this row's instant and the project would
        # read `unchanged` for as long as the row stands (issue #2421).
        raise Unmeasurable(
            f"the row for {name} carries the stamp {value}, later than "
            f"{taken.strftime(STAMP_FORMAT)} - the instant this reading was taken - so it "
            f"records a memory modification that has not happened yet: every file on disk is "
            f"older than it, and the project would read `unchanged` for as long as the row "
            f"stands"
        )
    state["bookmark"] = stamp.strftime(STAMP_FORMAT)
    if not root.is_dir():
        state.update(state="path missing", newest=None)
        return state
    if not memory_files(root):
        state.update(state=NO_MEMORY_ROOT, newest=None)
        return state
    newest = newest_memory(root, int(stamp.timestamp()))
    if newest is None:
        state.update(state="unchanged", newest=None)
        return state
    state.update(state="changed", newest={"path": str(newest[0]),
                                          "mtime": datetime.fromtimestamp(newest[1]).strftime(STAMP_FORMAT)})
    return state


def read(projects_path: Path, bookmark_path: Path) -> dict:
    """The whole reading: both subjects, every project's state, and the coverage faults."""
    projects = load_projects(projects_path)
    rows = parse_bookmark(bookmark_path)
    by_path, orphans, ambiguous = resolve_rows(rows, projects)
    states = [project_state(p, by_path.get(p.get("path") or p["name"])) for p in projects]
    faults = []
    for state in states:
        if state["state"] == "no bookmark row":
            faults.append(f"{state['name']} ({state['path']}) has a line in the register and none in "
                          f"the bookmark: it has never been visited, and §1.5 reads it")
    for state in states:
        if state["state"] == "row says path missing":
            faults.append(f"the bookmark's row for {state['name']} says its path is missing, but "
                          f"{state['path']} is on disk: the row is a note from an earlier day and "
                          f"needs a real stamp")
        elif state["state"] == "row says no memory root":
            faults.append(f"the bookmark's row for {state['name']} says it has no memory root, but "
                          f"{state['path']} has one now: the row is a note from an earlier day and "
                          f"needs a real stamp")
    for row in orphans:
        faults.append(f"the bookmark's row {row['key']!r} names no registered project")
    for item in ambiguous:
        faults.append(f"the bookmark's row {item['key']!r} names {len(item['projects'])} projects "
                      f"({', '.join(item['projects'])}): the key has to disambiguate them")
    return {
        "projects_file": str(projects_path),
        "bookmark_file": str(bookmark_path),
        "project_count": len(projects),
        "row_count": len(rows),
        "states": states,
        "faults": faults,
    }


def render(reading: dict, show_all: bool) -> str:
    """The report, with both subjects named before any verdict."""
    out = [
        f"projects: {reading['projects_file']} ({reading['project_count']} project(s))",
        f"bookmark: {reading['bookmark_file']} ({reading['row_count']} row(s))",
    ]
    changed = [s for s in reading["states"] if s["state"] == "changed"]
    counts: dict[str, int] = {}
    for state in reading["states"]:
        counts[state["state"]] = counts.get(state["state"], 0) + 1
    if changed:
        out.append(f"changed since their bookmark: {len(changed)}")
        for state in changed:
            out.append(f"  {state['name']}  {state['bookmark']} -> {state['newest']['mtime']}  "
                       f"{state['newest']['path']}")
    else:
        out.append("changed since their bookmark: 0 - every project is at or before its stamp")
    if show_all:
        others = [s for s in reading["states"] if s["state"] != "changed"]
        if others:
            out.append("every other project:")
            for state in others:
                out.append(f"  {state['state']:<16} {state['name']}  {state['path']}")
    summary = ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
    out.append(f"states: {summary}" if summary else "states: none recorded")
    if reading["faults"]:
        out.append(f"coverage faults: {len(reading['faults'])}")
        out.extend(f"  {fault}" for fault in reading["faults"])
        out.append("a project with no row is read this cycle, and the bookmark's next write carries "
                   "a line for it; a row naming no project is a stale or mistyped key")
    else:
        out.append(f"coverage: every project has a row and every row names a project "
                   f"({reading['project_count']} of {reading['project_count']})")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Which registered projects' memory moved since their bookmark line.")
    parser.add_argument("--projects", default=DEFAULT_PROJECTS,
                        help=f"the project register (default {DEFAULT_PROJECTS})")
    parser.add_argument("--bookmark", default=DEFAULT_BOOKMARK,
                        help=f"the rotation bookmark (default {DEFAULT_BOOKMARK})")
    parser.add_argument("--all", action="store_true",
                        help="list every project with its state, not only the ones that moved")
    parser.add_argument("--json", action="store_true", help="the same reading, as one document")
    args = parser.parse_args(argv)

    projects_path = Path(os.path.expanduser(args.projects))
    bookmark_path = Path(os.path.expanduser(args.bookmark))
    try:
        reading = read(projects_path, bookmark_path)
    except Unmeasurable as exc:
        print(f"not measurable: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(reading, indent=2, ensure_ascii=False))
    else:
        print(render(reading, args.all))
    return 1 if reading["faults"] else 0


if __name__ == "__main__":
    sys.exit(main())
