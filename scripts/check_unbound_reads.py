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
import builtins
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


def _collect(
    node: ast.AST,
    scope: _Scope,
    parameters: set[str],
    *,
    ignore: frozenset[int] = frozenset(),
) -> set[str]:
    """Walk `node`, filling `scope`; return names whose bindings are not this scope's.

    Recursion stops at a nested function definition (its body has its own scope,
    analysed separately) and at a comprehension/generator (its targets are not
    this scope's bindings, but the names it *reads* still belong to it).

    `ignore` holds ``id()``s of Name nodes to not count as reads at all. The
    second rule below is the only caller that passes anything: an annotation is
    not evaluated at runtime when the module carries ``from __future__ import
    annotations``, so a name appearing only there is not a read this guard can
    speak about (and the type-checking-import pattern depends on exactly that).
    """
    def _read(name: str, line: int, node: ast.AST) -> None:
        if id(node) not in ignore:
            scope.read(name, line)

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
                _read(current.id, current.lineno, current)
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
    """The phrase a finding uses for the scope it names, `()` included.

    A class body is named as a phrase rather than as a call, because that is the
    namespace the read resolves in -- `class C: a = 1; b = a` works, while a
    *method* body never sees the class namespace at all.
    """
    if isinstance(node, ast.ClassDef):
        return f"the body of class {node.name}"
    if isinstance(node, ast.Lambda):
        return "<lambda>()"
    return f"{node.name}()"


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


# ── the second rule: a name bound nowhere at all ────────────────────────────
#
# The rule above asks "is this name read before this scope binds it". Its own
# `findings` refuses the other half of the question in one line --
#
#     if bind_line is None:
#         # Never bound here: a global, a builtin, a closure variable of an
#         # enclosing function. Not this rule's question.
#
# -- and that is the point: a name bound *nowhere* is not "not yet bound", it is
# a `NameError` on the line that reads it, every time that line runs. The two
# halves want the same scope machinery and differ only in what counts as
# available, so they live here rather than in a second copy of the walker.
#
#
# What was measured
# -----------------
# `cyc20261003-065537`, on this checkout: one such name, in
# `emrg/server/daemon.py::build_shell_tool` --
#
#     config = sandbox_config or SandboxConfig()
#
# -- where `SandboxConfig` was never imported. Its docstring says "a missing one
# means the defaults", and the *annotation* two lines above is the same name, so
# the reading was plausible at every step; it raised `NameError` the first time
# anybody took the documented default. It shipped in b92ed00d (2026-09-23) and
# survived every suite, because production always passes the config explicitly
# and every test does too. `from __future__ import annotations` is what kept the
# annotation from raising as well, which is why the two are not symmetric and
# why the annotation half is only a read in a module that lacks it.
#
# The population was measured, not guessed: this rule, run over a clean
# `2ea0bbf9` tree (299 modules under `emrg/`, `scripts/`, `tests/`,
# `packaging/`), reports exactly this one line and no other -- and `rc=0` on the
# tree that imports the name. The reading is worth keeping because the defect was
# invisible in both directions it was looked at from: the module has always been
# imported and its own tests all pass a config in.
#
# Two candidate findings were measured and **rejected**, which is the other half
# of the population claim. Both were `Any` in `emrg/server/daemon.py`, read by two
# *local* annotations and bound nowhere -- and PEP 526 says a local annotation is
# never evaluated, so neither is a `NameError` (measured: with and without the
# future import, `def f(): x: NoSuchName = 1` runs). They are a checker's finding,
# not this rule's, and the import was added on that reading rather than the
# reading stretched to cover them: see `_annotation_ids` and the matrix below.
#
#
# What it does not claim
# ----------------------
# * A module with `from x import *` binds names this rule cannot enumerate, so
#   that file is reported *unmeasured*, never silently exempted.
# * Name-mangling, `exec`, `globals()[...]` and `setattr` are not modelled; none
#   of them can make a bare read resolve, so no finding turns on them.
# * A name read at *module* level (`print(UNDEFINED)` at the top of a file) is
#   not reported: this rule reads the scopes a `NameError` waits inside, the
#   function and class bodies, and the module body is where an unbound name dies
#   at import -- loudly, for whoever runs the file, rather than silently on a
#   path nothing has exercised.
# * A decorator, a default value and a *signature* annotation are evaluated where
#   the `def` statement sits, not inside the function, so a bare name there is not
#   a read this rule attributes to any scope it walks: it is deliberately silent
#   about `@UNDEFINED`, `def f(x=UNDEFINED)` and `def f() -> UNDEFINED`. That is
#   where this rule's boundary meets the `@x.setter` shape below -- the read is
#   real, but the namespace it resolves in is not one of the two this rule reads.
# * An *annotation* is a read only where Python evaluates it, and PEP 526 says
#   that depends on where it sits (measured on this checkout, all four rows):
#
#       module level  `x: T = 1`        evaluated   unless the module is lazy
#       class body    `class C: x: T`   evaluated   unless the module is lazy
#       local         `def f(): x: T`   **never**, lazy or not
#       signature     `def f(x: T) -> U` evaluated  unless the module is lazy
#
#   The local row is why `emrg/server/daemon.py` could read `Any` in two local
#   annotations without importing it (since #271) and still run: not a NameError
#   to report, but left alone it is a `typing.get_type_hints()` crash and a type
#   checker error, so the import was added rather than the reading explained away.

