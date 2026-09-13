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


class TestIndentedDeclarationsAreVisible:
    """The symbol axis must see a class-body method, not only a column-0 def.

    `_SYMBOL` was anchored at column 0, so every method in a class body was
    invisible - measured 2026-09-11 (cyc20260911-171843): 893 of 2155 declarations
    in this repo's own `tests/` + `scripts/` (41.4%), across 46 of 80 files. The
    result was not a missing label but an inverted one for the same collision,
    differing only in indentation: `overlapping`/rc 1 at column 0, and
    `disjoint` → "KEEP BOTH"/rc 0 when indented - which concatenates two same-name
    definitions, so one side's edit disappears at rc 0. Reported by
    how2how2how2-arch.
    """

    def test_a_class_body_method_collision_is_overlapping_not_disjoint(self, mod) -> None:
        ours = "    def test_alpha(self, x=1):\n        assert compute(x) == 1\n"
        theirs = "    def test_alpha(self, x=2):\n        assert compute(x) == 2\n"
        label, advice = mod.classify(ours, theirs)
        assert label == mod.OVERLAPPING, (
            "the same collision at column 0 already escalates; indentation must "
            "not change the verdict"
        )
        assert "KEEP BOTH" not in advice

    def test_an_indented_async_def_is_seen_too(self, mod) -> None:
        ours = "    async def fetch(self, u=1):\n        return u\n"
        theirs = "    async def fetch(self, u=2):\n        return u\n"
        assert mod.classify(ours, theirs)[0] == mod.OVERLAPPING

    def test_indented_disjoint_additions_stay_disjoint(self, mod) -> None:
        """Positive control: the widening must not over-escalate.

        Before the fix this was `overlapping` - the names were invisible, so the
        content-line path saw no shared line and no symbols and guessed the wrong
        way. Seeing the names is what makes KEEP BOTH correct here.
        """
        ours = "    def added_ours():\n        pass\n"
        theirs = "    def added_theirs():\n        pass\n"
        label, _ = mod.classify(ours, theirs)
        assert label == mod.DISJOINT

    def test_an_indented_duplicate_is_still_a_duplicate(self, mod) -> None:
        ours = "    def keep(self):\n        pass\n"
        theirs = "    def keep(self):\n        pass\n\n    def added(self):\n        pass\n"
        assert mod.classify(ours, theirs)[0] == mod.DUPLICATE

    def test_two_differently_named_indented_classes_are_disjoint(self, mod) -> None:
        """`class` counts as a declaration, which the content-line path cannot see.

        Both sides add a class whose *body* is identical (`pass`), so the line
        path finds a shared line and partially overlaps. The names are what make
        them different additions, and KEEP BOTH is correct for both.
        """
        ours = "    class Alpha:\n        pass\n"
        theirs = "    class Beta:\n        pass\n"
        label, advice = mod.classify(ours, theirs)
        assert label == mod.DISJOINT, "same body, different names - both are wanted"
        assert "KEEP BOTH" in advice


class TestASidePickMustNotSilentlyDropAnEdit:
    """The subset test alone is not enough — found by adversarial probing.

    `duplicate` was decided purely on *declared names*, so "theirs declares every
    name ours does" was read as "theirs contains ours". Those are different
    claims: when both sides contain `test_alpha` but with **different bodies**,
    taking theirs discards ours' edit with no signal — the exact data loss this
    tool exists to prevent, hidden behind the one verdict that recommends a
    side-pick.
    """

    def test_superset_names_with_a_modified_shared_body_is_not_a_duplicate(self, mod) -> None:
        ours = "def test_alpha():\n    assert compute() == 1\n"
        theirs = (
            "def test_alpha():\n    assert compute() == 2\n"
            "def test_beta():\n    assert True\n"
        )
        label, advice = mod.classify(ours, theirs)
        assert label == mod.OVERLAPPING, (
            "ours' edit to test_alpha would vanish if this were resolved by taking "
            "theirs; the shared symbol's body must be compared, not just its name"
        )
        assert "human" in advice.lower()

    def test_a_true_superset_with_identical_shared_bodies_is_still_a_duplicate(self, mod) -> None:
        """The #1136 shape must keep working: shared bodies identical → take theirs."""
        ours = "def test_alpha():\n    assert True\n"
        theirs = (
            "def test_alpha():\n    assert True\n\n\n"
            "def test_beta():\n    assert True\n"
        )
        label, advice = mod.classify(ours, theirs)
        assert label == mod.DUPLICATE
        assert "THEIRS" in advice

    def test_a_modified_shared_body_in_the_no_symbol_path_is_escalated(self, mod) -> None:
        """One line per side, no symbols, no count: ambiguous, so do not guess."""
        label, _ = mod.classify("x = compute(1)\n", "x = compute(2)\n")
        assert label == mod.OVERLAPPING, (
            "KEEP BOTH here would concatenate into nonsense; with a single "
            "differing line and no symbol or count, no verdict is the honest answer"
        )


