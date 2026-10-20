"""The rotation reading: `scripts/check-memory-rotation.py`.

What this file holds
--------------------
The tool answers §1.5's step 0 - which registered projects' memory is newer than that
project's bookmark row - and it exists because two hand-written probes answered it
wrongly while reading as clean (issue #2408). So the assertions here are the three
halves of "does it answer that question, and can a wrong answer look right":

* **each boundary is read in both states, and they are built from what the tool reads,
  never from a literal** - a file *at* its stamp is `unchanged` and one *after* it is
  `changed`; the file a row was written from, carrying a sub-second fraction, is
  `unchanged` (a comparison that kept the fraction reported **21 of 23** projects as
  changed on this host while their rows already named that very file); a project with a
  memory root and one without; a path on disk and a path that is gone.
* **all three key forms the bookmark writes are resolved** - a bare name, a name with a
  parenthetical disambiguator (inside the code span and just after it, because this
  host's bookmark carries both), and a path suffix. A form the parser drops is a project
  that leaves the rotation in silence, which is the failure the tool was written for.
* **a reading that could not be taken is never a pass** - no register, no bookmark, a
  stamp that does not parse, and a row that matches two projects all answer exit **2**
  or exit **1** with the reason named, and `0` is reserved for a reading that covered
  every project and every row;
* **a note from an earlier day is re-measured, not repeated** - `no memory root` and
  `path missing` are readings, so a row carrying one the disk now contradicts is a fault
  rather than a verdict that hides a project for ever.

It **measures and never repairs**: one assertion reads the bookmark's bytes before and
after a run, because a tool that quietly rewrote rows would hide exactly the drift its
report exists to show.

Loading
-------
By path (`spec_from_file_location`), the shape `test_check_memory_index.py` uses: the
filename is not importable by name (the dash), and the module stands alone - it imports
nothing from the package, so there is no tree to put on `sys.path`.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-memory-rotation.py"

#: One stamp, in the tool's own format. Every fixture time is an offset from it, so the
#: reading and the test cannot drift apart by a hand-written date.
BASE = "2026-10-20 16:06:24"


def _load():
    spec = importlib.util.spec_from_file_location("check_memory_rotation", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _stamp(mod, seconds: int = 0) -> float:
    """`BASE` plus `seconds`, as an mtime the tool will compare against."""
    from datetime import datetime, timedelta

    return (datetime.strptime(BASE, mod.STAMP_FORMAT) + timedelta(seconds=seconds)).timestamp()


def _write_memory(root: Path, relative: str, mtime: float) -> Path:
    """A memory file at `relative` under `root`, stamped `mtime`."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# memory\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _projects(path: Path, records: list[dict]) -> Path:
    """The register, written the way the daemon writes it: every scalar quoted."""
    text = "".join(f"- name: '{r['name']}'\n  path: '{r['path']}'\n" for r in records)
    path.write_text(text, encoding="utf-8")
    return path


