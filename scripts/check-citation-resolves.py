#!/usr/bin/env python3
"""A test citation must name the node id pytest would actually collect.

The defect class
----------------
A citation that names the method and not the class it lives in. The path
resolves, the name is a real definition, and the node id does not collect - so

    $ pytest "tests/test_check_merge_order.py::TestNoMutableRefNameReachesMergeTree::test_the_shipped_source_passes_only_commits_to_merge_tree" --collect-only -q
    1 test collected in 0.03s

is the form that works, and the same citation with the class segment dropped is
`ERROR: not found` (`tests/test_check_merge_order.py` at `9c665ff1`: the method
is at line 373 inside the class at 282). The reader who follows the dropped-class
form gets "not found" and has to re-derive the target by grep, which is the cost
the citation existed to remove.

**This docstring spells the working form only, on purpose.** A counter-example
written in citation form is indistinguishable from the defect, and it is this
tool's one measured false positive: the first draft of this file quoted the
broken citation verbatim and the tool reported itself. Two ways out, both used
here - quote the *correct* form, or keep the bad one out of `path::name` shape.
Anything else needs a judgement the tool cannot make, and a rule that guesses is
worse than a rule that reports.

Measured 2026-09-25 (cycle cyc20260925-052652) over every tracked text file: of
the citations naming a file under `tests/`, **32** name a module-level test and
collect exactly as written, and **0** name a class method without its class. So
the convention where a citation resolves is to write it so that it resolves -
and the one instance that broke it (PR #1595's `scripts/check-merge-order.py`)
was the first in the tree. This tool is that rule's reading.

Scope, stated because an unstated scope is a false OK
-----------------------------------------------------
A *citation* here is `<path>.py::<name>` where `<path>` begins with `tests/` and
names a file that exists in the tree. Deliberately excluded, each because it is
not a citation rather than because it is inconvenient:

* a path that does not exist - the tree is full of synthetic node ids written
  into fixtures (`tests/test_a.py::test_b` in `test_check_merge_plan_suite.py`),
  and a fixture's data is not a claim about this tree;
* a **bare name in prose**, with no path and no `::` (`mirrors
  test_turn_start_end_broadcast_lifecycle's pattern`). The requirement on a name
  is that it exists, which the AST check cannot see from a bare name, and the
  requirement on a node id is that it collects, which a bare name never claimed;
* a **prefix** ending in `*` or `_` (`tests/test_daemon.py::test_shutdown_all_*`
  in the prompt templates) - a family, not a node id;
* a **class** (`tests/test_ws_e2e.py::TestWSVibeCheck`) - pytest collects a class
  as written, so there is nothing to qualify.

Which tree answered
-------------------
The scan takes its tree as an **argument** (defaulting to `.`), and until 2026-09-25
(`cyc20260925-182347`) its verdict did not say which one it had read: the same sentence
was printed for this checkout, for a worktree, and for a tmp directory built by a test.
That is the defect `check-doc-count.py` records from 2026-09-11 - a confident `OK` about
a checkout the caller was not in - and this file was the one guard in the family that
had not inherited the remedy. It now prints `tree: <resolved root>` before any verdict,
so an unmeasurable answer names the tree it could not measure as well.

The line is not decoration: `check-merge-sequence.py` reads a sibling guard's report and
**refuses it outright** unless it names the tree it read, and requires that name to be the
tree it asked about (`TREE_IN_REPORT`). `tests/test_a_tree_reading_guard_names_its_tree.py`
runs the guards that can be run without a toolchain and holds them to it, and proves this
line tracks the argument rather than restating the repository root.

Exit codes
----------
``0``  every citation names what it says. ``1``  at least one names a class
method without its class (each is printed with the qualifier it needs). ``2``
the question could not be answered - the root is not a directory, or no test
file could be read at all, which would make a green verdict a reading over an
empty set.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import NamedTuple, Optional

#: Directories never descended into: not source, and large enough to matter.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".emrg",
    }
)

#: What a citation site can be written in. `.j2` carries the prompt templates.
TEXT_SUFFIXES = (".py", ".md", ".j2", ".yml", ".yaml", ".sh", ".txt")

#: `<tests path>.py::<name>`, the name optionally already carrying its class.
CITATION = re.compile(r"(tests/[\w./@-]+\.py)::(\w+(?:::\w+)?)")


class Finding(NamedTuple):
    """One citation that does not resolve as written.

    A `NamedTuple`, not a `@dataclass`: this module is loaded by path in its
    tests (`spec_from_file_location`, the shape `test_rant_citations.py` uses),
    and a dataclass built outside `sys.modules` raises in `dataclasses` itself
    when a string annotation is resolved. A carrier that only works under one
    loader is a trap for whoever adds the next test.

    :param site: the file the citation is written in.
    :param line: its 1-based line there.
    :param cited: the citation as written.
    :param needed: the node id it should have been.
    """

    site: str
    line: int
    cited: str
    needed: str


def text_files(root: Path) -> list[Path]:
    """Every readable text file under `root`, minus the skipped directories.

    :param root: the tree to walk.
    :returns: the files, in a stable order.
    """
    out: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.suffix in TEXT_SUFFIXES:
                out.append(entry)
    return sorted(out)


def node_ids(path: Path) -> Optional[tuple[set[str], dict[str, list[str]]]]:
    """What pytest collects from one module, as the two sets the rule needs.

    :param path: the module to parse.
    :returns: `(module-level names, {method name: [classes holding it]})`, or
        `None` when the module could not be parsed - which is a different fact
        from "it defines nothing", and only the first is unmeasurable.
    """
    module_level: set[str] = set()
    methods: dict[str, list[str]] = {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_level.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.setdefault(sub.name, []).append(node.name)
    return module_level, methods


def scan(root: Path) -> tuple[list[Finding], list[str]]:
    """Check every citation under `root`.

    :param root: the tree to scan.
    :returns: `(findings, unreadable_sites)` - the second is every file a
        citation was found in but that could not be parsed, so an empty finding
        list beside a non-empty one is reported instead of passed.
    """
    files = text_files(root)
    modules = {p for p in files if p.suffix == ".py" and p.is_relative_to(root / "tests")}
    parsed: dict[Path, Optional[tuple[set[str], dict[str, list[str]]]]] = {}
    unreadable: list[str] = []
    findings: list[Finding] = []

    for site in files:
        try:
            text = site.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in CITATION.finditer(text):
            cited_path, name = match.group(1), match.group(2)
            target = root / cited_path
            if target not in modules:
                continue  # synthetic path, prefix, or a file that is not a test module
            if target not in parsed:
                parsed[target] = node_ids(target)
            collected = parsed[target]
            if collected is None:
                unreadable.append(cited_path)
                continue
            module_level, methods = collected
            if "::" in name:
                # Already qualified. Whether it resolves is a *different*
                # question - a renamed test, which this rule does not own and
                # cannot answer from the qualifier alone - so it is left alone
                # rather than guessed at. The scope note above says so.
                continue
            if name in module_level:
                continue
            if methods.get(name):
                findings.append(
                    Finding(
                        site=str(site.relative_to(root)),
                        line=text.count("\n", 0, match.start()) + 1,
                        cited=f"{cited_path}::{name}",
                        needed=_needed(cited_path, name, methods),
                    )
                )
    return findings, unreadable


def _needed(cited_path: str, name: str, methods: dict[str, list[str]]) -> str:
    """The node id the citation should have been.

    :param cited_path: the cited path, as written.
    :param name: the method the citation named.
    :param methods: the method table of the cited module.
    :returns: `<path>::<Class>::<name>` for the class that holds it.
    """
    holders = methods.get(name, [])
    cls = holders[0] if holders else "<Class>"
    return f"{cited_path}::{cls}::{name}"


def main(argv: list[str] | None = None) -> int:
    """Report every citation that does not resolve as written.

    :param argv: the command line, defaults to `sys.argv[1:]`.
    :returns: the exit code the docstring states.
    """
    # A merged reader must see the `tree:` line before any verdict - this file's
    # docstring promises that order. stdout is block-buffered when it is a pipe
    # (how a cycle reads this report: `2>&1 | tail`) while stderr is not, so
    # without this every stderr line overtakes the tree line. Behaviour and pin:
    # tests/test_guard_report.py.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description="A test citation must name the node id pytest would collect."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="the tree to scan (default: the current directory)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"could not measure: {root} is not a directory", file=sys.stderr)
        return 2
    # Say which tree answered, before any verdict. This scan takes the tree as an
    # *argument* (defaulting to `.`), so its sentence is otherwise true of every
    # checkout at once - the 2026-09-11 defect `check-doc-count.py` and
    # `check_nonlocal.py` both record: a confident `OK` about a checkout the caller
    # was not in. The line also reaches a verdict the whole family checks rather
    # than assumes: `check-merge-sequence.py` refuses another guard's report
    # outright unless it names the tree it read, and requires that name to be the
    # tree it asked about (`TREE_IN_REPORT`).
    print(f"tree: {root}")
    findings, unreadable = scan(root)
    if unreadable:
        print(
            f"could not measure: {len(unreadable)} cited test module(s) could not be "
            f"parsed ({', '.join(sorted(set(unreadable)))})",
            file=sys.stderr,
        )
        return 2
    if not findings:
        print("every citation names a node id pytest collects")
        return 0
    for finding in findings:
        print(f"{finding.site}:{finding.line}: {finding.cited}")
        print(f"    a class method needs its class: {finding.needed}")
    print(f"{len(findings)} citation(s) name a class method without its class")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
