"""Tests for `scripts/check_unbound_reads.py` -- the read-before-binding guard.

Issue #1759: v0.3.6's TUI crashed on every Enter because `handle_key` read and
rebound `_approval_pending` without declaring it `nonlocal`, so the name was
local by assignment and the read raised `UnboundLocalError`. The guard exists to
keep that class out of the tree, so these tests are in two halves: a built tree
where it must fire and a built tree where it must stay silent, plus the verdict
on the checkout this suite lives in -- the half that was missing from the
neighbouring guard and let the crash ship.

`cyc20261003-065537` added the guard's **second** rule, BOUND-NOWHERE -- a name
read *inside a scope* that nothing in the module binds at all, which is a
`NameError` rather than an `UnboundLocalError`. Its own defect was
`emrg/server/daemon.py::build_shell_tool` writing `sandbox_config or
SandboxConfig()` with `SandboxConfig` never imported (b92ed00d, 2026-09-23): the
documented default path raised `NameError` and every caller in the tree passed a
config explicitly, so nothing ever hit it. Those tests are in
`TestTheBoundNowhereRule`; the third half of its design is that an *annotation*
is only a read where Python evaluates it, and PEP 526 was measured for that
rather than assumed -- see the annotation cases below.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_unbound_reads.py"


def _tree(tmp_path: Path, **files: str) -> Path:
    """A directory shaped like this project, so the script scans it.

    `emrg/` and `scripts/` are the two markers `_resolve_root` keys on, and
    `emrg/` is also a scanned directory, so one file there is enough. Module
    names are given without their extension (`bad=`, not `bad=`), because a
    keyword argument cannot contain a dot.
    """
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "emrg").mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        target = tmp_path / "emrg" / f"{name}.py"
        target.write_text(textwrap.dedent(source), encoding="utf-8")
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


class TestItFires:
    def test_a_read_above_the_binding_is_reported(self, tmp_path) -> None:
        """The exact shape of the shipped crash, reduced to two lines."""
        tree = _tree(
            tmp_path,
            bad="""\
            def f():
                print(flag)
                flag = 1
            """,
        )
        proc = _run(tree)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        # The guard prints `path.relative_to(root)`, which renders with the
        # platform's separator -- `emrg\bad.py` on Windows. Asserting the
        # POSIX spelling passed here and failed `test-windows` (measured
        # 2026-09-29, run 36644266639): the subject is "which file and line did
        # it name", never which separator this platform uses. Normalise both.
        reported = proc.stdout.replace("\\", "/")
        assert "emrg/bad.py:2" in reported, proc.stdout
        assert "in f()" in proc.stdout, proc.stdout
        assert "'flag'" in proc.stdout, proc.stdout
        assert "before its first binding at 3" in proc.stdout, proc.stdout

    def test_the_nested_function_shape_from_the_issue_is_reported(self, tmp_path) -> None:
        """A nested function reading an outer name it also writes: the real one.

        `handle_key` is nested in `interactive`, and the name it broke on is
        assigned by `interactive` -- so a guard that only ever looked at
        top-level functions would have missed it.
        """
        tree = _tree(
            tmp_path,
            bad="""\
            def outer():
                state = None

                def inner():
                    if state is not None:
                        return True
                    state = 1
                    return False
            """,
        )
        proc = _run(tree)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "in inner()" in proc.stdout, proc.stdout
        assert "'state'" in proc.stdout, proc.stdout

    def test_a_tree_with_two_findings_reports_both(self, tmp_path) -> None:
        tree = _tree(
            tmp_path,
            one="""\
            def f():
                return total
                total = 0
            """,
            two="""\
            def g():
                return other
                other = 0
            """,
        )
        proc = _run(tree)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "'total'" in proc.stdout and "'other'" in proc.stdout, proc.stdout
        assert "2 name(s)" in proc.stdout, proc.stdout


class TestItStaysSilent:
    @pytest.mark.parametrize(
        "label,source",
        [
            (
                "binding above the read",
                "def f():\n    flag = 1\n    print(flag)\n",
            ),
            (
                "a parameter",
                "def f(flag):\n    print(flag)\n",
            ),
            (
                "declared nonlocal",
                "def outer():\n"
                "    flag = 1\n"
                "    def inner():\n"
                "        nonlocal flag\n"
                "        print(flag)\n"
                "        flag = 2\n",
            ),
            (
                "declared global",
                "def f():\n"
                "    global flag\n"
                "    print(flag)\n"
                "    flag = 2\n",
            ),
            (
                "a for target",
                "def f(items):\n    for flag in items:\n        print(flag)\n",
            ),
            (
                "a with target",
                "def f(path):\n    with open(path) as handle:\n        print(handle)\n",
            ),
            (
                "an except target",
                "def f():\n    try:\n        pass\n    except ValueError as exc:\n"
                "        print(exc)\n",
            ),
            (
                "an import",
                "def f():\n    import json\n    print(json.dumps({}))\n",
            ),
            (
                "a lambda parameter",
                # `rants.py` and `check-vote-count.py` both sort with this shape,
                # and the first version of this guard read the lambda's `r` as
                # the enclosing function's read of the loop variable below.
                "def f(rows):\n"
                "    rows.sort(key=lambda r: r.get('at'))\n"
                "    for r in rows:\n"
                "        print(r)\n",
            ),
            (
                "a comprehension target",
                # The comprehension's own `i` is not a binding of `f`, so the
                # module-level `i` read above it must not be reported as a read
                # of an unbound local. Without the exemption this is a false
                # positive -- and a guard that cries wolf gets switched off.
                "i = 0\n\n"
                "def f(xs):\n"
                "    print(i)\n"
                "    rows = [i for i in xs]\n"
                "    return rows\n",
            ),
            (
                "a walrus inside a comprehension",
                # Two tests in this repository use exactly this shape: the `if`
                # clause runs before the pair that reads the name.
                "def f(items, check):\n"
                "    return {name: found for name, text in items.items()\n"
                "            if (found := check(text))}\n",
            ),
        ],
    )
    def test_a_correct_binding_is_not_reported(self, tmp_path, label, source) -> None:
        tree = _tree(tmp_path, good=source)
        proc = _run(tree)
        assert proc.returncode == 0, (
            f"the guard reported {label} as a defect:\n{proc.stdout}{proc.stderr}"
        )


class TestTheBoundNowhereRule:
    """The second rule: a name nothing in the module binds, read where it runs."""

    def test_a_name_bound_nowhere_is_reported(self, tmp_path) -> None:
        """The shipped shape, reduced: a call to a name that was never imported."""
        tree = _tree(
            tmp_path,
            bad="""\
            def build():
                return SandboxConfig()
            """,
        )
        proc = _run(tree)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        reported = proc.stdout.replace("\\", "/")
        assert "BOUND-NOWHERE" in reported, proc.stdout
        assert "emrg/bad.py:2" in reported, proc.stdout
        assert "in build()" in reported, proc.stdout
        assert "'SandboxConfig'" in reported, proc.stdout

    @pytest.mark.parametrize(
        "label,source",
        [
            (
                "an import",
                "import os\n\ndef f():\n    return os.sep\n",
            ),
            (
                "a parameter",
                "def f(thing):\n    return thing\n",
            ),
            (
                "a binding in an enclosing function",
                "def outer():\n    thing = 1\n    def inner():\n        return thing\n",
            ),
            (
                "a module-level assignment",
                "thing = 1\n\ndef f():\n    return thing\n",
            ),
            (
                "a name in a `TYPE_CHECKING` import",
                "from __future__ import annotations\n"
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from pathlib import Path\n"
                "\ndef f(p: Path) -> Path:\n"
                "    return p\n",
            ),
        ],
    )
    def test_a_name_something_binds_is_not_reported(self, tmp_path, label, source) -> None:
        tree = _tree(tmp_path, good=source)
        proc = _run(tree)
        assert proc.returncode == 0, (
            f"the guard reported {label} as bound nowhere:\n{proc.stdout}{proc.stderr}"
        )

    # ── annotations: read only where Python evaluates them ──────────────────
    #
    # PEP 526, measured on this checkout (see `_annotation_ids`):
    #   module level  `x: T = 1`      evaluated   (unless `from __future__ import annotations`)
    #   class body    `class C: x: T` evaluated   (unless lazy)
    #   local         `def f(): x: T` **never evaluated**, lazy or not
    # The local half is not symmetry for its own sake: `emrg/server/daemon.py`
    # has read `Any` since #271 without importing it and run for months, because
    # both of its uses are local annotations. A rule that called that a NameError
    # would be wrong about the tree it just measured, and a guard that is wrong
    # once is switched off.

    def test_a_local_annotation_is_never_a_read(self, tmp_path) -> None:
        tree = _tree(
            tmp_path,
            good="def f():\n    ctx: NoSuchName = {}\n    return ctx\n",
        )
        proc = _run(tree)
        assert proc.returncode == 0, (
            "a local variable annotation is never evaluated (PEP 526), so naming an "
            f"unbound type there is not a NameError:\n{proc.stdout}{proc.stderr}"
        )

    def test_a_local_annotation_does_not_mask_a_real_read(self, tmp_path) -> None:
        """The control: the same name read for real *is* still reported."""
        tree = _tree(
            tmp_path,
            bad="def f():\n    ctx: NoSuchName = {}\n    return NoSuchName\n",
        )
        proc = _run(tree)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "'NoSuchName'" in proc.stdout, proc.stdout

    def test_a_class_body_annotation_is_evaluated(self, tmp_path) -> None:
        """A class body stores its annotations in `__annotations__`, so it reads."""
        tree = _tree(
            tmp_path,
            bad="class C:\n    field: NoSuchName = 1\n",
        )
        proc = _run(tree)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "'NoSuchName'" in proc.stdout, proc.stdout
        assert "in the body of class C" in proc.stdout, proc.stdout

    def test_a_class_body_annotation_is_lazy_when_the_module_is(self, tmp_path) -> None:
        tree = _tree(
            tmp_path,
            good="from __future__ import annotations\n\nclass C:\n    field: NoSuchName = 1\n",
        )
        proc = _run(tree)
        assert proc.returncode == 0, (
            f"a lazy module does not evaluate its class annotations:\n{proc.stdout}{proc.stderr}"
        )

    def test_a_class_body_read_is_reported(self, tmp_path) -> None:
        tree = _tree(
            tmp_path,
            bad="class C:\n    field = NoSuchName\n",
        )
        proc = _run(tree)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "in the body of class C" in proc.stdout, proc.stdout

    def test_a_property_setter_decorator_is_not_a_read_of_the_enclosing_function(
        self, tmp_path
    ) -> None:
        """`@x.setter` reads `x` from the *class* body, where it is already bound.

        The first version of this rule attributed that read to the decorated
        function and reported 22 findings on this checkout, every one of them
        correct code.
        """
        tree = _tree(
            tmp_path,
            good="""\
            class C:
                @property
                def p(self):
                    return 1

                @p.setter
                def p(self, value):
                    self._p = value
            """,
        )
        proc = _run(tree)
        assert proc.returncode == 0, (
            f"the class body's own `@p.setter` read was misattributed:\n"
            f"{proc.stdout}{proc.stderr}"
        )

    def test_a_star_import_makes_the_reading_unmeasurable(self, tmp_path) -> None:
        """`2` is "could not measure", never a clean tree (R2's rule, in a guard)."""
        tree = _tree(tmp_path, good="from os import *\n\ndef f():\n    return path\n")
        proc = _run(tree)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "not measured" in proc.stderr, proc.stderr
        assert "OK" not in proc.stdout, proc.stdout


class TestATreeItCannotRead:
    """A root that is not this project's tree is `could not measure`, never `OK`.

    Measured 2026-10-03 (`cyc20261003-073709`), after it happened *during a cycle*:
    a relative `--root` was resolved twice -- the caller had `cd`-ed into it first --
    and the guard answered `OK` about a directory that does not exist. It could,
    because `scan` finds no directory, returns no findings, and **no findings is
    exactly what a clean tree returns**. Naming the tree is half the family's rule;
    this is the half that says the tree it named is one it can read.

    The population was measured rather than assumed: over the `scripts/` guards that
    take a filesystem root, every sibling already answers honestly about a path that
    is not there (`llm-cost-report.py` warns and exits non-zero; `check-release-tag.py`
    reports `not measurable`; `calibrate_silent_drift_threshold.py` says it cannot read
    the path). This was the one member that did not.
    """

    def test_a_root_that_does_not_exist_is_not_clean(self, tmp_path) -> None:
        missing = tmp_path / "not-here"
        proc = _run(missing)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "OK" not in proc.stdout, proc.stdout
        assert "not a directory" in proc.stderr, proc.stderr
        assert str(missing.resolve()) in proc.stderr, proc.stderr

    def test_a_root_that_is_a_file_is_not_clean(self, tmp_path) -> None:
        """The shape a mistyped `--root` lands on as easily as a missing one."""
        target = tmp_path / "a-file.py"
        target.write_text("x = 1\n", encoding="utf-8")
        proc = _run(target)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "OK" not in proc.stdout, proc.stdout

    def test_an_empty_directory_is_not_clean(self, tmp_path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        proc = _run(empty)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "OK" not in proc.stdout, proc.stdout
        assert "no Python module under" in proc.stderr, proc.stderr
        # The message names what it looked for, so a caller who pointed at the wrong
        # directory can see which one it wanted.
        for directory in ("emrg", "scripts", "tests", "packaging"):
            assert directory in proc.stderr, proc.stderr

    def test_a_directory_with_none_of_the_scanned_directories_is_not_clean(
        self, tmp_path
    ) -> None:
        """A real tree, just not *this* project's -- e.g. another repo's checkout."""
        other = tmp_path / "other-project"
        (other / "src").mkdir(parents=True)
        (other / "src" / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        proc = _run(other)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "OK" not in proc.stdout, proc.stdout

    def test_a_scanned_directory_with_no_module_is_not_clean(self, tmp_path) -> None:
        """`emrg/` present and empty is still zero modules read."""
        (tmp_path / "emrg").mkdir(parents=True)
        proc = _run(tmp_path)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "OK" not in proc.stdout, proc.stdout

    def test_a_subset_tree_is_still_measured(self, tmp_path) -> None:
        """The refusal must not swallow the trees this guard is *meant* to read.

        An attribution run builds a root holding one file -- `emrg/server/daemon.py`
        and nothing else -- and that tree is the whole reason `--root` exists. A
        guard that refused anything but a full checkout would have answered
        `unmeasurable` for the reading that found the defect it was written for.

        Deliberately asserts the verdict and not the count line: it is the control
        that must hold on the tree *without* this cycle's change as well, so a
        failure here is "the refusal over-reached" and nothing else.
        """
        (tmp_path / "emrg" / "server").mkdir(parents=True)
        (tmp_path / "emrg" / "server" / "only.py").write_text(
            "def f():\n    return 1\n", encoding="utf-8"
        )
        proc = _run(tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "OK" in proc.stdout, proc.stdout

    def test_a_subset_tree_still_reports_its_defect(self, tmp_path) -> None:
        """The control for the row above: measuring a subset is not clearing it.

        Uses the *first* rule's shape (a read above its binding) so that the answer
        does not depend on this cycle's change: on either tree the reading must be
        rc=1, which is what makes this a control for the refusal rather than a second
        test of something else.
        """
        (tmp_path / "emrg").mkdir(parents=True)
        (tmp_path / "emrg" / "bad.py").write_text(
            "def f():\n    return flag\n    flag = 1\n", encoding="utf-8"
        )
        proc = _run(tmp_path)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "'flag'" in proc.stdout, proc.stdout

    def test_a_clean_reading_names_how_many_modules_it_read(self, tmp_path) -> None:
        """`OK` is a claim about something, so the claim says what.

        Every verdict in this family names its subject: the tree is named before the
        verdict, and this is the same rule applied to the measurement -- a count of
        zero is what made an `OK` about a directory that is not there indistinguishable
        from an `OK` about a clean checkout, and printing the number is what keeps the
        two apart for a reader who only sees the verdict line.
        """
        tree = _tree(tmp_path, good="def f():\n    return 1\n")
        proc = _run(tree)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "1 module(s) read" in proc.stdout, proc.stdout


class TestItsExitCodes:
    def test_a_file_that_will_not_parse_is_unmeasurable_not_clean(self, tmp_path) -> None:
        """`0` means "measured and clean", so a tree it cannot read is never `0`."""
        tree = _tree(
            tmp_path,
            broken="def f(:\n    pass\n",
            good="def g():\n    value = 1\n    return value\n",
        )
        proc = _run(tree)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "UNMEASURABLE" in proc.stderr, proc.stderr
        assert "OK" not in proc.stdout, proc.stdout

    def test_the_first_line_names_the_tree_it_measured(self, tmp_path) -> None:
        tree = _tree(tmp_path, good="def g():\n    return 1\n")
        proc = _run(tree)
        first = (proc.stdout or "").splitlines()[0]
        assert first == f"tree: {tree.resolve()}", proc.stdout


class TestTheCheckout:
    def test_no_name_in_this_checkout_is_read_before_its_binding(self) -> None:
        """The verdict on the tree the suite lives in -- the enforcement.

        Before the fix this is `rc=1` with three findings (`_approval_pending`
        and `was_busy` in the TUI's key handler, `full_content` in the daemon's
        tool loop). Without this test the guard is a report nobody reads, which
        is precisely how the released client came to crash on every Enter.
        """
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, (
            f"{SCRIPT.name} reports a read-before-binding defect in {REPO_ROOT} "
            f"(rc={proc.returncode}):\n{proc.stdout}{proc.stderr}"
        )
        assert (proc.stdout or "").splitlines()[0] == f"tree: {REPO_ROOT}", proc.stdout
