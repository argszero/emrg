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
        offenders: list[str] = []
        unreadable: list[str] = []
        for name in SCANNED_DIRS:
            base = REPO_ROOT / name
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.py")):
                if set(path.parts) & SKIP_DIRS:
                    continue
                try:
                    source = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    unreadable.append(f"{path.relative_to(REPO_ROOT)} ({type(exc).__name__})")
                    continue
                try:
                    lines = dead_string_statements(source)
                except SyntaxError as exc:
                    unreadable.append(f"{path.relative_to(REPO_ROOT)} (SyntaxError: {exc.msg})")
                    continue
                offenders += [f"{path.relative_to(REPO_ROOT)}:{line}" for line in lines]
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
        scanned = [
            p
            for name in SCANNED_DIRS
            for p in (REPO_ROOT / name).rglob("*.py")
            if set(p.parts) & SKIP_DIRS == set()
        ]
        assert len(scanned) > 100, f"the scan saw only {len(scanned)} files"
        assert REPO_ROOT / "scripts" / "check-vote-count.py" in scanned, (
            "the file whose defect this rule came from must be in the scan"
        )


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
