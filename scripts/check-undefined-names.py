#!/usr/bin/env python3
"""CI check: a name read in a scope that nothing in the module binds.

The defect this exists for
--------------------------
A name that is bound **nowhere** is invisible to every leg of this repository's
verification. Measured 2026-10-09: an interrupted run left `emrg/client/app.py`
calling `new_task_id()` with no `def` and no import anywhere in the repo -- a
`NameError` on every Enter -- while

* `uv run pytest tests/` reported **4401 passed, 14 skipped**,
* `uv run python -c "from emrg.client.app import run_client"` passed,
* `uv run python -m emrg --help` passed,
* `scripts/check_unbound_reads.py` reported `OK`, and
* both CI legs reported nothing at all, because this repository has no ruff,
  pyflakes, mypy or flake8 in its workflow or in `pyproject.toml`.

Why neither sibling guard sees it
---------------------------------
`check_unbound_reads.py` asks a name's scope question for one function: *is this
name read before the first binding in its own scope*. A name bound nowhere has no
such first binding, so that rule passes it by construction. `check_nonlocal.py`
asks a third thing again -- whether a nested function writes a name its enclosing
function also assigns. The question left over, and the one the defect is made of,
is *"does anything in this module bind this name at all?"*

Its first run found a live instance
-----------------------------------
`emrg/server/daemon.py:552` is `config = sandbox_config or SandboxConfig()`, and
line 39's `from emrg.config import (...)` list does not carry `SandboxConfig`:

    $ uv run python -c "from emrg.server.daemon import build_shell_tool; build_shell_tool()"
    NameError: name 'SandboxConfig' is not defined

It was latent only because the one production call site always passes a config, so
the default branch the docstring advertises was unreachable by accident rather than
by contract.

What it does not claim
----------------------
It reads *binding*, not reachability: a name bound nowhere is reported wherever it
is read, whether or not that line ever runs. That is deliberately the same boundary
as `check_unbound_reads.py`'s -- the conservative approximation is what makes the
rule decidable at all -- and it is why the fix for a finding may be a definition, an
import, a parameter, or a move of the read.

The scoping decision is `symtable`'s, not this file's: comprehensions, walrus
targets, closure variables, class bodies and `global` declarations are all handled
by the interpreter's own rule set. The AST is consulted only to name the line a
finding is on, and a load inside a table `symtable` reports separately from the code
that reads it (an inlined comprehension on 3.12+, a generator expression's own
frame) falls back to the first line in the file that reads the name.

Exit codes: 0 clean, 1 findings, 2 unmeasurable - a root that is not a directory, a
tree that will not parse, or a root the scan read no file from. The third is not a
detail: `0` means "measured and clean", and a scan that read nothing has measured
nothing. A file with `from x import *` is *not* unmeasurable: that module's names
cannot be enumerated, so the file is not judged and the count is printed.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import symtable
import sys
from pathlib import Path

#: Directories the scan covers, relative to the tree root -- the same first-party
#: set `check_unbound_reads.py` reads, so a file that guard judges is judged here.
SCANNED_ROOTS = ("emrg", "scripts", "tests", "packaging")

#: Never descended into. `.emrg` matters most: it holds session scratch trees
#: (worktrees, checked-out copies) that are copies of this source, so scanning it
#: reports every finding twice or thrice under paths nobody is reviewing.
SKIPPED_DIRS = frozenset(
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

#: Names a module reads without binding them -- the interpreter's own, plus the
#: handouts a module or a package is given at import time. Without these, every
#: `print` and every `if __name__ == "__main__"` would be a finding.
IMPLICIT_NAMES = frozenset(dir(builtins)) | frozenset(
    {
        "__file__",
        "__name__",
        "__doc__",
        "__package__",
        "__spec__",
        "__loader__",
        "__builtins__",
        "__annotations__",
        "__debug__",
        "__class__",
        "__module__",
        "__qualname__",
        "__dict__",
        "__path__",
    }
)

#: The name `symtable` gives the module-level table, which is the root of every
#: scope path this file builds.
MODULE_TABLE_NAME = "top"

#: Nodes that open a scope `symtable` reports as its own table. Comprehensions are
#: deliberately absent: a list/set/dict comprehension is inlined into its enclosing
#: scope on 3.12+, and on both versions a name read inside one is reported against
#: the innermost *named* scope, so treating those loads as the enclosing scope's is
#: the reading that matches. A generator expression does keep its own frame, and a
#: finding inside one lands on the fallback path described in the docstring.
SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


#: How many read lines a finding names before it summarises the rest. The lines are
#: what makes a finding actionable -- a postponed annotation reads a name without
#: evaluating it, so the first read is not always the line that would raise, and a
#: report naming only that one invites the reader to dismiss it.
MAX_LINES_NAMED = 3


class Finding:
    """One name read where nothing binds it: where, in which scope, and the name."""

    def __init__(self, path: Path, lines: list[int], scope: str, name: str) -> None:
        self.path = path
        self.lines = lines
        self.scope = scope
        self.name = name

    def render(self, root: Path) -> str:
        where = "at module level" if self.scope == MODULE_TABLE_NAME else f"in {self.scope}()"
        named = ", ".join(str(line) for line in self.lines[:MAX_LINES_NAMED])
        if len(self.lines) > MAX_LINES_NAMED:
            named += f" (and {len(self.lines) - MAX_LINES_NAMED} more)"
        return (
            f"{self.path.relative_to(root)}:{named}  {where}  reads {self.name!r} - "
            f"nothing in this module binds it"
        )


def _resolve_root() -> Path:
    """The tree to inspect: the checkout the caller is *standing in*, else our own.

    Derived from the cwd when that is a checkout, not from `__file__` alone, for the
    reason `check_nonlocal.py` records at length: the guard is used from git
    worktrees, where a script-root rule inspects the main checkout and prints a
    confident verdict about a tree the caller is not looking at.
    """
    here = Path(__file__).resolve().parent.parent
    cwd = Path.cwd()
    if (cwd / "emrg").is_dir() and (cwd / "scripts").is_dir():
        return cwd
    return here


def _has_star_import(tree: ast.Module) -> bool:
    """Whether any `from x import *` makes this module's name set unenumerable."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            return True
    return False


