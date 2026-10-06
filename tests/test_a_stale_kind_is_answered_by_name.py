"""Every stale kind is answered by name, in both tools that must answer it.

Background (cycle cyc20261007-045324)
-------------------------------------
`check-merge-freshness.py` names each way a green CI verdict can stop being current
with a `_KIND_*` constant, and two tools read that name:

* `_remedy` turns it into the one line telling the reader what to do;
* `review-queue.py`'s `next_action` turns it into the one row a cycle should take.

Both of them are **chains of `if` branches**, and in both the *last* branch was not a
refusal but a specific kind's answer. So a kind that no branch named was not left
unanswered - it was answered **as something else**, silently:

* in `_remedy`, the fall-through was the **failing** remedy. A new kind would have
  been told to "fix the failure ... read the cause with
  `scripts/read-run-failure.py <run>`" for a failure that never happened, at the one
  moment the reader has no other reading to check it against;
* in `next_action`, the fall-through reached the rows that **spend** something -
  `measure-then-vote` / `measure-then-merge`, whose remedy is `scripts/cast-vote.py`.
  Measured on master while writing this guard, by running #1883's own fixture with the
  code that predates its row: a verdict of `no_run_yet` (a head whose CI run has not
  registered yet) fell through to `measure-then-vote` - i.e. the queue told a cycle to
  *vote* on a head whose CI had not begun.

Both halves are measured here rather than trusted to review, because the failure is
invisible in the direction that matters: adding a `_KIND_*` constant is a one-line
change, and nothing about the tree goes red until a live PR happens to be in the new
state.

The one deliberate exception
----------------------------
`ancestry` has no row in `next_action`, and that is correct rather than an omission:
"this head no longer contains master" *is* answered by the landing-tree measurement,
which is exactly what the generic `reading.stale` rows at the bottom of `next_action`
hand over (`measure-then-merge` / `measure-then-vote`, reached by reading the function,
not asserted here). The exemption is named once, below, so that a kind added without a
row is still a failure - which is the whole point of the guard.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FRESHNESS = REPO_ROOT / "scripts" / "check-merge-freshness.py"
QUEUE = REPO_ROOT / "scripts" / "review-queue.py"

#: The kind deliberately left to `next_action`'s generic stale rows, and why: the
#: landing-tree measurement the generic rows hand over *is* the remedy for a head
#: that no longer contains master. Every other kind has to be named by a row of its
#: own, because the generic rows vote.
GENERIC_KIND = "ancestry"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in the module")


def _kind_names(tree: ast.Module) -> set[str]:
    """The module-level `_KIND_*` constant names."""
    return {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id.startswith("_KIND_")
    }


def _kind_values(tree: ast.Module) -> set[str]:
    """The string value of each module-level `_KIND_*` constant."""
    return {
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and any(
            isinstance(target, ast.Name) and target.id.startswith("_KIND_")
            for target in node.targets
        )
    }


def _names_compared_in_remedy(tree: ast.Module) -> set[str]:
    """The `_KIND_*` names `_remedy` compares `kind` against."""
    found: set[str] = set()
    for node in ast.walk(_function(tree, "_remedy")):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "kind"
        ):
            for comparator in node.comparators:
                if isinstance(comparator, ast.Name) and comparator.id.startswith("_KIND_"):
                    found.add(comparator.id)
    return found


def _kinds_routed_by_the_queue(tree: ast.Module) -> set[str]:
    """The kind strings `next_action` compares `reading.stale_kind` against."""
    found: set[str] = set()
    for node in ast.walk(_function(tree, "next_action")):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "stale_kind"
        ):
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    found.add(comparator.value)
    return found


def _load_module():
    spec = importlib.util.spec_from_file_location("check_merge_freshness", FRESHNESS)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec for the reason the sibling suite gives: `_remedy`'s module
    # declares dataclasses, which resolve annotations through `sys.modules` at class
    # creation.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_every_stale_kind_is_named_by_the_remedy():
    """`_remedy` has to name every kind, because its fall-through is not a refusal.

    This is the arm that was red on master: `_KIND_FAILING` had no branch, so the
    failing remedy *was* the fall-through, and any kind added beside it would have
    inherited that answer.
    """
    tree = _parse(FRESHNESS)
    kinds = _kind_names(tree)
    named = _names_compared_in_remedy(tree)
    assert named == kinds, (
        "every `_KIND_*` has to be compared against `kind` in `_remedy`; a kind it "
        f"does not name is answered by whatever the fall-through says. unnamed: "
        f"{sorted(kinds - named)}"
    )


def test_a_kind_with_no_branch_is_refused_not_answered_as_a_failure():
    """An unnamed kind is a code fault, and it is refused where the fault is.

    Calling the remedy for a kind the tool cannot produce is the only way to reach
    the fall-through from a test, and it must not return prose: the reader would be
    told to go and fix a failure with a run id that belongs to no run.
    """
    mod = _load_module()
    with pytest.raises(ValueError):
        mod._remedy(1, "a_kind_added_without_a_branch", mod.Price(None))


def test_every_stale_kind_is_routed_by_the_queue():
    """A kind with no row falls through to the rows below it, and those rows vote.

    `ancestry` is the one exception, and it is the module docstring's: the generic rows
    *are* its remedy. Every other kind has to be named by a row of its own, because the
    generic rows vote.
    """
    kinds = _kind_values(_parse(FRESHNESS))
    routed = _kinds_routed_by_the_queue(_parse(QUEUE))
    unrouted = kinds - routed - {GENERIC_KIND}
    assert not unrouted, (
        "every stale kind needs a row of its own in `review-queue.py`'s `next_action`, "
        "or it falls through to a row that votes; unrouted: " + repr(sorted(unrouted))
    )
