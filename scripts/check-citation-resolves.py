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
  as written, so there is nothing to qualify;
* a citation inside **quoted material** - a string literal whose every line is a node
  id or pytest's own report line for one. That is what a fixture holds when it stubs a
  run, and a fixture's data is not a claim about this tree; the docstring's principle
  was applied only where the *path* was missing, which caught a synthetic `tests/test_a.py`
  and missed `tests/test_bash_v2_boundary.py`, a module this tree really has. One line of
  prose in the literal makes it a claim again, which is what keeps a dict of reasons and
  a "run this to reproduce" message inside the rule (both measured 2026-10-13,
  `cyc20261013-212318`).

**What the rule ranges over, added 2026-10-13** (`cyc20261013-212318`): a citation that
names *nothing* the module defines, and the **qualified** form `path::Class::name`. The
first was skipped by the same branch that skips a correct name - `if methods.get(name)`
was the only path to a finding, so "the name exists" was a premise nothing read - and one
live citation had already rotted: `scripts/review-queue.py` cited a test renamed to
`test_the_records_land_where_the_queue_reads_them`, which `grep -rn` finds nowhere but
there. The second was skipped outright, with a reason that was not true of the code
making it: `scan` holds the module's AST when it skips a qualified citation, and class
membership is exactly what it can answer from it. Measured on master `4b2d7013`: of the
citations this tool reads, 21 name something their module does not define - 8 are a
prefix, 5 a bare class, 7 quoted pytest output, and **1 was that stale citation** - and
the qualified form has 5 sites, all resolving.

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
``0``  every citation names what it says. ``1``  at least one does not - a class method
cited without its class, a citation naming nothing the module defines, or a qualified
citation whose class is absent or does not hold that name (each is printed with the
qualifier it needs, when one can be named). ``2`` the question could not be answered -
the root is not a directory, no test file could be read at all, which would make a green
verdict a reading over an empty set, or a qualified citation names a class that inherits
from a base the module does not define, so its inherited methods cannot be enumerated
from here. That last one is the ``#2211`` lesson: a subject this reader cannot classify
is *not measurable*, never a pass.

That second half was prose with no executor until 2026-10-06 (cycle
`cyc20261006-165503`): the paragraph promised the refusal and `main` had no branch
for it, so a root holding no test module printed the green line with rc 0 - measured
on an empty directory, and on `check_unbound_reads.py` too, which is the same class in
the other guard that can be pointed at a tree of the caller's choosing. Both now count
how many files they read and refuse when the answer is none; each count is pinned in
its own test file, and a third guard joining them is caught by
`tests/test_a_tree_reading_guard_names_its_tree.py`.
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

#: `tests/<path>.py::<name>`, the whole of a line - the shape a **quoted** node id has.
#: Used only to decide whether a string literal is quoted material; a line that is not
#: this (after `_QUOTED_LINE` has taken a pytest status word off) makes the literal prose,
#: and prose is a claim.
NODE_ID = re.compile(r"tests/[\w./@-]+\.py::\w+(?:::\w+)?")

#: pytest's short-summary line: `FAILED <node id> - <reason>` (`ERROR` for a collection
#: failure, `XFAIL`/`XPASS` for the two expected-outcome statuses).
_QUOTED_LINE = re.compile(r"(?:FAILED|ERROR|XFAIL|XPASS)\s+(?P<node>\S+)(?:\s+-\s+\S.*)?$")