def _module_bindings(top: symtable.SymbolTable) -> set[str]:
    """Every name the module level binds: assignments, imports, defs and classes.

    This is the "at all" half of the question. A name bound only inside some
    function is *not* here -- which is what keeps the finding real when the read is
    in a different function that cannot see that binding.
    """
    return {
        symbol.get_name()
        for symbol in top.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }


def _scope_paths(
    table: symtable.SymbolTable, prefix: tuple[str, ...]
) -> dict[tuple[str, ...], symtable.SymbolTable]:
    """Every scope table under `table`, keyed by the path of names that reaches it."""
    path = prefix + (table.get_name(),)
    out = {path: table}
    for child in table.get_children():
        out.update(_scope_paths(child, path))
    return out


def _unbound_by_scope(
    top: symtable.SymbolTable, module_bound: set[str]
) -> dict[tuple[str, ...], set[str]]:
    """Names each scope reads and nothing visible binds, keyed by the scope's path.

    A name is skipped when *this* scope binds it (assignment, parameter, import,
    `def`/`class`, `except ... as`), when it is a closure variable of an enclosing
    function (`is_free`), when the writer declared it `global` (the declaration is
    itself the claim that the module provides it -- not this rule's question), when
    the module level binds it, or when the interpreter provides it. What is left is
    a read whose lookup can find nothing.
    """
    out: dict[tuple[str, ...], set[str]] = {}
    for path, table in _scope_paths(top, ()).items():
        names: set[str] = set()
        for symbol in table.get_symbols():
            name = symbol.get_name()
            if not symbol.is_referenced():
                continue
            if (
                symbol.is_assigned()
                or symbol.is_parameter()
                or symbol.is_imported()
                or symbol.is_namespace()
                or symbol.is_declared_global()
                or symbol.is_free()
            ):
                continue
            if name in module_bound or name in IMPLICIT_NAMES:
                continue
            names.add(name)
        if names:
            out[path] = names
    return out


def _load_lines(
    tree: ast.Module,
) -> tuple[dict[tuple[str, ...], dict[str, list[int]]], dict[str, list[int]]]:
    """Where each scope reads each name.

    Returns (per-scope read lines, module-wide read lines). The second is the
    fallback for a finding whose table the walk does not open (a generator
    expression's frame), and it exists so such a finding is still named by a line
    instead of being reported without one.
    """
    per_scope: dict[tuple[str, ...], dict[str, list[int]]] = {}
    everywhere: dict[str, list[int]] = {}

    def note(target: dict[str, list[int]], name: str, line: int) -> None:
        lines = target.setdefault(name, [])
        if line not in lines:
            lines.append(line)

    def walk(node: ast.AST, path: tuple[str, ...], names: dict[str, list[int]]) -> None:
        # The walk records what `node`'s *own* body reads and hands every scope node it
        # meets to a pass of its own, so a load is attributed to the innermost named
        # scope that can hold it -- the reading `symtable` reports findings against.
        stack = list(ast.iter_child_nodes(node))
        while stack:
            current = stack.pop()
            if isinstance(current, SCOPE_NODES):
                child_path = path + (getattr(current, "name", "lambda"),)
                child_names: dict[str, list[int]] = {}
                per_scope[child_path] = child_names
                walk(current, child_path, child_names)
                continue
            if isinstance(current, ast.Name) and isinstance(current.ctx, ast.Load):
                note(names, current.id, current.lineno)
                note(everywhere, current.id, current.lineno)
            stack.extend(ast.iter_child_nodes(current))

    root_names: dict[str, list[int]] = {}
    per_scope[(MODULE_TABLE_NAME,)] = root_names
    walk(tree, (MODULE_TABLE_NAME,), root_names)
    return per_scope, everywhere


