"""Tests for scripts/check-merge-freshness.py - is a PR's CI verdict still current?

Background (cycle cyc20260911-083721)
-------------------------------------
On 2026-09-11 PR #1137 was `MERGEABLE/CLEAN` with both CI jobs green, and the
merge still produced a tree that failed two guards. The cause is worth pinning in
tests because the failure is silent and the obvious signals are all green:

* GitHub builds `Merge <head> into <merge-base>` for `pull_request`, so the verdict
  is about the head merged onto the *branch point*. When master moves, the branch
  point is no longer master, and the green run describes a tree that cannot be
  merged - yet it stays green, because master moving is not a branch push and
  fires no `synchronize` event.
* `gh pr view --json mergeable` says `CLEAN` throughout, because it answers "does
  this textually merge", which is the property that failed.

Both states are pinned here, never inferred from the failure case alone (#455):

* **fresh** - the head contains master's tip and has a passing run for that exact
  SHA; rc 0, and the tool is safe to gate a merge on.
* **stale** - four distinguishable ways (diverged, no run at all, still running,
  concluded non-success), each with its own reason, so a caller can tell "rebase
  it" apart from "fix it".

The fourth case is the one a naive implementation gets wrong: a head that contains
master but has *zero* CI runs is `no checks reported`, not a pass. Ancestry alone
is not enough to call a verdict current, so it is tested as its own state.

Nothing here touches the network: `_gh_json` is replaced, and the replacement is
asserted to receive the arguments the real helper would (so a test cannot pass by
never calling it).

Per-state remedies (cycle `cyc20260914-010711`)
----------------------------------------------
The remedy this tool prints used to be one sentence for every stale verdict:
"re-merge master into each stale branch, then let CI run". Measured on the live
queue that day (`#1197` 2 valid votes, `#1198` 1, `#1199`/`#1200` none, master
`abe6f8b`) the sentence was a way to *lose* work: a refresh moves the head, and
`check-vote-count.py` voids every vote predating a head push, so following it
would have paid three cycles of review for a freshness the landing tree can be
measured for free. Both directions are pinned here, because a remedy that is too
cautious is as wrong as one that is too eager:

* **with votes** - named, priced, and pointed at the landing-tree measurement;
* **without votes** - the refresh is the cheap way to a current verdict and is
  recommended unchanged;
* **unreadable count** - said so, never rendered as `0`, which is the line that
  means "refresh freely";
* **not ancestry-shaped** (no run / still running / failing) - the remedy that
  fits that state, since a rebase does not answer any of them, and no vote query
  is spent on a state whose remedy does not depend on the count.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-merge-freshness.py"

HEAD = "a" * 40
OTHER = "b" * 40
BASE = "c" * 40


def _load_module():
    spec = importlib.util.spec_from_file_location("check_merge_freshness", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the module declares a dataclass, and dataclasses
    # resolves annotations through sys.modules[cls.__module__] at class-creation
    # time. A module that is not registered there raises AttributeError inside
    # dataclasses itself - an error that names neither this test nor the cause.
    import sys

    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


class FakeGh:
    """A stand-in for `_gh_json` that answers from a routing table.

    Records every call so a test can assert the query was actually made - a test
    whose fake is never called would pass while the code under test queried
    nothing at all.
    """

    def __init__(self, pr_view: dict, compare: dict, runs: list[dict] | None):
        self.pr_view = pr_view
        self.compare = compare
        self.runs = runs
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> object:
        self.calls.append(list(args))
        # Every gh invocation must name its program at the call site (the helper
        # prepends it); assert the shape rather than trusting it.
        assert args, "gh was called with no arguments"
        if args[:2] == ["pr", "view"]:
            return self.pr_view
        if args[0] == "api":
            if any("actions/runs" in a for a in args):
                return {"runs": self.runs if self.runs is not None else []}
            assert any(a.startswith("repos/") and "/compare/" in a for a in args), args
            return self.compare
        raise AssertionError(f"unexpected gh call: {args}")


def _compare(status: str, ahead: int, behind: int, base: str = BASE) -> dict:
    return {"status": status, "ahead_by": ahead, "behind_by": behind, "merge_base": base}


def _view(sha: str = HEAD, branch: str = "feature/x") -> dict:
    return {"number": 1, "title": "t", "headRefOid": sha, "headRefName": branch}


def _run_(sha: str = HEAD, conclusion: str = "success", at: str = "2026-09-11T00:00:00Z") -> dict:
    """A workflow run, as the actions/runs payload reports it.

    `name` is not decoration: the tool pins the verdict to the `Test` workflow, so
    a fixture without it is a run the tool is right to ignore.
    """
    return {"headSha": sha, "name": "Test", "conclusion": conclusion, "createdAt": at}


def _install(mod, monkeypatch, fake: FakeGh) -> None:
    monkeypatch.setattr(mod, "_gh_json", fake)


@pytest.fixture(autouse=True)
def _no_vote_network(mod, monkeypatch):
    """No test reaches GitHub through the vote sibling either.

    The price of a refresh is read from `check-vote-count.py`, whose entry points
    run real `gh` commands. A test that forgot to fix the count would then not
    only make a network call but *pass* with whatever the live queue happened to
    be that minute. Both of the sibling's gh entry points are poisoned here, so an
    unpatched read degrades to "unavailable" (which `_valid_votes` is built to
    survive) instead of quietly answering with real data.
    """
    module = mod.votes_counter()

    def boom(*args, **kwargs):
        raise AssertionError("the sibling vote counter must not be reached in tests")

    monkeypatch.setattr(module, "_gh_json", boom)
    monkeypatch.setattr(module, "_gh_json_paginated", boom)


def _votes(mod, monkeypatch, count: int | None, note: str = "") -> list[int]:
    """Fix the count seam `main` uses, and record which PRs it was asked about."""
    asked: list[int] = []

    def fake(pr: int) -> tuple[int | None, str]:
        asked.append(pr)
        return count, note

    monkeypatch.setattr(mod, "_valid_votes", fake)
    return asked


def _run(mod, monkeypatch, fake: FakeGh, argv: list[str] | None = None) -> int:
    _install(mod, monkeypatch, fake)
    return mod.main(argv if argv is not None else ["1"])


# --- fresh -----------------------------------------------------------------


def test_fresh_when_head_contains_master_and_has_a_passing_run(mod, monkeypatch, capsys):
    fake = FakeGh(_view(), _compare("ahead", 4, 0), [_run_()])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 0
    assert "FRESH" in out
    assert "STALE" not in out
    assert len(fake.calls) == 3, "expected exactly the three queries, not a short-circuit"


def test_fresh_when_master_has_not_moved_at_all(mod, monkeypatch, capsys):
    """`identical` means master's tip *is* the head - trivially current."""
    fake = FakeGh(_view(), _compare("identical", 0, 0), [_run_()])
    assert _run(mod, monkeypatch, fake) == 0
    assert "FRESH" in capsys.readouterr().out


