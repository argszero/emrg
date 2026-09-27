"""The shipped evolution template must state what finishes an issue, and how to read it.

Why this file exists
--------------------
Host rant 2026-09-26T18:52:47 (`~/.emrg/rants.jsonl`, project `emrg`), in the host's
words: *每个issue应该在一个pr里处理完毕。每个pr都应该明确对应哪个issue，并在对应的issue里
说明。也就是双方应该有明确的双向连接。发生pr被拒绝或者要求更正时，应该还是在这个pr里更新。*
— one issue is finished by exactly one PR, the two name each other in both directions, and
a rejected or change-requested PR is updated in place.

The rant's acceptance item 2 is a *prompt* item, and it is the half that was still open
when this file was added: the reading the rant asks for exists (`scripts/check-issue-links.py`,
opened by #1643), but nothing told a cycle it existed, and nothing said that re-measuring an
issue is not the same act as finishing it. Measured before this, which is why the sentence is
worth a guard rather than a line of prose: 43 cycles / 32.3 hours (`cyc20260926-165037`) merged
28 PRs while closing **0** issues, and four issues 99 cycles old had each been re-measured by
later cycles and claimed by none.

So §1.3 states the rule where a cycle reads the backlog — the state of the rule, the reading
that answers it, and what "finished" means — and this module pins it there, so a later prompt
edit cannot drop it in silence. It is the same shape as `tests/test_evolution_prompt_ci_parking.py`,
which pins the sibling rule the host gave on 2026-09-24.

Named limits
------------
* This pins the *presence* of the rule, not obedience to it: no test shows that an agent
  reading the template closes an issue instead of re-testing it.
* It does not check that `scripts/check-issue-links.py` exists — the reading lands on the same
  branch as this sentence (#1643), and a template sentence is not the tool.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"

#: The section heading a cycle looks under when it reads the backlog, and the verbatim
#: terms that section must carry: the rule, the reading that answers it, and the
#: completion criterion. Kept as short phrases so a re-wrap of the paragraph does not
#: fail the guard, while a deletion of the rule does.
SECTION = "**Issue management**:"

REQUIRED_TERMS: tuple[str, ...] = (
    "finished by exactly one PR",              # the rule
    "the two name each other",                 # ... in both directions
    "updated in place, never replaced",        # ... and how a change request is answered
    "scripts/check-issue-links.py",            # the reading
    "A re-measurement is not progress",        # what is not work
    "closed with that reading",                # the remedy for landed work
    "never a pass",                            # an unreadable queue is not a clean one
)


def _section(text: str, heading: str) -> str:
    """The template's `heading` block, up to the next Markdown heading of its level.

    The split is on `^#{3,4} ` rather than on a bare `#`, because the template's shell
    blocks carry `#` comments — splitting on those would cut the section off at its own
    example.
    """
    start = text.find(heading)
    assert start != -1, f"the template has no `{heading}` section"
    rest = text[start + len(heading):]
    end = re.search(r"^#{3,4} ", rest, flags=re.MULTILINE)
    return rest if end is None else rest[: end.start()]


def _missing_terms(section: str, terms: tuple[str, ...]) -> list[str]:
    """The terms this section does not carry. Empty means the rule is stated."""
    return [term for term in terms if term not in section]


def test_the_issue_section_states_the_rule_and_its_reading() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    missing = _missing_terms(_section(text, SECTION), REQUIRED_TERMS)
    assert not missing, (
        "emrg/server/evolution_prompt.md §1.3 must state what finishes an issue — one PR, "
        "named both ways, read with `scripts/check-issue-links.py`, and a re-measurement is "
        f"not progress (host rant 2026-09-26T18:52:47); missing: {missing}"
    )


def test_the_reading_is_not_quoted_as_a_pass_when_it_cannot_measure() -> None:
    """The rule's third limb: the reading separates a clean queue from an unreadable one.

    Pinned as its own test because it is the limb a later "tightening" edit is most likely to
    drop — the same distinction (`check-issue-links.py` exits 2 when `gh` failed or a payload
    did not parse) is stated in this repo's other guards, and a template sentence that folded
    the two would teach exactly the misreading those guards exist to prevent.
    """
    section = _section(TEMPLATE.read_text(encoding="utf-8"), SECTION)
    assert "never a pass" in section, (
        "emrg/server/evolution_prompt.md §1.3 must say that the link reading's unmeasurable "
        "answer (exit 2) is not a pass"
    )


def test_the_check_can_report_absence() -> None:
    """The instrument's control: a section without the terms must read as missing.

    A check that reports the healthy answer whatever it is given is not a check; this feeds
    it a template carrying the heading and none of the terms, and requires every term back.
    """
    stub = f"{SECTION}\n\n- New issues need replies or triage?\n\n#### 1.2 Follow up on your own PRs\n"
    assert _missing_terms(_section(stub, SECTION), REQUIRED_TERMS) == list(REQUIRED_TERMS)


def test_a_template_without_the_issue_section_is_not_silently_healthy() -> None:
    """A missing section is a failure to measure, never a pass."""
    with pytest.raises(AssertionError):
        _section("# A template with no issue section", SECTION)
