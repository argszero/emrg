"""The shipped evolution template must tell a cycle to *park* a running CI, not wait.

Why this file exists
--------------------
Host rant 2026-09-24T14:46:10 (`~/.emrg/rants.jsonl`), in the host's words: after
opening a PR the cycle always waits for CI, and it should not — each cycle should read
the open PRs' CI itself, ignore the ones still running and do other work, then come
back to a PR once its run has concluded.

Two habits carried the old behaviour, one per side of the loop, and both were stated
in prose only:

* the reviewer's — ``then wait for the run to complete before LGTMing`` in §1.1, and
  ``Not satisfied → keep waiting`` further down the review list;
* the submitter's in §5 — nothing said what to do after ``gh pr create``, so a cycle
  that had just pushed sat on the run it had started.

The window a cycle spends blocking is the failure: the head it just pushed is one it
may neither vote on nor merge, so the verdict it waits ~10 minutes for is unusable by
that window, and the window itself is gone. After the 2026-09-29 restructure (`cyc20260929-110933`) the rule has **one home** —
rulebook §R4 — and the two sections a reader arrives from cite it: §1.1 takes the action
`scripts/review-queue.py` names for a row, and §5 states the post-`gh pr create` rule. That
is what makes the negative scan below possible: while the rule was copied into three
sections, no section-wide (let alone file-wide) scan could tell the rule's own words from a
duplicate of them — this module's own docstring used to record exactly that limit.

Named limit
-----------
This pins the *presence* of the rule, not obedience to it. No test can show that an
agent reading the template parks instead of blocking; what it can show is that the
template never again tells a cycle to wait — `tests/test_review_queue.py` and
`tests/test_check_merge_freshness.py` guard the two tools' side of the same rule
(`kind="park"`, "park it, the run has not concluded"), which is where the conduct is
mechanically readable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"

#: Verbatim substrings of the shipped wording, per section a reader arrives from. R4 is
#: the rule's home; §5 is where a cycle has just pushed and is tempted to watch the run
#: conclude; §1.1 is where the queue is worked.
REQUIRED_TERMS: dict[str, tuple[str, ...]] = {
    "#### R4. CI: three states, three actions": (
        "A run that has not concluded is a `park`, never a `wait`",  # the rule
        "park this PR and move on",                                  # the action
        "read it again next cycle",                                  # when it comes back
    ),
    "### 5. Submit": (
        "Submitting ends at `gh pr create`",  # the rule
        '"CI pending"',                       # pending is never recorded as a pass
        'never "CI green"',                   # the misreading it forbids
    ),
}

#: Where a reader arrives from, and the citation that must send them to the rule. The
#: section a reader is in is the section they act in, so a rule stated only in a rulebook
#: nobody is pointed at is a rule the reader never reaches.
POINTER_TERMS: dict[str, str] = {
    "#### 1.1 Repo management": "R4",
    "#### R3. Merging": "R4",
}

#: The rule's own opening words. Stated once, in R4: a second copy is a copy free to
#: drift, which is the defect this file's negative scan was written for.
RULE_SENTENCE = "A run that has not concluded is a `park`, never a `wait`"

#: The two habits the rant removed, verbatim. Their return is the regression this file
#: exists to catch, wherever in the template it happens.
REMOVED_TERMS = (
    "then wait for the run to complete before LGTMing",
    "Not satisfied → keep waiting",
    "keep waiting",
    "wait for the run to complete",
)


def _section(text: str, heading: str) -> str:
    """The template's `heading` block, up to the next Markdown heading of its level.

    The split is on `^#{3,4} ` rather than on a bare `#`, because the template's shell
    blocks carry `#` comments — splitting on those would cut a section off at its own
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


def test_both_sides_of_the_loop_state_the_parking_rule() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    missing = {
        heading: _missing_terms(_section(text, heading), terms)
        for heading, terms in REQUIRED_TERMS.items()
    }
    assert not any(missing.values()), (
        "emrg/server/evolution_prompt.md must tell a cycle to park a PR whose CI has not "
        "concluded rather than wait on it (host rant 2026-09-24T14:46:10); missing: "
        f"{ {k: v for k, v in missing.items() if v} }"
    )


def test_the_removed_habits_have_not_come_back() -> None:
    """The rule is a replacement, not an addition — the old instruction must be gone."""
    text = TEMPLATE.read_text(encoding="utf-8")
    found = [term for term in REMOVED_TERMS if term in text]
    assert not found, (
        "the template must not tell a cycle to wait for CI to finish — a window that "
        f"blocks is a window not spent (host rant 2026-09-24T14:46:10); found: {found}"
    )


def test_the_checks_can_report_absence() -> None:
    """The instrument's control: the sections without the rule must read as missing.

    A check that reports the healthy answer whatever it is given is not a check; this
    feeds it a template carrying both headings and none of the terms, and requires
    every term to come back missing.
    """
    stub = (
        "#### 1.1 Repo Management\n\n- something else\n"
        "### 5. Submit\n\n- something else\n"
        "#### R3. Merging\n\n- something else\n"
        "#### R4. CI: three states, three actions\n\n- something else\n"
    )
    missing = {
        heading: _missing_terms(_section(stub, heading), terms)
        for heading, terms in REQUIRED_TERMS.items()
    }
    assert all(v == list(REQUIRED_TERMS[k]) for k, v in missing.items())


def test_a_template_without_the_review_section_is_not_silently_healthy() -> None:
    """A missing section is a failure to measure, never a pass."""
    with pytest.raises(AssertionError):
        _section("# A template with no review section", "#### 1.1 Repo Management")


def test_each_section_a_reader_arrives_from_points_at_the_rule() -> None:
    """The rule has one home; the sections that act on it must name that home.

    Without this the rewrite would be the opposite failure: the rule stated once, in a
    place the reader working the queue is never sent to. `scripts/review-queue.py` prints
    the action word on the row, and this is the prompt side of the same instruction.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    missing = [
        f"{heading} does not cite {term}"
        for heading, term in POINTER_TERMS.items()
        if term not in _section(text, heading)
    ]
    assert not missing, (
        "the parking rule lives in rulebook R4, so every section that acts on it must "
        f"cite R4 rather than restate it; {missing}"
    )


def test_the_rule_is_stated_once() -> None:
    """One statement, in the rulebook — the property the restructure exists to gain.

    While the rule lived in three sections, no scan could separate the rule's own words
    from a duplicate of them: this module's docstring recorded that limit, and a mutation
    arm that re-spelled one copy back to `keep waiting` survived the first version of the
    table here (issue #1572). With one home, "how many times is it stated" is answerable.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    found = text.count(RULE_SENTENCE)
    assert found == 1, (
        f"the parking rule is stated {found} times in emrg/server/evolution_prompt.md; "
        "it lives in R4 alone, and a second copy is a copy free to drift from it"
    )


def test_the_retired_wait_spellings_are_absent_everywhere() -> None:
    """The global negative scan the duplicated rule made impossible.

    The question is not "is the rule still in its section" but "does the template tell a
    cycle to wait anywhere at all" — a file-wide question, and one only answerable once
    each spelling has a single legal home. It covers the wording the rule replaced, in
    every form the corpus has carried it.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    found = [term for term in REMOVED_TERMS if term in text]
    assert not found, (
        "the template tells a cycle to wait for a run to conclude: " + repr(found) + ". "
        "A window that blocks is a window not spent — the head it waits on is one it can "
        "neither vote on nor merge."
    )
