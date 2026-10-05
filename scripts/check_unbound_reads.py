#!/usr/bin/env python3
"""CI check: a name read before its first binding in the same function scope.

The defect this exists for
--------------------------
Python decides a name's scope per *function*, at compile time: a name assigned
anywhere in a function body is that function's local, wherever the assignment
sits. So a function that reads a name above the assignment that creates it does
not read the enclosing scope -- it raises

    UnboundLocalError: cannot access local variable 'x' where it is not
    associated with a value

on that line, every time the line is reached.

That shipped: v0.3.6's TUI crashed on *every* Enter because `handle_key` (nested
in `interactive`) read and rebound `_approval_pending` without declaring it
`nonlocal` (issue #1759). Two more instances of the same class were in the same
function (`was_busy`) and in the daemon's tool loop (`full_content`), all three
found by this rule.

Why it is not `check_nonlocal.py`'s rule
----------------------------------------
`check_nonlocal.py` asks a narrower question: a name written in one of
`interactive`'s nested functions that is *also assigned at the top level of*
`interactive`. `was_busy` is assigned in `handle_key` only, so that guard could
never have seen it, and neither guard covers the daemon. This one asks the
question the defect is actually made of -- "is this name read before anything
in this scope binds it" -- of every function in the first-party tree.

What it does not claim
----------------------
It compares *source order*, not reachability. A read that some earlier branch
always guards is still reported: the rule is a conservative approximation of
"can be unbound here", not a dataflow proof. Paths that only execute a read
after a loop has carried the binding around are therefore also reported, which
is right for a first iteration. Read inside a comprehension is attributed to the
enclosing function (good enough: a comprehension cannot bind the enclosing
scope's names) and its own target names are not treated as this scope's
bindings.

Exit codes: 0 clean, 1 findings, 2 unmeasurable (a tree that will not parse).
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

#: Directories the scan covers, relative to the tree root. First-party source
#: only: `tests/` is included because a test that crashes on collection is the
#: same defect, and `packaging/` because its modules ship in the installer.
SCANNED_DIRS = ("emrg", "scripts", "tests", "packaging")

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

FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)

#: A name occurring in one of these contexts is *bound* by the statement, not
#: read: `Store` is an assignment target, `Del` a deletion.
BINDING_CONTEXTS = (ast.Store, ast.Del)

#: Comprehensions and generator expressions have their own scope in Python 3, so
#: their target names are not bindings of the enclosing function -- and recording
#: them as such would flag a perfectly good `print(i)` above `[i for i in xs]`.
COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _exempt_inside_comprehension(node: ast.AST) -> set[str]:
    """Names a comprehension binds whose binding this scope cannot be reasoned about.

    A dict comprehension runs its `if` clause before the key/value pair, so

        {name: gone for name, text in texts.items() if (gone := f(text))}

    binds `gone` *before* the pair that reads it -- while the AST puts the read on
    the earlier line. Source order is the wrong instrument here, so the name is
    exempted rather than guessed at (measured on this repository: two tests use
    exactly this shape, and both are correct).
    """
    exempt: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.NamedExpr) and isinstance(inner.target, ast.Name):
            exempt.add(inner.target.id)
    return exempt


def _resolve_root() -> Path:
    """The tree to inspect: the checkout the caller is *standing in*, else our own.

    Derived from the cwd when that is a checkout, not from `__file__` alone, for
    the reason `check_nonlocal.py` records at length: the guard is used from git
    worktrees, where a script-root rule inspects the main checkout and prints a
    confident verdict about a tree the caller is not looking at.
    """
    here = Path(__file__).resolve().parent.parent
    cwd = Path.cwd()
    if (cwd / "emrg").is_dir() and (cwd / "scripts").is_dir():
        return cwd
    return here


class _Scope:
    """One function's own scope: its bindings and its reads, by first line."""

    def __init__(self) -> None:
        self.first_binding: dict[str, int] = {}
        self.first_read: dict[str, int] = {}

    def bind(self, name: str, line: int) -> None:
        current = self.first_binding.get(name)
        if current is None or line < current:
            self.first_binding[name] = line

    def read(self, name: str, line: int) -> None:
        current = self.first_read.get(name)
        if current is None or line < current:
            self.first_read[name] = line

    def findings(self, not_this_scope: set[str]) -> list[tuple[str, int, int]]:
        """(name, read line, binding line) for reads that precede the binding."""
        out = []
        for name, read_line in self.first_read.items():
            if name in not_this_scope:
                continue
            bind_line = self.first_binding.get(name)
            if bind_line is None:
                # Never bound here: a global, a builtin, a closure variable of an
                # enclosing function. Not this rule's question.
                continue
            if bind_line > read_line:
                out.append((name, read_line, bind_line))
        return sorted(out, key=lambda item: item[1])


