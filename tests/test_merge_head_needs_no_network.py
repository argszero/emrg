"""A PR head this clone already holds is read without the network (2026-11-12).

Background (cycle `cyc20261112-115744`)
---------------------------------------
Every gate in the `check-merge-*` family reads a PR head by fetching
`refs/pull/<N>/head`. A fetch is a network operation, so on a host whose git
transport is down each of them answers "could not measure" — and this host has been
in exactly that state while the cycle that needed the reading had the same commit
resident as a local branch.

`merge_tree.head_commit_already_here` is the cheap path: the sha comes from
`gh pr view <N>` (so it is the PR's head by definition, not whatever a local ref
happens to say) and the object store is asked whether that commit is present.

Two tests, in the two directions the property has:

* **both carriers** consult it — read structurally, because the failure this guards
  is one carrier being wired and the other not, and a behavioural test on one script
  cannot see the other at all;
* **the helper itself** answers `None` for every way the cheap path is unavailable,
  so a caller always falls through to the fetch it already had.
"""

import ast
import pathlib
import subprocess

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
CARRIERS = ("check-merge-plan-suite.py", "check-merge-sequence.py")


def _fetch_head_body(script: str) -> ast.FunctionDef:
    tree = ast.parse((SCRIPTS / script).read_text(encoding="utf-8"))
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_fetch_head"
    ]
    assert len(found) == 1, f"{script} carries {len(found)} _fetch_head definitions"
    return found[0]


@pytest.mark.parametrize("script", CARRIERS)
def test_the_fetch_head_asks_the_object_store_before_the_network(script: str) -> None:
    """Each carrier's fetch site must try the resident commit first.

    The sibling gate `check-merge-pairs.py` reads `seq._fetch_head`, so those two
    carriers are the whole family for this rule and both are read here: wiring one and
    leaving the other is the defect, and it looks identical from the wired side.
    """
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(_fetch_head_body(script))
        if isinstance(node, ast.Call)
    }
    assert "head_commit_already_here" in called, (
        f"{script}'s _fetch_head goes straight to the network: {sorted(called)}"
    )


def _loaded():
    """The real `scripts/merge_tree.py`, loaded under its own name.

    Registered in `sys.modules` before it executes, because `merge_tree` defines
    dataclasses and `dataclasses` resolves a field's annotations through
    `sys.modules[cls.__module__]` — an unregistered load raises `AttributeError` from
    the stdlib rather than running the module, which reads like a defect in the
    subject.
    """
    import importlib.util
    import sys

    name = "_merge_tree_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "merge_tree.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class _Proc:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_the_helper_answers_none_for_every_way_the_cheap_path_is_closed() -> None:
    """Three ways out, and each ends at the fetch rather than at a wrong answer.

    A helper that raised, or that answered a sha it had not confirmed, would turn a
    network problem into a gate verdict — the failure this family exists to prevent.
    """
    mod = _loaded()
    sha = "a" * 40

    def run_factory(gh_rc: int, gh_out: str, cat_rc: int):
        def run(argv, **kw):
            if argv[0] == "gh":
                return _Proc(gh_rc, gh_out)
            assert argv[:3] == ["git", "cat-file", "-e"], argv
            return _Proc(cat_rc)

        return run

    assert mod.head_commit_already_here(7, run=run_factory(1, "", 0)) is None, (
        "the API could not be asked, and the helper answered a commit anyway"
    )
    assert mod.head_commit_already_here(7, run=run_factory(0, "not-a-sha\n", 0)) is None, (
        "a non-sha answer was treated as a head"
    )
    assert mod.head_commit_already_here(7, run=run_factory(0, sha + "\n", 1)) is None, (
        "a commit this clone does not have was reported as present"
    )
    assert mod.head_commit_already_here(7, run=run_factory(0, sha + "\n", 0)) == sha


def test_the_helper_checks_the_object_as_a_commit() -> None:
    """`cat-file -e <sha>^{commit}` peels, so an annotated tag is not accepted as a head."""
    mod = _loaded()
    seen: list[list[str]] = []

    def run(argv, **kw):
        seen.append(list(argv))
        return _Proc(0, "b" * 40 + "\n") if argv[0] == "gh" else _Proc(0)

    mod.head_commit_already_here(9, run=run)
    cat = [c for c in seen if c[0] == "git"]
    assert cat and cat[0][-1].endswith("^{commit}"), cat
