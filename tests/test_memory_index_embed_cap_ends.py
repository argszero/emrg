"""Which parts of an over-budget index survive, and who is told (issue #1676).

Two mechanisms that had never been read against each other are pinned together here:

* `EmrgServer._cap_memory_index` — the embed cap, which cuts an index before it reaches
  a prompt. Its **direction** was an open decision until 2026-09-28: keeping the head
  alone left the reader with the oldest part of a file whose rows append newest-last
  (issue #1554's reading). The decision taken is *keep the head and the tail, drop the
  middle*, stated at `MEMORY_INDEX_CAP_HEAD_CHARS`, and it is measured here on a fixture
  whose rows are numbered so the parts are read, not described.
* `_memory_index_compaction_note` — the instruction that asks the agent to compact. Its
  trigger counted **lines** while the cap cuts on **characters**, so an index could be
  truncated on every request with nobody told: measured 2026-09-28, a 71-line index of
  81,974 bytes was `lines 71 of 100 - within` while the tail it lost never reached a
  prompt. The trigger now fires on any number the rule names — the line cap, the embed
  budget, or a row past the bound — so "the cap would cut this file" implies "the agent
  is asked to compact it", which is the invariant the last test below measures.

The rejected alternatives are recorded where the decision is (the constant's comment and
`_cap_memory_index`), not restated here: what a test can hold is the shape, and the shape
is that neither end is the one dropped.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.memory import INDEX_SIZE_WARN, INDEX_TITLE_MAX_CHARS  # noqa: E402
from emrg.server.daemon import (  # noqa: E402
    MEMORY_INDEX_CAP_HEAD_CHARS,
    MEMORY_INDEX_ROW_CAP,
    EmrgServer,
    _memory_index_compaction_note,
)

#: The notice's shape, so the count it prints can be read back out of the text the
#: prompt carries rather than recomputed from the budget.
NOTICE = re.compile(r"\n… \[truncated (\d+) chars[^\]]*\]\n")


def _cap(path: Path) -> str:
    """The real cap, on an instance that was never constructed.

    `_cap_memory_index` reads nothing but the path it is handed, so this neither builds
    a server nor touches host state — the shape `test_daemon.py::_cap_memory_index_*`
    and `tests/test_memory_index_thresholds.py` use.
    """
    return EmrgServer.__new__(EmrgServer)._cap_memory_index(path)


def _numbered_index(path: Path, rows: int) -> str:
    """An index of `rows` numbered rows, every row the same width.

    Numbered so the cut is *measured*: row0000 is the head of the file, row0599 the
    newest row in it, and which of them survive is read off the text the cap returns.
    """
    text = "".join(
        f"- [row{i:04d}](cycle-20260823-{i:06d}.md) — " + "x" * 100 + "\n"
        for i in range(rows)
    )
    path.write_text(text, encoding="utf-8")
    return text


def _table_index(path: Path, rows: int, row_chars: int) -> str:
    """An index in Markdown-table form, past the embed budget.

    The shape that made the old trigger's blind spot matter: a table's lines do not
    start with `- `, so every *row* reading in this family reports zero rows for it
    while its size is what the cap acts on.
    """
    lines = ["# Memory Index", "", "| id | note |", "| --- | --- |"]
    lines += ["| n | " + "x" * row_chars + " |" for _ in range(rows)]
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def _kept_rows(capped: str, rows: int) -> tuple[list[int], list[int]]:
    """The row numbers present in `capped`, and the ones missing from it."""
    kept = [i for i in range(rows) if f"row{i:04d}" in capped]
    lost = [i for i in range(rows) if i not in kept]
    return kept, lost


# ── which parts survive ───────────────────────────────────────────────────────


def test_the_head_and_the_tail_survive_and_the_middle_is_dropped(tmp_path: Path) -> None:
    """The decision, measured: the rows lost are strictly between two kept rows.

    One assertion carries both arms of the alternative it rejects — a cap keeping only
    the head loses `max(kept)`, a cap keeping only the tail loses `min(kept)`, and
    either makes the two comparisons below false.
    """
    path = tmp_path / "MEMORY.md"
    rows = 600
    text = _numbered_index(path, rows)
    assert len(text) > INDEX_SIZE_WARN, "the fixture must be one the cap acts on"

    capped = _cap(path)
    kept, lost = _kept_rows(capped, rows)
    assert kept and lost, "the cap kept everything or nothing: the fixture proves nothing"
    assert min(kept) < min(lost) and max(lost) < max(kept), (
        f"the rows dropped are not the middle (kept {min(kept)}..{max(kept)}, lost "
        f"{min(lost)}..{max(lost)}): the head row0000 and the newest row{rows - 1:04d} "
        f"must both survive, and the rows between them must be what is missing"
    )
    assert min(kept) == 0 and max(kept) == rows - 1, (
        "the first and the last row of the file are the two ends the decision keeps"
    )


def test_the_notice_names_the_parts_and_the_file_and_counts_what_it_dropped(
    tmp_path: Path,
) -> None:
    """The notice may not disagree with the mechanism, and its number is measured.

    The text is embedded in every request, so it is read far more often than it is
    written: a sentence naming the wrong part is worse than no sentence. The count is
    read back out of the notice and checked against the text actually removed, rather
    than against the budget — a notice stating a derived number is only true while the
    derivation is.
    """
    path = tmp_path / "MEMORY.md"
    text = _numbered_index(path, 600)

    capped = _cap(path)
    match = NOTICE.search(capped)
    assert match, "an over-budget index must carry a truncation notice"
    assert "head and the tail are kept" in capped and "middle is dropped" in capped, (
        "the notice must name the parts of the file the reader still holds"
    )
    assert "oldest rows are missing" in capped, (
        "which rows are missing is the part a reader acts on"
    )
    assert str(path) in capped, (
        "the notice must name the file the dropped text is still in (issue #1551)"
    )
    assert int(match.group(1)) == len(text) - len(NOTICE.sub("", capped)), (
        f"the notice says {match.group(1)} chars were dropped; the text it returns is "
        f"missing a different number"
    )
    assert "row0000" in capped and "row0599" in capped


def test_what_the_cap_keeps_is_within_the_budget_it_caps_to(tmp_path: Path) -> None:
    """The cap's own promise: what reaches the prompt is inside the budget.

    Two slices and a notice are assembled here, so the arithmetic could drift from the
    budget it is supposed to respect; this reads the kept text rather than the slices.
    """
    path = tmp_path / "MEMORY.md"
    _numbered_index(path, 600)

    capped = _cap(path)
    kept = NOTICE.sub("", capped)
    assert len(kept) <= INDEX_SIZE_WARN, (
        f"the cap returned {len(kept)} chars of index for a budget of {INDEX_SIZE_WARN}"
    )
    assert MEMORY_INDEX_CAP_HEAD_CHARS < INDEX_SIZE_WARN, (
        "the head's share must leave room for a tail, or the decision is head-only "
        "under another name"
    )


def test_an_index_within_the_budget_is_returned_whole(tmp_path: Path) -> None:
    """The other direction: nothing over the budget is touched, notice included."""
    path = tmp_path / "MEMORY.md"
    text = "# Index\n- [x](x.md) — a row\n"
    path.write_text(text, encoding="utf-8")

    assert _cap(path) == text


# ── who is told ───────────────────────────────────────────────────────────────


def test_the_trigger_fires_for_an_index_over_the_budget_under_the_line_cap(
    tmp_path: Path,
) -> None:
    """Issue #1676's acceptance: many lines is not the only way to be over.

    The fixture is one the old trigger was silent about — 73 table lines, under the
    100-line cap, over the budget — which is exactly the state in which the cap cuts a
    file while nobody is asked to compact it.
    """
    path = tmp_path / "MEMORY.md"
    text = _table_index(path, rows=71, row_chars=745)
    assert len(text.splitlines()) <= MEMORY_INDEX_ROW_CAP, (
        "the fixture must stay under the line cap, or the old trigger would have fired"
    )
    assert len(text) > INDEX_SIZE_WARN, "the fixture must be one the cap cuts"

    note = _memory_index_compaction_note([path])
    assert note, (
        "an index the cap cuts drew no compaction note: the trigger is not asking "
        "about the condition the cap enforces"
    )
    assert f"{INDEX_SIZE_WARN}-char embed budget" in note, (
        "the note must name the reading that fired it, not the line cap it is under"
    )
    assert f"past the {MEMORY_INDEX_ROW_CAP}-line cap" not in note, (
        "the note claims a breach of the line cap in a file that is under it: the "
        "instruction must state the number it acted on"
    )


def test_the_trigger_fires_for_a_row_past_the_bound(tmp_path: Path) -> None:
    """The rule's second number had no trigger at all; it has one now."""
    path = tmp_path / "MEMORY.md"
    path.write_text(
        "# Index\n- [x](x.md) " + "y" * (INDEX_TITLE_MAX_CHARS + 10) + "\n",
        encoding="utf-8",
    )

    note = _memory_index_compaction_note([path])
    assert note, "a row past the bound is a breach of the rule and drew no note"
    assert f"row(s) past {INDEX_TITLE_MAX_CHARS} chars" in note
    assert str(INDEX_TITLE_MAX_CHARS + 10 + len("- [x](x.md) ")) in note, (
        "the note must print the longest row it counted"
    )


