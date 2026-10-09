"""An annotation can name a name its module never binds — and nothing asked until now.

The reading, and the two it found
---------------------------------
The guards in `scripts/` ask questions of the *value* space — is a read bound
(`check-undefined-names.py`), is it bound before its own scope reads it
(`check_unbound_reads.py`), did a nested write declare it (`check_nonlocal.py`). None
asks about a **name written in an annotation**, and `from __future__ import annotations`
makes those inert at run time, so no test could see one either.

Measured on `503a332b` (2026-10-10, cycle `cyc20261010-021847`), which found two:

* `emrg/server/daemon.py` — `dict[str, Any]` in four annotations, and the module imports
  only `Optional` from `typing`;
* `tests/test_memory_index_thresholds.py:84` — `def _store(...) -> "MemoryStore":`, a
  forward reference to a base class the module never imports (it imports the two
  subclasses).

Both are inert today: `__future__` annotations are never evaluated, and a *string*
annotation never is either. They are still wrong readings of the code — the name is not
there — and anything that does resolve annotations says so out loud:

    >>> typing.get_type_hints(EmrgServer._collect_memory_data)
    NameError: name 'Any' is not defined

which is exactly what a type checker, an introspection-based framework or a doc tool
would hit. `pyflakes` finds the class (`undefined name 'Any'`), but this repository has
no linter by design, so nothing in CI does.

The question this file asks, and the one it does not
----------------------------------------------------
**Module-wide**, like `check-undefined-names.py`: a name is reported only when *no form
anywhere in that module* binds it. A name some other form binds — even in a scope the
annotation cannot see — is left alone, so the reading can cost a missed finding and
never a fabricated one.

That limit has a live instance, named so a later cycle does not read this file's green
as a stronger claim: `emrg/server/daemon.py`'s `_get_jinja_env() -> "jinja2.Environment"`
resolves to nothing at run time (jinja2 is imported *inside* the function), and
`typing.get_type_hints` raises on it — but `jinja2` **is** bound in that module, so this
rule is silent. Catching it is a **scope-aware** reading (the annotation resolves in the
enclosing namespace, not "somewhere in the module"), which is a different question, a
different tool's territory (`pyflakes`), and not what the family's siblings do.

The subject must be non-empty, and the reader must work in both directions
--------------------------------------------------------------------------
A scanner that finds nothing passes trivially, so the checkout test asserts the scan
read hundreds of files and that annotations really did name names — a reading of nothing
is not a clean reading. And the shape this rule looks for is built in `tmp_path` and
required to be *reported*, so a reader that has quietly stopped matching fails here
rather than reporting a clean tree.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: What a tree-reading guard must say before its verdict, and what it must not walk. Both
#: are the family's own values -- the same roots and the same skip set the three guards in
#: `scripts/` and `tests/test_no_dead_string_statement.py` declare -- because this file
#: judges the same tree they do: a root or a skip that drifts here would leave a file one
#: of them judges unjudged by this one, silently. `tests/test_guard_scan_scope_pairing.py`
#: pins the relation.
SCANNED_ROOTS = ("emrg", "scripts", "tests", "packaging")
SKIP_DIRS = frozenset(
    {
        ".git",
        ".emrg",
        ".venv",
        "venv",
        "node_modules",
        "build",
        "dist",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
BUILTIN_NAMES = frozenset(dir(builtins))


def _bound_anywhere(tree: ast.Module) -> set[str]:
    """Every name some form in this module binds, in any scope.

    Assignment and `AnnAssign` targets, walrus targets, comprehension and `for`
    targets, `with ... as`, `except ... as`, parameters, imports, `def`/`class` names,
    `global`/`nonlocal`, and `match` captures. Deliberately module-wide: see the
    docstring's "the one it does not".
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Global):
            names.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            names.update(node.names)
        elif isinstance(node, ast.MatchAs) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
    return names