# --- stale: the four distinguishable ways ----------------------------------


def test_stale_when_the_head_does_not_contain_master(mod, monkeypatch, capsys):
    """The #1137 case: the verdict was green but about an older master."""
    fake = FakeGh(
        _view(), _compare("diverged", 2, 1, base="cb651a4"), [_run_()]
    )
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1
    assert "STALE" in out
    assert "no longer be merged" in out
    # The reason must name the branch point, or a reader cannot tell which master
    # the verdict was actually about.
    assert "cb651a4" in out


def test_stale_when_there_is_no_ci_run_for_this_head(mod, monkeypatch, capsys):
    """Ancestry is not enough: `no checks reported` is not a pass.

    This is the state a dropped push event leaves behind, and it is the one an
    ancestry-only implementation would wrongly call fresh.
    """
    fake = FakeGh(_view(), _compare("ahead", 1, 0), [])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr()
    assert rc == 1
    assert "NO Test run" in out.out
    assert "no checks reported" in out.out


def test_stale_when_a_run_exists_only_for_a_different_sha(mod, monkeypatch, capsys):
    """Keyed on the SHA, not the branch.

    A branch pushed twice has runs for both heads; reading the older one as the
    current verdict is the same mistake one step smaller.
    """
    fake = FakeGh(_view(), _compare("ahead", 1, 0), [_run_(OTHER)])
    rc = _run(mod, monkeypatch, fake)
    assert rc == 1
    assert "NO Test run" in capsys.readouterr().out


def test_a_passing_run_from_another_workflow_is_not_the_verdict(mod, monkeypatch, capsys):
    """The claim is "the *tests* passed", so the workflow is part of the query.

    Today `test.yml` is the only workflow `pull_request` triggers, so accepting
    any passing run happens to give the right answer - which is exactly the kind
    of coincidence that stops being true silently, the first time a second
    workflow is added to a branch. Pinned on a fixture whose only difference from
    the fresh case is the workflow name.
    """
    other_workflow = _run_()
    other_workflow["name"] = "Build Release"
    fake = FakeGh(_view(), _compare("ahead", 4, 0), [other_workflow])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1
    assert "NO Test run" in out, out


