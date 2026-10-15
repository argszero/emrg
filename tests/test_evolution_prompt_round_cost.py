"""The cycle is told to read several things in one call — and given no round budget.

Why this file exists
--------------------
Item (a) of rant 2026-10-15T12:35:03, host decision 「就定 a + b（c 去掉）」 (2026-10-15
12:34:24, session `s_260727_1103_866d`; 「a 可以，c 没意义，肯定不行」 at 12:29:10). The
measurement behind it, taken the same day over the 45 cycles that finished on 2026-10-14:
the median cycle ran **66** tool-loop rounds and the median round brought **545** new tokens
— so a cycle pays its whole conversation again on every round that did one small thing. The
host chose to fix that by asking for **fewer calls that read more**, and explicitly refused
to fix it by capping rounds, because a cap truncates a long task.

Both halves are pinned here, because they are one decision: the batching rule without the
refusal is a rule a later cycle can re-spell as a budget, and the refusal without the rule
leaves the cost where it was. The rule lives in rulebook **R10**; §1 is where a cycle reads,
so §1 is the section that must point at it — a rule stated only where nobody is standing is a
rule the reader never reaches (the same property `test_evolution_prompt_ci_parking.py` pins
for R4).

Named limits
------------
* This counts *clauses*, not words. A rewording of R10 is caught by
  `test_the_rule_is_stated_once`; the shape it must not take (a budget) is caught by the
  negative scan.
* The negative scan names the budget spellings this rant rejected. A different cap wording
  would pass it — the positive clause "No round budget" is the arm that generalises.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"

#: Verbatim substrings of the shipped wording, per section a reader arrives from. R10 is the
#: rule's home; §1 is where the cycle is actually reading.
REQUIRED_TERMS: dict[str, tuple[str, ...]] = {
    "#### R10. One reading is one call": (
        "read in one call",           # the rule
        "join the commands",          # the action
        "Batch reads; never batch a judgement",  # the boundary
        "No round budget.",           # the refusal, stated positively
        "66",                         # the measurement that justifies it
    ),
    "### 1. Look": (
        "R10",                        # the pointer: a reader in §1 reaches the rule
    ),
}

#: A cap on rounds, in the spellings a cycle would reach for. The host rejected item (c)
#: outright; none of these may appear anywhere in the template.
BUDGET_SPELLINGS = (
    "round budget",
    "max rounds",
    "stop at 30 rounds",
    "cap the rounds",
    "limit your rounds",
    "fewer than 40 rounds",
)


def _section(text: str, heading: str) -> str:
    """The template's `heading` block, up to the next Markdown heading of its level."""
    start = text.find(heading)
    assert start != -1, f"the template has no `{heading}` section"
    rest = text[start + len(heading):]
    end = re.search(r"^#{3,4} ", rest, flags=re.MULTILINE)
    return rest if end is None else rest[: end.start()]


def _missing_terms(section: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term not in section]


def test_both_sides_of_the_loop_state_the_batching_rule() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    missing = {
        heading: _missing_terms(_section(text, heading), terms)
        for heading, terms in REQUIRED_TERMS.items()
    }
    assert not any(missing.values()), (
        "emrg/server/evolution_prompt.md must tell a cycle to read several things in one "
        "call and must not give it a round budget (host rant 2026-10-15T12:35:03 item (a)); "
        f"missing: { {k: v for k, v in missing.items() if v} }"
    )


def test_the_rule_is_stated_once() -> None:
    """One home for the rule: a second copy drifts from the first."""
    text = TEMPLATE.read_text(encoding="utf-8")
    found = text.count("read in one call")
    assert found == 1, (
        f"the batching rule is stated {found} times in emrg/server/evolution_prompt.md; "
        "it lives in rulebook R10 and every other section cites R10"
    )


#: The refusal sentence, verbatim. It names the thing it refuses, so the negative scan has
#: to remove it before scanning — otherwise the guard reddens on its own rule, which is the
#: false positive a negative scan reaches for whenever the prohibition mentions the banned
#: word. Nothing else in the template may say it.
REFUSAL_SENTENCE = "**No round budget.**"


def _without_refusal(text: str) -> str:
    """The template with the refusal sentence removed, so the scan sees only the rest."""
    assert REFUSAL_SENTENCE in text, (
        "the refusal sentence is gone, so this scan cannot tell a rule from a cap"
    )
    return text.replace(REFUSAL_SENTENCE, "")


def test_no_round_budget_appears_anywhere() -> None:
    """The rejected half of the rant stays rejected, wherever a later cycle spells it."""
    text = _without_refusal(TEMPLATE.read_text(encoding="utf-8"))
    hits = [spelling for spelling in BUDGET_SPELLINGS if spelling.lower() in text.lower()]
    assert not hits, (
        f"emrg/server/evolution_prompt.md caps the cycle's rounds ({hits}): the host "
        "rejected a round budget explicitly — 「c 没意义，肯定不行」 (rant "
        "2026-10-15T12:35:03 item (c)) — because it truncates a long task"
    )


def test_the_refusal_names_what_it_refuses() -> None:
    """The refusal is not a bare prohibition: it says the reason and the source.

    A cycle that reads "no round budget" without the reason re-derives the temptation and
    can talk itself out of it; the clause has to carry both the consequence (truncation) and
    the host's own decision, which is what makes it a red line rather than a preference.
    """
    section = _section(TEMPLATE.read_text(encoding="utf-8"), "#### R10.")
    assert "truncates long tasks" in section, (
        "the refusal must state the harm the host named, not only forbid the cap"
    )
    assert "2026-10-15T12:35:03" in section, (
        "the refusal must name the rant it comes from, so a later cycle can re-read it"
    )
