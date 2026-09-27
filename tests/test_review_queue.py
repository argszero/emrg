"""Tests for scripts/review-queue.py - what may a cycle do about each open PR?

Background (cycle cyc20260917-221117, issue #1340)
--------------------------------------------------
Issue #1340 is a question the host asked about a cycle: seven open PRs, no votes
cast. The cycle had not been lazy — it was following a rule it had derived by hand
and got wrong ("a stale PR cannot be voted on"), and re-deriving the rules showed
nothing had prevented the votes. The rules were already mechanical and already
split between two tools, so the fix is to assemble them once.

The reading is a *decision*, and the decision is only useful if it is right in both
directions. So what is pinned here is every branch's order, and each pair of states
that look alike in the count while calling for **opposite** actions:

* `0/3` from a ❌ at this head -> a fix push, **not** a vote. The counter resets the
  run on a veto, so this row and "never reviewed" read as the same number; treating
  them alike is how a cycle votes into an answered objection.
* a head that no longer contains master -> measure the landing tree and vote on
  *that*, **not** "unreviewable" and not a refresh. The refresh is the remedy that
  costs every vote the branch has, and the landing-tree reading is the one whose
  absence stalled cycle `cyc20260917-190356`.
* a red run, no run, and a run still going -> three actions, none of them a vote.
  Collapsing them into "not fresh" is the shortcut that produces one wrong remedy
  for three states.
* `--cycle` already voted here -> stop. One counted vote per cycle per PR, so the
  next vote at that head has to come from another cycle.

Nothing here touches the network or `gh`: both siblings' entry points are replaced,
and each replacement is asserted to receive the arguments the real one would, so a
test cannot pass by never asking. The three answers this tool must never invent are
pinned too — an unreadable count is `?` (not `0/3`), an unreadable queue is exit 2
(not "nothing to review"), and an unreadable row is exit 2 (not a green light).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review-queue.py"

HEAD = "a" * 40
MASTER = "c" * 40
PUSH_TIME = "2026-09-17T10:00:00Z"
CYCLE = "cyc20260917-221117"
#: The cycle immediately before `CYCLE`: it started at 21:59:29 in the host's own
#: zone, 11m48s before `CYCLE`. A cycle id *is* its local start time, so this pair is
#: what the abstention window's two ends are pinned against.
PREV_CYCLE = "cyc20260917-215929"

LOCAL = datetime.now().astimezone().tzinfo


def _push(year, month, day, hour, minute, second=0):
    """A push instant named in the host's own zone, as GitHub would report it.

    Push times arrive as UTC while a cycle id is local, so a test that wrote a bare
    `...Z` would be measuring the *runner's* timezone: `cyc20260917-221117` is
    22:11:17 local wherever the cycle ran, and a push at 22:05 local is inside the
    window on a +08 host, a +04 one, and a UTC one alike. The conversion happens here
    rather than in the assertion so both ends of the comparison are built the same
    way a caller would build them.
    """
    return (
        datetime(year, month, day, hour, minute, second, tzinfo=LOCAL)
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _load_module():
    spec = importlib.util.spec_from_file_location("review_queue", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the module declares a dataclass, and dataclasses
    # resolves annotations through sys.modules[cls.__module__] at class-creation
    # time. A module that is not registered there raises AttributeError from inside
    # dataclasses itself - an error that names neither this test nor the cause.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


# --- the two halves, as the siblings answer them ---------------------------

class FakeVotes:
    """`check-vote-count.py`, answering `check_pr` from a routing list.

    Built around the real `Verdict`/`Vote` dataclasses with the real field values,
    so a field this tool reads under the wrong name fails here rather than in the
    live queue. `DEFAULT_MIN_VOTES` is carried because the tool reads the gate's
    threshold from the counter rather than keeping a second copy of the number.
    """

    DEFAULT_MIN_VOTES = 3

    def __init__(self, reviews: list[dict] | None = None, mergeable: str = "MERGEABLE",
                 state: str = "CLEAN", head: str = HEAD, valid: int | None = None,
                 push: str | None = None, exact: bool = True):
        self.reviews = reviews if reviews is not None else []
        self.mergeable = mergeable
        self.state = state
        self.head = head
        self.forced_valid = valid
        #: When this head was pushed — the datum the abstention clause compares
        #: against. `None` keeps the historical default, far from every window.
        self.push = PUSH_TIME if push is None else push
        self.exact = exact
        self.calls: list[tuple[int, int, float]] = []
        #: The real sibling module, attached by `_install` so the fakes can build
        #: its dataclasses instead of a lookalike that would agree with a misreading.
        self.real = None

    def check_pr(self, number, needed, *, mergeability_wait=0.0, cycles_log=None):
        self.calls.append((number, needed, mergeability_wait))
        counter = self.real
        votes = [
            counter.Vote(
                at=review["at"],
                kind=review["kind"],
                cycle=review.get("cycle"),
                valid=review.get("valid", True),
                why=review.get("why", ""),
                ids=(review["cycle"],) if review.get("cycle") else (),
            )
            for review in self.reviews
        ]
        # The counter's own rule, replayed here: a veto resets the run, and a valid
        # approval counts once per cycle. Written out rather than imported because
        # the point of this fake is to be a second, independent answer this tool is
        # measured against - if it delegated to the counter, both could be wrong
        # together.
        run = 0
        seen: set[str] = set()
        for vote in votes:
            if vote.kind == "veto":
                run = 0
                seen.clear()
            elif vote.valid and vote.cycle and vote.cycle not in seen:
                seen.add(vote.cycle)
                run += 1
        return counter.Verdict(
            pr=number,
            title=f"pr {number}",
            head_sha=self.head,
            push_time=self.push,
            push_time_exact=self.exact,
            mergeable=self.mergeable,
            merge_state=self.state,
            votes=votes,
            counted=[],
            valid_count=run if self.forced_valid is None else self.forced_valid,
            needed=needed,
        )


class FakeFresh:
    """`check-merge-freshness.py`, for both the fresh case and the four stale kinds."""

    def __init__(self, stale: bool = False, kind: str = "", behind: int = 0,
                 reason: str = ""):
        self.stale = stale
        self.kind = kind
        self.behind = behind
        self.reason = reason or f"some reason for {kind or 'fresh'}"
        self.calls: list[int] = []
        self.real = None

    def check_pr(self, number):
        self.calls.append(number)
        verdict = self.real
        return verdict.Verdict(
            pr=number,
            title=f"pr {number}",
            head_sha=HEAD,
            merge_base=MASTER,
            ahead_by=1,
            behind_by=self.behind,
            run_created_at=None,
            run_conclusion=None,
            stale=self.stale,
            reason=self.reason,
            stale_kind=self.kind,
        )


def _install(mod, monkeypatch, votes, fresh):
    """Point the tool's two sibling seams at the fakes.

    `real` is kept on each fake so it can build the sibling's own dataclasses: the
    point is to exercise this tool against the other tools' *declared* shapes, and a
    self-authored lookalike would agree with a misreading.
    """
    votes.real = mod.vote_counter()
    fresh.real = mod.freshness()
    monkeypatch.setattr(mod, "vote_counter", lambda: votes)
    monkeypatch.setattr(mod, "freshness", lambda: fresh)


def _review(at="2026-09-17T10:05:00Z", kind="approve", cycle=CYCLE, valid=True, why=""):
    return {"at": at, "kind": kind, "cycle": cycle, "valid": valid, "why": why}


def _read(mod, monkeypatch, votes, fresh, *, number=1, cycle=None, prs=()):
    _install(mod, monkeypatch, votes, fresh)
    argv = [str(pr) for pr in prs] if prs else [str(number)]
    if cycle:
        argv += ["--cycle", cycle]
    return mod.main(argv)


# --- the vote branch: the ordinary case first ------------------------------


def test_three_votes_on_a_fresh_head_is_a_merge(mod, monkeypatch, capsys):
    """The positive control: every branch below is measured against this one."""
    votes = FakeVotes(reviews=[_review(cycle=f"cyc20260917-1{n}") for n in range(3)])
    fresh = FakeFresh()
    rc = _read(mod, monkeypatch, votes, fresh)
    out = capsys.readouterr().out
    assert rc == 0
    assert "3/3 votes" in out
    assert "merge" in out
    assert "gh pr merge 1 -R argszero/emrg --squash" in out
    # Both halves were asked, in the order the row needs them.
    assert votes.calls == [(1, 3, 30.0)]
    assert fresh.calls == [1]


def test_no_votes_on_a_fresh_head_is_a_vote(mod, monkeypatch, capsys):
    votes = FakeVotes(reviews=[])
    fresh = FakeFresh()
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "0/3 votes" in out
    assert "cast-vote.py 1 --cycle cyc20260917-221117" in out


def test_the_vote_command_carries_the_cycle_that_will_attribute_it(mod, monkeypatch, capsys):
    """A vote body with no cycle id is uncountable, so the printed command must ask
    for one rather than let the reader post a review the counter drops."""
    votes = FakeVotes(reviews=[])
    fresh = FakeFresh()
    _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert "--cycle cyc20260917-221117" in out


# --- the states that look like 0/3 and are not ----------------------------


def test_a_veto_at_this_head_is_a_fix_push_not_a_vote(mod, monkeypatch, capsys):
    """The counter resets the run on a veto, so a vetoed PR reads `0/3` - exactly
    like a never-reviewed one. Voting on it would answer an objection."""
    votes = FakeVotes(
        reviews=[
            _review(cycle="cyc1", kind="approve"),
            _review(at="2026-09-17T10:06:00Z", cycle="cyc2", kind="veto"),
        ],
    )
    fresh = FakeFresh()
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "fix-push" in out
    assert "veto" in out
    assert "cast-vote.py" not in out, "a vote must not be suggested while a veto stands"


def test_a_veto_from_before_the_head_push_does_not_block_a_vote(mod, monkeypatch, capsys):
    """It is already excluded from the count, so treating it as standing would stall
    a PR that has no live objection."""
    votes = FakeVotes(
        reviews=[
            _review(at="2026-09-17T09:00:00Z", cycle="cyc1", kind="veto", valid=False),
        ],
    )
    fresh = FakeFresh()
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "vote" in out
    assert "fix-push" not in out


def test_a_head_behind_master_is_voted_on_its_landing_tree(mod, monkeypatch, capsys):
    """The measured failure this tool exists for: the rule "a stale PR cannot be
    voted on" is false, and the landing-tree measurement preserves the votes."""
    votes = FakeVotes(reviews=[_review(cycle="cyc1")])
    fresh = FakeFresh(stale=True, kind="ancestry", behind=3,
                      reason="head does not contain master")
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "measure-then-vote" in out
    assert "check-merge-plan-suite.py 1" in out
    assert "cast-vote.py 1 --cycle cyc20260917-221117" in out
    # The refresh is the expensive remedy and must not be the advice here.
    assert "git merge FETCH_HEAD" not in out