def _bookmark(path: Path, rows: list[tuple[str, str]]) -> Path:
    """A bookmark with a title, the table's two header lines, and one row per pair."""
    lines = ["# Bookmark", "", "| project | newest read | files |", "| --- | --- | --- |"]
    lines += [f"| {key} | {value} | 0 |" for key, value in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _reading(mod, tmp_path: Path, projects: list[dict], rows: list[tuple[str, str]]):
    register = _projects(tmp_path / "projects.yml", projects)
    book = _bookmark(tmp_path / "bookmark.md", rows)
    return mod.read(register, book)


def _root(tmp_path: Path, name: str) -> str:
    root = tmp_path / "projects" / name
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


# --------------------------------------------------------------------------- the states


def test_a_file_at_its_stamp_is_unchanged_and_one_second_later_is_not(mod, tmp_path):
    """The boundary, in both states, from the stamp the tool itself parses."""
    at, after = _root(tmp_path, "at"), _root(tmp_path, "after")
    _write_memory(Path(at), ".emrg/memory/MEMORY.md", _stamp(mod))
    _write_memory(Path(after), ".emrg/memory/MEMORY.md", _stamp(mod, 1))
    reading = _reading(mod, tmp_path,
                       [{"name": "at", "path": at}, {"name": "after", "path": after}],
                       [("`at`", BASE), ("`after`", BASE)])
    states = {s["name"]: s for s in reading["states"]}
    assert states["at"]["state"] == "unchanged"
    assert states["after"]["state"] == "changed"
    assert states["after"]["newest"]["mtime"] == "2026-10-20 16:06:25"
    assert reading["faults"] == []


def test_a_sub_second_fraction_does_not_re_flag_the_file_its_row_was_written_from(mod, tmp_path):
    """The row is written from `stat`'s second-precision `%Sm`; the file keeps a fraction.

    This is the defect the tool was written against: a comparison that kept the fraction
    reported 21 of 23 projects as changed on this host while their rows already named the
    very files it was reporting.
    """
    root = _root(tmp_path, "fraction")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod) + 0.825689)
    reading = _reading(mod, tmp_path, [{"name": "fraction", "path": root}], [("`fraction`", BASE)])
    assert reading["states"][0]["state"] == "unchanged"


def test_a_sessions_memory_counts_and_a_non_markdown_file_does_not(mod, tmp_path):
    """Both roots §1.5 names are read, and only the files its rule names."""
    root = _root(tmp_path, "sessioned")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod))
    _write_memory(Path(root), ".emrg/sessions/s_1/memory/note.md", _stamp(mod, 5))
    _write_memory(Path(root), ".emrg/memory/notes.txt", _stamp(mod, 9))
    reading = _reading(mod, tmp_path, [{"name": "sessioned", "path": root}],
                       [("`sessioned`", BASE)])
    state = reading["states"][0]
    assert state["state"] == "changed"
    assert Path(state["newest"]["path"]).parts[-3:] == ("s_1", "memory", "note.md"), state["newest"]
    assert not state["newest"]["path"].endswith(".txt")


def test_the_newest_of_several_is_the_one_named(mod, tmp_path):
    """A reading that kept the first file it walked would name the wrong one."""
    root = _root(tmp_path, "several")
    _write_memory(Path(root), ".emrg/memory/a.md", _stamp(mod, 3))
    newest = _write_memory(Path(root), ".emrg/memory/b.md", _stamp(mod, 7))
    reading = _reading(mod, tmp_path, [{"name": "several", "path": root}], [("`several`", BASE)])
    assert reading["states"][0]["newest"]["path"] == str(newest)


def test_a_row_stamped_later_than_the_clock_is_unmeasurable(mod, tmp_path, monkeypatch):
    """A row stamped after the reading refuses, because it would keep every file for ever.

    `newest_memory` keeps the files strictly newer than the row's instant, so a row stamped
    later than the clock keeps *nothing*: its project reads `unchanged`, and a bookmark with
    a row for every project prints "every project is at or before its stamp" and exits 0 —
    the silently-forever failure the module docstring already catalogues for the ISO `T`
    form and for the sub-second comparison, with a stamp that parses. Measured in the cycle
    that filed issue #2421: rows stamped `2099-01-01 00:00:00` answered `changed since their
    bookmark: 0` with exit 0, while the same bookmark stamped in the past named both changed
    files. The refusal is asserted through `main` as well, because it is the tool's own
    reading and not only a function's return.
    """
    from datetime import datetime, timedelta

    root = _root(tmp_path, "ahead")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod))
    taken = datetime.strptime(BASE, mod.STAMP_FORMAT)
    monkeypatch.setattr(mod, "now", lambda: taken)
    ahead = (taken + timedelta(seconds=1)).strftime(mod.STAMP_FORMAT)
    register = _projects(tmp_path / "projects.yml", [{"name": "ahead", "path": root}])
    book = _bookmark(tmp_path / "ahead.md", [("`ahead`", ahead)])
    with pytest.raises(mod.Unmeasurable) as excinfo:
        mod.read(register, book)
    message = str(excinfo.value)
    assert ahead in message, message
    assert taken.strftime(mod.STAMP_FORMAT) in message, message
    assert "ahead" in message, message
    assert mod.main(["--projects", str(register), "--bookmark", str(book)]) == 2