#: A read inside a nested lambda or comprehension is attributed to the enclosing
#: function (that is where its free names resolve), but a name such a nested scope
#: *binds itself* -- a lambda's parameters, a comprehension's targets -- is
#: exempted by name, which `_collect` already reports as `foreign`. Exempting by
#: name can only hide a finding, never invent one, and hiding is the direction
#: this half of the rule must err in: a guard that cries wolf is switched off.

#: Names Python puts in every module's namespace before its code runs.
_MODULE_INJECTED = frozenset(
    {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
     "__builtins__", "__debug__", "__dict__", "__annotations__"}
)

#: Every builtin a bare read can resolve to.
_BUILTIN_NAMES = frozenset(dir(builtins)) | _MODULE_INJECTED


def _bound_names(node: ast.AST) -> set[str]:
    """Every name *this* scope binds, not descending into nested scopes.

    Scope here is Python's: a `def`, a `class` or a `lambda` starts a new one and
    a comprehension has its own, so a name bound in a nested scope is not
    available to the outer one and must not count. Asked of the module for the
    global half of the reading, and of each enclosing function for the closure
    half.
    """
    names: set[str] = set()
    stack: list[ast.AST] = [node]
    while stack:
        current = stack.pop()
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(child.name)   # the definition binds its own name here
                continue                # …but its body is a scope of its own
            if isinstance(child, (ast.Lambda, *COMPREHENSION_NODES)):
                continue                # binds nothing in *this* scope
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    if alias.name != "*":
                        names.add((alias.asname or alias.name).split(".")[0])
                continue
            if isinstance(child, ast.Name):
                if isinstance(child.ctx, BINDING_CONTEXTS):
                    names.add(child.id)
                continue
            if isinstance(child, ast.ExceptHandler) and child.name:
                names.add(child.name)
            if isinstance(child, (ast.Global, ast.Nonlocal)):
                names.update(child.names)
            stack.append(child)
    return names


def _has_star_import(tree: ast.Module) -> bool:
    """`from x import *` anywhere in the module: its bindings cannot be listed.

    Only module-level star imports are legal Python, so a whole-tree walk is the
    right instrument and cannot miss one inside a function.
    """
    return any(
        isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)
        for node in ast.walk(tree)
    )


def _subtree_ids(roots: list[ast.AST]) -> set[int]:
    """`id()`s of every node in these subtrees, roots included."""
    ids: set[int] = set()
    for root in roots:
        for inner in ast.walk(root):
            ids.add(id(inner))
    return ids


def _evaluated_outside(node: ast.AST) -> set[int]:
    """Nodes of a `def`/lambda that Python evaluates in the *enclosing* scope.

    A decorator, a default value, a keyword default and an annotation are all
    evaluated where the `def` statement sits -- not inside the function -- so a
    name they read is not this scope's read. Measured on this checkout: the
    `@dirty.setter` and `@_tasks_file.setter` decorators read the property name
    from a **class body**, and attributing that read to the decorated function
    reported 22 findings, every one of them correct code.
    """
    roots: list[ast.AST] = list(getattr(node, "decorator_list", []) or [])
    arguments = getattr(node, "args", None)
    if arguments is not None:
        roots.extend(arguments.defaults)
        roots.extend(d for d in arguments.kw_defaults if d is not None)
        if getattr(node, "returns", None) is not None:
            roots.append(node.returns)
        for arg in (*arguments.posonlyargs, *arguments.args,
                    *arguments.kwonlyargs, arguments.vararg, arguments.kwarg):
            if arg is not None and arg.annotation is not None:
                roots.append(arg.annotation)
    return _subtree_ids(roots)


