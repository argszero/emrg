"""The index rule's reading: `scripts/check-memory-index.py`.

What this file holds
--------------------
The tool measures a `MEMORY.md` against the two numbers `emrg/server/evolution_prompt.md`
§6 names - an index past `MEMORY_INDEX_ROW_CAP` lines is compacted in place, and no
row is past `INDEX_TITLE_MAX_CHARS` chars - and nothing else does: the reflection
round's compaction note counts two *other* files, and the store's write-time
advisory fires only on a store write, which the `write`/`edit` path a cycle uses
bypasses. So the assertions here are the three halves of "does it answer that
question":

* **the numbers are the products', not a second spelling** - the module's two
  attributes are the objects `emrg/memory.py` and `emrg/server/daemon.py` define,
  the same two the prompt's own rule is pinned to;
* **each boundary is read in both states** - `cap` lines then `cap + 1`, a row of
  exactly `512` chars then `513`, because a reading that only ever sees the
  failing state cannot tell "fires past the bound" from "fires at it";
* **unmeasurable is never a pass** - a path that is not there, a file that is not
  UTF-8 text, and an index the tool cannot read all answer exit 2 with the reason,
  and a run that read *some* indexes still says the reading is incomplete.

The boundary states are built from the constants, never from `100`/`512` written
here: a literal in this file would be the second spelling the tool is built to
avoid, and it would pass while the rule moved underneath it.

Loading
-------
By path (`spec_from_file_location`), the shape `test_rant_citations.py` uses: the
filename is not importable by name (the dash), and the module inserts its own tree
into `sys.path` for its two imports - which is deliberate and pinned below, since
this environment's `PYTHONPATH` puts an installed copy of the package ahead of the
checkout.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-memory-index.py"


def _load():
    """The tool, loaded by path with its own module name."""
    spec = importlib.util.spec_from_file_location("check_memory_index", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _row(label: str, length: int) -> str:
    """A row of exactly `length` characters, ending in `label`.

    :param label: the text the row ends with, so a failure names the row.
    :param length: the row's total length in characters.
    :returns: the line, with no trailing newline.
    """
    prefix = "- [x](f.md) "
    assert length > len(prefix) + len(label)
    return prefix + "y" * (length - len(prefix) - len(label)) + label


def _index(tmp_path: Path, name: str, lines: list[str]) -> Path:
    """Write an index file and return its path, with its line count asserted."""
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert len(path.read_text(encoding="utf-8").splitlines()) == len(lines)
    return path


def _detail(tmp_path: Path, name: str) -> None:
    """Write one detail file beside the index, so the row that names it resolves.

    Every fixture that is about the two *numbers* needs this: since the resolution
    reading landed, a row pointing at nothing makes the run exit 1 whatever its
    length, so a fixture that names a file it never writes no longer isolates the
    number it was built to test. Written from the row's own link, so the two
    cannot drift apart.
    """
    (tmp_path / name).write_text(f"# {name}\n", encoding="utf-8")


# ── the numbers, and where they come from ─────────────────────────────────────


def test_the_two_numbers_are_the_products_own(mod) -> None:
    """The tool holds no second spelling of either number.

    Imported, so the assertion is about identity rather than agreement: a literal
    that happened to match today would pass an equality check and drift tomorrow.
    """
    from emrg.memory import INDEX_TITLE_MAX_CHARS
    from emrg.server.daemon import MEMORY_INDEX_ROW_CAP

    assert mod.THRESHOLD_ERROR == "", mod.THRESHOLD_ERROR
    assert mod.INDEX_TITLE_MAX_CHARS is INDEX_TITLE_MAX_CHARS
    assert mod.MEMORY_INDEX_ROW_CAP is MEMORY_INDEX_ROW_CAP


def test_the_row_predicate_is_the_stores_own_function(mod) -> None:
    """One predicate, two readers: the tool holds no spelling of its own.

    Identity rather than agreement, for the same reason as the numbers above — and here
    the second reader is the daemon's compaction trigger
    (`daemon._memory_index_compaction_note`), so a shape one of them learns the other
    counts too. Spelled apart once, both as `- `, a table index fell between them: the
    trigger drew no note while four of that index's rows were past the bound
    (`emrg/memory.py` carries the incident).
    """
    from emrg.memory import is_index_row

    assert mod.THRESHOLD_ERROR == "", mod.THRESHOLD_ERROR
    assert mod.is_index_row is is_index_row


def test_the_numbers_come_from_the_tree_the_report_names(mod) -> None:
    """`tree:` and the two numbers answer about one tree.

    Measured 2026-09-26: this environment's `PYTHONPATH` carries
    `<install>/source` (the installed 0.3.1) ahead of the checkout, so a plain
    `import emrg` here resolves to the *installed* copy - which has no
    `MEMORY_INDEX_ROW_CAP` at all. The tool inserts its own root first and checks
    where the numbers landed; this asserts the landing, not the mechanism.
    """
    assert Path(mod.THRESHOLD_SOURCE).is_relative_to(mod.REPO_ROOT), (
        f"the rule's numbers were read from {mod.THRESHOLD_SOURCE}, which is not "
        f"under the tree the report names ({mod.REPO_ROOT})"
    )


def test_a_tree_whose_numbers_come_from_elsewhere_is_unmeasurable(tmp_path) -> None:
    """The check after the import, in the state it exists for.

    A tree that looks like a checkout (an `Agent.md` and a `scripts/`) but does not
    carry the package: the numbers then resolve *outside* it, from whatever the
    environment offers instead, and the tool says so rather than printing another
    tree's rule as this one's. Driven as a subprocess because both the path insert
    and the import happen at load time - patching `REPO_ROOT` afterwards cannot
    reach either.
    """
    fake_root = tmp_path / "other-checkout"
    (fake_root / "scripts").mkdir(parents=True)
    (fake_root / "Agent.md").write_text("# not this repo\n", encoding="utf-8")
    tool = fake_root / "scripts" / SCRIPT.name
    tool.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    proc = subprocess.run(
        [sys.executable, str(tool), str(fake_root / "MEMORY.md")],
        cwd=str(fake_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert proc.stdout.splitlines()[0] == f"tree: {fake_root}"
    assert "not under" in proc.stderr, proc.stderr
    assert "OK:" not in proc.stdout


# ── the line cap, both states ─────────────────────────────────────────────────


def test_an_index_of_exactly_the_cap_is_within(mod, tmp_path, capsys) -> None:
    """`cap` lines pass; the rule fires *past* the cap, not at it."""
    for i in range(mod.MEMORY_INDEX_ROW_CAP):
        _detail(tmp_path, f"f{i}.md")
    path = _index(
        tmp_path,
        "at-cap.md",
        [f"- [r{i}](f{i}.md)" for i in range(mod.MEMORY_INDEX_ROW_CAP)],
    )
    assert mod.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert f"lines {mod.MEMORY_INDEX_ROW_CAP} of {mod.MEMORY_INDEX_ROW_CAP} - within" in out
    assert "OK:" in out


def test_one_line_past_the_cap_is_reported_with_its_number(mod, tmp_path, capsys) -> None:
    """One line more fails, and the report says by how much and where."""
    cap = mod.MEMORY_INDEX_ROW_CAP
    path = _index(tmp_path, "over-cap.md", [f"- [r{i}](f{i}.md)" for i in range(cap + 1)])
    assert mod.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert f"lines {cap + 1} of {cap} - over by 1" in out
    assert "OK:" not in out


# ── the row bound, both states ────────────────────────────────────────────────


def test_a_row_of_exactly_the_bound_is_within(mod, tmp_path, capsys) -> None:
    """A row of exactly `INDEX_TITLE_MAX_CHARS` chars passes."""
    bound = mod.INDEX_TITLE_MAX_CHARS
    _detail(tmp_path, "f.md")
    path = _index(tmp_path, "at-bound.md", [_row("tail", bound)])
    assert mod.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert f"longest {bound} chars" in out


def test_one_char_past_the_bound_names_the_row(mod, tmp_path, capsys) -> None:
    """One char more fails, and the row is named by its line number and length."""
    bound = mod.INDEX_TITLE_MAX_CHARS
    path = _index(tmp_path, "over-bound.md", ["- [short](f.md)", _row("tail", bound + 1)])
    assert mod.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert f"row at line 2 is {bound + 1} chars" in out


def test_a_row_is_a_list_line_by_shape(mod, tmp_path, capsys) -> None:
    """The predicate is `- `, so a long line that is not one is not a row.

    Both halves matter: a row the store's grammar cannot see still counts (a
    hand-written pointer line is what the index a cycle writes is full of), and a
    line that is not a list item is not a row whatever its length - the bound the
    rule names is a bound on rows, which is the scope line in the tool's docstring
    rather than an accident of the predicate. The prose line here is *past* the
    bound on purpose, so the assertion is about the predicate and not about a line
    too short to matter.
    """
    bound = mod.INDEX_TITLE_MAX_CHARS
    prose = "> " + "p" * (bound + 50)
    unit = "[a](a.md) "
    pointer = "- **note** (pointer): " + unit * (bound // len(unit) + 2)
    path = _index(tmp_path, "shape.md", [prose, pointer])
    assert len(prose) > bound and len(pointer) > bound
    assert mod.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert "rows 1," in out
    assert "row at line 2" in out
    assert "row at line 1" not in out


def test_a_pointer_row_counts_even_though_the_store_parses_no_entry(
    mod, tmp_path, capsys
) -> None:
    """The shape rule's reason, as a reading rather than a claim.

    This test used to carry the *opposite* half: it asserted that
    `MemoryIndex.from_text` recognised no entry in a pointer row, and its failure
    message said the tool's shape rule would need its reason restated if that ever
    changed. It changed on 2026-10-02 - the parser now asks `is_index_row` and
    `row_links`, so a pointer row *is* an entry (that is what stopped the duplicate
    row the tool's own docstring cites) - and this is the restatement.

    The reason the bound is read by shape survives, and it is now the honest one:
    the scope is the lines the embed pays for, and a `- ` line carrying no
    `](target)` outside a code span is such a line whether or not the store can
    model it. The parser cannot: an entry is a model of a target, and this row
    quotes its links rather than using them. Asserted both ways round - the parser
    sees nothing and the tool still counts the row.
    """
    from emrg.memory import MemoryIndex

    bound = mod.INDEX_TITLE_MAX_CHARS
    unit = "`[id1](id1.md)` "  # quoted, so no link the reading may follow
    pointer = "- " + unit * (bound // len(unit) + 2)
    assert len(pointer) > bound
    assert MemoryIndex.from_text(pointer + "\n").entries == [], (
        "the claim this test makes is that a row quoting its links is no entry for "
        "the store's parser - if it now is one, the tool's shape rule needs its "
        "reason restated again, and this failure is where that is noticed"
    )
    path = _index(tmp_path, "pointer.md", [pointer])
    assert mod.main([str(path)]) == 1
    assert "row at line 1" in capsys.readouterr().out


def test_a_hand_written_pointer_row_is_an_entry_now(mod, tmp_path, capsys) -> None:
    """The other half of the restatement: the row an agent writes *is* read.

    A row the store's grammar cannot produce and the predicate counts - the
    hand-written pointer line - is the one that used to leave a file invisible to
    the model and cost a duplicate row on the next write. Both readers name it, and
    the tool counts it.
    """
    from emrg.memory import MemoryIndex

    pointer = "- [Hand written](f.md)"
    (tmp_path / "f.md").write_text("# f\n", encoding="utf-8")
    assert [e.filename for e in MemoryIndex.from_text(pointer + "\n").entries] == ["f.md"]
    path = _index(tmp_path, "pointer.md", [pointer])
    assert mod.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "rows 1," in out


# ── unmeasurable is never a pass ──────────────────────────────────────────────


def test_a_missing_index_is_unmeasurable(mod, tmp_path, capsys) -> None:
    """A path that is not there answers 2 with the reason, never a clean verdict."""
    missing = tmp_path / "nope" / "MEMORY.md"
    assert mod.main([str(missing)]) == 2
    captured = capsys.readouterr()
    assert "OK:" not in captured.out
    assert "could not measure" in captured.err
    assert "FileNotFoundError" in captured.err


def test_a_file_that_is_not_utf8_is_unmeasurable(mod, tmp_path, capsys) -> None:
    """A torn or non-UTF-8 index is reported by name, not read through."""
    path = tmp_path / "MEMORY.md"
    path.write_bytes(b"- [x](f.md)\n\xff\xfe not utf-8\n")
    assert mod.main([str(path)]) == 2
    captured = capsys.readouterr()
    assert "OK:" not in captured.out
    assert "UnicodeDecodeError" in captured.err


def test_one_unreadable_subject_makes_the_whole_reading_incomplete(
    mod, tmp_path, capsys
) -> None:
    """A partial reading is not a pass: the readable half is printed, and the
    verdict is exit 2 - "every index read is within" would be a claim about a set
    this run could not read."""
    good = _index(tmp_path, "good.md", ["- [a](a.md)"])
    assert mod.main([str(good), str(tmp_path / "gone.md")]) == 2
    captured = capsys.readouterr()
    assert str(good) in captured.out
    assert "incomplete" in captured.err
    assert "OK:" not in captured.out


# ── the default subject ───────────────────────────────────────────────────────


def test_the_default_subject_is_both_levels_the_tree_carries(mod) -> None:
    """The project's index and one per session, and nothing else."""
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / ".emrg" / "memory").mkdir(parents=True)
        (root / ".emrg" / "memory" / "MEMORY.md").write_text("- [a](a.md)\n", encoding="utf-8")
        (root / ".emrg" / "memory" / "other.md").write_text("- [b](b.md)\n", encoding="utf-8")
        for name in ("s1", "s2"):
            (root / ".emrg" / "sessions" / name / "memory").mkdir(parents=True)
            (root / ".emrg" / "sessions" / name / "memory" / "MEMORY.md").write_text(
                "- [c](c.md)\n", encoding="utf-8"
            )
        found = mod.default_indexes(root)
        assert found == [
            root / ".emrg" / "memory" / "MEMORY.md",
            root / ".emrg" / "sessions" / "s1" / "memory" / "MEMORY.md",
            root / ".emrg" / "sessions" / "s2" / "memory" / "MEMORY.md",
        ]