def test_a_stale_head_is_sent_to_the_landing_diff_before_the_vote(mod, monkeypatch, capsys):
    """A stale head's `diff(master, head)` is not the change that merges: it shows
    the base's own later commits as reversals the PR does not make (measured on
    #1423, cycle cyc20260919-165319 - three of five paths were the base's own work,
    including another PR's test file printed as deleted). Voting is a judgement about
    the *landing* change, so the tool that hands out the vote command hands out the
    instrument that shows that change first."""
    votes = FakeVotes(reviews=[_review(cycle="cyc1")])
    fresh = FakeFresh(stale=True, kind="ancestry", behind=3,
                      reason="head does not contain master")
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "measure-then-vote" in out
    assert "check-merge-landing-diff.py 1" in out
    commands = [line for line in out.splitlines() if line.strip().startswith("$ ")]
    landing_diff = next(i for i, c in enumerate(commands)
                        if "check-merge-landing-diff.py 1" in c)
    vote = next(i for i, c in enumerate(commands) if "cast-vote.py 1" in c)
    assert landing_diff < vote, commands
    # The hazard is named, not just the command: a reader who does not know why
    # must not conclude the two diffs are interchangeable.
    assert "reversals" in out


def test_a_fresh_head_is_not_sent_to_the_landing_diff(mod, monkeypatch, capsys):
    """The negative control, so the line above is a reading of staleness and not a
    constant: on a head that contains master the two diffs coincide, and naming a
    third command for a question that cannot arise is how a warning turns into noise
    every cycle skips."""
    votes = FakeVotes(reviews=[_review(cycle="cyc1")])
    fresh = FakeFresh()
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "cast-vote.py 1" in out
    assert "check-merge-landing-diff" not in out


