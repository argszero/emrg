"""Tests for `scripts/check-undefined-names.py` -- the bound-nowhere guard.

Issue #1990. The class it exists for left a `NameError` on every Enter in
`emrg/client/app.py` while 4401 tests, the import check and `emrg --help` all passed,
because nothing in this repository evaluates a name that is bound nowhere. Its first run
found a live instance instead of a hypothetical one -- `emrg/server/daemon.py`'s
`build_shell_tool` calls `SandboxConfig()`, which its own import list does not carry --
and that is why both halves matter: a tree where it must fire, a tree where it must stay
silent, and the verdict on the checkout this suite lives in.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-undefined-names.py"


def _tree(tmp_path: Path, **files: str) -> Path:
    """A directory shaped like this project, so the script scans it.

    `emrg/` and `scripts/` are the two markers the guard's own-checkout resolution keys
    on, and `emrg/` is also a scanned root, so one file there is enough. Module names are
    given without their extension (`bad=`, not `bad.py=`), because a keyword argument
    cannot contain a dot.
    """
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "emrg").mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        (tmp_path / "emrg" / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return tmp_path


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=str(REPO_ROOT),
    )


def _reported(proc: subprocess.CompletedProcess) -> str:
    """The guard's report with separators normalised.

    `Finding.render` prints `path.relative_to(root)`, which renders with the platform's
    separator -- `emrg\\bad.py` on Windows. What these assertions are about is *which file
    and which lines* a finding names, never which separator this platform happens to use;
    the POSIX spelling passed here and failed `test-windows` (run 37923398190, measured
    2026-10-09), the same defect `tests/test_check_unbound_reads.py` records.
    """
    return proc.stdout.replace("\\", "/")


class TestItFires:
    def test_the_incident_shape_is_reported(self, tmp_path) -> None:
        """The shipped shape, reduced to two lines: a call, and no binding anywhere."""
        tree = _tree(
            tmp_path,
            bad="""\
            async def h():
                return new_task_id()
            """,
        )

        proc = _run(tree)

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "emrg/bad.py:2" in _reported(proc)
        assert "new_task_id" in proc.stdout
        assert "in h()" in proc.stdout

    def test_a_name_bound_only_in_another_scope_is_reported(self, tmp_path) -> None:
        """The precision the rule is bought for.

        A local of *another* function is not a binding this scope can see, so the read is
        a `NameError` even though the name does occur somewhere in the file. A guard that
        answered "is this name anywhere in the file" would pass this tree silently.
        """
        tree = _tree(
            tmp_path,
            bad="""\
            def setter():
                only_here = 1
                return only_here


            def reader():
                return only_here
            """,
        )

        proc = _run(tree)

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "emrg/bad.py:7" in _reported(proc)
        assert "in reader()" in proc.stdout

    def test_every_read_line_is_named_not_just_the_first(self, tmp_path) -> None:
        """Why the report lists lines rather than pointing at one.

        A postponed annotation reads a name without evaluating it, so the first read of
        the live `SandboxConfig` occurrence is `daemon.py:526` -- a line that looks
        harmless in a file whose every annotation is a string. The reader is owed the
        line that would actually raise, which is the second read.
        """
        tree = _tree(
            tmp_path,
            bad="""\
            from __future__ import annotations


            def build(thing: Missing = None):
                return thing or Missing()
            """,
        )

        proc = _run(tree)

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "emrg/bad.py:4, 5" in _reported(proc)


    def test_an_augmented_assignment_with_nothing_else_binding_it_is_reported(
        self, tmp_path
    ) -> None:
        """`x += 1` reads `x`; bound nowhere, the read can only raise (issue #2005).

        Three independent facts hid this shape, so it is pinned as three assertions'
        worth of behaviour rather than one: the target is `Store`, not `Load`;
        `symtable` calls it *assigned*; and `symtable` calls it **not referenced**.
        The line is named, because `del`/`+=` is where the lookup that finds nothing
        actually happens.
        """
        tree = _tree(
            tmp_path,
            bad="""\
            def f():
                counter += 1
            """,
        )

        proc = _run(tree)

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "emrg/bad.py:2" in _reported(proc)
        assert "counter" in proc.stdout

    def test_an_augmented_assignment_at_module_level_is_reported(self, tmp_path) -> None:
        """The same shape outside a function raises `NameError`, not `UnboundLocalError`."""
        tree = _tree(tmp_path, bad="total += 1\n")

        proc = _run(tree)

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "emrg/bad.py:1" in _reported(proc)
        assert "total" in proc.stdout

    def test_a_del_of_a_name_nothing_binds_is_reported(self, tmp_path) -> None:
        """`del x` looks `x` up to remove it, so an unbound `x` is a `NameError` too."""
        tree = _tree(
            tmp_path,
            bad="""\
            def f():
                del undefined_name
            """,
        )

        proc = _run(tree)

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "emrg/bad.py:2" in _reported(proc)
        assert "undefined_name" in proc.stdout

    def test_a_name_read_only_before_a_binding_is_still_reported(self, tmp_path) -> None:
        """The reopened gates must not silence an ordinary unbound read.

        `is_referenced` and `is_assigned` are consulted for every symbol, so a guard
        that let the `read_before_write` set widen the gate for names outside it
        would stop seeing the incident shape entirely.
        """
        tree = _tree(
            tmp_path,
            bad="""\
            def f():
                return undefined_plain_read
            """,
        )

        assert _run(tree).returncode == 1

class TestItStaysSilent:
    def test_a_module_level_binding_covers_every_read(self, tmp_path) -> None:
        tree = _tree(
            tmp_path,
            good="""\
            LIMIT = 3


            def f():
                return LIMIT
            """,
        )

        assert _run(tree).returncode == 0

    def test_an_enclosing_functions_binding_is_visible_to_a_nested_one(self, tmp_path) -> None:
        """A closure variable is a binding, and `symtable` says so."""
        tree = _tree(
            tmp_path,
            good="""\
            def outer():
                factor = 2

                def inner():
                    return factor

                return inner
            """,
        )

        assert _run(tree).returncode == 0

    @pytest.mark.parametrize(
        "source",
        [
            # a value established first, in the same scope
            "def f():\n    counter = 0\n    counter += 1\n",
            # a parameter
            "def f(counter):\n    counter += 1\n",
            # `global`: the declaration is the claim the module provides it
            "x = 0\ndef f():\n    global x\n    x += 1\n",
            # a for target
            "def f(items):\n    for total in items:\n        total += 1\n",
            # `with ... as`, and a `del` after it
            "with open('/dev/null') as fh:\n    del fh\n",
            # `except ... as`, and a `del` after it
            "try:\n    pass\nexcept OSError as exc:\n    del exc\n",
            # a walrus target
            "def f():\n    (n := 1)\n    n += 1\n",
            # a match capture
            "def f(v):\n    match v:\n        case [*rest]:\n            rest += [1]\n",
            # `nonlocal`: the enclosing function binds it
            "def outer():\n    c = 0\n    def inner():\n        nonlocal c\n        c += 1\n    return inner\n",
            # an import
            "import os\ndef f():\n    os.path.join('a', 'b')\n",
            # a plain assignment, then a del of it
            "x = 1\ndel x\n",
        ],
    )
    def test_a_real_binding_keeps_the_read_before_write_line_clean(
        self, tmp_path, source
    ) -> None:
        """The other direction: the set must not manufacture a finding.

        Every form here binds the name somewhere the rule can see, so the reopened
        gate (`is_referenced`, `is_assigned`) must not turn any of them into a
        finding. `x = 0; x += 1` is the shape the third fact would break if
        `_read_before_write_names` counted the augmented target itself as a binding.
        """
        tree = _tree(tmp_path, good=source)

        assert _run(tree).returncode == 0, source

    def test_parameters_locals_imports_and_builtins_are_bindings(self, tmp_path) -> None:
        tree = _tree(
            tmp_path,
            good="""\
            import json


            def f(given, *rest, **kw):
                local = json.dumps({})
                try:
                    pass
                except ValueError as exc:
                    local = str(exc)
                return len(given), rest, kw, local, print
            """,
        )

        assert _run(tree).returncode == 0

    def test_a_walrus_target_and_a_comprehension_are_bound_by_the_interpreter(self, tmp_path) -> None:
        tree = _tree(
            tmp_path,
            good="""\
            def f():
                return [doubled for x in range(3) if (doubled := x * 2)]
            """,
        )

        assert _run(tree).returncode == 0

    def test_a_class_body_reads_its_own_class_level_names(self, tmp_path) -> None:
        """A class scope is a scope: its own earlier bindings are visible inside it."""
        tree = _tree(
            tmp_path,
            good="""\
            class C:
                A = 1
                B = A + 1

                def method(self):
                    return self.A
            """,
        )

        assert _run(tree).returncode == 0

    def test_a_star_import_module_is_not_judged_and_says_so(self, tmp_path) -> None:
        """`from x import *` makes the module's names unenumerable, so it is exempt.

        Exempt is not the same as unmeasurable: the file is counted and named on the clean
        verdict, so a reader can see that the silence about it is a decision rather than a
        scan that stopped early. The readable file beside it is what makes this a clean
        reading rather than a refusal -- a tree whose every module is a star import is
        judged nothing, and the leg below pins that it says so.
        """
        tree = _tree(
            tmp_path,
            starred="""\
            from os.path import *

            print(new_task_id())
            """,
            ok="""\
            def f():
                return 1
            """,
        )

        proc = _run(tree)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "1 file(s) judged" in proc.stdout
        assert "1 file(s) not judged" in proc.stdout

    def test_a_tree_that_is_only_star_imports_is_not_a_clean_one(self, tmp_path) -> None:
        tree = _tree(
            tmp_path,
            starred="""\
            from os.path import *

            print(new_task_id())
            """,
        )

        proc = _run(tree)

        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stdout + proc.stderr


class TestTheVerdictOnThisCheckout:
    def test_the_tree_read_is_named_before_the_verdict(self) -> None:
        proc = _run(REPO_ROOT)

        assert proc.stdout.splitlines()[0] == f"tree: {REPO_ROOT}"

    def test_this_checkout_carries_no_unbound_read(self) -> None:
        """The half that was missing from the family's first guards.

        A guard correct in itself is not a guard if nothing reads its verdict on the tree
        being changed -- `check_nonlocal.py` reported the v0.3.6 crash for as long as it
        shipped and no CI job and no test ever looked at its exit code.
        """
        proc = _run(REPO_ROOT)

        assert proc.returncode == 0, (
            "the guard names a defect in this checkout:\n" + proc.stdout + proc.stderr
        )
        assert "OK:" in proc.stdout

    def test_the_report_and_the_exit_code_are_one_answer(self, tmp_path) -> None:
        """rc 1 must carry a finding line, and rc 0 must not."""
        bad = _tree(tmp_path, bad="def f():\n    return nowhere_at_all()\n")

        dirty = _run(bad)
        clean = _run(REPO_ROOT)

        assert (dirty.returncode, "ERROR:" in dirty.stdout) == (1, True)
        assert (clean.returncode, "ERROR:" in clean.stdout) == (0, False)


class TestTheRefusals:
    def test_an_empty_tree_is_not_a_clean_one(self, tmp_path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        proc = _run(empty)

        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stdout + proc.stderr

    def test_a_path_that_is_not_there_is_not_a_clean_one(self, tmp_path) -> None:
        proc = _run(tmp_path / "absent")

        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stdout + proc.stderr

    def test_a_file_that_will_not_parse_is_not_a_clean_reading(self, tmp_path) -> None:
        tree = _tree(tmp_path, broken="def f(:\n    pass\n")

        proc = _run(tree)

        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "UNMEASURABLE" in proc.stdout + proc.stderr
