#!/usr/bin/env python3
"""CI check: ensure all nonlocal declarations in TUI nested functions are complete.

Rant #31: "put selector variables into a SelectorState class… also add a CI step
to run a nonlocal integrity check."

Uses AST to parse emrg/client/app.py, verifying that every variable assigned in
the `interactive` function and also accessed in its `handle_key` inner function
has a corresponding `nonlocal` declaration.  Catches bugs where a developer adds
a new state variable in interactive but forgets to declare it nonlocal in
handle_key — exactly the class of bug that the SelectorState refactoring (#124)
is designed to prevent.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _assigned_names(node: ast.AST) -> set[str]:
    """Collect all names assigned in a subtree (including nested functions)."""
    names: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def _collect_from(self, node: ast.AST) -> None:
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, (ast.Tuple, ast.List)):
                for elt in node.elts:
                    self._collect_from(elt)

        def visit_Assign(self, n: ast.Assign) -> None:
            for t in n.targets:
                self._collect_from(t)
            self.generic_visit(n)

        def visit_AnnAssign(self, n: ast.AnnAssign) -> None:
            if isinstance(n.target, ast.Name):
                names.add(n.target.id)
            self.generic_visit(n)

        def visit_AugAssign(self, n: ast.AugAssign) -> None:
            if isinstance(n.target, ast.Name):
                names.add(n.target.id)
            self.generic_visit(n)

        def visit_NamedExpr(self, n: ast.NamedExpr) -> None:
            if isinstance(n.target, ast.Name):
                names.add(n.target.id)
            self.generic_visit(n)

        # Don't recurse into nested function definitions — their locals aren't
        # visible to the enclosing function's nonlocal checks.
        def visit_FunctionDef(self, n: ast.FunctionDef) -> None:
            pass

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    Visitor().visit(node)
    return names


def _written_names(node: ast.AST) -> set[str]:
    """Collect all names *written* (Store/Del context) in a subtree.

    Excludes names that are local parameters or nonlocal declarations
    (those are intentionally bound locally).
    """
    writes: set[str] = set()
    bound_locally: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_Name(self, n: ast.Name) -> None:
            if isinstance(n.ctx, (ast.Store, ast.Del)):
                writes.add(n.id)

        def visit_arg(self, n: ast.arg) -> None:
            bound_locally.add(n.arg)

        def visit_Nonlocal(self, n: ast.Nonlocal) -> None:
            for name in n.names:
                bound_locally.add(name)

    Visitor().visit(node)
    return writes - bound_locally


def _find_interactive_body(tree: ast.AST) -> ast.FunctionDef | None:
    """Find the top-level `interactive` async function in the module."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "interactive":
                return node
    return None


def _find_inner_fn(body: list[ast.stmt], name: str) -> ast.FunctionDef | None:
    """Find a nested function `name` inside a function body (one level deep)."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    return None


def check_nonlocal(app_path: str) -> int:
    src = Path(app_path).read_text(encoding="utf-8")
    tree = ast.parse(src)

    interactive_fn = _find_interactive_body(tree)
    if interactive_fn is None:
        print("ERROR: could not find `interactive` function in app.py", file=sys.stderr)
        return 2

    # Collect names assigned at the top level of interactive's body
    # (but NOT inside its nested functions — those have their own scopes)
    interactive_assigned: set[str] = set()
    for stmt in interactive_fn.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # skip nested functions — their locals are separate scopes
        interactive_assigned |= _assigned_names(stmt)

    # Collect nonlocal names from each inner function
    handle_key = _find_inner_fn(interactive_fn.body, "handle_key")
    read_server = _find_inner_fn(interactive_fn.body, "read_server")
    _on_sigwinch = _find_inner_fn(interactive_fn.body, "_on_sigwinch")
    _run_elapsed_timer = _find_inner_fn(interactive_fn.body, "_run_elapsed_timer")

    inner_fns = []
    if handle_key:
        inner_fns.append(("handle_key", handle_key))
    if read_server:
        inner_fns.append(("read_server", read_server))
    if _on_sigwinch:
        inner_fns.append(("_on_sigwinch", _on_sigwinch))
    if _run_elapsed_timer:
        inner_fns.append(("_run_elapsed_timer", _run_elapsed_timer))

    exit_code = 0
    for fn_name, fn_node in inner_fns:
        nonlocal_names: set[str] = set()
        for stmt in fn_node.body:
            if isinstance(stmt, ast.Nonlocal):
                nonlocal_names.update(stmt.names)

        written = _written_names(fn_node)

        # Exclude names that are purely local temporaries in the inner
        # function (e.g. data/json parsing results, line for readline).
        # These don't need nonlocal because they don't share state with
        # the outer scope — the inner function creates its own local.
        _LOCAL_TEMPS: dict[str, set[str]] = {
            "read_server": {"data", "line", "text"},
        }
        exclude = _LOCAL_TEMPS.get(fn_name, set())
        written -= exclude

        # Variables that are *written* (assigned) in the inner fn and also
        # assigned in interactive's scope — if not declared nonlocal, Python
        # treats them as local, causing UnboundLocalError at runtime.
        missing = (written & interactive_assigned) - nonlocal_names

        if missing:
            print(
                f"ERROR: {fn_name}() accesses nonlocal variables missing from "
                f"`nonlocal` declaration: {sorted(missing)}",
                file=sys.stderr,
            )
            print(
                f"  Add to nonlocal declaration: {', '.join(sorted(missing))}",
                file=sys.stderr,
            )
            exit_code = 1

    if exit_code == 0:
        print("✅ nonlocal integrity check passed")

    return exit_code


if __name__ == "__main__":
    app_path = Path(__file__).resolve().parent.parent / "emrg" / "client" / "app.py"
    if not app_path.exists():
        print(f"ERROR: app.py not found at {app_path}", file=sys.stderr)
        sys.exit(2)
    sys.exit(check_nonlocal(str(app_path)))