def _class_defs(node: ast.AST) -> list[ast.ClassDef]:
    """Every class definition *below* `node` -- `node` itself is never one of them.

    The exclusion is load-bearing: when `node` is itself a `ClassDef`, `ast.walk`
    yields it first, and the caller feeds this list into the ignore set -- so
    including it makes the class body ignore its own reads. Measured: the
    class-body rule reported nothing at all until this line, which a synthetic
    `class C: field = NoSuchName` caught.
    """
    return [inner for inner in ast.walk(node)
            if isinstance(inner, ast.ClassDef) and inner is not node]


def _annotation_ids(node: ast.AST) -> set[int]:
    """`id()`s of every node inside a *variable* annotation anywhere below `node`.

    Whether these are evaluated is the caller's question, and the answer depends
    on where the annotation sits -- PEP 526, measured on this checkout:

        module level  `x: T = 1`      evaluated        (unless the module is lazy)
        class body    `class C: x: T` evaluated        (unless the module is lazy)
        local         `def f(): x: T` **never evaluated**, lazy or not

    A *signature* annotation (`def f(x: T) -> U`) is not collected here: it is
    evaluated in the enclosing scope at `def` time, so it belongs to no scope
    this rule walks either way.
    """
    ids: set[int] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.AnnAssign):
            ids |= _subtree_ids([inner.annotation])
    return ids