def test_a_tree_with_no_index_has_no_default_subject(mod) -> None:
    """An empty tree yields nothing - the caller is then told to name a path."""
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        assert mod.default_indexes(Path(raw)) == []


def test_an_empty_tree_is_unmeasurable_not_clean(mod, monkeypatch, capsys) -> None:
    """The no-subject branch: exit 2, and the message says how to reach an index."""
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        monkeypatch.setattr(mod, "REPO_ROOT", Path(raw))
        assert mod.main([]) == 2
    captured = capsys.readouterr()
    assert "OK:" not in captured.out
    assert "no memory index under" in captured.err


# ── the tree line, and the order a merged reader gets ─────────────────────────


def test_the_first_line_names_the_tree_before_any_verdict(mod, tmp_path, capsys) -> None:
    """The convention: which tree answered, before anything else."""
    path = _index(tmp_path, "MEMORY.md", ["- [a](a.md)"])
    mod.main([str(path)])
    assert capsys.readouterr().out.splitlines()[0] == f"tree: {mod.REPO_ROOT}"


def test_the_tree_line_is_first_under_a_merged_pipe(tmp_path) -> None:
    """The reading mode the promise is for: `2>&1 | cat`, as a cycle reads it.

    The unmeasurable path writes to stderr, which is unbuffered while a piped
    stdout is not - so without the tool's line-buffering call the refusal would
    overtake the `tree:` line for exactly the reader the order exists for.
    `PYTHONUNBUFFERED` is removed from the child's environment on purpose: it is
    the variable that hides this defect.
    """
    missing = tmp_path / "gone" / "MEMORY.md"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(missing)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 2
    first = proc.stdout.splitlines()[0]
    assert first.startswith("tree: "), (
        f"the refusal overtook the identity line under a pipe: {proc.stdout[:200]!r}"
    )
    assert "could not measure" in proc.stdout


