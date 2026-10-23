"""A cross-reference role must name something the tree has, or send the reader nowhere.

The defect this holds
---------------------
A docstring wrote that a legacy row "is moved by `migrate_rant_agent_field` before any reader
sees it", and no such function existed anywhere -- `git grep` found exactly one hit for the name,
the sentence that claimed it. The reader who wants to know how the migration works goes looking
for it, finds nothing, and cannot tell a rename from a rewrite from a promise. The path opens and
the module parses, so nothing complains.

Sibling guards, and why this is a third
--------------------------------------
Two guards already read a pointer in prose: `tests/test_check_citation_resolves.py` (through
`scripts/check-citation-resolves.py`) for a `tests/test_x.py::node` citation that must collect,
and `tests/test_docstring_title_citations_resolve.py` for "see `Title` in the module docstring"
that must land on a heading the module carries. This one holds a third subject with a third
resolution rule: a **Sphinx cross-reference role** names a code symbol, and what decides it is
whether that symbol exists.

What is measured, and what a role claims
----------------------------------------
A role is a *declaration*: `func` says "callable", `class` says "type", `meth` says "method",
`data` says "a value". Each is a claim the tree can answer, because the claim is that the cited
name exists. Two spellings resolve differently:

* a **bare** name is the claim that the tree defines it -- any def, class, assignment or import
  anywhere under the scanned directories, or a builtin;
* a **dotted** name is a claim made *through* something, and static resolution cannot follow the
  tail of `os.path.expandvars`. So the bound is the head: the first segment must be a name the
  citing file can see -- imported by it, bound somewhere in the tree, or a builtin. That is the
  decidable half of the claim ("do not cite through a module you never opened") and it is where
  the guard stops.

The `attr` role is deliberately outside the scan. It names a member of an object, not a binding
a module holds, so the tree's `attr` references are `ctypes` structure fields -- three sites a
rule reading them as module bindings would report and be wrong about every time.

What this does not measure
--------------------------
Whether the cited name is the *right* one. A reference to a real function that has since stopped
doing what the sentence says is invisible here; this guard answers only whether the pointer lands
on a name the tree has. It also does not read a **bare backticked name in prose** -- the shape
the defect actually took. A backtick in prose is not a declaration, and the tree holds hundreds
that name files, config keys and protocol frames rather than code; a rule over them would report
prose and be deleted.
"""

from __future__ import annotations

import ast
import builtins
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The directories whose Python sources carry this convention.
SCANNED = ("emrg", "scripts", "tests")

#: Vendored trees inside a scanned directory, whose sources are not this project's.
SKIPPED = ("node_modules", "site-packages")

#: A cross-reference role whose subject is a symbol the tree can be asked about. `attr` is out
#: for the reason the module docstring gives: it names a member, not a binding.
ROLE = re.compile(r":(?P<role>func|class|meth|data):`~?(?P<name>[^`\n]+)`")


def tree_roots(root: Path = REPO_ROOT) -> list[Path]:
    """The directories this convention covers, under one root."""
    return [Path(root) / name for name in SCANNED]


def sources(roots) -> list[Path]:
    """Every Python file under the given roots, in a stable order."""
    found = []
    for root in roots:
        root = Path(root)
        for path in root.rglob("*.py"):
            # Judged on the path relative to the root it was walked from: an absolute path's
            # parts let any ancestor named `node_modules` or `.venv` silence the walk, and the
            # reading then reports a clean nothing.
            relative = path.relative_to(root)
            if any(part in SKIPPED for part in relative.parts):
                continue
            if path.is_file():
                found.append(path)
    return sorted(set(found))


def bound(path: Path) -> set[str]:
    """Every name one module binds: a def, a class, an assignment target, an import."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for inner in ast.walk(target):
                    if isinstance(inner, ast.Name):
                        names.add(inner.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def scan(roots) -> list[str]:
    """One finding per cross-reference that lands on no name the scanned tree has."""
    paths = sources(roots)
    per_file = {path: bound(path) for path in paths}
    tree_names: set[str] = set()
    for names in per_file.values():
        tree_names |= names

    findings = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(f"{path}: could not be read ({exc}); unmeasured, never a pass")
            continue
        visible = per_file[path]
        for match in ROLE.finditer(text):
            role = match.group("role")
            name = match.group("name").strip()
            if not name:
                continue
            line = text.count("\n", 0, match.start()) + 1
            if "." in name:
                head = name.split(".")[0]
                if head in visible or head in tree_names or head in vars(builtins):
                    continue
                findings.append(
                    f"{path}:{line}: cites {name!r} as a {role}, and {head!r} is neither "
                    f"imported in this file nor bound anywhere in the tree — the reader is "
                    f"sent through a module nothing here has opened"
                )
                continue
            if name in tree_names or name in vars(builtins):
                continue
            findings.append(
                f"{path}:{line}: cites {name!r} as a {role}, and no def, class, assignment or "
                f"import under {', '.join(str(r) for r in roots)} binds that name — the "
                f"reader follows the pointer to something that is not there"
            )
    return findings


# ── the tree ──────────────────────────────────────────────────────────────────
#
# Fixtures below assemble the role from parts on purpose. Written inline, each would be a
# cross-reference site in *this* file, and the tree reading above would report this guard's own
# fixtures back at it — a guard that cannot be run on the tree that holds it is no guard.


def _ref(role: str, name: str) -> str:
    """A cross-reference role around a name, built rather than typed."""
    return f":{role}:`{name}`"


def test_every_cross_reference_in_the_tree_resolves():
    """The subject is this tree, and the reading names every site that does not land."""
    findings = scan(tree_roots())
    assert not findings, (
        "a cross-reference names something the tree does not have; the reader who follows it "
        "finds nothing, and nothing else in the suite asks:\n  " + "\n  ".join(findings)
    )


def test_a_name_the_tree_does_not_define_is_reported(tmp_path):
    """The defect's shape: a role pointing at a callable no module here defines."""
    site = tmp_path / "citing.py"
    site.write_text(
        f'"""{_ref("func", "migrate_rant_agent_field")} moves the old key first."""\n\n\n'
        "def migrate_something_else():\n    pass\n",
        encoding="utf-8",
    )
    findings = scan([tmp_path])
    assert findings, (
        "a cross-reference to a name no module defines was read as resolved; this is the "
        "defect the guard exists for"
    )
    assert "migrate_rant_agent_field" in findings[0], findings[0]


