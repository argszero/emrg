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
produced" is every shape a walk result reaches a predicate by **binding a name in the scope that
reads it**: a `for` target, a comprehension target, and a name assigned from a walk expression --
the statement spellings (`found = ...`, `found: list = ...`) and the expression one
(`if (found := ...):`), both of which are assignments -- to a small fixpoint, so
`found = sorted(root.rglob(...))` followed by `for p in found` is covered rather than left as the
one spelling that escapes. The binding is **scoped**, so a walk bound in
one function says nothing about a name reused in another, and a module-level or closure variable
is still visible to the function that reads it. The read is an **AST walk** rather than a text
match, so a predicate split across lines, written as a comprehension filter, or reached through
`Path(...)` is seen; and it keys on the `.parts` read rather than on the walk, which is what
keeps the `git ls-files` shape clean -- git emits `/`-separated names on every platform, so
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
* a walk whose result is **stored on an attribute** is out of reach for the same reason: the
  detector follows names a walk binds in the scope that reads them, and `self.found = list(...)`
  binds nothing in the method that later reads `self.found`. The two escapes are one door -- an
  indirection the detector does not follow -- and both close the same way, by reading the path
  relative to the root it was walked from at the point of use rather than narrowing the rule;
* the sweep covers the first-party trees. A walk in `emrg/gui/` reads a different toolchain and
  is not swept. Measured 2026-10-10: `emrg/gui/` holds **no Python at all** (`emrg/gui/**/*.py`
  matches nothing), so this exclusion conceals no site, and `SWEPT` is the whole first-party
  `.py` surface -- every top-level directory this checkout holds Python in is one of the four,
  with none outside them.
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


