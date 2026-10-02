"""`grep`'s `max_results` is a maximum, in matches — measured both ways.

The schema says *"Maximum matches to return"*, and until now the parameter decided
three different things at once: the number of blocks returned (via an
``len(results) > max_results * (2 + context_before + context_after)`` test that kept
the extra block which proved there was more), a *line* budget for the print cut, and
nothing at all when the two disagreed.

Measured on master `eb20d6e3`, 2026-10-02, against a file of 300 matching lines:

| `max_results` | context | reported | blocks printed |
|---|---|---|---|
| 10 | none | **11** | 11 |
| 10 | 1 / 1 | **11** | 7 |
| 10 | 3 / 3 | **11** | 4 |
| 200 | 1 / 1 | **201** | 150 |

The first row is the one that needs no design question answered: with no context the
print budget never cut the extra block, so a documented **maximum** was silently
exceeded. The count is now taken where the cap is applied, and reaching the cap with a
match still in hand is what sets the floor note.

These live in their own file rather than in `test_grep_tool.py` because two other
changes touch that file (the trailing-newline rule and the searched-files count), and a
test file that merges cleanly is worth more than one that does not.
"""

import asyncio
import re
import sys
import tempfile
from pathlib import Path

import pytest

from emrg.tools.grep_tool import GrepTool


def _run(coro):
    return asyncio.run(coro)


def _grep(path, **kwargs):
    args = {"pattern": "TARGET", "path": str(path), "intent": "measure the cap"}
    args.update(kwargs)
    return _run(GrepTool().execute(args))


def _reported(content: str) -> int:
    """The number the summary claims."""
    match = re.search(r"Found (\d+) matches", content)
    assert match, content
    return int(match.group(1))


HEADER = re.compile(r"^[^ ].*:\d+:$")


def _blocks(content: str) -> list[list[str]]:
    """The returned output split into blocks by its own header shape."""
    out: list[list[str]] = []
    for line in content.split("\n"):
        if HEADER.match(line):
            out.append([line])
        elif out and (line.startswith("  ") or line.startswith(" >")):
            out[-1].append(line)
    return out


@pytest.fixture
def many(tmp_path) -> Path:
    """300 matching lines, each with a label line after it (context bait)."""
    body = []
    for i in range(300):
        body.append(f"label{i}:")
        body.append(f"TARGET {i}")
    f = tmp_path / "many.txt"
    f.write_text("\n".join(body) + "\n", encoding="utf-8")
    return f


class TestTheCountNeverExceedsTheMaximum:
    """The parameter's own word, in the direction that needs no interpretation."""

    @pytest.mark.parametrize("cap", [1, 10, 50, 200])
    @pytest.mark.parametrize("context", [(0, 0), (1, 1), (3, 3)])
    def test_the_reported_count_is_at_most_max_results(self, many, cap, context):
        result = _grep(many, max_results=cap,
                       context_before=context[0], context_after=context[1])

        assert not result.error
        assert _reported(result.content) <= cap, (
            f"asked for at most {cap} matches, the summary claims "
            f"{_reported(result.content)}"
        )

    @pytest.mark.parametrize("cap", [1, 10, 50, 200])
    def test_a_capped_search_reports_exactly_the_maximum(self, many, cap):
        """At the cap the count is the cap — not the cap plus the block that proved it."""
        result = _grep(many, max_results=cap)

        assert _reported(result.content) == cap, result.content.split("\n")[0]

    def test_the_returned_blocks_are_the_count(self, many):
        """The number and the output have to be the same fact."""
        for cap in (3, 10, 25):
            result = _grep(many, max_results=cap)
            assert _reported(result.content) == cap
            assert len(_blocks(result.content)) == cap, result.content

    def test_the_count_does_not_depend_on_the_context_asked_for(self, many):
        """Context changes what a block *contains*, never how many there may be."""
        counts = {
            ctx: _reported(_grep(many, max_results=10,
                                 context_before=ctx, context_after=ctx).content)
            for ctx in (0, 1, 3)
        }
        assert set(counts.values()) == {10}, counts