def test_a_row_stamped_at_the_clock_is_read_normally(mod, tmp_path, monkeypatch):
    """The other half of the same second: one second earlier is a reading, not a refusal.

    These two rows differ by one second and by nothing else, so a guard that refused both
    would look exactly like one that refused neither.
    """
    from datetime import datetime

    root = _root(tmp_path, "at-the-clock")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod))
    taken = datetime.strptime(BASE, mod.STAMP_FORMAT)
    monkeypatch.setattr(mod, "now", lambda: taken)
    register = _projects(tmp_path / "projects.yml", [{"name": "at-the-clock", "path": root}])
    book = _bookmark(tmp_path / "at.md", [("`at-the-clock`", BASE)])
    reading = mod.read(register, book)
    assert reading["states"][0]["state"] == "unchanged"
    assert mod.main(["--projects", str(register), "--bookmark", str(book)]) == 0


def test_a_project_with_no_memory_root_and_one_whose_path_is_gone(mod, tmp_path):
    """Two states that are not a time: neither can be ordered against an mtime."""
    bare = _root(tmp_path, "bare")
    gone = str(tmp_path / "projects" / "gone")
    reading = _reading(mod, tmp_path,
                       [{"name": "bare", "path": bare}, {"name": "gone", "path": gone}],
                       [("`bare`", "no memory root"), ("`gone`", "path missing (gone from disk)")])
    states = {s["name"]: s for s in reading["states"]}
    assert states["bare"]["state"] == "no memory root"
    assert states["gone"]["state"] == "path missing"
    assert reading["faults"] == []


def test_a_note_the_disk_contradicts_is_a_fault_in_both_directions(mod, tmp_path):
    """A row may carry `no memory root` / `path missing` instead of a stamp - and those are
    readings from a day, not properties. Repeating a note would hide a project that started
    writing memory or came back on disk, for ever: the class of stale-past-state defect the
    sub-second comparison belongs to as well. Each direction is asserted, and each is
    asserted where the note is still *true* too, so the fault cannot fire unconditionally.
    """
    now_memory = _root(tmp_path, "now-memory")
    _write_memory(Path(now_memory), ".emrg/memory/MEMORY.md", _stamp(mod))
    still_bare = _root(tmp_path, "still-bare")
    back = _root(tmp_path, "back")
    still_gone = str(tmp_path / "projects" / "still-gone")

    reading = _reading(mod, tmp_path,
                       [{"name": "now-memory", "path": now_memory},
                        {"name": "still-bare", "path": still_bare},
                        {"name": "back", "path": back},
                        {"name": "still-gone", "path": still_gone}],
                       [("`now-memory`", "no memory root"), ("`still-bare`", "no memory root"),
                        ("`back`", "path missing (gone from disk)"),
                        ("`still-gone`", "path missing (gone from disk)")])
    states = {s["name"]: s["state"] for s in reading["states"]}
    assert states["now-memory"] == "row says no memory root"
    assert states["still-bare"] == "no memory root"
    assert states["back"] == "row says path missing"
    assert states["still-gone"] == "path missing"
    assert len(reading["faults"]) == 2, reading["faults"]
    assert any("has one now" in f for f in reading["faults"])
    assert any("is on disk" in f for f in reading["faults"])


def test_an_empty_memory_directory_is_nothing_to_read(mod, tmp_path):
    """A directory is not a reading: three projects on this host carry an empty
    `.emrg/sessions/*/memory/`, and counting the directory reported all three as
    contradicting their `no memory root` note while nothing was there to read."""
    empty = _root(tmp_path, "empty-root")
    (Path(empty) / ".emrg" / "sessions" / "s_1" / "memory").mkdir(parents=True)
    reading = _reading(mod, tmp_path, [{"name": "empty-root", "path": empty}],
                       [("`empty-root`", "no memory root")])
    assert reading["states"][0]["state"] == "no memory root"
    assert reading["faults"] == [], reading["faults"]


