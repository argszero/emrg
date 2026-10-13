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
    findings, unreadable, _modules = mod.scan(root)
    assert unreadable == []
    assert len(findings) == 1
    found = findings[0]
    assert found.cited == "tests/test_thing.py::test_inside_a_class"
    assert found.needed == "tests/test_thing.py::TestHolder::test_inside_a_class"
    assert found.site == "notes.md" and found.line == 1


def test_the_qualified_form_is_accepted(mod, tmp_path):
    """The remedy the tool prints is the form the tool accepts: it must be."""
    root = _tree(tmp_path, "`tests/test_thing.py::TestHolder::test_inside_a_class`")
    findings, unreadable, _modules = mod.scan(root)
    assert (findings, unreadable) == ([], [])


def test_a_qualified_citation_whose_class_is_absent_is_reported(mod, tmp_path):
    """The qualified form is the most explicit citation there is, so it has to be read.

    It was skipped outright until 2026-10-13 (`cyc20261013-212318`), by a branch whose
    comment said the rule "cannot answer from the qualifier alone" - while it held the
    module's AST in the line above.
    """
    root = _tree(tmp_path, "`tests/test_thing.py::TestGhost::test_inside_a_class`")
    findings, _unreadable, _modules = mod.scan(root)
    assert len(findings) == 1
    assert findings[0].cited == "tests/test_thing.py::TestGhost::test_inside_a_class"
    assert "no class by that name" in findings[0].reason


def test_a_qualified_citation_naming_the_wrong_class_is_reported(mod, tmp_path):
    """`TestHolder` really is defined - it just does not hold the name, and pytest says so.

    The remedy is the class that does hold it, which is the one thing a reader cannot
    derive from the citation or from a plain `not found`.
    """
    root = _tree(tmp_path, "`tests/test_thing.py::TestAnother::test_inside_a_class`")
    (root / "tests" / "test_thing.py").write_text(
        MODULE.replace("class TestHolder:", "class TestAnother:\n    def test_other(self):\n        pass\n\n\nclass TestHolder:"),
        encoding="utf-8",
    )
    findings, _unreadable, _modules = mod.scan(root)
    assert len(findings) == 1
    assert findings[0].needed == "tests/test_thing.py::TestHolder::test_inside_a_class"


def test_a_class_that_inherits_from_outside_the_module_is_unmeasurable(mod, tmp_path, capsys):
    """The third answer, on the same subject: this reader cannot enumerate those methods.

    A class whose base this module does not define may hold the name through the base,
    so neither "it collects" nor "it does not" is readable from here. `2`, never `0` -
    a subject the reader cannot classify is not a pass (`#2211`).
    """
    root = _tree(tmp_path, "`tests/test_thing.py::TestChild::test_inside_a_class`")
    (root / "tests" / "test_thing.py").write_text(
        "from somewhere import Base\n\n\nclass TestChild(Base):\n    def test_own(self):\n        pass\n",
        encoding="utf-8",
    )
    assert mod.main([str(root)]) == 2
    err = capsys.readouterr().err
    assert "could not measure" in err and "TestChild" in err

    # The control: the same class shape with its base defined *in* the module, so the
    # reader can enumerate the methods it collects - the exclusion is the undefined
    # base, not the class.
    (root / "tests" / "test_thing.py").write_text(
        "class Base:\n    def test_inherited(self):\n        pass\n\n\n"
        "class TestChild(Base):\n    def test_own(self):\n        pass\n",
        encoding="utf-8",
    )
    (root / "notes.md").write_text(
        "`tests/test_thing.py::TestChild::test_inherited`\n", encoding="utf-8"
    )
    assert mod.main([str(root)]) == 0


def test_a_module_level_test_needs_no_qualifier(mod, tmp_path):
    """The common case: 32 citations in this tree take this shape and collect."""
    root = _tree(tmp_path, "`tests/test_thing.py::test_at_module_level`")
    assert mod.scan(root) == ([], [], 1)


# --- the lookalikes, each of which must stay silent -----------------------------


def test_a_path_that_does_not_exist_is_not_a_citation(mod, tmp_path):
    """Synthetic node ids are fixture *data* (`test_check_merge_plan_suite.py`).

    A guard that flagged them would be red at master for prose that is correct.
    """
    root = _tree(tmp_path, "`tests/test_a.py::test_b`")
    assert mod.scan(root) == ([], [], 1)


def test_a_bare_name_in_prose_is_not_a_citation(mod, tmp_path):
    """A name is required to exist; a node id is required to collect. Different claims."""
    root = _tree(tmp_path, "mirrors `test_inside_a_class`'s slow-chat pattern")
    assert mod.scan(root) == ([], [], 1)


def test_a_prefix_citation_is_not_a_citation(mod, tmp_path):
    """`tests/test_daemon.py::test_shutdown_all_*` names a family, not a node id."""
    root = _tree(tmp_path, "`tests/test_thing.py::test_inside_*`")
    assert mod.scan(root) == ([], [], 1)


def test_a_class_citation_is_not_flagged(mod, tmp_path):
    """pytest collects a class as written, so there is nothing to qualify."""
    root = _tree(tmp_path, "`tests/test_thing.py::TestHolder`")
    assert mod.scan(root) == ([], [], 1)


