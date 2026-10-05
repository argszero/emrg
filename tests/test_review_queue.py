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

import ast
import importlib.util
import json
import os
import re
import sys
import types
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


class FakeLedger:
    """`open_rant_rows` replaced: rows in, rows out, and what it was asked.

    Only the last of that function's three steps is faked. The other two — parsing the
    ledger's file format, and keeping only the rows whose status is open — are the
    sibling's, and they are pinned where they live (`tests/test_check_issue_links.py`);
    a lookalike here would agree with a misreading of the same file. What this file is
    about is what the queue *does* with those rows.
    """

    def __init__(self, rows: list | None = None, boom: str | None = None):
        self.rows = rows if rows is not None else []
        self.boom = boom
        self.calls: list[tuple[str | None, str | None]] = []

    def __call__(self, rants=None, repo=None):
        self.calls.append((rants, repo))
        if self.boom:
            raise RuntimeError(self.boom)
        return self.rows



def rants_of(mod, monkeypatch, *specs, boom=None):
    """Install ledger rows on the seam and hand back the fake, for its `calls`.

    Each `spec` is `(timestamp, status)`, `(timestamp, status, issues)` or
    `(timestamp, status, issues, project)` — the shape a caller varies one axis of, so the
    tests below read as one line each. A spec that names no project installs a row without
    one, which is the state §2.2 tells a cycle to ignore entirely, so it must be reachable.
    """
    rows = []
    for spec in specs:
        stamp, status = spec[0], spec[1]
        issues = list(spec[2]) if len(spec) > 2 else []
        project = spec[3] if len(spec) > 3 else ""
        rows.append(
            mod.Rant(
                timestamp=stamp,
                status=status,
                message=f"the ledger row for {stamp}",
                issues=issues,
                project=project,
            )
        )
    fake = FakeLedger(rows, boom)
    monkeypatch.setattr(mod, "open_rant_rows", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_ledger_unless_asked(mod, monkeypatch):
    """Every run reads the rant ledger through the seam, never through the host's file.

    `main` asks "is any rant open?" on **every** invocation now, so without this a test
    would open `~/.emrg/rants.jsonl` — and, when that file has an open row, make a second
    `gh api` call for the issues that declare it. A test that measures the machine it runs
    on is not a test of the tool, and on this host the ledger is *not* empty: the answer
    would differ between the author's laptop, CI, and the next cycle. The default is the
    empty ledger; `rants_of` installs rows for the tests that are about them.
    """
    monkeypatch.setattr(mod, "open_rant_rows", FakeLedger([]))


# --- the two halves, as the siblings answer them ---------------------------

class FakeVotes:
    """`check-vote-count.py`, answering `check_pr` from a routing list.

    Built around the real `Verdict`/`Vote` dataclasses with the real field values,
    so a field this tool reads under the wrong name fails here rather than in the
    live queue. `DEFAULT_MIN_VOTES` is carried because the tool reads the gate's
    threshold from the counter rather than keeping a second copy of the number.
    """

    DEFAULT_MIN_VOTES = 3
    #: The lifecycle states that mean the PR is over. Carried for the same reason as
    #: the threshold above: the tool asks the counter rather than spelling a second
    #: copy, so a fake that did not carry it would fail here — which is how the
    #: second copy was found (measured 2026-10-03, `cyc20261003-231313`).
    TERMINAL_STATES = ("MERGED", "CLOSED")

    def __init__(self, reviews: list[dict] | None = None, mergeable: str = "MERGEABLE",
                 state: str = "CLEAN", head: str = HEAD, valid: int | None = None,
                 push: str | None = None, exact: bool = True,
                 pr_state: str = "OPEN", merged_at: str = ""):
        self.reviews = reviews if reviews is not None else []
        self.mergeable = mergeable
        self.state = state
        #: GitHub's *lifecycle* state, which is a different field from `state` above
        #: (the merge state). Kept under its own name so a test cannot pass the merge
        #: state where the lifecycle one is read and still look right.
        self.pr_state = pr_state
        self.merged_at = merged_at
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
            state=self.pr_state,
            merged_at=self.merged_at,
            votes=votes,
            counted=[],
            valid_count=run if self.forced_valid is None else self.forced_valid,
            needed=needed,
        )