def test_enough_votes_on_a_stale_head_measures_before_merging(mod, monkeypatch, capsys):
    """Merging a stale head merges a tree no CI judged, so the vote count alone is
    not the green light."""
    votes = FakeVotes(reviews=[_review(cycle=f"cyc20260917-1{n}") for n in range(3)])
    fresh = FakeFresh(stale=True, kind="ancestry", behind=2)
    rc = _read(mod, monkeypatch, votes, fresh)
    out = capsys.readouterr().out
    assert rc == 0
    assert "measure-then-merge" in out
    assert "gh pr merge" not in out


# --- the three CI states, which are not one state -------------------------


def test_a_red_run_is_not_votable_and_names_the_run(mod, monkeypatch, capsys):
    votes = FakeVotes(reviews=[])
    fresh = FakeFresh(stale=True, kind="failing",
                      reason="CI concluded 'failure' on head aaaa")
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "ci-red" in out
    assert "gh pr checks 1" in out
    assert "cast-vote.py" not in out


def test_a_head_with_no_run_is_retriggered_not_refreshed(mod, monkeypatch, capsys):
    """A re-trigger fires a run on the same head and keeps the votes; a refresh
    would spend them for a question the re-trigger answers."""
    votes = FakeVotes(reviews=[_review(cycle="cyc1")])
    fresh = FakeFresh(stale=True, kind="no_run",
                      reason="there is NO Test run for head aaaa")
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "retrigger-ci" in out
    assert "cast-vote.py" not in out
    assert "git merge FETCH_HEAD" not in out


def test_a_run_still_going_is_parked_not_waited_on(mod, monkeypatch, capsys):
    """The verb is the instruction: `wait` told the reader to block on the run.

    A run that has not concluded is not votable, so blocking on it spends the window
    on a PR this cycle cannot move; the row is deferred to a later cycle instead
    (host rant 2026-09-24T14:46:10). The name is asserted, not just the reason —
    renaming the kind back to `wait` must fail this test.
    """
    votes = FakeVotes(reviews=[])
    fresh = FakeFresh(stale=True, kind="running",
                      reason="CI is still in_progress on head aaaa")
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "park" in out
    assert "wait" not in out, "a row that is parked must not be told to wait"
    assert "next cycle" in out, "the deferral has to name when the row comes back"
    assert "cast-vote.py" not in out


# --- branch states, and the one a committer resolves directly --------------


def test_a_run_in_flight_on_a_behind_master_head_is_parked_not_sent_to_the_branch(
    mod, monkeypatch, capsys
):
    """#1573's measured shape: `behind_by=4`, `MERGEABLE/UNSTABLE`, both legs in flight.

    The row is built from the kind the freshness tool now reports for that state
    (`running`) — before that fix it reported `ancestry`, which matched no CI branch
    here and fell through to the merge-state branch, answering `unblock`: "the branch
    has to remove it". That is the one instruction that voids the votes such a head
    may be carrying, and it is the opposite of what the freshness tool says about the
    same head, which is why the state is pinned here as well as at its source.
    """
    votes = FakeVotes(
        reviews=[_review(cycle="cyc1"), _review(at="2026-09-17T10:15:00Z", cycle="cyc2")],
        state="UNSTABLE",
    )
    fresh = FakeFresh(
        stale=True, kind="running", behind=4,
        reason="CI is still pending on head aaaa (and the head does not contain master)",
    )
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "2/3 votes" in out, "the votes this row is protecting are on it"
    assert "park" in out
    assert "unblock" not in out, "an unfinished run is not a branch defect"
    assert "stale:running" in out
    assert "cast-vote.py" not in out


def test_a_conflict_names_the_merge_and_the_classifier(mod, monkeypatch, capsys):
    votes = FakeVotes(reviews=[], mergeable="CONFLICTING", state="DIRTY")
    fresh = FakeFresh()
    rc = _read(mod, monkeypatch, votes, fresh)
    out = capsys.readouterr().out
    assert rc == 0
    assert "resolve-conflict" in out
    assert "git merge FETCH_HEAD" in out
    assert "classify-conflict.py --all" in out


def test_a_conflict_is_not_reported_as_an_ordinary_blocker(mod, monkeypatch, capsys):
    """A non-conflicting state is the branch's to remove, so the row must not hand
    the reader a merge cascade for a draft."""
    votes = FakeVotes(reviews=[], mergeable="MERGEABLE", state="DRAFT")
    fresh = FakeFresh()
    rc = _read(mod, monkeypatch, votes, fresh)
    out = capsys.readouterr().out
    assert rc == 0
    assert "unblock" in out
    assert "resolve-conflict" not in out


# --- the per-cycle rule ----------------------------------------------------