def test_the_trigger_counts_a_table_row(tmp_path: Path) -> None:
    """A table's rows are rows to the *trigger*, not only to the script.

    Both readers call `memory.is_index_row`; each spelled `- ` before it existed, so a
    table index's per-row breach was invisible to both. Measured 2026-10-01 on this host:
    `.emrg/memory/MEMORY.md` (a table, 36 lines, 13,398 chars) drew no note at all while
    four of its rows were past the bound.
    """
    path = tmp_path / "MEMORY.md"
    row = "| f2a71c04 | decision | " + "z" * (INDEX_TITLE_MAX_CHARS + 40) + " | active |"
    path.write_text(
        "# Memory Index\n\n| ID | Type | Title | Status | Updated |\n|---|---|---|---|---|\n"
        f"{row}\n",
        encoding="utf-8",
    )
    text = path.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= MEMORY_INDEX_ROW_CAP, "under the line cap, on purpose"
    assert len(text) <= INDEX_SIZE_WARN, "under the budget, on purpose"

    note = _memory_index_compaction_note([path])
    assert note, "a table row past the bound drew no compaction note"
    assert f"row(s) past {INDEX_TITLE_MAX_CHARS} chars" in note
    assert str(len(row)) in note, "the note must print the longest row it counted"


def test_the_trigger_counts_an_indented_row(tmp_path: Path) -> None:
    """The second reader inherits the indentation limit, so an indented row draws a note.

    The trigger calls the same `memory.is_index_row`, so this is the same fix read
    through the other reader - and the reason the fix belongs in the predicate rather
    than in the script: a script-side strip would leave the daemon's compaction note
    silent about an index the guard would now flag. Measured on `890bf02d` 2026-10-01:
    a two-space-indented row past the bound drew no note.
    """
    path = tmp_path / "MEMORY.md"
    row = "  - [x](x.md) " + "y" * (INDEX_TITLE_MAX_CHARS + 10)
    path.write_text("# Index\n" + row + "\n", encoding="utf-8")
    assert len(path.read_text(encoding="utf-8")) <= INDEX_SIZE_WARN, "under the budget, on purpose"

    note = _memory_index_compaction_note([path])
    assert note, "an indented row past the bound drew no compaction note"
    assert f"row(s) past {INDEX_TITLE_MAX_CHARS} chars" in note
    assert str(len(row)) in note, "the note must print the longest row it counted"


