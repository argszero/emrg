"""The row rule's arithmetic: `MEMORY_INDEX_ROW_CAP` rows of `INDEX_TITLE_MAX_CHARS`
must actually fit the budget the cap measures.

The class
---------
Three constants, one rule, stated in two units:

* `daemon.MEMORY_INDEX_ROW_CAP` — **lines**, the trigger the prompt's compaction
  instruction rests on ("an index past 100 lines is compacted in place");
* `memory.INDEX_TITLE_MAX_CHARS` — **chars per row**, the write-time truncation, which
  is what bounds a single line;
* `memory.INDEX_SIZE_WARN` — the **character budget** `_cap_memory_index` applies to
  the text it embeds (`daemon._cap_memory_index` reads it as a character count while the
  two advisory readers compare the same constant against a *byte* file size; that pair
  is `tests/test_memory_index_thresholds.py`'s subject, not this file's).

The trigger is safe only because the other two bound the file: with every line inside
the row cap, a line count at or below the row cap must not be able to overflow the
budget. That is the sentence `daemon.py` states — "it can fire on an index that would
have fit, never miss one that would not" — and it is an arithmetic claim about three
constants, which is exactly the kind of claim that has to be **measured**.

The claim that was false
------------------------
The derivation was written as `100 rows x 512 chars = 51,200 chars, which is the embed
cap's budget`. True as multiplication, false as a file: **a row owns its newline**, so
`n` maximum-width rows occupy `n * (512 + 1)` characters. Measured 2026-09-26 on master
`920d12c3`, with the product's own functions:

    99 lines x 512 chars: len= 50787 -> cap returns  50787 chars, truncated=False
    100 lines x 512 chars: len= 51300 -> cap returns  51069 chars, truncated=True
    101 lines x 512 chars: len= 51813 -> cap returns  51070 chars, truncated=True

So at exactly the row cap the cap **truncated** (keeping 50,787 chars = the first 99
rows and dropping the 100th) while the note's trigger, `lines > 100`, stayed silent —
a *miss*, the direction the comment promised was impossible. And the row it dropped is
the newest one, which is the failure class issue #1554 measures: the cap keeps the head
of an index whose rows append newest-last, so a silent truncation is precisely the rows
an agent needs in order not to redo work.

What has to hold, and how it is read here
-----------------------------------------
    MEMORY_INDEX_ROW_CAP * (INDEX_TITLE_MAX_CHARS + 1) <= INDEX_SIZE_WARN

The `+ 1` is each row's newline. The reading is not a restatement of that line: the
boundary is *built* and the real `_cap_memory_index` and the real
`_memory_index_compaction_note` are asked, so the model (one newline per line) is
checked against the mechanism rather than assumed — and the first test below fails
loudly if any of the three constants is retuned out of agreement with the other two.

Why the fix is the budget and not the row cap
---------------------------------------------
`MEMORY_INDEX_ROW_CAP = 100` is not a cycle's to move: `emrg/server/evolution_prompt.md`
carries the host's compaction rule as "past **100 lines**", and that number is stated
there in the host's own words. The budget is the constant nobody's instruction quotes,
so it is the one that moves to where the rule's arithmetic puts it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.memory import INDEX_SIZE_WARN, INDEX_TITLE_MAX_CHARS  # noqa: E402
from emrg.server.daemon import (  # noqa: E402
    MEMORY_INDEX_ROW_CAP,
    EmrgServer,
    _memory_index_compaction_note,
)


def _max_width_index(path: Path, lines: int) -> str:
    """Write `lines` rows of exactly `INDEX_TITLE_MAX_CHARS` chars each.

    The worst case the row rule permits, and the only shape in which its arithmetic
    can be decided: shorter rows leave a budget question whose answer depends on how
    much shorter they are, which is not the claim under test.
    """
    text = "\n".join("x" * INDEX_TITLE_MAX_CHARS for _ in range(lines)) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def _cap(path: Path) -> str:
    """The real cap, on an instance that was never constructed.

    `_cap_memory_index` reads nothing but the path it is handed, so this neither
    builds a server nor touches host state — the shape
    `tests/test_memory_index_thresholds.py::_cap_truncates` uses.
    """
    return EmrgServer.__new__(EmrgServer)._cap_memory_index(path)


def test_the_three_constants_can_hold_the_row_rule() -> None:
    """The derivation, from the constants themselves, with the newline term named.

    This is the general claim — it holds for every line count up to the row cap, so it
    is the half a retuned constant trips first. The two measurements below are the
    other half: they show the *model* behind it (one newline per line) is the one the
    cap actually applies.
    """
    worst_case = MEMORY_INDEX_ROW_CAP * (INDEX_TITLE_MAX_CHARS + 1)
    assert worst_case <= INDEX_SIZE_WARN, (
        f"{MEMORY_INDEX_ROW_CAP} rows of {INDEX_TITLE_MAX_CHARS} chars, each with its "
        f"own newline, are {worst_case} chars but the embed budget is "
        f"{INDEX_SIZE_WARN}: an index at the row cap can be truncated by "
        f"`_cap_memory_index` while `_memory_index_compaction_note` stays silent "
        f"(its trigger is `lines > {MEMORY_INDEX_ROW_CAP}`), which is the miss the "
        f"row cap exists to prevent. Either raise the budget to at least {worst_case} "
        f"or lower the row cap to "
        f"{INDEX_SIZE_WARN // (INDEX_TITLE_MAX_CHARS + 1)}"
    )


def test_a_full_width_index_at_the_row_cap_is_not_truncated(tmp_path: Path) -> None:
    """The boundary case, measured: the cap must answer with the file unchanged.

    On master `920d12c3` this is the assertion that failed — `_cap_memory_index`
    returned 50,787 of 51,300 chars, i.e. it dropped the newest row with no note
    having asked anyone to compact.
    """
    path = tmp_path / "MEMORY.md"
    text = _max_width_index(path, MEMORY_INDEX_ROW_CAP)

    # The fixture has to *be* the counterexample, or the assertion below is about a
    # file smaller than the one the arithmetic is about: it must exceed the budget the
    # rule's own multiplication quotes (rows x chars, without the newlines).
    assert len(text) > MEMORY_INDEX_ROW_CAP * INDEX_TITLE_MAX_CHARS, (
        "the fixture is not at the worst case the row rule permits"
    )

    capped = _cap(path)
    assert capped == text, (
        f"an index of {MEMORY_INDEX_ROW_CAP} maximum-width rows ({len(text)} chars) "
        f"was truncated by the embed cap (returned {len(capped)} chars) while its line "
        f"count ({MEMORY_INDEX_ROW_CAP}) is not over `MEMORY_INDEX_ROW_CAP`, so no "
        f"compaction note fires to warn anybody"
    )


def test_one_line_over_the_row_cap_asks_for_compaction(tmp_path: Path) -> None:
    """The other side of the boundary: past the row cap the note fires.

    Together with the test above this pins the ordering the comment claims — the agent
    is asked to compact by the time a maximum-width index can first be truncated — and
    it keeps the *conservative* direction honest as well: an index one line over the cap
    is still under the budget (51,813 <= 52,224 measured 2026-09-26) and the note fires
    anyway, which is the "can fire on an index that would have fit" half.
    """
    path = tmp_path / "MEMORY.md"
    text = _max_width_index(path, MEMORY_INDEX_ROW_CAP + 1)

    note = _memory_index_compaction_note([path])
    assert note, (
        f"an index of {MEMORY_INDEX_ROW_CAP + 1} lines drew no compaction note, so the "
        f"row rule is not firing where its own trigger says it must"
    )
    assert str(MEMORY_INDEX_ROW_CAP) in note
    # Said explicitly so the pair's claim is not overread: the note arrives *before*
    # the cap can cut, not as a consequence of a cut.
    assert _cap(path) == text, (
        f"the note fired, but the cap truncated the same file ({len(text)} chars) - the "
        f"two readers disagree about what the budget holds"
    )
