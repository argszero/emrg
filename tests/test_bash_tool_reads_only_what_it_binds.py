"""Every name read in a module is bound in it — measured with `ast`, not by running it.

`bash_tool.py` is being emptied into its successors one block at a time (P7, issue
#1675).  A block that leaves while a reader stays behind does not fail at import:
`_UNRESOLVED_VAR_RE` was read by `_cwd_left_workspace` only from an `ast`
analysis of the *other* module, so the extraction looked complete, and 152 tests
across five files failed at runtime instead — the defect is invisible to
`py_compile` and to any test that does not call the exact line.

So the reading is mechanical here: for every `Name` load, is the name bound in
the scope reading it — locally, at module level, or in the builtins?  The check is
deliberately *lexical* and shallow about scopes (a name stored anywhere inside a
function counts as local to it, which is what its own body would do at runtime
for every spelling that does not hit an unbound read first), so it may miss a
genuinely unbound read that a nested scope shadows — it never reports a read
that resolves, which is the direction a guard on a doomed file needs.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "emrg" / "tools" / "bash_tool.py"


def _imported(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Import):
        return {a.asname or a.name.split(".")[0] for a in node.names}
    if isinstance(node, ast.ImportFrom):
        return {a.asname or a.name for a in node.names}
    return set()


def _bound_in(body: list[ast.AST]) -> set[str]:
    """Every name bound anywhere in these statements, this level or deeper."""
    out: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                out.add(child.id)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(child.name)
            elif isinstance(child, ast.arg):
                out.add(child.arg)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                out.add(child.name)
            elif isinstance(child, ast.Global | ast.Nonlocal):
                out.update(child.names)
            out.update(_imported(child))
            visit(child)

    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(stmt.name)
        out.update(_imported(stmt))
        if isinstance(stmt, ast.Name) and isinstance(stmt.ctx, ast.Store):
            out.add(stmt.id)
        visit(stmt)
    return out


def _undefined_reads(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text())
    known = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__"} | _bound_in(tree.body)
    bad: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in known:
            bad.append((node.lineno, node.id))
    return bad


def test_the_legacy_scanner_reads_only_names_it_still_binds() -> None:
    """A block that left while a reader stayed behind named here, not by 152 tests."""
    assert TARGET.exists(), f"{TARGET} is gone — this guard has no subject and must go with it"
    bad = _undefined_reads(TARGET)
    assert bad == [], "reads with no binding: " + ", ".join(
        f"{name} (line {line})" for line, name in sorted(bad)
    )
