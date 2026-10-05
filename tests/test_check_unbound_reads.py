"""Tests for `scripts/check_unbound_reads.py` -- the read-before-binding guard.

Issue #1759: v0.3.6's TUI crashed on every Enter because `handle_key` read and
rebound `_approval_pending` without declaring it `nonlocal`, so the name was
local by assignment and the read raised `UnboundLocalError`. The guard exists to
keep that class out of the tree, so these tests are in two halves: a built tree
where it must fire and a built tree where it must stay silent, plus the verdict
on the checkout this suite lives in -- the half that was missing from the
neighbouring guard and let the crash ship.
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

    def test_a_tree_with_no_module_is_unmeasurable_not_clean(self, tmp_path) -> None:
        """A verdict over a tree the walk read nothing from is the file's own named failure.

        Measured 2026-10-05 (`cyc20261005-145352`) on the master this pins: an empty
        directory, and one holding a single unrelated file, both answered the clean
        line with exit 0. `scanned_modules` already names that mode — "a clean verdict
        about nothing, which is the one failure mode this file must not have" — and
        the note was written for the whole-tree skip, so the same verdict from a
        different cause was still reachable.
        """
        empty = tmp_path / "empty"
        empty.mkdir()
        proc = _run(empty)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stderr, proc.stderr
        assert "OK" not in proc.stdout, proc.stdout

        # A tree that carries the scanned *directories* and no module in them is the
        # same fact: what is inspected is the modules, not the directory names.
        bare = tmp_path / "bare"
        (bare / "emrg").mkdir(parents=True)
        (bare / "notes.md").write_text("nothing to inspect\n", encoding="utf-8")
        proc = _run(bare)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stderr, proc.stderr

    def test_quiet_does_not_bypass_the_empty_tree_refusal(self, tmp_path) -> None:
        """`--quiet` suppresses the clean *line*; it must not suppress the refusal.

        The refusal exists to replace that line, so a flag whose help says it prints
        "nothing but the verdict line on a clean tree" must not be the way past it.
        """
        empty = tmp_path / "empty"
        empty.mkdir()
        proc = _run(empty, "--quiet")
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stderr, proc.stderr

    def test_a_tree_with_a_module_and_no_defect_is_still_clean(self, tmp_path) -> None:
        """The other direction, and the reason it is needed.

        The cheapest way to satisfy the two tests above is to answer `2` whenever
        there are no findings — a guard that never passes, which reads as cautious
        and is the same as not having one. A module present and nothing wrong must
        still be `0`.
        """
        tree = _tree(tmp_path, good="def g():\n    value = 1\n    return value\n")
        proc = _run(tree)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "OK" in proc.stdout, proc.stdout


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