#: The rest of pytest's own output, which a fixture quoting a run carries beside the node
#: ids: the summary line (`12 failed, 4381 passed in 165.56s`), an assertion's source
#: context (`>           assert mapping["a"] == 2`), its explanation (`E   assert 0 == 1`),
#: and a section banner (`===== short test summary info =====`).
_SUMMARY_LINE = re.compile(r"\d+\s+\w+.*$")
_CONTEXT_LINE = re.compile(r"[E>]\s")
_BANNER_LINE = re.compile(r"={3,}.*$")


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
    :param reason: what is wrong with it, as the sentence printed under the site -
        the tool reports three defects now, so the fault has to name itself.
    :param needed: the node id it should have been, when one can be named.
    """

    site: str
    line: int
    cited: str
    reason: str
    needed: str


class Scan(NamedTuple):
    """Everything one pass over a tree measured.

    `scan` keeps returning the three answers its callers always asked for; this is
    the whole reading, which `main` prints. `quoted` and `prefixes` are subjects the
    rule deliberately does not read, kept as counts so a narrowing cannot become
    invisible: a rule that quietly reads fewer subjects is how a green run stops
    meaning what it says.

    :param findings: one per citation that does not resolve as written.
    :param unreadable: cited modules that could not be parsed.
    :param modules: how many test modules the citations were resolved against.
    :param claims: citations read as claims about this tree.
    :param quoted: citations inside quoted material (a literal of node ids only).
    :param prefixes: citations naming a family (a trailing `_` or `*`).
    :param unclassified: qualified citations this reader cannot decide, because the
        class inherits from a base the module does not define.
    """

    findings: list[Finding]
    unreadable: list[str]
    modules: int
    claims: int
    quoted: int
    prefixes: int
    unclassified: list[str]


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


def node_ids(
    path: Path,
) -> Optional[tuple[set[str], dict[str, list[str]], dict[str, tuple[set[str], list[str]]]]]:
    """What pytest collects from one module, as the three tables the rule needs.

    :param path: the module to parse.
    :returns: `(module-level names, {method name: [classes holding it]},
        {class name: (its own methods, its base names)})`, or `None` when the module
        could not be parsed - which is a different fact from "it defines nothing",
        and only the first is unmeasurable.
    """
    module_level: set[str] = set()
    methods: dict[str, list[str]] = {}
    classes: dict[str, tuple[set[str], list[str]]] = {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_level.add(node.name)
        elif isinstance(node, ast.ClassDef):
            own = {
                sub.name
                for sub in node.body
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            classes[node.name] = (own, [ast.unparse(base) for base in node.bases])
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.setdefault(sub.name, []).append(node.name)
    return module_level, methods, classes


def _line_starts(text: str) -> list[int]:
    """The character offset every 1-based line of `text` starts at.

    :param text: the file's decoded text.
    :returns: one offset per line, in order.
    """
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _offset(starts: list[int], text: str, line: int, col: int) -> int:
    """`(line, col_offset)` as one character offset into `text`.

    `ast` reports `col_offset` as a **UTF-8 byte** offset while `re` reports character
    offsets, so the two only agree on an all-ASCII line: the conversion has to happen
    here rather than being assumed away.

    :param starts: `_line_starts(text)`.
    :param text: the file's decoded text.
    :param line: a 1-based line number from `ast`.
    :param col: that node's `col_offset`, in bytes.
    :returns: the character offset.
    """
    if line < 1 or line > len(starts):
        return len(text)
    start = starts[line - 1]
    end = starts[line] if line < len(starts) else len(text)
    prefix = text[start:end].encode("utf-8")[:col]
    return start + len(prefix.decode("utf-8", errors="ignore"))


def _is_quoted_output(value: str) -> bool:
    """Whether a string literal is quoted pytest output rather than a sentence.

    Quoted output is what a fixture holds when it stubs a run: node ids, pytest's own
    summary and assertion lines, and blank lines - and **nothing else**. One line of
    prose is enough to make the literal a claim, because a citation embedded in a
    sentence is exactly how this tree cites: a dict whose value is a reason, or a
    message that tells a reader which test to run, is documentation, not data.

    :param value: the literal's value, as Python decoded it.
    :returns: `True` when nothing in it is prose.
    """
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return False
    for line in lines:
        status = _QUOTED_LINE.match(line)
        if status is not None:
            if not NODE_ID.fullmatch(status.group("node")):
                return False
            continue
        if NODE_ID.fullmatch(line):
            continue
        if _SUMMARY_LINE.match(line) or _CONTEXT_LINE.match(line) or _BANNER_LINE.match(line):
            continue
        return False
    return True


def quoted_spans(text: str) -> list[tuple[int, int]]:
    """The spans of every string literal in a `.py` file that holds only node ids.

    :param text: the module's decoded text.
    :returns: `(start, end)` character offsets, unsorted.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    starts = _line_starts(text)
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if not _is_quoted_output(node.value):
                continue
            spans.append(
                (
                    _offset(starts, text, node.lineno, node.col_offset),
                    _offset(starts, text, node.end_lineno, node.end_col_offset),
                )
            )
    return spans


