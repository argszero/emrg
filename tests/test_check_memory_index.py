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
    """The justification for the shape rule, as a reading rather than a claim.

    A row may be one an agent's compaction wrote: several `[id](file.md)`
    references on one line. `MemoryIndex.from_text` recognises no entry in it,
    which is exactly why a reading built on the parser's grammar would exempt the
    rows only an agent writes. Asserted both ways round - the parser sees nothing
    and the tool still counts the row.
    """
    from emrg.memory import MemoryIndex

    bound = mod.INDEX_TITLE_MAX_CHARS
    unit = "[id1](id1.md) "
    pointer = "- " + unit * (bound // len(unit) + 2)
    assert len(pointer) > bound
    assert MemoryIndex.from_text(pointer + "\n").entries == [], (
        "the claim this test makes is that the store's parser sees no entry in a "
        "pointer row - if it now does, the tool's shape rule needs its reason "
        "restated, and this failure is where that is noticed"
    )
    path = _index(tmp_path, "pointer.md", [pointer])
    assert mod.main([str(path)]) == 1
    assert "row at line 1" in capsys.readouterr().out


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
    assert "row at line" not in out, "a resolving row was reported as unresolved: " + out


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
    # The three shapes were read; none of them is a file, so resolution has no
    # subject. What is asserted is that count - the links the reading *read* - and
    # not the sentence around it: an assertion on the exact wording fails a correct
    # implementation that words it differently, which is how a guard teaches its
    # reader to ignore it (measured 2026-10-01, below).
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


# ── what counts as a link, and what a reading with no link says ───────────────
#
# Issue #1793, measured 2026-10-01 against a sibling instance's index: a row that
# documents the store's own link shape by writing it inside an inline code span was
# read as a row pointing at a file called `target`, and reported as a broken link -
# the guard firing on its own subject's documentation. The second half of that
# issue is the reading with no subject at all: an index whose rows name their files
# in prose printed `row links 0, unresolved: 0` and a summary claiming every row
# link resolves, which is a pass-shaped answer about a set that was never read.
#
# Both directions are asserted for each, because a reader that suppresses too much
# is the same defect as one that invents a link: the code span alone must be silent,
# and a genuine broken link beside it must still be reported.


def test_a_link_shape_inside_a_code_span_is_not_a_link(mod, tmp_path, capsys) -> None:
    """A code span is literal text, so the shape it quotes is not a link.

    The row here is the one the issue was filed about: it documents the store's
    link shape by writing it in an inline code span. Markdown renders that span as
    text, and this reading must agree - the alternative is a guard that fires on
    the documentation of the thing it checks.
    """
    _detail(tmp_path, "detail.md")
    path = _index(
        tmp_path,
        "quoted.md",
        ["- [a](detail.md) - rows are written as " + "`" + "](target)" + "`"],
    )
    assert mod.main([str(path)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "row links 1, unresolved: 0" in out, out
    assert "target" not in out, (
        "the code span's content was read as a row link: " + out
    )


def test_a_genuine_link_beside_a_code_span_is_still_reported(mod, tmp_path, capsys) -> None:
    """The other direction: suppressing the span must not suppress the row.

    A row may both quote the shape and carry a real link. Only the quoted one is
    not a link, so the genuine broken one is named with its line - a reading that
    dropped the whole row on seeing a backtick would pass an index whose rows point
    at files that are gone.
    """
    path = _index(
        tmp_path,
        "quoted-and-broken.md",
        ["- [a](gone.md) and " + "`" + "](target)" + "`" + " documents the shape"],
    )
    assert mod.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert "row links 1, unresolved: 1" in out, out
    assert "row at line 1 names gone.md" in out, out
    assert "names target" not in out, out


def test_a_closed_code_span_does_not_swallow_the_rest_of_the_row(mod, tmp_path, capsys) -> None:
    """A span ends at its closing backticks; what follows is Markdown again.

    The pair here is `` `x` `` twice, and the link sits between them - outside both
    spans, so it is a link. A reader that cut the line at the first backtick would
    miss it.
    """
    path = _index(tmp_path, "spans.md", ["- `a` [x](gone.md) `b`"])
    assert mod.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert "row at line 1 names gone.md" in out, out


def test_an_index_whose_rows_carry_no_link_does_not_claim_a_resolution(
    mod, tmp_path, capsys
) -> None:
    """No link read must not be reported as a resolution performed.

    An index whose rows name their detail files in prose is not a fault - the store
    does not render that shape either - but the reading used to print
    `row links 0, unresolved: 0` under a summary claiming that every row link
    resolves, which is a statement about a set with no members.

    The assertion is the **defect's own words**, and that is deliberate: the
    sentence this test forbids is the one master printed, so the test says "this
    claim may not come back" without saying what the replacement must be. Asserting a
    replacement sentence instead would fail every correct implementation that words
    it differently - the mistake the sibling test in this section records.
    """
    path = _index(
        tmp_path,
        "table.md",
        [
            "# Memory Index",
            "",
            "| id | note |",
            "| --- | --- |",
            "| a1b2 | Detail in `x.md` |",
        ],
    )
    assert mod.main([str(path)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "and every row link resolves" not in out, (
        "the summary claims a resolution the reading did not perform: " + out
    )
    # And the positive half: the reading must still distinguish a file whose rows
    # carry no link from one whose rows carry links, which is what the claim above
    # was covering up for.
    _detail(tmp_path, "one.md")
    with_links = _index(tmp_path, "links.md", ["- [a](one.md)"])
    assert mod.main([str(with_links)]) == 0, capsys.readouterr().out
    assert _link_reading(capsys.readouterr().out) != _link_reading(out), (
        "an index with no row link and one with a row link read the same"
    )


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
