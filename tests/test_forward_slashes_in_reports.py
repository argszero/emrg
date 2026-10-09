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
``relative.parts`` and ``relative.as_posix()``) clean. "A name bound to the call" is every
binding shape a renderer uses -- a plain assignment, an annotated one, a tuple target and a
walrus -- because a rule that said "bound" and recognised one spelling of it would be silent
about the other three while the sentence here promised otherwise.

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

#: Directories that hold no source this rule owns. Read **relative to the root the sweep was
#: pointed at**, never against an absolute path: an ancestor of the checkout carrying one of
#: these names is not an in-tree directory, and reading past the root let it silence the whole
#: sweep (measured 2026-10-10 on this file's own tree: the same commit answered 11 passed under
#: `<plain>/wt` and `only 0 file(s) were judged` under `<plain>/node_modules/wt`).
SKIPPED = {".venv", "__pycache__", "node_modules"}


def swept_files(base: Path) -> list[Path]:
    """Every `.py` under `base` the skip does not exclude, in a stable order.

    The skip reads `path.relative_to(base).parts`, so it is a fact about the tree it was
    pointed at rather than about where the machine put the checkout; the directories the skip
    is for stay skipped, because inside `base` they are the vendored trees it means.

    :param base: the root to walk.
    :returns: the paths, sorted.
    """
    return [
        path
        for path in sorted(Path(base).rglob("*.py"))
        if not SKIPPED & set(path.relative_to(base).parts)
    ]


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


def _bound_to_relative_to(tree: ast.Module) -> dict[str, int]:
    """Every name this module binds to a ``.relative_to(...)`` call, at any binding shape.

    The shapes a renderer actually writes: a plain assignment (``rel = p.relative_to(root)``),
    an annotated one (``relative: Path = p.relative_to(root)``), a tuple target, where the
    name is the element *paired with* the call (``rel, rest = p.relative_to(root), other``),
    and a walrus. The first cut recognised only ``ast.Assign`` with a plain ``Name`` target,
    so a renderer written in any of the other three was reported by nothing -- while the
    module docstring promised "a name bound to the call", and ``f"{relative}"`` after
    ``relative: Path = p.relative_to(root)`` came back clean (measured 2026-10-10).

    :param tree: the parsed module.
    :returns: name -> the statement's line, first binding wins.
    """
    bound: dict[str, int] = {}

    def note(target: ast.AST, value: ast.AST | None) -> None:
        if isinstance(target, ast.Name) and _is_relative_to_call(value):
            bound.setdefault(target.id, target.lineno)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            # A tuple target is paired with a tuple value element by element, so only the
            # position holding the call is bound to it -- `other` is not.
            elements = node.value.elts if isinstance(node.value, (ast.Tuple, ast.List)) else None
            for target in node.targets:
                if (
                    isinstance(target, (ast.Tuple, ast.List))
                    and elements is not None
                    and len(target.elts) == len(elements)
                ):
                    for element, value in zip(target.elts, elements):
                        note(element, value)
                else:
                    note(target, node.value)
        elif isinstance(node, ast.AnnAssign):
            note(node.target, node.value)
        elif isinstance(node, ast.NamedExpr):
            note(node.target, node.value)
    return bound


def offenders(source: str) -> list[int]:
    """Line numbers where `source` renders a `.relative_to(...)` without `.as_posix()`.

    :param source: the module text to read.
    :returns: the sorted line numbers, one per offending rendering.
    """
    tree = ast.parse(source)
    bound = _bound_to_relative_to(tree)
    found: list[int] = []
    for expression in rendered_expressions(tree):
        # A walrus rendered in place *is* the call it binds: `f"{(rel := p.relative_to(r))}"`
        # reaches the same text as the two-line spelling, so it is read the same way.
        if isinstance(expression, ast.NamedExpr):
            expression = expression.value
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

    def test_an_annotated_binding_is_reported(self) -> None:
        """`relative: Path = p.relative_to(root)` binds the value the plain spelling binds.

        This is the shape that made the rule's own sentence false: it is a name bound to the
        call and rendered later, and the detector's `ast.Assign`-only binding map did not see
        it (measured 2026-10-10 -- this source came back `[]`).
        """
        assert offenders('relative: Path = p.relative_to(root)\nprint(f"{relative}:{n}")\n') == [1]

    def test_a_tuple_binding_reports_only_the_element_paired_with_the_call(self) -> None:
        """The tuple value is paired element by element; the other name is bound to something else."""
        assert offenders('rel, other = p.relative_to(root), compute()\nprint(f"{rel}")\n') == [1]
        assert offenders('other, rel = compute(), p.relative_to(root)\nprint(f"{rel}")\n') == [1]

    def test_a_walrus_rendered_in_place_is_reported(self) -> None:
        assert offenders('print(f"{(rel := p.relative_to(root))}")\n') == [1]

    def test_a_name_bound_to_something_else_is_silent(self) -> None:
        """The other direction: the widened binding map must not report a name it is not bound to.

        Each case is one of the shapes above with the call replaced, so a detector that
        reported the whole binding rather than which value it holds would fail here.
        """
        assert offenders('relative: Path = compute()\nprint(f"{relative}")\n') == []
        assert offenders('rel, other = compute(), also_compute()\nprint(f"{rel}")\n') == []
        assert offenders('print(f"{(rel := compute())}")\n') == []

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
            for path in swept_files(base):
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


class TestTheSweep:
    def test_the_skip_reads_the_path_relative_to_the_root(self, tmp_path: Path) -> None:
        """Two directions on one tree, and the tree is put where the skip's own name is an ancestor.

        The sweep is given a root that **sits under** a directory named `node_modules` -- the
        component the skip is about -- and must still judge the module inside it. An absolute
        `set(path.parts)` cannot tell that ancestor from an in-tree directory, so it skipped the
        whole sweep and the counting assertion above read 0 file(s) judged (measured 2026-10-10:
        the same commit passed on a plain path and failed under a `node_modules` one). The
        directory the skip is for must keep being skipped, which is the exclusion's purpose.
        """
        root = tmp_path / "node_modules" / "checkout" / "emrg"
        root.mkdir(parents=True)
        (root / "subject.py").write_text('print(f"{p.relative_to(root)}")\n', encoding="utf-8")
        vendored = root / "node_modules" / "dep"
        vendored.mkdir(parents=True)
        (vendored / "captured.py").write_text(
            'print(f"{p.relative_to(root)}")\n', encoding="utf-8"
        )

        judged = [path.name for path in swept_files(root)]

        assert "subject.py" in judged, (
            f"the ordinary module under {root} was not judged - the sweep answers about where "
            f"the checkout sits rather than about the tree: {judged}"
        )
        assert "captured.py" not in judged, (
            f"an in-tree vendored directory must stay skipped: {judged}"
        )