def test_a_vote_this_cycle_already_cast_stops_the_next_one(mod, monkeypatch, capsys):
    votes = FakeVotes(reviews=[_review(cycle=CYCLE)])
    fresh = FakeFresh()
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "already-voted" in out
    assert "cast-vote.py" not in out


def test_the_same_vote_without_this_cycle_id_is_just_a_count(mod, monkeypatch, capsys):
    """Without `--cycle` the tool answers the first question only - and says so by
    not claiming the vote budget is spent."""
    votes = FakeVotes(reviews=[_review(cycle=CYCLE)])
    fresh = FakeFresh()
    rc = _read(mod, monkeypatch, votes, fresh)
    out = capsys.readouterr().out
    assert rc == 0
    assert "already-voted" not in out
    assert "cast-vote.py 1" in out


# --- what it must never invent ---------------------------------------------


def test_an_unreadable_count_is_a_question_mark_not_zero(mod, monkeypatch, capsys):
    class Boom:
        # A faithful stand-in for the counter: the tool reads the gate's threshold
        # from it, so a stand-in without it would fail for the wrong reason.
        DEFAULT_MIN_VOTES = 3
        calls: list = []

        def check_pr(self, number, needed, *, mergeability_wait=0.0, cycles_log=None):
            raise RuntimeError("gh failed: mergeable UNKNOWN")

    fresh = FakeFresh()
    votes = Boom()
    monkeypatch.setattr(mod, "vote_counter", lambda: votes)
    monkeypatch.setattr(mod, "freshness", lambda: fresh)
    rc = mod.main(["1"])
    out = capsys.readouterr().out
    assert rc == 2, "an unread count is a failure to measure, not a clean run"
    assert "? votes" in out
    assert "0/3" not in out, "0/3 is the line that says 'vote freely'"
    assert "read-first" in out
    assert "UNKNOWN" in out
    assert fresh.calls == [], "an unread count means no later decision is worth making"


def test_an_unreadable_queue_is_not_an_empty_one(mod, monkeypatch, capsys):
    def boom(repo=mod.REPO):
        raise RuntimeError("gh failed (rc=1): gh pr list")

    monkeypatch.setattr(mod, "open_prs", boom)
    rc = mod.main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "could not list open PRs" in err
    assert "nothing to review" not in err


def test_an_empty_queue_is_an_empty_one(mod, monkeypatch, capsys):
    """The other side of the same distinction: `[]` read successfully is a state."""
    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to review" in out


# --- the queue the tool is asked about -------------------------------------


def test_named_prs_are_asked_about_instead_of_the_whole_queue(mod, monkeypatch, capsys):
    votes = FakeVotes(reviews=[])
    fresh = FakeFresh()
    _install(mod, monkeypatch, votes, fresh)
    monkeypatch.setattr(
        mod, "open_prs", lambda repo=mod.REPO: pytest.fail("the queue must not be listed")
    )
    rc = mod.main(["7", "9"])
    out = capsys.readouterr().out
    assert rc == 0
    assert [call[0] for call in votes.calls] == [7, 9]
    assert fresh.calls == [7, 9]
    assert "#7" in out and "#9" in out


