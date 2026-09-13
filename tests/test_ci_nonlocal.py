"""Unit tests for scripts/check_nonlocal.py — verifies the AST-based
nonlocal integrity checker correctly identifies missing declarations.

Rant #31 part 3: CI step to prevent UnboundLocalError from new state
variables added to `interactive` without nonlocal in `handle_key`.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

# Import directly (the script is structured as a module-friendly file)
sys_path = Path(__file__).resolve().parent.parent / "scripts"
import sys
sys.path.insert(0, str(sys_path))
import check_nonlocal


class TestAssignedNames:
    def test_simple_assignment(self) -> None:
        src = textwrap.dedent("""\
            a = 1
            b = 2
        """)
        tree = ast.parse(src)
        names = check_nonlocal._assigned_names(tree)
        assert names == {"a", "b"}

    def test_tuple_unpacking(self) -> None:
        src = textwrap.dedent("""\
            x, y = 1, 2
            a, (b, c) = [], (1, 2)
        """)
        tree = ast.parse(src)
        names = check_nonlocal._assigned_names(tree)
        assert names == {"x", "y", "a", "b", "c"}

    def test_ann_assign(self) -> None:
        src = textwrap.dedent("""\
            x: int = 1
            y: str
        """)
        tree = ast.parse(src)
        names = check_nonlocal._assigned_names(tree)
        assert names == {"x", "y"}

    def test_aug_assign(self) -> None:
        src = "x += 1"
        tree = ast.parse(src)
        names = check_nonlocal._assigned_names(tree)
        assert "x" in names

    def test_walrus(self) -> None:
        src = "if (x := 1): pass"
        tree = ast.parse(src)
        names = check_nonlocal._assigned_names(tree)
        assert "x" in names

    def test_skips_nested_function_locals(self) -> None:
        src = textwrap.dedent("""\
            outer_var = 1
            def inner():
                inner_var = 2
        """)
        tree = ast.parse(src)
        names = check_nonlocal._assigned_names(tree)
        # inner_var should be excluded (it's inside a nested function)
        assert names == {"outer_var"}


class TestWrittenNames:
    def test_store_ctx_only(self) -> None:
        src = textwrap.dedent("""\
            x = 1   # Store
            print(y)  # Load — should NOT appear
        """)
        tree = ast.parse(src)
        names = check_nonlocal._written_names(tree)
        assert names == {"x"}

    def test_excludes_parameters(self) -> None:
        src = textwrap.dedent("""\
            def fn(a, b):
                a = a + 1  # Store to parameter 'a'
                c = b       # Load 'b', store 'c'
        """)
        tree = ast.parse(src)
        # The function body should exclude 'a' (parameter)
        fn_node = tree.body[0]
        names = check_nonlocal._written_names(fn_node)
        assert "a" not in names  # excluded as parameter
        assert "c" in names

    def test_excludes_nonlocal_declarations(self) -> None:
        src = textwrap.dedent("""\
            def outer():
                x = 1
                def inner():
                    nonlocal x
                    x = 2
        """)
        tree = ast.parse(src)
        outer = tree.body[0]
        inner = outer.body[1]
        names = check_nonlocal._written_names(inner)
        # 'x' is declared nonlocal → excluded
        assert "x" not in names


class TestFindInteractiveBody:
    def test_finds_async_interactive(self) -> None:
        src = textwrap.dedent("""\
            async def foo():
                pass
            async def interactive():
                pass
            def bar():
                pass
        """)
        tree = ast.parse(src)
        fn = check_nonlocal._find_interactive_body(tree)
        assert fn is not None
        assert fn.name == "interactive"

    def test_returns_none_when_missing(self) -> None:
        src = "x = 1"
        tree = ast.parse(src)
        fn = check_nonlocal._find_interactive_body(tree)
        assert fn is None


class TestFindInnerFn:
    def test_finds_nested_function(self) -> None:
        src = textwrap.dedent("""\
            async def interactive():
                async def handle_key(data):
                    pass
                async def read_server():
                    pass
        """)
        tree = ast.parse(src)
        interactive = tree.body[0]
        hk = check_nonlocal._find_inner_fn(interactive.body, "handle_key")
        assert hk is not None
        assert hk.name == "handle_key"

    def test_returns_none_when_missing(self) -> None:
        src = textwrap.dedent("""\
            async def interactive():
                x = 1
        """)
        tree = ast.parse(src)
        interactive = tree.body[0]
        hk = check_nonlocal._find_inner_fn(interactive.body, "handle_key")
        assert hk is None


class TestEndToEnd:
    """Tests check_nonlocal() against synthetic source to verify real detection."""

    def test_missing_nonlocal_detected(self, tmp_path: Path) -> None:
        app_py = tmp_path / "app.py"
        app_py.write_text(textwrap.dedent("""\
            async def interactive():
                state_var = False

                async def handle_key(data):
                    x = 1
                    state_var = True
        """))
        rc = check_nonlocal.check_nonlocal(str(app_py))
        # Should detect state_var is missing nonlocal
        assert rc == 1

    def test_correct_nonlocal_passes(self, tmp_path: Path) -> None:
        app_py = tmp_path / "app.py"
        app_py.write_text(textwrap.dedent("""\
            async def interactive():
                state_var = False

                async def handle_key(data):
                    nonlocal state_var
                    state_var = True
        """))
        rc = check_nonlocal.check_nonlocal(str(app_py))
        assert rc == 0

    def test_local_temp_not_flagged(self, tmp_path: Path) -> None:
        """read_server's local `data` and `line` should be whitelisted."""
        app_py = tmp_path / "app.py"
        app_py.write_text(textwrap.dedent("""\
            async def interactive():
                data = None
                line = None

                async def read_server():
                    data = {"key": "val"}
                    line = "hello"
        """))
        rc = check_nonlocal.check_nonlocal(str(app_py))
        assert rc == 0

    def test_selector_state_consolidation_caught(self, tmp_path: Path) -> None:
        """Simulate the rant #31 scenario: adding a new selector variable
        without nonlocal declaration."""
        app_py = tmp_path / "app.py"
        app_py.write_text(textwrap.dedent("""\
            async def interactive():
                # New selector state added
                model_selector_active = False
                model_selector_widget = None

                async def handle_key(data):
                    nonlocal busy
                    busy = False
                    model_selector_active = False  # BUG: missing nonlocal
                    model_selector_widget = None   # BUG: missing nonlocal
        """))
        rc = check_nonlocal.check_nonlocal(str(app_py))
        assert rc == 1

    def test_nothing_to_evolve_passes(self, tmp_path: Path) -> None:
        """When all state is in SelectorState objects, only the selector
        instances themselves need nonlocal — their attributes don't."""
        app_py = tmp_path / "app.py"
        app_py.write_text(textwrap.dedent("""\
            async def interactive():
                sel = SelectorState()

                async def handle_key(data):
                    nonlocal sel
                    sel.active = False  # attribute mutation, not rebinding
                    sel.widget = None
        """))
        rc = check_nonlocal.check_nonlocal(str(app_py))
        assert rc == 0


