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
For each `try` whose **direct** body performs a text read (`read_text`, `read_bytes`,
`open`) and under which a parse (`loads` / `load` / `safe_load` / ...) happens, the
handlers must together name a decode-covering exception: `UnicodeDecodeError`,
`UnicodeError`, `ValueError`, `Exception`, or a bare `except`. Module-level constants
are resolved by name across the tree, so a reader that catches
`emrg.read_errors.JSON_READ_ERRORS` (or `FILE_READ_ERRORS`, or `YAML_READ_ERRORS`,
or `CONFIG_READ_ERRORS`) counts as naming whatever that constant holds.

Only *direct* body calls count for the read, because the decode happens at the read:
a `read_text` inside a nested `try` is that `try`'s business, not this one's.

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


def _call_names(node: ast.AST, *, skip_nested_code: bool) -> set[str]:
    """Call names reachable from `node`.

    `skip_nested_code` stops at a nested `try` or function definition, because those
    are their own error-handling scopes: the read inside them is not this try's.
    """
    out: set[str] = set()

    def walk(n: ast.AST) -> None:
        for child in ast.iter_child_nodes(n):
            if skip_nested_code and isinstance(
                child, (ast.Try, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            ):
                continue
            if isinstance(child, ast.Call):
                f = child.func
                out.add(f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else "")
            walk(child)

    walk(node)
    return out


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
    """(findings, unmeasured) — never a single list, so "clean" and "could not read"
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
                # A `try`/`finally` with no `except` is not an error guard — it has no
                # answer to be right or wrong about, and judging it would report the
                # absence of a decision as a wrong one.
                continue
            direct: set[str] = set()
            for stmt in node.body:
                direct |= _call_names(stmt, skip_nested_code=True)
            if not (direct & READ_CALLS):
                continue
            under: set[str] = set()
            for stmt in node.body:
                under |= _call_names(stmt, skip_nested_code=False)
            if not (under & PARSE_CALLS):
                continue
            caught: set[str] = set()
            for handler in node.handlers:
                caught |= _caught_names(handler, consts)
            if caught & DECODE_COVERING:
                continue
            findings.append(
                f"{path}:{node.lineno} reads {sorted(direct & READ_CALLS)} and parses "
                f"{sorted(under & PARSE_CALLS)}, but catches only {sorted(caught)}"
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
            f"ERROR: {len(findings)} site(s) read a file and parse it without naming a "
            "decode error. A file whose bytes are not UTF-8 is the same input class as "
            "a missing or malformed one, and `UnicodeDecodeError` is a ValueError, not "
            "an OSError - so those readers answer one shape differently from the other "
            "two. Catch `emrg.read_errors.FILE_READ_ERRORS` plus the format's parse "
            "error (or `JSON_READ_ERRORS` / `YAML_READ_ERRORS` / `CONFIG_READ_ERRORS`, "
            "which are built from it)."
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
        print("OK: every reader that parses a file it read also names a decode error.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