def test_stale_and_distinguished_when_ci_is_still_running(mod, monkeypatch, capsys):
    fake = FakeGh(_view(), _compare("ahead", 1, 0), [_run_(conclusion="pending")])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1
    assert "still pending" in out


def test_a_failing_verdict_is_reported_as_failing_not_as_stale(mod, monkeypatch, capsys):
    """Re-running CI will not help, so the wording must not suggest a rebase does."""
    fake = FakeGh(_view(), _compare("ahead", 1, 0), [_run_(conclusion="failure")])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1
    assert "failing verdict, not a stale one" in out


def test_the_newest_run_for_the_head_wins(mod, monkeypatch, capsys):
    """Two runs on one SHA: the freshest conclusion decides, not the first seen."""
    fake = FakeGh(
        _view(),
        _compare("ahead", 1, 0),
        [
            _run_(conclusion="failure", at="2026-09-11T00:00:00Z"),
            _run_(at="2026-09-11T01:00:00Z"),
        ],
    )
    rc = _run(mod, monkeypatch, fake)
    assert rc == 0
    assert "FRESH" in capsys.readouterr().out


# --- the remedy is priced by the state, not printed blanket ----------------
#
# Every test here asserts on the prose the tool adds *after* the verdicts, which
# is where the action lives: the verdict says what is wrong, the remedy says what
# it costs, and the two were previously disconnected (a stale-with-votes PR was
# told to do the one thing that voids its votes).


def test_a_stale_branch_with_votes_is_told_what_a_refresh_would_cost(mod, monkeypatch, capsys):
    """The measured queue: stale *and* carrying review.

    #1197 sat at 2/3 and #1198 at 1/3 when the whole queue went stale. The advice
    they got - "re-merge master into each stale branch" - would have returned them
    to 0/3 to make CI's verdict current, which is not a trade the tool has any
    business recommending silently.
    """
    fake = FakeGh(_view(), _compare("diverged", 2, 1, base="cb651a4"), [_run_()])
    asked = _votes(mod, monkeypatch, 2)
    rc = _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert rc == 1
    assert asked == [1], "the price is read for the stale PR"
    assert "2 valid vote(s) at risk" in err
    assert "voids all 2" in err
    assert "check-merge-plan-suite.py 1" in err
    # The *review* is named as the vehicle, and the plain comment is explicitly the
    # one that carries no vote. Reviews are the only channel check-vote-count.py
    # reads, so a remedy that names the comment as the vehicle reads as "do not
    # vote here" - and a stale PR whose only route to the threshold is the
    # landing-tree vote would then never reach 3/3. Measured 2026-09-14
    # (cyc20260914-040021): #1200's 2nd vote was a review on a stale head and
    # counted; #1199/#1201 each merged on a 3rd vote cast the same way.
    #
    # The channel is necessary and not sufficient: two votes cast this way were
    # lost on 2026-09-16 (cyc20260916-020149) because `gh pr review --body-file`
    # returned 0 with no output while the body carried no cycle id, which
    # check-vote-count.py reads the voting cycle out of. Both halves are pinned
    # here - the helper that enforces it, and the format the helper needs - because
    # a remedy that names only the channel is the advice that lost them.
    assert "cast-vote.py 1 --body-file" in err
    assert "cycYYYYMMDD-HHMMSS" in err
    assert "prints nothing on success" in err
    assert "not a review" not in err
    assert "carries the reading but no vote" in err
    assert "Re-merge master into each stale branch" not in err


def test_a_stale_branch_with_no_votes_is_told_the_refresh_is_free(mod, monkeypatch, capsys):
    """The other direction: with nothing to void, the refresh IS the remedy.

    #1199/#1200 were stale at 0/3. Telling them to measure a landing tree by hand
    instead would be the cautious-but-wrong output - CI on the real merged tree is
    strictly better evidence, and it costs nothing here.
    """
    fake = FakeGh(_view(), _compare("diverged", 2, 1, base="cb651a4"), [_run_()])
    _votes(mod, monkeypatch, 0)
    rc = _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert rc == 1
    assert "0 valid votes - nothing to void" in err
    assert "Re-merge master into the branch" in err
    assert "at risk" not in err
    # The other direction of the channel rule: with no vote at risk there is
    # nothing to vote on, so the remedy line must name no channel at all. Asserted
    # on that line rather than on the whole output, and on the concept rather than
    # on a command string: naming a review here would offer a vote whose evidence
    # is a local reading as an alternative to CI on the real merged tree, which is
    # strictly better and free at 0 votes. (A mutant that said "or cast a review"
    # survived the `gh pr review` version of this assertion.)
    remedy = next(
        line for line in err.splitlines() if "#1:" in line and "nothing to void" in line
    )
    assert "review" not in remedy and "comment" not in remedy, remedy


