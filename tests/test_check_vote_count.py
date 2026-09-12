"""Tests for scripts/check-vote-count.py - are a PR's LGTM votes still valid?

Background (cycle cyc20260911-091230)
-------------------------------------
The merge rule is "3 consecutive ✅ LGTMs from different evolution cycles with no ❌
in between". Reading that off a comment history is misleading, and every recent
cycle re-derived it by hand: a rebase pushes a new head, which voids **every**
earlier vote, while the history still shows five or six "✅ LGTM" lines. Measured
2026-09-11: #1133/#1134/#1136/#1137 each displayed 4-6 LGTMs and each had **0**
valid votes before re-counting, then 2 after this cycle's votes.

The three rules that make the count non-obvious are each pinned below:

* a vote submitted before the head push is void;
* a ❌ resets the run (three ✅ then ❌ then ✅ is one vote);
* a repeat cycle inside the run is one vote, so a single cycle cannot carry a PR.

The four states of the vet. This is a gate, so both sides are pinned (#455): the
approving state must reach READY, and each of the four ways a vote fails to count
must keep it SHORT - never infer the rule from the failing case alone.

Nothing here touches the network: `_gh_json` is replaced, and the fake is asserted
to be called, so a test cannot pass by never querying.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-vote-count.py"

HEAD = "a" * 40
T0 = "2026-09-11T00:00:00Z"  # the head push time
BEFORE = "2026-09-10T00:00:00Z"  # any vote before it


def _load_module():
    spec = importlib.util.spec_from_file_location("check_vote_count", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the module declares dataclasses, and dataclasses
    # resolves through sys.modules[cls.__module__] at class-creation time.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


class FakeGh:
    """Routes gh calls to canned answers and records them."""

    def __init__(
        self,
        reviews: list[dict],
        push_time: str = T0,
        exact: bool = True,
        mergeable: str = "MERGEABLE",
        merge_state: str = "CLEAN",
    ):
        self.reviews = reviews
        self.push_time = push_time
        self.exact = exact
        self.mergeable = mergeable
        self.merge_state = merge_state
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> object:
        self.calls.append(list(args))
        assert args and args[0] in {"pr", "api", "run"}, args
        if args[:2] == ["pr", "view"]:
            return {
                "number": 1,
                "title": "t",
                "headRefOid": HEAD,
                "mergeable": self.mergeable,
                "mergeStateStatus": self.merge_state,
            }
        if args[0] == "api":
            joined = " ".join(args)
            if "actions/runs" in joined:
                return {"t": self.push_time if self.exact else ""}
            if "/commits/" in joined:
                return {"t": self.push_time}
        raise AssertionError(f"unexpected gh call: {args}")

    def paginated(self, args: list[str]) -> list:
        """The reviews endpoint, which the tool reads page by page."""
        self.calls.append(list(args))
        assert args[0] == "api" and "/reviews" in " ".join(args), args
        return self.reviews


def _review(at: str, body: str) -> dict:
    return {"at": at, "body": body}


def _approve(cycle: str, at: str) -> dict:
    return _review(at, f"\u2705 LGTM - cycle `{cycle}`")


def _veto(cycle: str, at: str) -> dict:
    return _review(at, f"\u274c Needs fix - cycle `{cycle}`")


def _run(mod, monkeypatch, fake: FakeGh, argv: list[str] | None = None) -> int:
    monkeypatch.setattr(mod, "_gh_json", fake)
    monkeypatch.setattr(mod, "_gh_json_paginated", fake.paginated)
    return mod.main(argv if argv is not None else ["1"])


# --- classification: the mark, however it is decorated ---------------------


def test_a_vote_that_merely_mentions_the_absence_of_a_veto_is_still_an_approval():
    """The bug the second version shipped with.

    Measured 2026-09-11 on #1134: the first version searched the first *line* for
    the veto mark and read a real approval as a veto, because the body says
    "(two prior ✅ at this head; **no ❌ at this head**)". That PR then reported 9
    usable votes where it had 11, silently discarding two - and the failure
    direction matters: an under-count looks like "not ready yet", which is a
    plausible-enough state that nobody investigates.

    Measured again over all 176 reviews on the 40 most recent PRs: 45 bodies carry
    both marks, and **every one of the 6 that approves while mentioning ❌ on its
    first line does so in this negated form**. So this shape is real and common,
    and it is pinned here rather than assumed.

    The *reason* it survives is no longer the negation list, though. A later
    version kept the line-wide veto scan first and only recognised the absence
    when the negation was one of a fixed set of words within four characters of
    the mark - so `(0 ❌ at this head)`, `(zero ❌)`, `(none ❌)` and `(no prior ❌)`
    all classified as vetoes, discarding real approvals. See
    `test_a_leading_mark_decides_the_line` below for how that was closed: no
    vocabulary list can enumerate every way to say "none".
    """
    from_check = _load_module()
    body = (
        "\u2705 **LGTM - third vote at this head**, from cycle `x` "
        "(two prior \u2705 at this head; **no \u274c at this head**).\n\nVerified from scratch:"
    )
    assert from_check._classify(body) == "approve"


def test_the_verdict_mark_is_read_after_markdown_decoration(mod):
    """The bug the third version fixed - a veto behind `**`, `-`, `>` or `#`.

    Measured 2026-09-11 (cycle cyc20260911-130120) against the version that read
    the body's first *character*: 0 of 176 real bodies use any of these shapes, so
    the defect was latent. Latent is not the same as harmless, and the reason is
    the direction it fails in - a veto that classifies as a comment is skipped
    wholesale by `check_pr`, so it never resets the run:

        ✅ (cycle X)  ✅ (cycle Y)  **❌ Needs fix**  ✅ (cycle Z)
                                ^ read as comment
        -> run = 3 -> READY 3/3, on reviews a ❌ had already answered.

    It was also asymmetric: "**✅ LGTM**" still reached approval through the LGTM
    fallback below, so only the veto side was ever wrong - which is why the fix
    has to cover both marks.
    """
    assert mod._classify("**\u274c Needs fix:** something") == "veto", "bold veto"
    assert mod._classify("- \u274c needs fix") == "veto", "bullet veto"
    assert mod._classify("> \u274c needs fix") == "veto", "quoted veto"
    assert mod._classify("## \u274c Needs fix") == "veto", "heading veto"
    assert mod._classify("1. \u274c needs fix") == "veto", "ordered-list veto"
    assert mod._classify("**\u2705 LGTM** - cycle `c`") == "approve", "bold approval"
    assert mod._classify("- \u2705 LGTM - cycle `c`") == "approve", "bullet approval"
    assert mod._classify("> \u2705 LGTM - cycle `c`") == "approve", "quoted approval"


def test_a_decorated_veto_resets_the_run_instead_of_being_skipped(mod, monkeypatch, capsys):
    """The consequence above, at the level that actually matters: the count.

    Two approvals then a decorated veto then a third approval is **one** vote.
    Reading the veto as a comment reports READY 3/3 and would merge on a review
    that asked for a fix - the one outcome this tool exists to prevent.
    """
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z"),
                   _approve("cyc20260911-020000", "2026-09-11T02:00:00Z"),
                   _review("2026-09-11T03:00:00Z",
                           "**\u274c Needs fix:** cycle `cyc20260911-030000`"),
                   _approve("cyc20260911-040000", "2026-09-11T04:00:00Z")])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "SHORT 1/3" in out
    assert "NO  " in out, "the veto must be listed as a veto, not skipped as a comment"


def test_a_negated_mark_is_not_a_statement_of_that_mark(mod):
    """"no ❌" is prose about the veto; "Not LGTM" is a refusal, not an approval."""
    assert mod._classify("\u2705 LGTM - cycle `c` (no \u274c at this head)") == "approve"
    assert mod._classify("Not LGTM - cycle `c`") == "veto", "a refusal must not count as a vote"
    assert mod._classify("no \u2705 from me yet, cycle `c`") == "comment"
    # The negation must not reach across a word: "not bad" is praise.
    assert mod._classify("Not bad, LGTM - cycle `c`") == "approve"


def test_a_leading_mark_decides_the_line(mod):
    """An approving body may *mention* the other mark, in any words it likes.

    The version that ran the line-wide veto scan *first* could only stay correct
    by recognising the absence-form, and it did so with a fixed word list bounded
    to four non-word characters before the mark. Measured 2026-09-11 against that
    version, all of these real approval shapes classified as **veto**:

        ✅ LGTM - cycle `c` (0 ❌ at this head)     -> veto
        ✅ LGTM - cycle `c` (zero ❌)              -> veto
        ✅ LGTM - cycle `c` (none ❌)              -> veto
        ✅ LGTM - cycle `c` (no prior ❌)          -> veto
        ✅ LGTM - cycle `c` (no      ❌)           -> veto   (5-space gap)

    "0 ❌" is zero vetoes - the strongest possible approval - and reading it as a
    veto resets the run and discards every approval before it. The list cannot be
    repaired by extending it: the ways to say "none" are an open set, and an
    approval only has to outrun the vocabulary once. So the leading mark decides,
    and the scan below is the fallback for verdicts written as prose.
    """
    assert mod._classify("\u2705 LGTM - cycle `c` (0 \u274c at this head)") == "approve"
    assert mod._classify("\u2705 LGTM - cycle `c` (zero \u274c)") == "approve"
    assert mod._classify("\u2705 LGTM - cycle `c` (none \u274c)") == "approve"
    assert mod._classify("\u2705 LGTM - cycle `c` (no prior \u274c)") == "approve"
    assert mod._classify("\u2705 LGTM - cycle `c` (no     \u274c)") == "approve"
    assert mod._classify("\u2705 LGTM - cycle `c` (not a single \u274c)") == "approve"
    # The fallback still catches a veto written as prose, and still lets it win
    # there: with no leading mark there is nothing to read the line through.
    assert mod._classify("Result: \u274c needs fix - cycle `c`") == "veto"
    assert mod._classify("Result: \u274c because \u2705 was premature") == "veto"
    # ...but a prose line that *claims* LGTM is an approval, and the mark on it is
    # a mention of the other mark - the same reasoning as the leading case, applied
    # to the shape prose takes. Each of these was read as a veto before:
    assert mod._classify("Results: no \u274c; LGTM - cycle `c`") == "approve"
    assert mod._classify("Results: no \u274c, LGTM - cycle `c`") == "approve"
    assert mod._classify("Summary: zero \u274c so LGTM from me") == "approve"
    assert mod._classify("Findings: no \u274c -> LGTM") == "approve"
    # The leading mark is read as a *mark*, not inferred from the word "LGTM":
    # 37 of the 173 measured bodies state their verdict with a bare ✅ and no
    # "LGTM" anywhere. Without the leading branches these fall through to the
    # substring test and stop counting as votes at all.
    assert mod._classify("\u2705 - third vote at this head, verified from scratch") == "approve"
    assert mod._classify("\u274c LGTM was premature - cycle `c`") == "veto"
    # A prose line that names the absence of a veto is not a veto either - the
    # scan below is negation-aware for the same reason.
    assert mod._classify("Results: no \u274c anywhere in this diff") == "comment"
    # and a refusal is still a veto, since it is a claim *against* LGTM:
    assert mod._classify("Not LGTM - cycle `c`") == "veto"
    assert mod._classify("I can't LGTM this") == "veto"


def test_an_approval_mentioning_a_veto_does_not_reset_the_run(mod, monkeypatch, capsys):
    """The regression above, at the level that decides a merge.

    Three approvals, the third written `✅ LGTM (0 ❌ at this head)`. Reported as a
    veto it resets the run to 1/3 and the PR looks unready; the reviews that
    approved it are all still there.
    """
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z"),
                   _approve("cyc20260911-020000", "2026-09-11T02:00:00Z"),
                   _review("2026-09-11T03:00:00Z",
                           "\u2705 LGTM - cycle `cyc20260911-030000` (0 \u274c at this head)")])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "READY 3/3" in out


def test_the_verdict_is_read_from_the_first_content_line(mod):
    assert mod._classify("\u2705 LGTM - cycle `c`") == "approve"
    assert mod._classify("\u274c Needs fix: something") == "veto"
    assert mod._classify("  \u2705 LGTM - cycle `c`") == "approve", "leading whitespace is common"
    assert mod._classify("Just a comment about the code") == "comment"
    assert mod._classify("") == "comment"
    assert mod._classify("\n\n   \n") == "comment", "whitespace-only body is not a vote"
    # A decoration-only first line is skipped: the verdict is on the line after it.
    assert mod._classify("---\n\u274c needs fix - cycle `c`") == "veto"
    assert mod._classify("---\n\u2705 LGTM - cycle `c`") == "approve"


def test_a_body_that_does_not_open_with_a_mark_but_claims_lgtm_counts(mod):
    """Human-written reviews in this repo sometimes omit the mark."""
    assert mod._classify("LGTM, verified locally.") == "approve"
    assert mod._classify("## Review\nThis needs work") == "comment"


# --- the mergeable clause: enough votes is not the same as mergeable -------


def _three_votes() -> list[dict]:
    return [
        _approve("cyc20260911-010000", "2026-09-11T01:00:00Z"),
        _approve("cyc20260911-020000", "2026-09-11T02:00:00Z"),
        _approve("cyc20260911-030000", "2026-09-11T03:00:00Z"),
    ]


def test_a_conflicting_pr_with_three_votes_is_blocked_not_ready(mod, monkeypatch, capsys):
    """The bug: six PRs printed READY 3/3 while CONFLICTING and unmergeable.

    Measured 2026-09-12 on the live repo - #1152/#1151/#1145/#1142/#1141/#1136 each
    reported `READY 3/3` with `mergeable=CONFLICTING`, `mergeStateStatus=DIRTY`.
    Every one of them was a merge `gh pr merge` refuses, so the queue read as six PRs
    waiting on a formality when it was a deadlock.

    Both halves are asserted: the verdict must not be READY, and the *count* must
    still be reported. Suppressing the count would hide the review work that was
    actually done, and this is a tool whose whole point is an honest count.
    """
    fake = FakeGh(_three_votes(), mergeable="CONFLICTING", merge_state="DIRTY")
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1, "a PR that cannot be merged must not exit 0"
    assert "READY" not in out, "CONFLICTING must never render as READY"
    assert "BLOCKED 3/3" in out
    assert "merge state: CONFLICTING/DIRTY" in out


def test_the_blocked_reason_names_the_conflict_and_not_more_review(mod, monkeypatch, capsys):
    """The cure differs from SHORT's, so the message must not be the SHORT one.

    A reviewer told "not enough votes" goes and reviews; on a conflicting branch that
    work is thrown away by the rebase that resolving the conflict requires, which
    pushes a new head and voids every vote. The stderr line has to send them to the
    conflict instead.
    """
    fake = FakeGh(_three_votes(), mergeable="CONFLICTING", merge_state="DIRTY")
    _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert "cannot merge the text" in err
    assert "Not enough votes yet" not in err, (
        "three votes are present, so the shortfall message would be false"
    )


def test_a_pr_that_is_both_short_and_conflicting_is_reported_as_blocked(mod, monkeypatch, capsys):
    """Blocked wins the headline, and the output says why the votes do not matter yet.

    Reporting `SHORT 1/3` here would be the original bug one layer down: it names the
    wrong cure. The conflict has to be resolved first, and doing that voids the vote
    anyway - so the shortfall is real but not the thing to act on.
    """
    fake = FakeGh(
        [_approve("cyc20260911-010000", "2026-09-11T01:00:00Z")],
        mergeable="CONFLICTING",
        merge_state="DIRTY",
    )
    rc = _run(mod, monkeypatch, fake)
    # One read for both streams: `readouterr()` drains, so a second call returns "".
    captured = capsys.readouterr()
    assert rc == 1
    assert "BLOCKED 1/3" in captured.out, "the conflict is the blocking fact, not the vote deficit"
    assert "voids them" in captured.err


def test_a_mergeable_pr_with_three_votes_is_ready(mod, monkeypatch, capsys):
    """The approving direction of the new clause (#455: pin both states).

    Without this, a predicate that blocked *everything* would pass every other test
    in this section - the failure would be in the safe direction, and safe-direction
    failures are exactly the ones nobody investigates.
    """
    fake = FakeGh(_three_votes(), mergeable="MERGEABLE", merge_state="CLEAN")
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 0
    assert "READY 3/3" in out
    assert "merge state: MERGEABLE/CLEAN" in out


def test_a_mergeable_pr_with_too_few_votes_is_still_short(mod, monkeypatch, capsys):
    """The mergeable clause can only downgrade; it must not upgrade a vote deficit."""
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z")],
                  mergeable="MERGEABLE", merge_state="CLEAN")
    rc = _run(mod, monkeypatch, fake)
    assert rc == 1
    assert "SHORT 1/3" in capsys.readouterr().out


def test_an_uncomputed_mergeability_fails_loud(mod, monkeypatch, capsys):
    """`UNKNOWN` is a question not yet answered, not an answer of "mergeable".

    GitHub computes mergeability lazily, so a head pushed seconds ago reports
    UNKNOWN. Reading that as permission would be the same class of mistake as
    defaulting a missing field to "no conflict" - and the missing-field case is
    already pinned below. Asserts the count is *not* printed: a verdict that was not
    computed must not be shipped alongside a number that looks like one.
    """
    fake = FakeGh(_three_votes(), mergeable="UNKNOWN", merge_state="UNKNOWN")
    rc = _run(mod, monkeypatch, fake)
    assert rc == 2
    err = capsys.readouterr().err
    assert "not a computed mergeability" in err
    assert "3/3" not in err


def test_an_unrecognised_mergeability_value_fails_loud(mod, monkeypatch, capsys):
    """A value this version does not know must not fall through to "not blocking".

    Same reasoning as the `_FRESH_STATUSES` naming in check-merge-freshness.py: a new
    GitHub state silently read as permission is a gate that rots without changing.
    """
    fake = FakeGh(_three_votes(), mergeable="SOMETHING_NEW", merge_state="CLEAN")
    assert _run(mod, monkeypatch, fake) == 2
    assert "not a computed mergeability" in capsys.readouterr().err


def test_a_payload_without_mergeability_fails_loud(mod, monkeypatch, capsys):
    """An absent field must fail loud, for the same reason the `at` check exists.

    A dropped projection would leave `mergeable` empty, and empty is not CONFLICTING -
    so a defaulting implementation would report READY for a PR nobody checked. That is
    the exact shape of the `--jq` bug this file already hit once.
    """
    class NoMergeFields(FakeGh):
        def __call__(self, args):
            payload = super().__call__(args)
            if isinstance(payload, dict) and "mergeable" in payload:
                payload = dict(payload)
                payload.pop("mergeable")
                payload.pop("mergeStateStatus")
            return payload

    fake = NoMergeFields(_three_votes())
    rc = _run(mod, monkeypatch, fake)
    assert rc == 2
    assert "no mergeability" in capsys.readouterr().err


def test_json_mode_carries_the_merge_state_and_the_verdict(mod, monkeypatch, capsys):
    """Machine callers need both facts separately: the count is still the count."""
    fake = FakeGh(_three_votes(), mergeable="CONFLICTING", merge_state="DIRTY")
    rc = _run(mod, monkeypatch, fake, ["1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload[0]["valid_votes"] == 3
    assert payload[0]["enough_votes"] is True, "the votes are enough; the merge is not"
    assert payload[0]["mergeable"] == "CONFLICTING"
    assert payload[0]["merge_state"] == "DIRTY"
    assert payload[0]["verdict"] == "BLOCKED"
    assert payload[0]["ready"] is False


def test_the_mergeable_clause_never_raises_the_exit_code_above_one(mod, monkeypatch, capsys):
    """Blocked is a finding (exit 1), not a could-not-check (exit 2).

    The distinction matters because exit 2 means "ask again"; a conflict is a fact
    about the branch, and reporting it as a transient error would have a caller
    retry forever instead of resolving it. Pinned separately from the verdict test
    because conflating the two exit codes is how a CI gate becomes a no-op.
    """
    for mergeable, state in [("CONFLICTING", "DIRTY"), ("CONFLICTING", "BEHIND")]:
        fake = FakeGh(_three_votes(), mergeable=mergeable, merge_state=state)
        assert _run(mod, monkeypatch, fake) == 1, (mergeable, state)


# --- the gate is TWO fields, and the second one was ignored -----------------
#
# `MERGEABLE` alone is not the gate's spelling: evolution_prompt Step 5 requires
# `MERGEABLE`/`CLEAN`. An earlier version of this tool read only `mergeable` and
# documented the omission as deliberate ("`mergeStateStatus` is a finer-grained view
# of the same fact ... never branched on"). Measured (cyc20260912-190602): with
# three valid votes it printed `READY` and exited 0 for all four short cases below -
# including `DRAFT`, which cannot be merged by anyone, and `UNSTABLE`, which *is*
# the CI conjunct this tool's docstring says it leaves to a sibling. The existing
# tests could not catch it: every one of them used `merge_state="CLEAN"` or
# `"DIRTY"`, i.e. only the two states the code happened to branch on.


def test_every_non_clean_merge_state_blocks_with_enough_votes(mod, monkeypatch, capsys):
    """One case per state, because the bug was an unhandled *state*, not a state.

    A single test asserting that one non-clean state blocks would have passed on the
    old code (it handled `DIRTY`) while the other four stayed broken - which is
    exactly how this shipped.
    """
    for state, why in [
        ("DIRTY", "conflicts"),
        ("UNSTABLE", "checks are failing"),
        ("BEHIND", "behind the base"),
        ("BLOCKED", "protected"),
        ("DRAFT", "a draft"),
    ]:
        fake = FakeGh(_three_votes(), mergeable="MERGEABLE", merge_state=state)
        rc = _run(mod, monkeypatch, fake)
        captured = capsys.readouterr()
        assert rc == 1, f"MERGEABLE/{state} must block, not exit 0"
        assert "READY" not in captured.out, f"MERGEABLE/{state} rendered READY"
        assert "BLOCKED" in captured.out, f"MERGEABLE/{state} should read BLOCKED"
        assert state in captured.err, f"the reason must name {state}"


def test_a_draft_pull_request_is_never_reported_as_ready(mod, monkeypatch, capsys):
    """The clearest case, pinned on its own so it cannot be lost in a loop.

    `DRAFT` is not a mergeability question at all - no reviewer vote can merge a
    draft. Reporting it `READY` is not a near-miss reading; it is a false statement
    about a PR that cannot land.
    """
    fake = FakeGh(_three_votes(), mergeable="MERGEABLE", merge_state="DRAFT")
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1
    assert "READY" not in out
    assert "DRAFT" in out


def test_unstable_is_named_as_the_ci_conjunct(mod, monkeypatch, capsys):
    """`UNSTABLE` is "checks failing or unfinished" - i.e. CI is not green.

    The tool's own docstring says the CI conjunct is a sibling's question; that is
    true of *whether the verdict is stale*, but not of *whether checks pass*, and
    GitHub already answers the latter here. So this is pinned as a blocked state,
    with the reason saying checks, not "conflict" - the old single-reason message
    would have told the reader to resolve a conflict that does not exist.
    """
    fake = FakeGh(_three_votes(), mergeable="MERGEABLE", merge_state="UNSTABLE")
    _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert "checks" in err
    assert "conflict" not in err.lower(), (
        "UNSTABLE is a CI problem; telling the reader to resolve a conflict sends "
        "them after something that is not there"
    )


def test_an_unknown_merge_state_fails_loud_rather_than_passing(mod, monkeypatch, capsys):
    """A state GitHub adds later must not be read as permission.

    This is the rot-resistance the old "never branch on it" comment was reaching
    for. Enumerating the *known* states and rejecting the rest buys the same
    property without also passing every state the enumeration covers, which is what
    the ignore-the-field version got wrong.
    """
    fake = FakeGh(_three_votes(), mergeable="MERGEABLE", merge_state="HAS_HOOKS")
    rc = _run(mod, monkeypatch, fake)
    assert rc == 2, "an unrecognised merge state is a could-not-check, not a pass"
    captured = capsys.readouterr()
    assert "not a state this check knows" in captured.err
    # Assert on the verdict stream, not on the error prose: the refusal message
    # itself says "reporting READY from it would be ..." - a substring check on
    # stderr would fail on the very sentence whose absence it means to prove.
    assert "READY" not in captured.out


def test_the_mergeable_query_is_asked_of_the_pr_it_reports_on(mod):
    """The states must come from the same `gh pr view` as the head they describe.

    A second call keyed on a different PR (or a list endpoint's cached state) would
    let the merge state describe a different PR than the votes - the two facts would
    be about different things, with nothing in the output to show it.
    """
    fake = FakeGh(_three_votes())
    mod._gh_json = fake
    view = fake(["pr", "view", "1", "-R", mod.REPO, "--json",
                 "number,title,headRefOid,mergeable,mergeStateStatus"])
    mergeable, state = mod._merge_state(view)
    assert (mergeable, state) == ("MERGEABLE", "CLEAN")


def test_the_pr_view_actually_requests_the_merge_fields(mod, monkeypatch, capsys):
    """A fixture that answers from a keyed dict cannot catch a missing field.

    `FakeGh.__call__` returns its canned payload whatever fields were asked for, so
    the tests above would pass even if the real `--json` list never asked for
    mergeability - the tool would then fail loud on every real PR while the suite
    stayed green. So the requested field list itself is asserted.
    """
    seen: list[list[str]] = []

    class Recording(FakeGh):
        def __call__(self, args):
            if args[:2] == ["pr", "view"]:
                seen.append(list(args))
            return super().__call__(args)

    fake = Recording(_three_votes())
    assert _run(mod, monkeypatch, fake) == 0
    assert seen, "the tool must ask gh for the PR"
    fields = " ".join(seen[0])
    assert "mergeable" in fields and "mergeStateStatus" in fields, seen[0]


# --- the run rule ----------------------------------------------------------


def test_three_consecutive_votes_from_distinct_cycles_are_ready(mod, monkeypatch, capsys):
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z"),
                   _approve("cyc20260911-020000", "2026-09-11T02:00:00Z"),
                   _approve("cyc20260911-030000", "2026-09-11T03:00:00Z")])
    assert _run(mod, monkeypatch, fake) == 0
    out = capsys.readouterr().out
    assert "READY 3/3" in out
    assert len(fake.calls) == 3, fake.calls


def test_a_vote_before_the_head_push_is_void(mod, monkeypatch, capsys):
    """The rebase rule - the one that makes a 6-LGTM PR have zero votes."""
    fake = FakeGh([_approve("cyc20260911-010000", BEFORE),
                   _approve("cyc20260911-020000", BEFORE),
                   _approve("cyc20260911-030000", "2026-09-11T01:00:00Z")])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1
    assert "SHORT 1/3" in out
    assert out.count("VOID") == 2
    assert out.count("- submitted before the head push") == 2


def test_a_voided_approval_is_not_labelled_ok(mod, monkeypatch, capsys):
    """A ✅ that predates the head is still a ✅ - and it still does not count.

    Measured 2026-09-11 on #1133: four lines rendered as "OK  ... VOID: ...",
    the kind column and the validity column disagreeing in one row. The mark now
    answers the only question the reader has (does this vote count?), so the two
    can no longer contradict each other.
    """
    fake = FakeGh([_approve("cyc20260911-010000", BEFORE)])
    _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert "OK" not in out, out
    assert "VOID" in out


def test_a_veto_resets_the_run(mod, monkeypatch, capsys):
    """Three ✅ then ❌ then ✅ is one vote, not four."""
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z"),
                   _approve("cyc20260911-020000", "2026-09-11T02:00:00Z"),
                   _veto("cyc20260911-030000", "2026-09-11T03:00:00Z"),
                   _approve("cyc20260911-040000", "2026-09-11T04:00:00Z")])
    rc = _run(mod, monkeypatch, fake)
    assert rc == 1
    assert "SHORT 1/3" in capsys.readouterr().out


def test_a_repeat_cycle_inside_the_run_counts_once(mod, monkeypatch, capsys):
    """A cycle cannot carry a PR to the threshold by voting repeatedly."""
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z"),
                   _approve("cyc20260911-010000", "2026-09-11T02:00:00Z"),
                   _approve("cyc20260911-010000", "2026-09-11T03:00:00Z")])
    assert _run(mod, monkeypatch, fake) == 1
    assert "SHORT 1/3" in capsys.readouterr().out


def test_a_cycle_that_voted_before_a_veto_counts_again_after_it(mod, monkeypatch, capsys):
    """The veto resets the run, so the same cycle may vote again in the new run.

    This is the mirror of the repeat rule: distinctness is per-run, not per-PR.
    """
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z"),
                   _veto("cyc20260911-020000", "2026-09-11T02:00:00Z"),
                   _approve("cyc20260911-010000", "2026-09-11T03:00:00Z"),
                   _approve("cyc20260911-030000", "2026-09-11T04:00:00Z"),
                   _approve("cyc20260911-040000", "2026-09-11T05:00:00Z")])
    assert _run(mod, monkeypatch, fake) == 0
    assert "READY 3/3" in capsys.readouterr().out


def test_a_vote_without_a_cycle_id_is_reported_not_counted(mod, monkeypatch, capsys):
    """Distinctness cannot be shown, so the vote is void rather than counted."""
    fake = FakeGh([_review("2026-09-11T01:00:00Z", "\u2705 LGTM, looks good"),
                   _approve("cyc20260911-020000", "2026-09-11T02:00:00Z"),
                   _approve("cyc20260911-030000", "2026-09-11T03:00:00Z")])
    rc = _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert rc == 1
    assert "SHORT 2/3" in out
    assert "no cycle id" in out


# --- honest bounds ---------------------------------------------------------


def test_the_push_time_fallback_is_disclosed_not_silently_used(mod, monkeypatch, capsys):
    """With no CI run, the commit date stands in - and the output must say so.

    A commit date can precede the push, so the fallback is the optimistic
    direction: it can let a vote count that should not. It is also the case where
    the PR has no CI run at all, and that is the third conjunct unverified, so the
    same input now *blocks* as well as being disclosed. Both are asserted here:
    the disclosure is still what tells a reader the count is approximate, and the
    block is what keeps `READY` off a head nothing ever ran on.
    """
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z")], exact=False)
    _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert "push time approximated by commit date" in out
    assert "BLOCKED" in out


def test_a_head_with_no_ci_run_is_blocked_even_with_three_votes(mod, monkeypatch, capsys):
    """The regression this pins: `CLEAN` does not mean checks ran.

    `MergeStateStatus` counts *required* checks, and this repo has no branch
    protection and no rulesets, so a head with zero check runs reports `CLEAN` -
    the same value a double-green head reports. Measured 2026-09-12 on three
    historical PRs (heads c0860a35 / af2e0efd / 5358d294: zero workflow runs each,
    all three `MERGEABLE`/`CLEAN`). An earlier version read the merge state alone
    and therefore printed READY, exit 0, for a head no CI had ever judged.
    """
    fake = FakeGh(
        [_approve(f"cyc2026091{i}-010000", f"2026-09-1{i}T01:00:00Z") for i in (1, 2, 3)],
        exact=False,
        mergeable="MERGEABLE",
        merge_state="CLEAN",
    )
    rc = _run(mod, monkeypatch, fake)
    assert rc == 1, "a head with no CI run must not exit 0"
    out = capsys.readouterr().out
    assert "BLOCKED" in out
    assert "READY" not in out


def test_the_no_ci_block_names_the_missing_run(mod, monkeypatch, capsys):
    """The reason has to say what is missing, not just that something is.

    `mergeable`/`mergeStateStatus` are both clean here, so a reader who is told
    only "blocked" has nothing to act on - the state looks perfect.
    """
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z")], exact=False)
    _run(mod, monkeypatch, fake)
    err = capsys.readouterr().err
    assert "no CI run" in err


def test_a_head_with_a_ci_run_is_not_blocked_for_that_reason(mod, monkeypatch, capsys):
    """The other arm of the same predicate: with a run, nothing here blocks.

    Without this, a `blocked` that returned True unconditionally would pass every
    test above."""
    fake = FakeGh(
        [_approve(f"cyc2026091{i}-010000", f"2026-09-1{i}T01:00:00Z") for i in (1, 2, 3)],
        exact=True,
    )
    rc = _run(mod, monkeypatch, fake)
    assert rc == 0
    assert "READY" in capsys.readouterr().out


def test_json_mode_carries_the_ci_conjunct(mod, monkeypatch, capsys):
    """`ci_ran` is reported separately, so a caller does not have to infer it from
    the verdict (the same reason the merge fields are exposed)."""
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z")], exact=False)
    _run(mod, monkeypatch, fake, ["1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["ci_ran"] is False
    assert payload[0]["blocked"] is True
    assert payload[0]["ready"] is False


def test_an_exact_push_time_is_not_flagged(mod, monkeypatch, capsys):
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z")], exact=True)
    _run(mod, monkeypatch, fake)
    assert "approximated" not in capsys.readouterr().out


# --- machine output and failure modes --------------------------------------


def test_json_mode_reports_the_count_and_readiness(mod, monkeypatch, capsys):
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z")])
    rc = _run(mod, monkeypatch, fake, ["1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload[0]["valid_votes"] == 1
    assert payload[0]["ready"] is False
    assert payload[0]["push_time_exact"] is True


def test_min_votes_lowers_the_threshold(mod, monkeypatch, capsys):
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z"),
                   _approve("cyc20260911-020000", "2026-09-11T02:00:00Z")])
    assert _run(mod, monkeypatch, fake, ["1", "--min-votes", "2"]) == 0
    assert "READY 2/2" in capsys.readouterr().out


def test_a_gh_failure_exits_2_with_the_reason(mod, monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("gh failed (rc=1): gh api ...\nreason")

    monkeypatch.setattr(mod, "_gh_json", boom)
    assert mod.main(["1"]) == 2
    assert "gh failed" in capsys.readouterr().err


def test_the_helper_invokes_gh_by_name(mod, monkeypatch):
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


# --- discoverability -------------------------------------------------------


def test_agent_md_documents_the_canonical_invocation(mod) -> None:
    """The tool must be findable, in the runnable form.

    Same convention as the node-count tool's twin test: the Agent.md line is how
    a host (or the next cycle) learns the tool exists, and a bare name is not
    enough - it has to be the invocation that actually runs.
    """
    doc = (REPO_ROOT / "Agent.md").read_text(encoding="utf-8")
    assert mod.INVOCATION in doc, (
        "Agent.md must document the vote-count tool with the runnable form - that "
        "line is how a host learns the tool exists"
    )


def test_the_invocation_constant_matches_the_usage_block(mod) -> None:
    """`INVOCATION` is what the Agent.md guard searches for, so it must be runnable.

    Two traps, both measured on 2026-09-11 by mutating this file:

    * `INVOCATION in source` is **circular** - the constant is defined in that
      very file, so it passes whatever the documented command says.
    * `INVOCATION in docstring` is a **substring** test, and a shortened constant
      hides inside the real command: `"python3 scripts/check-vote-count.py"` is a
      substring of `"uv run --no-sync python3 scripts/check-vote-count.py"`, so a
      constant pointing at a bare `python3` - no uv, no `--no-sync` - passed.

    So the usage line must *begin* with the constant. That is the shape a reader
    copies, and a truncated constant cannot be a prefix of the full command.
    """
    docstring = mod.__doc__ or ""
    usage_lines = [ln.strip() for ln in docstring.splitlines() if ln.strip()]
    assert any(ln.startswith(mod.INVOCATION) for ln in usage_lines), (
        f"no usage line in the tool's docstring starts with {mod.INVOCATION!r} - "
        f"the constant and the documented command have drifted apart.\n"
        f"Usage lines seen: {[ln for ln in usage_lines if 'check-vote-count' in ln]}"
    )


# --- the list must be complete ---------------------------------------------


def test_reviews_are_read_page_by_page(mod, monkeypatch, capsys):
    """The reviews endpoint truncates at 30 and orders **oldest first**.

    So a busy PR loses its *newest* reviews - exactly the votes that count, since
    the rule is about votes cast after the head push. Measured 2026-09-11: #1134
    already had 7 reviews after one unblock, #1136 had been through three. It is
    a merge gate, so the count must not depend on how busy a PR has been.

    This is the same defect class pm25coder caught in the sibling freshness tool
    (#1138): querying a bounded window and treating it as the whole set.
    """
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z")])
    _run(mod, monkeypatch, fake)
    # The paginated helper is used for reviews; the plain one never sees them.
    assert any("/reviews" in " ".join(c) for c in fake.calls), fake.calls


def test_a_vote_beyond_the_first_page_still_counts(mod, monkeypatch, capsys):
    """The newest vote decides, and it is the one truncation would drop.

    `FakeGh.paginated` is what the tool receives, so a fixture of 40 reviews
    models what `--paginate` returns: all of them, not the first 30.
    """
    older = [_approve(f"cyc20260910-{i:06d}", "2026-09-10T10:00:00Z") for i in range(40)]
    newest = [
        _approve("cyc20260911-010000", "2026-09-11T01:00:00Z"),
        _approve("cyc20260911-020000", "2026-09-11T02:00:00Z"),
        _approve("cyc20260911-030000", "2026-09-11T03:00:00Z"),
    ]
    fake = FakeGh(older + newest)
    assert _run(mod, monkeypatch, fake) == 0
    assert "READY 3/3" in capsys.readouterr().out


def test_the_paginated_helper_passes_paginate_and_flattens_pages(mod, monkeypatch):
    """`--paginate` + `--jq '.[]'` yields one object per line; the helper parses
    every line. Concatenated page arrays are not valid JSON, so parsing the whole
    stdout as one document would be the bug this guards against."""
    import subprocess as sp

    seen: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = '{"at": "a", "body": "b"}\n{"at": "c", "body": "d"}\n'
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return _Done()

    monkeypatch.setattr(sp, "run", fake_run)
    out = mod._gh_json_paginated(["api", "repos/x/pulls/1/reviews", "--jq", "{at, body}"])
    assert out == [{"at": "a", "body": "b"}, {"at": "c", "body": "d"}]
    assert "--paginate" in seen[0], seen[0]
    assert seen[0][0] == "gh", seen[0]


def test_reviews_are_ordered_before_the_run_is_walked(mod, monkeypatch, capsys):
    """The run rule is positional, so the walk needs chronological order.

    The walk resets on a veto and counts a cycle once per run - both depend on
    reading the votes in time order. GitHub's reviews endpoint happens to return
    oldest-first, so concatenating pages preserves that today; that is a property
    of the *endpoint*, not of this code, and a `direction=desc` query would invert
    every verdict without changing a byte of the classifier. Sorted here so the
    rule does not silently depend on the server's ordering.

    The fixture is deliberately newest-first. Chronologically the veto sits
    between B and C, so the run after it is one vote; walked in the fixture's own
    order (A and B land *after* the veto) it would be two. `SHORT 1/3` versus
    `SHORT 2/3` is what makes this discriminating - both are short, so a test
    asserting only the verdict would pass either way.
    """
    fake = FakeGh([
        _approve("cyc20260911-040000", "2026-09-11T03:00:00Z"),
        _veto("cyc20260911-030000", "2026-09-11T02:00:00Z"),
        _approve("cyc20260911-020000", "2026-09-11T01:00:00Z"),
        _approve("cyc20260911-010000", "2026-09-11T00:30:00Z"),
    ])
    rc = _run(mod, monkeypatch, fake)
    assert rc == 1
    assert "SHORT 1/3" in capsys.readouterr().out


def test_a_lost_jq_projection_fails_loud_instead_of_voiding_every_vote(mod, monkeypatch, capsys):
    """The bug this shipped with, found by running it against the live PRs.

    `_gh_json_paginated` appended `--jq ".[]"` while the call site passed its own
    `--jq ".[] | {at, body}"`. gh honours the *last* `--jq`, so the projection was
    dropped, every review came back under its raw field names, and `at` read as
    `""`. Since `"" <= push_time` is true, **every** vote was voided: a PR with two
    valid votes printed `SHORT 0/3`.

    That is the worst shape of failure in a merge gate - not an exception, but a
    plausible-looking count in the safe direction, so a reviewer would simply wait
    for votes that already existed. The test fake cannot catch it (it returns
    dicts and never models the jq contract), which is why the check is a runtime
    assertion on the payload's shape rather than a unit-test expectation.
    """
    bad = [{"submitted_at": "2026-09-11T01:00:00Z", "body": "\u2705 LGTM - cycle `cyc20260911-010000`"}]
    fake = FakeGh(bad)
    rc = _run(mod, monkeypatch, fake)
    assert rc == 2, "a missing `at` field must be a could-not-check, not a zero count"
    err = capsys.readouterr().err
    assert "did not apply" in err
    assert "0/3" not in err
