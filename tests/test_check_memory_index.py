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
    assert "every row link resolves" in out


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
    assert "row links 0, unresolved: 0" in out


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


# ── measures, never repairs ───────────────────────────────────────────────────


def test_an_index_over_the_cap_is_not_rewritten(mod, tmp_path, capsys) -> None:
    """The tool reads. Compaction is the agent's, in place, and needs judgement."""
    cap = mod.MEMORY_INDEX_ROW_CAP
    path = _index(tmp_path, "over.md", [f"- [r{i}](f{i}.md)" for i in range(cap + 1)])
    before = path.read_bytes()
    assert mod.main([str(path)]) == 1
    assert path.read_bytes() == before
    capsys.readouterr()
