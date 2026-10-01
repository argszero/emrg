"""The section-citation reading: `scripts/check-prompt-citations.py`.

What this file holds
--------------------
The tool answers one question - does a citation that names a file of this tree and a
section of it name a heading the file actually has - and the assertions here are the
three halves of "does it answer that question":

* **both states of every boundary**: a citation whose section exists, and one whose
  section does not, because a reading that only ever sees the failing state cannot tell
  "reports a moved number" from "reports everything";
* **the scope is the tool's, not the assertion's**: a bare section number, a cited file
  this tree does not carry, and a section of a source file are each *not* findings -
  and each is asserted, because a rule that silently grew to cover them would be a false
  positive on this tree rather than a red test. Measured 2026-10-01, extending the rule to
  a document's **own** section references (no filename on the line) produces four false
  positives: `DEVELOPMENT.md`'s `§1.5`, `system.j2`'s `§14.5` and `§Forbidden`, and
  `packaging/requirements-README.md`'s `§3` all cite *other* documents;
* **unmeasurable is never a pass**: a root that is not a directory, and a tree with no
  text file at all, both answer exit 2 with the reason.

The one assertion that is not a fixture is `test_the_tree_carries_no_stale_citation`:
it runs the tool over this checkout, which is the reading that reds when a document is
restructured and its citations are left behind (the incident this tool was written for:
`emrg/server/evolution_prompt.md`'s index rule was section 6 before #1790 and is `R9`
now, and citations kept naming the old number). **The incident is described here with the
number spelled out and never in citation form**: a counter-example written in the shape
the tool looks for is indistinguishable from the defect, and the tool's own run reported
this docstring when it was first written that way - the false positive
`check-citation-resolves.py` records for its own file, reproduced one tool over.

Loading
-------
By path (`spec_from_file_location`), the shape `test_rant_citations.py` uses: the
filename is not importable by name (the dashes).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-prompt-citations.py"


def _load():
    """The tool, loaded by path with its own module name."""
    spec = importlib.util.spec_from_file_location("check_prompt_citations", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """A tree carrying exactly `files`, as paths relative to its root.

    :param tmp_path: pytest's per-test directory.
    :param files: the relative path to the text to write there.
    :returns: the root the tool is pointed at.
    """
    root = tmp_path / "tree"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


#: A document with two headings, one of them numbered - the shapes a citation lands on.
DOC = "#### R9. The memory index\n\ntext\n\n### 2. Promotion Channels\n\ntext\n"


def test_a_citation_that_names_a_heading_resolves(mod, tmp_path, capsys) -> None:
    """The state a correct citation is in: exit 0, and it says what it read."""
    root = _tree(tmp_path, {"doc.md": DOC, "note.py": "# see doc.md §R9\n"})
    assert mod.main([str(root)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert f"tree: {root.resolve()}" in out, "the tree must be named before any verdict"


def test_a_citation_whose_section_moved_is_reported(mod, tmp_path, capsys) -> None:
    """The state the tool exists for: the number is not a heading, with its site named.

    `§6` is the incident's own token - the index rule's old number, which
    `emrg/server/evolution_prompt.md` no longer has.
    """
    root = _tree(tmp_path, {"doc.md": DOC, "note.py": "# see doc.md §6, in this checkout\n"})
    assert mod.main([str(root)]) == 1
    out = capsys.readouterr().out
    assert "note.py:1 cites doc.md, section 6" in out, out
    assert "not a heading of it" in out, out


def test_a_numbered_section_resolves_against_a_punctuated_heading(mod) -> None:
    """`§2` names `2. Promotion Channels` - the `.` belongs to the heading.

    Measured 2026-10-01, when the boundary was written the other way round: the run
    reported `tests/test_scheduler.py`'s `promote_prompt.md §2` against a file whose
    heading is `2. Promotion Channels`, i.e. a guard firing on a correct citation.
    """
    assert mod.names_a_heading("2", ("2. Promotion Channels",))
    assert mod.names_a_heading("R9", ("R9. The memory index",))
    assert mod.names_a_heading("Forbidden", ("Forbidden",))


def test_a_subsection_is_not_read_as_its_parent(mod) -> None:
    """`§2.2` must not be satisfied by a heading that only starts `2.`."""
    assert not mod.names_a_heading("2.2", ("2. Either way",))
    assert mod.names_a_heading("2.2", ("2.2 Every open issue and every rant",))


def test_a_bare_section_number_is_out_of_scope(mod, tmp_path, capsys) -> None:
    """No document named on the line, so there is no document to resolve against.

    `design §5`, `rant #12 §11` and a `.ts` header comment all take this shape, and each
    names a document that is not this tree's.
    """
    root = _tree(
        tmp_path,
        {"doc.md": DOC, "note.py": "# the design's §6 and rant #12 §11\n"},
    )
    assert mod.main([str(root)]) == 0, capsys.readouterr().out


def test_a_cited_file_this_tree_does_not_carry_is_out_of_scope(mod, tmp_path, capsys) -> None:
    """`~/.emrg/designs/*-design.md` is cited from here and is deliberately not in it.

    A citation that cannot be resolved is not a citation that is wrong, so it is neither
    reported nor allowed to change the exit code.
    """
    root = _tree(
        tmp_path,
        {"doc.md": DOC, "note.py": "# see /Users/nobody/designs/index-design.md §6\n"},
    )
    assert mod.main([str(root)]) == 0, capsys.readouterr().out


def test_a_section_of_a_source_file_is_out_of_scope(mod, tmp_path, capsys) -> None:
    """A source file has no headings, so the rule would report the shape, not a fault."""
    root = _tree(
        tmp_path,
        {"doc.md": DOC, "other.py": "x = 1\n", "note.py": "# other.py §6 means nothing here\n"},
    )
    assert mod.main([str(root)]) == 0, capsys.readouterr().out


def test_the_short_form_of_a_path_resolves(mod, tmp_path, capsys) -> None:
    """A citation may write the file's own name where the full path is deeper.

    The incident's sites write the bare name with the old number beside it, and the file
    is at `emrg/server/evolution_prompt.md` - so a bare name is looked up rather than
    missed, which is the half that would otherwise make the rule vacuous on exactly the
    citations that were stale.
    """
    root = _tree(
        tmp_path,
        {"emrg/server/doc.md": DOC, "note.py": "# see doc.md §6\n"},
    )
    assert mod.main([str(root)]) == 1
    assert "note.py:1 cites doc.md, section 6" in capsys.readouterr().out


def test_a_root_that_is_not_a_directory_cannot_be_measured(mod, tmp_path, capsys) -> None:
    """An unreadable subject is exit 2, never a green verdict over nothing."""
    missing = tmp_path / "not-here"
    assert mod.main([str(missing)]) == 2
    out = capsys.readouterr().out
    assert f"tree: {missing.resolve()}" in out, "the tree is named even when it cannot be read"
    assert "could not measure" in out


def test_a_tree_with_no_text_file_cannot_be_measured(mod, tmp_path, capsys) -> None:
    """Nothing to read is the second unmeasurable state, and it is not a pass."""
    root = tmp_path / "empty"
    root.mkdir()
    assert mod.main([str(root)]) == 2
    assert "could not measure" in capsys.readouterr().out


def test_the_tree_carries_no_stale_citation(mod, capsys) -> None:
    """The reading over this checkout: the incident, pinned so it cannot come back.

    This is the assertion a later restructure reds. It is deliberately the *tool's*
    verdict rather than a count of citations here: the tool is the one authority on what
    it read, and a number written into this file would be a second spelling of it.
    """
    assert mod.main([str(REPO_ROOT)]) == 0, capsys.readouterr().out
