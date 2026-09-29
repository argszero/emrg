"""Each reading the evolution template points at is named once — at most twice.

Why this file exists
--------------------
The 2026-09-29 restructure (`cyc20260929-110933`) gave `emrg/server/evolution_prompt.md` one
home per rule, and this is the property that restructure bought: a reading script's name
appears where the rule lives and where the step that must call it occurs, and nowhere else.
Before it, the live-scan checklist was copied into seven steps, so "has this been stated
already" had no answer at all — and a copy free to drift from the rule the reading enforces
is indistinguishable from the rule. Issue #1572 is the measured cost: a mutation re-spelled
one copy of the CI parking rule back to `keep waiting` and **survived** the first guard
written for it.

The design this implements asks for exactly this count ("每个读数脚本名在文件里出现 ≤2 次"),
with the retired spellings scanned negatively as well. That half lives in
`tests/test_evolution_prompt_ci_parking.py`, because the spellings it retires belong to the
rule it pins; this module holds the half that is about every reading at once.

Named limits
------------
* The limit counts *names*, not statements. A rule restated in different words is invisible
  here; the sibling modules' `test_the_rule_is_stated_once` pin the clauses their rules are
  stated with, which is what makes a rewording of *those* rules visible.
* The floor of one is deliberate: a name that disappears from the template entirely fails
  here rather than passing quietly, so a script cannot be dropped while this list still
  claims the template points at it. Retiring a reading is therefore an edit to this list in
  the same change — visible in the diff, which is the point.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"

#: How many times the template may name one reading. One mention is the rule's home (Part B,
#: the rulebook) and one is the step that must call it (Part A, the loop); a third is a copy
#: free to drift from the rulebook.
MAX_MENTIONS = 2

#: The readings a cycle is told to run. Each must be named at least once and at most
#: `MAX_MENTIONS` times.
READING_SCRIPTS: tuple[str, ...] = (
    "scripts/review-queue.py",
    "scripts/check-vote-count.py",
    "scripts/check-merge-freshness.py",
    "scripts/check-merge-plan-suite.py",
    "scripts/check-issue-links.py",
    "scripts/find-host-message.py",
    "scripts/re-trigger-ci.sh",
)


def _over_stated(text: str, names: tuple[str, ...], limit: int = MAX_MENTIONS) -> list[str]:
    """The names stated more than `limit` times, each with the count it was stated at."""
    return [f"{name} ({text.count(name)}×)" for name in names if text.count(name) > limit]


def _never_named(text: str, names: tuple[str, ...]) -> list[str]:
    """The names the text does not carry at all."""
    return [name for name in names if name not in text]


def test_each_reading_is_named_at_most_twice() -> None:
    """One home, one call site — a third mention is a copy that can drift."""
    text = TEMPLATE.read_text(encoding="utf-8")
    over = _over_stated(text, READING_SCRIPTS)
    assert not over, (
        f"emrg/server/evolution_prompt.md names {over} at most twice each: the rulebook states "
        f"the rule and the step that must call it cites it (≤{MAX_MENTIONS} mentions). A third "
        "mention is a copy of the rule, and a copy is free to drift from the one the reading "
        "enforces — state it once, or cite it."
    )


def test_every_reading_in_this_list_is_still_named() -> None:
    """The count above must not be satisfiable by deleting the reading instead."""
    text = TEMPLATE.read_text(encoding="utf-8")
    absent = _never_named(text, READING_SCRIPTS)
    assert not absent, (
        f"emrg/server/evolution_prompt.md no longer names {absent}. Either the template "
        "stopped pointing at that reading — in which case remove it from READING_SCRIPTS in "
        "this same change, so the retirement is deliberate and visible — or the mention was "
        "dropped by accident."
    )


def test_the_count_can_report_a_third_mention() -> None:
    """The instrument's control: three mentions must read as over-stated, two must not.

    A count that reports the healthy answer whatever it is given is not a count. This feeds
    `_over_stated` and `_never_named` a text at, under and over the limit, so the two tests
    above are known to be reading something.
    """
    name = READING_SCRIPTS[0]
    assert _over_stated(f"{name} {name} {name}", READING_SCRIPTS) == [f"{name} (3×)"]
    assert _over_stated(f"{name} {name}", READING_SCRIPTS) == []
    assert _over_stated(name, READING_SCRIPTS) == []
    assert _never_named(name, READING_SCRIPTS) == list(READING_SCRIPTS[1:])