def _with_bases(
    name: str,
    classes: dict[str, tuple[set[str], list[str]]],
    seen: set[str],
) -> Optional[set[str]]:
    """Every method a class collects, following bases the module itself defines.

    :param name: the class.
    :param classes: the module's class table.
    :param seen: classes already walked, so a cycle of bases terminates.
    :returns: the method names, or `None` when the class - or a base it inherits
        from - is not defined in this module, which is the one case the reader
        cannot answer.
    """
    if name in seen or name not in classes:
        return None
    seen.add(name)
    own, bases = classes[name]
    known = set(own)
    for base in bases:
        inherited = _with_bases(base, classes, seen)
        if inherited is None:
            return None
        known |= inherited
    return known


def read_sites(root: Path) -> Scan:
    """Check every citation under `root`, and count what the rule did not read.

    :param root: the tree to scan.
    :returns: the `Scan` - see its fields. Every exclusion is counted rather than
        dropped, so the verdict can say what it did not look at.
    """
    files = text_files(root)
    module_root = root / "tests"
    modules = {p for p in files if p.suffix == ".py" and p.is_relative_to(module_root)}
    parsed: dict[Path, Optional[tuple[set[str], dict[str, list[str]], dict[str, tuple[set[str], list[str]]]]]] = {}
    unreadable: list[str] = []
    findings: list[Finding] = []
    unclassified: list[str] = []
    claims = quoted = prefixes = 0

    for site in files:
        try:
            text = site.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        spans = quoted_spans(text) if site.suffix == ".py" else []
        for match in CITATION.finditer(text):
            cited_path, name = match.group(1), match.group(2)
            target = root / cited_path
            if target not in modules:
                continue  # a synthetic path, or a file that is not a test module
            if any(start <= match.start() < end for start, end in spans):
                # Quoted output, a fixture's data - not a claim about this tree.
                quoted += 1
                continue
            where = site.relative_to(root).as_posix()
            line = text.count("\n", 0, match.start()) + 1
            if target not in parsed:
                parsed[target] = node_ids(target)
            collected = parsed[target]
            if collected is None:
                unreadable.append(cited_path)
                continue
            module_level, methods, classes = collected
            claims += 1
            if _qualified(name) is not None:
                verdict = _read_qualified(cited_path, name, classes, module_level)
                if verdict is None:
                    continue
                if verdict == "unclassified":
                    unclassified.append(f"{cited_path}::{name}")
                    continue
                findings.append(
                    Finding(where, line, f"{cited_path}::{name}", verdict[0], verdict[1])
                )
                continue
            if name in module_level:
                continue
            if name in classes:
                # pytest collects a class as written, so there is nothing to qualify.
                continue
            if methods.get(name):
                findings.append(
                    Finding(
                        where,
                        line,
                        f"{cited_path}::{name}",
                        "a class method needs its class",
                        _needed(cited_path, name, methods),
                    )
                )
                continue
            if name.endswith("_") or name.endswith("*"):
                # A family - `test_shutdown_all_` - rather than a node id.
                prefixes += 1
                continue
            findings.append(
                Finding(
                    where,
                    line,
                    f"{cited_path}::{name}",
                    "the module defines no test, class or method by that name, so pytest "
                    "collects nothing",
                    "",
                )
            )
    return Scan(findings, unreadable, len(modules), claims, quoted, prefixes, unclassified)


def _qualified(name: str) -> Optional[tuple[str, str]]:
    """The two halves of a `Class::name` citation, or `None` for the other form.

    :param name: the citation's name segment.
    :returns: `(class, name)`.
    """
    if "::" not in name:
        return None
    cls, meth = name.split("::", 1)
    return cls, meth