class TestCountLineIsADocumentedCountNotAnyInteger:
    """`count-line` must not fire on code that merely contains a literal.

    The one-line-vs-one-line numeric rule matched `x = compute(1)` vs
    `x = compute(2)`, so a plain code change was answered "MEASURE ... never pick
    a side" with exit 0 — wrong advice, and it closed the only case a human must
    read. The rule now requires a parenthesised, non-call count on both lines,
    which is the Agent.md shape.
    """

    def test_code_differing_by_a_literal_is_not_a_count_line(self, mod) -> None:
        label, advice = mod.classify("x = compute(1)\n", "x = compute(2)\n")
        assert label != mod.COUNT_LINE
        assert "measure" not in advice.lower()

    def test_the_documented_count_line_still_fires(self, mod) -> None:
        ours = "Python: `uv run pytest tests/ -v` (1407) - import check: x\n"
        theirs = "Python: `uv run pytest tests/ -v` (1410) - import check: x\n"
        label, advice = mod.classify(ours, theirs)
        assert label == mod.COUNT_LINE
        assert "never pick a side" in advice.lower()

    def test_a_call_argument_at_line_start_is_still_code(self, mod) -> None:
        """`(5)` as a bare tuple element is not a documented count either."""
        label, _ = mod.classify("log(1)\n", "log(2)\n")
        assert label != mod.COUNT_LINE

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


# Captured verbatim from the shape `merge.conflictStyle = diff3` produces: the
# base section sits between ours and the separator, and the sides are *reordered*
# relative to git's default layout for this input (HEAD is added last).
#
# Without a base-section check `CONFLICT_BLOCK` still matches - it reads the base
# into OURS, so the compared hunks are (ours + base) versus theirs. Measured
# 2026-09-11 with the real block below:
#
#     ours   = "def test_alpha():\n    assert 1 == 1\n"
#              "||||||| merged common ancestors\n"
#              "def test_beta():\n    assert 2 == 2\n"
#     theirs = "def test_gamma():\n    assert 3 == 3\n"
#     -> "disjoint ... KEEP BOTH (concatenate)", exit 0
#
# Concatenating that keeps the *base* copy - a third version of the same hunk that
# neither side wants. The advice is not merely imprecise; acting on it does the
# wrong thing, which is what this tool exists to prevent.
_DIFF3_BLOCK = (
    "before\n"
    "<<<<<<< HEAD\n"
    "def test_alpha():\n"
    "    assert 1 == 1\n"
    "||||||| merged common ancestors\n"
    "def test_beta():\n"
    "    assert 2 == 2\n"
    "=======\n"
    "def test_gamma():\n"
    "    assert 3 == 3\n"
    ">>>>>>> master\n"
    "after\n"
)