class FakeFresh:
    """`check-merge-freshness.py`, for both the fresh case and the four stale kinds."""

    def __init__(self, stale: bool = False, kind: str = "", behind: int = 0,
                 reason: str = "", run_id: str = ""):
        self.stale = stale
        self.kind = kind
        self.behind = behind
        self.reason = reason or f"some reason for {kind or 'fresh'}"
        self.run_id = run_id
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
            run_id=self.run_id,
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
        # Pin the previous cycle, so the abstention window is a property of this file
        # and not of the machine that runs it. Left to itself the tool resolves it from
        # the host's own cycles log, and the window's start is then whatever record
        # happens to sort last before this cycle's id: on a corpus that reaches back to
        # 2026-09-17 that is `PREV_CYCLE` (21:59) and every `PUSH_TIME` row is outside
        # the window, but a record set with a gap there falls back to `cycle-20260916`
        # and moves the start *behind* `PUSH_TIME`, which turns each `vote` /
        # `already-voted` row under test into `abstain`. Measured 2026-09-28: six rows
        # failed on a host whose newest record before `CYCLE` is 2026-09-16T15:46:18,
        # while a checkout with no records at all (which is what CI has) passes them.
        # Callers that exercise the log-reading path pass their own `--cycles-log`.
        if "--cycles-log" not in argv and "--prev-cycle" not in argv:
            argv += ["--prev-cycle", PREV_CYCLE]
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
    fresh = FakeFresh(stale=True, kind="failing", run_id="37194550758",
                      reason="CI concluded 'failure' on head aaaa")
    rc = _read(mod, monkeypatch, votes, fresh, cycle=CYCLE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "ci-red" in out
    assert "gh pr checks 1" in out
    assert "cast-vote.py" not in out
    # `gh pr checks` names the failing check, not why it failed, and the second half of
    # step 0.4's duty is "read its failing job's log to a cause". The row has to name the
    # reading that does, **with this run's id**, so the printed command runs as printed:
    # an id the reader has to fish out of the link above is one they can get wrong at the
    # moment they are least able to tell. And the broken path is not repeated as a thing
    # to try - `gh run view --log-failed` answers 0 bytes with rc 0 on this host (measured
    # 2026-10-04, a green run included), which is a failure to measure wearing a pass's
    # shape.
    assert "read-run-failure.py 37194550758" in out, (
        "the ci-red row names the failing check but no reading that can produce its "
        "cause with the run it is about"
    )
    assert "--log-failed" not in out, (
        "the row offers `gh run view --log-failed`, which answers 0 bytes with exit 0 "
        "here - a failure to measure wearing the shape of a pass"
    )
    # A red row is not always the head's: a base-level failure turns every open PR red,
    # and the reading that separates the two is the plan suite's base comparison.
    assert "check-merge-plan-suite.py 1" in out, (
        "without the base comparison the reader cannot tell this head's failure from one "
        "the base fails too, and fixing the wrong tree is what that costs"
    )


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
    # The command has to run as printed, and this row's did not: `re-trigger-ci.sh` is
    # a **bash** script, and it was printed behind the python runner, which hands python
    # a bash file. Measured 2026-10-04: `uv run --no-sync python3 scripts/re-trigger-ci.sh`
    # exits 1 with `SyntaxError: invalid syntax`. The one row whose whole remedy is
    # "re-trigger CI" was the one handing over a command that could not re-trigger
    # anything.
    #
    # Carrying `bash` was not the end of it either (#1852, re-opened by cycle
    # `cyc20261005-054639`): `bash` is on PATH on this host and is not on a Windows one,
    # so the row now **leads** with the command that needs only `gh` - the tool every
    # reader of this queue has already run - and keeps the script beneath it. Both halves
    # are asserted, and by position: a row that puts `bash` first is back to a command
    # that runs on one host family only.
    assert "$ gh workflow run test.yml --ref <branch-of-" in out, (
        "the re-trigger row leads with the host-portable form: `gh workflow run "
        "test.yml --ref <branch>`, which is what `re-trigger-ci.sh` itself runs"
    )
    assert "$ bash scripts/re-trigger-ci.sh" in out, (
        "the script stays as the alternative under it - it is the shorter spelling "
        "where `bash` exists"
    )
    lead = out.index("$ gh workflow run test.yml")
    alternative = out.index("$ bash scripts/re-trigger-ci.sh")
    assert lead < alternative, (
        "the portable form must come first: the reader who stops at the first line is "
        "the one this fix is for"
    )
    assert "uv run --no-sync python3 scripts/re-trigger-ci.sh" not in out, (
        "the re-trigger row hands a bash script to python, which exits 1 with a "
        "SyntaxError - the command cannot run as printed"
    )


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


def test_a_finished_pr_is_answered_as_over_and_hands_out_no_command(
    mod, monkeypatch, capsys
):
    """Issue #1837. The row this tool used to print for a PR that had just merged.

    Measured 2026-10-03 (`cyc20261003-224625`): a parallel cycle merged #1836 four
    seconds after this cycle's scan listed it open, so the queue read a finished PR as
    a live one and answered `read-first` with
    `check-vote-count.py <PR> --mergeability-wait 60` — a minute of waiting on a
    question GitHub never answers for a merged PR (measured live: 6.2 s and the same
    failure). The state now decides the row, and the freshness half is not read at all:
    it prices a branch refresh for a branch that is finished.
    """
    votes = FakeVotes(pr_state="MERGED", merged_at="2026-10-03T14:46:59Z")
    fresh = FakeFresh(stale=True, kind="ancestry", behind=2)
    _install(mod, monkeypatch, votes, fresh)
    rc = mod.main(["1"])
    out = capsys.readouterr().out
    assert rc == 0, "an answered question is not a failure to measure"
    assert "terminal" in out
    assert "MERGED" in out and "2026-10-03T14:46:59Z" in out
    assert "0/3 votes" not in out, "a finished PR has no review left to count"
    assert "$" not in out, "there is no command to hand a cycle here"
    assert "--mergeability-wait" not in out, "the wait that cannot succeed"
    assert fresh.calls == [], "no ancestry question about a PR with no merge left"


def test_a_closed_pr_is_not_reported_as_merged(mod, monkeypatch, capsys):
    """Closed-unmerged is the other terminal state, and it did not land.

    A single "finished" word would be wrong half the time: `CLOSED` says the branch is
    out of play, and a reader who acted on `MERGED` would look for it on master.
    """
    votes = FakeVotes(pr_state="CLOSED")
    fresh = FakeFresh()
    _install(mod, monkeypatch, votes, fresh)
    rc = mod.main(["1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CLOSED" in out
    assert "MERGED" not in out


def test_which_states_are_terminal_is_read_from_the_counter(mod, monkeypatch):
    """The vocabulary has one home, and this tool asks it instead of keeping a copy.

    Measured 2026-10-03 (`cyc20261003-231313`): `Reading.terminal` spelled
    `("MERGED", "CLOSED")` by hand while the counter introduced `TERMINAL_STATES` as
    "the one spelling of 'the PR is over' in the family" and the *other* sibling
    (`check-merge-freshness.py`) already asked it. This file is the one that reads the
    state off the verdict, so it is the one a drifted copy would mislead.

    The leg narrows the counter's list and requires the row to follow it: a copy in
    this file would keep saying `MERGED` is terminal and pass a test written the other
    way round. It is the behavioural half of `test_it_does_not_spell_the_words_itself`.
    """
    class _Narrowed:
        TERMINAL_STATES = ("CLOSED",)

    monkeypatch.setattr(mod, "vote_counter", lambda: _Narrowed)
    assert mod.Reading(pr=1, head="", state="CLOSED").terminal is True
    assert mod.Reading(pr=1, head="", state="MERGED").terminal is False, (
        "the counter says MERGED is not terminal here, so this row must follow it"
    )


def test_an_unread_state_is_not_a_second_failure(mod, monkeypatch):
    """`""` means "not read", and asking the vocabulary there would raise again.

    Measured 2026-10-03 (`cyc20261003-231313`): making the property ask the counter
    broke `test_an_unreadable_count_is_a_question_mark_not_zero`, whose fake raises
    from `check_pr` and carries no vocabulary at all. A row whose count could not be
    read has no state either, and one failure must not become two.
    """
    class _Boom:
        # No TERMINAL_STATES: touching this fake from here is the defect.
        def __getattr__(self, name):
            raise AssertionError(f"the counter was asked for {name!r} despite no state")

    monkeypatch.setattr(mod, "vote_counter", lambda: _Boom())
    assert mod.Reading(pr=1, head="", state="").terminal is False


def test_it_does_not_spell_the_words_itself(mod):
    """The source half of the same rule, kept beside the behavioural one.

    A rule with two homes is free to drift, and the drift is invisible until the two
    disagree — which is exactly the state this file was in. The words belong to
    `check-vote-count.py`; every other tool asks for the list.

    Read with `ast`, not with a substring search: the docstring above `terminal`
    **quotes** the old spelling while explaining why it is gone, and a text search
    cannot tell a rule's explanation from a rule's violation. (The first version of
    this leg was written that way and failed on its own prose — the same lesson
    `scripts/check_read_parse_guards.py` records about reading what a call is fed
    rather than the line it sits on.)
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    copies = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.Tuple, ast.Set, ast.List))
        and {
            e.value
            for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        }
        >= {"MERGED", "CLOSED"}
    ]
    assert not copies, (
        "review-queue.py spells the terminal states again at line(s) "
        f"{copies} - read them from the counter (`vote_counter().TERMINAL_STATES`), the "
        "way `votes_needed` reads the threshold"
    )


def test_the_pusher_claim_matches_what_github_records(mod):
    """A stated reason must match what the instrument can read, not assert a universal.

    The clause is read off the clock, and the reason given for it was "who pushed a head
    is not a fact GitHub records" — refuted by one probe: the repository's events feed
    carries the pusher as `PushEvent.actor` (measured 2026-10-05). What the feed cannot
    do is answer for a *given* head — it is rolling, and over the window measured it
    carried no `PushEvent` for `#1857`'s head while reaching back three days. So the
    reason has to name that reading and its limit; a universal a reader can disprove with
    one command is the family this repo keeps out of its prompts and scripts alike.
    """
    doc = ast.get_docstring(ast.parse(SCRIPT.read_text(encoding="utf-8"))) or ""
    assert "is not a fact GitHub records" not in doc, (
        "the pusher *is* recorded — the events feed carries `PushEvent.actor`; the clause "
        "is read off the clock because that feed cannot answer for a given head, and the "
        "reason has to say so rather than claim the fact is unrecorded"
    )
    assert "PushEvent.actor" in doc, (
        "the clause's stated reason must name the reading it rests on (the rolling "
        "events feed), not leave the clock unexplained"
    )


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
        "subject": "pr",
        "tree": "/checkout",
        "branch": "some-branch",
        "pr": 1,
        "head": HEAD,
        "title": "pr 1",
        "votes": 1,
        "needed": 3,
        "mergeable": "MERGEABLE",
        "merge_state": "CLEAN",
        "state": "OPEN",
        "terminal": False,
        "merged_at": None,
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


def test_the_records_land_where_the_queue_reads_them(mod, tmp_path):
    """The record root the memory system writes is the one this tool searches.

    This guard used to scan the rendered template for its `cycle-<ts>.md` path and
    require it to land in `DEFAULT_CYCLES_LOGS`. On 2026-09-29 the host ruled that the
    prompt stops naming *where* a record goes — 「不需要指定什么东西记录到什么地方」, since a
    session has a memory system and only needs telling *what* to record — so the path left
    the template and the prose-level claim became unmeasurable. The invariant it protected
    is still real and still worth a reading, so it is now read against the writer
    (`_collect_memory_data`, the loader that embeds the index a cycle appends to) and
    composed with the reader (`DEFAULT_CYCLES_LOGS`).

    **This test failed in CI on its first run, and why is the point of its shape.** It
    first read the index at *this checkout's* `.emrg/memory/MEMORY.md`, which exists on a
    host that runs cycles and **not in a fresh clone**: `.emrg` is gitignored, so a runner
    has no such file and the guard failed there while passing here (run 36519484170,
    `test` and `test-windows`). A guard whose premise is a local-only artifact measures
    the host, not the tree. So the checkout is built here with `tmp_path` and the claim is
    split into its two halves, each measured where it is decidable:

    1. the **writer** derives `<cwd>/.emrg/memory` — measured on a directory this test
       creates, so it holds on any host; and
    2. the **reader** searches `<this checkout>/.emrg/memory` — a constant, needing no file.

    Composed, they are the sentence the old scan asserted: a cycle running in this checkout
    records where the queue reads. A record written anywhere else is a record no window
    reads — and the abstention window is read from these files.
    """
    from types import SimpleNamespace

    from emrg.server.daemon import EmrgServer

    # 1. The writer, on a directory that exists wherever this runs.
    checkout = tmp_path / "checkout"
    memory_root = checkout / ".emrg" / "memory"
    memory_root.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text("# index\n", encoding="utf-8")

    # The same shape `tests/test_prompt_templates.py` uses: an uninitialised instance is
    # enough, because the loader touches only `session.cwd` / `session.memory_dir`.
    daemon = EmrgServer.__new__(EmrgServer)
    data = daemon._collect_memory_data(
        SimpleNamespace(cwd=checkout, memory_dir=checkout / ".emrg" / "sessions" / "x" / "memory")
    )
    assert data and data.get("project_memory_dir"), (
        "the loader embedded no project root for a session whose cwd carries "
        f"{memory_root}/MEMORY.md — the reading below would be about nothing"
    )
    loaded = Path(data["project_memory_dir"]).resolve()
    assert loaded == memory_root.resolve(), (
        f"the daemon's project-memory root is {loaded}, not {memory_root} — a cycle's "
        "record lands under whatever this returns, so a change here moves the corpus"
    )

    # 2. The reader: the same directory, inside THIS checkout. No file is required: the
    # root is a constant, and requiring the index is what made this guard fail on a
    # fresh clone.
    roots = {root.resolve() for root in mod.DEFAULT_CYCLES_LOGS}
    assert (REPO_ROOT / ".emrg" / "memory").resolve() in roots, (
        f"review-queue searches {sorted(str(r) for r in roots)}, which does not include "
        f"{REPO_ROOT / '.emrg' / 'memory'} — the abstention window is read from the "
        "records, so a cycle writing where the queue does not look is one no window can see"
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


# --- the rows the PR half cannot show --------------------------------------
#
# The defect these pin (memory `queue-renders-no-row-for-a-pending-rant.md`, measured
# 2026-09-29): the reading had no rant input at all, so a cycle that asked it "is there
# anything to move?" was told `3 PR(s): measure-then-vote 2, park 1` while three pending
# rants with no issue sat in the ledger. The rows below are the second half of the
# answer, and the pair that matters is the two `nothing` cases — an empty queue *with* an
# open rant, and one without — because collapsing those is the whole defect.


def test_an_empty_queue_with_an_open_rant_does_not_say_nothing_to_review(
    mod, monkeypatch, capsys
):
    """The trap itself. `nothing to review` is a claim about the whole queue, and the
    ungated version of this line printed it while unstarted work existed."""
    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    rants_of(mod, monkeypatch, ("2026-09-30T09:17:54+08:00", "pending"))
    rc = mod.main([])
    out = capsys.readouterr().out

    assert rc == 0
    assert "nothing to review" not in out, out
    assert "1 open rant(s)" in out
    assert "2026-09-30T09:17:54+08:00" in out


def test_an_empty_queue_and_an_empty_ledger_is_nothing_to_review(
    mod, monkeypatch, capsys
):
    """The control on the line above: with no PR and no open rant the sentence is true,
    so the guard must not be bought by never printing it."""
    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    rc = mod.main([])
    out = capsys.readouterr().out

    assert rc == 0
    assert "nothing to review" in out
    assert "open rant(s)" not in out


def test_a_pending_rant_is_a_row_with_its_status_and_no_issue(mod, monkeypatch, capsys):
    """`no issue yet` is R5 seen from the queue's side: the issue and its PR are born
    together, so a rant row that has neither is the row a cycle must act on first."""
    votes, fresh = FakeVotes(reviews=[]), FakeFresh()
    rants_of(mod, monkeypatch, ("2026-09-30T09:35:04+08:00", "pending"))
    _run(mod, monkeypatch, votes, fresh, ["1"])
    out = capsys.readouterr().out

    assert "rant 2026-09-30T09:35:04+08:00  pending  no issue yet" in out
    assert "the ledger row for 2026-09-30T09:35:04+08:00" in out


def test_a_rant_names_the_issue_that_declares_it(mod, monkeypatch, capsys):
    """Two numbers mean two issues claim one rant — a duplicate `check-issue-links.py`
    reports; this row names them all rather than picking one."""
    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    rants_of(mod, monkeypatch, ("2026-09-30T09:30:16+08:00", "in_progress", [1771, 1772]))
    mod.main([])
    out = capsys.readouterr().out

    assert "in_progress  #1771, #1772" in out


def test_the_rant_rows_are_after_the_pr_rows_and_under_their_own_header(
    mod, monkeypatch, capsys
):
    """A cycle that reads only the `N PR(s)` summary line must not come away thinking the
    queue was the whole of what is open."""
    votes, fresh = FakeVotes(reviews=[_review(cycle="cyc1")]), FakeFresh()
    rants_of(mod, monkeypatch, ("2026-09-30T09:17:54+08:00", "pending"))
    _run(mod, monkeypatch, votes, fresh, ["1"])
    out = capsys.readouterr().out

    lines = [line for line in out.splitlines() if line.strip()]
    pr_row = next(i for i, line in enumerate(lines) if line.startswith("#1 "))
    header = next(i for i, line in enumerate(lines) if "open rant(s)" in line)
    assert pr_row < header, out
    assert "1 PR(s)" in out


def test_a_rant_row_names_the_project_it_belongs_to(mod, monkeypatch, capsys):
    """§2.2 decides membership by `project`, so the row has to carry it.

    The reading exists for the cycle that reads the queue *alone* (the tool's own reason for
    adding these rows), and that reader cannot apply a rule whose field is not printed: it
    would have to open the ledger again — the cost the row was added to save. Measured
    2026-10-01 on this host, the queue rendered 39 open rants and every one belonged to
    another project, while this task's ledger rows were all `completed`.

    Read through `--all-rants`, because this test is about the **row's shape** and the
    default rendering withholds another project's row (that behaviour is pinned next door).
    """
    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    rants_of(
        mod,
        monkeypatch,
        ("2026-09-30T09:35:04+08:00", "pending", [], "silicon-science-cs"),
        ("2026-09-30T09:30:16+08:00", "pending", [], ""),
    )
    mod.main(["--all-rants"])
    out = capsys.readouterr().out

    assert "project=silicon-science-cs" in out
    assert "project=(none)" in out, (
        "a row that names no project must say so: §2.2 makes it one to ignore entirely, "
        "and a row that prints nothing there is a row with no such state"
    )


def test_another_projects_rant_is_counted_and_not_printed(mod, monkeypatch, capsys):
    """The bulk this report pays for, and the row it must not lose.

    Measured 2026-10-04 on this host: 40 open rants, 39 of them `silicon-science-cs` — about
    8.7KB of a 9.7KB report, rendered for a cycle the prompt itself tells that those rows are
    not its work. They are now **counted and not printed**, which is the reading's cost and
    not its content: the header keeps the ledger whole, so nothing is hidden.
    """
    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    rants_of(
        mod,
        monkeypatch,
        ("2026-09-30T09:35:04+08:00", "pending", [], "silicon-science-cs"),
        ("2026-09-30T09:30:16+08:00", "pending", [], "emrg"),
    )
    rc = mod.main([])
    out = capsys.readouterr().out

    assert rc == 0
    assert "rant 2026-09-30T09:30:16+08:00" in out, "this task's own row is rendered"
    assert "rant 2026-09-30T09:35:04+08:00" not in out, (
        "another project's row body is what the default withholds"
    )
    header = next(line for line in out.splitlines() if "open rant(s)" in line)
    assert "2 open rant(s) across 2 project(s)" in header, (
        "the count stays the ledger's: withholding a row is not hiding it"
    )
    withheld = next(line for line in out.splitlines() if "not printed here" in line)
    assert "1 of them names another project" in withheld, withheld
    assert "--all-rants" in withheld, (
        "a withheld row must come with the way to see it, or the reader has no remedy"
    )
    assert "`rendered_here` withholds another project's rows and only those" in withheld, (
        "the reason must name the axis that withholds. It used to say `no issue in "
        "argszero/emrg can declare them`, which is also true of the project-less row this "
        "report prints - so it separated nothing (see "
        "test_the_rant_header_and_the_withheld_line_name_what_withholds)"
    )


def test_a_project_less_rant_is_still_printed_though_it_can_declare_nothing_here(
    mod, monkeypatch, capsys
):
    """`could_declare_here` is not the rendering rule, and the difference is deliberate.

    That predicate answers "could an issue in this repo carry the rant's `Origin:` line",
    and for a row naming no project it is false. A project-less row is not *another*
    project's work, though — it is undeclared, and undeclared is something the cycle has to
    look at. The rows this table prints exist because a cycle reading the queue alone
    concluded "nothing to review" while three pending rants had no issue (2026-09-29), so
    hiding an undeclared one would restore exactly that defect for the row most likely to be
    this task's.
    """
    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    rants_of(
        mod,
        monkeypatch,
        ("2026-09-30T09:30:16+08:00", "pending", [], ""),
        ("2026-09-30T09:35:04+08:00", "pending", [], "silicon-science-cs"),
    )
    mod.main([])
    out = capsys.readouterr().out

    assert "rant 2026-09-30T09:30:16+08:00" in out, "the undeclared row is rendered"
    assert "project=(none)" in out
    assert "rant 2026-09-30T09:35:04+08:00" not in out
    assert mod.could_declare_here("", mod.REPO) is False, (
        "the two rules must be seen to differ here, or this test would pass under either"
    )
    assert mod.rendered_here("", mod.REPO) is True


def test_all_rants_prints_the_withheld_rows_and_the_line_disappears(
    mod, monkeypatch, capsys
):
    """The other direction of the same flag: withheld means withheld *by default*."""
    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    rants_of(
        mod,
        monkeypatch,
        ("2026-09-30T09:35:04+08:00", "pending", [], "silicon-science-cs"),
        ("2026-09-30T09:30:16+08:00", "pending", [], "emrg"),
    )
    mod.main(["--all-rants"])
    out = capsys.readouterr().out

    assert "rant 2026-09-30T09:35:04+08:00" in out
    assert "rant 2026-09-30T09:30:16+08:00" in out
    assert "not printed here" not in out, (
        "nothing was withheld, so the sentence about withholding would be false"
    )


def test_the_all_rants_help_states_the_withheld_set_the_predicate_defines(mod, capsys):
    """The `--help` sentence is the only statement of the flag's scope a reader meets.

    It was wrong here: it said the default counts "a rant naming another project (or none)",
    while `rendered_here` renders a project-less row by default - pinned above, and for the
    reason that an undeclared row is not *another* project's work. A sentence restating a
    rule that lives elsewhere is a second copy of it; this holds the copy against the
    original, the predicate's three answers beside the sentence's two claims.
    """
    rendered = [
        mod.rendered_here(project, mod.REPO)
        for project in ("emrg", "silicon-science-cs", "")
    ]
    assert rendered == [True, False, True], (
        "the rule the sentence states: this repo's rows, and the rows naming no project"
    )

    with pytest.raises(SystemExit):
        mod.main(["--help"])
    # argparse wraps the text at the terminal width, so a phrase can span a line break: the
    # claims are read from the help text with its whitespace collapsed, not from its lines.
    out = " ".join(capsys.readouterr().out.split())

    assert "rendered_here" in out, "the sentence names the rule that decides it"
    assert "naming no project is still printed" in out, (
        "the one case the sentence had wrong, now stated the way the predicate reads"
    )


def test_the_rant_header_and_the_withheld_line_name_what_withholds(
    mod, monkeypatch, capsys
):
    """Both sentences a cycle reads on every run, held against the predicate that decides.

    The `--help` copy was fixed for this on the same branch and the report's **header** was
    not: it still told the reader that "a row naming another project - or none - is not this
    cycle's work", one line above the project-less row it then printed. The withheld line
    gave a reason that does not discriminate either - "no issue in argszero/emrg can declare
    them" is equally true of that project-less row, because `could_declare_here("")` is False
    too. The rule is `rendered_here`, spelled once, and these are its two copies.
    """
    rendered = [
        mod.rendered_here(project, mod.REPO)
        for project in ("emrg", "silicon-science-cs", "")
    ]
    assert rendered == [True, False, True], (
        "the rule both sentences state: this repo's rows, and the rows naming no project"
    )
    assert mod.could_declare_here("silicon-science-cs", mod.REPO) is False
    assert mod.could_declare_here("", mod.REPO) is False, (
        "the reason the withheld line used to give is true of a row this report prints, so "
        "it is not the reason anything is withheld - which is what this test keeps true"
    )

    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    rants_of(
        mod,
        monkeypatch,
        ("2026-09-30T09:35:04+08:00", "pending", [], "silicon-science-cs"),
        ("2026-09-30T09:30:16+08:00", "pending", [], ""),
    )
    mod.main([])
    # The sentences are read with their whitespace collapsed, so the assertions are about
    # the claim and not about where a line happens to break.
    out = " ".join(capsys.readouterr().out.split())

    assert out.count("rendered_here") >= 2, (
        "both sentences name the rule that decides them, the way the --help copy does"
    )
    assert "or none" not in out, (
        "the header's own clause, the defect this pins: a row naming no project is not "
        "withheld, and a report saying so is contradicted by the row it prints beside it"
    )
    assert "one naming no project is undeclared rather than another project's" in out, (
        "the case the header had wrong, stated the way the predicate reads"
    )
    assert "rant 2026-09-30T09:30:16+08:00" in out, (
        "and the row the header is about really is printed"
    )


def test_the_json_document_keeps_every_rant_row_and_labels_it(mod, monkeypatch, capsys):
    """A consumer must not silently receive a shorter list than the ledger holds.

    The prose withholds another project's row because its reader pays for the bytes; the
    document keeps every row and carries the prose's own predicate as a field, spelled from
    the same function. A JSON path that dropped rows with no marker would be this family's
    "never a pass" defect in its machine-readable form: a short list read as a whole one.
    """
    votes, fresh = FakeVotes(reviews=[]), FakeFresh()
    monkeypatch.setattr(mod, "local_tree", lambda: ("/checkout", "some-branch", "b" * 40))
    rants_of(
        mod,
        monkeypatch,
        ("2026-09-30T09:35:04+08:00", "pending", [], "silicon-science-cs"),
        ("2026-09-30T09:30:16+08:00", "pending", [], ""),
        ("2026-09-30T09:17:54+08:00", "pending", [], "emrg"),
    )
    _run(mod, monkeypatch, votes, fresh, ["1", "--json"])
    payload = json.loads(capsys.readouterr().out)

    rants = [row for row in payload if row["subject"] == "rant"]
    assert [row["project"] for row in rants] == ["silicon-science-cs", "", "emrg"]
    assert [row["rendered"] for row in rants] == [False, True, True], (
        "the field is the prose's predicate, so a consumer can apply the same rule"
    )


def test_the_rant_section_counts_the_ledger_by_project(mod, monkeypatch, capsys):
    """The number heading the section is the ledger's, not this task's, and it says which.

    The trap this closes: a cycle reads "39 open rant(s) - each needs an issue and its PR"
    as priority 1 (R8.1) and opens an issue in its own repo for another project's work —
    the cross-project contamination §2.2's filter exists to prevent. Two projects, so the
    grouping is read rather than inferred from a single name.
    """
    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    rants_of(
        mod,
        monkeypatch,
        ("2026-09-30T09:35:04+08:00", "pending", [], "emrg"),
        ("2026-09-30T09:34:00+08:00", "pending", [], "emrg"),
        ("2026-09-30T09:30:16+08:00", "pending", [], "silicon-science-cs"),
    )
    mod.main([])
    out = capsys.readouterr().out

    header = next(line for line in out.splitlines() if "open rant(s)" in line)
    assert "3 open rant(s) across 2 project(s) - emrg 2, silicon-science-cs 1" in header, header
    assert "once it is this task's" in header, (
        "the sentence must not claim every row's issue and PR unconditionally"
    )
    assert "matched to a task by" in header or "matches a rant to a task by" in header, (
        "the header must say how membership is decided, so the reader can apply it"
    )


def test_the_ledger_read_is_spent_only_when_a_rant_is_open(mod, monkeypatch, capsys):
    """The seam is the tool's own function, so this test also pins the *arguments*: the
    `--rants` override is what a host with a ledger elsewhere has to be able to move."""
    fake = rants_of(mod, monkeypatch, ("2026-09-30T09:17:54+08:00", "pending"))
    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    mod.main(["--rants", "/somewhere/else.jsonl", "--repo", "owner/repo"])
    capsys.readouterr()

    assert fake.calls == [("/somewhere/else.jsonl", "owner/repo")]


def test_an_unreadable_ledger_is_not_an_empty_one(mod, monkeypatch, capsys):
    """`no open rants` and `could not read the ledger` are the two answers this file's
    sibling exists to keep apart, and the empty one is the one a cycle acts on."""
    monkeypatch.setattr(mod, "open_prs", lambda repo=mod.REPO: [])
    rants_of(mod, monkeypatch, boom="the rant ledger could not be read (/nope.jsonl)")
    rc = mod.main([])
    captured = capsys.readouterr()

    assert rc == 2
    assert "could not be read" in captured.err
    assert "nothing to review" not in captured.out, captured.out
    assert "open rant(s)" not in captured.out, captured.out


def test_an_unreadable_ledger_does_not_erase_the_pr_half(mod, monkeypatch, capsys):
    """The PR rows are measured before the ledger is opened, so a cycle still gets the
    half that could be measured — and exit 2, because the other half is not a pass."""
    votes, fresh = FakeVotes(reviews=[_review(cycle="cyc1")]), FakeFresh()
    rants_of(mod, monkeypatch, boom="the rant ledger could not be read (/nope.jsonl)")
    rc = _run(mod, monkeypatch, votes, fresh, ["1"])
    captured = capsys.readouterr()

    assert rc == 2
    assert "#1 " in captured.out, captured.out
    assert "could not be read" in captured.err


def test_the_json_document_carries_both_subjects_in_one_list(mod, monkeypatch, capsys):
    """One shape, and `subject` on **every** row: a consumer that had to infer "no `pr`
    key means a rant" would be reading a shape by absence."""
    votes, fresh = FakeVotes(reviews=[]), FakeFresh()
    monkeypatch.setattr(mod, "local_tree", lambda: ("/checkout", "some-branch", "b" * 40))
    rants_of(mod, monkeypatch, ("2026-09-30T09:17:54+08:00", "pending", [1771]))
    _run(mod, monkeypatch, votes, fresh, ["1", "--json"])
    out = capsys.readouterr().out

    payload = json.loads(out)
    assert isinstance(payload, list), type(payload)
    assert [row["subject"] for row in payload] == ["pr", "rant"], payload
    rant = payload[-1]
    assert rant["timestamp"] == "2026-09-30T09:17:54+08:00"
    assert rant["status"] == "pending"
    assert rant["issues"] == [1771]
    assert rant["tree"] == "/checkout"
    assert rant["branch"] == "some-branch"
    assert rant["project"] == "", (
        "the two renderings must not disagree: whatever the text row shows, the document "
        "carries the same field"
    )


def test_the_json_rant_row_carries_the_project(mod, monkeypatch, capsys):
    """The document's half of the same fix, with a project set rather than absent."""
    votes, fresh = FakeVotes(reviews=[]), FakeFresh()
    monkeypatch.setattr(mod, "local_tree", lambda: ("/checkout", "some-branch", "b" * 40))
    rants_of(mod, monkeypatch, ("2026-09-30T09:17:54+08:00", "pending", [], "emrg"))
    _run(mod, monkeypatch, votes, fresh, ["1", "--json"])
    out = capsys.readouterr().out

    assert json.loads(out)[-1]["project"] == "emrg"


def test_the_json_document_keeps_its_shape_with_no_rants(mod, monkeypatch, capsys):
    """The other direction: the list is the document whether or not there are rants, and
    a rant row is appended to it rather than replacing its shape."""
    votes, fresh = FakeVotes(reviews=[]), FakeFresh()
    monkeypatch.setattr(mod, "local_tree", lambda: ("/checkout", "some-branch", "b" * 40))
    _run(mod, monkeypatch, votes, fresh, ["1", "--json"])
    out = capsys.readouterr().out

    payload = json.loads(out)
    assert [row["subject"] for row in payload] == ["pr"], payload


# --- the body the autouse stub hides ---------------------------------------
#
# Everything above replaces `open_rant_rows` wholesale, which is right for testing what the
# queue *does* with rows — and wrong for the three decisions inside it: which rows are
# open, whether the issue lookup is spent, and which ledger is read. Those are pinned here
# against a second import of the tool, because the autouse fixture has already replaced the
# attribute on the shared module by the time a test body runs.


def _fresh_tool():
    """The tool imported again, under a name of its own.

    A distinct name so this load does not displace the fixture's module in `sys.modules`:
    `dataclasses` resolves annotations through it at class-creation time, and a missing
    entry raises an `AttributeError` from inside `dataclasses` itself.
    """
    spec = importlib.util.spec_from_file_location("review_queue_real", SCRIPT)
    tool = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tool
    spec.loader.exec_module(tool)
    return tool


class FakeLinks:
    """`check-issue-links.py` as `open_rant_rows` uses it — ledger and queue, no network.

    The four names this tool reaches for, and nothing else: a wider fake would let the
    tool call something the real sibling does not have, and the failure would land in the
    live queue instead of here.
    """

    def __init__(self, rows, issues=(), path="/tmp/rants.jsonl", closed_issues=()):
        self.rows = rows
        self.issues = [{"number": n, "body": b} for n, b in issues]
        self.closed_issues = [{"number": n, "body": b} for n, b in closed_issues]
        self.path = Path(path)
        self.queue_calls: list[tuple[str, str, str | None]] = []
        self.read: Path | None = None
        self.origins_asked: list[list[dict]] = []

    def rants_path(self, override=None):
        return Path(override) if override else self.path

    def load_rant_rows(self, path):
        self.read = path
        return self.rows

    def load_queue(self, repo, state="open", since=None):
        """The sibling's loader, mirroring its signature — including the bounded closed
        reading `open_rant_rows` spends for rants with no open declaring issue."""
        self.queue_calls.append((repo, state, since))
        if state == "closed":
            return types.SimpleNamespace(issues=self.closed_issues)
        return types.SimpleNamespace(issues=self.issues)

    def declared_origins(self, issues):
        """The sibling's reading, as its own tests pin it: the `Origin: rant <ts>` line."""
        self.origins_asked.append(list(issues))
        found = {}
        for issue in issues:
            stamps = re.findall(r"^Origin: rant (\S+)\s*$", issue["body"], re.M)
            if stamps:
                found[int(issue["number"])] = stamps
        return found


def _links(monkeypatch, tool, rows, issues=(), path="/tmp/rants.jsonl", closed_issues=()):
    links = FakeLinks(rows, issues, path, closed_issues)
    monkeypatch.setattr(tool, "issue_links", lambda: links)
    return links


def _row(stamp, status="pending", message="a rant"):
    return {"timestamp": stamp, "status": status, "message": message, "project": "emrg"}


def test_only_open_rants_become_rows_and_the_newest_is_first(monkeypatch):
    """`completed` is history (R6's cleanup keeps the ten most recent), so rendering it
    would bury the work that is actually left — and the order is what a cycle reads first."""
    tool = _fresh_tool()
    _links(
        monkeypatch,
        tool,
        [
            _row("2026-09-30T09:17:54+08:00", "pending"),
            _row("2026-09-30T09:35:04+08:00", "completed"),
            _row("2026-09-30T09:30:16+08:00", "in_progress"),
        ],
    )

    rows = tool.open_rant_rows(None, tool.REPO)

    assert [row.timestamp for row in rows] == [
        "2026-09-30T09:30:16+08:00",
        "2026-09-30T09:17:54+08:00",
    ], rows


def test_the_issue_lookup_is_not_spent_when_no_rant_is_open(monkeypatch):
    """It costs a `gh` call, and a queue with nothing pending should not pay it. A short
    circuit written the other way round reads a second endpoint on every run, which is a
    cost nobody sees and a network dependency on a queue that needs none."""
    tool = _fresh_tool()
    links = _links(monkeypatch, tool, [_row("2026-09-30T09:35:04+08:00", "completed")])

    assert tool.open_rant_rows(None, tool.REPO) == []
    assert links.queue_calls == [], "the queue must not be listed for a closed ledger"


def test_a_rants_project_is_carried_from_the_row_the_ledger_parsed(monkeypatch):
    """The field comes from the row the sibling handed over, and nothing else.

    A missing `project` is **not** an error and not a default: §2.2 makes a rant that names
    no project one to ignore, so the difference between "names none" and "names another" has
    to survive the parse — a `row["project"]` reached for directly would raise here, and a
    fallback to the reading's own repo would invent a membership the ledger never claimed.
    """
    tool = _fresh_tool()
    _links(
        monkeypatch,
        tool,
        [
            _row("2026-09-30T09:17:54+08:00"),
            {"timestamp": "2026-09-30T09:30:16+08:00", "status": "pending", "message": "m"},
            {"timestamp": "2026-09-30T09:35:04+08:00", "status": "pending", "project": None},
        ],
    )

    rows = {row.timestamp: row.project for row in tool.open_rant_rows(None, tool.REPO)}

    assert rows == {
        "2026-09-30T09:17:54+08:00": "emrg",
        "2026-09-30T09:30:16+08:00": "",
        "2026-09-30T09:35:04+08:00": "",
    }, rows


def test_an_issue_declaring_a_rant_is_attached_to_that_rants_row(monkeypatch):
    """The join is by timestamp, per `Origin: rant <ts>` — and an issue naming no rant
    must not be attached to one by accident of ordering."""
    tool = _fresh_tool()
    _links(
        monkeypatch,
        tool,
        [_row("2026-09-30T09:17:54+08:00"), _row("2026-09-30T09:30:16+08:00")],
        issues=[
            (1771, "Origin: rant 2026-09-30T09:17:54+08:00\n\nthe body"),
            (1772, "no origin line here"),
        ],
    )

    rows = {row.timestamp: row.issues for row in tool.open_rant_rows(None, tool.REPO)}

    assert rows == {
        "2026-09-30T09:17:54+08:00": [1771],
        "2026-09-30T09:30:16+08:00": [],
    }, rows


def test_the_ledger_the_override_names_is_the_one_read(monkeypatch):
    """`--rants` is the whole of how a host with a ledger elsewhere is reached.

    The two calls are pinned as a **rule**, not as a count: the open reading is always
    spent, and the closed one only for a rant the open reading could not place, bounded by
    that rant's own instant normalised to UTC. `+08:00` in a query string is a space to
    the API, so the spelling here is the whole reason the bound is expressible at all.
    """
    tool = _fresh_tool()
    links = _links(monkeypatch, tool, [_row("2026-09-30T09:17:54+08:00")])

    tool.open_rant_rows("/elsewhere/rants.jsonl", tool.REPO)

    assert links.read == Path("/elsewhere/rants.jsonl")
    assert links.queue_calls == [
        (tool.REPO, "open", None),
        (tool.REPO, "closed", "2026-09-30T01:17:54Z"),
    ], links.queue_calls


def test_another_projects_rants_do_not_widen_the_closed_window(monkeypatch):
    """The bound is the oldest instant that can *place* a row, and only this repo's rants can.

    Measured 2026-10-02: 40 open rants, 39 of them `silicon-science-cs` reaching back to
    2026-09-11, and the one `emrg` rant 8 hours old. Letting the other project set the
    bound spent **48.8s / 668 rows** on a reading whose usable window costs **5.8s / 6
    rows** — for a repo that can never place a row for a rant it cannot declare.
    """
    tool = _fresh_tool()
    emrg_stamp = "2026-10-02T07:48:41.525323+08:00"
    other_stamp = "2026-09-11T23:51:35.442412+08:00"
    links = _links(
        monkeypatch,
        tool,
        [
            _row(emrg_stamp),
            {"timestamp": other_stamp, "status": "pending", "message": "theirs",
             "project": "silicon-science-cs"},
        ],
    )

    rows = tool.open_rant_rows(None, tool.REPO)

    assert links.queue_calls == [
        (tool.REPO, "open", None),
        (tool.REPO, "closed", "2026-10-01T23:48:41Z"),
    ], links.queue_calls
    assert len(rows) == 2, (
        "the narrowing is about the *reading*, not the rows: another project's rant is "
        "still rendered with its project named, so the cycle reading the queue alone can "
        f"skip it - got {rows!r}"
    )
    assert [r.project for r in rows] == ["emrg", "silicon-science-cs"], (
        "newest first, each row carrying its own project"
    )


def test_a_foreign_project_alone_spends_no_closed_reading(monkeypatch):
    """No rant this repo could declare means no second call at all.

    The reading exists to tell `never filed` from `filed and closed` *in this repo*. With
    only another project's rants open, this repo has nothing to look up: the call would
    return rows that cannot place any of them.
    """
    tool = _fresh_tool()
    links = _links(
        monkeypatch,
        tool,
        [{"timestamp": "2026-09-11T23:51:35.442412+08:00", "status": "pending",
          "message": "theirs", "project": "silicon-science-cs"}],
    )

    (rant,) = tool.open_rant_rows(None, tool.REPO)

    assert links.queue_calls == [(tool.REPO, "open", None)], links.queue_calls
    assert (rant.issues, rant.closed_issues) == ([], [])


def test_the_two_spellings_of_this_project_both_match(monkeypatch):
    """`emrg` and `argszero/emrg` are the same project, and R5's own rule says so.

    The template matches a rant to a task by either spelling, so a bound that recognised
    only one would silently stop placing rows for rants written the other way — and a
    row that stops being placed is the `no issue yet` defect this reading removes.
    """
    tool = _fresh_tool()
    assert tool.could_declare_here("emrg", tool.REPO) is True
    assert tool.could_declare_here(tool.REPO, tool.REPO) is True
    assert tool.could_declare_here("silicon-science-cs", tool.REPO) is False
    assert tool.could_declare_here("", tool.REPO) is False, (
        "a rant naming no project belongs to no task, so no repo can declare it"
    )
    assert tool.could_declare_here("emrg-other", tool.REPO) is False, (
        "a prefix is not the project: `emrg-other` is another ledger row's name"
    )

    stamp = "2026-10-02T07:48:41.525323+08:00"
    links = _links(
        monkeypatch, tool,
        [{"timestamp": stamp, "status": "pending", "message": "ours",
          "project": tool.REPO}],
        closed_issues=[(1807, f"Origin: rant {stamp}\n\nbody")],
    )

    (rant,) = tool.open_rant_rows(None, tool.REPO)

    assert [call[1] for call in links.queue_calls] == ["open", "closed"], links.queue_calls
    assert rant.closed_issues == [1807]


def test_a_rant_whose_issue_was_closed_says_so_instead_of_no_issue_yet(monkeypatch):
    """The measured case, 2026-10-02: the release rant rendered `no issue yet` while its
    issue #1807 existed and had been closed when its own PR #1808 merged."""
    tool = _fresh_tool()
    stamp = "2026-10-02T07:48:41.525323+08:00"
    _links(
        monkeypatch,
        tool,
        [_row(stamp)],
        closed_issues=[(1807, f"Origin: rant {stamp}\n\nbody")],
    )

    (rant,) = tool.open_rant_rows(None, tool.REPO)

    assert rant.issues == []
    assert rant.closed_issues == [1807]
    out = tool.render_rant(rant)
    assert f"rant {stamp}  pending  #1807 closed" in out, out
    assert "no issue yet" not in out, out


def test_an_open_declaring_issue_spends_no_closed_reading(monkeypatch):
    """The second call is a bound, not a habit: a rant the open reading places costs one
    request, exactly as it did before this reading existed."""
    tool = _fresh_tool()
    stamp = "2026-09-30T09:17:54+08:00"
    links = _links(
        monkeypatch,
        tool,
        [_row(stamp)],
        issues=[(1771, f"Origin: rant {stamp}\n\nbody")],
        closed_issues=[(1772, f"Origin: rant {stamp}\n\nbody")],
    )

    (rant,) = tool.open_rant_rows(None, tool.REPO)

    assert rant.issues == [1771]
    assert rant.closed_issues == []
    assert links.queue_calls == [(tool.REPO, "open", None)], links.queue_calls
    assert "#1771" in tool.render_rant(rant)
    assert "closed" not in tool.render_rant(rant)


def test_neither_state_is_still_no_issue_yet(monkeypatch):
    """The words keep their meaning: they are printed only when both readings found
    nothing, so a cycle reading them is being told to file one."""
    tool = _fresh_tool()
    _links(
        monkeypatch,
        tool,
        [_row("2026-09-30T09:35:04+08:00")],
        issues=[(1771, "Origin: rant 2026-09-30T09:17:54+08:00\n\nbody")],
        closed_issues=[(1772, "no origin line here")],
    )

    (rant,) = tool.open_rant_rows(None, tool.REPO)

    assert (rant.issues, rant.closed_issues) == ([], [])
    assert "  no issue yet  " in tool.render_rant(rant)


def test_a_closed_pull_request_is_not_read_as_a_declaring_issue(monkeypatch):
    """`Origin:` is an issue's line, and the issues endpoint returns PRs among them."""
    tool = _fresh_tool()
    stamp = "2026-09-30T09:17:54+08:00"
    links = _links(monkeypatch, tool, [_row(stamp)])
    links.closed_issues = [
        {
            "number": 1773,
            "body": f"Origin: rant {stamp}\n\nbody",
            "pull_request": {"url": "https://api.github.com/repos/x/y/pulls/1773"},
        }
    ]

    (rant,) = tool.open_rant_rows(None, tool.REPO)

    assert rant.closed_issues == []
    assert "no issue yet" in tool.render_rant(rant)