def test_a_citation_whose_name_is_absent_is_reported(mod, tmp_path):
    """A citation naming nothing the module defines collects nothing, and is now read.

    It was left alone until 2026-10-13 (`cyc20261013-212318`) as "another rule's"
    question, and no other rule read it: `scripts/review-queue.py` had cited a renamed
    test for as long as the green line claimed every citation collects.
    """
    root = _tree(tmp_path, "`tests/test_thing.py::test_that_never_existed`")
    findings, _unreadable, _modules = mod.scan(root)
    assert len(findings) == 1
    assert findings[0].cited == "tests/test_thing.py::test_that_never_existed"
    assert "defines no test, class or method" in findings[0].reason
    assert findings[0].needed == "", "there is no node id to name as the remedy"


# --- quoted material: what a fixture holds is not a claim about this tree --------


def _fixture_tree(tmp_path: Path, body: str) -> Path:
    """A tree whose `tests/` holds the module under test plus a literal `body`.

    The prose file's own citation is a module-level one that resolves, so the second
    module is the only thing this tree asks about.
    """
    root = _tree(tmp_path, "`tests/test_thing.py::test_at_module_level`")
    (root / "tests" / "test_quotes_output.py").write_text(body, encoding="utf-8")
    return root


def test_quoted_pytest_output_inside_a_literal_is_not_a_claim(mod, tmp_path):
    """The lookalike that made the name rule need a scope at all.

    `tests/test_run_mutation_arm.py` stubs pytest's output, and its node ids name a tree
    some test built - `tests/test_bash_v2_boundary.py` is a module this checkout really
    has, so the path check cannot exclude them.
    """
    root = _fixture_tree(
        tmp_path,
        'OUT = "FAILED tests/test_thing.py::test_absent - AssertionError\\n'
        '1 failed, 3 passed in 0.5s\\n"\n',
    )
    findings, unreadable, modules = mod.scan(root)
    assert (findings, unreadable, modules) == ([], [], 2)


def test_a_literal_holding_a_bare_node_id_is_not_a_claim(mod, tmp_path):
    """The other shape a fixture has: the expected node list, nothing else in it."""
    root = _fixture_tree(
        tmp_path, 'EXPECTED = ["tests/test_thing.py::test_absent"]\n'
    )
    findings, unreadable, modules = mod.scan(root)
    assert (findings, unreadable, modules) == ([], [], 2)


def test_a_citation_inside_prose_is_still_a_claim(mod, tmp_path):
    """The half that keeps the exclusion from swallowing the rule.

    One line of prose in the literal makes it documentation: a `run this to reproduce`
    message and a dict of reasons are both claims about this tree, and a reader acts on
    them.
    """
    root = _fixture_tree(
        tmp_path,
        'HINT = "run tests/test_thing.py::test_absent to reproduce\\n1 failed\\n"\n',
    )
    findings, _unreadable, _modules = mod.scan(root)
    assert len(findings) == 1, "a citation inside a sentence is a claim, not quoted output"
    assert findings[0].cited == "tests/test_thing.py::test_absent"


def test_the_two_exclusions_are_counted_rather_than_dropped(mod, tmp_path, capsys):
    """A narrowing nobody can see is how a green run stops meaning what it says."""
    root = _tree(tmp_path, "`tests/test_thing.py::test_inside_a_class_`")
    (root / "tests" / "test_quotes_output.py").write_text(
        'OUT = "tests/test_thing.py::test_absent"\n', encoding="utf-8"
    )
    assert mod.main([str(root)]) == 0
    out = capsys.readouterr().out
    assert "1 in quoted material" in out, out
    assert "1 naming a family" in out, out
    assert "citation(s) read as claims" in out, out


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
    err = capsys.readouterr().err
    assert "could not measure" in err
    assert "is not a directory" in err, (
        "a path that is not a directory and a directory holding no test module are two "
        "branches with two remedies (fix the path, versus run it where the tests are), so "
        "the refusal has to name the one it read rather than answering both with the "
        f"empty-set message:\n{err}"
    )


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


def test_a_root_with_no_test_module_is_unmeasurable_not_clean(mod, tmp_path, capsys):
    """The half the exit-code paragraph promised and `main` had no branch for.

    Measured 2026-10-06 (`cyc20261006-165503`): an empty directory printed
    `every citation names a node id pytest collects` with rc 0. A citation is resolved
    *against* a test module, so with none under `<root>/tests` the green line is a
    statement over an empty set - the same false OK the unreadable branch above
    refuses, one reading further out.
    """
    root = tmp_path / "empty"
    root.mkdir()
    assert mod.main([str(root)]) == 2
    captured = capsys.readouterr()
    assert "could not measure" in captured.err
    assert "collects" not in captured.out


def test_the_clean_verdict_names_how_many_modules_it_resolved_against(mod, tmp_path, capsys):
    """`0` has to say what it is a statement about - the count is the subject it read.

    Without it, "every citation resolves" over 137 test modules and over none are the
    same sentence, which is how the empty root above stayed invisible.
    """
    root = _tree(tmp_path, "`tests/test_thing.py::test_at_module_level`")
    assert mod.main([str(root)]) == 0
    assert "1 test module(s) read" in capsys.readouterr().out


def test_the_real_tree_cites_no_class_method_without_its_class(mod):
    """The rule, held against the tree it was derived from (measured: 0 sites).

    This is the assertion that goes red the day the un-qualified form is written
    again - and it is only meaningful beside the lookalike tests above, because a
    rule that also fires on a fixture's synthetic id would have to be deleted
    rather than obeyed.
    """
    findings, unreadable, modules = mod.scan(REPO_ROOT)
    assert unreadable == [], f"unmeasurable: {unreadable}"
    assert modules, (
        "this checkout carries no test module, so the green verdict below would be a "
        "reading over an empty set rather than a rule held against a tree"
    )
    assert findings == [], "\n".join(f"{f.site}:{f.line}: {f.cited} -> {f.needed}" for f in findings)
