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

    def test_a_root_that_is_not_a_directory_is_unmeasurable_not_clean(self, tmp_path) -> None:
        """`0` means "measured and clean", so a root nobody could walk is never `0`.

        Measured 2026-10-06 (`cyc20261006-165503`): `--root /nonexistent` printed
        `tree: /nonexistent` and then the `OK:` line, exit 0 - a pass for a tree the
        guard had not read a byte from. The `tree:` line is still first, because the
        refusal is a verdict and the family's rule is that the identity comes before it.
        """
        proc = _run(tmp_path / "not-there")
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stderr, proc.stderr
        assert "is not a directory" in proc.stderr, (
            "a path that is not there and a tree that holds no source are two causes "
            "with two remedies (fix the path, versus check that the sources are "
            "checked out), so the refusal has to name the one it read instead of "
            f"answering both with the count's message:\n{proc.stderr}"
        )
        assert "OK" not in proc.stdout, proc.stdout
        assert (proc.stdout or "").splitlines()[0].startswith("tree: "), proc.stdout

    def test_a_tree_with_no_python_file_is_unmeasurable_not_clean(self, tmp_path) -> None:
        """The second shape of the same reading: a tree that exists and holds nothing.

        `_tree` builds `emrg/` and `scripts/` and writes no file, which is what a root
        pointed one level too high (or at a directory whose sources were not checked
        out) looks like. An empty finding list beside an unread tree is not a pass, and
        neither is an empty one beside a tree with no file to read.
        """
        proc = _run(_tree(tmp_path))
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stderr, proc.stderr
        assert "OK" not in proc.stdout, proc.stdout

    def test_the_clean_verdict_names_how_many_files_it_read(self, tmp_path) -> None:
        """The count is what tells "no findings in 300 files" from "no findings in none".

        Without it the two verdicts are the same sentence, which is how the empty root
        above stayed invisible: a reader has no field to notice is zero.
        """
        proc = _run(_tree(tmp_path, good="def g():\n    return 1\n"))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "1 file(s) read" in proc.stdout, proc.stdout


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