def test_the_default_threshold_comes_from_the_counter(mod, monkeypatch, capsys):
    """Never a second copy of the number: moving the gate must move this."""
    votes = FakeVotes(reviews=[_review(cycle=f"cyc20260917-1{n}") for n in range(2)])
    fresh = FakeFresh()
    _install(mod, monkeypatch, votes, fresh)
    votes.DEFAULT_MIN_VOTES = 2
    rc = mod.main(["1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert votes.calls == [(1, 2, 30.0)]
    assert "2/2 votes" in out
    assert "merge" in out


def test_json_carries_the_reading_and_the_action(mod, monkeypatch, capsys):
    votes = FakeVotes(reviews=[_review(cycle="cyc1")])
    fresh = FakeFresh(stale=True, kind="ancestry", behind=1)
    _install(mod, monkeypatch, votes, fresh)
    # The tree fields are asserted here as literal values, so the reading is substituted:
    # the branch of the checkout these tests run in is a fact about the runner (CI
    # checks out a detached HEAD), and an expected dict that varies by runner would be a
    # test of the environment. `test_the_json_document_keeps_its_shape_and_carries_the_
    # same_fact` pins the live reading.
    monkeypatch.setattr(mod, "local_tree", lambda: ("/checkout", "some-branch", "b" * 40))
    # `--prev-cycle` rather than the default cycle-record directory: the shape has to
    # be the same everywhere, and the inferred window is a fact about the host.
    rc = mod.main(
        ["1", "--cycle", CYCLE, "--prev-cycle", PREV_CYCLE, "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(payload) == 1
    row = dict(payload[0])
    why = row.pop("why")
    assert row == {
        "tree": "/checkout",
        "branch": "some-branch",
        "pr": 1,
        "head": HEAD,
        "title": "pr 1",
        "votes": 1,
        "needed": 3,
        "mergeable": "MERGEABLE",
        "merge_state": "CLEAN",
        "block_reason": "",
        "veto_at_head": False,
        "voted_by_this_cycle": False,
        "head_pushed_at": PUSH_TIME,
        "head_pushed_exact": True,
        "vote_window_start": (
            datetime(2026, 9, 17, 21, 59, 29, tzinfo=LOCAL).isoformat(timespec="seconds")
        ),
        "vote_window_source": f"previous cycle {PREV_CYCLE} (named by --prev-cycle)",
        "stale": True,
        "stale_kind": "ancestry",
        "behind_by": 1,
        "unread": "",
        "action": "measure-then-vote",
        "command": "uv run --no-sync python3 scripts/check-merge-plan-suite.py 1",
    }
    # The reason is the sibling's sentence, carried through rather than rephrased.
    assert "some reason for ancestry" in why


def test_the_summary_counts_every_row_once(mod, monkeypatch, capsys):
    votes = FakeVotes(reviews=[])
    fresh = FakeFresh()
    _install(mod, monkeypatch, votes, fresh)
    rc = mod.main(["1", "2", "3"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "3 PR(s): vote 3" in out


# --- the abstention clause: whose head is it? (issue #1408) ------------------
#
# The counter's half of "may this cycle vote here" is the count. The other half is
# *whose head is it*, and that is the half that has cost votes: a cycle neither votes
# on nor merges a head it pushed, and because every cycle on a host is the same
# instance running again, the window immediately before this one counts as its own
# (the applied precedent is `cyc20260917-125823`). `CYCLE` began at 22:11:17 in the
# host's zone and `PREV_CYCLE` at 21:59:29, so the two ends of the window are 11m48s
# apart and every push below is named in that zone rather than in UTC.


def _run(mod, monkeypatch, votes, fresh, argv):
    """`main` with both sibling seams replaced — the shape `_read` uses, for argvs
    `_read` does not build."""
    _install(mod, monkeypatch, votes, fresh)
    return mod.main(argv)


def test_a_cycle_id_is_its_start_time_in_the_host_zone(mod):
    """The one conversion the clause rests on: ids are local, push times are UTC, and
    comparing the two without it is an error of whole hours that reads as an answer."""
    start = mod.cycle_start(CYCLE)
    assert start is not None and start.tzinfo is not None
    assert start.isoformat(timespec="seconds") == datetime(
        2026, 9, 17, 22, 11, 17, tzinfo=LOCAL
    ).isoformat(timespec="seconds")
    assert mod.cycle_start("cyc-2026-09-17") is None
    assert mod.cycle_start("") is None


def test_a_head_pushed_inside_this_cycle_is_an_abstain(mod, monkeypatch, capsys):
    votes = FakeVotes(reviews=[], push=_push(2026, 9, 17, 22, 30))
    fresh = FakeFresh()
    rc = _run(mod, monkeypatch, votes, fresh, ["1", "--cycle", CYCLE])
    out = capsys.readouterr().out
    assert rc == 0
    assert "abstain" in out
    assert "cast-vote.py 1" not in out  # never the command that spends the vote
    assert "check-vote-count.py 1" in out


def test_a_head_pushed_by_the_previous_cycle_is_an_abstain(mod, monkeypatch, capsys):
    """The half the precedent widened: 22:05 is after the previous cycle began
    (21:59:29) and before this one did, so the push belongs to the cycle immediately
    before — the same instance, one window back."""
    votes = FakeVotes(reviews=[], push=_push(2026, 9, 17, 22, 5))
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh,
         ["1", "--cycle", CYCLE, "--prev-cycle", PREV_CYCLE])
    out = capsys.readouterr().out
    assert "abstain" in out
    assert PREV_CYCLE in out


def test_a_push_at_the_previous_cycles_start_is_inside_the_window(mod, monkeypatch, capsys):
    """The boundary is closed at the start: a window runs from its own id to the next
    one, so the previous cycle's first instant belongs to it and not to the cycle
    before it."""
    votes = FakeVotes(reviews=[], push=_push(2026, 9, 17, 21, 59, 29))
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh,
         ["1", "--cycle", CYCLE, "--prev-cycle", PREV_CYCLE])
    out = capsys.readouterr().out
    assert "abstain" in out


def test_a_head_pushed_two_cycles_back_is_still_votable(mod, monkeypatch, capsys):
    """The negative control for the clause: the window is one cycle wide and no
    wider. 20:00 is before the previous cycle began, so nothing about it is one's own
    and the row is the ordinary vote it would always have been."""
    votes = FakeVotes(reviews=[], push=_push(2026, 9, 17, 20, 0))
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh,
         ["1", "--cycle", CYCLE, "--prev-cycle", PREV_CYCLE])
    out = capsys.readouterr().out
    assert "abstain" not in out
    assert "cast-vote.py 1 --cycle cyc20260917-221117" in out


def test_a_head_this_cycle_pushed_is_not_merged_either(mod, monkeypatch, capsys):
    """The other thing the clause withholds: `3/3` at a head one pushed is not this
    cycle's merge, so the merge command must not be printed either."""
    votes = FakeVotes(
        reviews=[_review(cycle=f"cyc20260917-1{n}") for n in range(3)],
        push=_push(2026, 9, 17, 22, 30),
    )
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh, ["1", "--cycle", CYCLE])
    out = capsys.readouterr().out
    assert "3/3 votes" in out
    assert "abstain" in out
    assert "gh pr merge 1" not in out


def test_an_inexact_push_time_never_decides_the_window(mod, monkeypatch, capsys):
    """A push time that fell back to the commit date is a lower bound. The counter
    already calls such a head blocking, so the row asks for the run — the clause is
    never applied to a datum that cannot support it."""
    votes = FakeVotes(reviews=[], push=_push(2026, 9, 17, 22, 30), exact=False)
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh, ["1", "--cycle", CYCLE])
    out = capsys.readouterr().out
    assert "unblock" in out
    assert "abstain" not in out


def test_the_previous_cycle_is_read_from_the_cycle_records(mod, monkeypatch, capsys,
                                                           tmp_path):
    """`--cycles-log`: the newest cycle record that sorts before this cycle's id — not
    the newest record, and not an archive's filename.

    The records are named `cycle-<date>-<time>.md`, without the id's `cyc` prefix, so
    this also pins the filename-to-id mapping: the row names `PREV_CYCLE` with its
    `cyc`, which is not what the filename says.
    """
    (tmp_path / f"cycle-{PREV_CYCLE[3:]}.md").write_text("x", encoding="utf-8")
    (tmp_path / "cycle-20260917-100000.md").write_text("x", encoding="utf-8")
    (tmp_path / f"cycle-{CYCLE[3:]}.md").write_text("x", encoding="utf-8")
    (tmp_path / "cycle-archive-20260917.md").write_text("x", encoding="utf-8")
    votes = FakeVotes(reviews=[], push=_push(2026, 9, 17, 22, 5))
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh,
         ["1", "--cycle", CYCLE, "--cycles-log", str(tmp_path)])
    out = capsys.readouterr().out
    assert "abstain" in out
    assert PREV_CYCLE in out


