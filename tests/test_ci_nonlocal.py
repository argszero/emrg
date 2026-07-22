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