def test_a_name_the_tree_does_define_resolves(tmp_path):
    """The convention's ordinary case, so the guard is not green only on the defect."""
    site = tmp_path / "defined.py"
    site.write_text(
        f'"""Uses {_ref("func", "real_helper")} below."""\n\n\n'
        "def real_helper():\n    pass\n",
        encoding="utf-8",
    )
    assert scan([tmp_path]) == [], "a cross-reference to a def in the same file was reported"


def test_a_name_bound_in_another_module_resolves(tmp_path):
    """A pointer to a sibling module's symbol is a fair pointer — the tree has it."""
    (tmp_path / "owner.py").write_text("class ExtraRootVerdict:\n    pass\n", encoding="utf-8")
    site = tmp_path / "citing.py"
    site.write_text(
        f'"""{_ref("class", "ExtraRootVerdict")} documents what the store holds."""\n',
        encoding="utf-8",
    )
    assert scan([tmp_path]) == [], (
        "a cross-reference to a class another module in the tree defines was reported; the "
        "tree answers that claim"
    )


def test_a_dotted_reference_through_an_import_resolves(tmp_path):
    """`os.path.expandvars` is undecidable past the head, and the head is the claim."""
    site = tmp_path / "dotted.py"
    site.write_text(
        f'"""Resolved by {_ref("func", "os.path.expandvars")}."""\n\nimport os\n',
        encoding="utf-8",
    )
    assert scan([tmp_path]) == [], (
        "a dotted reference whose head the file imports was reported; static resolution "
        "cannot follow the tail and must not pretend to"
    )


def test_a_dotted_reference_through_a_module_never_opened_is_reported(tmp_path):
    """The decidable half: a head the file cannot see is a claim nothing supports."""
    site = tmp_path / "unopened.py"
    site.write_text(
        f'"""Resolved by {_ref("func", "elsewhere.helper")}."""\n',
        encoding="utf-8",
    )
    findings = scan([tmp_path])
    assert findings, (
        "a dotted reference whose head is neither imported nor bound anywhere was read as "
        "resolved"
    )
    assert "elsewhere" in findings[0], findings[0]


def test_a_builtin_resolves(tmp_path):
    """A role naming a builtin is a real symbol, so the guard must not report it."""
    site = tmp_path / "builtin.py"
    site.write_text(f'"""Wraps {_ref("func", "len")}."""\n', encoding="utf-8")
    assert scan([tmp_path]) == [], "a cross-reference to a builtin was reported"


def test_a_bare_backticked_name_in_prose_is_not_a_reference(tmp_path):
    """The lookalikes are the load-bearing half: a guard that reports prose gets deleted."""
    site = tmp_path / "prose.py"
    site.write_text(
        '"""Rows are stored in `rants.jsonl` and `tasks.yml`, keyed by `agent`."""\n',
        encoding="utf-8",
    )
    assert scan([tmp_path]) == [], (
        "a bare backticked word in prose was read as a cross-reference; a backtick is not a "
        "declaration"
    )


def test_a_member_role_is_not_a_module_binding(tmp_path):
    """`attr` names a field of a structure, not a name the module binds."""
    site = tmp_path / "fields.py"
    site.write_text(
        f'"""{_ref("attr", "SubAuthority")} is a field of the struct below."""\n\n\n'
        "class Struct:\n    _fields_ = [(\"SubAuthority\", 1)]\n",
        encoding="utf-8",
    )
    assert scan([tmp_path]) == [], (
        "an `attr` reference to a structure field was reported as a missing module binding; "
        "that role makes no such claim"
    )


def test_a_reference_in_a_comment_is_still_a_reference(tmp_path):
    """A reader follows the pointer wherever it is written, the code block included."""
    site = tmp_path / "comment.py"
    site.write_text(
        f"# The move is done by {_ref('func', 'absent_mover')} before any read.\nVALUE = 1\n",
        encoding="utf-8",
    )
    findings = scan([tmp_path])
    assert findings, (
        "a cross-reference written in a comment was not read at all; the docstring-only reader "
        "misses the half of this class that lives outside docstrings"
    )
