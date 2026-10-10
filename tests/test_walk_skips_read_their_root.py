"""A tree walk's skip predicate reads the path relative to the root it walks.

Why this file exists
--------------------
A walk rooted at `R` yields **absolute** paths, so a predicate written
`set(path.parts) & SKIP` decides from where the machine put the checkout rather than from
the tree it is reading: any ancestor directory named `node_modules` (or `.venv`,
`__pycache__`) silences the whole walk, and the test that owns it reports a clean reading of
nothing. The family fixed exactly this, one file at a time, five times in two days:

* `b6f322dc` (#2012) -- `tests/test_no_dead_string_statement.py`;
* `01331b4d` (#2014) -- `tests/test_app_logs_live_under_logs_dir.py`;
* `82844d62` (#2022) -- `tests/test_probe_guard_reach.py`, two sites;
* `625d950a` (#2024) -- `tests/test_script_decode_is_locale_independent.py`, two of five
  vendored checks;
* #2018 head `30d8f719` -- `tests/test_forward_slashes_in_reports.py`.

Each fix added its own two-direction test, and none stopped the next one, because the rule
lived only in prose and in five private tests. `tests/test_guard_scan_scope_pairing.py` pins
the **roots** the family declares and says nothing about how the skip is spelled; this file
is that half. Measured 2026-10-10 on `625d950a`, the detector below reports 7 sites across
the five pre-fix files (every line each fix had to change), 0 in those files today, and
exactly 1 in the whole first-party tree -- `tests/test_check_patch_files.py`'s scratch-repo
cleanup, which now reads `relative_to(tmp_path)`.

What is asserted
----------------
`.parts` read from a name a walk produced must go through `.relative_to(...)`. "A name a walk
produced" is every shape a walk result reaches a predicate by: a `for` target, a comprehension
target, and a name assigned from a walk expression -- to a small fixpoint, so
`found = sorted(root.rglob(...))` followed by `for p in found` is covered rather than left as
the one spelling that escapes. The read is an **AST walk** rather than a text match, so a
predicate split across lines, written as a comprehension filter, or reached through `Path(...)`
is seen; and it keys on the `.parts` read rather than on the walk, which is what keeps the
`git ls-files` shape clean -- git emits `/`-separated names on every platform, so
`Path(rel).parts` over a tracked-name list is not a tree walk and is deliberately silent.

The controls below drive the detector in both directions on synthetic sources, so a detector
that has silently stopped matching cannot pass this file by returning an empty list -- the
failure mode a sweep like this has by construction. The tree leg asserts a non-trivial number
of files judged for the same reason.

Named limits
------------
* a walk is assumed to be rooted at an **absolute** path, which every walk in this suite is
  (`REPO_ROOT` or a `tmp_path`). A walk on a provably relative root -- a `Path("...")` holding
  a literal without a leading `/` -- is recognised and left alone; a relative root reached
  through a name is not, and a `.parts` read of it would be reported. There is none today, and
  the repair (`.relative_to(<that root>)`) is the same one the rule asks for anyway;
* a walk whose skip is delegated to a **helper** is out of reach: `#2024`'s fix moved the
  decision into `_is_vendored(p, root)`, and no reading of the call site can tell whether that
  helper's root argument is the one the path came from. The helper is where the fix lives, and
  this file's tree leg is green on it;
* the sweep covers the first-party trees. A walk in `emrg/gui/` reads a different toolchain and
  is not swept.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The trees whose walks read this checkout.
SWEPT = ("emrg", "scripts", "tests", "packaging")

#: Directories that hold no first-party source. The test walks this checkout, so its own skip
#: reads the path relative to the root -- the rule this file exists for, applied to itself.
SKIPPED = {".venv", "__pycache__", "node_modules"}

#: The methods that produce a walk. Both yield paths under their receiver, which is why a
#: result of one is an absolute path here.
WALK_METHODS = ("rglob", "glob")


def _as_walk_call(node: ast.AST) -> ast.Call | None:
    """`node` as a `.rglob(...)` / `.glob(...)` call, or `None`."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in WALK_METHODS
    ):
        return node
    return None


def _provably_relative(call: ast.Call) -> bool:
    """Is this walk's receiver a literal relative path -- `Path("tests")`?

    Only the literal spelling is decidable: a relative root reached through a name is
    indistinguishable from `REPO_ROOT`, and this file resolves the doubt toward reporting.
    """
    receiver = call.func.value  # type: ignore[attr-defined]
    if (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Name)
        and receiver.func.id == "Path"
        and len(receiver.args) == 1
        and not receiver.keywords
    ):
        receiver = receiver.args[0]
    return (
        isinstance(receiver, ast.Constant)
        and isinstance(receiver.value, str)
        and not receiver.value.startswith(("/", "~"))
    )