def test_an_unreadable_vote_count_is_said_so_and_never_read_as_zero(mod, monkeypatch, capsys):
    """`0` is an answer, so a failed read must not produce it.

    Zero is the only count that licenses the destructive action, which makes it
    the one value this line must never invent. The failure cause is carried into
    the message so a reader can tell a gh outage from a permissions problem.
    """
    fake = FakeGh(_view(), _compare("diverged", 2, 1, base="cb651a4"), [_run_()])
    _votes(mod, monkeypatch, None, "RuntimeError: gh failed (rc=1)")
    rc = _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert rc == 1
    assert "vote count unavailable (RuntimeError: gh failed (rc=1))" in err
    assert "0 valid votes" not in err
    assert "Re-merge master into the branch" not in err


def test_a_missing_run_is_told_to_re_trigger_rather_than_refresh(mod, monkeypatch, capsys):
    """An unjudged head is a real state, and a rebase is the expensive way out.

    `gh pr checks` reports "no checks reported" after a dropped push event. A
    refresh fixes that too - but by moving the head, so the cheaper remedy that
    answers the same question on the same head is named instead. It is also the
    arm that pins the query discipline: a state whose remedy does not depend on
    the count must not spend a gh query asking for it.
    """
    fake = FakeGh(_view(), _compare("ahead", 1, 0), [])
    asked = _votes(mod, monkeypatch, 7)
    rc = _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert rc == 1
    assert asked == [], "no count is read for a state whose remedy ignores it"
    assert "re-trigger CI on the same head" in err
    assert "keeps the votes" in err
    assert "Re-merge master into the branch" not in err


def test_a_run_still_going_is_told_to_wait(mod, monkeypatch, capsys):
    fake = FakeGh(_view(), _compare("ahead", 1, 0), [_run_(conclusion="pending")])
    asked = _votes(mod, monkeypatch, 3)
    rc = _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert rc == 1
    assert asked == []
    assert "wait for the run" in err


def test_a_failing_run_is_told_to_fix_the_failure_not_to_refresh(mod, monkeypatch, capsys):
    """A red run is not an expired one, so no refresh-shaped advice may appear.

    The verdict reason already says this; the remedy has to agree with it, or the
    two halves of one output recommend opposite actions.
    """
    fake = FakeGh(_view(), _compare("ahead", 1, 0), [_run_(conclusion="failure")])
    asked = _votes(mod, monkeypatch, 3)
    rc = _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert rc == 1
    assert asked == []
    assert "fix the failure" in err
    assert "does not make a failing run pass" in err


