"""No test module may bind the same name twice in one scope.

Why this exists
---------------
Python binds a name to the **last** definition in a scope, so a second
``def test_x`` in one module does not add a test — it replaces the first. The
original body stops running while remaining visible in the file, and nothing in
a green run says so: the suite total is unchanged, the surviving item keeps the
same node id, and a reviewer reading the diff sees a test being added.

Measured specimen on master ``dd2a0e64`` (2026-09-19):
``tests/test_ws_e2e.py::TestWSVibeCheck`` carried
``test_vibe_check_uses_session_history`` twice — the first definition a
docstring stub, the second the real test — so the file named one test twice and
the stub was dead text. Removed together with this guard, which is what makes
the assertion below hold.

The case this guard was written for is the one where the shadowed body is *not*
vestigial: PR #1414 (reviewed by cycle ``cyc20260919-122655``) appended a second
definition of an existing test name in ``tests/test_scheduler.py``, and the
original body's 17 assertions — contribution level, de-EMRG-ified scope,
adversarial checks, direction diversity, hotspot/current-time injection, both
roles — stopped executing. They still **passed** against that tree, and the
landing tree's suite total rose by two while the file added three functions, so
the loss was silent in both directions.

What is scanned, and what is not
--------------------------------
Scanned, because a repeat there is always a shadow:

* every module-level ``def`` and ``class`` in a test module;
* ``test_*`` methods of ``Test*`` classes, including classes nested inside
  classes — pytest recurses into those.

Not scanned, because a repeat there is legal, idiomatic, or invisible to the
collector:

* **class bodies other than the ``Test*`` ones** — the property/setter idiom
  defines ``dirty`` twice on purpose (``emrg/client/widgets.py``, and
  ``_CountingWidget`` in ``tests/test_app_widgets.py``), so a guard over all
  method names would flag every property in the tree;
* **anything inside a function body** — a ``class _Proc`` (or a ``Test*`` class)
  defined inside a test is local to that test, and eight such classes in
  ``tests/test_check_doc_count.py`` are the reason this distinction is drawn
  rather than assumed: an ``ast.walk`` over the module reports them as eight
  bindings of one name.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"


def _classes_reachable(body: list[ast.stmt], prefix: str = ""):
    """``(qualified_name, node)`` for every class not hidden inside a function body.

    Recursion descends through class bodies only: pytest recurses into a class
    nested in a class, but a class defined inside a function is local to that
    function and is never collected.
    """
    for node in body:
        if isinstance(node, ast.ClassDef):
            yield prefix + node.name, node
            yield from _classes_reachable(node.body, prefix + node.name + ".")


def bound_names(source: str) -> list[str]:
    """The names one module binds where a repeat would silently win.

    Module-level definitions keep their bare name; a class's test methods are
    qualified by their class, so the same method name in two classes is two
    distinct names rather than a collision.
    """
    tree = ast.parse(source)
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append(node.name)
    for qualified, node in _classes_reachable(tree.body):
        if not qualified.rsplit(".", 1)[-1].startswith("Test"):
            continue
        for sub in node.body:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                found.append(f"{qualified}::{sub.name}")
    return found


def shadowed(names: list[str]) -> list[str]:
    """The names occurring more than once — the ones a later definition takes over."""
    counts = Counter(names)
    return sorted(name for name, seen in counts.items() if seen > 1)


def test_no_test_module_binds_a_name_twice() -> None:
    """Every module under ``tests/`` binds each of its names exactly once."""
    files = sorted(TESTS_DIR.glob("test_*.py"))
    seen = 0
    offenders: list[str] = []
    for path in files:
        names = bound_names(path.read_text(encoding="utf-8"))
        seen += len(names)
        for name in shadowed(names):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {name}")

    # The scan must be looking somewhere: a `bound_names` that returned [] for
    # every file would make the assertion below pass over nothing.
    assert seen >= 1000, (
        f"only {seen} bound name(s) found across {len(files)} test module(s) — "
        "the scan is not looking where it thinks it is"
    )
    assert not offenders, (
        "these names are bound twice in one module, so the earlier definition no "
        f"longer runs while its body is still in the file: {offenders} — give the "
        "second one its own name, or fold its assertions into the first"
    )


def test_the_scan_flags_a_name_bound_twice() -> None:
    """The refusing half: each shape the class takes is reported.

    Fed one by one rather than relying on the tree happening to contain them, so
    an instrument that handled only test functions would be caught here.
    """
    module_level_test = (
        "def test_thing():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_thing():\n"
        "    assert True\n"
    )
    assert shadowed(bound_names(module_level_test)) == ["test_thing"]

    a_test_method = (
        "class TestWidget:\n"
        "    def test_render(self):\n"
        "        pass\n"
        "\n"
        "    def test_render(self):\n"
        "        pass\n"
    )
    assert shadowed(bound_names(a_test_method)) == ["TestWidget::test_render"]

    a_nested_test_class = (
        "class TestOuter:\n"
        "    class TestInner:\n"
        "        def test_one(self): pass\n"
        "        def test_one(self): pass\n"
    )
    assert shadowed(bound_names(a_nested_test_class)) == ["TestOuter.TestInner::test_one"]

    a_module_level_class = "class TestA:\n    pass\n\n\nclass TestA:\n    pass\n"
    assert shadowed(bound_names(a_module_level_class)) == ["TestA"]

    a_module_level_helper = "def _make():\n    pass\n\n\ndef _make():\n    pass\n"
    assert shadowed(bound_names(a_module_level_helper)) == ["_make"]


def test_the_scan_leaves_legal_repetition_alone() -> None:
    """The accepting half: repetition that is legal, or invisible to the collector.

    Without this the guard could be satisfied by an instrument that flags
    anything repeated — the property/setter idiom alone would make that
    unusable, and it is written here from this tree's own spelling of it.
    """
    property_setter = (
        "class Widget:\n"
        "    @property\n"
        "    def dirty(self): return self._dirty\n"
        "    @dirty.setter\n"
        "    def dirty(self, v): self._dirty = v\n"
    )
    assert bound_names(property_setter) == ["Widget"]

    a_helper_repeated_in_a_non_test_class = (
        "class _Recorder:\n"
        "    def render(self): pass\n"
        "    def render(self): pass\n"
    )
    assert bound_names(a_helper_repeated_in_a_non_test_class) == ["_Recorder"]

    nested_in_a_test_body = (
        "def test_outer():\n"
        "    def test_inner():\n"
        "        pass\n"
        "    def test_inner():\n"
        "        pass\n"
        "    test_inner()\n"
    )
    assert bound_names(nested_in_a_test_body) == ["test_outer"]

    a_test_class_defined_inside_a_test = (
        "def test_builds_its_own_fixture():\n"
        "    class TestLocal:\n"
        "        def test_one(self): pass\n"
        "\n"
        "class TestLocal:\n"
        "    def test_two(self): pass\n"
    )
    # The local class is not collected, so `TestLocal::test_one` is not a name
    # in this scope at all — only the module-level class's method is.
    assert bound_names(a_test_class_defined_inside_a_test) == [
        "test_builds_its_own_fixture",
        "TestLocal",
        "TestLocal::test_two",
    ]

    the_same_method_name_in_two_classes = (
        "class TestA:\n"
        "    def test_one(self): pass\n"
        "\n"
        "class TestB:\n"
        "    def test_one(self): pass\n"
    )
    assert shadowed(bound_names(the_same_method_name_in_two_classes)) == []

    a_non_test_method_named_like_a_test = "class Helper:\n    def test_looking_name(self): pass\n"
    assert bound_names(a_non_test_method_named_like_a_test) == ["Helper"]