def test_the_trigger_leaves_a_four_space_line_alone(tmp_path: Path) -> None:
    """And the boundary holds for the second reader too: four spaces draws nothing.

    Both readers share one predicate, so the control has to be asserted on both sides
    or the pair proves nothing about the predicate: this is the same fixture as the test
    above with two more spaces, and it must be silent.
    """
    path = tmp_path / "MEMORY.md"
    path.write_text(
        "# Index\n    - [x](x.md) " + "y" * (INDEX_TITLE_MAX_CHARS + 10) + "\n",
        encoding="utf-8",
    )
    assert _memory_index_compaction_note([path]) == "", (
        "a four-space line is an indented code block, not a row"
    )


def test_a_compliant_index_draws_nothing(tmp_path: Path) -> None:
    """The direction that keeps the note worth reading: silence when nothing is over."""
    path = tmp_path / "MEMORY.md"
    path.write_text("# Index\n- [x](x.md) — a row\n", encoding="utf-8")

    assert _memory_index_compaction_note([path]) == ""


def test_every_index_the_cap_cuts_draws_the_note(tmp_path: Path) -> None:
    """The invariant the two mechanisms owe each other, over several shapes.

    Each shape is *asked* whether the cap cuts it, so the case list cannot drift from
    the cap's condition: a shape the cap no longer cuts stops being evidence, and one it
    cuts must be answered by a note. The long-row shape is the control for the opposite
    error — a file that is over the rule in a way the cap does not act on must not be
    required to draw a note by this test.
    """
    shapes = {
        "many-lines": "- [r](r.md)\n" * (MEMORY_INDEX_ROW_CAP + 1),
        "table-over-budget": _table_index(tmp_path / "t.md", rows=71, row_chars=745),
        "max-width-rows": ("- " + "z" * (INDEX_TITLE_MAX_CHARS - 3) + "\n")
        * MEMORY_INDEX_ROW_CAP,
        "one-long-row": "# I\n- [x](x.md) " + "y" * 4000 + "\n",
    }
    for name, text in shapes.items():
        path = tmp_path / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        cuts = _cap(path) != text
        note = _memory_index_compaction_note([path])
        assert not cuts or note, (
            f"{name}: the cap cuts this file ({len(text)} chars, "
            f"{len(text.splitlines())} lines) and nothing asks the agent to compact it"
        )