class TestTheFloorIsStillSaidOutLoud:
    """The cap is still a cap: the count is exact only when the scan finished."""

    def test_a_capped_scan_says_the_count_is_a_floor(self, many):
        result = _grep(many, max_results=10)

        assert "floor" in result.content, result.content.split("\n")[0]

    def test_a_scan_that_finished_carries_no_floor(self, tmp_path):
        """The control: one match, one file, nothing left to read."""
        f = tmp_path / "small.txt"
        f.write_text("one\nTARGET here\ntwo\n", encoding="utf-8")

        result = _grep(f)

        assert "floor" not in result.content
        assert _reported(result.content) == 1

    def test_a_capped_search_returns_the_cap_not_a_smaller_number(self, many):
        """The cap is what the caller asked for; a budget may not quietly halve it."""
        for cap in (5, 20):
            assert len(_blocks(_grep(many, max_results=cap).content)) == cap

    def test_the_floor_note_travels_with_the_summary(self, many):
        """The caveat belongs to the number, so it is in the sentence with it."""
        result = _grep(many, max_results=10)

        summary = result.content.split("\n")[0]
        assert "floor" in summary, summary
        assert str(10) in summary, summary


class TestThePrintNoteNamesACutItMade:
    """A notice is a measurement too — and a render that cut nothing did not make one."""

    def test_no_notice_when_the_budget_kept_every_block(self, many):
        """`max_results=1` keeps its one block whole; the budget is then moot.

        The first block is kept whatever the budget says (a notice with no output under
        it is worse than exceeding a soft line budget), so with a cap of one this render
        drops **nothing** — and a notice reading `truncated: 1 of 1 match blocks shown`
        is a cut that was never made.
        """
        result = _grep(many, max_results=1, context_before=1, context_after=2)

        assert "output truncated" not in result.content, result.content
        assert len(_blocks(result.content)) == 1

    def test_a_notice_that_is_printed_names_true_numbers(self, many):
        result = _grep(many, max_results=10, context_before=1, context_after=2)
        match = re.search(
            r"\[output truncated: (\d+) of (\d+) match blocks shown\]", result.content
        )

        assert match, result.content.split("\n")[-1]
        shown, total = int(match.group(1)), int(match.group(2))
        assert total == _reported(result.content), "the note's total is not the count"
        assert shown == len(_blocks(result.content)), "the note's 'shown' is not printed"
        assert shown < total, "a note is only appended when something was dropped"

    def test_the_cut_keeps_a_whole_number_of_blocks(self, many):
        result = _grep(many, max_results=10, context_before=1, context_after=2)
        blocks = _blocks(result.content)

        assert blocks
        assert "TARGET" in "\n".join(blocks[-1]), (
            "the last printed block carries no matching line, so the cut left a header "
            "whose match is gone"
        )


@pytest.mark.skipif(sys.platform == "win32", reason="path separator differs")
class TestTheOtherDirectionsAreUnchanged:
    """The fix is the cap; everything around it has to read the same as before."""

    def test_an_ordinary_search_is_untouched(self, tmp_path):
        f = tmp_path / "ord.txt"
        f.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")

        result = _grep(f, pattern="alpha")

        assert _reported(result.content) == 2
        assert "ord.txt:3:" in result.content

    def test_no_matches_still_says_so(self, tmp_path):
        f = tmp_path / "none.txt"
        f.write_text("alpha\n", encoding="utf-8")

        result = _grep(f, pattern="ZEBRA")

        assert result.content.startswith("No matches")
        assert not result.error

    def test_context_is_still_printed(self, tmp_path):
        f = tmp_path / "ctx.txt"
        f.write_text("before:\nTARGET\nafter:\n", encoding="utf-8")

        result = _grep(f, context_before=1, context_after=1)

        assert "  before:" in result.content
        assert "  after:" in result.content
        assert " >TARGET" in result.content
