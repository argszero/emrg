#!/usr/bin/env python3
"""CI check: a text read that parses its content must name a decode error.

The rule
--------
Reading a state file can fail in three shapes, and they are **one class** because a
reader cannot tell them apart by asking "could I read it?":

* an `OSError` -- no such file, a directory where the file should be, a permission bit
* the format's own parse error -- `json.JSONDecodeError`, `yaml.YAMLError`,
  `tomllib.TOMLDecodeError`
* a `UnicodeDecodeError` -- bytes that are not valid UTF-8. It is a **`ValueError`,
  not an `OSError`**, so a tuple spelled `(OSError, json.JSONDecodeError)` -- which
  reads like "every way reading this file can fail" -- catches two of the three and
  lets the third escape.

The third shape is not a message difference, it is an **answer** difference: the same
call gets a returned default for two shapes and a raised exception for the third, and
every caller written against the default is wrong for exactly one byte.

Measured 2026-10-03 over three evolution cycles (`cyc20261003-211427`, `-213507`,
`-215322`): twenty sites in three spellings, found by three separate manual sweeps.
Twenty sites is not twenty mistakes, it is one missing rule -- so the rule is
mechanised here instead of being re-swept.

What it checks
--------------
Two halves of one rule, because the decode happens at the **read** in both:

* a `try` whose **direct** body performs a text read (`read_text`, or `open` in a read
  mode) **and** parses what it read (`loads` / `load` / `safe_load` / ...);
* a `try` whose direct body only performs that text read, parsing nothing at all.
  `read_text` is where `UnicodeDecodeError` is raised, so a reader that parses nothing
  decodes exactly as much as one that does.

The second half was added 2026-10-03 (`cyc20261003-222355`). The first version asked
only about readers that parse, and it reported a clean tree over **19** sites that read
a file's text under a handler naming no decode error and parse nothing -- a question
narrower than the rule it claimed to hold. Eleven of those were real readers; the other
eight are read with `open` in a binary or write mode, or with an `errors=` that makes
the decode unfailable, and telling those apart is most of what the code below does.

In both halves the handlers must together name a decode-covering exception:
`UnicodeDecodeError`, `UnicodeError`, `ValueError`, `Exception`, or a bare `except`.
Module-level constants are resolved by name across the tree, so a reader that catches
`emrg.read_errors.JSON_READ_ERRORS` (or `FILE_READ_ERRORS`, or `YAML_READ_ERRORS`,
or `CONFIG_READ_ERRORS`) counts as naming whatever that constant holds.

Only *direct* body calls count for the read, because the decode happens at the read:
a `read_text` inside a nested `try` is that `try`'s business, not this one's. A write
is not a read -- `open(path, "w")` truncates, it does not decode -- and neither is
`read_bytes`, which hands back bytes and decodes nothing.

Output: one `ERROR:` line per site, exit 1; a tree it could not parse is reported
`UNMEASURABLE` and exits 2 -- a question this check cannot answer is never a pass.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

#: Calls that read a file's content into the process.
READ_CALLS = {"read_text", "read_bytes", "open"}
#: Calls that interpret that content as a serialisation format.
PARSE_CALLS = {"loads", "load", "safe_load", "safe_load_all", "full_load"}
#: Mode characters that make `open` a write, which decodes nothing.
WRITE_MODE_CHARS = ("w", "a", "x", "+")
#: Exception names that cover a decode failure, one way or another.
DECODE_COVERING = {"UnicodeDecodeError", "UnicodeError", "ValueError", "Exception", "<bare>"}
#: Directories that are not product code.
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}


def _resolve_root() -> Path:
    """The tree to inspect: the checkout the caller is *standing in*, else our own.

    Derived from the cwd when that is a checkout, not from `__file__` alone, for the
    reason `check_unbound_reads.py` records: the guard is used from git worktrees,
    where a script-root rule inspects the main checkout and prints a confident verdict
    about a tree the caller is not looking at.
    """
    here = Path(__file__).resolve().parent.parent
    cwd = Path.cwd()
    if (cwd / "emrg").is_dir() and (cwd / "scripts").is_dir():
        return cwd
    return here


#: Nodes that are their own error-handling scope: a read inside one is that scope's
#: business, not the enclosing `try`'s.
_NESTED_SCOPES = (ast.Try, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _call_names(node: ast.AST, *, skip_nested_code: bool) -> set[str]:
    """Call names reachable from `node` -- the same walk as `_call_nodes`.

    Derived rather than written twice: two walkers with the same nesting rule are two
    places for it to drift, and this rule has already been wrong once (the root-node
    case below). The names are what the parse half asks for; the nodes are what the
    text-read half needs.
    """
    return {
        (c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", ""))
        for c in _call_nodes(node, skip_nested_code=skip_nested_code)
    }


def _call_nodes(node: ast.AST, *, skip_nested_code: bool) -> list[ast.Call]:
    """The `ast.Call` nodes reachable from `node`, with the same nesting rule.

    `_call_names` answers "which calls", which is enough to ask *whether* a read or a
    parse happens; a text read also needs the **node**, because `open`'s mode decides
    whether it decodes anything at all, and that is a property of the call's arguments.
    """
    out: list[ast.Call] = []
    if skip_nested_code and isinstance(node, _NESTED_SCOPES):
        # The node *itself*, not only its children: a `try` that is a direct statement
        # of another `try` is the scope that owns its own reads, and a walk that only
        # filters children counts the inner read as the outer try's. Measured
        # 2026-10-03 (`cyc20261003-222355`) on `daemon.py`'s `file_content` handler,
        # where the inner try catches `UnicodeDecodeError` and the outer one was
        # blamed for it -- the same shape as `check_unbound_reads.py`'s `_class_defs`,
        # which counted `node` itself in its own ignore set and so reported nothing.
        return out

    def walk(n: ast.AST) -> None:
        for child in ast.iter_child_nodes(n):
            if skip_nested_code and isinstance(child, _NESTED_SCOPES):
                continue
            if isinstance(child, ast.Call):
                out.append(child)
            walk(child)

    walk(node)
    return out


def _open_mode(call: ast.Call) -> str:
    """The mode argument of an `open` call, defaulted the way Python defaults it.

    The builtin is `open(path, mode)`, so its mode is the *second* positional
    argument; `Path.open(mode)` is a method whose `self` is implicit, so its mode is
    the *first*. Reading one position for both is how a write gets classified as a
    read and a read as a write.
    """
    positional = 1 if isinstance(call.func, ast.Name) else 0
    mode = "r"
    if len(call.args) > positional and isinstance(call.args[positional], ast.Constant):
        if isinstance(call.args[positional].value, str):
            mode = call.args[positional].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            mode = kw.value.value
    return mode


def _is_text_read(call: ast.Call) -> bool:
    """Does this call decode bytes into text, and so raise `UnicodeDecodeError`?

    Two ways a call that looks like a text read decodes nothing at all, and both were
    measured on this tree before the rule was allowed to fire (2026-10-03,
    `cyc20261003-222355`) -- a first version counted `path.open("w", ...)` as a read
    because it read the wrong argument, and would have blamed a reader that had already
    made the decode unfailable:

    * a **write** mode (`w`/`a`/`x`/`+`) truncates rather than decodes;
    * a **binary** mode (`b`) hands back bytes, and an `errors=` that is not `strict`
      tells the codec to substitute instead of raising.
    """
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
    if name == "read_text":
        return not _decode_cannot_fail(call)
    if name != "open":
        return False
    mode = _open_mode(call)
    if any(c in mode for c in WRITE_MODE_CHARS) or "b" in mode:
        return False
    return not _decode_cannot_fail(call)


def _decode_cannot_fail(call: ast.Call) -> bool:
    """Does an explicit `errors=` tell the codec to substitute rather than raise?"""
    for kw in call.keywords:
        if kw.arg != "errors":
            continue
        value = kw.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value != "strict"
        return True  # a computed value is not something this guard can read as "strict"
    return False


def _module_constants(trees: dict[Path, ast.Module]) -> dict[str, set[str]]:
    """Every module-level tuple/set constant in the tree, by name.

    By name rather than by module, because a home constant is imported into the
    modules that catch it: `from emrg.read_errors import JSON_READ_ERRORS` binds a
    name whose *value* lives elsewhere, and the reader is judged on the value.
    """
    consts: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, (ast.Tuple, ast.Set)):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = {
                        ast.unparse(e.value) if isinstance(e, ast.Starred) else ast.unparse(e)
                        for e in node.value.elts
                    }
    return consts


def _resolve(name: str, consts: dict[str, set[str]], depth: int = 0) -> set[str]:
    """The exception names a caught name stands for, following constants and stars."""
    if depth > 6 or name not in consts:
        return {name}
    out: set[str] = set()
    for part in consts[name]:
        out |= _resolve(part, consts, depth + 1) if part.isidentifier() and part in consts else {part}
    return out


def _caught_names(handler: ast.ExceptHandler, consts: dict[str, set[str]]) -> set[str]:
    if handler.type is None:
        return {"<bare>"}
    out: set[str] = set()
    for e in (handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]):
        out |= _resolve(ast.unparse(e.value) if isinstance(e, ast.Starred) else ast.unparse(e), consts)
    return out


def scan(root: Path) -> tuple[list[str], list[str]]:
    """(findings, unmeasured) -- never a single list, so "clean" and "could not read"
    stay distinguishable (the family's rule: a report that cannot measure has not
    passed)."""
    findings: list[str] = []
    unmeasured: list[str] = []
    trees: dict[Path, ast.Module] = {}

    for path in sorted(root.rglob("*.py")):
        if set(path.parts) & SKIP_DIRS:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unmeasured.append(f"{path} ({type(exc).__name__})")
            continue
        try:
            trees[path] = ast.parse(source)
        except SyntaxError as exc:
            unmeasured.append(f"{path} (SyntaxError: {exc.msg})")
        except ValueError as exc:  # a source file CPython itself cannot tokenise
            unmeasured.append(f"{path} ({type(exc).__name__})")

    consts = _module_constants(trees)

    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if not node.handlers:
                # A `try`/`finally` with no `except` is not an error guard -- it has no
                # answer to be right or wrong about, and judging it would report the
                # absence of a decision as a wrong one.
                continue
            direct: set[str] = set()
            text_reads: list[ast.Call] = []
            for stmt in node.body:
                direct |= _call_names(stmt, skip_nested_code=True)
                text_reads += [c for c in _call_nodes(stmt, skip_nested_code=True) if _is_text_read(c)]
            if not (direct & READ_CALLS):
                continue
            under: set[str] = set()
            for stmt in node.body:
                under |= _call_names(stmt, skip_nested_code=False)
            parses = bool(under & PARSE_CALLS)
            # The rule has two halves, and the second is the one that is easy to miss:
            # a read that parses its content is the shape the family was found in, but a
            # read that parses nothing decodes exactly as much -- `read_text` is where the
            # `UnicodeDecodeError` is raised, so a reader with no parse call is not
            # exempt from it. A first version of this guard asked only about readers that
            # parse, which is why twenty read-only sites went on answering one input
            # class two ways while the guard reported a clean tree.
            if not parses and not text_reads:
                continue
            caught: set[str] = set()
            for handler in node.handlers:
                caught |= _caught_names(handler, consts)
            if caught & DECODE_COVERING:
                continue
            if parses:
                findings.append(
                    f"{path}:{node.lineno} reads {sorted(direct & READ_CALLS)} and parses "
                    f"{sorted(under & PARSE_CALLS)}, but catches only {sorted(caught)}"
                )
            else:
                seen = sorted(
                    {
                        (c.func.attr if isinstance(c.func, ast.Attribute) else c.func.id)
                        for c in text_reads
                        if isinstance(c.func, (ast.Attribute, ast.Name))
                    }
                )
                findings.append(
                    f"{path}:{node.lineno} reads text ({seen}) and parses nothing, but "
                    f"catches only {sorted(caught)} - a text read decodes, so "
                    "`UnicodeDecodeError` escapes this handler too"
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
        "--quiet", action="store_true", help="print nothing but the verdict line on a clean tree"
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else _resolve_root()
    sys.stdout.reconfigure(line_buffering=True)
    print(f"tree: {root}")

    findings, unmeasured = scan(root)

    for line in findings:
        print(f"ERROR: {line}")
    for line in unmeasured:
        print(f"UNMEASURABLE: cannot read {line}", file=sys.stderr)

    if findings:
        print(
            f"ERROR: {len(findings)} site(s) read a file as text without naming a "
            "decode error - whether or not they parse what they read. A file whose "
            "bytes are not UTF-8 is the same input class as a missing or malformed "
            "one, and `UnicodeDecodeError` is a ValueError, not an OSError, so those "
            "readers answer one shape differently from the other two. Catch "
            "`emrg.read_errors.FILE_READ_ERRORS` plus the format's parse error (or "
            "`JSON_READ_ERRORS` / `YAML_READ_ERRORS` / `CONFIG_READ_ERRORS`, which are "
            "built from it)."
        )
        return 1
    if unmeasured:
        print(
            f"UNMEASURABLE: {len(unmeasured)} file(s) could not be read or parsed, so "
            "this is not a clean reading of the tree.",
            file=sys.stderr,
        )
        return 2
    if not args.quiet:
        print("OK: every text read that names an error guard names a decode error too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