# ----------------------------------------------------------------------- the three keys


def test_all_three_key_forms_the_bookmark_writes_resolve(mod, tmp_path):
    """A bare name, a parenthetical that separates two same-named projects, a path suffix.

    The middle form is asserted in **both** spellings, because this host's bookmark
    carries both: the disambiguator inside the code span (`aitokenpool (github)`) and just
    after it (`1` (Downloads/1)). A parser that insisted on one dropped the other row and
    reported its project as never visited.
    """
    git = _root(tmp_path, "github/aitokenpool")
    lab = _root(tmp_path, "gitlab.xinluex.com/shaoaq/aitokenpool")
    mem = _root(tmp_path, "gitlab.xinluex.com/shaoaq/mem")
    download = _root(tmp_path, "Downloads/1")
    for root in (git, lab, mem, download):
        _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod))
    projects = [
        {"name": "aitokenpool", "path": git},
        {"name": "aitokenpool", "path": lab},
        {"name": "mem", "path": mem},
        {"name": "1", "path": download},
    ]
    reading = _reading(mod, tmp_path, projects, [
        ("`aitokenpool` (github)", BASE),          # a name, hint inside the span
        ("`gitlab/shaoaq/aitokenpool`", BASE),     # a path suffix, same name as the first
        ("`gitlab/shaoaq/mem`", BASE),             # a path suffix for an unambiguous name
        ("`1` (Downloads/1)", BASE),               # a name, hint just after the span
    ])
    by_key = {s["key"]: s for s in reading["states"]}
    assert by_key["aitokenpool (github)"]["path"] == git
    assert by_key["gitlab/shaoaq/aitokenpool"]["path"] == lab
    assert by_key["gitlab/shaoaq/mem"]["path"] == mem
    assert by_key["1 (Downloads/1)"]["path"] == download
    assert reading["faults"] == [], reading["faults"]


def test_a_project_whose_name_looks_numeric_still_matches_its_row(mod, tmp_path):
    """Machine-written YAML hands `1` over as an `int`, and the row writes it as text.

    Left as the loader found it, the comparison is `int == str` and answers no: the project
    reads as never visited while its row sits in the file. This host registers a project by
    exactly that name.
    """
    root = _root(tmp_path, "numeric")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod))
    register = tmp_path / "projects.yml"
    register.write_text(f"- name: 1\n  path: '{root}'\n", encoding="utf-8")
    book = _bookmark(tmp_path / "bookmark.md", [("`1`", BASE)])
    reading = mod.read(register, book)
    assert reading["states"][0]["name"] == "1"
    assert reading["states"][0]["state"] == "unchanged"
    assert reading["faults"] == []


def test_a_bare_name_shared_by_two_projects_is_reported_rather_than_guessed(mod, tmp_path):
    """Two projects, one name, one bare row: which was read is a judgement, not a match."""
    one, two = _root(tmp_path, "one"), _root(tmp_path, "two")
    reading = _reading(mod, tmp_path,
                       [{"name": "twin", "path": one}, {"name": "twin", "path": two}],
                       [("`twin`", BASE)])
    assert len(reading["faults"]) == 3, reading["faults"]      # ambiguous row + two unrowed
    assert any("names 2 projects" in f for f in reading["faults"])
    assert sum(1 for f in reading["faults"] if "never been visited" in f) == 2

# -------------------------------------------------------- measuring, and never a pass


