"""The one reading stays one reading — checked, not promised.

Why this file exists
--------------------
`scripts/merge_tree.py` was created by collapsing five gates' copies of two facts
(git's merge verdict, git's conflicted path names) into one module. A collapse is a
*snapshot*; what makes it hold is a rule something runs. Without one, the sixth copy
is written by the next person who needs a path in a hurry — which is exactly how this
family spent five PRs (#1210, #1212, #1213, #1215, #1216) repairing one defect five
times, each repair blind to the copies it did not touch.

So the "one owner" claim is mechanised here, in rules that are measured in both
directions (#455 lesson — a discriminator proven only in the failing state proves
nothing): `test_a_sixth_copy_is_caught` plants the copies this guard exists to catch,
and `test_prose_may_quote_the_rule` plants the one thing it must *not* catch.

The rules
---------
R1  **No re-declaration.** A def/class in a gate whose name is one of the owner's
    rules must ask `merge_tree` for that rule. `def _is_object_name(line): return
    merge_tree.is_object_name(line)` is delegation; the same name with
    `re.fullmatch(r"[0-9a-f]{40}|...")` in the body was the sixth copy, live, until
    this cycle.
R1b **No re-binding.** A module-level name in those rules may only be bound to an
    attribute (`PLAN_COMMIT_DATE = merge_tree.PLAN_COMMIT_DATE`) — the two gates that
    still name the pin alias it; a literal would be a second declaration.
R2  **No re-spelling.** The shapes the deleted copies were made of — the object-name
    regex, the stage-block head, the prose fallback, the pinned instant, the octal
    decode — may not appear as *code* in a gate. A docstring may quote them: prose is
    allowed to describe a rule, it just may not be one.
R3  **No unimported call.** A gate that asks `merge_tree.<rule>` must import it; the
    import is path-based (these scripts have hyphenated names and are loaded by file
    path), so a missing one is a `NameError` at the first measurement, not an import
    error at review time.

What this is not: a proof that the reading is *correct*. The arm table lives in
`tests/test_merge_tree.py` and the rule itself in `scripts/merge_tree.py`. This file
only answers "is there one of it?" — the question five copies made unanswerable.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

OWNER = SCRIPTS / "merge_tree.py"

#: Every gate, discovered rather than listed: a new `check-merge-*.py` is covered the
#: day it lands, which is the day a copy would be pasted into it.
FAMILY = sorted(SCRIPTS.glob("check-merge-*.py"))

#: The rules `merge_tree.py` owns — its public surface, by the name a copy would reuse.
#: The four gates that only ever *consume* these are not listed: a caller's own mapping
#: (`_conflict_paths`, `_merge_tree_paths`) belongs to the caller.
RULES = (
    "MeasurementError",
    "PLAN_COMMIT_DATE",
    "is_object_name",
    "unquote_path",
    "stage_block_paths",
    "fold",
    "merged_tree",
    "merged_tree_sha",
    "commit_env",
    "commit_tree",
    "merge_commit",
)

#: The spellings the deleted copies carried, each one a rule re-implemented under a
#: different name — which is why the name-based rule (R1) alone is not enough.
SPELLINGS = {
    "the object-name shape": re.compile(r"\[0-9a-f\]\{40\}|\[0-9a-f\]\{64\}"),
    "the stage-block head": re.compile(r"\[0-7\]\{6\}"),
    "the prose fallback": re.compile(r"Merge conflict in"),
    "the pinned instant": re.compile(re.escape("2000-01-01T00:00:00")),
}

#: The owner, so the rules can be asserted to be *about* something.
OWNER_RULES = tuple(RULES)


def _docstrings(tree: ast.AST) -> set[int]:
    """The `id()` of every string constant that is a docstring.

    Prose may quote a rule's spelling (several gates' docstrings do, to explain what
    they used to get wrong); only code may not contain it. `ast` keeps the two apart
    for exactly this reason.
    """
    found: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def code_strings(tree: ast.AST) -> list[ast.Constant]:
    """Every string constant of the file that is code rather than a docstring."""
    prose = _docstrings(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in prose
    ]


def _asks_the_owner(node: ast.AST, rule: str) -> bool:
    """Whether a definition's body reaches `merge_tree.<rule>`."""
    return any(
        isinstance(inner, ast.Attribute)
        and isinstance(inner.value, ast.Name)
        and inner.value.id == "merge_tree"
        and inner.attr == rule
        for inner in ast.walk(node)
    )


