"""The shipped evolution template must tell a cycle to *park* a running CI, not wait.

Why this file exists
--------------------
Host rant 2026-09-24T14:46:10 (`~/.emrg/rants.jsonl`), in the host's words: after
opening a PR the cycle always waits for CI, and it should not — each cycle should read
the open PRs' CI itself, ignore the ones still running and do other work, then come
back to a PR once its run has concluded.

Two habits carried the old behaviour, one per side of the loop, and both were stated
in prose only:

* the submitter's — ``then wait for the run to complete before LGTMing`` in §1.1, and
  ``Not satisfied → keep waiting`` further down the review list;
* the submitter's in §5 — nothing said what to do after ``gh pr create``, so a cycle
  that had just pushed sat on the run it had started.

The window a cycle spends blocking is the failure: the head it just pushed is one it
may neither vote on nor merge, so the verdict it waits ~10 minutes for is unusable by
that window, and the window itself is gone. The replacement rule is stated where each
reader looks — §1.1 (the `gh pr checks` bullet) and §5 (just after `gh pr create`) —
and this module pins it there, so a later prompt edit cannot drop it in silence.

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

#: Verbatim substrings of the shipped wording, per section a reader looks in. The
#: reviewer-side section is where `gh pr checks` is explained; the submit section is
#: where a cycle has just pushed and is tempted to watch the run conclude.
REQUIRED_TERMS: dict[str, tuple[str, ...]] = {
    "#### 1.1 Repo Management": (
        "A run that has not concluded is a `park`, never a `wait`",  # the rule
        "park this PR and move on",                                  # the action
        "read it again next cycle",                                  # when it comes back
        # The merge-conditions bullet, which the PR rewrote into "park it …" — the third
        # site the rule is stated at. Pinned verbatim because a mutation arm re-spelled it
        # back to "keep waiting" and *survived* the first version of this table (issue
        # #1572): the old spelling is only caught for the two phrases in `REMOVED_TERMS`,
        # and this site has its own. A section-wide negative scan is not available - §1.1
        # legitimately contains the rule's own words ("never a `wait`", "not a row to
        # block on") - so the site is pinned by what it must say.
        "Not satisfied → **park it and go do other work in this cycle**",
    ),
    "### 5. Submit": (
        "Submitting ends at `gh pr create`",  # the rule
        '"CI pending"',                       # pending is never recorded as a pass
        'never "CI green"',                   # the misreading it forbids
    ),
}

#: The two habits the rant removed, verbatim. Their return is the regression this file
#: exists to catch, wherever in the template it happens.
REMOVED_TERMS = (
    "then wait for the run to complete before LGTMing",
    "Not satisfied → keep waiting",
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
    stub = "#### 1.1 Repo Management\n\n- something else\n\n### 5. Submit\n\n- something else\n"
    missing = {
        heading: _missing_terms(_section(stub, heading), terms)
        for heading, terms in REQUIRED_TERMS.items()
    }
    assert all(v == list(REQUIRED_TERMS[k]) for k, v in missing.items())


def test_a_template_without_the_review_section_is_not_silently_healthy() -> None:
    """A missing section is a failure to measure, never a pass."""
    with pytest.raises(AssertionError):
        _section("# A template with no review section", "#### 1.1 Repo Management")