def test_a_row_is_found_in_a_table_of_any_width(mod, tmp_path):
    """§1.5 fixes what a row states, not how many columns the table lays it out in.

    This installation writes three cells (`| `name` | stamp | files |`); another writes
    four (`| project | path | stamp | read by |`), and three cells was the whole of the
    pattern an earlier revision matched on. Measured against a four-column bookmark, that
    pattern read **0 rows**, so every project came back `no bookmark row` and the report
    said "every project is at or before its stamp" - a false statement about live data,
    where the shape is exactly what the repository has twice refused to answer (#2275,
    #2277: *could not read* narrowed into *read as empty*). Both files must read alike.
    """
    wide = _root(tmp_path, "wide")
    narrow = _root(tmp_path, "narrow")
    for root in (wide, narrow):
        _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod, 60))
    register = _projects(tmp_path / "projects.yml", [
        {"name": "wide", "path": wide}, {"name": "narrow", "path": narrow}])

    # The same two rows, in the two tables the two installations write. The wide one puts
    # the project's path and the reading cycle on either side of the stamp, so a reader
    # that counted cells would take the path for the value.
    narrow_book = _bookmark(tmp_path / "narrow.md", [("`wide`", BASE), ("`narrow`", BASE)])
    wide_book = tmp_path / "wide.md"
    wide_book.write_text("\n".join([
        "# Bookmark", "",
        "| project | path | newest | read by |",
        "| --- | --- | --- | --- |",
        f"| `wide` | {wide} | {BASE} (`MEMORY.md`) | cyc20261020-141450 |",
        f"| `narrow` | {narrow} | {BASE} | cyc20261020-141450 |",
    ]) + "\n", encoding="utf-8")

    narrow_reading = mod.read(register, narrow_book)
    wide_reading = mod.read(register, wide_book)
    assert wide_reading["row_count"] == 2
    assert wide_reading["faults"] == [], wide_reading["faults"]
    assert [(s["key"], s["state"], s["bookmark"]) for s in wide_reading["states"]] == \
           [(s["key"], s["state"], s["bookmark"]) for s in narrow_reading["states"]]
    # And it is a reading, not an empty one that happens to agree: both files say `changed`.
    assert [s["state"] for s in wide_reading["states"]] == ["changed", "changed"]


def test_a_row_that_carries_no_stamp_is_unmeasurable_rather_than_dropped(mod, tmp_path):
    """The other half: a table this reader cannot read must not answer as an empty one.

    With the rows located by shape, the remaining failure is a row that names its project
    in a code span and then carries neither a stamp nor a note in any cell. Dropping it
    would report that project as never visited while its row sits in the file - the
    original defect - so it is exit 2, and the message names the line it could not read.
    """
    root = _root(tmp_path, "unreadable")
    register = _projects(tmp_path / "projects.yml", [{"name": "unreadable", "path": root}])
    book = _bookmark(tmp_path / "bookmark.md", [("`unreadable`", "read on Tuesday")])
    with pytest.raises(mod.Unmeasurable) as excinfo:
        mod.read(register, book)
    message = str(excinfo.value)
    assert "cannot" in message and "never visited" in message
    assert "line 5" in message and "`unreadable`" in message


def test_a_header_written_with_a_code_span_is_not_a_body_row(mod, tmp_path):
    """The rows are located by shape, so the header is the one shape that must not qualify.

    A table may label its columns with code spans (`| `project` | `newest` |`), and read as a
    body row that header would ask for a stamp the header does not carry - turning a
    readable bookmark into an unmeasurable one. Markdown decides a header by the separator
    under it, which is what this pins, and the body rows beneath it still read.
    """
    root = _root(tmp_path, "spanned")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod, 60))
    register = _projects(tmp_path / "projects.yml", [{"name": "spanned", "path": root}])
    book = tmp_path / "bookmark.md"
    book.write_text("\n".join([
        "# Bookmark", "",
        "| `project` | `newest` | `files` |",
        "| --- | --- | --- |",
        f"| `spanned` | {BASE} | 0 |",
    ]) + "\n", encoding="utf-8")
    reading = mod.read(register, book)
    assert reading["row_count"] == 1
    assert reading["states"][0]["state"] == "changed"
    assert reading["faults"] == [], reading["faults"]