def _names_bound(node: ast.AST) -> list[str]:
    """The plain names a target binds -- `p`, or both of `p, q`."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [element.id for element in node.elts if isinstance(element, ast.Name)]
    return []


def _spreads_a_walk(node: ast.AST, names: set[str]) -> bool:
    """Does this expression carry a walk, or a name already known to hold one?

    The walk stops at a `.relative_to(...)` rendering and does not descend into it: that call
    turns an absolute path into a relative one, which is the repair this file exists to ask
    for, so a name bound to it is **not** a name holding a walk. `check-doc-count.py`'s
    `relative = path.relative_to(REPO_ROOT)` followed by `relative.parts` is that shape, and a
    detector that propagated through it would report one of the correct renderers.
    """
    stack = [node]
    while stack:
        inner = stack.pop()
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "relative_to"
        ):
            continue
        call = _as_walk_call(inner)
        if call is not None and not _provably_relative(call):
            return True
        if isinstance(inner, ast.Name) and inner.id in names:
            return True
        stack.extend(ast.iter_child_nodes(inner))
    return False


def walk_names(tree: ast.Module) -> set[str]:
    """Every name a walk result reaches the body by, to a small fixpoint.

    Loop targets, comprehension targets, and assignment targets whose value carries a walk
    -- repeated, because `found = sorted(root.rglob(...))` makes `found` a walk expression
    for the `for p in found` that consumes it.
    """
    names: set[str] = set()
    for _ in range(len(list(ast.walk(tree))) + 1):
        before = len(names)
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.AsyncFor)):
                if _spreads_a_walk(node.iter, names):
                    names.update(_names_bound(node.target))
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                for generator in node.generators:
                    if _spreads_a_walk(generator.iter, names):
                        names.update(_names_bound(generator.target))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                if _spreads_a_walk(node.value, names):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        names.update(_names_bound(target))
        if len(names) == before:
            break
    return names


def _parts_of_a_walk_name(node: ast.AST, names: set[str]) -> str | None:
    """The name this `.parts` belongs to, if it is a walk result -- `p`, or `Path(p)`."""
    if not isinstance(node, ast.Attribute) or node.attr != "parts":
        return None
    value = node.value
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "Path"
        and len(value.args) == 1
        and not value.keywords
    ):
        value = value.args[0]
    if isinstance(value, ast.Name) and value.id in names:
        return value.id
    return None


def offenders(source: str) -> list[tuple[int, str]]:
    """`(line, name)` for every `.parts` read of a walk result in `source`."""
    tree = ast.parse(source)
    names = walk_names(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        name = _parts_of_a_walk_name(node, names)
        if name is not None:
            found.append((node.lineno, name))
    return sorted(set(found))


class TestTheDetector:
    """Both directions, on sources small enough to read."""

    def test_the_loop_form_is_reported(self) -> None:
        assert offenders('for p in root.rglob("*.py"):\n    if S & set(p.parts):\n        continue\n')

    def test_the_comprehension_form_is_reported(self) -> None:
        assert offenders('[p for p in root.rglob("*.py") if S & set(p.parts)]\n')

    def test_a_name_assigned_from_a_walk_is_reported_when_consumed(self) -> None:
        source = 'found = sorted(root.rglob("*.py"))\nfor p in found:\n    S & set(p.parts)\n'

        assert [line for line, _ in offenders(source)] == [3]

    def test_the_path_wrapper_is_reported(self) -> None:
        assert offenders('for p in root.rglob("*"):\n    S & set(Path(p).parts)\n')

    def test_the_generator_form_is_reported(self) -> None:
        assert offenders('for p in root.rglob("*"):\n    any(part in S for part in p.parts)\n')

    def test_the_relative_rendering_is_silent(self) -> None:
        assert offenders('for p in root.rglob("*.py"):\n    S & set(p.relative_to(root).parts)\n') == []

    def test_a_name_that_is_not_a_walk_is_silent(self) -> None:
        """The control that keeps the rule about walks rather than about `.parts`.

        `git ls-files` emits `/`-separated names on every platform, so `Path(rel).parts` over
        a tracked-name list is not a tree walk and must not be reported. A rule keyed on
        `.parts` alone would report it, and a rule that cannot tell the two apart is one
        nobody would keep.
        """
        source = (
            "def is_vendored(rel: str) -> bool:\n"
            "    return any(part in S for part in Path(rel).parts)\n"
        )

        assert offenders(source) == []

    def test_a_walk_on_a_relative_literal_root_is_silent(self) -> None:
        assert offenders('for p in Path("tests").glob("test_*.py"):\n    S & set(p.parts)\n') == []


class TestThisTree:
    def test_no_walk_reads_an_absolute_path(self) -> None:
        """The rule, over this checkout."""
        found: list[str] = []
        judged = 0
        for directory in SWEPT:
            base = REPO_ROOT / directory
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.py")):
                if SKIPPED & set(path.relative_to(REPO_ROOT).parts):
                    continue
                judged += 1
                try:
                    source = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for line, name in offenders(source):
                    found.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line}: {name}.parts")

        assert judged > 100, (
            f"only {judged} file(s) were judged: a sweep over an empty set is a clean verdict "
            "about nothing, and that is the one reading this file must not give"
        )
        assert not found, (
            "a walk rooted at an absolute path yields absolute paths, so `path.parts` decides "
            "from where the machine put the checkout -- any ancestor named `node_modules`, "
            "`.venv` or `__pycache__` silences the walk and the test reports a clean reading of "
            "nothing. Read the path relative to the root it was walked from: "
            + ", ".join(found)
        )