def test_json_carries_the_kind_and_the_count(mod, monkeypatch, capsys):
    """A caller scripting the queue needs the remedy's inputs, not just the prose."""
    fake = FakeGh(_view(), _compare("diverged", 2, 1, base="cb651a4"), [_run_()])
    _votes(mod, monkeypatch, 3)
    rc = _run(mod, monkeypatch, fake, ["1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload[0]["stale_kind"] == "ancestry"
    assert payload[0]["valid_votes"] == 3


def test_a_fresh_verdict_carries_neither_a_kind_nor_a_count(mod, monkeypatch, capsys):
    """Fresh is not a fifth kind, and it must not pay for a remedy query: a fresh
    verdict has no remedy to price, and its query count staying at three is what
    keeps the healthy path exactly as cheap as it was before this existed."""
    fake = FakeGh(_view(), _compare("ahead", 4, 0), [_run_()])
    asked = _votes(mod, monkeypatch, 9)
    rc = _run(mod, monkeypatch, fake, ["1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload[0]["stale_kind"] == ""
    assert payload[0]["valid_votes"] is None
    assert asked == []
    assert len(fake.calls) == 3


def test_the_count_comes_from_the_sibling_that_owns_it(mod, monkeypatch):
    """The delegation, tested where it actually happens.

    A local re-reading of the vote rule would be a second answer to "how many
    votes does this PR have", and the count is now load-bearing for a destructive
    recommendation. `needed` is asserted too: the gate is three, and a sibling
    called with a made-up threshold would answer the wrong question.
    """
    seen: list[tuple[int, int]] = []

    class _Verdict:
        valid_count = 2

    def fake_check_pr(pr: int, needed: int):
        seen.append((pr, needed))
        return _Verdict()

    monkeypatch.setattr(mod.votes_counter(), "check_pr", fake_check_pr)
    assert mod._valid_votes(41) == (2, "")
    assert seen == [(41, 3)]


def test_a_broken_count_read_degrades_to_unavailable(mod, monkeypatch):
    """The seam's own failure path: the freshness answer survives a missing price."""
    def boom(pr: int, needed: int):
        raise RuntimeError("gh failed (rc=1): gh api repos/...")

    monkeypatch.setattr(mod.votes_counter(), "check_pr", boom)
    count, note = mod._valid_votes(41)
    assert count is None
    assert "gh failed" in note


def test_a_broken_count_read_reaches_main_as_unavailable_not_zero(mod, monkeypatch, capsys):
    """The same property one layer up, with the *real* reader in place.

    `test_an_unreadable_vote_count_is_said_so_and_never_read_as_zero` fixes the
    count seam, so it cannot see what the real reader returns when the sibling
    breaks - and that return value is exactly where a helpful-looking `0` would
    be written. Here the real `_valid_votes` runs against a broken sibling, and
    the assertions are on what the user is told.
    """
    fake = FakeGh(_view(), _compare("diverged", 2, 1, base="cb651a4"), [_run_()])

    def boom(pr: int, needed: int):
        raise RuntimeError("gh failed (rc=1): gh api repos/argszero/emrg/commits/...")

    monkeypatch.setattr(mod.votes_counter(), "check_pr", boom)
    rc = _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert rc == 1, "an unreadable price must not cost the answer to the real question"
    assert "vote count unavailable (RuntimeError: gh failed (rc=1)" in err
    assert "0 valid votes" not in err
    assert "Re-merge master into the branch" not in err


def test_the_sibling_is_loaded_once(mod, monkeypatch):
    """Cached, not reloaded per PR: this runs once per stale PR in a queue."""
    assert mod.votes_counter() is mod.votes_counter()


# --- fail loud, never guess ------------------------------------------------


def test_an_unrecognised_compare_status_is_refused_not_called_fresh(mod, monkeypatch, capsys):
    """A new GitHub status must not silently read as fresh.

    The freshness sets are named rather than written as `status == "ahead"` for
    exactly this case: anything unrecognised has to fail loud.
    """
    fake = FakeGh(_view(), _compare("some_new_status", 1, 0), [_run_()])
    rc = _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert rc == 2
    assert "unrecognised compare status" in err


def test_a_gh_failure_exits_2_with_the_reason(mod, monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("gh failed (rc=1): gh api repos/x/compare/...\nsome stderr")

    monkeypatch.setattr(mod, "_gh_json", boom)
    rc = mod.main(["1"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "gh failed" in err


def test_the_helper_invokes_the_gh_program_by_name(mod, monkeypatch):
    """`_gh_json` must prepend the program name itself.

    Measured 2026-09-11: a call site that passed `["pr", "view", ...]` to a helper
    which also omitted the program name ran the POSIX `pr` utility, whose failure
    message (`pr: cannot open view`) names neither gh nor the real mistake.
    """
    import subprocess as sp

    seen: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return _Done()

    monkeypatch.setattr(sp, "run", fake_run)
    mod._gh_json(["pr", "view", "1"])
    assert seen and seen[0][0] == "gh", seen
    assert seen[0][1:] == ["pr", "view", "1"]


def test_json_mode_is_machine_readable(mod, monkeypatch, capsys):
    fake = FakeGh(_view(), _compare("ahead", 4, 0), [_run_()])
    rc = _run(mod, monkeypatch, fake, ["1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload[0]["stale"] is False
    assert payload[0]["head"] == HEAD
    assert payload[0]["merge_base"] == BASE


def test_no_function_has_an_unused_parameter() -> None:
    """Every declared parameter must be read somewhere in its own body.

    Added because this script shipped one: `_latest_run_for_head(head, branch,
    number)` never read `number`, left over from a draft that used it to build the
    error message. A dead parameter is not cosmetic here - it tells the next reader
    the function needs the PR number to do its job, which is exactly the kind of
    false signal about a *merge-gate* helper that this repo treats as a defect.

    Parsed with `ast` rather than a regex, and checked against the whole function
    body including nested scopes, so a parameter read only inside a closure still
    counts as used. `self` is excluded (methods), as are names prefixed with `_`.
    """
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        declared = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
        if args.vararg:
            declared.append(args.vararg.arg)
        if args.kwarg:
            declared.append(args.kwarg.arg)
        named = set(declared) - {"self"}
        used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        for param in sorted(named):
            if param not in used:
                offenders.append(f"{node.name}({param})")
    assert not offenders, (
        "unused parameter(s) - each declares a dependency the body does not have: "
        f"{offenders}"
    )