def test_a_bookmark_in_another_shape_is_unmeasurable_rather_than_empty(mod, tmp_path):
    """The shape §1.5's prose asks for - "one line per project" - is not a table.

    Nothing in that sentence says markdown table with a code span in the first cell, so a
    cycle that follows it writes a file this reader cannot parse. Read as an empty bookmark
    that file produces one "never been visited" fault per project (exit 1), whose remedy is
    to re-read all of them — the opposite of the truth, which is that the bookmark's shape
    needs fixing. Measured on this host in the cycle that filed issue #2419: a bullet-list
    bookmark and a prose-only bookmark both answered `0 row(s)` with a fault per project.
    """
    root = _root(tmp_path, "unlisted")
    register = _projects(tmp_path / "projects.yml", [{"name": "unlisted", "path": root}])
    for shape, text in (
            ("one line per project",
             f"# Rotation bookmark\n\n- `unlisted` {BASE} (cyc20261020-214924)\n"),
            ("prose only", "# Rotation bookmark\n\nNothing has been read yet.\n")):
        book = tmp_path / f"{shape.replace(' ', '-')}.md"
        book.write_text(text, encoding="utf-8")
        with pytest.raises(mod.Unmeasurable) as excinfo:
            mod.read(register, book)
        message = str(excinfo.value)
        assert "no markdown table" in message, shape
        assert str(book) in message, shape
        assert "never visited" in message, shape


def test_a_body_row_that_names_its_project_in_no_code_span_is_unmeasurable(mod, tmp_path):
    """The same silence one level in: a real table whose keys are written plainly.

    `KEY_CELL` requires the span, so such a row was skipped by the loop with no record
    that it had been seen. Rows are there and the reading says `0 row(s)`, so the two
    refusals are the same defect - a shape the reader does not know answered as an absence.
    """
    root = _root(tmp_path, "plain")
    register = _projects(tmp_path / "projects.yml", [{"name": "plain", "path": root}])
    book = tmp_path / "bookmark.md"
    book.write_text("\n".join([
        "# Bookmark", "",
        "| project | newest read | files |",
        "| --- | --- | --- |",
        f"| plain | {BASE} | 0 |",
    ]) + "\n", encoding="utf-8")
    with pytest.raises(mod.Unmeasurable) as excinfo:
        mod.read(register, book)
    message = str(excinfo.value)
    assert "cannot" in message and "never visited" in message
    assert "line 5" in message and "| plain |" in message


def test_a_bookmark_that_really_is_empty_still_reports_every_project_never_visited(mod, tmp_path):
    """The control for the arm above: the same `0 row(s)`, and the honest answer.

    A header and no body rows is a first-ever bookmark, so "never been visited" is true
    and exit 1 is right. This file and the bullet-list file above differ only in shape,
    and the guard must separate them — measured here rather than assumed, because a guard
    that swallowed this case would turn an empty bookmark into "could not measure" and the
    rotation would never start.
    """
    root = _root(tmp_path, "fresh")
    register = _projects(tmp_path / "projects.yml", [{"name": "fresh", "path": root}])
    book = tmp_path / "bookmark.md"
    book.write_text("\n".join([
        "# Bookmark", "",
        "| project | newest read | files |",
        "| --- | --- | --- |",
    ]) + "\n", encoding="utf-8")
    reading = mod.read(register, book)
    assert reading["row_count"] == 0
    assert reading["states"][0]["state"] == "no bookmark row"
    assert len(reading["faults"]) == 1
    assert "never been visited" in reading["faults"][0]


def test_a_project_with_no_row_is_a_fault_and_is_reported_as_never_visited(mod, tmp_path):
    root = _root(tmp_path, "unrowded")
    reading = _reading(mod, tmp_path, [{"name": "unrowded", "path": root}], [])
    assert reading["states"][0]["state"] == "no bookmark row"
    assert len(reading["faults"]) == 1
    assert "never been visited" in reading["faults"][0]


def test_a_row_that_names_no_project_is_a_fault(mod, tmp_path):
    root = _root(tmp_path, "kept")
    reading = _reading(mod, tmp_path, [{"name": "kept", "path": root}],
                       [("`kept`", BASE), ("`ghost`", BASE)])
    assert len(reading["faults"]) == 1
    assert "names no registered project" in reading["faults"][0]


