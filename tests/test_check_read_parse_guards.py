"""Tests for `scripts/check_read_parse_guards.py` — the decode-shape guard.

Measured 2026-10-03 (`cyc20261003-215322`): twenty readers across three families
spelled `(OSError, <format error>)` and so let a non-UTF-8 file escape — two default
answers for two shapes of "I could not read this file", and a raised exception for the
third. Twenty sites is not twenty mistakes, so the rule is mechanised in the guard
these tests pin.

The tests are in two halves, because a check written against failing data alone has
already been measured wrong four times in this repo (#455, #461, #464, #477): a built
tree where it **must fire**, a built tree where it **must stay silent** (including the
cases a careless rule would flag — a parse of a string, a read inside a nested try
that handles it, and a `try`/`finally` with no handler at all), and the verdict on the
checkout this suite lives in.

The silent half is the one that matters most here: the first version of this rule
flagged four test files that guard nothing (bare `try`/`finally`) and was fixed in
`cyc20261003-215322` — a rule that reports "no decision" as "wrong decision" is a rule
its own users learn to ignore.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_read_parse_guards.py"


def _tree(tmp_path: Path, **files: str) -> Path:
    """A directory shaped like this project, so the script scans it.

    `emrg/` and `scripts/` are the two markers `_resolve_root` keys on, and `emrg/` is
    also a scanned directory, so one file there is enough.
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


class TestItFires:
    def test_the_shipped_two_shape_tuple_is_reported(self, tmp_path) -> None:
        """The exact defect, reduced to five lines: `(OSError, JSONDecodeError)`."""
        tree = _tree(
            tmp_path,
            bad="""\
            import json

            def read(path):
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return {}
            """,
        )
        proc = _run(tree)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "read_text" in proc.stdout and "json.JSONDecodeError" in proc.stdout

    def test_a_yaml_reader_with_the_shipped_tuple_is_reported(self, tmp_path) -> None:
        """The same rule, the other format — the guard is about the class, not JSON."""
        tree = _tree(
            tmp_path,
            bad="""\
            import yaml

            def read(path):
                try:
                    return yaml.safe_load(path.read_text(encoding="utf-8"))
                except (yaml.YAMLError, OSError):
                    return []
            """,
        )
        assert _run(tree).returncode == 1

    def test_a_reader_whose_only_handler_is_oserror_is_reported(self, tmp_path) -> None:
        """Naming no parse error at all is also the miss — this is the `open()` shape."""
        tree = _tree(
            tmp_path,
            bad="""\
            import json

            def read(path):
                try:
                    with open(path, encoding="utf-8") as fh:
                        return [json.loads(line) for line in fh]
                except OSError:
                    return []
            """,
        )
        assert _run(tree).returncode == 1

    def test_a_tree_it_cannot_parse_is_unmeasurable_not_clean(self, tmp_path) -> None:
        """Exit 2, never 0: a question the guard cannot answer is not a pass."""
        tree = _tree(tmp_path, broken="def f(:\n")
        proc = _run(tree)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "UNMEASURABLE" in proc.stderr


class TestItStaysSilent:
    def test_the_shared_home_counts_as_naming_the_shape(self, tmp_path) -> None:
        """Catching the home is the fix, so the guard has to resolve the constant.

        The home is *assigned* in the tree as well as imported into the reader, which
        is how the real one is laid out (`emrg/read_errors.py`) and what the guard's
        by-name resolution keys on. A tree that imports a constant nobody in it
        defines is a tree the guard cannot resolve, and it reports that as a finding
        rather than as a pass — loud, which is the direction that stays visible.
        """
        tree = _tree(
            tmp_path,
            home="""\
            import json

            FILE_READ_ERRORS = (OSError, UnicodeDecodeError)

            JSON_READ_ERRORS = (*FILE_READ_ERRORS, json.JSONDecodeError)
            """,
            reader="""\
            import json

            from emrg.good import JSON_READ_ERRORS


            def read(path):
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except JSON_READ_ERRORS:
                    return {}
            """,
        )
        proc = _run(tree)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    @pytest.mark.parametrize(
        "caught",
        [
            "(OSError, json.JSONDecodeError, UnicodeDecodeError)",
            "(OSError, json.JSONDecodeError, UnicodeError)",
            "(OSError, json.JSONDecodeError, ValueError)",
            "(OSError, json.JSONDecodeError, Exception)",
            "",
        ],
    )
    def test_every_name_that_covers_the_shape_is_accepted(self, tmp_path, caught) -> None:
        """The forms that really do catch it — enumerated, because a matcher that
        accepts one spelling and misses its siblings is the defect #461 measured.

        `""` is the bare `except:`, spelled separately because it is not a name.
        """
        clause = f"except {caught}:" if caught else "except:"
        tree = _tree(
            tmp_path,
            good=f"""\
            import json

            def read(path):
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                {clause}
                    return None
            """,
        )
        assert _run(tree).returncode == 0, _run(tree).stdout

    @pytest.mark.parametrize(
        "caught",
        [
            "OSError",
            "json.JSONDecodeError",
            "(OSError, json.JSONDecodeError)",
        ],
    )
    def test_the_forms_that_do_not_cover_it_are_reported(self, tmp_path, caught) -> None:
        """The complement, kept beside the accepted list so the two cannot drift.

        A handler set that names the format error but not the decode shape is the
        defect, and so is one that names only an I/O error — which is why `OSError`
        is here and not in the list above.
        """
        tree = _tree(
            tmp_path,
            bad=f"""\
            import json

            def read(path):
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except {caught}:
                    return None
            """,
        )
        assert _run(tree).returncode == 1, _run(tree).stdout

    def test_a_parse_of_a_string_is_not_a_read(self, tmp_path) -> None:
        """No decode step, so no decode error — the guard must not flag it."""
        tree = _tree(
            tmp_path,
            good="""\
            import json

            def parse(text):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return None
            """,
        )
        assert _run(tree).returncode == 0, _run(tree).stdout

    def test_a_read_inside_a_nested_try_is_that_trys_business(self, tmp_path) -> None:
        """The inner try handles it; the outer one must not be blamed for it."""
        tree = _tree(
            tmp_path,
            good="""\
            import json

            def read_many(paths):
                total = 0
                try:
                    for p in paths:
                        try:
                            total += len(json.loads(p.read_text(encoding="utf-8")))
                        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                            continue
                except OSError:
                    return total
                return total
            """,
        )
        assert _run(tree).returncode == 0, _run(tree).stdout

    def test_a_try_with_no_handler_is_not_a_guard(self, tmp_path) -> None:
        """`try`/`finally` decides nothing, so it cannot decide wrongly.

        Measured: the rule's first version flagged four test files that only close a
        handle. Reporting the absence of a decision as a wrong one is how a guard
        teaches its readers to ignore it.
        """
        tree = _tree(
            tmp_path,
            good="""\
            import json

            def read(path):
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                finally:
                    pass
            """,
        )
        assert _run(tree).returncode == 0, _run(tree).stdout


class TestThisCheckout:
    def test_every_reader_in_this_checkout_names_its_three_shapes(self) -> None:
        """The verdict on the tree the suite lives in — the enforcement.

        Without this the guard is a report nobody reads, which is exactly how twenty
        readers came to answer one input class two ways across three cycles.
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
            f"{SCRIPT.name} reports a reader that parses a file without naming a decode "
            f"error in {REPO_ROOT} (rc={proc.returncode}):\n{proc.stdout}{proc.stderr}"
        )
        assert (proc.stdout or "").splitlines()[0] == f"tree: {REPO_ROOT}", proc.stdout