#: The node types that open a scope. A name a walk binds is visible to the scope that binds it
#: and to the scopes nested inside it, and to no other: `found = sorted(root.rglob(...))` in one
#: function says nothing about a `p` a different function bound for its own purposes.
SCOPES = (
    ast.Module,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _scopes(tree: ast.AST) -> tuple[dict[int, tuple[ast.AST, ...]], list[ast.AST]]:
    """`(id(node) -> enclosing scopes, outermost first)` for every node, and every scope.

    The chain is what keeps the binding honest in both directions: a `.parts` read may use a name
    a walk bound in an **enclosing** scope, because a module-level or closure variable really is
    that walk's result; a name bound in a **sibling** scope says nothing about this one.
    """
    chains: dict[int, tuple[ast.AST, ...]] = {}
    found: list[ast.AST] = []

    def visit(node: ast.AST, chain: tuple[ast.AST, ...]) -> None:
        if isinstance(node, SCOPES):
            chain = chain + (node,)
            found.append(node)
        chains[id(node)] = chain
        for child in ast.iter_child_nodes(node):
            visit(child, chain)

    visit(tree, ())
    return chains, found


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


def walk_names(
    scope: ast.AST, chains: dict[int, tuple[ast.AST, ...]], inherited: frozenset[str] = frozenset()
) -> set[str]:
    """Every name a walk result reaches the body by **within `scope`**, to a small fixpoint.

    Loop targets, comprehension targets, and assignment targets whose value carries a walk
    -- repeated, because `found = sorted(root.rglob(...))` makes `found` a walk expression
    for the `for p in found` that consumes it. Only nodes this scope owns are read: a walk
    inside a nested function binds its name there, not here. `inherited` carries the names the
    enclosing scopes bound, so a module-level walk is still a walk inside the function that
    consumes it -- narrowing past that would turn a real site into a silent one.

    A walrus target is an assignment target like any other (`:=` is the assignment-expression
    operator), so `if (found := sorted(root.rglob(...))):` binds a walk name the same way
    `found = ...` does. Reading only `ast.Assign`/`ast.AnnAssign` left every walrus form silent
    while the rule above promised "a name assigned from a walk expression" -- measured
    2026-10-10 on the pre-walrus detector: four shapes (`if (found := ...)`, a call argument, an
    in-place rendering, an immediate index) all returned `[]`, beside `found = ...` reported.
    """
    nodes = [node for node in ast.walk(scope) if chains[id(node)][-1] is scope]
    names: set[str] = set(inherited)
    for _ in range(len(nodes) + 1):
        before = len(names)
        for node in nodes:
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
            elif isinstance(node, ast.NamedExpr) and _spreads_a_walk(node.value, names):
                names.update(_names_bound(node.target))
        if len(names) == before:
            break
    return names


def _parts_of_a_walk_name(node: ast.AST, names: set[str]) -> str | None:
    """The name this `.parts` belongs to, if it is a walk result -- `p`, `Path(p)` or `(p := ...)`."""
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
    # `(p := root.rglob("*")).parts` renders the walrus in place rather than binding a name
    # for a later line, so it is the same read as the two-line spelling and is unwrapped here
    # for the same reason `walk_names` binds the target.
    if isinstance(value, ast.NamedExpr):
        value = value.target
    if isinstance(value, ast.Name) and value.id in names:
        return value.id
    return None


def offenders(source: str) -> list[tuple[int, str]]:
    """`(line, name)` for every `.parts` read of a walk result in `source`."""
    tree = ast.parse(source)
    chains, scopes = _scopes(tree)
    # `scopes` is outermost-first, so every enclosing scope's binding is already known when a
    # scope is reached.
    bound: dict[int, set[str]] = {}
    for scope in scopes:
        inherited: set[str] = set()
        for enclosing in chains[id(scope)][:-1]:
            inherited |= bound[id(enclosing)]
        bound[id(scope)] = walk_names(scope, chains, frozenset(inherited))
    found: list[tuple[int, str]] = []
    for scope in scopes:
        visible: set[str] = set()
        for enclosing in chains[id(scope)]:
            visible |= bound[id(enclosing)]
        for node in ast.walk(scope):
            if chains[id(node)][-1] is not scope:
                continue
            name = _parts_of_a_walk_name(node, visible)
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

    def test_a_name_reused_in_another_function_is_silent(self) -> None:
        """The control that keeps the binding about scopes rather than about the whole module.

        A walk binds `p` in `scan`; `other` binds its own `p` to something that is not a walk.
        Reporting the second one because of the first is what a module-wide name union does, and
        it turns a legitimate tree red -- a false red is the cost that erodes a guard.
        """
        source = (
            "def scan(root):\n"
            '    for p in root.rglob("*.py"):\n'
            "        use(p)\n"
            "\n"
            "def other(paths):\n"
            "    for p in paths:\n"
            "        print(p.parts)\n"
        )

        assert offenders(source) == []

    def test_the_walrus_spellings_are_reported(self) -> None:
        """`:=` is an assignment, so its target is a walk name like any other.

        The rule's own enumeration promises "a name assigned from a walk expression", and the
        detector read only `ast.Assign`/`ast.AnnAssign` -- so every walrus form was silent while
        the statement spelling beside it was reported (measured 2026-10-10: each of the three
        walrus shapes below returned `[]`, the statement control `[(3, 'p')]`). They are one read
        reached three ways, plus the in-place rendering that needs no later line at all.
        """
        by_statement = (
            "def f(root):\n"
            '    found = sorted(root.rglob("*.py"))\n'
            "    return [p.parts for p in found]\n"
        )
        in_condition = (
            "def f(root):\n"
            '    if (found := sorted(root.rglob("*.py"))):\n'
            "        return [p.parts for p in found]\n"
        )
        in_argument = (
            "def f(root):\n"
            '    use(found := sorted(root.rglob("*.py")))\n'
            "    return [p.parts for p in found]\n"
        )
        rendered_in_place = 'def f(root):\n    return (p := root.rglob("*.py")).parts\n'

        assert [line for line, _ in offenders(by_statement)] == [3]
        assert [line for line, _ in offenders(in_condition)] == [3]
        assert [line for line, _ in offenders(in_argument)] == [3]
        assert [line for line, _ in offenders(rendered_in_place)] == [2]

    def test_a_walrus_over_something_that_is_not_a_walk_is_silent(self) -> None:
        """The other direction: binding a walrus must not report a name that holds no walk.

        Widening the binding shapes is only safe while `_spreads_a_walk` still decides *what* is
        bound -- a walrus over a computed value is not a walk, and a `.parts` read of it is
        legitimate (it is `git ls-files`' shape, not a tree walk).
        """
        source = (
            "def f(names):\n"
            "    if (found := sorted(names)):\n"
            "        return [p.parts for p in found]\n"
        )

        assert offenders(source) == []

    def test_a_walk_name_is_visible_to_a_nested_scope(self) -> None:
        """The other direction: binding must not become so local that it stops seeing out.

        A module-level walk's name is a real walk result inside every function below it, and a
        closure's is a real walk result inside the closure. Narrowing the report to the exact
        scope that bound the name would miss both -- a false negative is worse than a false red.
        """
        module_level = (
            'found = sorted(root.rglob("*.py"))\n'
            "\n"
            "def report():\n"
            "    for p in found:\n"
            "        print(p.parts)\n"
        )
        closure = (
            "def scan(root):\n"
            '    found = sorted(root.rglob("*.py"))\n'
            "    def report():\n"
            "        for p in found:\n"
            "            print(p.parts)\n"
        )

        assert offenders(module_level) == [(5, "p")]
        assert offenders(closure) == [(5, "p")]


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
