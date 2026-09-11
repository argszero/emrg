"""Tests for scripts/classify-conflict.py — the conflict-classification aid.

The tool exists because the same "both sides edited this file" shape needed
**opposite** resolutions twice in one cycle (`cyc20260911-112155`):

* `#1136` — the branch carried an unmerged *duplicate* of `#1134`'s tests (it was
  built on `#1134`, later squash-merged, so the common ancestry is invisible).
  Master's copy was a strict superset, so **take theirs** was correct.
* `#1140` — the sides were **disjoint** additions, so the only correct resolution
  is **keep both**; either side-pick silently drops work.

These tests pin the *discrimination*, because that is the whole value: a tool
that recommends a side-pick for the disjoint case is worse than no tool.

The last test drives it against the **real** conflict states reconstructed with
`git merge`, which is how the classifier was debugged — my first version compared
text lines and called both `#1140` conflicts `overlapping`, because both sides
happened to contain `    \"\"\"` and `    )`.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "classify-conflict.py"


def _load():
    spec = importlib.util.spec_from_file_location("classify_conflict", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


class TestNumberOnlyConflicts:
    def test_count_line_is_named_as_a_measurement_not_a_choice(self, mod) -> None:
        """The Agent.md count line must never be resolved by picking a side."""
        label, advice = mod.classify(
            "Python: `uv run pytest tests/ -v` (1407) - import check: x\n",
            "Python: `uv run pytest tests/ -v` (1410) - import check: x\n",
        )
        assert label == mod.COUNT_LINE
        assert "measure" in advice.lower()
        assert "never pick a side" in advice.lower()

    def test_two_lines_differing_by_a_number_are_not_a_count_line(self, mod) -> None:
        """The count-line rule is one line vs one line; a multi-line hunk is not it."""
        label, _ = mod.classify("a = 1\nb = 2\n", "a = 9\nb = 2\n")
        assert label != mod.COUNT_LINE

    def test_identical_sides(self, mod) -> None:
        label, _ = mod.classify("same\n", "same\n")
        assert label == mod.IDENTICAL


class TestDuplicateVersusDisjoint:
    """The discrimination that decides `#1136` (take theirs) vs `#1140` (keep both)."""

    def test_strict_superset_theirs_is_a_duplicate_and_says_take_theirs(self, mod) -> None:
        ours = "def keep():\n    pass\n"
        theirs = "def keep():\n    pass\n\n\ndef added():\n    pass\n"
        label, advice = mod.classify(ours, theirs)
        assert label == mod.DUPLICATE
        assert "THEIRS" in advice

    def test_strict_superset_ours_says_take_ours(self, mod) -> None:
        ours = "def keep():\n    pass\n\n\ndef added():\n    pass\n"
        theirs = "def keep():\n    pass\n"
        label, advice = mod.classify(ours, theirs)
        assert label == mod.DUPLICATE
        assert "OURS" in advice

    def test_disjoint_sides_say_keep_both(self, mod) -> None:
        ours = "def only_ours():\n    pass\n"
        theirs = "def only_theirs():\n    pass\n"
        label, advice = mod.classify(ours, theirs)
        assert label == mod.DISJOINT
        assert "KEEP BOTH" in advice

    def test_same_names_in_both_is_the_one_case_a_human_must_read(self, mod) -> None:
        """A real edit collision: both sides changed the same function."""
        ours = "def shared():\n    return 1\n"
        theirs = "def shared():\n    return 2\n"
        label, advice = mod.classify(ours, theirs)
        assert label == mod.OVERLAPPING
        assert "human" in advice.lower()

    def test_shared_boilerplate_does_not_make_sides_overlapping(self, mod) -> None:
        """The exact bug in the first version, pinned as a regression.

        Both real `#1140` hunks contained `    \"\"\"` and `    )`. Comparing text
        lines called them `overlapping`; the declared symbols are disjoint, which
        is the property that matters. Shared structural lines must not veto the
        `disjoint` verdict.
        """
        ours = 'def only_ours():\n    """Doc."""\n    return (\n        1\n    )\n'
        theirs = 'def only_theirs():\n    """Doc."""\n    return (\n        2\n    )\n'
        label, advice = mod.classify(ours, theirs)
        assert label == mod.DISJOINT, (
            "shared boilerplate (`\"\"\"`, `)`) was treated as shared work - this is "
            "the defect the symbol-based predicate exists to avoid"
        )
        assert "KEEP BOTH" in advice

    def test_a_hunk_with_no_symbols_falls_back_to_content_lines(self, mod) -> None:
        """Non-code hunks (docs, config) still get a verdict."""
        label, advice = mod.classify("alpha\nbeta\n", "gamma\ndelta\n")
        assert label == mod.DISJOINT

        label, advice = mod.classify("alpha\nbeta\n", "alpha\nbeta\ngamma\n")
        assert label == mod.DUPLICATE

    def test_classes_are_mutually_exclusive(self, mod) -> None:
        """Every verdict is one of the five labels, never a mix."""
        labels = {
            mod.IDENTICAL,
            mod.DUPLICATE,
            mod.DISJOINT,
            mod.COUNT_LINE,
            mod.OVERLAPPING,
        }
        cases = [
            ("a\n", "a\n"),
            ("a\n", "a\nb\n"),
            ("def x():\n    pass\n", "def y():\n    pass\n"),
            ("n = 1\n", "n = 2\n"),
            ("def s():\n    return 1\n", "def s():\n    return 2\n"),
        ]
        for ours, theirs in cases:
            label, _ = mod.classify(ours, theirs)
            assert label in labels


class TestBlockParsing:
    def test_it_finds_a_real_block_and_its_two_sides(self, mod) -> None:
        text = (
            "before\n"
            "<<<<<<< HEAD\n"
            "ours line\n"
            "=======\n"
            "theirs line\n"
            ">>>>>>> origin/master\n"
            "after\n"
        )
        blocks = mod.conflicts_in(text)
        assert len(blocks) == 1
        ours, theirs, label = blocks[0]
        assert ours.strip() == "ours line"
        assert theirs.strip() == "theirs line"
        assert label == "origin/master"

    def test_a_file_without_blocks_yields_none(self, mod) -> None:
        assert mod.conflicts_in("no conflicts here\n") == []

    def test_conflict_markers_inside_string_literals_are_not_counted(self, mod) -> None:
        """The repo's own tests embed markers as literals; a mid-line one is not a block."""
        text = 'f"<<<<<<< HEAD\\n{sides[0]}\\n=======\\n{sides[1]}\\n>>>>>>> master"\n'
        assert mod.conflicts_in(text) == []