def _enclosing_chains(tree: ast.Module) -> dict[int, list[ast.AST]]:
    """For every function/lambda node, the scopes that enclose it, outermost first.

    A `class` body is deliberately **not** in the chain: Python does not search
    it when a method reads a bare name, so a method that reads a class-level name
    gets a `NameError` and belongs in the findings.
    """
    chains: dict[int, list[ast.AST]] = {}

    def walk(node: ast.AST, chain: tuple[ast.AST, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                chains[id(child)] = list(chain)
                walk(child, chain + (child,))
            else:
                walk(child, chain)

    walk(tree, ())
    return chains


def _class_chains(tree: ast.Module) -> dict[int, list[ast.AST]]:
    """For every class definition, the **function** scopes that enclose it.

    Class bodies are not in a chain of their own (a method cannot see one), but a
    class body's own bare reads resolve through the functions around it, so those
    are what it needs.
    """
    chains: dict[int, list[ast.AST]] = {}

    def walk(node: ast.AST, chain: tuple[ast.AST, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                walk(child, chain + (child,))
            elif isinstance(child, ast.ClassDef):
                chains[id(child)] = list(chain)
                walk(child, chain)
            else:
                walk(child, chain)

    walk(tree, ())
    return chains


def undefined_reads(path: Path) -> tuple[list[tuple[str, int, str]], bool]:
    """Findings for one file: (name, read line, function) and whether it was measurable.

    The second return value is False only for a module with `from x import *`,
    where the set of names in scope cannot be enumerated: "could not measure" is
    reported as such and never as a clean reading (→ R2's rule for the queue
    readings, applied to a source guard).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if _has_star_import(tree):
        return [], False

    lazy = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(a.name == "annotations" for a in node.names)
        for node in ast.walk(tree)
    )

    module_names = _bound_names(tree)
    chains = _enclosing_chains(tree)
    class_chains = _class_chains(tree)
    findings: list[tuple[str, int, str]] = []

    def report(node: ast.AST, name: str, read_line: int) -> None:
        findings.append((name, read_line, _function_name(node)))

    # ── function and lambda bodies ──
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        available = set(module_names) | _BUILTIN_NAMES | _parameters(node)
        for enclosing in chains.get(id(node), []):
            available |= _bound_names(enclosing) | _parameters(enclosing)
        # A nested class body is a namespace of its own, analysed below; its
        # *header* (decorators, bases, keywords) is evaluated here, so those
        # stay out of both and are the one place this rule declines to look.
        # An annotation on a *local* variable is never evaluated -- PEP 526, and
        # measured -- so a name appearing only there is not a read, which is the
        # whole reason `emrg/server/daemon.py` has read `Any` without importing
        # it since #271 and run anyway.
        ignore = frozenset(
            _evaluated_outside(node)
            | _annotation_ids(node)
            | _subtree_ids(_class_defs(node))
        )
        scope = _Scope()
        shadowed = _collect(node, scope, _parameters(node), ignore=ignore)
        # This scope's own bindings count, from both readers because they see
        # different halves of the same scope: `_bound_names` has the names a
        # *definition* binds (a nested `def`/`class` the body calls by name,
        # which `_collect` deliberately does not bind), and `_collect` has what
        # a walrus inside a comprehension binds. And a read of a name this
        # function binds *later* is the first rule's finding -- reporting it here
        # too would make one defect look like two.
        available |= _bound_names(node) | set(scope.first_binding) | shadowed
        for name, read_line in scope.first_read.items():
            if name not in available:
                report(node, name, read_line)

    # ── class bodies ──
    #
    # A class body looks bare names up in its **own** namespace before the
    # enclosing function's (`class C: a = 1; b = a`), while a *method* body does
    # not see the class namespace at all. Both halves matter here: attributing
    # the class body's reads to the enclosing function invented findings (the
    # `@x.setter` shape above), and skipping class bodies entirely would miss
    # `class C: x = UNDEFINED`.
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        available = set(module_names) | _BUILTIN_NAMES
        for enclosing in class_chains.get(id(cls), []):
            available |= _bound_names(enclosing) | _parameters(enclosing)
        ignore = frozenset(
            _subtree_ids(list(cls.bases) + list(cls.keywords)
                         + list(cls.decorator_list)
                         + _class_defs(cls))
            # A class-body annotation is evaluated into `__annotations__` unless
            # the module is lazy -- the mirror image of the local case above.
            | (_annotation_ids(cls) if lazy else set())
        )
        scope = _Scope()
        shadowed = _collect(cls, scope, set(), ignore=ignore)
        available |= _bound_names(cls) | set(scope.first_binding) | shadowed
        for name, read_line in scope.first_read.items():
            if name not in available:
                report(cls, name, read_line)

    return sorted(findings, key=lambda item: item[1]), True


def scan(root: Path) -> tuple[list[str], list[str]]:
    """(findings as lines, files that could not be measured)."""
    findings: list[str] = []
    unmeasured: list[str] = []
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
            try:
                file_findings = check_file(path)
                undefined, definite = undefined_reads(path)
            except SyntaxError as exc:
                unmeasured.append(f"{path}: {exc}")
                continue
            if not definite:
                unmeasured.append(
                    f"{path}: a `from ... import *` makes the names in scope "
                    f"unenumerable, so this file was not measured for the "
                    f"bound-nowhere rule"
                )
            for name, read_line, bind_line, function in file_findings:
                findings.append(
                    f"UNBOUND-LATER: {path.relative_to(root)}:{read_line}  in "
                    f"{function}  reads {name!r} before its first binding at "
                    f"{bind_line}"
                )
            for name, read_line, function in undefined:
                findings.append(
                    f"BOUND-NOWHERE: {path.relative_to(root)}:{read_line}  in "
                    f"{function}  reads {name!r}, which nothing in this module "
                    f"binds - this line raises NameError whenever it runs"
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
        later = sum(1 for line in findings if line.startswith("UNBOUND-LATER"))
        nowhere = len(findings) - later
        if later:
            print(
                f"ERROR: {later} name(s) read before their first binding in the same "
                "function scope - each is an UnboundLocalError waiting for its line "
                "to be reached. Bind the name before the read (an assignment above "
                "it, a parameter, or `nonlocal`/`global` when it belongs to the "
                "enclosing scope)."
            )
        if nowhere:
            print(
                f"ERROR: {nowhere} name(s) bound nowhere in their module - each is a "
                "NameError on the line that reads it, every time that line runs. "
                "Import or define the name, or pass it in; a name that only a type "
                "annotation needs belongs behind `TYPE_CHECKING` with "
                "`from __future__ import annotations` in the module."
            )
        return 1
    if unmeasured:
        print(
            f"UNMEASURABLE: {len(unmeasured)} file(s) could not be parsed, so this is "
            "not a clean reading of the tree.",
            file=sys.stderr,
        )
        return 2
    if not args.quiet:
        print(
            "OK: no name is read before its first binding in its own function "
            "scope, and no name read inside a function or class body is bound "
            "nowhere in its module."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