def findings(path: Path, owner: str = "merge_tree") -> list[str]:
    """Every way the file keeps a second copy of a rule the owner holds.

    Empty means the file asks the owner for the whole reading. The owner itself is not
    checked — it *is* the reading — so callers skip it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name.lstrip("_")
            if name in RULES and not _asks_the_owner(node, name):
                found.append(
                    f"{path.name}:{node.lineno}: defines `{node.name}` itself instead of "
                    f"asking {owner}.{name}"
                )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name) or target.id.lstrip("_") not in RULES:
                    continue
                # An attribute — the owner's, or a sibling that got it from the owner.
                if not isinstance(node.value, ast.Attribute):
                    found.append(
                        f"{path.name}:{node.lineno}: binds `{target.id}` to a value, not "
                        f"to {owner}'s (a second declaration)"
                    )

    for node in code_strings(tree):
        for label, pattern in SPELLINGS.items():
            if pattern.search(node.value):
                found.append(
                    f"{path.name}:{node.lineno}: re-spells {label} in code "
                    f"({node.value[:60]!r}) instead of asking {owner}"
                )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "int"
            and len(node.args) == 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == 8
        ):
            found.append(
                f"{path.name}:{node.lineno}: decodes an octal escape (`int(..., 8)`) "
                f"instead of asking {owner}.unquote_path"
            )

    return found


def _imports_owner(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if "merge_tree" in [alias.name for alias in node.names]:
                return True
        if isinstance(node, ast.ImportFrom) and node.module == "merge_tree":
            return True
    return False


class TestTheFamilyAsksTheOwner:
    def test_every_gate_is_discovered(self) -> None:
        """The gate set is found, not listed — a new gate cannot escape the guard."""
        names = {path.name for path in FAMILY}
        assert {
            "check-merge-freshness.py",
            "check-merge-landing-diff.py",
            "check-merge-order.py",
            "check-merge-pairs.py",
            "check-merge-plan-suite.py",
            "check-merge-sequence.py",
            "check-merge-tree-health.py",
        } <= names, sorted(names)

    def test_no_gate_keeps_a_copy(self) -> None:
        """The whole point: the reading exists in one gate, and none of the others."""
        found = [finding for path in FAMILY for finding in findings(path)]
        assert found == [], "\n".join(found)

    def test_the_owner_holds_the_rules_the_other_gates_ask_for(self) -> None:
        """A gate told to "ask the owner" must be asking about something that exists."""
        tree = ast.parse(OWNER.read_text(encoding="utf-8"), filename=str(OWNER))
        declared = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        } | {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        assert set(OWNER_RULES) <= declared, sorted(set(OWNER_RULES) - declared)

    def test_a_gate_that_asks_the_owner_imports_it(self) -> None:
        """R3: the path-based import is one line, and its absence is a NameError later."""
        asked = [
            path
            for path in FAMILY
            if "merge_tree." in path.read_text(encoding="utf-8")
        ]
        assert asked, "no gate asks merge_tree at all — the collapse is not in place"
        missing = [path.name for path in asked if not _imports_owner(path)]
        assert missing == [], missing


class TestTheGuardIsLoadBearing:
    """The two directions. A guard that only ever sees a clean family is a tautology."""

    def _write(self, tmp_path: Path, name: str, body: str) -> Path:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_sixth_copy_is_caught(self, tmp_path: Path) -> None:
        """Each spelling the family actually shipped, planted one at a time.

        These are not invented mutants: every line below is a shape that was live in
        this repo, and the guard's job is to be the reason it can never be live again.
        """
        copied_rule = self._write(
            tmp_path,
            "check-merge-copy.py",
            "import re\n"
            "def _is_object_name(line):\n"
            "    return bool(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', line))\n",
        )
        assert any("asking merge_tree.is_object_name" in f for f in findings(copied_rule))

        respelled_reading = self._write(
            tmp_path,
            "check-merge-respelled.py",
            "import re\n"
            "_CONFLICT_LINE = re.compile(r'^CONFLICT \\\\([^)]+\\\\): "
            "(?:Merge conflict in )?(?P<path>.+)$')\n",
        )
        assert any("the prose fallback" in f for f in findings(respelled_reading))

        stage_head = self._write(
            tmp_path,
            "check-merge-stage.py",
            "import re\n"
            "STAGE = re.compile(r'^[0-7]{6} [0-9a-f]+ [123]\\t(?P<path>.+)$')\n",
        )
        assert any("the stage-block head" in f for f in findings(stage_head))

        second_pin = self._write(
            tmp_path,
            "check-merge-pin.py",
            'PLAN_COMMIT_DATE = "2000-01-01T00:00:00 +0000"\n',
        )
        second_pin_findings = findings(second_pin)
        assert any("a second declaration" in f for f in second_pin_findings)
        assert any("the pinned instant" in f for f in second_pin_findings)

        octal_decode = self._write(
            tmp_path,
            "check-merge-decode.py",
            "def unquote(path):\n"
            "    return bytes([int(path[i:i + 3], 8)]).decode('latin-1')\n",
        )
        assert any("int(..., 8)" in f for f in findings(octal_decode))

    def test_delegation_is_not_a_copy(self, tmp_path: Path) -> None:
        """The other direction: the shape every gate is *supposed* to have."""
        delegating = self._write(
            tmp_path,
            "check-merge-delegating.py",
            "import merge_tree\n"
            "\n"
            "PLAN_COMMIT_DATE = merge_tree.PLAN_COMMIT_DATE\n"
            "MeasurementError = merge_tree.MeasurementError\n"
            "\n"
            "def _is_object_name(line):\n"
            "    return merge_tree.is_object_name(line)\n"
            "\n"
            "def _conflict_paths(a, b):\n"
            "    return merge_tree.fold(a, b, run=_run).paths\n",
        )
        assert findings(delegating) == []

    def test_prose_may_quote_the_rule(self, tmp_path: Path) -> None:
        """A docstring explaining the defect must not be read as the defect.

        Without this arm the guard would be unusable: the gates' docstrings quote the
        old spellings on purpose, to say what they got wrong.
        """
        quoting = self._write(
            tmp_path,
            "check-merge-quoting.py",
            '"""Explains why the old `re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}")` and\n'
            "`Merge conflict in <path>` readings were blind to a modify/delete\n"
            'conflict, and why `2000-01-01T00:00:00 +0000` is pinned.\n"""\n'
            "import merge_tree\n"
            "\n"
            "def _merge_tree_paths(a, b):\n"
            '    """The paths, from the stage block - see merge_tree.unquote_path."""\n'
            "    return merge_tree.fold(a, b, run=_run).paths\n",
        )
        assert findings(quoting) == []