def _collect(node: ast.AST, scope: _Scope, parameters: set[str]) -> set[str]:
    """Walk `node`, filling `scope`; return names whose bindings are not this scope's.

    Recursion stops at a nested function definition (its body has its own scope,
    analysed separately) and at a comprehension/generator (its targets are not
    this scope's bindings, but the names it *reads* still belong to it).
    """
    foreign: set[str] = set()
    stack: list[ast.AST] = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        if isinstance(current, FUNCTION_NODES):
            continue
        if isinstance(current, ast.Lambda):
            # A nested scope like any other: its parameters are its own, so a
            # `key=lambda r: r.get(...)` must not read as this scope reading `r`
            # (measured on `rants.py` and `check-vote-count.py`, both correct
            # code). Its body still reads this scope's names, so descend.
            for arg in (*current.args.posonlyargs, *current.args.args, *current.args.kwonlyargs):
                foreign.add(arg.arg)
            if current.args.vararg:
                foreign.add(current.args.vararg.arg)
            if current.args.kwarg:
                foreign.add(current.args.kwarg.arg)
            stack.extend(ast.iter_child_nodes(current))
            continue
        if isinstance(current, COMPREHENSION_NODES):
            # Its iterable and element expressions read in this scope; its target
            # names do not bind here. Descend, but exempt the targets.
            for generator in current.generators:  # type: ignore[attr-defined]
                for target in ast.walk(generator.target):
                    if isinstance(target, ast.Name):
                        foreign.add(target.id)
            foreign |= _exempt_inside_comprehension(current)
            stack.extend(ast.iter_child_nodes(current))
            continue
        if isinstance(current, ast.Name):
            if isinstance(current.ctx, BINDING_CONTEXTS):
                scope.bind(current.id, current.lineno)
            else:
                scope.read(current.id, current.lineno)
        elif isinstance(current, (ast.Import, ast.ImportFrom)):
            for alias in current.names:
                bound = (alias.asname or alias.name).split(".")[0]
                scope.bind(bound, current.lineno)
        elif isinstance(current, ast.ExceptHandler) and current.name:
            # `except E as exc:` binds `exc` at the handler line. The name is a
            # string in the ASDL, not a Name node, so it needs this branch.
            scope.bind(current.name, current.lineno)
        stack.extend(ast.iter_child_nodes(current))
    return foreign


def _parameters(node: ast.AST) -> set[str]:
    args = node.args  # type: ignore[attr-defined]
    names = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _declared(node: ast.AST) -> set[str]:
    """`global`/`nonlocal` names: bound elsewhere by declaration, so asks no question."""
    names: set[str] = set()
    stack = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        if isinstance(current, FUNCTION_NODES):
            continue
        if isinstance(current, (ast.Global, ast.Nonlocal)):
            names.update(current.names)
        stack.extend(ast.iter_child_nodes(current))
    return names


def _function_name(node: ast.AST) -> str:
    return getattr(node, "name", "<lambda>")