def test_a_corpus_split_across_two_roots_is_read_whole(mod, tmp_path):
    """The newest record wins *across* the roots, not within one of them.

    The state this search exists for (measured 2026-09-25): 1,354 records sit beside
    the checkout, while the root this tree's template names — the checkout one D9
    (PR #1555) re-based onto — is empty, so a reinstall moves the next records onto
    the other side. Either root alone then answers a different question: the older one
    freezes the window at the last cycle before the move, the newer one cannot resolve
    it at all until records accumulate there. The control is the first assertion — the
    older root read alone really does answer the older cycle. Order is not the claim:
    the same answer has to come out either way round.
    """
    legacy, project = tmp_path / "legacy", tmp_path / "project"
    legacy.mkdir()
    project.mkdir()
    (legacy / "cycle-20260917-100000.md").write_text("x", encoding="utf-8")
    (project / f"cycle-{PREV_CYCLE[3:]}.md").write_text("x", encoding="utf-8")

    frozen, _ = mod.previous_cycle(CYCLE, (legacy,))
    assert frozen == "cyc20260917-100000", "the older root alone is the frozen reading"

    for order in ((project, legacy), (legacy, project)):
        previous, where = mod.previous_cycle(CYCLE, order)
        assert previous == PREV_CYCLE, (
            f"the newest record wins across the roots, in either order ({order})"
        )
        assert where == str(project), "the answer names the root it came from"


def test_a_root_that_cannot_be_read_is_named_rather_than_passed_over(mod, tmp_path):
    """An answer drawn from the readable half is still an answer — and the reader is
    told which half it came from, because a record in the unreadable one could be
    newer."""
    project = tmp_path / "project"
    project.mkdir()
    (project / f"cycle-{PREV_CYCLE[3:]}.md").write_text("x", encoding="utf-8")
    missing = tmp_path / "gone"

    previous, where = mod.previous_cycle(CYCLE, (project, missing))
    assert previous == PREV_CYCLE
    assert str(missing) in where and "could not be read" in where

    # Nothing readable at all: the reason is the read failure, not "no record" —
    # the two are different facts and only one of them is a gap in the corpus.
    previous, where = mod.previous_cycle(CYCLE, (missing,))
    assert previous == ""
    assert "could not be read" in where and "no cycle record" not in where


def test_the_cycle_log_override_names_one_root_or_several(mod, monkeypatch, tmp_path):
    """`--cycles-log` / `EMRG_CYCLES_LOG` keep the single-directory meaning and accept
    the pair, so a caller can state the roots instead of relying on the defaults."""
    first, second = tmp_path / "first", tmp_path / "second"
    assert mod.resolve_cycle_logs(str(first)) == (first,)
    assert mod.resolve_cycle_logs(os.pathsep.join([str(first), str(second)])) == (
        first,
        second,
    )
    monkeypatch.setenv("EMRG_CYCLES_LOG", str(second))
    assert mod.resolve_cycle_logs(None) == (second,)
    monkeypatch.delenv("EMRG_CYCLES_LOG")
    assert mod.resolve_cycle_logs(None) == mod.DEFAULT_CYCLES_LOGS


def test_the_cli_reads_both_roots_named_by_the_override(mod, monkeypatch, capsys, tmp_path):
    """The wiring, end to end: `--cycles-log` carrying two roots resolves the window
    from the newer record, so the row is an abstain rather than a vote."""
    legacy, project = tmp_path / "legacy", tmp_path / "project"
    legacy.mkdir()
    project.mkdir()
    (legacy / "cycle-20260917-100000.md").write_text("x", encoding="utf-8")
    (project / f"cycle-{PREV_CYCLE[3:]}.md").write_text("x", encoding="utf-8")
    votes = FakeVotes(reviews=[], push=_push(2026, 9, 17, 22, 5))
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh,
         ["1", "--cycle", CYCLE, "--cycles-log",
          os.pathsep.join([str(legacy), str(project)])])
    out = capsys.readouterr().out
    assert "abstain" in out and PREV_CYCLE in out
    assert str(project) in out


def test_the_prompt_writes_its_cycle_records_where_the_queue_reads_them(mod):
    """The template's record path lands in a directory this tool searches.

    *Which directory holds the cycle records* is a fact about the template, not about
    this tool — and it moved once already: D9 (PR #1555, in every build from v0.3.2)
    re-based the prompt's memory roots from `{{ evolution_cwd }}` onto
    `{{ source_dir }}`, i.e. from the root beside the checkout to the project memory
    root inside it, while the corpus stayed where it was. Repairing the reader once
    would not have caught that, and would not catch the next move; so the template is
    rendered here, with the two variables the daemon supplies for the evolution task
    (`scheduler._build_evolution_prompt`), and every directory its `cycle-<ts>.md`
    path names is required to be one of the roots this tool reads. A record written
    anywhere else is a record no window reads.
    """
    import re

    import jinja2

    template = (
        REPO_ROOT / "emrg" / "server" / "evolution_prompt.md"
    ).read_text(encoding="utf-8")
    rendered = (
        jinja2.Environment(undefined=jinja2.Undefined)
        .from_string(template)
        .render(
            source_dir=str(REPO_ROOT),
            evolution_cwd=str(REPO_ROOT.parent),
            timestamp="20260925-101010",
            # The two mappings the template iterates over; every other name it uses is
            # a display field, and the daemon's `Undefined` renders those empty here as
            # it would there. The claim below is about a *path*, built from the three
            # names set above, which the daemon supplies from
            # `_source_dir`, `EVOLUTION_CWD` and the render clock.
            task={},
            project={},
        )
    )
    written = [
        Path(match) for match in re.findall(r"`([^`]*/cycle-[^`]*\.md)`", rendered)
    ]
    assert written, (
        "the template no longer names a cycle-record path this guard can read - "
        "update the guard with it rather than deleting the claim"
    )
    roots = {root.resolve() for root in mod.DEFAULT_CYCLES_LOGS}
    elsewhere = [path for path in written if path.parent.resolve() not in roots]
    assert not elsewhere, (
        "these cycle-record paths land outside the roots review-queue searches "
        f"({', '.join(str(root) for root in mod.DEFAULT_CYCLES_LOGS)}): "
        + ", ".join(str(path) for path in elsewhere)
    )