class TestTheDiff3LayoutIsRefusedNotMisread:
    """The layout the tool cannot read must be named, not answered.

    Same conclusion the sibling tool reached first: `check-doc-count.py` refuses a
    `|||||||` base section by name, because it cannot tell whether the conflict is
    the count line. Here the stakes are the advice itself.
    """

    def test_the_base_section_is_named(self, mod) -> None:
        assert mod.base_section(_DIFF3_BLOCK) == "||||||| merged common ancestors"

    def test_no_base_section_is_reported_as_none(self, mod) -> None:
        """The discriminating signal must be absent for the layout we do parse."""
        default_layout = (
            "<<<<<<< HEAD\nours line\n=======\ntheirs line\n>>>>>>> master\n"
        )
        assert mod.base_section(default_layout) is None

    def test_the_cli_refuses_the_diff3_layout(self, mod, tmp_path, capsys) -> None:
        f = tmp_path / "diff3.py"
        f.write_text(_DIFF3_BLOCK, encoding="utf-8")
        assert mod.main([str(f)]) == 1, (
            "a layout the tool cannot read must not exit 0 - rc 0 is the caller's "
            "signal that every block was classified and the advice is safe to act on"
        )
        out = capsys.readouterr().out
        assert "unparsed-layout" in out
        assert "|||||||" in out, "the refusal must name the marker it found"
        assert "KEEP BOTH" not in out, (
            "the whole defect: this block was answered `disjoint - KEEP BOTH`, "
            "which concatenates the base copy back in"
        )

    def test_the_default_layout_still_classifies(self, mod, tmp_path) -> None:
        """Positive control: the refusal must not swallow the layout we do read.

        Without this, a blanket refusal of every file would pass the test above.
        """
        f = tmp_path / "normal.py"
        f.write_text(
            "<<<<<<< HEAD\ndef only_ours():\n    pass\n=======\n"
            "def only_theirs():\n    pass\n>>>>>>> origin/master\n",
            encoding="utf-8",
        )
        assert mod.main([str(f)]) == 0

    def test_a_file_that_only_mentions_the_marker_is_not_refused(self, mod, tmp_path, capsys) -> None:
        """The false positive: a marker line is not a conflict.

        The first version matched `|||||||` anywhere in the file. A prose file
        that merely *mentions* the marker - or a doc, or a test fixture - has no
        conflict at all, yet was refused with "the file uses the diff3 layout"
        and told to re-merge. Measured 2026-09-11 (cyc20260911-165337) against
        this three-line file: rc 1 and a re-merge instruction about a document
        with nothing to re-merge.
        """
        f = tmp_path / "prose.md"
        f.write_text(
            "The diff3 layout inserts a marker line:\n"
            "||||||| merged common ancestors\n"
            "which this tool cannot read.\n",
            encoding="utf-8",
        )
        rc = mod.main([str(f)])
        out = capsys.readouterr().out
        assert "unparsed-layout" not in out, (
            "the refusal must be about a conflict block, not about the marker "
            "line appearing anywhere - this file has no conflict to refuse"
        )
        assert "re-merge" not in out, "nothing here needs re-merging"
        assert rc == 2, "no conflict blocks -> the usage error, not a refusal"

    def test_a_marker_after_a_closed_block_is_not_refused(self, mod, tmp_path, capsys) -> None:
        """The other half of the anchoring: the block must *close*.

        Without this, "the marker is inside an open region" could be satisfied by
        latching `open_block` on forever - the first `<<<<<<<` anywhere would make
        every later marker a conflict. This file has one default-layout block
        (fully readable) plus a stray marker afterwards, which is the shape a doc
        describing a merge has.
        """
        f = tmp_path / "after.md"
        f.write_text(
            "<<<<<<< HEAD\ndef only_ours():\n    pass\n=======\n"
            "def only_theirs():\n    pass\n>>>>>>> master\n"
            "\nAfter merging, git leaves a base marker like:\n"
            "||||||| merged common ancestors\n",
            encoding="utf-8",
        )
        assert mod.main([str(f)]) == 0
        out = capsys.readouterr().out
        assert "unparsed-layout" not in out
        assert "disjoint" in out, "the real block must still be classified"

    def test_the_shape_this_repo_actually_hits_is_refused_too(self, mod, tmp_path, capsys) -> None:
        """The realistic case is Agent.md's count line, not a code hunk.

        Measured with this very input before the fix: the block came back
        `duplicate - ours is a strict superset - take OURS`, because the base
        section (which carries the *stale* count) counted as one of ours' extra
        lines. `take OURS` there discards the base the reviewer was shown and
        says nothing about measuring the merged tree.
        """
        line_ours = "Python: `uv run pytest tests/ -v` (1438) - import check\n"
        line_base = "Python: `uv run pytest tests/ -v` (1416) - import check\n"
        f = tmp_path / "Agent.md"
        f.write_text(
            "head\n"
            "<<<<<<< HEAD\n"
            f"{line_ours}"
            "||||||| 1a2b3c4\n"
            f"{line_base}"
            "=======\n"
            f"{line_base}"
            ">>>>>>> master\n"
            "tail\n",
            encoding="utf-8",
        )
        assert mod.main([str(f)]) == 1
        out = capsys.readouterr().out
        assert "unparsed-layout" in out
        assert "duplicate" not in out and "COUNT-LINE" not in out