def test_a_stamp_that_does_not_parse_is_unmeasurable_never_unchanged(mod, tmp_path):
    """The shell form's silent failure, answered out loud: the `T` form cannot be ordered."""
    root = _root(tmp_path, "iso")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod, 1))
    with pytest.raises(mod.Unmeasurable) as excinfo:
        _reading(mod, tmp_path, [{"name": "iso", "path": root}],
                 [("`iso`", "2026-10-20T16:06:24")])
    assert "cannot be ordered" in str(excinfo.value)
    assert "2026-10-20T16:06:24" in str(excinfo.value)


def test_no_register_and_no_bookmark_are_unmeasurable(mod, tmp_path):
    for subject in ("register", "bookmark"):
        register = _projects(tmp_path / "projects.yml", [{"name": "x", "path": str(tmp_path)}])
        book = _bookmark(tmp_path / "bookmark.md", [("`x`", BASE)])
        if subject == "register":
            register.unlink()
        else:
            book.unlink()
        with pytest.raises(mod.Unmeasurable) as excinfo:
            mod.read(register, book)
        assert "no project register" in str(excinfo.value) or "no bookmark" in str(excinfo.value)


def test_the_bookmark_is_not_rewritten(mod, tmp_path):
    """It measures and never repairs: the bytes are the same after a run."""
    root = _root(tmp_path, "stable")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod, 4))
    register = _projects(tmp_path / "projects.yml", [{"name": "stable", "path": root}])
    book = _bookmark(tmp_path / "bookmark.md", [("`stable`", BASE)])
    before = book.read_bytes()
    mod.read(register, book)
    assert book.read_bytes() == before


# --------------------------------------------------------------------------- the report


def test_both_subjects_are_named_before_any_verdict(mod, tmp_path):
    root = _root(tmp_path, "named")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod, 2))
    reading = _reading(mod, tmp_path, [{"name": "named", "path": root}], [("`named`", BASE)])
    lines = mod.render(reading, False).splitlines()
    assert lines[0].startswith("projects: ") and str(tmp_path) in lines[0]
    assert lines[1].startswith("bookmark: ") and "1 row(s)" in lines[1]
    assert "changed since their bookmark: 1" in lines[2]
    assert "coverage: every project has a row" in "\n".join(lines)


def test_an_empty_reading_says_zero_rather_than_printing_nothing(mod, tmp_path):
    """A report that omits the changed section reads as a tool that did not look."""
    root = _root(tmp_path, "quiet")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod))
    reading = _reading(mod, tmp_path, [{"name": "quiet", "path": root}], [("`quiet`", BASE)])
    assert "changed since their bookmark: 0" in mod.render(reading, False)


def test_the_exit_codes_are_0_1_and_2_and_2_is_never_a_pass(mod, tmp_path):
    """The contract a caller reads: clean, a coverage fault, unmeasurable."""
    root = _root(tmp_path, "codes")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod))
    register = _projects(tmp_path / "projects.yml", [{"name": "codes", "path": root}])
    clean = _bookmark(tmp_path / "clean.md", [("`codes`", BASE)])
    faulty = _bookmark(tmp_path / "faulty.md", [("`codes`", BASE), ("`ghost`", BASE)])
    assert mod.main(["--projects", str(register), "--bookmark", str(clean)]) == 0
    assert mod.main(["--projects", str(register), "--bookmark", str(faulty)]) == 1
    assert mod.main(["--projects", str(tmp_path / "absent.yml"),
                     "--bookmark", str(clean)]) == 2


def test_json_is_one_document_that_names_its_subjects(mod, tmp_path, capsys):
    root = _root(tmp_path, "documented")
    _write_memory(Path(root), ".emrg/memory/MEMORY.md", _stamp(mod, 1))
    register = _projects(tmp_path / "projects.yml", [{"name": "documented", "path": root}])
    book = _bookmark(tmp_path / "bookmark.md", [("`documented`", BASE)])
    assert mod.main(["--projects", str(register), "--bookmark", str(book), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["project_count"] == 1
    assert document["states"][0]["state"] == "changed"
    assert document["projects_file"] == str(register)