def test_an_unresolvable_previous_cycle_narrows_the_window_and_says_so(
        mod, monkeypatch, capsys, tmp_path):
    """No record to read: the scan shrinks to this cycle's own start. That is a
    strictly weaker reading, so it is reported as one — a narrowed window must never
    be printed as if the clause had been applied in full."""
    votes = FakeVotes(reviews=[], push=_push(2026, 9, 17, 22, 5))
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh,
         ["1", "--cycle", CYCLE, "--cycles-log", str(tmp_path)])
    out = capsys.readouterr().out
    assert "abstain" not in out  # not a claim the tool cannot support
    assert "note: the abstention window could not be widened" in out
    assert "no cycle record before" in out
    # The other half of "narrowed, and said so": the note names the boundary that
    # *was* checked, so a reader can still apply the clause by hand to an earlier push
    # (the push time is on the row) instead of taking the silence for a pass.
    assert datetime(2026, 9, 17, 22, 11, 17, tzinfo=LOCAL).isoformat(timespec="seconds") \
        in out
    # And it is printed *ahead* of the rows it weakens (`cyc20260926-120320` moved it
    # there): the tail placement left it at line 39 of a queue a reader pipes through
    # `head`, or reads only as far as the first row whose command they copy.
    #
    # The tree line moved in front of it on 2026-09-26 (#1635), so "ahead of the rows"
    # is now "the line after the tree line" rather than "line 0" - the reading that
    # matters is the order, and it is asserted as an order.
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0].startswith("tree: "), lines[0]
    assert lines[1].startswith("note: the abstention window could not be widened"), lines[:2]
    assert out.index("could not be widened") < out.index("#1 "), out[:300]


def test_a_narrowed_window_still_catches_this_cycles_own_push(mod, monkeypatch, capsys,
                                                              tmp_path):
    """The half that survives without a previous cycle, because it needs only this
    cycle's own id — so narrowing must not turn into not checking at all."""
    votes = FakeVotes(reviews=[], push=_push(2026, 9, 17, 22, 30))
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh,
         ["1", "--cycle", CYCLE, "--cycles-log", str(tmp_path)])
    out = capsys.readouterr().out
    assert "abstain" in out


def test_without_a_cycle_the_clause_is_not_applied(mod, monkeypatch, capsys):
    """`--cycle` is what makes the question a cycle's own. Without it the tool answers
    the count and nothing else, and the push time stays on the row for a reader who
    knows which window it falls in."""
    pushed = _push(2026, 9, 17, 22, 30)
    votes = FakeVotes(reviews=[], push=pushed)
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh, ["1"])
    out = capsys.readouterr().out
    # About the *rows*, not the whole output: the note added on 2026-09-26 names the
    # verdict the clause would withhold (`abstain`), so the old whole-output proxy would
    # now fail on the note that exists to make the reading honest. What must not happen
    # is a row reading `abstain`, and that is what is asserted.
    rowlines = [line for line in out.splitlines() if line.startswith("#")]
    assert rowlines, out
    assert all("abstain" not in line for line in rowlines), rowlines
    assert "vote" in out
    assert f"pushed {pushed}" in out


def test_without_a_cycle_the_report_says_the_clause_was_not_applied(mod, monkeypatch, capsys):
    """The `says so` the docstring has promised since the clause was written, and the
    line that had no carrier until it was measured missing.

    Measured 2026-09-26 (`cyc20260926-120320`): the first run of a cycle, without the
    flags, answered `vote` for the head the cycle immediately before it had pushed —
    the row the clause exists to turn into `abstain` — and nothing in the prose
    distinguished that report from a windowed one. The row's own printed remedy omits
    `--cycle` too (the tool was never given one), so the reader who copies the command
    spends exactly the vote the clause withholds.
    """
    votes = FakeVotes(reviews=[], push=_push(2026, 9, 17, 22, 30))
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh, ["1"])
    out = capsys.readouterr().out
    assert "no --cycle was given" in out, (
        "an unflagged run reports `vote` rows and says nothing about the own-window "
        f"clause having been skipped: {out!r}"
    )
    assert "--cycle <this cycle's id>" in out, (
        "the note has to name the remedy, or the reader is left to discover the flag: "
        f"{out!r}"
    )
    # The placement is the assertion, not a detail of it: a caveat read *after* the row
    # it weakens arrives after the reader has copied the command.
    # The placement is the assertion, and the tree line moved in front of it on
    # 2026-09-26 (#1635): the note must still precede the rows, now as the line after
    # the tree line rather than as line 0.
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0].startswith("tree: "), lines[0]
    assert lines[1].startswith("note: no --cycle was given"), lines[:2]
    assert out.index("no --cycle was given") < out.index("#1 "), (
        "the note is not ahead of the row it qualifies"
    )


# --- the tree the readings came from --------------------------------------


