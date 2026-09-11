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

    def __init__(self, reviews: list[dict], push_time: str = T0, exact: bool = True):
        self.reviews = reviews
        self.push_time = push_time
        self.exact = exact
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> object:
        self.calls.append(list(args))
        assert args and args[0] in {"pr", "api", "run"}, args
        if args[:2] == ["pr", "view"]:
            return {"number": 1, "title": "t", "headRefOid": HEAD}
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

    Measured again this cycle over all 176 reviews on the 40 most recent PRs: 45
    bodies carry both marks, and **every one of the 6 that approves while
    mentioning ❌ on its first line does so in this negated form**. So
    negation-awareness is what makes a line-wide veto test safe, and it is pinned
    here rather than assumed.
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


def test_a_veto_wins_when_both_marks_are_on_the_verdict_line(mod):
    """The rule the `_VETO_MARK` comment has always asserted, now actually held.

    "✅ but ❌ on the second point" is a request for changes. The version that read
    only the first character returned `approve` here - the comment described
    behaviour the code did not have.
    """
    assert mod._classify("\u2705 LGTM, but \u274c on the second point - cycle `c`") == "veto"


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
    direction: it can let a vote count that should not. It is flagged rather than
    silently trusted, which is also the case where the PR has no CI at all.
    """
    fake = FakeGh([_approve("cyc20260911-010000", "2026-09-11T01:00:00Z")], exact=False)
    _run(mod, monkeypatch, fake)
    out = capsys.readouterr().out
    assert "push time approximated by commit date" in out


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
