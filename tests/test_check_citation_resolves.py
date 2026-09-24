"""Tests for scripts/check-citation-resolves.py - a test citation must collect.

Background (cycle cyc20260925-052652, 2026-09-25)
-------------------------------------------------
A docstring citation `tests/test_foo.py::some_method` is this repo's way of
naming a runnable node id, and a reader copies it into a shell. When the method
lives inside a class, the citation collects nothing - `pytest "<that node id>"`
answers `ERROR: not found` - so the reader re-derives the target by grep, which
is the cost the citation existed to remove. It is a *silent* defect: the path
resolves when opened, the name really exists, and only pytest disagrees.

Measured on `master` @ `9c665ff1`: of the citations naming a file under `tests/`,
**32** name a module-level test and collect exactly as written, and **0** name a
class method without its class. So "write it so that it resolves" is the tree's
actual convention, and the tool's job is to hold it.

Two things this file pins, in the shape the session's other guards use:

* the rule fires on the real defect and stays silent on every lookalike (a
  synthetic path, a bare name in prose, a `*` prefix, a class, its own
  counter-example) - a guard whose false positives outnumber its true ones gets
  deleted, so the lookalikes are the load-bearing half;
* the real tree is clean, which is the assertion that fails the day someone
  writes the un-qualified form again.

The unit tests run on a tree the test builds, never on the repository: a test
that cited a *real* module in the defect's shape would make its own file a site.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-citation-resolves.py"

#: A cited module with one module-level test and one class holding two methods.
MODULE = '''
class TestHolder:
    def test_inside_a_class(self):
        pass

    def test_another(self):
        pass


def test_at_module_level():
    pass
'''


def _load():
    spec = importlib.util.spec_from_file_location("check_citation_resolves", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load()


def _tree(tmp_path: Path, citation: str) -> Path:
    """A tree whose only citation is `citation`, in a prose file.

    :param tmp_path: the test's temporary directory.
    :param citation: the citation text to write, verbatim.
    :returns: the tree root.
    """
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_thing.py").write_text(MODULE, encoding="utf-8")
    (tmp_path / "notes.md").write_text(f"The reading is in {citation} today.\n", encoding="utf-8")
    return tmp_path


# --- the defect, and the shape of its remedy -----------------------------------


def test_a_class_method_cited_without_its_class_is_reported(mod, tmp_path):
    """The defect itself: the file resolves, the name resolves, the node id does not."""
    root = _tree(tmp_path, "`tests/test_thing.py::test_inside_a_class`")
    findings, unreadable = mod.scan(root)
    assert unreadable == []
    assert len(findings) == 1
    found = findings[0]
    assert found.cited == "tests/test_thing.py::test_inside_a_class"
    assert found.needed == "tests/test_thing.py::TestHolder::test_inside_a_class"
    assert found.site == "notes.md" and found.line == 1


def test_the_qualified_form_is_accepted(mod, tmp_path):
    """The remedy the tool prints is the form the tool accepts: it must be."""
    root = _tree(tmp_path, "`tests/test_thing.py::TestHolder::test_inside_a_class`")
    findings, unreadable = mod.scan(root)
    assert (findings, unreadable) == ([], [])


def test_a_module_level_test_needs_no_qualifier(mod, tmp_path):
    """The common case: 32 citations in this tree take this shape and collect."""
    root = _tree(tmp_path, "`tests/test_thing.py::test_at_module_level`")
    assert mod.scan(root) == ([], [])


# --- the lookalikes, each of which must stay silent -----------------------------


def test_a_path_that_does_not_exist_is_not_a_citation(mod, tmp_path):
    """Synthetic node ids are fixture *data* (`test_check_merge_plan_suite.py`).

    A guard that flagged them would be red at master for prose that is correct.
    """
    root = _tree(tmp_path, "`tests/test_a.py::test_b`")
    assert mod.scan(root) == ([], [])


def test_a_bare_name_in_prose_is_not_a_citation(mod, tmp_path):
    """A name is required to exist; a node id is required to collect. Different claims."""
    root = _tree(tmp_path, "mirrors `test_inside_a_class`'s slow-chat pattern")
    assert mod.scan(root) == ([], [])


def test_a_prefix_citation_is_not_a_citation(mod, tmp_path):
    """`tests/test_daemon.py::test_shutdown_all_*` names a family, not a node id."""
    root = _tree(tmp_path, "`tests/test_thing.py::test_inside_*`")
    assert mod.scan(root) == ([], [])


def test_a_class_citation_is_not_flagged(mod, tmp_path):
    """pytest collects a class as written, so there is nothing to qualify."""
    root = _tree(tmp_path, "`tests/test_thing.py::TestHolder`")
    assert mod.scan(root) == ([], [])


def test_a_citation_whose_name_is_absent_is_left_to_another_rule(mod, tmp_path):
    """A renamed test is a different defect, and guessing at it would be worse."""
    root = _tree(tmp_path, "`tests/test_thing.py::test_that_never_existed`")
    assert mod.scan(root) == ([], [])


# --- the exit-code contract, and the tree --------------------------------------


def test_the_exit_code_says_which_of_the_three_answers_it_is(mod, tmp_path, capsys):
    """`0` clean, `1` a finding, `2` unmeasurable - never two answers in one number."""
    assert mod.main([str(_tree(tmp_path / "clean", "`tests/test_thing.py::test_at_module_level`"))]) == 0
    assert "collects" in capsys.readouterr().out

    bad = _tree(tmp_path / "bad", "`tests/test_thing.py::test_inside_a_class`")
    assert mod.main([str(bad)]) == 1
    out = capsys.readouterr().out
    assert "TestHolder" in out, "the remedy has to be printed, not only the fault"

    assert mod.main([str(tmp_path / "not-a-directory")]) == 2
    assert "could not measure" in capsys.readouterr().err


def test_a_cited_module_that_cannot_be_parsed_is_unmeasurable(mod, tmp_path, capsys):
    """A syntax error in a cited module must not read as "clean".

    The green verdict is a statement over the modules the scan could read, so an
    unreadable one is `2`. Reporting it as `0` is the false OK this whole family
    of tools exists to prevent.
    """
    root = _tree(tmp_path, "`tests/test_thing.py::test_inside_a_class`")
    (root / "tests" / "test_thing.py").write_text("def broken(:\n", encoding="utf-8")
    assert mod.main([str(root)]) == 2
    assert "could not measure" in capsys.readouterr().err


def test_the_real_tree_cites_no_class_method_without_its_class(mod):
    """The rule, held against the tree it was derived from (measured: 0 sites).

    This is the assertion that goes red the day the un-qualified form is written
    again - and it is only meaningful beside the lookalike tests above, because a
    rule that also fires on a fixture's synthetic id would have to be deleted
    rather than obeyed.
    """
    findings, unreadable = mod.scan(REPO_ROOT)
    assert unreadable == [], f"unmeasurable: {unreadable}"
    assert findings == [], "\n".join(f"{f.site}:{f.line}: {f.cited} -> {f.needed}" for f in findings)
