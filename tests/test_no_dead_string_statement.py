"""Nothing is written into a function body that the interpreter throws away.

Why this file exists
--------------------
A **string statement that is not the first statement of a body** is not text the
program keeps: Python takes the first one as `__doc__` and evaluates every later one
and discards it. When a new docstring is inserted *above* an existing one, the old
text silently becomes that dead statement — the two read as one paragraph in a diff,
and nothing in the suite notices, because no test reads `__doc__` of the member the
text describes.

Measured 2026-10-03 (`cyc20261003-231313`) on `scripts/check-vote-count.py`:
`Verdict.mark` gained a terminal paragraph above its existing docstring, and lines
1058-1066 (`BLOCKED` whenever the text cannot merge, …) became unreachable text. It
was found by reading the member for another reason, not by any check.

What is asserted, and the leg that keeps it from being decoration
-----------------------------------------------------------------
* the **tree** carries no such statement (the enforcement), and
* a **synthetic source built to carry the exact shape is reported**, so a checker that
  silently matched nothing cannot pass this file. That leg is the point: with only the
  first assertion, deleting the checker would look identical to a clean tree.

The rule is about **adjacent** string statements, which is the shape the mistake
makes, and it is deliberately narrower than "any non-first string statement".
Measured over `emrg/`, `scripts/`, `tests/` and `packaging/` before the rule was
written: 3 non-first string statements exist, and 2 of them are the module-level
variable-documentation idiom in `emrg/server/content_risk_probe.py`
(`Record = dict` followed by its description), which is intentional. Zero of the 3
are adjacent pairs, so the narrow rule has no false positives and the broader one
would have had to carry an allow-list. `tests/test_a_skipped_subject_is_reported.py`
records the same direction of judgement.

A second shape is dead for the same reason and is reported too (added 2026-10-04,
`cyc20261004-011003`, while reviewing this file): an **f-string** is not an
`ast.Constant`, so the adjacent rule as first written could not see one - and an
f-string in the first position of a body is not a docstring at all, because
`ast.get_docstring` does not recognise it. Neither shape needs an allow-list: no
idiom makes a first-statement f-string intentional, and 0 f-string statements exist
in this tree, so both clauses cost nothing today.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = ("emrg", "scripts", "tests", "packaging")
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}
#: A body whose first statement is a docstring: module, class, function, method.
BODY_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _is_string_statement(node: ast.stmt) -> bool:
    """Whether `node` is a bare string expression - the only shape that can be dead.

    The **f-string** counts, which is not a technicality: it is not an `ast.Constant`,
    so a rule written around `Constant` alone is blind to it, while the interpreter
    evaluates and discards it exactly like a literal one *and* `ast.get_docstring`
    does not recognise it - so an f-string in the docstring position leaves the member
    with `__doc__ = None` while looking like documentation in the source. Measured
    2026-10-04 (`cyc20261004-011003`): 0 f-string statements in this tree, so the
    clause costs nothing and closes a shape of the same class.
    """
    if not isinstance(node, ast.Expr):
        return False
    if isinstance(node.value, ast.JoinedStr):
        return True
    return isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _is_fstring_statement(node: ast.stmt) -> bool:
    """Whether `node` is a bare f-string expression - never a docstring, always dropped."""
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.JoinedStr)


def dead_string_statements(source: str) -> list[int]:
    """Line numbers of a string statement the interpreter throws away.

    Two shapes, both of them text that is evaluated and discarded:

    * one that **directly follows another string statement** - the inserted-docstring
      defect this file was written for, where the upper text survives as `__doc__` and
      the lower is dead;
    * an **f-string in the first position of a body** - not the adjacent case, and not
      a docstring either (`ast.get_docstring` answers `None` for it), so a member
      documented that way has no documentation at all. Reported because no allow-list
      is needed for it: there is no idiom in which a first-statement f-string is
      intentional.
    """
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, BODY_OWNERS):
            continue
        body = node.body
        for first, second in zip(body, body[1:]):
            if _is_string_statement(first) and _is_string_statement(second):
                found.append(second.lineno)
        if body and _is_fstring_statement(body[0]):
            found.append(body[0].lineno)
    return sorted(found)


def scanned_files(root: Path) -> list[Path]:
    """Every `.py` under `root`'s scanned dirs that the skip set does not exclude.

    The skip test reads the path **relative to `root`**, which is what makes this a
    function of `root` alone: an absolute-parts test would answer differently on a
    machine whose checkout happens to sit under a directory named `build`, `dist` or
    `node_modules`, and a reading that depends on where the machine put the tree is not
    a reading of the tree. The three scripts in this family spell the same test the same
    way.
    """
    out: list[Path] = []
    for name in SCANNED_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if set(path.relative_to(root).parts) & SKIP_DIRS:
                continue
            out.append(path)
    return out


def scan_tree(root: Path) -> tuple[list[str], list[str]]:
    """Read `root`; return (`offenders`, `unreadable`).

    `unreadable` is the leg that keeps a clean verdict honest - a file this rule could
    not read is not a file it cleared - and it is returned rather than asserted here so
    that the caller can tell the two apart, and so that a tree this rule cannot read has
    a test of its own instead of being pinned by nothing.
    """
    offenders: list[str] = []
    unreadable: list[str] = []
    for path in scanned_files(root):
        rel = path.relative_to(root)
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f"{rel} ({type(exc).__name__})")
            continue
        try:
            lines = dead_string_statements(source)
        except SyntaxError as exc:
            unreadable.append(f"{rel} (SyntaxError: {exc.msg})")
            continue
        offenders += [f"{rel}:{line}" for line in lines]
    return offenders, unreadable


class TestTheRuleIsReal:
    def test_the_inserted_docstring_shape_is_reported(self) -> None:
        """The exact defect, reduced: a new docstring inserted above the old one."""
        source = (
            "def f():\n"
            '    """The new paragraph.\n'
            "\n"
            "    Why it is here.\n"
            '    """\n'
            '    """The old paragraph, now dead.\n'
            '    """\n'
            "    return 1\n"
        )
        reported = dead_string_statements(source)
        assert len(reported) == 1, reported
        # Named by its text rather than by a hand-counted line number: the question is
        # *which* statement is dead, and a number that drifts with the fixture proves
        # only that the fixture is unchanged.
        assert source.splitlines()[reported[0] - 1].strip().startswith('"""The old paragraph')

    def test_two_string_statements_in_a_module_are_reported_too(self) -> None:
        """Not a function-only rule: the shape is the same in any body."""
        assert dead_string_statements('"""One."""\n"""Two."""\n') == [2]

    def test_a_single_docstring_is_not_reported(self) -> None:
        """The control: the ordinary case must stay silent, or the rule is noise."""
        assert dead_string_statements('def f():\n    """Only one."""\n    return 1\n') == []

    def test_a_variable_documented_by_a_string_is_not_reported(self) -> None:
        """`x = 1` followed by its description is the idiom, not the mistake.

        Two of the three non-first string statements on this tree are this shape, and
        the rule is adjacent-only so that they are not reported. If this ever fails,
        the rule was widened and the allow-list it needed is missing.
        """
        source = 'x = 1\n"""What x is."""\n'
        assert dead_string_statements(source) == []

    def test_an_fstring_beside_a_string_is_reported(self) -> None:
        """The adjacent shape, written with an f-string below the real docstring.

        Added 2026-10-04 (`cyc20261004-011003`) after reading this file: the predicate
        was written around `ast.Constant`, and an f-string is a `JoinedStr` - so the
        rule was silent on the whole shape. The text is dropped exactly the same way,
        which is the rule's subject, so the blindness was to a *kind*, not a case.
        """
        source = 'def f():\n    """The live text."""\n    f"""The dead {1}."""\n    return 1\n'
        assert dead_string_statements(source) == [3]

    def test_an_fstring_that_opens_a_body_is_reported(self) -> None:
        """The first-position shape: not adjacent, and not a docstring either.

        `ast.get_docstring` answers `None` for a body that opens with an f-string, so
        the member silently has no documentation - and the text is evaluated and
        discarded. No allow-list is needed for this one (there is no idiom in which a
        first-statement f-string is intentional), which is why it is reported rather
        than traded away for the narrowness the paragraph above records.
        """
        source = 'def f():\n    f"""Documents nothing."""\n    return 1\n'
        assert dead_string_statements(source) == [2]
        assert ast.get_docstring(ast.parse(source).body[0]) is None, (
            "the premise of this test: Python does not read that string as a docstring"
        )