def test_the_report_names_the_checkout_and_the_branch_it_read(mod, monkeypatch, capsys):
    """The first line is the clone, the way every `check-merge-*.py` names its base.

    Without it the report is a set of readings with no statement of which checkout
    produced them — and the reader's next act is to open a file with the `read` tool,
    which reads *this branch's* content. Asserted against the live checkout rather than
    a substituted one: the checkout path is what a reader has to recognise, and a
    hardcoded string would pass while pointing somewhere else. The branch is not
    asserted (a CI checkout is detached; that state has its own test below).
    """
    votes = FakeVotes(reviews=[_review(cycle=f"cyc20260917-1{n}") for n in range(3)])
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh, ["1"])
    lines = capsys.readouterr().out.splitlines()

    assert lines[0].startswith("tree: "), (
        f"the report's first line is {lines[0]!r}; a reader has to be told which checkout "
        "and branch the readings below came from before any verdict"
    )
    assert str(REPO_ROOT) in lines[0], (
        f"the tree line names {lines[0]!r}, not the checkout the readings came from "
        f"({REPO_ROOT})"
    )
    assert " on " in lines[0], lines[0]


def test_a_detached_head_is_a_state_not_a_failure(mod):
    """`symbolic-ref` exits non-zero when HEAD is detached, which is how CI checks out.

    A branch reading that treated that as unreadable would print a failure on every CI
    run; a branch reading that did not read at all would print `master` for a checkout
    that is not on master, which is the one thing this line exists to prevent.
    """
    root, branch, head = mod.local_tree()

    assert root == str(REPO_ROOT)
    assert branch, "a checkout always has a state to name, detached included"
    assert len(head) in (8, 40) or head == "????????", head


def test_the_tree_line_says_so_when_the_working_tree_is_at_an_open_prs_head(
    mod, monkeypatch, capsys
):
    """The measured incident: a cycle began on the previous cycle's PR branch.

    Stated in `local_tree`'s docstring; pinned here because the signal is only worth
    having if it is emitted. `local_tree` is replaced rather than the repository moved —
    the fact under test is that the report compares its own HEAD against the open heads,
    which is a computation, not a check of this checkout.
    """
    monkeypatch.setattr(mod, "local_tree", lambda: (str(REPO_ROOT), "feature/x", HEAD))
    votes = FakeVotes(reviews=[])
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh, ["1"])
    out = capsys.readouterr().out
    assert "on feature/x" in out, out[:200]
    assert "at the head of #1" in out, (
        "the working tree matches an open PR's head and the report did not say so, so a "
        f"file read from here would answer about an unmerged tree: {out[:400]!r}"
    )
    assert "not master's" in out, out[:400]
def test_the_note_is_absent_when_the_tree_is_not_at_an_open_prs_head(
    mod, monkeypatch, capsys
):
    """The control: the note is about a comparison, not about running at all.

    With a HEAD that matches no open head — the ordinary case, and the one every cycle
    that has just returned to master is in — the report names the tree and says nothing
    else. Without this half the test above would pass for a note printed unconditionally.
    """
    monkeypatch.setattr(mod, "local_tree", lambda: (str(REPO_ROOT), "master", "b" * 40))
    votes = FakeVotes(reviews=[])
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh, ["1"])
    out = capsys.readouterr().out

    assert "on master" in out
    assert "at the head of" not in out, (
        f"this HEAD matches no open head, so the note must not be printed: {out[:400]!r}"
    )
def test_the_json_document_keeps_its_shape_and_carries_the_same_fact(
    mod, monkeypatch, capsys
):
    """`--json` is a list, and a prose line ahead of it would break the parse.

    The fact rides as fields on each reading instead — the shape `check-merge-landed.py`
    uses for its tree in `--json`, and the reason the prose line is suppressed there.
    """
    monkeypatch.setattr(mod, "local_tree", lambda: (str(REPO_ROOT), "feature/x", "b" * 40))
    votes = FakeVotes(reviews=[])
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh, ["1", "--json"])
    out = capsys.readouterr().out

    payload = json.loads(out)  # a prose line here raises, which is the assertion
    assert isinstance(payload, list), type(payload)
    assert payload[0]["tree"] == str(REPO_ROOT)
    assert payload[0]["branch"] == "feature/x"
    assert "tree: " not in out, "the prose line must not be emitted into the JSON document"
def test_a_windowed_run_prints_no_such_note(mod, monkeypatch, capsys):
    """The control: with `--cycle` and a previous cycle in hand the clause *is*
    applied in full, and a note printed anyway would be a claim about a gap that is not
    there — the reader would learn to skim the line that matters."""
    votes = FakeVotes(reviews=[])
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh, ["1", "--cycle", CYCLE, "--prev-cycle",
                                          "cyc20260917-221117"])
    out = capsys.readouterr().out

    assert "no --cycle was given" not in out, out[:300]
    assert "could not be widened" not in out, out[:300]
    # Same move as above: the row is the first line after the tree line, and no
    # window note sits between them.
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0].startswith("tree: "), lines[0]
    assert lines[1].startswith("#1 "), lines[:2]
def test_the_absent_cycle_note_stays_out_of_the_json_document(mod, monkeypatch, capsys):
    """`--json` is one document: the gap is carried by the documented
    `vote_window_start: null` / `vote_window_source: ""` pair, and a prose line ahead
    of the list would break every machine consumer instead of warning them."""
    votes = FakeVotes(reviews=[])
    fresh = FakeFresh()
    _run(mod, monkeypatch, votes, fresh, ["1", "--json"])
    out = capsys.readouterr().out

    payload = json.loads(out)  # a prose line here raises, which is the assertion
    assert "no --cycle was given" not in out, out[:300]
    assert payload[0]["vote_window_start"] is None
    assert payload[0]["vote_window_source"] == ""
    assert payload[0]["action"] == "vote"