def _read_qualified(
    cited_path: str,
    name: str,
    classes: dict[str, tuple[set[str], list[str]]],
    module_level: set[str],
) -> Optional[tuple[str, str]] | str:
    """Answer one already-qualified citation.

    :param cited_path: the cited path, as written.
    :param name: the citation as written, `<Class>::<method>`.
    :param classes: the cited module's class table.
    :param module_level: the cited module's module-level names.
    :returns: `None` when it resolves; the literal `"unclassified"` when the class
        inherits from a base this module does not define, so its methods cannot be
        enumerated; else `(reason, needed)`.
    """
    cls_name, meth = _qualified(name)  # type: ignore[misc]
    if cls_name not in classes:
        # The unqualified form is the remedy when the name is a module-level test.
        needed = f"{cited_path}::{meth}" if meth in module_level else ""
        return "no class by that name in the module, so this node id cannot collect", needed
    known = _with_bases(cls_name, classes, set())
    if known is None:
        # The class inherits from a base this module does not define, so its methods
        # cannot be enumerated from here - the caller reports that rather than a verdict.
        return "unclassified"
    if meth in known:
        return None
    holders = [c for c, (own, _bases) in classes.items() if meth in own]
    if holders:
        needed = f"{cited_path}::{holders[0]}::{meth}"
    elif meth in module_level:
        needed = f"{cited_path}::{meth}"
    else:
        needed = ""
    return f"the class {cls_name} does not define a test by that name", needed


def scan(root: Path) -> tuple[list[Finding], list[str], int]:
    """Check every citation under `root`.

    :param root: the tree to scan.
    :returns: `(findings, unreadable_sites, test_modules)` - the second is every
        file a citation was found in but that could not be parsed, so an empty
        finding list beside a non-empty one is reported instead of passed. The
        third is the set every citation is resolved *against*: with none of them
        the green verdict is a reading over an empty set, so the caller refuses
        rather than printing it, and prints the count when it does not.
    """
    report = read_sites(root)
    return report.findings, report.unreadable, report.modules


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
    report = read_sites(root)
    if report.unreadable:
        print(
            f"could not measure: {len(report.unreadable)} cited test module(s) could not be "
            f"parsed ({', '.join(sorted(set(report.unreadable)))})",
            file=sys.stderr,
        )
        return 2
    if report.modules == 0:
        # The docstring has promised this refusal since the tool was written and
        # `main` had no branch for it: a citation is resolved *against* a test
        # module, so with none under `<root>/tests` there is nothing to resolve
        # anything against and "every citation names what it says" is a reading
        # over an empty set. Measured 2026-10-06 (`cyc20261006-165503`) on an
        # empty directory: the green line, rc 0.
        print(
            f"could not measure: no test module was read under {root} "
            f"({root / 'tests'} holds no .py file), so there was nothing for a "
            "citation to resolve against - `0` says the citations were checked, "
            "and none was.",
            file=sys.stderr,
        )
        return 2
    if report.findings:
        for finding in report.findings:
            print(f"{finding.site}:{finding.line}: {finding.cited}")
            tail = f": {finding.needed}" if finding.needed else ""
            print(f"    {finding.reason}{tail}")
    if report.unclassified:
        # A qualified citation naming a class that inherits from outside the module:
        # its inherited methods cannot be enumerated from here, so whether it collects
        # is a question this reader cannot answer. Reported as unmeasurable rather than
        # passed - the `#2211` lesson, and the same shape as an unreadable module above.
        print(
            f"could not measure: {len(report.unclassified)} qualified citation(s) name a "
            "class that inherits from a base this module does not define, so its methods "
            f"cannot be read from here ({', '.join(sorted(set(report.unclassified)))})",
            file=sys.stderr,
        )
        return 2
    if not report.findings:
        print(
            "every citation names a node id pytest collects "
            f"({report.modules} test module(s) read, {report.claims} citation(s) read as "
            "claims)"
        )
        if report.quoted or report.prefixes:
            # The rule deliberately does not read these, and a narrowing nobody can see
            # is how a green run stops meaning what it says, so the counts are printed.
            print(
                f"    not read as claims: {report.quoted} in quoted material (a literal "
                f"holding only node ids), {report.prefixes} naming a family"
            )
        return 0
    print(f"{len(report.findings)} citation(s) do not name a node id pytest collects")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
