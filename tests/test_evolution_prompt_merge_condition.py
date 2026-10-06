"""The shipped template's merge condition must name the rule the counter enforces.

Why this file exists
--------------------
The template states the merge condition as *"3 consecutive ✅ LGTMs from different
cycles with no ❌ in between"* and tells the cycle to read it off the PR's comment
history. `scripts/check-vote-count.py` exists because that reading is wrong, and its
own docstring carries the measurement (2026-09-11: #1133/#1134/#1136/#1137 each
showed 4-6 "✅ LGTM" lines and each had **0 valid votes**). Its condition has a clause
the template's sentence did not: **a vote submitted before the head push is void**.
So an instance whose task prompt is built from the shipped template was told a rule
narrower than the one this project's gates measure, and pointed at a route (count the
✅ lines by hand) that answers wrongly whenever a head has moved.

A sibling defect — the vote *body* — was fixed the same way (#1409): the template's
example now names the cycle the counter reads. This file is the other half of that
contract, the *condition* rather than the body, pinned where a cycle reads it
(§1.1 Step 1's "Check merge conditions" and §5's "Merge condition").

Why the pin renders instead of grepping the template
----------------------------------------------------
The template is rendered with ``undefined=jinja2.Undefined``, so a name that is not
in the builder's context renders as the empty string: text present in the file can be
absent from the prompt an instance receives, and a file-level grep would still pass.
These tests render through the *real* builder (``TaskHandler._build_evolution_prompt``)
and read the rendered prompt.

The control
-----------
A prose pin can only show that a sentence is present, so the last test drives the
counter itself over the same three approvals twice — once submitted after the head
push, once before it. That is what makes the phrase list below evidence rather than
decoration: the clause the template now states is one the instrument enforces.

Named limit
-----------
This pins that the condition and its instrument are stated where the prompt is built.
It cannot pin that an agent then runs the tool, and it says nothing about *whether* a
vote should be cast — that is the abstain rule, issue #1408.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from emrg.protocol import InstanceIdentity
from emrg.server import scheduler as mod
from emrg.server.scheduler import TaskHandler
from tests.task_handler_factory import make_handler

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "emrg" / "server"
COUNTER = REPO_ROOT / "scripts" / "check-vote-count.py"

#: The instrument that reads the condition, and its two companions. Each is checked as
#: a verbatim substring of the shipped wording, so a rename of a script or a dropped
#: clause makes these tests say so instead of passing on a paraphrase.
COUNTER_SCRIPT = "scripts/check-vote-count.py"
FRESHNESS_SCRIPT = "scripts/check-merge-freshness.py"
PLAN_SUITE_SCRIPT = "scripts/check-merge-plan-suite.py"

#: The clause the counter implements and the template's one-line condition omitted.
CLAUSE = "before the head push is void"

#: The route a voter takes when a stale head has votes standing on it. Stated because
#: the tempting alternative — refresh the branch to make it fresh — is the one action
#: that destroys the votes it was meant to preserve.
STALE_ROUTE = "voids every vote standing on it"

#: The rule's home after the 2026-09-29 restructure (`cyc20260929-110933`): the condition
#: is stated once, in rulebook R3. Before that it was stated twice — §1.1's "Check merge
#: conditions" and §5's "Merge condition" — and this file pinned both, because a rule the
#: voter reads nowhere is a rule the voter does not apply. One home plus a citation from
#: each place that acts on it is the stronger shape: the count is now answerable.
REGION_START = "#### R3. Merging"
REGION_END = "#### R4. CI: three states, three actions"

#: The two sections a reader arrives from — deciding whether to vote (§2.1) and deciding
#: whether the merge may proceed (§3.1). Each must cite R3, or the rule is stated somewhere
#: the reader is never sent.
CITING_SECTIONS = {
    "#### 2.1 Every open PR": "#### 2.2 Every open issue and every rant",
    "#### 3.1 Vote, merge, close": "#### 3.2 Follow up on your own PRs",
}
CITATION = "R3"


def _load_counter():
    """`scripts/check-vote-count.py` (hyphenated, so not importable by name)."""
    spec = importlib.util.spec_from_file_location("check_vote_count_merge_condition", COUNTER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def counter():
    return _load_counter()


@pytest.fixture(autouse=True)
def _pinned_cycle_records(counter, tmp_path, monkeypatch):
    """Read the abstention window from an empty directory, never from this host.

    `check-vote-count.py` reads that window out of cycle records on disk, defaulting to
    the corpus of whichever machine runs the suite. The control below is about the
    *head push*, so its votes must not also be judged against the cycles this host
    happens to have recorded: an empty directory narrows the window to the voting
    cycle's own start, which the fixture above places an hour after the push.
    """
    monkeypatch.delenv("EMRG_CYCLES_LOG", raising=False)
    empty = tmp_path / "no-cycle-records"
    empty.mkdir()
    monkeypatch.setattr(counter.review_queue(), "DEFAULT_CYCLES_LOGS", (empty,))


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> str:
    """The real template, rendered through the real builder with the real context."""
    tmp_path = tmp_path_factory.mktemp("merge-condition")
    project_dir = tmp_path / "demoproj"
    project_dir.mkdir(exist_ok=True)
    (tmp_path / "projects.yml").write_text(
        yaml.safe_dump([{"name": "demoproj", "path": str(project_dir)}]), encoding="utf-8"
    )
    original = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        handler = make_handler(
            name="demo-task",
            config={"project": "demoproj"},
            interval=300,
            identity=InstanceIdentity(),
            template_path=PROMPTS_DIR / "evolution_prompt.md",
        )
        return handler._build_evolution_prompt()
    finally:
        mod.config_dir = original


def _region(text: str, start: str, end: str, what: str) -> str:
    """The shipped text between two anchors, or a failure to measure — never a pass."""
    i = text.find(start)
    j = text.find(end)
    assert i != -1, (
        f"the {what} anchor {start!r} is gone from the rendered prompt — this test "
        "cannot measure where the condition is stated, which is a failure to measure, "
        "not a pass"
    )
    assert j > i, (
        f"the {what} region's end anchor {end!r} no longer follows {start!r} — the "
        "prompt was restructured, so the region this test reads is no longer the one "
        "it describes"
    )
    return text[i:j]


def test_the_rule_names_the_instruments_that_answer_it(rendered: str) -> None:
    """R3 names the counter and the two freshness readings, not just the sentence.

    A condition stated without its instrument leaves the reader the comment history,
    which the counter was written because it misleads: on 2026-09-11 four PRs showed
    four to six "✅ LGTM" lines each and had zero valid votes between them.
    """
    region = _region(rendered, REGION_START, REGION_END, "rulebook R3")
    for term in (COUNTER_SCRIPT, FRESHNESS_SCRIPT, PLAN_SUITE_SCRIPT, CLAUSE, STALE_ROUTE):
        assert term in region, (
            f"R3 no longer states {term!r}. It is the merge condition's one home, and "
            "the clause it must keep is the one the counter enforces: a vote submitted "
            "before the head push is void — so a stale head is measured on the tree it "
            "would land rather than refreshed, because a push voids the votes it was "
            "meant to preserve"
        )


def test_the_rule_is_stated_once(rendered: str) -> None:
    """One statement — the count the duplicated shape made unanswerable."""
    found = rendered.count(CLAUSE)
    assert found == 1, (
        f"the merge condition's clause is stated {found} times in the rendered prompt; "
        "it lives in R3 alone, and a second copy is a copy free to drift from the one "
        "the counter enforces"
    )


def test_each_section_that_acts_on_the_rule_cites_it(rendered: str) -> None:
    """The voter's section and the merger's section both reach R3.

    The property a rulebook stands on: a rule stated once is only *read* if the places
    that act on it send the reader there. Both directions are checked, because each is a
    different reader with a different question — whether to vote, and whether to merge.
    """
    for start, end in CITING_SECTIONS.items():
        region = _region(rendered, start, end, start)
        assert CITATION in region, (
            f"{start} does not cite {CITATION}; the merge condition lives in the "
            "rulebook, and a section that acts on it without naming its home leaves the "
            "reader to reconstruct the rule from memory"
        )


HEAD = "b" * 40

# Every instant here is built from the host's own zone, because a cycle id is **local**
# time while a push arrives as UTC: two bare literals describe their order differently on
# every runner, and the order is the whole subject of the control below. (`_push` in
# `tests/test_cast_vote.py` exists for the same reason.)
LOCAL = datetime.now().astimezone().tzinfo


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_PUSH_LOCAL = datetime(2026, 9, 19, 12, 0, 0, tzinfo=LOCAL)
PUSH = _utc(_PUSH_LOCAL)
AFTER = _utc(_PUSH_LOCAL + timedelta(hours=2))
BEFORE = _utc(_PUSH_LOCAL - timedelta(hours=2))

#: The voting cycles: one hour after the head push, and one second apart. Both facts
#: matter — the second keeps them three *distinct* cycles, and the first keeps them
#: outside the window `check-vote-count.py` reads (a cycle does not count a vote on a
#: head pushed inside its own window, and its own window starts when it starts). A
#: vote cast here is an ordinary vote, which is what this control needs.
_CYCLE_LOCAL = _PUSH_LOCAL + timedelta(hours=1)


def _cycle_id(offset_seconds: int) -> str:
    return "cyc" + (_CYCLE_LOCAL + timedelta(seconds=offset_seconds)).strftime("%Y%m%d-%H%M%S")


class _FakeGh:
    """Canned answers for the three calls `check_pr` makes; nothing leaves the process."""

    def __init__(self, reviews: list[dict]) -> None:
        self.reviews = reviews

    def __call__(self, args: list[str]) -> object:
        if args[:2] == ["pr", "view"]:
            return {
                "number": 1,
                "title": "t",
                "headRefOid": HEAD,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
            }
        assert args and args[0] == "api", args
        if "actions/runs" in " ".join(args):
            # The projection the counter asks for now carries the run's actor beside its
            # time. This fixture's cycles all start an hour *after* the push, so the
            # abstention clause never reads it here - the vote is an ordinary one. The
            # actor is left unread, which is the shape that keeps the clause applied if a
            # future fixture moves the window; the ownership arms live with the
            # instrument, in `tests/test_check_vote_count.py`.
            return {"runs": [{"t": PUSH, "a": ""}]}
        return {"t": PUSH}

    def paginated(self, args: list[str]) -> list:
        assert "/reviews" in " ".join(args), args
        return self.reviews


def _counted(counter, monkeypatch, at: str) -> "object":
    reviews = [
        {"at": at, "body": f"\u2705 LGTM — cycle {_cycle_id(i)}"} for i in range(1, 4)
    ]
    fake = _FakeGh(reviews)
    monkeypatch.setattr(counter, "_gh_json", fake)
    monkeypatch.setattr(counter, "_gh_json_paginated", fake.paginated)
    return counter.check_pr(1, counter.DEFAULT_MIN_VOTES, mergeability_wait=0.0)


def test_the_counter_voids_the_vote_the_clause_names(counter, monkeypatch) -> None:
    """The control: the same three approvals count after the push, and not before.

    Without this, the phrases above could be satisfied by sentences describing a rule
    nothing enforces. Both directions are shown because only one of them is the
    failure: the votes are identical, the head push is the only difference, and the
    difference between 3/3 and 0/3 is what the template now tells a reader to ask
    about instead of counting.
    """
    after = _counted(counter, monkeypatch, AFTER)
    assert after.valid_count == 3, (
        f"three approvals submitted after the head push counted {after.valid_count} — "
        "the control needs this direction green before the other one means anything"
    )

    before = _counted(counter, monkeypatch, BEFORE)
    assert before.valid_count == 0, (
        "the same three approvals submitted *before* the head push counted "
        f"{before.valid_count}: the clause the template states is not the one the "
        "instrument applies, so the pin above would be pinning prose"
    )
    assert all("submitted before the head push" in v.why for v in before.votes), (
        [v.why for v in before.votes]
    )