def check_file(path: Path) -> tuple[list[Finding], bool]:
    """(findings, judged) for one file. `judged` is False for a star-import module."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    if _has_star_import(tree):
        return [], False
    top = symtable.symtable(source, str(path), "exec")
    unbound = _unbound_by_scope(top, _module_bindings(top))
    per_scope, everywhere = _load_lines(tree)
    findings: list[Finding] = []
    for scope_path, names in sorted(unbound.items()):
        lines_by_name = per_scope.get(scope_path, {})
        for name in sorted(names):
            lines = lines_by_name.get(name) or everywhere.get(name) or [0]
            findings.append(
                Finding(path, sorted(lines), scope_path[-1] if scope_path else "?", name)
            )
    return findings, True


def scan(root: Path) -> tuple[list[Finding], list[str], int, int]:
    """(findings, files that could not be measured, files judged, files not judged).

    The last two are what separate "nothing to report" from "nothing was judged": a tree
    whose every file is a star-import module is read and skipped, and its `OK:` line would
    be a verdict about no file at all.
    """
    findings: list[Finding] = []
    unmeasured: list[str] = []
    judged = 0
    skipped = 0
    for directory in SCANNED_ROOTS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            # Judged on the path *relative to the root*, never on the absolute path: a
            # tree under `.emrg/session/.../tmp/` (a worktree) has `.emrg` in its
            # absolute parts and would be skipped whole -- a clean verdict about
            # nothing, which is the one failure mode this file must not have.
            if any(part in SKIPPED_DIRS for part in path.relative_to(root).parts):
                continue
            try:
                file_findings, was_judged = check_file(path)
            except SyntaxError as exc:
                unmeasured.append(f"{path}: {exc}")
                continue
            if not was_judged:
                skipped += 1
                continue
            findings.extend(file_findings)
            judged += 1
    return findings, unmeasured, judged, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        help="the tree to inspect; defaults to the checkout the caller stands in "
        "(or the checkout this script lives in, when the cwd is not one)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing but the verdict line on a clean tree",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else _resolve_root()
    # This report prints a `tree:` line and also writes findings to stderr, so a piped
    # reader can see the verdict first (the family's rule: an unflushed stdout block
    # would otherwise let the stderr half overtake the verdict).
    sys.stdout.reconfigure(line_buffering=True)
    print(f"tree: {root}")

    if not root.is_dir():
        print(
            f"could not measure: {root} is not a directory, so no file was read - "
            "`0` says measured and clean, and this is neither",
            file=sys.stderr,
        )
        return 2

    findings, unmeasured, judged, skipped = scan(root)

    for finding in findings:
        print(f"ERROR: {finding.render(root)}")
    for line in unmeasured:
        print(f"UNMEASURABLE: cannot parse {line}", file=sys.stderr)

    if findings:
        print(
            f"ERROR: {len(findings)} name(s) are read where nothing in the module binds "
            "them - each is a NameError waiting for its line to be reached. Bind the "
            "name where it is read (a definition, an import, a parameter), or move the "
            "read to where it is bound."
        )
        return 1
    if unmeasured:
        print(
            f"UNMEASURABLE: {len(unmeasured)} file(s) could not be parsed, so this is "
            "not a clean reading of the tree.",
            file=sys.stderr,
        )
        return 2
    if judged == 0:
        if skipped:
            print(
                f"could not measure: {skipped} file(s) were read under {root} and none "
                "was judged - a star import makes a module's names unenumerable, so "
                "there is nothing to read a verdict from.",
                file=sys.stderr,
            )
        else:
            print(
                f"could not measure: no Python file was read under {root} (this guard "
                f"scans {', '.join(SCANNED_ROOTS)} below the root), so nothing was "
                "judged - a reading of nothing is not a clean reading.",
                file=sys.stderr,
            )
        return 2
    if not args.quiet:
        note = (
            f"; {skipped} file(s) not judged - a star import makes their names unenumerable"
            if skipped
            else ""
        )
        print(
            f"OK: no name is read that nothing in its module binds ({judged} file(s) judged{note})."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
