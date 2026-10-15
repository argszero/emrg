"""The cycle is told to issue several tool calls in one round — and given no round budget.

Why this file exists
--------------------
Item (a) of rant 2026-10-15T12:35:03, host decision 「就定 a + b（c 去掉）」 (2026-10-15
12:34:24, session `s_260727_1103_866d`; 「a 可以，c 没意义，肯定不行」 at 12:29:10). The
measurement behind it, taken the same day over the 45 cycles that finished on 2026-10-14:
the median cycle ran **66** tool-loop rounds and the median round brought **545** new tokens
— so a cycle pays its whole conversation again on every round that did one small thing. The
host chose to fix that by asking for **fewer rounds that read more**, and explicitly refused
to fix it by capping rounds, because a cap truncates a long task.

The unit that first shipped for it was wrong, and rant 2026-10-15T21:21:29 (issue #2300)
corrected it. R10 said "read in one call" and taught joining several shell commands into one
`bash` call, while `emrg/server/prompts/system.j2` — the one prompt every session is rendered
under — states the rule as **several tool calls in one round** (host ruling 2026-10-15T16:32:06:
「批量的意思，是指一次下发多个工具调用，不是把多个工具调用合并为一个工具调用」, the
meaning of batching is issuing several tool calls at once, not merging several tool calls into
one). A cycle reads both carriers, so the two must state one unit and one criterion, and the
guard that keeps them in step compares them **both directions**: a carrier compared only with
its own past drifts from its sibling without anything going red.

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
* `RETIRED_SPELLINGS` names the unit the first version used. It is a spelling list, not a
  rule: a third way of saying "one call is the unit" would pass it, and the positive pins
  (which name the unit that is required) are the arms that generalise instead.
* The background scan reads one section. It cannot see a cycle that follows the rule and then
  backgrounds the command anyway — that is what the **Forbidden** listing is for. What it
  pins is narrower and checkable: R10 never *offers* a detached form as the way to batch.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"
#: The rule's other carrier. This file reads two files rather than one on purpose: `system.j2`
#: is what every session is rendered under and R10 is what a cycle reads, and the defect this
#: pair guards is the two of them disagreeing.
SYSTEM_PROMPT = REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2"

R10_HEADING = "#### R10. One round may carry several readings"

#: Verbatim substrings of the shipped wording, per section a reader arrives from. R10 is the
#: rule's home; §1 is where the cycle is actually reading.
REQUIRED_TERMS: dict[str, tuple[str, ...]] = {
    R10_HEADING: (
        "issue them together",                      # the rule
        "several tool calls in one round",          # the unit, spelled as system.j2 spells it
        "Batch readings; never batch a judgement",  # the boundary
        "outranks",                                 # the red line's precedence, named
        "No round budget.",                         # the refusal, stated positively
        "66",                                       # the measurement that justifies it
    ),
    "### 1. Look": (
        "R10",                                      # the pointer: a reader in §1 reaches it
    ),
}

#: The rule's unit and its criterion, as shipped in **both** carriers. Editing either one alone
#: is the defect this pair catches, so `test_both_carriers_state_the_same_unit_and_criterion`
#: reads the live text of each rather than one file's expectations about the other.
SHARED_UNIT = "several tool calls in one round"
SHARED_CRITERION = "that is a judgement, and a judgement gets its own round"

#: The unit this rule may no longer be stated in. The first version of R10 taught exactly
#: these; the host retired the merge-several-calls-into-one reading explicitly at
#: 2026-10-15T16:32:06, so none of them may come back anywhere in the template.
RETIRED_SPELLINGS = (
    "read in one call",
    "one reading is one call",
    "join the commands",
)

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

#: A detached form, in the spellings a cycle would reach for — plus the `&` character itself,
#: which is how a shell detaches. The `Forbidden` listing is where these belong; R10 must cite
#: that red line rather than repeat any of its forms as a way to batch.
BACKGROUND_SPELLINGS = (
    "nohup",
    "disown",
    "setsid",
    "detached",
    "in the background",
    "check later",
    "&",
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
        "emrg/server/evolution_prompt.md must tell a cycle to issue several tool calls in one "
        "round and must not give it a round budget (host rant 2026-10-15T12:35:03 item (a), "
        f"unit corrected by rant 2026-10-15T21:21:29); missing: "
        f"{ {k: v for k, v in missing.items() if v} }"
    )


def test_the_rule_is_stated_once() -> None:
    """One home for the rule: a second copy drifts from the first."""
    text = TEMPLATE.read_text(encoding="utf-8")
    found = text.count("issue them together")
    assert found == 1, (
        f"the batching rule is stated {found} times in emrg/server/evolution_prompt.md; "
        "it lives in rulebook R10 and every other section cites R10"
    )


def test_the_retired_unit_is_gone() -> None:
    """The unit the first version taught may not come back, in the template or beside it."""
    text = TEMPLATE.read_text(encoding="utf-8")
    hits = [spelling for spelling in RETIRED_SPELLINGS if spelling.lower() in text.lower()]
    assert not hits, (
        f"emrg/server/evolution_prompt.md states the rule in the retired unit {hits}: the host "
        "fixed the unit at 2026-10-15T16:32:06 — several tool calls in one round, not several "
        "calls merged into one — and system.j2 already carries it (issue #2300)"
    )


def test_both_carriers_state_the_same_unit_and_criterion() -> None:
    """One rule, two carriers: the unit and the criterion must be the *same* text in both.

    Comparing each carrier against its own past cannot see a drift between them, and the
    drift is what happened (issue #2300): the two prompts stated opposite units for one rule
    and both files' guards stayed green. The comparison therefore runs on the shipped text of
    each, and names the file whose copy moved.
    """
    carriers = {
        "emrg/server/evolution_prompt.md (R10)": _section(
            TEMPLATE.read_text(encoding="utf-8"), R10_HEADING
        ),
        "emrg/server/prompts/system.j2": SYSTEM_PROMPT.read_text(encoding="utf-8"),
    }
    for marker, label in ((SHARED_UNIT, "unit"), (SHARED_CRITERION, "criterion")):
        missing = [name for name, text in carriers.items() if marker not in text]
        assert not missing, (
            f"the {label} `{marker}` is not in {missing}: R10 and system.j2 carry one rule, so "
            "a change to either one moves both (issue #2300, rant 2026-10-15T21:21:29)"
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


def test_r10_offers_no_background_form() -> None:
    """The rule is bought with the round, and R10 may not sell a detached form as the price.

    R10 cites the red line by name and its own text names none of the forms. An invitation
    added here — "issue it, then `nohup` the slow one" — would read as a licence the rest of
    the template spends a paragraph forbidding, and it is a licence a cycle would act on.
    """
    section = _section(TEMPLATE.read_text(encoding="utf-8"), R10_HEADING)
    assert "outranks" in section, (
        "R10 must cite the background-process red line's precedence — without that sentence "
        "the scan below cannot say what a cycle should do instead of detaching"
    )
    hits = [spelling for spelling in BACKGROUND_SPELLINGS if spelling in section]
    assert not hits, (
        f"R10 names the detached form(s) {hits} as part of the batching rule: the red line in "
        "**Forbidden** outranks this rule, and batching is a technique for the round, never "
        "for the shell (rant 2026-10-15T21:21:29 acceptance 2)"
    )