class TestCli:
    def test_no_paths_is_a_usage_error(self, mod, capsys) -> None:
        assert mod.main([]) == 2

    def test_missing_file_is_an_error(self, mod, tmp_path) -> None:
        assert mod.main([str(tmp_path / "nope.txt")]) == 2

    def test_a_file_without_blocks_is_an_error(self, mod, tmp_path) -> None:
        f = tmp_path / "clean.txt"
        f.write_text("nothing to see\n", encoding="utf-8")
        assert mod.main([str(f)]) == 2

    def test_overlapping_in_a_file_exits_1_so_the_loop_cannot_auto_resolve(
        self, mod, tmp_path
    ) -> None:
        """Exit 1 is the 'a human must look' signal; 0 means the classes are decidable."""
        f = tmp_path / "x.py"
        f.write_text(
            "<<<<<<< HEAD\ndef shared():\n    return 1\n=======\n"
            "def shared():\n    return 2\n>>>>>>> origin/master\n",
            encoding="utf-8",
        )
        assert mod.main([str(f)]) == 1

    def test_disjoint_in_a_file_exits_0(self, mod, tmp_path) -> None:
        f = tmp_path / "y.py"
        f.write_text(
            "<<<<<<< HEAD\ndef only_ours():\n    pass\n=======\n"
            "def only_theirs():\n    pass\n>>>>>>> origin/master\n",
            encoding="utf-8",
        )
        assert mod.main([str(f)]) == 0

    def test_the_reported_summary_names_the_classes(self, mod, tmp_path, capsys) -> None:
        f = tmp_path / "z.py"
        f.write_text(
            "<<<<<<< HEAD\ndef only_ours():\n    pass\n=======\n"
            "def only_theirs():\n    pass\n>>>>>>> origin/master\n",
            encoding="utf-8",
        )
        mod.main([str(f)])
        out = capsys.readouterr().out
        assert "disjoint=1" in out


@pytest.mark.parametrize(
    "branch,expect",
    [
        # #1140: disjoint additions -> keep both. Recorded because the *first*
        # version of the predicate got this wrong (reported `overlapping`).
        ("a73eba58", "disjoint"),
        # #1136: an unmerged duplicate of #1134's tests -> take theirs.
        ("7147666", "duplicate"),
    ],
)
def test_it_reproduces_the_real_historical_verdicts(mod, tmp_path, branch, expect) -> None:
    """Drive the classifier against a reconstructed real merge.

    This is ground truth, not a fixture: both branches are reconstructed with
    `git merge --no-commit` against the same master, and the expected labels are
    the ones derived by hand during the cycle (and acted on, with verification
    after). Skips if the objects are unavailable (e.g. a shallow clone).
    """
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{branch}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"{branch} is not available in this clone")

    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(wt), branch],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(
            ["git", "merge", "--no-commit", "--no-ff", "origin/master"],
            cwd=wt,
            capture_output=True,
        )
        unmerged = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=wt,
            capture_output=True,
            text=True,
        ).stdout.split()
        if not unmerged:
            pytest.skip("no conflict state to classify (master already merged)")

        # Classify the test-file conflicts; Agent.md is always the count line.
        verdicts = []
        for name in unmerged:
            if name.startswith("tests/"):
                text = (wt / name).read_text(encoding="utf-8")
                for ours, theirs, _ in mod.conflicts_in(text):
                    verdicts.append(mod.classify(ours, theirs)[0])
        assert expect in verdicts, (
            f"expected a `{expect}` verdict among {verdicts} for {branch}; the "
            f"discrimination between duplicate and disjoint is the tool's whole value"
        )
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=REPO_ROOT,
            capture_output=True,
        )