# ── the third reading: a row's link, resolved ─────────────────────────────────


def test_a_row_that_names_a_file_beside_the_index_resolves(mod, tmp_path, capsys) -> None:
    """The resolving half, and the assertion is the *report's* count.

    A row whose detail file is beside the index is the ordinary state, so the
    reading has to say so positively: an instrument that only ever fires cannot be
    told from one that fires on everything.
    """
    _detail(tmp_path, "detail.md")
    path = _index(tmp_path, "links.md", ["- [a](detail.md)"])
    assert mod.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "row links 1, unresolved: 0" in out
    assert "1 row link(s) read, none resolving to a missing file" in out


def test_a_row_that_names_no_file_is_reported_with_its_line_and_target(
    mod, tmp_path, capsys
) -> None:
    """The failing half: the row's line number *and* the target it named."""
    path = _index(
        tmp_path, "links.md", ["- [a](there.md)", "- [b](gone.md)"]
    )
    _detail(tmp_path, "there.md")
    assert mod.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert "row links 2, unresolved: 1" in out
    assert "row at line 2 names gone.md" in out, out
    assert "there.md" not in out.split("row at line")[1], (
        "the row that resolves must not be reported as unresolved: " + out
    )


def test_resolution_is_relative_to_the_index_not_the_callers_cwd(
    mod, tmp_path, capsys, monkeypatch
) -> None:
    """These rows name siblings, so the base is the index's own directory.

    Run from a directory that holds nothing, so a reading that joined the target
    onto the *cwd* would report every row unresolved - which is the defect the
    position of the base decides, not a detail of the implementation.
    """
    _detail(tmp_path, "sibling.md")
    path = _index(tmp_path, "links.md", ["- [a](sibling.md)"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert mod.main([str(path)]) == 0
    assert "unresolved: 0" in capsys.readouterr().out


def test_a_cross_directory_target_resolves_where_it_points(mod, tmp_path, capsys) -> None:
    """A `../`-relative target is resolved as written, not treated as missing.

    The store writes sibling links, but an agent hand-editing an index may point
    at a detail file one level up; the check must read that spelling rather than
    guess a second one.
    """
    (tmp_path / "mem").mkdir()
    _detail(tmp_path, "up.md")
    path = _index(tmp_path / "mem", "links.md", ["- [a](../up.md)"])
    assert mod.main([str(path)]) == 0, capsys.readouterr().out


def test_a_url_and_an_anchor_are_not_rows_that_point_nowhere(
    mod, tmp_path, capsys
) -> None:
    """The exemption list, in the state it exists for - and it is the whole list.

    A row citing a GitHub URL or an in-file `#heading` is not naming a detail file,
    so reporting it would make the reading fire on correct indexes and train its
    reader to ignore it. Measured on this host's three live indexes 2026-09-26:
    245 links, 0 unresolved, so the exemption is not doing the work of the rule.
    """
    path = _index(
        tmp_path,
        "links.md",
        ["- [a](https://example.com/x.md) and [b](#heading) and [c](mailto:x@y.z)"],
    )
    assert mod.main([str(path)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    # The three shapes were read; none of them is a file, so the resolution has no
    # subject. What is asserted is the count of the links the reading *read*, and not
    # the sentence around it: an assertion on the exact wording fails a correct
    # implementation that words it differently, which is how a guard teaches its
    # reader to ignore it (measured 2026-10-01).
    assert "row links 3, unresolved: 0" in out, out


def _link_reading(report: str) -> str:
    """A report's lines about row links, with what is about the *file* masked out.

    Two fixtures differ in size and name, so comparing their reports means removing
    the lines that answer about the file rather than about the reading: the `tree:`
    line, the path line, and the three number lines (`lines`/`chars`/`rows`), whose
    values differ because the fixtures differ. What survives is the part that answers
    about row links - the row-link line, any coverage sentence, the unresolved
    findings and the run's summary - which is the part under test.

    Masking is what makes the comparison a test of the *reading* and not of its
    wording: an assertion on a sentence passes only for the implementation that wrote
    it, and the reading under test is "does this distinguish the two states".
    """
    keep = []
    for line in report.splitlines():
        stripped = line.strip()
        if line.startswith("tree: ") or stripped.endswith(".md"):
            continue
        if stripped.startswith(("lines ", "chars ", "rows ")):
            continue
        keep.append(line)
    return "\n".join(keep)


#: The count line's shape, which is the one format this tool had **before** the change
#: under test (`row links N, unresolved: M`, and the exemption suffix the newer reading
#: adds to it). Matching it is not matching the reading's wording - it is how a line that
#: reports *numbers* is told apart from a line that makes a *claim*, and the distinction
#: is the whole point of the helper below.
_COUNT_LINE = re.compile(r"^\s*row links \d+(,|\s*$)")
_SUMMARY_LINE = re.compile(r"^(OK:|could not measure|\d+ of \d+ index)")


def _coverage_lines(report: str) -> set[str]:
    """The report's non-count lines: what it *says* beyond the numbers it read.

    `_link_reading` above compares two states, and that comparison is satisfied by the
    counts alone - so it passes for a reading whose coverage sentence is **always** the
    same, or always false. Measured 2026-10-01: making the no-link sentence print
    unconditionally left every test in this file green as long as the sentence was
    reworded to drop the literal ``](target)`` the one incidental leg matched on. A leg
    that fires on the words cannot see a claim that is uniformly wrong.

    So this helper isolates the other half: the lines that answer a question other than
    "how many links", with the file-specific lines, the count line and the run's summary
    masked out (the summary restates the counts, so it tracks them too). What is left is
    the reading's *prose*, keyed by nothing in particular - which is what lets a test
    compare the prose of two states without naming either one's words.

    :param report: one run's stdout.
    :returns: the set of lines that are neither numbers nor the summary.
    """
    keep = set()
    for line in _link_reading(report).splitlines():
        if _COUNT_LINE.match(line) or _SUMMARY_LINE.match(line):
            continue
        keep.add(line.strip())
    return keep




def test_an_exempt_only_row_is_not_read_as_a_row_with_no_link(mod, tmp_path, capsys) -> None:
    """Three exempt links and no link at all are different states, and read apart.

    `NON_FILE_TARGET` is an exemption, and it removes links the row *does* carry - so
    a reading that derives "this index carries no link to resolve" from the
    exempt-filtered list reports a row carrying a URL, an anchor and a `mailto:` as
    carrying nothing. Both files are built here and their link readings are compared
    with the file-specific lines masked (`_link_reading`), so what is asserted is that
    the reading *distinguishes the two states* - not that it says it in my words.

    Measured 2026-10-01 against a sibling implementation (PR #1794, which fixed the
    code-span half of issue #1793 and checked coverage against the exempt-filtered
    list): the exempt-only and the link-free index produce byte-identical link
    readings, and the first version of this test caught that only by accident - it
    asserted my exact sentence, so it went red on a punctuation difference (their
    sentence carries no backticks) while the defect it was written for went
    unread. A guard whose red is cosmetic is a guard that will be ignored; the
    comparison below is the assertion that has to hold.

    The counts are asserted too, in the format the tool already had before this
    change (`row links N, unresolved: M`): the exempt-only index read three links and
    the link-free one none, and a reading that reports 0 for both has stopped
    answering the question it was asked.
    """
    exempt = _index(
        tmp_path,
        "exempt-only.md",
        ["- [a](https://example.com/x.md) and [b](#heading) and [c](mailto:x@y.z)"],
    )
    assert mod.main([str(exempt)]) == 0, capsys.readouterr().out
    exempt_report = capsys.readouterr().out

    nothing = _index(tmp_path, "link-free.md", ["- a table row names its file in prose"])
    assert mod.main([str(nothing)]) == 0, capsys.readouterr().out
    nothing_report = capsys.readouterr().out

    assert "row links 3" in exempt_report, exempt_report
    assert "row links 0" in nothing_report, nothing_report
    assert _link_reading(exempt_report) != _link_reading(nothing_report), (
        "the two states read the same, so the coverage sentence is answering about "
        "the links the exemption removed rather than the links that were read - the "
        "exempt-only index carries three:\n"
        "--- exempt-only ---\n" + _link_reading(exempt_report)
        + "\n--- link-free ---\n" + _link_reading(nothing_report)
    )




def test_the_coverage_prose_follows_the_state_not_the_implementation(mod, tmp_path, capsys) -> None:
    """The reading's *prose* must change between the two states, not just its numbers.

    This is the leg that was missing here, and it was found by mutating this reader
    rather than by reading it. Measured 2026-10-01, on this branch before the test
    existed: taking the no-link sentence and printing it **unconditionally** - so it
    asserts "no row carries a link" about an index whose rows carry three - left the
    whole file green, because the one leg that happened to fire matched the literal
    ``](target)`` inside the sentence, and rewording the sentence to drop that literal
    made even that leg pass. A claim that is uniformly false was invisible.

    So the assertion is not about any sentence: it requires the *non-count* lines of the
    two states to differ (see `_coverage_lines`). The exempt-only index is the state the
    claim is false in - its rows carry three links, all of them exempt - and the
    link-free index is the state it is true in. An implementation that says the same
    thing about both fails here whatever it says; one that distinguishes them passes
    whatever words it chooses.

    The pair is deliberately the one from `test_an_exempt_only_row_is_not_read_as_a_row_with_no_link`
    above, which asserts the *counts* differ: together the two tests pin both halves of
    "this reading answers about the links that were read", and neither can be satisfied
    by the other's subject.
    """
    exempt = _index(
        tmp_path,
        "exempt-only.md",
        ["- [a](https://example.com/x.md) and [b](#heading) and [c](mailto:x@y.z)"],
    )
    assert mod.main([str(exempt)]) == 0, capsys.readouterr().out
    exempt_report = capsys.readouterr().out

    nothing = _index(tmp_path, "link-free.md", ["- a table row names its file in prose"])
    assert mod.main([str(nothing)]) == 0, capsys.readouterr().out
    nothing_report = capsys.readouterr().out

    assert _coverage_lines(exempt_report) != _coverage_lines(nothing_report), (
        "the two states carry the same prose, so whatever the reading says about "
        "coverage it says about both - and one of the two carries three links:\n"
        "--- exempt-only ---\n" + exempt_report
        + "\n--- link-free ---\n" + nothing_report
    )




def test_several_links_in_one_row_are_all_resolved(mod, tmp_path, capsys) -> None:
    """A hand-written pointer row carries several links; each is a thing to follow.

    This host's evolution index is exactly that shape: 67 rows by the `- `
    predicate, 221 links among them, while the store's parser recognises 21 rows.
    A reading that checked only the first link would pass an index whose other 200
    pointers are dead.
    """
    _detail(tmp_path, "one.md")
    _detail(tmp_path, "three.md")
    path = _index(
        tmp_path, "links.md", ["- [a](one.md) · [b](two.md) · [c](three.md)"]
    )
    assert mod.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert "row links 3, unresolved: 1" in out
    assert "row at line 1 names two.md" in out, out


def test_a_row_whose_link_resolves_is_not_repaired(mod, tmp_path, capsys) -> None:
    """The tool reads. Rewriting a stale row is the agent's judgement, in place."""
    path = _index(tmp_path, "links.md", ["- [a](gone.md)"])
    before = path.read_bytes()
    assert mod.main([str(path)]) == 1
    assert path.read_bytes() == before
    capsys.readouterr()


def test_a_link_quoted_inside_a_code_span_is_not_a_link(mod, tmp_path, capsys) -> None:
    """A row that *documents* the link shape does not carry one.

    Measured 2026-10-01 on this host: the evolution index's row at line 8 writes the
    shape inside an inline code span while describing it, and the reading reported
    `target` as a row link resolving to no file beside the index - a guard firing on
    its own subject's documentation, which is how a check trains its reader to ignore
    it. In Markdown an inline code span is literal text, not a link, so the span is
    removed before the links are read.
    """
    path = _index(
        tmp_path, "links.md", ["- the shape is `](target)` and that is all"]
    )
    assert mod.main([str(path)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "row links 0" in out, out
    assert "names target" not in out, out


def test_a_real_link_beside_a_code_span_is_still_read(mod, tmp_path, capsys) -> None:
    """The control: removing the code span must not hide a genuine link.

    A row can quote the shape and use it at the same time, and the reading has to
    keep the second one - the direction that tells "the span is skipped" apart from
    "the whole line is skipped".
    """
    path = _index(
        tmp_path,
        "links.md",
        ["- the shape is `](target)` and a real one is [a](gone.md)"],
    )
    assert mod.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert "row links 1, unresolved: 1" in out
    assert "row at line 1 names gone.md" in out, out


def test_an_index_whose_rows_carry_no_link_says_so(mod, tmp_path, capsys) -> None:
    """A zero must not read as a pass.

    A table row names its detail file in prose, not with the store's `](target)`
    shape, so the resolution reading has no subject in a table index. Measured
    2026-10-01 on this host: `.emrg/memory/MEMORY.md` printed `row links 0,
    unresolved: 0` while its summary claimed every row link resolved - a clean verdict
    about links the reading had not looked at. The report now states the coverage, and
    the summary names how many links were read.
    """
    path = _index(
        tmp_path,
        "table.md",
        [
            "# Memory Index",
            "",
            "| id | note |",
            "| --- | --- |",
            "| a1 | see detail.md |",
        ],
    )
    assert mod.main([str(path)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "row links 0" in out, out
    # The defect's own words, and deliberately: "no row carries a ](target) link ... a
    # table row names its detail file in prose" was printed here while the fixture is a
    # table row, so it was the one sentence the reading owed this state - and it was
    # also printed for a `- ` row carrying three exempt links (the `_coverage_lines`
    # leg above). Asserting a *replacement* sentence instead would fail every correct
    # implementation that words it differently.
    assert "a table row names its detail file in prose" not in out, out
    assert "0 row link(s) read" in out, out


def test_the_summary_answers_about_the_links_that_were_read(mod, tmp_path, capsys) -> None:
    """A clean run's last line must be sensitive to how much it checked.

    The claim that has to die is a summary that reads the same whether the run read
    two row links or none: "every row link resolves" is a sentence about a set with
    no members when nothing was read. So the assertion compares the **last line** of
    two clean runs - one over an index whose rows carry links, one over an index
    whose rows carry none - and requires them to differ. Wording is not pinned, only
    sensitivity: a summary that ignores the count fails, whatever it says.
    """
    _detail(tmp_path, "one.md")
    _detail(tmp_path, "two.md")
    with_links = _index(tmp_path, "links.md", ["- [a](one.md) - [b](two.md)"])
    assert mod.main([str(with_links)]) == 0, capsys.readouterr().out
    read_two = capsys.readouterr().out.splitlines()[-1]

    no_links = _index(tmp_path, "no-links.md", ["- a table row names its file in prose"])
    assert mod.main([str(no_links)]) == 0, capsys.readouterr().out
    read_none = capsys.readouterr().out.splitlines()[-1]

    assert read_two != read_none, (
        "the summary is the same whether the run read two row links or none, so it "
        "answers about neither:\n  " + read_two
    )




def test_help_does_not_claim_what_the_report_disowns(mod, capsys) -> None:
    """Exit 0's claim about links has three homes, and all three must agree.

    The homes are the `--help` description, the `--help` epilog, and the module
    docstring's *Exit codes* section — all three hand-written, nothing tying them
    together. Measured 2026-10-01, on this branch before this assertion and on PR
    #1794 before it: the report was changed to stop claiming "every row link
    resolves" — the sentence that is false for an index whose rows carry no link, or
    only exempt ones — while `--help` went on claiming it. The tool then explained
    itself with the sentence it had just removed from its own output, which is the
    "a claim wider than the reading" defect one step out from the reading.

    The first assertion is the behavioural one, and the order is deliberate: on a
    tool that still claims it, the red is this line and says so. The others hold the
    one spelling (`LINK_CLAIM`) in place, including the docstring home, which no
    constant can reach — hence the comparison against the rendered text rather than
    a sentence of this file's own.
    """
    with pytest.raises(SystemExit) as exit_code:
        mod.main(["--help"])
    assert exit_code.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())

    assert "every row link resolves" not in help_text, (
        "`--help` claims what the report disowns:\n  " + help_text
    )
    assert "row link" in help_text, (
        "the sensitivity leg: a help text that dropped the topic entirely would pass "
        "the line above for the wrong reason:\n  " + help_text
    )
    assert help_text.count(mod.LINK_CLAIM) == 2, (
        "both `--help` homes state the claim, through the one spelling:\n  " + help_text
    )
    assert mod.LINK_CLAIM in " ".join((mod.__doc__ or "").split()), (
        "the docstring's *Exit codes* section is the claim's third home, and the one "
        "no constant reaches"
    )


# ── a fence's content is not a row ────────────────────────────────────────────
#
# The row predicate judges a line by its shape, and shape cannot answer this question on
# its own: inside a fenced code block a `- ` line is literal text, which is what an index
# is made of when it documents the format it is written in. Measured 2026-10-01; the same
# defect the inline code span had (issue #1793), one scale up.
#
# Both directions matter here for the usual reason: the fence must hide what is inside it
# **and** hide nothing else - an ordinary row beside the fence is still read, and a file
# with no fence reports exactly what it reported before (no new line, no new number).




# ── a fence's content is not a row ────────────────────────────────────────────
#
# The row predicate judges a line by its shape, and shape cannot answer this question on
# its own: inside a fenced code block a `- ` line is literal text, which is what an index
# is made of when it documents the format it is written in. Measured 2026-10-01; the same
# defect the inline code span had (issue #1793), one scale up.
#
# Both directions matter here for the usual reason: the fence must hide what is inside it
# **and** hide nothing else - an ordinary row beside the fence is still read, and a file
# with no fence reports exactly what it reported before (no new line, no new number).


def test_a_fenced_example_is_not_read_as_a_row_or_a_link(mod, tmp_path, capsys) -> None:
    """The fenced half is not a row, and the row outside it still is.

    The fixture is the shape that made this necessary: an index that documents its own
    format inside a fence, plus one real row with a link that resolves to nothing. A
    reading that counted the example would report two rows and two unresolved links; the
    right answer is the real row alone, with its link followed - so the assertion is on
    the row the report names *and* on the row it does not.
    """
    _detail(tmp_path, "real.md")
    path = _index(
        tmp_path,
        "fenced-example.md",
        [
            "# Memory Index",
            "",
            "A row is written like this:",
            "",
            "```markdown",
            "- [title](not-a-file.md) - one row per memory",
            "```",
            "",
            "- [real](real.md) - the index's one row",
        ],
    )
    assert mod.main([str(path)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "rows 1," in out, out
    assert "row links 1, unresolved: 0" in out, out
    assert "1 line(s) inside a fenced code block" in out, (
        "the lines the reading declined to read as rows must be accounted for: " + out
    )
    assert "not-a-file.md" not in out, (
        "the fenced example's link was followed as if the row carried it: " + out
    )


def test_the_hidden_line_count_does_not_include_the_fence_markers(
    mod, tmp_path, capsys
) -> None:
    """One line inside a fence is one hidden line, not one per marker plus content.

    Written after the first version of this reading got it wrong: it took the count as
    `lines - len(unfenced)`, which also subtracts the opening and closing markers, so a
    one-line example was reported as three hidden lines - a number about the file that no
    reader could reconcile with the file. The fixture is small enough to count by eye.
    """
    path = _index(
        tmp_path,
        "one-line-fence.md",
        ["# Memory Index", "", "```", "- [a](a.md)", "```", ""],
    )
    assert mod.main([str(path)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "1 line(s) inside a fenced code block" in out, out
    assert "rows 0," in out, out


def test_a_file_with_no_fence_reports_no_fenced_lines(mod, tmp_path, capsys) -> None:
    """The direction that keeps the line worth reading: silence when nothing was skipped.

    A coverage sentence printed for every file would be noise, and a reader who sees it
    always stops believing it - the same reason the coverage sentence for row links only
    appears when there was nothing to resolve.
    """
    _detail(tmp_path, "a.md")
    path = _index(tmp_path, "plain.md", ["- [a](a.md) - one row"])
    assert mod.main([str(path)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "fenced code block" not in out, out


def test_an_unclosed_fence_is_read_as_ordinary_text(mod, tmp_path, capsys) -> None:
    """A fence the file never closes hides nothing - the conservative direction.

    CommonMark runs an unclosed fence to the end of the document, and a reading that did
    the same would stop counting rows at the opener: for an index whose author simply
    forgot the closing line, it would print `rows 1 - within` about a file with forty
    rows, which is the pass-shaped answer this whole tool exists to prevent. So an
    unclosed fence is ordinary text: the rows below it are read, and the report says
    nothing about hidden lines because it hid none.
    """
    path = _index(
        tmp_path,
        "unclosed.md",
        ["# Memory Index", "", "```markdown", "- [x](x.md) - a row below an unclosed fence"],
    )
    assert mod.main([str(path)]) == 1, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "rows 1," in out, out
    assert "row at line 4 names x.md" in out, out
    assert "fenced code block" not in out, (
        "the reading hid nothing here, so it must not claim it did: " + out
    )


def test_only_a_matching_fence_closes(mod, tmp_path, capsys) -> None:
    """The closer is the same character and at least as long; anything else is content.

    Two fixtures whose inner lines are only hidden if the pairing is right: a `~~~` block
    opened with three backticks and a four-backtick block containing a three-backtick
    line. A reader that closed on *any* fence-marker line would call the first file's
    body one line short, and a reader that closed on any *length* would end the second
    block early - both are under-counts of what it read, which is how a reading starts
    reporting a number that is not about the file.
    """
    tilde_inside = _index(
        tmp_path,
        "tilde-inside.md",
        [
            "# Memory Index",
            "",
            "```markdown",
            "~~~",
            "- [a](a.md)",
            "~~~",
            "```",
        ],
    )
    assert mod.main([str(tilde_inside)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "3 line(s) inside a fenced code block" in out, out
    assert "rows 0," in out, out

    shorter_inside = _index(
        tmp_path,
        "shorter-inside.md",
        [
            "# Memory Index",
            "",
            "````markdown",
            "```",
            "- [a](a.md)",
            "```",
            "````",
            "",
            "- [b](b.md)",
        ],
    )
    _detail(tmp_path, "b.md")
    assert mod.main([str(shorter_inside)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "3 line(s) inside a fenced code block" in out, out
    assert "rows 1," in out, out


# ── the embed budget, both directions ─────────────────────────────────────────


def _table(rows: int, row_chars: int) -> list[str]:
    """A Markdown-table index's lines.

    The shape that made this reading necessary: no line begins with `- `, so before the
    table shape was added the two row readings (count and bound) bound nothing here while
    the file's size is exactly what the embed cap acts on. Both are measured now, so a
    fixture here must also keep its rows inside the per-row bound, or the file exits 1
    for that reason and the reading under test is not the one asserted.
    """
    return ["# Memory Index", "", "| id | note |", "| --- | --- |"] + [
        "| n | " + "x" * row_chars + " |" for _ in range(rows)
    ]


def test_the_budget_is_the_products_own(mod) -> None:
    """The third number is imported too, so the tool cannot hold a stale copy."""
    from emrg.memory import INDEX_SIZE_WARN

    assert mod.INDEX_SIZE_WARN is INDEX_SIZE_WARN


def test_a_table_index_over_the_budget_is_reported(mod, tmp_path, capsys) -> None:
    """A table index is measured on every number, and its rows are read, not skipped.

    The budget is the reading that answers for the file's *size*, whatever shape its rows
    are in - measured 2026-09-28 on this host, `aitokenpool`'s 71-line index reads
    `lines 71 of 100 - within` while the file is past the embed budget (issue #1676 holds
    that reading). The row bound is the other half, and this file is where it used to be
    skipped: with `- ` alone for a predicate, a table read `rows 0, longest 0 chars,
    over 512: 0` and `OK` while 12 of its 25 rows were past the bound (measured
    2026-09-30, longest 6,208 chars). Both are asserted here.
    """
    rows = 71
    lines = _table(rows=rows, row_chars=745)
    path = _index(tmp_path, "table.md", lines)
    text = path.read_text(encoding="utf-8")
    assert len(lines) <= mod.MEMORY_INDEX_ROW_CAP, "under the line cap, on purpose"
    assert len(text) > mod.INDEX_SIZE_WARN, "and over the budget, which is the point"

    assert mod.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert (
        f"chars {len(text)} of {mod.INDEX_SIZE_WARN} - over by "
        f"{len(text) - mod.INDEX_SIZE_WARN}" in out
    )
    assert (
        f"rows {rows + 1}, longest {len(lines[-1])} chars, "
        f"over {mod.INDEX_TITLE_MAX_CHARS}: {rows}" in out
    ), "a table's rows are rows: the header counts, the delimiter row does not"
    assert "OK:" not in out


def test_a_table_index_within_the_budget_is_within(mod, tmp_path, capsys) -> None:
    """The other direction, so the reading above is not a check that always fires."""
    path = _index(tmp_path, "small-table.md", _table(rows=5, row_chars=40))
    text = path.read_text(encoding="utf-8")

    assert mod.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert f"chars {len(text)} of {mod.INDEX_SIZE_WARN} - within" in out
    assert "OK:" in out


def test_a_row_indented_up_to_three_spaces_is_still_a_row(mod, tmp_path, capsys) -> None:
    """An indented row is a row: markdown's block-level limit is 3 spaces, for both shapes.

    Both shapes may carry it (`  | a | b |` is still a table row, `  - row` is still a list
    item). Measured 2026-10-01 on `890bf02`, with the predicate matching only at column 0: a
    table whose rows were the only indented thing read `rows 1, longest 31 chars, over 512: 0`
    and `OK`, four bytes longer than the same file at column 0 and with its 604-char row still
    in it — the "clean verdict about rows the tool never looked at" this reading exists to
    remove, one indentation level out.
    """
    table = ["# Memory Index", "", "  | id | note |", "  | --- | --- |"] + [
        "  | n | " + "x" * 600 + " |" for _ in range(3)
    ]
    listing = ["# Memory Index", ""] + ["  - " + "y" * 600 for _ in range(3)]

    # The header counts and the delimiter row does not, so the table's three entries read as
    # four rows — but only the three *entries* are past the bound: the header is short. A list
    # has no header, so its three rows all are. The guard must exit 1 and say both numbers.
    for name, lines, rows, over in (
        ("indent-table.md", table, 4, 3),
        ("indent-list.md", listing, 3, 3),
    ):
        path = _index(tmp_path, name, lines)
        assert mod.main([str(path)]) == 1, f"{name}: an indented row went unseen"
        out = capsys.readouterr().out
        assert f"rows {rows}, longest {len(lines[-1])} chars" in out, f"{name}: {out}"
        assert f"over {mod.INDEX_TITLE_MAX_CHARS}: {over}" in out, f"{name}: {out}"
        assert "OK:" not in out, f"{name}: an over-bound index was called OK"


def test_a_four_space_indented_block_is_not_a_row(mod, tmp_path, capsys) -> None:
    """Where the limit stops, so "strip the indent" is not the reading that shipped.

    Four spaces is an indented code block: a line the embed pays for, but not a row, and not
    a line the per-row bound is about. Asserted on both sides of the one byte that decides
    it, or the fix above would be "strip whatever the indent is".
    """
    body = "- " + "y" * 600
    for name, indent, rows in (("three.md", "   ", 1), ("four.md", "    ", 0)):
        path = _index(tmp_path, name, ["# Memory Index", "", indent + body])
        assert mod.main([str(path)]) == (1 if rows else 0), f"{name}: {indent!r} rows {rows}"
        out = capsys.readouterr().out
        assert f"rows {rows}, longest {len(body) + len(indent) if rows else 0} chars" in out, (
            f"{name}: {out}"
        )
        assert (f"over {mod.INDEX_TITLE_MAX_CHARS}: 1" in out) is bool(rows), f"{name}: {out}"



def test_the_budget_is_counted_in_characters_not_bytes(mod, tmp_path, capsys) -> None:
    """The unit is the cap's, and its divergence from the advisory is deliberate.

    `_cap_memory_index` compares `INDEX_SIZE_WARN` against the text's **characters**,
    while the store's advisory compares the same constant against the file's **bytes**
    (`tests/test_memory_index_thresholds.py` measures that pair). A CJK index is where
    they disagree, and this reading must take the cap's side: measuring bytes would
    report a file as over when the prompt carries it whole.

    The rows are held inside the per-row bound on purpose (the first version of this
    fixture used 900-char rows, which became a *second*, unrelated reason to exit 1 the
    moment table rows were read): 90 rows of 500 CJK characters is 45,7xx chars /
    135,7xx bytes, 94 lines, longest row 508.
    """
    lines = ["# 索引", "", "| 编号 | 说明 |", "| --- | --- |"] + [
        "| n | " + "汉" * 500 + " |" for _ in range(90)
    ]
    path = _index(tmp_path, "cjk-table.md", lines)
    text = path.read_text(encoding="utf-8")
    assert len(text) <= mod.INDEX_SIZE_WARN < len(path.read_bytes()), (
        "the fixture must be the case the two units disagree on: within in characters "
        "(the cap's unit), over in bytes (the advisory's)"
    )

    assert mod.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert f"chars {len(text)} of {mod.INDEX_SIZE_WARN} - within" in out, (
        "the reading must print the character count — the unit the cap acts on"
    )
    assert "OK:" in out


# ── measures, never repairs ───────────────────────────────────────────────────


def test_an_index_over_the_cap_is_not_rewritten(mod, tmp_path, capsys) -> None:
    """The tool reads. Compaction is the agent's, in place, and needs judgement."""
    cap = mod.MEMORY_INDEX_ROW_CAP
    path = _index(tmp_path, "over.md", [f"- [r{i}](f{i}.md)" for i in range(cap + 1)])
    before = path.read_bytes()
    assert mod.main([str(path)]) == 1
    assert path.read_bytes() == before
    capsys.readouterr()
