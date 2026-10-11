"""The skip policy reads **names**, and `..` is not one.

Measured 2026-10-11 (`cyc20261011-091039`) on master `0b5f502f`, in this checkout,
workdir `<checkout>/emrg` and pattern `../tests/*.py` — the spelling a caller uses to
name a *sibling* directory:

    glob  -> "No files matched pattern '../tests/*.py' in <root> - 204 path(s) matched
             it but were skipped as hidden or ignored …"
    grep  -> "No matches for … in <root> (searched 0 files) matching '../tests/*.py'"

Both tools had walked the tree and found the 204 files (`tests/*.py` from the root
returns the same set). The skip policy then dropped every one of them, because both
copies asked `part.startswith(".")` over `path.relative_to(root).parts` and `".."`
starts with `"."`. A relative-path **component** was read as if it were a name.

So this module pins the rule in both directions: a `..` (or `.`) component does not
make a path hidden, and every other half of the policy — a dot-name, the four noise
directories, `.emrg`'s exemption — still holds exactly as before. The unit legs are on
`base.is_hidden_or_ignored`; the tool legs go through `glob` and `grep` because that is
where the false negative was answered.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from emrg.tools import base as base_module
from emrg.tools.glob_tool import GlobTool
from emrg.tools.grep_tool import GrepTool

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A root holding a sibling of `sub`, plus one of each thing the policy skips.

    `sub` is where the caller stands, so `../sibling/*.py` is the only way to name the
    files beside it — the shape the blind spot answered "nothing here" about.
    """
    (tmp_path / "sibling").mkdir()
    (tmp_path / "sibling" / "one.py").write_text("def one():\n    return 1\n")
    (tmp_path / "sibling" / ".hidden.py").write_text("def hidden():\n    return 0\n")
    (tmp_path / "sibling" / "nested").mkdir()
    (tmp_path / "sibling" / "nested" / "two.py").write_text("def two():\n    return 2\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "here.py").write_text("def here():\n    return 3\n")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "compiled.pyc").write_text("")
    (tmp_path / ".emrg" / "memory").mkdir(parents=True)
    (tmp_path / ".emrg" / "memory" / "MEMORY.md").write_text("# index\n")
    return tmp_path


def _run(coro):
    return asyncio.run(coro)


def _policy(path: Path, root: Path) -> bool:
    """The skip policy, read off the module so a missing home is a leg's failure.

    A `from … import` of a name the tree does not have yet fails at collection, which
    reports one error for the whole file; reached this way, each leg on the unfixed
    tree fails on its own reason — the tool legs on the false negative itself.
    """
    return base_module.is_hidden_or_ignored(path, root)


def _listed(result_content: str) -> list[str]:
    """The paths a `Found N matches` answer lists, without its header."""
    return [
        line.strip()
        for line in result_content.splitlines()
        if line.startswith("  ") and line.strip()
    ]


# --- the unit: what the rule reads -------------------------------------------------


def test_a_dotdot_component_is_not_a_hidden_name(tree: Path):
    """The blind spot itself: `..` starts with `.` and is not a name."""
    assert not _policy(tree / "sub" / ".." / "sibling" / "one.py", tree / "sub")


def test_a_dot_component_never_reaches_the_policy(tree: Path):
    """Why the rule handles `..` and not `.`: pathlib removes the second one.

    This leg replaces one that asserted `not _policy(tree / "." / "sibling" / "one.py",
    tree)` — and a mutation arm (A2, 2026-10-11 `cyc20261011-091039`) that deleted `.`
    from the rule left it green, because `Path` had already dropped the component before
    the predicate saw it: `Path("a/./b").parts == ("a", "b")`, so the assertion was
    about a path with no `.` in it at all. What is worth pinning is the premise itself,
    since a `.` member of the rule is dead code for exactly this reason.
    """
    assert Path("a/./b").parts == ("a", "b")
    assert (tree / "sub" / "." / "one.py").parts[-2:] == ("sub", "one.py")


def test_a_dotdot_does_not_excuse_a_real_hidden_component(tree: Path):
    """The other direction: `..` is dropped from the reading, not from the path."""
    assert _policy(
        tree / "sub" / ".." / "sibling" / ".hidden.py", tree / "sub"
    )


def test_a_noise_directory_under_a_dotdot_is_still_skipped(tree: Path):
    assert _policy(
        tree / "sub" / ".." / "node_modules" / "pkg" / "index.js", tree / "sub"
    )


def test_the_emrg_exception_survives_a_dotdot_component(tree: Path):
    assert not _policy(
        tree / "sub" / ".." / ".emrg" / "memory" / "MEMORY.md", tree / "sub"
    )


@pytest.mark.parametrize(
    "relative",
    ["sibling/one.py", "sibling/nested/two.py"],
)
def test_a_plain_path_is_kept(tree: Path, relative: str):
    assert not _policy(tree / relative, tree)


@pytest.mark.parametrize("relative", ["__pycache__/compiled.pyc", "sibling/.hidden.py"])
def test_each_half_of_the_policy_still_skips(tree: Path, relative: str):
    assert _policy(tree / relative, tree)


# --- the tools: where the false negative was answered ------------------------------


def test_glob_reaches_the_sibling_directory_a_dotdot_pattern_names(tree: Path):
    result = _run(
        GlobTool().execute({"pattern": "../sibling/*.py", "workdir": str(tree / "sub")})
    )
    assert not result.error, result.content
    assert "Found 1 matches" in result.content, result.content
    assert "../sibling/one.py" in result.content, result.content
    # …and the file that really is hidden is still not read, by the same rule.
    assert ".hidden.py" not in result.content, result.content


def test_the_skip_note_counts_the_one_path_that_really_is_hidden(tree: Path):
    """The false cause, named: the note's subject is the file that *is* hidden.

    On master the same answer said all 204 paths had been skipped as hidden. Here the
    same sentence has to name exactly the one file in `../sibling` that is hidden, and
    the answer must not be the `No files matched` the blind spot produced.
    """
    result = _run(
        GlobTool().execute({"pattern": "../sibling/*.py", "workdir": str(tree / "sub")})
    )
    assert "No files matched" not in result.content, result.content
    assert "1 path(s) also matched but were skipped as hidden or ignored" in result.content, (
        result.content
    )
    assert "../sibling/one.py" in result.content, result.content


def test_grep_searches_the_sibling_directory_a_dotdot_pattern_names(tree: Path):
    result = _run(
        GrepTool().execute(
            {
                "pattern": "def one",
                "path": str(tree / "sub"),
                "glob": "../sibling/*.py",
            }
        )
    )
    assert not result.error, result.content
    assert "Found 1 matches" in result.content, result.content
    assert "searched 1 files" in result.content, result.content


def test_both_readings_of_the_same_tree_agree(tree: Path):
    """The differential: the `..` spelling and the rooted spelling are one reading.

    A repair that only moved the `..` case from "nothing" to "something" could still
    have answered a *different* set, so the two spellings are compared rather than each
    being asserted against a hand-written list.
    """
    from_root = _run(
        GlobTool().execute({"pattern": "sibling/*.py", "workdir": str(tree)})
    )
    through_dotdot = _run(
        GlobTool().execute({"pattern": "../sibling/*.py", "workdir": str(tree / "sub")})
    )
    assert _listed(through_dotdot.content) == [
        "../" + path for path in _listed(from_root.content)
    ], (from_root.content, through_dotdot.content)


def test_the_noise_half_of_the_policy_still_answers_through_a_dotdot(tree: Path):
    """The half the fix must not have neutered: node_modules stays skipped.

    Read against its sibling in the same shape — one `..` pattern that must be dropped
    by the policy, one that must not — because the skip note alone is printed on the
    unfixed tree too, for the wrong reason, and a leg that cannot distinguish the two
    trees would hold nothing.
    """
    noise = _run(
        GlobTool().execute(
            {"pattern": "../node_modules/*/index.js", "workdir": str(tree / "sub")}
        )
    )
    assert not noise.error, noise.content
    assert "No files matched" in noise.content, noise.content
    assert "1 path(s) matched it but were skipped as hidden or ignored" in noise.content, (
        noise.content
    )

    same_shape_but_kept = _run(
        GlobTool().execute({"pattern": "../sibling/*.py", "workdir": str(tree / "sub")})
    )
    assert "Found 1 matches" in same_shape_but_kept.content, same_shape_but_kept.content


# --- the rule has one home ----------------------------------------------------------


def test_both_tools_ask_the_one_home():
    """`glob` and `grep` each call `base.is_hidden_or_ignored`, and neither defines one.

    The two spellings drifted into the same blind spot once; a third copy is what this
    leg is for. It reads the call site rather than the name in prose, so a module that
    merely mentions the function in a docstring does not satisfy it.
    """
    for module in ("glob_tool.py", "grep_tool.py"):
        source = (REPO_ROOT / "emrg" / "tools" / module).read_text(encoding="utf-8")
        tree_ast = ast.parse(source)
        calls = {
            node.func.id
            for node in ast.walk(tree_ast)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "is_hidden_or_ignored" in calls, f"{module} does not ask the one home"
        defined = {
            node.name
            for node in ast.walk(tree_ast)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert "is_hidden_or_ignored" not in defined, f"{module} defines a second home"