class TestCli:
    def test_no_paths_is_a_usage_error(self, mod, capsys) -> None:
        assert mod.main([]) == 2

    def test_all_with_nothing_unmerged_is_a_state_not_a_usage_error(
        self, mod, capsys, monkeypatch
    ) -> None:
        """`--all` answered with an empty list is rc 0, not the usage error (cyc20260913-082711).

        Measured before this: with `--all` passed explicitly and no unmerged paths
        (a merge that resolved cleanly), the tool printed "error: no paths given
        (pass files, or --all for every unmerged path)" and exited 2 - telling the
        caller to pass the flag they had just passed, and reporting a clean merge
        as a malformed invocation. Hit in practice at the moment a clean merge had
        produced a tree that fails the doc-count guard, i.e. exactly when the
        silence needed an explanation rather than a usage complaint.
        """
        monkeypatch.setattr(mod, "_unmerged_paths", lambda: [])
        rc = mod.main(["--all"])
        captured = capsys.readouterr()
        assert rc == 0, "a clean merge is a state, not a usage error"
        assert "nothing to classify" in captured.out
        assert "no paths given" not in captured.out + captured.err, (
            "the caller did pass --all; the message must not ask for it again"
        )
        assert captured.err == "", "this is not an error, so nothing goes to stderr"

        # The pointer must be to a real script: a hint at a renamed or deleted tool
        # is worse than no hint, and it is read at the one moment the reader has just
        # merged something and wants to know whether the resulting tree is healthy.
        referenced = [t for t in captured.out.split() if t.endswith(".py")]
        assert referenced, f"the message no longer points at a tool: {captured.out!r}"
        for name in referenced:
            assert (REPO_ROOT / name).is_file(), (
                f"the message points at {name}, which does not exist in the repo"
            )

    def test_all_still_classifies_when_paths_are_unmerged(
        self, mod, tmp_path, capsys, monkeypatch
    ) -> None:
        """The other direction: the new early return must not swallow the normal path."""
        f = tmp_path / "x.py"
        f.write_text(
            "<<<<<<< HEAD\ndef ours_only():\n    pass\n=======\n"
            "def theirs_only():\n    pass\n>>>>>>> origin/master\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "_unmerged_paths", lambda: [str(f)])
        rc = mod.main(["--all"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "disjoint" in out.lower() or "KEEP BOTH" in out
        assert "nothing to classify" not in out

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


class TestMultipleCountLinesInOneBlock:
    """A block covering several documented counts is still a `count-line`.

    Measured 2026-09-11 (`cyc20260911-190629`): 3 of the last 51 commits touching
    Agent.md moved 2+ documented counts at once (e.g. `e46c160`, `5c039b4`,
    `0c8a212`), and `git merge` then emits a single block spanning every one of
    them. The one-line-only predicate let that block fall through to the
    content-line fallback, which answered "share no content line - KEEP BOTH
    (concatenate)" at rc 0 and concatenated two copies of each count line - the
    exact state `tests/test_doc_counts.py::_duplicated_count_line_kinds` rejects.
    Both shapes below were reproduced with a real `git merge` before the fix.
    """

    def test_two_aligned_count_lines_are_a_count_line(self, mod) -> None:
        ours = (
            "Python: `uv run pytest tests/ -v` (1438) - import check\n"
            "GUI: `cd emrg/gui && npm test` (101: 44 daemon_client)\n"
        )
        theirs = (
            "Python: `uv run pytest tests/ -v` (1477) - import check\n"
            "GUI: `cd emrg/gui && npm test` (102: 45 daemon_client)\n"
        )
        label, advice = mod.classify(ours, theirs)
        assert label == mod.COUNT_LINE, (
            "an aligned multi-count block is the same shape as the single-line one; "
            "calling it `disjoint` concatenates duplicate count lines"
        )
        assert "KEEP BOTH" not in advice
        assert "measure" in advice.lower()

    def test_three_aligned_count_lines_are_a_count_line(self, mod) -> None:
        ours = (
            "Python: `uv run pytest tests/ -v` (1438) - import check\n"
            "GUI: `cd emrg/gui && npm test` (101: 44 daemon_client)\n"
            "Renderer: `cd emrg/gui/renderer && npm test` (518: 5 snapshot-store)\n"
        )
        theirs = (
            "Python: `uv run pytest tests/ -v` (1477) - import check\n"
            "GUI: `cd emrg/gui && npm test` (102: 45 daemon_client)\n"
            "Renderer: `cd emrg/gui/renderer && npm test` (520: 5 snapshot-store)\n"
        )
        assert mod.classify(ours, theirs)[0] == mod.COUNT_LINE

    def test_a_multi_line_text_change_is_still_not_a_count_line(self, mod) -> None:
        """Generalising the rule must not swallow real content.

        Every *differing* pair must pass the documented-count test, so a renamed
        test or a reworded sentence keeps its content classification (this is the
        #1125-class mistake: a predicate that fires on code it should not).
        """
        ours = (
            "Python: `uv run pytest tests/ -v` (1438) - import check\n"
            "renamed_a_test_case: something_else\n"
        )
        theirs = (
            "Python: `uv run pytest tests/ -v` (1477) - import check\n"
            "renamed_a_test_case: something_different\n"
        )
        assert mod.classify(ours, theirs)[0] != mod.COUNT_LINE

    def test_unaligned_sides_are_not_a_count_line(self, mod) -> None:
        """No pairing to compare: different lengths is a different shape."""
        ours = "Python: `uv run pytest tests/ -v` (1438) - import check\n"
        theirs = (
            "Python: `uv run pytest tests/ -v` (1477) - import check\n"
            "GUI: `cd emrg/gui && npm test` (102: 45 daemon_client)\n"
        )
        assert mod.classify(ours, theirs)[0] != mod.COUNT_LINE


class TestRevisionPrefixesAreNotDisjointAdditions:
    """Sharing no byte-equal line is not evidence of separate additions.

    The live case (`cyc20260911-190629`): #1140's Agent.md block 2 has ours' two
    paragraph lines as strict *prefixes* of master's two - the same paragraphs at
    an older revision (890 vs 539, 601 vs 471 characters, same order). The
    fallback saw no shared line and said KEEP BOTH at rc 0, which emits the stale
    **and** the current copy of each paragraph.
    """

    def test_a_stale_longer_copy_escalates_instead_of_keep_both(self, mod) -> None:
        """Pinned to the live shape, including its *zero* shared lines.

        The fixture must share no line at all - that is what routes the pair into
        the `not ours_set & theirs_set` branch. A fixture with even one shared line
        passes through the older partial-overlap branch instead, so it would keep
        passing with the prefix rule removed (measured: an earlier version of this
        test survived exactly that mutation).
        """
        ours = (
            "Doc count sync: `uv run --no-sync python3 scripts/check-doc-count.py` "
            "- measures the tree and checks Agent.md; root now resolves from cwd\n"
            "Node count sync: `uv run --no-sync python3 scripts/check-node-test-count.py` "
            "- asks the real runners\n"
        )
        theirs = (
            "Doc count sync: `uv run --no-sync python3 scripts/check-doc-count.py`\n"
            "Node count sync: `uv run --no-sync python3 scripts/check-node-test-count.py`\n"
            "Vote count: `uv run --no-sync python3 scripts/check-vote-count.py <PR>...`\n"
        )
        assert not (set(mod._content_lines(ours)) & set(mod._content_lines(theirs)))
        label, advice = mod.classify(ours, theirs)
        assert label == mod.OVERLAPPING, (
            "KEEP BOTH would emit both the stale and the current paragraph"
        )
        # Assert on the *recommendation*, not the substring: the advice explains
        # why KEEP BOTH would be wrong, so it mentions the phrase.
        assert "KEEP BOTH (concatenate)" not in advice
        assert "human must read" in advice

    def test_genuinely_unrelated_paragraphs_stay_disjoint(self, mod) -> None:
        """The fix must not turn every prose conflict into an escalation.

        Two lines per side on purpose: a single line per side is the *older*
        rule's escalation (one line against one line has no evidence to decide
        between an edit and an adjacent addition), so a one-line fixture would
        pass for the wrong reason and would not exercise the prefix rule at all.
        """
        ours = "alpha beta gamma\ndelta epsilon zeta\n"
        theirs = "eta theta iota\nkappa lambda mu\n"
        label, advice = mod.classify(ours, theirs)
        assert label == mod.DISJOINT
        assert "KEEP BOTH" in advice


class TestACountLineRevisedBesideATextRevision:
    """A block mixing a count line with a text edit is one revision, not two adds.

    Measured 2026-09-11 (`cyc20260911-194733`), reproduced with a real
    `git merge-file` on adjacent lines: ours pairs the Python count line with a
    `Doc count sync:` line, theirs carries the same two lines edited divergently.
    The head's prefix rule cannot see it - neither line is a prefix of its
    counterpart, because the count digits sit *inside* the line and everything
    after them was rewritten - so the fallback answered `disjoint - KEEP BOTH
    (concatenate)` at rc 0, and the concatenation holds two
    ``Python: `uv run pytest` `` lines. That is the state
    `tests/test_doc_counts.py::_duplicated_count_line_kinds` rejects, i.e. a doc
    claiming two different pytest counts.

    Across the last 400 commits touching `Agent.md`, 5 hunks reach the fallback in
    a multi-line block with a count line in it (`cb651a4` 2v2, `5c039b4` 3v3,
    `3335877` `444e1d5` 1v2, `18fd0af` 1v13) and all 5 would emit that duplicate.
    Deriving this by running the parent rule and this rule over the same blocks
    gives exactly those 5, and `cb651a4` and `5c039b4` are the two *aligned* ones:
    the previous rule saw equal-length sides whose pairs all carry a documented
    count and answered `count-line` only when the masking made the pairs equal,
    which a text revision beyond the number defeats - so `5c039b4` (`disjoint` at
    the parent head) is one of the 5 this rule fixes, not one the previous rule
    caught.

    There are also two further hunks with a count line (`e46c160`, `0c8a212`,
    both 2v2) which the parent rule already answers `count-line`; this rule leaves
    them there. So 7 hunks reach the fallback region with a count line and 5 of
    them change verdict.
    """

    def test_a_count_line_beside_a_text_revision_escalates(self, mod) -> None:
        ours = (
            "Python: `uv run pytest tests/ -v` (1393) — x\n"
            "Doc count sync: `check-doc-count.py [--write|--dry-run]` — 测量树里\n"
        )
        theirs = (
            "Python: `uv run pytest tests/ -v` (1382) — x\n"
            "Doc count sync: `check-doc-count.py [--write]` — 测量当前树上的\n"
        )
        assert not (set(mod._content_lines(ours)) & set(mod._content_lines(theirs)))
        label, advice = mod.classify(ours, theirs)
        assert label == mod.OVERLAPPING, (
            "KEEP BOTH would concatenate two Python count lines, so the doc would "
            "claim two different pytest counts"
        )
        assert "KEEP BOTH (concatenate)" not in advice
        assert "human must read" in advice

    def test_the_predicate_is_narrow_about_what_counts_as_the_same_count(self, mod) -> None:
        """Two *different* count lines are two facts, not a revision of one.

        The masking must be the evidence: only a pair that is equal once digits
        are removed is "one fact re-measured". `Python:` against `Renderer:` is a
        different fact, and two genuinely separate additions may carry different
        counts - escalating those is right for the count-duplication reason, but
        the predicate must not fire on lines that share no count shape at all.
        """
        ours = (
            "Python: `uv run pytest tests/ -v` (1393) — x\n"
            "Some unrelated prose that was added here\n"
        )
        theirs = (
            "Renderer: `cd emrg/gui/renderer && npm test` (514) — y\n"
            "Entirely different prose, also added\n"
        )
        assert not mod._looks_like_a_count_revision(
            mod._content_lines(ours), mod._content_lines(theirs)
        )

    def test_two_numbers_that_are_not_counts_do_not_escalate(self, mod) -> None:
        """A bare numeric difference in code is not a count revision.

        Index-alignment alone must not be the evidence: `x = compute(1)` beside
        `x = compute(2)` is a code change, not a documented count left behind by a
        revision, and it already reaches `overlapping` through the declared-symbol
        path. The gate that keeps this rule off it is the *documented count* shape
        (a parenthesised number), the same condition the aligned
        `_differ_only_by_number` applies - without it, removing digits makes any
        two locally-numbered code lines a "count pair". Measured on the 931-block
        corpus, dropping the gate fires on 19 blocks that are not counts at all,
        e.g. `emrg/gui/package-lock.json` `"version": "0.2.91"` against
        `"version": "0.2.92"`; a test that only ever changes digits is silent to it,
        so the case must differ in the surrounding text too.
        """
        assert not mod._looks_like_a_count_revision(
            ["x = compute(1)"], ["x = compute(2)"]
        )
        assert not mod._looks_like_a_count_revision(
            ['  "version": "0.2.91",'], ['  "version": "0.2.92",']
        )
        # ...while the documented-count pair still escalates (identical prose, so
        # the digits are the *only* difference - one fact re-measured).
        assert mod._looks_like_a_count_revision(
            ["Python: `uv run pytest tests/ -v` (1393) — x"],
            ["Python: `uv run pytest tests/ -v` (1382) — x"],
        )

    def test_unaligned_sides_still_escalate(self, mod) -> None:
        """A 1-line side against a 2-line side is still a count revision.

        Measured 2026-09-11: three of the five Agent.md hunks this rule fixes are
        unaligned (`3335877` 1v2, `444e1d5` 1v2, `18fd0af` 1v13 - 1v13 because the
        rest of the count block reads as an addition beside ours' single stale
        line), and all three got `disjoint - KEEP BOTH`. Requiring both
        sides to be the same length is what hid them - the count lines pair up at
        the front regardless, and the concatenation still holds two
        ``Python: `uv run pytest` `` lines.
        """
        ours = "Python: `uv run pytest tests/ -v` (1393) — x\n"
        theirs = (
            "Python: `uv run pytest tests/ -v` (1382) — x\n"
            "Doc count sync: `check-doc-count.py [--write]` — 测量当前树上的\n"
        )
        assert mod._looks_like_a_count_revision(
            mod._content_lines(ours), mod._content_lines(theirs)
        )
        assert mod.classify(ours, theirs)[0] == mod.OVERLAPPING

    def test_a_count_line_re_breakdown_is_the_same_kind_not_a_disjoint_add(
        self, mod
    ) -> None:
        """The same count kind at two revisions, whose parenthesised detail moved.

        A real block, not a shape I imagined: it is the conflict git produced when
        merge `47af6bc2` met master (rebuilt from that merge's three real blobs with
        `git merge-tree`). Ours states the GUI count at `(92: ... + 3 preload-api +
        3 boot-contract)`; master states the same kind at `(89: ... + 3 preload-api)`
        - one component removed *and* the total re-measured 92 -> 89. The two sides
        share no line and are not the same length (1 vs 2), so neither the
        equal-length rule nor the "equal once digits are masked" comparison sees
        them, and the block was answered `disjoint - KEEP BOTH (concatenate)` at
        rc 0.

        The concatenation contains two `GUI: ` lines, which is precisely the state
        `tests/test_doc_counts.py::_duplicated_count_line_kinds` rejects - this test
        drives that guard over the concatenation rather than asserting the shape by
        eye. The correct verdict is `overlapping`: a human reads it.

        The axis that catches it is the unit the repo's own guard uses - *the same
        documented-count kind stated twice* - not "the lines are equal". Measured
        over 185 conflict blocks rebuilt from this repo's real merge commits, this
        rule changes exactly one class: this block. Nothing else moves.
        """
        ours = (
            "GUI: `cd emrg/gui && npm test` (92: 45 daemon_client + 20 conn-manager "
            "+ 8 integration + 6 build-config + 7 gui-state + 3 preload-api + "
            "3 boot-contract) — syntax: `node --check main.js`\n"
        )
        theirs = (
            "GUI: `cd emrg/gui && npm test` (89: 45 daemon_client + 20 conn-manager "
            "+ 8 integration + 6 build-config + 7 gui-state + 3 preload-api) — "
            "syntax: `node --check main.js`\n"
            "Renderer: `cd emrg/gui/renderer && npm run typecheck && npm test` (445: "
            "5 snapshot-store)\n"
        )
        label, advice = mod.classify(ours, theirs)
        assert label == mod.OVERLAPPING, (
            "one count kind at two revisions must not be called two separate "
            "additions: `KEEP BOTH` emits both copies"
        )
        assert "human must read" in advice

        # The consequence, driven through the repo's own guard rather than asserted:
        # concatenating duplicates the `GUI: ` kind, which is the rejected state.
        sys.path.insert(0, str(REPO_ROOT))
        try:
            from tests.test_doc_counts import _duplicated_count_line_kinds
        finally:
            sys.path.pop(0)
        concat = ours.rstrip("\n") + "\n" + theirs
        assert _duplicated_count_line_kinds(concat), (
            "the KEEP BOTH result must be the state the repo's guard rejects, "
            "otherwise this rule is not protecting anything"
        )

    def test_the_kind_prefix_is_the_kind_text_not_a_comment_marker(self, mod) -> None:
        """The prefix must be the *kind text*, located by match position.

        A claim about a file the file does not make, reproduced from review
        `cyc20260912-070619`: the clause masked digits with `#` and then split on
        `#` - but `#` begins a comment, so on a line whose prose contains a `#`
        before its digits, the "prefix" collapsed to bare indentation. Measured at
        the reviewed head, two unrelated comments both produced `'    '` and
        escalated each other; lines beginning with a digit produced `''` and matched
        on nothing at all.

        This test recomputes the prefix directly rather than only observing the
        verdict, so it fails on the *mechanism* and not merely on one consequence.
        """
        comment_a = "    # the daemon's OWN scheduler lost the file (93x) while retrying"
        comment_b = "    # a different subsystem failed (41x) here"
        # The prefix is the kind text, stripped: indentation names no kind, and
        # stripping is what makes the "no kind supplied" case detectable below.
        assert mod._count_kind_prefix(comment_a) == (
            "# the daemon's OWN scheduler lost the file"
        )
        assert mod._count_kind_prefix(comment_b) == "# a different subsystem failed"
        # Both are real kind text, not the bare indentation the split form produced.
        assert mod._count_kind_prefix(comment_a) != mod._count_kind_prefix(comment_b)
        # Two unrelated comments: different kinds, so no escalation.
        assert not mod._looks_like_a_count_revision([comment_a], [comment_b])

        # A digit-leading line still *has* kind text after the position lookup, so
        # two different ones do not collapse into a match. Under the split form both
        # masked to a leading `#` and produced `''`, so any two such lines were the
        # "same kind".
        assert mod._count_kind_prefix("3 things happened (12)") == "3 things happened"
        assert not mod._looks_like_a_count_revision(
            ["3 things happened (12)"], ["5 other things occurred (7)"]
        )
        # Whitespace only before the count is not kind text: with the tails also
        # different (so the masked-equality test above cannot fire), the None guard
        # is what keeps this pair from escalating on the strength of an empty
        # string.
        assert mod._count_kind_prefix("   (12) things") is None
        assert not mod._looks_like_a_count_revision(
            ["   (12) things"], ["   (7) other"]
        )

    def test_a_same_kind_line_with_a_different_tail_still_escalates(self, mod) -> None:
        """The deliberate widening, pinned so it stays deliberate.

        Review `cyc20260912-090216` asked for this widening to be stated and pinned
        rather than argued: the clause now keys on the kind text, so a same-kind pair
        that differs *after* the count fires too. `... (900) # 1 note` beside
        `... (900) # 2 notes` is neither a re-breakdown nor "one fact re-measured
        with a different breakdown" - it is the same kind with two different tails.

        It is accepted because the verdict errs toward a human read: both sides state
        the same count kind, so `KEEP BOTH` would concatenate a duplicate - the state
        `_duplicated_count_line_kinds` rejects. The alternative (a strict "compare the
        text between digit runs" test) was proposed and withdrawn in review
        `cyc20260912-090216`; withdrawn in part because it was measured wrong, see
        the re-breakdown test below.
        """
        assert mod._looks_like_a_count_revision(
            ["Python: `uv run pytest tests/ -v` (900) # 1 note"],
            ["Python: `uv run pytest tests/ -v` (900) # 2 notes"],
        )
        assert mod._looks_like_a_count_revision(
            ["Python: `uv run pytest tests/ -v` (900) covers the reader"],
            ["Python: `uv run pytest tests/ -v` (900) covers the writer"],
        )

    def test_the_rewritten_predicate_still_catches_the_re_breakdown(self, mod) -> None:
        """The kind-prefix form keeps the rule's only reason to exist.

        Review `cyc20260912-090216` withdrew an earlier fix on the grounds that a
        "compare the text between the digit runs" test returns False on this shape.
        That measurement is correct about *that* test, and does not apply to the
        kind-prefix form the first review proposed: the prefix stops at the first
        count, so a different number of digit runs is irrelevant. Measured here, both
        forms side by side, so the distinction is pinned rather than remembered.
        """
        ours = "GUI: `cd emrg/gui && npm test` (92: 89 GUI + 3 renderer)"
        theirs = "GUI: `cd emrg/gui && npm test` (89: 86 GUI + 3)"

        # kind-prefix form: fires (the shape must not regress)
        assert mod._looks_like_a_count_revision([ours], [theirs]), (
            "the re-breakdown is this clause's whole reason to exist"
        )
        # "text between the digit runs": does not fire - which is why that form was
        # the wrong proposal, not why this one is wrong.
        def between_digit_runs(line: str) -> str:
            return "|".join(mod._NUMBER.split(line))

        assert between_digit_runs(ours) != between_digit_runs(theirs)
        assert mod._count_kind_prefix(ours) == mod._count_kind_prefix(theirs)

    def test_a_count_re_breakdown_of_a_different_kind_does_not_escalate(
        self, mod
    ) -> None:
        """The negative control: the rule keys on the *kind*, not on "carries a count".

        Two genuinely different count lines - `Python:` against `GUI:` - are two
        facts, not one re-measured, so escalating them would be noise that sends a
        human to read a block no human needs to read. The text before the first
        count is what separates the two cases.
        """
        assert not mod._looks_like_a_count_revision(
            ["Python: `uv run pytest tests/ -v` (1494: 1 skipped) — x"],
            ["GUI: `cd emrg/gui && npm test` (100: 44 daemon_client) — y"],
        )
        assert not mod._looks_like_a_count_revision(
            ["Python: `uv run pytest tests/ -v` (1494) — x"],
            ["Renderer: `npm test` (514) — y"],
        )

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


# The master each recorded branch was reconstructed against. This has to be a
# fixed commit, never `origin/master`: the expectations below are statements
# about *historical* merges, and `origin/master` moves.
#
# Concretely - and this is issue #1160, where it cost a red master - `a73eba58`
# is #1140's head. While #1140 was open, merging it against master produced a
# genuine `disjoint` (two independent additions). After #1140 merged (`6797821`),
# merging that same branch against the new master puts master's own copy of the
# change on the "theirs" side, so the answer is correctly `duplicate` - and the
# recorded `disjoint` expectation fails on a tree where nothing is wrong. The
# expectation did not go stale because the classifier changed; it went stale
# because the base did. Measured (both cases, this file's own reconstruction):
#
#     base        a73eba58             7147666
#     147a80c     disjoint, disjoint    duplicate
#     efd6673     disjoint, disjoint    duplicate
#     c641859     disjoint, disjoint    duplicate   <- pinned
#     6797821     duplicate, duplicate  duplicate, identical, ...
#
# Pinned to `6797821^` - the master immediately before #1140 merged, which is
# also this branch's fork point - because that is the only base on which *both*
# recorded expectations hold at once.
#
# The pin is self-enforcing: reverting this to `origin/master` turns the suite
# red immediately, since master has already moved past `6797821` for good.
HISTORICAL_BASE = "c641859d687692a04d13f0574d8d1f8e30cfae39"


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
    `git merge --no-commit` against a *fixed* master (HISTORICAL_BASE), and the
    expected labels are the ones derived by hand during the cycle (and acted on,
    with verification after). Skips if the objects are unavailable (e.g. a
    shallow clone).

    The base is pinned rather than read from `origin/master` because these are
    claims about specific historical merges: "the`disjoint` case" means the one
    that existed while #1140 was open, not whatever the same two trees produce
    once that PR is on master (issue #1160).
    """
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{branch}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"{branch} is not available in this clone")

    base_probe = subprocess.run(
        ["git", "cat-file", "-e", f"{HISTORICAL_BASE}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if base_probe.returncode != 0:
        pytest.skip(f"pinned base {HISTORICAL_BASE[:8]} is not available in this clone")

    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(wt), branch],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(
            ["git", "merge", "--no-commit", "--no-ff", HISTORICAL_BASE],
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