def check_file(path: Path) -> list[tuple[str, int, int, str]]:
    """Findings for one file: (name, read line, binding line, function name)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings: list[tuple[str, int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, FUNCTION_NODES):
            continue
        scope = _Scope()
        foreign = _collect(node, scope, _parameters(node))
        exempt = foreign | _parameters(node) | _declared(node)
        for name, read_line, bind_line in scope.findings(exempt):
            findings.append((name, read_line, bind_line, _function_name(node)))
    return findings


def scanned_modules(root: Path) -> list[Path]:
    """The first-party modules a verdict about this tree is a statement over.

    One home for the question, because two readers ask it: `scan` walks exactly
    these files, and `main` asks whether the walk had any subject at all. The
    note below is why the second reader exists — a walk that read nothing is
    "a clean verdict about nothing, which is the one failure mode this file
    must not have", and *no module found* is that same failure with a different
    cause from the whole-tree skip the note records.
    """
    out: list[Path] = []
    for directory in SCANNED_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            # Judged on the path *relative to the root*, never on the absolute
            # path: a tree under `.emrg/session/.../tmp/` (a worktree, the shape
            # this guard was first pointed at) has `.emrg` in its absolute parts
            # and was skipped whole -- a clean verdict about nothing, which is
            # the one failure mode this file must not have.
            if any(part in SKIPPED_DIRS for part in path.relative_to(root).parts):
                continue
            out.append(path)
    return out


def scan(root: Path) -> tuple[list[str], list[str]]:
    """(findings as lines, files that could not be measured)."""
    findings: list[str] = []
    unmeasured: list[str] = []
    for path in scanned_modules(root):
        try:
            file_findings = check_file(path)
        except SyntaxError as exc:
            unmeasured.append(f"{path}: {exc}")
            continue
        for name, read_line, bind_line, function in file_findings:
            findings.append(
                f"{path.relative_to(root)}:{read_line}  in {function}()  reads "
                f"{name!r} before its first binding at {bind_line}"
            )
    return findings, unmeasured


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
    # This report prints a `tree:` line and also writes findings to stderr, so a
    # piped reader can see the verdict first (the family's rule: an unflushed
    # stdout block would otherwise let the stderr half overtake the verdict).
    sys.stdout.reconfigure(line_buffering=True)
    print(f"tree: {root}")

    findings, unmeasured = scan(root)

    for line in findings:
        print(f"ERROR: {line}")
    for line in unmeasured:
        print(f"UNMEASURABLE: cannot parse {line}", file=sys.stderr)

    if findings:
        print(
            f"ERROR: {len(findings)} name(s) read before their first binding in the "
            "same function scope - each is an UnboundLocalError waiting for its line "
            "to be reached. Bind the name before the read (an assignment above it, a "
            "parameter, or `nonlocal`/`global` when it belongs to the enclosing scope)."
        )
        return 1
    if unmeasured:
        print(
            f"UNMEASURABLE: {len(unmeasured)} file(s) could not be parsed, so this is "
            "not a clean reading of the tree.",
            file=sys.stderr,
        )
        return 2
    # ...but only over a tree that had something to read. `scan` reports a file it could
    # not *parse*; it cannot report the case where it found no first-party module at all,
    # and "OK: no name is read before its first binding" over a tree with no module in it
    # is the file's own named failure mode — "a clean verdict about nothing, which is the
    # one failure mode this file must not have" (the note in `scanned_modules`). Measured
    # 2026-10-05 (`cyc20261005-145352`) on the master this is based on: an empty directory,
    # and one holding a single unrelated file, both answered `OK` with exit 0. Asked here,
    # before `--quiet`, because a flag that suppresses the *clean* line must not suppress
    # the refusal that replaces it.
    if not scanned_modules(root):
        print(
            f"could not measure: no first-party module under {root} "
            f"({'/, '.join(SCANNED_DIRS)}) - the clean verdict is a statement over "
            "the modules read, and none was read here",
            file=sys.stderr,
        )
        return 2
    if not args.quiet:
        print("OK: no name is read before its first binding in its own function scope.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