class TestThisTree:
    def test_no_dead_string_statement_is_written_in_this_tree(self) -> None:
        """The enforcement — and it reads the tree it names."""
        offenders, unreadable = scan_tree(REPO_ROOT)
        assert not unreadable, (
            "a file this rule could not read is not a file it cleared: " + ", ".join(unreadable)
        )
        assert not offenders, (
            "a string statement that is not the first in its body is evaluated and "
            "thrown away - the text is dead, and the two statements read as one "
            "paragraph in a diff:\n  " + "\n  ".join(offenders)
        )

    def test_the_scan_reaches_the_family_it_names(self) -> None:
        """A scan that finds no file would pass the assertion above for the wrong reason."""
        scanned = scanned_files(REPO_ROOT)
        assert len(scanned) > 100, f"the scan saw only {len(scanned)} files"
        assert REPO_ROOT / "scripts" / "check-vote-count.py" in scanned, (
            "the file whose defect this rule came from must be in the scan"
        )


class TestTheRefusals:
    """A reading of nothing is not a clean reading, and the leg that says so needs a test.

    Both legs are driven on a root this file builds, which is what the extraction bought:
    before it, the scan lived inside the enforcement test and walked `REPO_ROOT` only, so
    the `unreadable` branch could not be reached by any input and a later edit that dropped
    it would have left the file green while the rule silently cleared what it could not
    read.
    """

    def test_a_file_that_will_not_parse_is_not_a_clean_reading(self, tmp_path: Path) -> None:
        tree = tmp_path / "emrg"
        tree.mkdir()
        (tree / "broken.py").write_text("def f(:\n    pass\n")
        (tree / "fine.py").write_text('"""Docs."""\n\n\ndef f():\n    return 1\n')

        offenders, unreadable = scan_tree(tmp_path)

        assert offenders == [], offenders
        assert len(unreadable) == 1, unreadable
        assert "broken.py" in unreadable[0] and "SyntaxError" in unreadable[0], unreadable

    def test_the_same_rule_reports_the_dead_shape_on_a_root(self, tmp_path: Path) -> None:
        """The other direction, at the layer the synthetic sources above do not cover."""
        tree = tmp_path / "tests"
        tree.mkdir()
        (tree / "bad.py").write_text(
            '"""Docs."""\n'
            "\n"
            "\n"
            "def f():\n"
            '    """One."""\n'
            '    """Two."""\n'
            "    return 1\n"
        )

        offenders, unreadable = scan_tree(tmp_path)

        assert unreadable == [], unreadable
        # Built the way the scanner builds it, so the expectation names the fact (this
        # file, this line) and not the separator: written as `"tests/bad.py:6"` it passes
        # on POSIX and fails on Windows, where the report reads `tests\bad.py:6`
        # (measured: run 37983292835, `test-windows`).
        assert offenders == [f"{Path('tests') / 'bad.py'}:6"], offenders


def test_the_rule_is_about_python_not_prose() -> None:
    """Prose that *quotes* the shape is not the shape.

    The same lesson the sibling rules record: a text search cannot tell a rule's
    explanation from a rule's violation. `review-queue.py`'s docstring quotes the old
    tuple it used to spell, and this file's own text quotes the defect — neither is
    code, and `ast` is what separates them.
    """
    quoted = 'x = 1\n# a docstring inserted above another: """a""" then """b"""\n'
    assert dead_string_statements(quoted) == []
    with pytest.raises(SyntaxError):
        dead_string_statements("def f(:\n")