# ---------------------------------------------------------------------------
# Which tree does the check inspect?
#
# Measured 2026-09-11, in exactly the situation this tool is used in: unblocking
# a PR means working in a git worktree, and the natural invocation there is
# `<worktree>/.venv/bin/python <main-checkout>/scripts/check_nonlocal.py`. The
# old root was `Path(__file__).resolve().parent.parent` — the *main* checkout.
#
# Reproduced live: with the worktree's `interactive` renamed away, the worktree's
# own copy printed "ERROR: could not find interactive function in app.py" and
# exited 2, while the main checkout's copy run from that same directory printed
# "OK: nonlocal integrity check passed" and exited 0. Both lines are confident;
# the OK line is also byte-identical to what a correct run prints, so the wrong
# answer is indistinguishable from the right one by reading the output.
# ---------------------------------------------------------------------------


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_nonlocal.py"


def _fake_checkout(root: Path) -> Path:
    """A directory that has the two markers `_resolve_root` keys on."""
    (root / "scripts").mkdir(parents=True)
    target = root / check_nonlocal.TARGET
    target.parent.mkdir(parents=True)
    target.write_text(
        "async def interactive():\n"
        "    state = 0\n"
        "\n"
        "    async def handle_key(data):\n"
        "        nonlocal state\n"
        "        state = 1\n",
        encoding="utf-8",
    )
    return root


def test_the_tree_is_the_checkout_you_are_standing_in(monkeypatch, tmp_path):
    """The defect: the inspected tree came from `__file__`, not from the cwd.

    Pinned on the predicate, not on the printed line: `_resolve_root` is the
    decision, and a test that only made `main()` print the right path could pass
    while the wrong root was still chosen.
    """
    fake = _fake_checkout(tmp_path / "checkout")
    monkeypatch.chdir(fake)
    assert check_nonlocal._resolve_root() == fake.resolve(), (
        "the tool must inspect the checkout the caller is standing in; deriving "
        "the root from __file__ inspects a different tree (the one the script "
        "happens to live in) and reports its verdict as if it were yours"
    )
    assert check_nonlocal._resolve_root() != SCRIPT.parent.parent, (
        "the fixture must not be the script's own root, or this test proves nothing"
    )


def test_the_inspected_tree_is_named_in_the_output(monkeypatch, tmp_path, capsys):
    """`which tree did you inspect` must never be ambiguous.

    Naming the tree turns the silent wrong answer above into a visible one.
    """
    fake = _fake_checkout(tmp_path / "checkout")
    monkeypatch.chdir(fake)
    monkeypatch.setattr(check_nonlocal, "REPO_ROOT", check_nonlocal._resolve_root())
    assert check_nonlocal.main([]) == 0
    out = capsys.readouterr().out
    assert f"tree: {fake.resolve()}" in out, out


def test_a_directory_that_is_not_a_checkout_falls_back_to_the_script_root(
    monkeypatch, tmp_path
):
    """The documented invocation must keep working from anywhere.

    `python3 scripts/check_nonlocal.py` runs from the repo root in every hint,
    but an absolute-path call from elsewhere (a wrapper, an editor task, `git -C`)
    has no checkout in the cwd to stand in — it must fall back, not fail.
    """
    monkeypatch.chdir(tmp_path)  # bare temp dir: no app.py, no scripts/
    assert check_nonlocal._resolve_root() == SCRIPT.parent.parent.resolve()


def test_a_directory_with_only_half_the_shape_is_not_a_checkout(monkeypatch, tmp_path):
    """Both markers are required, so a stray app.py cannot claim the tree.

    The predicate is a heuristic for "this is a checkout of this project";
    requiring the scripts directory too keeps a report directory that happens to
    contain an app.py from silently becoming the measured tree.
    """
    target = tmp_path / check_nonlocal.TARGET
    target.parent.mkdir(parents=True)
    target.write_text("async def interactive():\n    pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert check_nonlocal._resolve_root() == SCRIPT.parent.parent.resolve()
