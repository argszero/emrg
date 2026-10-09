"""A path rendered into a report is `/`-separated on every platform.

The rule, and why it is not a style preference
----------------------------------------------
``str(Path.relative_to(...))`` renders with the *platform's* separator: ``emrg/bad.py`` on
POSIX, ``emrg\\bad.py`` on Windows. A report carrying that text is read across a boundary --
by a host, by a CI log, by a sibling guard, and by the tests written against it -- so the
separator it happens to carry is a fact about the machine that produced it, not about the
finding. `tests/test_conflict_markers.py` states the rule for its own consumers ("every
consumer below must use `.as_posix()`"), and `check-doc-count.py`, `check-install-drift.py`,
`check-merge-plan-suite.py` and `check-rant-citations.py` already obey it. Nothing failed
when a new renderer did not, and three times in the last month one did not:

* 2026-09-10, `check-conflict-markers` -- **two** CI rounds, the second because only the
  string git emits had been fixed and not the ``Path.relative_to`` rendering;
* 2026-10-09, run `37923398190` -- a test compared findings against ``emrg/bad.py:2``, and
  the remedy was a per-file normaliser (``_reported()``) rather than a fix;
* 2026-10-10, `#2012`'s run `37983292835` -- ``AssertionError: ['tests\\\\bad.py:6']``, again
  found only by the ``windows-2025`` leg.

It had also already cost coverage: the one test asserting the shape of a ``grep`` result was
skipped on Windows for exactly this reason (`test_grep_simple`, whose skipif is gone now that
the tool renders `.as_posix()`).

What is asserted
----------------
Every ``.relative_to(...)`` value that reaches text -- an f-string field, a name bound to the
call and rendered later, or ``str(...)`` of the call -- must carry ``.as_posix()``. The
detector is an AST walk rather than a text match, so a call split across lines, an f-string
built by implicit concatenation and a rendering written in a nested expression are all seen;
and it keys on the **rendering**, not on the binding, which is what keeps
`check-doc-count.py`'s shape (``relative = path.relative_to(REPO_ROOT)`` read for
``relative.parts`` and ``relative.as_posix()``) clean.

The controls below drive the detector in both directions on synthetic sources, so a detector
that has silently stopped matching cannot pass this file by returning an empty list -- the
failure mode a sweep like this has by construction. The tree leg is asserted to have judged a
non-trivial number of files for the same reason.

Named limits
------------
* the sweep covers ``emrg/`` and ``scripts/``, the trees whose output crosses the boundary.
  A **test-local** scanner and the assertion that reads it may legitimately agree on either
  spelling -- `#2012`'s remedy builds the expectation with ``f"{Path('tests') / 'bad.py'}:6"``
  -- because its only consumer is the same file;
* an absolute ``str(Path)`` is not a repo-relative rendering and no assertion can be written
  about it portably, so it is out of scope;
* ``git ls-files`` names are out of scope too: git emits ``/`` on every platform, so the
  normalisation `check-conflict-markers.py` needs there is a different, already-fixed strand;
* ``"{}".format(...)`` and ``%`` formatting are not recognised as renderings -- neither
  spelling is used in the swept trees today, and a new one would arrive unwatched rather than
  reported wrongly.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The trees whose rendered output crosses a boundary: a host reads it, a CI log keeps it,
#: and a sibling guard parses it.
SWEPT = ("emrg", "scripts")

#: Directories that hold no source this rule owns.
SKIPPED = {".venv", "__pycache__", "node_modules"}


def _is_relative_to_call(node: ast.AST) -> bool:
    """Is this expression literally a `.relative_to(...)` call?"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "relative_to"
    )


def rendered_expressions(tree: ast.Module) -> list[ast.AST]:
    """Every expression this module turns into text, at the two spellings used here.

    An f-string field and ``str(<one argument>)``: those are the two ways a path reaches a
    report in this repository. The ``str`` case refuses a call with keywords, so a
    ``str(x, encoding=...)`` -- which is not a rendering -- is not mistaken for one.
    """
    out: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FormattedValue):
            out.append(node.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "str"
            and len(node.args) == 1
            and not node.keywords
        ):
            out.append(node.args[0])
    return out


def offenders(source: str) -> list[int]:
    """Line numbers where `source` renders a `.relative_to(...)` without `.as_posix()`.

    :param source: the module text to read.
    :returns: the sorted line numbers, one per offending rendering.
    """
    tree = ast.parse(source)
    bound: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_relative_to_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = node.value.lineno
    found: list[int] = []
    for expression in rendered_expressions(tree):
        if _is_relative_to_call(expression):
            found.append(expression.lineno)
        elif isinstance(expression, ast.Name) and expression.id in bound:
            found.append(bound[expression.id])
    return sorted(set(found))


class TestTheDetector:
    """Both directions, on sources small enough to read: a detector that stopped matching
    would otherwise pass the tree leg by finding nothing."""

    def test_a_bare_rendering_is_reported(self) -> None:
        assert offenders('f"{p.relative_to(root)}"\n') == [1]

    def test_the_str_spelling_is_reported_too(self) -> None:
        assert offenders("x = str(p.relative_to(root))\n") == [1]

    def test_a_name_bound_to_it_and_rendered_is_reported(self) -> None:
        assert offenders('rel = p.relative_to(root)\nprint(f"{rel}:{n}")\n') == [1]

    def test_the_as_posix_spelling_is_silent(self) -> None:
        assert offenders('f"{p.relative_to(root).as_posix()}"\n') == []
        assert offenders("x = str(p.relative_to(root).as_posix())\n") == []

    def test_a_binding_read_for_parts_and_as_posix_is_silent(self) -> None:
        """`check-doc-count.py`'s shape: the call is bound and never rendered raw.

        This is the control that keeps the rule about the *rendering* rather than about the
        call: a rule keyed on `relative_to` alone would report the two renderers that are
        already right, and a rule that cannot tell them apart is one nobody would keep.
        """
        source = (
            "relative = path.relative_to(REPO_ROOT)\n"
            "if any(part in BUILDS for part in relative.parts):\n"
            "    continue\n"
            "name = relative.as_posix()\n"
            "print(f'  {name}:{lineno}')\n"
        )

        assert offenders(source) == []

    def test_parts_alone_is_silent(self) -> None:
        assert offenders('print(f"{p.relative_to(root).parts}")\n') == []


class TestThisTree:
    def test_no_report_renders_a_platform_separator(self) -> None:
        """The rule, over the trees whose reports cross a boundary."""
        found: list[str] = []
        judged = 0
        for directory in SWEPT:
            base = REPO_ROOT / directory
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.py")):
                if any(part in SKIPPED for part in path.parts):
                    continue
                try:
                    source = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                judged += 1
                for line in offenders(source):
                    found.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line}")

        assert judged > 50, (
            f"only {judged} file(s) were judged: a sweep over an empty set is a clean verdict "
            "about nothing, and that is the one reading this file must not give"
        )
        assert not found, (
            "a report that renders `str(Path.relative_to(...))` reads `emrg/bad.py` on POSIX "
            "and `emrg\\bad.py` on Windows, so every assertion written against the POSIX "
            "spelling passes here and fails `test-windows`. Render `.as_posix()`: "
            + ", ".join(found)
        )