def _annotations_of(node: ast.AST) -> list[ast.expr]:
    """Every annotation expression `node` carries — parameters, return, `AnnAssign`."""
    out: list[ast.expr] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = node.args
        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            out.append(arg.annotation)
        if args.vararg is not None:
            out.append(args.vararg.annotation)
        if args.kwarg is not None:
            out.append(args.kwarg.annotation)
        out.append(node.returns)
    elif isinstance(node, ast.AnnAssign):
        out.append(node.annotation)
    return [ann for ann in out if ann is not None]


def _names_in(annotation: ast.expr) -> set[str]:
    """The names an annotation expression refers to, string form parsed.

    A string annotation is the classic forward reference (`"MemoryStore"`), so the
    literal is parsed as an expression before its names are read; an unparsable one
    names nothing this rule can judge rather than failing the whole scan.
    """
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return set()
    return {node.id for node in ast.walk(annotation) if isinstance(node, ast.Name)}


def scan(root: Path) -> tuple[list[str], int, int, list[str]]:
    """Read `root`; return (findings, files read, annotation names read, unparsed)."""
    findings: list[str] = []
    files = 0
    reads = 0
    unparsed: list[str] = []
    for rel in SCANNED_ROOTS:
        base = root / rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIP_DIRS & set(path.relative_to(root).parts):
                continue
            files += 1
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            except (SyntaxError, UnicodeDecodeError) as exc:
                unparsed.append(f"{path.relative_to(root)}: {type(exc).__name__}")
                continue
            bound = _bound_anywhere(tree) | BUILTIN_NAMES
            for node in ast.walk(tree):
                for annotation in _annotations_of(node):
                    for name in sorted(_names_in(annotation)):
                        reads += 1
                        if name not in bound:
                            where = getattr(annotation, "lineno", 0) or getattr(node, "lineno", 0)
                            findings.append(
                                f"{path.relative_to(root)}:{where}: an annotation names "
                                f"{name!r}, which nothing in this module binds"
                            )
    return findings, files, reads, unparsed


def test_this_checkout_has_no_annotation_naming_a_name_nothing_binds() -> None:
    findings, files, reads, unparsed = scan(REPO_ROOT)
    assert unparsed == [], (
        f"file(s) could not be measured, which is not the same as clean: {unparsed}"
    )
    assert files > 100, (
        f"the scan read only {files} file(s) under {REPO_ROOT} - a reading of nothing "
        "is not a clean reading"
    )
    assert reads > 50, (
        f"only {reads} annotation name(s) were read, so an empty finding list proves "
        "nothing about this tree"
    )
    assert findings == [], "\n".join(findings)


def test_the_reader_reports_a_name_nothing_binds(tmp_path: Path) -> None:
    """The control: the shape must be *reported* here, or a clean tree proves nothing."""
    tree = tmp_path / "emrg"
    tree.mkdir()
    (tree / "bad.py").write_text(
        'def f() -> "Nope":\n'
        "    return 1\n"
        "\n"
        '\nmissing: "AlsoNope"\n'
        "\n"
        '\ndef g(x: "ThirdNope") -> int:\n'
        "    return x\n"
    )
    (tree / "good.py").write_text(
        "from typing import Any\n"
        "\n"
        "\ndef f() -> Any:\n"
        "    return 1\n"
        "\n"
        '\ndef g() -> "Any":\n'
        "    return 2\n"
        "\n"
        "\nclass C:\n"
        "    field: Any = None\n"
    )
    findings, files, reads, unparsed = scan(tmp_path)
    assert (files, unparsed) == (2, [])
    named = sorted(f.split("names ")[1].split(",")[0] for f in findings)
    assert named == ["'AlsoNope'", "'Nope'", "'ThirdNope'"], findings
    # `Any` is imported in `good.py`, and is named there in a plain, a string and a
    # class-body annotation - three forms, all silent.
    assert all("good.py" not in f for f in findings), findings
    assert reads == 7, reads
