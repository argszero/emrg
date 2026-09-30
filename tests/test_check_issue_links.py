"""The issue/PR link reading: claims, mentions, and the states that are not `linked`.

Host 2026-09-26T18:52:57 stated the rule (`scripts/check-issue-links.py` quotes it
verbatim in its docstring): one issue is finished by exactly one PR, each names the
other, and a rejected or change-requested PR is updated in place. This file is the
evidence that the reading built for it answers *that* question.

The two mistakes the tool can make and still look right
-------------------------------------------------------
**Reading a mention as a claim.** The first version of the tool treated any PR
cross-reference as a claim, and its own pull request proved it wrong: a body that
*cites* #1553/#1598/#1606 as evidence made all three read as claimed by a PR that
finishes none of them. A claim is GitHub's closing keyword (the form the platform
itself auto-closes on), and `test_a_mention_is_not_a_claim` plus
`test_a_body_that_cites_issues_declares_none_of_them` are what hold that line.

**Transposing the two directions.** Both are read from GitHub, but not from the same
place: an issue's timeline records the PRs that referenced it, and a PR's timeline
records the issues that referenced it. Read the wrong map for a side and every one-sided
link becomes bidirectional — the report says `linked` everywhere and the tool is
worthless while looking healthy. `test_the_two_directions_are_read_from_opposite_sources_and_never_transposed`
pins that at the unit level, and three report-level tests ask the three shapes that a
confusion would flatten.

The fakes never touch the network: `_gh` is replaced, and the replacement is a routing
table that records every call, so a test whose fake was never called cannot pass while
the code under test queried nothing (the failure `tests/test_check_merge_freshness.py`
names for the same reason).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-issue-links.py"

REPO = "argszero/emrg"

#: A frozen clock, so an age assertion cannot drift with the wall clock.
NOW = "2026-09-26T10:00:00+00:00"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_issue_links", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _issue(
    number: int, title: str = "an issue", created: str = "2026-09-24T10:00:00Z",
    body: str = "",
) -> dict:
    return {"number": number, "title": title, "created_at": created, "body": body}


def _comment(text: str) -> dict:
    """One issue comment, as `/issues/N/comments` reports it.

    The claim normally lives here rather than in the issue body: every `ok` row on the
    live queue is a comment (`Handled by #1666` on #1665, `Taken by **#1655**` on #1654)
    and the remedy this tool prints is a comment too. A cross-reference event does **not**
    carry it — its source body is the issue's body (measured 2026-09-27 on
    `issues/1655/timeline`) — which is why `issue_text` fetches the comments and why the
    fixtures serve the two separately.
    """
    return {"body": text}


def _pr(
    number: int,
    title: str = "a pr",
    body: str = "",
    created: str = "2026-09-26T05:00:00Z",
) -> dict:
    """An open PR as `/issues` reports it — the discriminator is `pull_request`."""
    return {
        "number": number,
        "title": title,
        "body": body,
        "created_at": created,
        "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"},
    }


def _refers_to(
    number: int,
    *,
    is_pr: bool,
    state: str = "open",
    body: str = "",
    merged_at: str | None = None,
) -> dict:
    """A `cross-referenced` event whose source is `number`.

    `is_pr` is one half of the test: GitHub records whether the *referrer* was a PR by
    putting a `pull_request` object on the source. `body` is the other half — the
    referrer's own text, which is what decides claim vs mention for a PR source, and it
    is present on a real event (measured 2026-09-26), so the fixtures carry it rather
    than fetching it separately.

    `merged_at` is the third: it is what separates *landed* work from an abandoned
    attempt, and it is on the real event too (measured 2026-09-26 on
    `issues/1553/timeline`, where `#1565` carries `2026-09-24T05:24:54Z` and an issue
    source carries nothing). A merged PR is `state: closed` **with** this set — which is
    why the reading keys on it and not on the state, and why the fixture can express the
    two separately.

    For an **issue** source this `body` is the issue's *body* and never the comment that
    made the reference (measured 2026-09-27: `issues/1655/timeline` carries an event from
    #1654 whose source body is #1654's body, while the claim is a comment) — so an issue's
    claim is read from `issue_text`, not from here. The fixtures keep the two apart
    deliberately: passing the claim in this field would pin a shape the API does not send.
    """
    source: dict = {"number": number, "state": state, "body": body}
    if is_pr:
        pull: dict = {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}
        if merged_at:
            pull["merged_at"] = merged_at
        source["pull_request"] = pull
    return {"event": "cross-referenced", "source": {"issue": source}}


def _merged(number: int, *, body: str = "", merged_at: str = "2026-09-25T00:00:00Z") -> dict:
    """A merge that referenced the subject without declaring it — the shape #1644 names."""
    return _refers_to(number, is_pr=True, state="closed", body=body, merged_at=merged_at)


class FakeGh:
    """`_gh` replaced by a routing table; every call recorded.

    Three payloads are served, all as the JSON *text* `gh` would print, because the tool
    parses its own output: the open queue (one `/issues` call), a per-subject timeline,
    and a per-issue comment list. An unexpected query is an assertion failure rather than
    an empty answer, so a typo in a path cannot read as "no links" — and a comments call
    for an issue that is not in the queue is such a typo, since no claiming issue can be
    outside it.

    The shape served is the one measured on this machine (2026-09-26): with no `--jq`,
    `gh api --paginate` prints **one merged array on one line**. The other measured
    shape — a filtered call's one-object-per-line — has its own test.
    """

    def __init__(
        self,
        issues: list[dict],
        prs: list[dict],
        timelines: dict[int, list[dict]],
        comments: dict[int, list[dict]] | None = None,
    ):
        self.issues = issues
        self.prs = prs
        self.timelines = timelines
        self.comments = comments or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        assert args, "gh was called with no arguments"
        assert args[0] == "api", f"unexpected gh call: {args}"
        assert args[-1] == "--paginate", args
        target = next((a for a in args if a.startswith("repos/")), None)
        assert target, args
        if "/issues?" in target:
            return json.dumps([*self.issues, *self.prs])
        if "/comments?" in target:
            number = int(target.split("/issues/")[1].split("/")[0])
            known = {int(row["number"]) for row in self.issues}
            assert number in known, f"comments for an issue outside the queue: {args}"
            return json.dumps(self.comments.get(number, []))
        for number, events in self.timelines.items():
            if target.startswith(f"repos/{REPO}/issues/{number}/timeline?"):
                return json.dumps(events)
        raise AssertionError(f"unexpected gh query: {args}")

    @property
    def timeline_calls(self) -> list[int]:
        return [
            int(a.split("/issues/")[1].split("/")[0])
            for c in self.calls
            for a in c
            if "/issues/" in a and "/timeline?" in a
        ]

    @property
    def comments_calls(self) -> list[int]:
        return [
            int(a.split("/issues/")[1].split("/")[0])
            for c in self.calls
            for a in c
            if "/issues/" in a and "/comments?" in a
        ]


def _install(mod, monkeypatch, fake: FakeGh) -> None:
    monkeypatch.setattr(mod, "_gh", fake)


def _run(mod, capsys, argv: list[str] | None = None):
    rc = mod.main(argv or [])
    out = capsys.readouterr().out
    return rc, out


def _detail(out: str, subject: str) -> str:
    """The indented detail of one subject's block, and only that subject's.

    Asserting `"…" in out` over a whole report lets a *different* subject's row satisfy
    the assertion, and the first version of this file did exactly that: a mutation arm
    that made issue-to-issue references read as PR claims survived
    `test_an_issue_reference_is_not_counted_as_a_pr_claim`, because the sibling issue
    #30's own row carried the words `nothing has been opened for it` while #10's row said
    the opposite. The arm is the evidence, not the reasoning — so the detail assertions
    read the block they are about.
    """
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(subject):
            return lines[index + 1].strip()
    raise AssertionError(f"{subject} is not in the report:\n{out}")


def _linked_pair() -> FakeGh:
    """The positive case: PR 20 declares `Closes #10`, and issue 10 names it back.

    Issue 10's half is a **comment**, which is the shape the live queue carries and the
    reason the claim text is fetched at all: the cross-reference event on #20's timeline
    carries issue 10's *body* (here empty), while the claim sits in a comment — the shape
    `Handled by #1666` (#1665) and `Taken by **#1655**` (#1654) really have.
    """
    return FakeGh(
        [_issue(10, "the problem")],
        [_pr(20, "the fix", body="Closes #10.")],
        {
            10: [_refers_to(20, is_pr=True, body="Closes #10.")],
            20: [_refers_to(10, is_pr=False)],
        },
        comments={10: [_comment("Handled by #20 — the daemon half, updated in the PR.")]},
    )


# --------------------------------------------------------------------------- #
# The claim contract: a closing keyword, not a mention
# --------------------------------------------------------------------------- #


def test_declared_claims_reads_githubs_closing_keywords_only(mod) -> None:
    """Both halves of the contract: every keyword form, and everything that is not one.

    The negative half is the one that matters — it is the shape that made the first
    version of this tool report three unclaimed issues as claimed. `None` is included
    because GitHub reports a bodyless PR as JSON `null`, and a caller should not have to
    remember to coalesce that.
    """
    for body in (
        "Closes #1",
        "closes #1",
        "Closed #1.",
        "Fix #1",
        "fixes #1",
        "Fixed: #1",
        "Resolve #1",
        "resolves #1",
        "resolved #1",
        "This PR closes #1, because the reader needs it.",
    ):
        assert mod.declared_claims(body) == {1}, body
    assert mod.declared_claims("Closes: #7") == {7}

    # The list form: references follow the keyword separated by commas or spaces —
    # `Closes #1, #2`, `Closes #1, #2, #3`, `Closes #1 #2`.
    assert mod.declared_claims("Closes #1, #2") == {1, 2}
    assert mod.declared_claims("Closes #1, #2, #3 and #4") == {1, 2, 3}
    assert mod.declared_claims("Closes #1 #2") == {1, 2}
    assert mod.declared_claims("Fixes #1\nCloses #2") == {1, 2}

    # …and a conjunction ends it, so `#4` above is *not* read as a claim. This is the
    # conservative side on purpose: stopping early costs a false alarm (`UNCLAIMED` on a
    # PR that does claim it, which a reader sees and fixes by adding the keyword), while
    # running on costs a false link — a claim asserted where none exists, which is the
    # silent direction and the one this whole file exists to prevent.
    assert mod.declared_claims("Closes #1, #2 and #3") == {1, 2}
    assert mod.declared_claims("Closes #1 and #2") == {1}


def test_a_negated_keyword_is_not_a_claim(mod) -> None:
    """The one place the reading leaves a plain regex, and both of its edges.

    A body that writes `it is not closed: #1 has no handler` carries a keyword *and* a
    reference, so a regex-only reading claims #1 — a link that does not exist, reported
    silently. That sentence is not hypothetical for this tool: the prose that motivated
    it (a docstring explaining what a mention is not) is exactly that shape.

    The bound is asserted too, because a heuristic that quietly covers everything is a
    different tool: the negation must be in the keyword's own clause and within two
    words. A comma or a fuller clause ends the window, so the two `True` cases below are
    the documented limit — they read as claims, and a reader sees them in the report.
    """
    assert mod.declared_claims("it is not closed: #1 has no handler") == set()
    assert mod.declared_claims("This does not close #2.") == set()
    assert mod.declared_claims("never fixes #3") == set()
    assert mod.declared_claims("doesn't resolve #4") == set()

    # The window, stated: a new clause after the negation does not reach the keyword.
    assert mod.declared_claims("it is not this, but it closes #5") == {5}
    assert mod.declared_claims("does not touch #6, closes #7") == {7}

    for body in (
        "",
        None,
        "the #1 class, in the carrier that matters",  # a citation
        "See #1 for the measurement.",                # a pointer
        "closes the loop on #1",                       # a verb, not a keyword + ref
        "fixes up the #1 path",                        # ditto
        "it is not closed: #1 has no handler",         # a keyword in prose
    ):
        assert mod.declared_claims(body) == set(), body



def test_a_quoted_keyword_is_not_a_claim(mod) -> None:
    """A **quoted** closing keyword declares nothing — the second live self-caught defect.

    Not hypothetical: this reading's own pull request (#1643) explained the
    claim/mention distinction with the sentence *"of the two open PRs naming #1606,
    #1638's body ends `Closes #1606.`"*, and the parsed claim made issue **#1606 read
    `DUPLICATE`** — claimed by #1638 *and* by #1643, whose second claim existed only
    inside backticks. A tool that documents the syntax it reads quotes it, so this is the
    family's normal input rather than an edge.

    Three forms are pinned, because the masking has two mechanisms and the weaker one
    cannot cover the fenced case: an inline span, a fenced block (whose interior a
    line-bounded inline rule cannot reach), and an **unterminated** fence, which the
    fenced rule has to carry to the end of the body rather than leave open.

    This test lives apart from the negation and list-form cases for a reason that was
    measured: an arm disabling the fenced rule SURVIVED while these assertions sat inside
    another test's body — the arm named the test that holds the contract, and the
    contract was elsewhere. A mutation arm's node is part of the claim it makes.
    """
    assert mod.declared_claims("the body ends `Closes #1606.` while") == set()
    assert mod.declared_claims("example:\n```\nCloses #11.\n```\nand Closes #12.") == {12}
    assert mod.declared_claims("an unterminated fence:\n```\nCloses #13.") == set()
    assert mod.declared_claims("a ``double `Closes #14.` `` span") == set()
    # …and the masking is a shield, not a gag: a real declaration beside a quotation is
    # still read, which is the case every PR body in this repo with a quoted example has.
    assert mod.declared_claims("Closes #15.\n\n(the quote above is `Closes #16.`)") == {15}


def test_a_masked_claim_is_named_as_quoted_rather_than_counted(mod) -> None:
    """The mirror of the masking: the same keyword, read a second way, names itself.

    Masking is right — a quoted keyword is not a claim — but a report that says "this PR
    declares no issue" while the body visibly carries `Closes #N` sends its reader to look
    for a sentence that is already there. Measured 2026-09-29 on PR #1715: its Tracking
    section read ``"`Closes #1718`."`` in backticks, the row said the PR declares nothing,
    and the author's own sentence — that the tool read the pair `ok` — was the only wrong
    thing in a body that had made the declaration being asked for.

    The two readings are complements over every shape the masker knows, and that is what
    is pinned here rather than three examples: inline, fenced, unterminated fence and a
    double-backtick span all read as quoted; a bare mention reads as neither; a real
    declaration beside a quotation is still declared and not quoted.
    """
    assert mod.quoted_claims("The `Closes #1718` line sits in a code span.") == {1718}
    assert mod.quoted_claims("example:\n```\nCloses #11.\n```\n") == {11}
    assert mod.quoted_claims("an unterminated fence:\n```\nCloses #13.") == {13}
    assert mod.quoted_claims("a ``double `Closes #14.` `` span") == {14}
    # Neither reading claims a bare mention, and neither loses a real declaration.
    assert mod.quoted_claims("see #1718 for the evidence") == set()
    assert mod.quoted_claims("Closes #99.\n\n(the quote above is `Closes #16.`)") == {16}
    assert mod.declared_claims("Closes #99.\n\n(the quote above is `Closes #16.`)") == {99}
    # A body GitHub reports as null reads as nothing in both directions.
    assert mod.quoted_claims(None) == set()

    # The negation window has to survive the reading, and it is why this one blanks the
    # marks rather than keeping the text as written: a backtick is not a word, and left in
    # place it breaks the pattern's end anchor, so "does not `close #1718`" would read as a
    # quotation of a claim. It is a quotation of a *denial*, and neither reading counts it.
    assert mod.quoted_claims("This does not `close #1718`.") == set()
    assert mod.quoted_claims("never `closes #1718`") == set()
    assert mod.quoted_claims("prose\n```\nnever closes #1718\n```\n") == set()


def test_a_crlf_body_closes_its_fence_where_the_fence_closes(mod) -> None:
    """A CRLF body must not mask to its own end — the live defect of issue #1697.

    The closer is `$`-anchored, and under `re.M` that matches before `\\n`; in a CRLF
    body a `\\r` sits between the fence marker and the position `$` accepts, so the
    closer never fired and the alternation fell through to `\\Z`. Every fence then
    blanked the rest of the text — and for this tool "the rest of the text" is
    **every comment appended to the body**, because `issue_text` concatenates them.

    Not a constructed input: issue #1696's body is CRLF (written on Windows), so the
    `Handled by #1688` comment answering it was invisible, and the printed remedy
    ("post the link in the issue") had already been carried out while the row stayed
    `one-way`. Measured through this module's own pattern, before the fix: the same
    body masks `13..25` of 45 under LF and `15..52` of 52 under CRLF.

    Both directions are pinned, because a fix that merely stopped masking would be
    worse than the defect: an **unterminated** fence must still carry to the end.
    """
    lf = "example:\n```\nCloses #11.\n```\nand Closes #12.\n"
    crlf = lf.replace("\n", "\r\n")

    # The claim the fence encloses declares nothing, and the one after it is read —
    # under both line endings, which is the whole claim of this test.
    assert mod.declared_claims(lf) == {12}
    assert mod.declared_claims(crlf) == {12}

    # The issue side's phrase, so the fix is not proven on one reader only.
    issues_lf = "example:\n```\nHandled by #11.\n```\nand handled by #12.\n"
    assert mod.issue_claims(issues_lf) == {12}
    assert mod.issue_claims(issues_lf.replace("\n", "\r\n")) == {12}

    # The deliberate direction survives: no closer means mask to the end, CRLF too.
    assert mod.declared_claims("an unterminated fence:\r\n```\r\nCloses #13.") == set()

    # The masking stays length-preserving, which the negation window and every match
    # offset depend on: a fix that shortened the text would move the characters the
    # reading points at. Asserted on CRLF, where the fix now consumes the `\r`.
    assert len(mod._without_code(crlf)) == len(crlf)

    # The shape that broke the live row: a fenced body with the answer appended as a
    # comment, which is exactly how a handler names its issue back.
    appended = mod._without_code(crlf + "\r\nHandled by #1234.\r\n")
    assert "Handled by #1234." in appended, (
        "the comment appended to a CRLF body was masked away, so the row can never "
        "read as linked however many times the remedy is carried out"
    )


def test_issue_claims_reads_the_claim_phrase_and_not_a_citation(mod) -> None:
    """The issue side's claim form, in both directions, from the live vocabulary.

    Issue #1660 is the defect this pins: the reading counted *any* reference from an
    issue as the issue naming its handler, so #1650's reopen comment — which cited
    **#1653** as the branch a new guard lived on — manufactured a `one-way` row whose
    remedy asked an unrelated PR to declare the issue.

    The surviving forms are the ones the live queue actually carries, quoted from it
    (`Handled by #1659` on #1658, `Taken by **#1655**` on #1654 — the emphasis between the
    verb and the number is why the separator is what it is). The non-claims are the two
    shapes #1650 really writes: a bare citation, and a bare designation with no claim
    verb at all. Both must read as nothing, and the second is the one a reader would
    most expect to count — which is why it is asserted rather than left to judgment.
    """
    # The claims: the measured queue, verbatim, plus the closing verbs' passive voice.
    assert mod.issue_claims("Handled by #1659 (`emrg: a session snapshot`)") == {1659}
    assert mod.issue_claims("Handled by: #1666 — the separator may be a colon") == {1666}
    assert mod.issue_claims("Taken by **#1655** — the branch name becomes one constant") == {1655}
    assert mod.issue_claims("Taken by #1653 — `scripts/check-release-tag.py`") == {1653}
    assert mod.issue_claims("Closed by #7, fixed by #8 and resolved by #9") == {7, 8, 9}
    assert mod.issue_claims("Handled by #1, #2") == {1, 2}

    # A citation is not a claim — the live #1650 text, both halves of it.
    assert mod.issue_claims(
        "run `scripts/check-release-tag.py v0.3.4` … it exists only on #1653's branch"
    ) == set()
    assert mod.issue_claims(
        "The release PR for this is **#1651** (`emrg: release v0.3.4`, branch "
        "`release/v0.3.4`), and its body declares `Closes #1650`"
    ) == set()

    # The same two bounds the PR side applies: a negated phrase declares nothing, and a
    # quoted one declares nothing. `unhandled by` is the third form — the `\b` before the
    # verb is what keeps it out, since there is no boundary inside `unhandled`.
    assert mod.issue_claims("This is not handled by #1653") == set()
    assert mod.issue_claims("unhandled by #7, and unfixed by #8") == set()
    assert mod.issue_claims("it is not this, but handled by #5") == {5}
    assert mod.issue_claims("a quoted `Handled by #13` span") == set()
    assert mod.issue_claims("example:\n```\nHandled by #11.\n```\nand handled by #12.") == {12}
    assert mod.issue_claims("an unterminated fence:\n```\nHandled by #14.") == set()
    assert mod.issue_claims(None) == set()
    assert mod.issue_claims("nothing to see here") == set()


def test_claiming_issues_keeps_only_the_claimants(mod) -> None:
    """The filter between "referenced it" and "claims it", asserted as a set operation.

    Three issues reference one PR; only two claim it, and one of those claims a
    *different* PR — which must not transfer. An issue with no text read at all is not a
    claimant either, the same rule the PR side applies to a source with no body.
    """
    claims = {10: {20, 30}, 11: {20}, 12: {99}}
    assert mod.claiming_issues({10, 11, 12}, claims, 20) == {10, 11}
    assert mod.claiming_issues({10, 11, 12}, claims, 30) == {10}
    assert mod.claiming_issues({10, 11, 12}, claims, 42) == set()
    assert mod.claiming_issues({13}, claims, 20) == set()


def test_a_quoted_keyword_does_not_claim_anything(mod, monkeypatch, capsys) -> None:
    """The incident at report level: the quotation must not reach the issue's state.

    Written as the live contradiction rather than as a unit call, because that is how it
    was found: #1643 declared two issues as far as the reading could tell, so an issue
    that one PR legitimately finishes read `duplicate` — the state that says a second PR
    competes for the same work. The row for the *other* issue (#11 here) must stay
    `unclaimed` while the declared one is claimed.
    """
    quoting = "Closes #10.\n\n(as #20's body says: `Closes #11.` — quoted, not claimed)"
    fake = FakeGh(
        [_issue(10, "the finished one"), _issue(11, "only quoted by name")],
        [_pr(20, "the fix", body=quoting)],
        {
            10: [_refers_to(20, is_pr=True, body=quoting)],
            11: [_refers_to(20, is_pr=True, body=quoting)],
            20: [_refers_to(10, is_pr=False)],
        },
        comments={10: [_comment("Handled by #20")]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#11 issue UNCLAIMED" in out
    assert "#11 issue DUPLICATE" not in out
    assert "#10 issue ok" in out


def test_landed_work_is_not_reported_as_nothing_being_opened(mod, monkeypatch, capsys) -> None:
    """A merged referrer is work on master, and the row must say so (issue #1644).

    The measured defect: five of nine open issues were reported as *"nothing has been
    opened for it"* while merged PRs referenced them (all of #1553's #1565/#1569/#1607,
    all six of #1554's, #1614, #1589, #1599/#1600). An idle issue and an issue whose
    work landed read identically, which is exactly the ambiguity a never-closed backlog
    is made of.

    Both halves are asserted: the landed sentence appears, and the idle sentence is
    *absent* — the second is the one that was wrong, and an assertion that only checks
    for the new words would have passed against the old output too.
    """
    fake = FakeGh(
        [_issue(10, "landed but open")],
        [],
        {10: [_merged(20, body="a related fix, never declaring it"), _merged(21)]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    detail = _detail(out, "#10 issue UNCLAIMED")
    assert "#20 #21 referenced it and are merged, and neither declares" in detail
    assert "work has landed on master carrying this number" in detail
    assert "nothing has been opened for it" not in detail


def test_the_remedy_agrees_with_how_many_referrers_there_are(mod, monkeypatch, capsys) -> None:
    """Three referrers take `none of them`, not `neither` (measured 2026-09-26).

    The second revision of #1644's fix printed *"#1614 referenced it and are merged"* for
    the one-referrer case and *"#1565 #1569 #1607 ... and neither declares"* for the
    three-referrer one — a sentence with the wrong number in it on a row the reader is
    meant to act on, which is the same failure mode as the defect it was fixing, one
    level down: the text a reader acts on must say what is actually there. The corpus
    carries 1 (#1556), 2 and 6 (#1554) referrers, so all three branches are exercised
    between this test and the two above it.
    """
    fake = FakeGh(
        [_issue(10, "three landed referrers")],
        [],
        {10: [_merged(20), _merged(21), _merged(22)]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    detail = _detail(out, "#10 issue UNCLAIMED")
    # The absence first: it is the half the previous revision got wrong, so a run that
    # stops at the first failure must be stopped by the assertion that names the defect.
    assert "and neither declares" not in detail
    assert "#20 #21 #22 referenced it and are merged, and none of them declares" in detail


def test_an_abandoned_attempt_is_named_as_one(mod, monkeypatch, capsys) -> None:
    """A referrer closed **without** merging says a different thing again.

    `state: closed` is true of a merge and of an abandoned attempt alike, so the reading
    keys on `merged_at` — and this test is the one that keeps that distinction honest: a
    closed-unmerged referrer must not be reported as landed work. The remedy it carries
    is the host's third clause (`a rejected or change-requested PR is updated in place`),
    which is the reader's next move for this class.
    """
    fake = FakeGh(
        [_issue(10, "an attempt was abandoned")],
        [],
        {10: [_refers_to(20, is_pr=True, state="closed", body="a try, no keyword")]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    detail = _detail(out, "#10 issue UNCLAIMED")
    assert "#20 referenced it and was closed without merging and declares no" in detail
    assert "updated in place, never replaced by a second one" in detail
    assert "work has landed on master" not in detail


def test_all_three_referrer_classes_are_named_in_one_row(mod, monkeypatch, capsys) -> None:
    """An issue can carry all three at once, and a reader told only one gets it wrong.

    Composing them is not decoration: the landed clause invites a close, the abandoned
    clause explains a dead attempt, and the open clause is what the tool said before —
    and an issue whose landed referrer does *not* finish it would be closed by a reader
    who saw only the first clause. Order matters too and is asserted: the class that
    changes what a reader does next comes first.
    """
    fake = FakeGh(
        [_issue(10, "all three")],
        [],
        {
            10: [
                _refers_to(30, is_pr=True, body="mentions it"),
                _merged(31),
                _refers_to(32, is_pr=True, state="closed", body="abandoned"),
            ]
        },
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    detail = _detail(out, "#10 issue UNCLAIMED")
    assert "#31 referenced it and is merged, and declares no" in detail
    assert "#32 referenced it and was closed without merging and declares no" in detail
    assert "#30 referenced it without declaring" in detail
    assert (
        detail.index("#31")
        < detail.index("#32")
        < detail.index("#30")
    ), detail


def test_the_mention_split_is_read_from_the_events_own_fields(mod) -> None:
    """The classification, at unit level, on one event stream (issue #1644).

    `merged_at` is the discriminator and `state` is not, and the two only disagree in the
    direction that matters: a *closed issue* references a PR with `state: closed` and no
    merge, and a *merge* is `state: closed` with one. Reading the state alone would call
    an abandoned PR landed work.
    """
    events = [
        _refers_to(20, is_pr=True, body="open mention"),                       # open PR
        _merged(21),                                                           # merged
        _refers_to(22, is_pr=True, state="closed", body="abandoned"),          # closed
        _refers_to(23, is_pr=True, state="closed", body="Closes #10."),        # declared
        _refers_to(24, is_pr=True, body="Closes #10."),                        # declared, open
        _refers_to(25, is_pr=False),                                           # an *issue*
    ]

    refs = mod.referencing_prs(events, 10)

    assert refs.declared_open == {24}
    assert refs.declared_closed == {23}
    assert refs.mentioned_open == {20}
    assert refs.mentioned_landed == {21}
    assert refs.mentioned_abandoned == {22}
    # #25 is an issue referencing the subject, so it is in none of the five sets above.
    assert 25 not in refs.mentioned_open | refs.mentioned_landed | refs.mentioned_abandoned


def test_a_mention_is_not_a_claim(mod, monkeypatch, capsys) -> None:
    """A PR that references an issue without declaring it does not claim it.

    This is the incident, reproduced at report level: the reference is real and GitHub
    records it, so a mention-counting reading reports the issue as handled. The issue
    must read `unclaimed`, and the row must say *why* the reference did not count.
    """
    fake = FakeGh(
        [_issue(10, "the problem")],
        [_pr(20, "cites it", body="See #10 for the measurement.")],
        {
            10: [_refers_to(20, is_pr=True, body="See #10 for the measurement.")],
            20: [],
        },
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#10 issue UNCLAIMED" in out
    detail = _detail(out, "#10 issue UNCLAIMED")
    assert "#20 referenced it without declaring `Closes #10`" in detail
    assert "a mention is not a claim" in detail
    assert "#10 issue ok" not in out


def test_a_body_that_cites_issues_declares_none_of_them(mod, monkeypatch, capsys) -> None:
    """The measured incident exactly: one claim among several citations.

    PR #1643's body (this tool's own) cites #1551/#1553/#1598/#1606 as evidence and
    declares only #1642. The three cited issues must stay unclaimed while the declared
    one is claimed — a count that moved for all four is the defect, and a count that
    moved for none would mean the declaration is not read at all.
    """
    cited = "Cited: #11, #12 and #13 are the evidence."
    fake = FakeGh(
        [_issue(10, "the one it finishes"), _issue(11), _issue(12), _issue(13)],
        [_pr(20, "the fix", body=f"Closes #10.\n\n{cited}")],
        {
            10: [_refers_to(20, is_pr=True, body=f"Closes #10.\n\n{cited}")],
            11: [_refers_to(20, is_pr=True, body=f"Closes #10.\n\n{cited}")],
            12: [_refers_to(20, is_pr=True, body=f"Closes #10.\n\n{cited}")],
            13: [_refers_to(20, is_pr=True, body=f"Closes #10.\n\n{cited}")],
            20: [],
        },
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#10 issue ONE-WAY" in out or "#10 issue ok" in out  # claimed, one way or both
    for number in (11, 12, 13):
        assert f"#{number} issue UNCLAIMED" in out, out
    assert "#10 issue UNCLAIMED" not in out


def test_an_issue_reference_is_not_counted_as_a_pr_claim(mod, monkeypatch, capsys) -> None:
    """A cross-reference from another *issue* is not a PR claiming this one.

    Measured shape: issue timelines really do carry issue-to-issue cross-references
    (#1551's timeline shows #1554 and #1606 among its PR references). A reading that
    ignored the `pull_request` key would call this issue claimed by "#30" and print a
    `linked` row for a PR that does not exist.
    """
    fake = FakeGh(
        [_issue(10, "the problem"), _issue(30, "a related one")],
        [],
        {10: [_refers_to(30, is_pr=False)], 30: [_refers_to(10, is_pr=False)]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#10 issue UNCLAIMED" in out
    assert _detail(out, "#10 issue UNCLAIMED") == "nothing has been opened for it"
    assert "UNLINKED" not in out  # an issue reference is not a PR row either


def test_a_merged_declarer_names_the_landed_and_never_closed_shape(
    mod, monkeypatch, capsys
) -> None:
    """A merged PR that declared the issue, with the issue still open.

    The keyword should have closed it when the PR merged, so this is an anomaly worth
    its own words — and different from a mention, which is why the two details are
    asserted against a fixture that carries one of each.
    """
    fake = FakeGh(
        [_issue(10, "landed but open")],
        [],
        {
            10: [
                _refers_to(30, is_pr=True, state="closed", body="Closes #10."),
                _refers_to(40, is_pr=True, state="open", body="just mentions #10"),
            ]
        },
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    detail = _detail(out, "#10 issue UNCLAIMED")
    assert "#30 declared `Closes #10`" in detail
    assert "merged or closed" in detail
    assert "landed and was never closed" in detail


def test_a_closed_issue_naming_a_pr_is_not_a_live_link(mod, monkeypatch, capsys) -> None:
    """The open-queue boundary, and it is stated in the row rather than implied.

    A PR whose only mention is in an issue that has since closed reads `unlinked`: the
    mention is history, and counting it as "the issue names it back" would invent a live
    link out of an archived one. The remedy text still tells the reader what to do.
    """
    fake = FakeGh([], [_pr(20, "the fix", body="")], {20: [_refers_to(99, is_pr=False)]})
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#20 PR UNLINKED" in out
    assert "0 open issue(s), 1 open PR(s)" in out
    # the remedy, not just the label
    assert "declare" in _detail(out, "#20 PR UNLINKED")


def test_a_declaration_on_a_number_that_is_not_open_is_named(mod, monkeypatch, capsys) -> None:
    """`Closes #99` where 99 is not among the open issues.

    Two real causes — the issue was already closed, or a PR number was written with the
    closing form — and the row names the number instead of saying the PR declares
    nothing, because those are different diagnoses.
    """
    fake = FakeGh([], [_pr(20, "the fix", body="Closes #99.")], {20: []})
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#20 PR UNLINKED" in out
    detail = _detail(out, "#20 PR UNLINKED")
    assert "it declares #99" in detail
    assert "not among the open issues" in detail


# --------------------------------------------------------------------------- #
# The link, as both sides record it
# --------------------------------------------------------------------------- #


def test_a_linked_pair_exits_zero_and_names_both_directions(mod, monkeypatch, capsys) -> None:
    """The positive case, and the words it is reported in.

    Issue #10's timeline carries PR #20; PR #20's timeline carries issue #10; and #20's
    body declares `Closes #10`. Both rows read `linked`, the exit code is 0, and the
    details name the counterpart rather than merely asserting that something was found.
    """
    fake = _linked_pair()
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 0, out
    assert "OK:" in out
    assert "#10 issue ok" in out and "#20 PR ok" in out
    assert "#20 declares it and this issue names #20 back" in _detail(out, "#10 issue ok")
    assert "declares #10, named back in the issue" in _detail(out, "#20 PR ok")
    # The subject comes before any verdict, and both sides were really queried -
    # a `linked` verdict built without reading a timeline would otherwise pass here.
    assert out.splitlines()[0].startswith(f"repo: {REPO}, 1 open issue(s), 1 open PR(s)")
    assert sorted(fake.timeline_calls) == [10, 20]


def test_the_queue_is_read_from_the_one_endpoint_that_returns_both(mod, monkeypatch, capsys) -> None:
    """Issues and PRs are separated by the `pull_request` key, not by a number range.

    A range would be a guess; the key is what the API says. The assertion is on the
    counts the report prints, so a queue that put the PR in the issue list would be
    visible.
    """
    fake = _linked_pair()
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 0
    assert "1 open issue(s), 1 open PR(s)" in out
    queue_calls = [c for c in fake.calls if any("/issues?" in a for a in c)]
    assert len(queue_calls) == 1, "the queue must be one call, not one per subject"


def test_both_measured_gh_output_shapes_are_read(mod, monkeypatch, capsys) -> None:
    """The queue reads correctly in both shapes `gh api --paginate` can print.

    Both were measured on this machine (2026-09-26), and the parser branches on them,
    so both are asserted rather than one being left to a comment:

    * **unfiltered** — this tool's call — merges every page into **one JSON array on one
      line**. That is the shape the fake serves everywhere else in this file, and the
      assertion here is that it reads as exactly the rows it contains (one issue, one
      PR) with a single queue call.
    * **`--jq`-filtered** — the shape `check-vote-count.py` records — is one JSON object
      per line, which is not one document. A call site that adds a filter must not turn
      the queue into an "unreadable payload" false alarm, so the per-line branch is
      pinned here.
    """
    fake = _linked_pair()
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 0, out
    assert "1 open issue(s), 1 open PR(s)" in out
    assert len([c for c in fake.calls if any("/issues?" in a for a in c)]) == 1

    # The filtered shape: each line its own JSON value, so the stream is not one
    # document and the per-line branch is the one that must answer.
    def routed(args: list[str]) -> str:
        target = next(a for a in args if a.startswith("repos/"))
        if "/issues?" in target:
            return "\n".join(
                json.dumps(row)
                for row in [
                    [_issue(10, "the problem")],
                    [_pr(20, "the fix", body="Closes #10.")],
                ]
            )
        number = int(target.split("/issues/")[1].split("/")[0])
        if "/comments?" in target:
            return json.dumps(fake.comments.get(number, []))
        return json.dumps(fake.timelines[number])

    monkeypatch.setattr(mod, "_gh", routed)
    rc, out = _run(mod, capsys)

    assert rc == 0, out
    assert "1 open issue(s), 1 open PR(s)" in out


# --------------------------------------------------------------------------- #
# The states that are not `linked`
# --------------------------------------------------------------------------- #


def test_the_two_directions_are_read_from_opposite_sources_and_never_transposed(mod) -> None:
    """The unit-level pin on the trap the fixtures above are built around.

    Both helpers read `cross-referenced` events, and they must agree about which
    *kind* of source each is looking for: a source carrying `pull_request` is a PR
    referencing an issue, and one without it is an issue referencing a PR. Given the
    same two events, each helper must return only the one it is for - a helper that
    returned both would make every one-sided link read as mutual.

    `named_by_issue` is asserted separately because it is the inversion, and the
    inversion is where the two maps can be swapped without either helper being wrong:
    the issue-side verdict reads `named_by_issue(stated_by_pr)[issue]`, so an inverse
    that returned `{20: {10}}` instead of `{10: {20}}` would answer "does this issue
    name its PR?" from the wrong subject's row.
    """
    events = [
        _refers_to(20, is_pr=True, state="open", body="Closes #10."),
        _refers_to(10, is_pr=False),
        _refers_to(30, is_pr=True, state="open", body="mentions #10"),
    ]

    refs = mod.referencing_prs(events, 10)
    assert refs.declared_open == {20}
    assert refs.declared_closed == set()
    # #30 is an *open* PR that only mentions the issue: the bucket a citation belongs in.
    assert refs.mentioned_open == {30}
    assert refs.mentioned_landed == set() and refs.mentioned_abandoned == set()
    assert mod.referencing_issues(events, {10}) == {10}
    assert mod.named_by_issue({20: {10}}) == {10: {20}}

    # An empty reading is empty, not a row of `None`s.
    empty = mod.referencing_prs([], 10)
    assert (
        empty.declared_open,
        empty.declared_closed,
        empty.mentioned_open,
        empty.mentioned_landed,
        empty.mentioned_abandoned,
    ) == (set(), set(), set(), set(), set())
    assert mod.referencing_issues([], {10}) == set()
    assert mod.named_by_issue({}) == {}
    assert mod.declared_claims(None) == set()


def test_a_pr_that_declares_nothing_and_is_named_by_nobody_is_unlinked(
    mod, monkeypatch, capsys
) -> None:
    """An open PR belonging to no tracked problem, in both directions.

    Not `one-way`: there is nothing on either side, so the remedy is to declare the
    issue rather than to answer a mention.

    The fixture carries the measured shape that makes this decidable: a PR's timeline
    really does hold `cross-referenced` events whose source is **another PR** (PR #1638
    carried #1633/#1637/#1640). Those are PR-to-PR mentions and say nothing about which
    issue owns this PR, so the reading must ignore them — a `pull_request` key read as
    "an issue names it back" would turn every cross-referenced PR into a `one-way` row.
    """
    fake = FakeGh(
        [],
        [_pr(20, "an orphan"), _pr(21, "a related PR", body="Closes #20.")],
        {20: [_refers_to(21, is_pr=True, body="Closes #20.")], 21: []},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#20 PR UNLINKED" in out
    assert "belongs to no tracked problem" in _detail(out, "#20 PR UNLINKED")
    # #21 declares #20, which is a PR number, not an open issue: the mis-formed case.
    assert "#21 PR UNLINKED" in out
    assert "it declares #20" in _detail(out, "#21 PR UNLINKED")


def test_two_open_prs_declaring_one_issue_is_reported_as_duplicate(
    mod, monkeypatch, capsys
) -> None:
    """The shape the host's third clause forbids: a second PR where an update belongs.

    Both PR numbers must appear, because the remedy is to fold one into the other and a
    reader cannot do that from a count. The measured live shape is #1606, declared by
    #1638 while #1641 names it in prose only — the fixture carries a declarer and a
    non-declarer to pin that only declarers compose this state.
    """
    declaring = "Closes #10."
    fake = FakeGh(
        [_issue(10, "the problem")],
        [_pr(20, "one attempt", body=declaring), _pr(21, "another attempt", body=declaring)],
        {
            10: [
                _refers_to(20, is_pr=True, body=declaring),
                _refers_to(21, is_pr=True, body=declaring),
            ],
            20: [_refers_to(10, is_pr=False)],
            21: [_refers_to(10, is_pr=False)],
        },
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#10 issue DUPLICATE" in out
    detail = _detail(out, "#10 issue DUPLICATE")
    assert "more than one open PR declares it (#20, #21)" in detail
    assert "updated in place, never replaced by a second one" in detail


def test_a_pr_that_declares_an_issue_the_issue_never_names_is_one_way(
    mod, monkeypatch, capsys
) -> None:
    """The PR declares the issue; the issue never says so.

    The remedy names the issue's comment, because a reader of the issue is the one left
    looking for the work.
    """
    fake = FakeGh(
        [_issue(10, "the problem")],
        [_pr(20, "the fix", body="Closes #10.")],
        {10: [_refers_to(20, is_pr=True, body="Closes #10.")], 20: []},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#10 issue ONE-WAY" in out
    assert "#20 PR ONE-WAY" in out
    detail = _detail(out, "#10 issue ONE-WAY")
    assert "#20 declares it, and this issue never names #20" in detail
    assert "gh issue comment 10" in detail


def test_the_issue_naming_a_pr_that_declares_no_issue_is_one_way_not_linked(
    mod, monkeypatch, capsys
) -> None:
    """The mirror: the issue names the PR and the PR declares no issue at all.

    Different reader, different remedy — the declaration belongs in the PR body. This
    is the live shape of #1641, which names #1606 in prose and declares nothing.
    """
    fake = FakeGh(
        [_issue(10, "the problem")],
        [_pr(20, "the fix", body="Issue #10: the count cannot fire for it.")],
        {10: [], 20: [_refers_to(10, is_pr=False)]},
        comments={10: [_comment("Handled by #20 — it declares nothing, which is the row.")]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#20 PR ONE-WAY" in out
    detail = _detail(out, "#20 PR ONE-WAY")
    assert "#10 names it and this PR declares no issue" in detail
    assert "`Closes #N` where the PR finishes it" in detail
    # …and nothing about a code span: this body declares nothing anywhere, so the sentence
    # that names the mask would be a claim about evidence this body does not have.
    assert "inside a code span" not in detail
    assert "#20 PR ok" not in out


def test_a_masked_declaration_is_named_on_the_one_way_row(mod, monkeypatch, capsys) -> None:
    """#1715's live shape, 2026-09-29: the declaration is in the body, in backticks.

    Its Tracking section read ``"`Closes #1718`."`` — a real closing keyword for the issue
    that names it, invisible to `declared_claims` because the masking is right that a quoted
    keyword is not a claim — and the row answered with the generic remedy, "state it in the
    PR body", for a body that had already stated it. The author read that, concluded the tool
    was wrong, and wrote *that* in the body. The remedy now names the mask, so the reader is
    handed the one thing that fixes the row: move the keyword out of the span.

    The claim is still not counted, and that is asserted too — the state stays `one-way` and
    the row stays red. A friendlier sentence must not be bought with a weaker reading.
    """
    fake = FakeGh(
        [_issue(1718, "a confined runner announces its own start")],
        [
            _pr(
                1715,
                "the runner announces its own start",
                body="## Tracking\n\n`Closes #1718`. The tool reads both directions ok.",
            )
        ],
        {1718: [], 1715: [_refers_to(1718, is_pr=False)]},
        comments={1718: [_comment("Handled by #1715 — the pair is born together.")]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#1715 PR ONE-WAY" in out
    detail = _detail(out, "#1715 PR ONE-WAY")
    assert "#1718 names it and this PR declares no issue" in detail
    assert "inside a code span" in detail
    assert "#1715 PR ok" not in out
    # A body whose declaration is *not* masked gets no such sentence: the note is evidence
    # about this body, not decoration on the row.
    assert mod.quoted_claims("Closes #1718.") == set()


def test_a_citation_of_a_pr_does_not_read_as_that_issue_naming_it(
    mod, monkeypatch, capsys
) -> None:
    """Issue #1660's defect, at report level: a citation must not manufacture a row.

    The live shape, measured 2026-09-27 on the real API and reproduced here: issue #1650's
    reopen comment cites **#1653** because a guard it wants run lives on that branch. The
    reference is real — GitHub records it, and `referencing_issues` still returns #1650
    for #1653's timeline (re-measured on the live API while writing this) — so a reading
    that counts references says the issue names #1653 as its handler and prints a
    `one-way` row asking an unrelated PR to declare #1650.

    Both rows are asserted, because the defect was visible on both: the PR row must read
    `unlinked` (nothing claims it, so "it belongs to no tracked problem" is the remedy)
    and the issue row `unclaimed` (nothing declares it). Neither may read `one-way`, and
    the words that would say a citation counted must appear nowhere.
    """
    citation = (
        "The reopen comment pointed at the branch carrying a new guard: run "
        "`scripts/check-release-tag.py v0.3.4` … it exists only on #20's branch"
    )
    fake = FakeGh(
        [_issue(10, "master is past the last release")],
        [_pr(20, "a guard, belonging to another issue", body="")],
        {10: [], 20: [_refers_to(10, is_pr=False, body="master is past the last release")]},
        comments={10: [_comment(citation)]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#20 PR UNLINKED" in out
    assert "belongs to no tracked problem" in _detail(out, "#20 PR UNLINKED")
    assert "#10 issue UNCLAIMED" in out
    assert _detail(out, "#10 issue UNCLAIMED") == "nothing has been opened for it"
    assert "names it and this PR declares no issue" not in out
    assert "this issue names #20" not in out
    # The citation's own number is still read — it is the *claim* that is filtered, not
    # the reference, so a row that vanished because nothing was fetched would pass the
    # assertions above for the wrong reason.
    assert fake.comments_calls == [10]


def test_an_issue_that_claims_in_its_body_is_read_too(mod, monkeypatch, capsys) -> None:
    """The claim is read from the body as well as the comments.

    `issue_text` joins the two, and the live vocabulary does both (`Taken by #1653` is a
    comment on #1652; an issue body naming its handler is the same act). A reading that
    only fetched comments would report `one-way` here, and the reverse — a body read that
    skipped the comments — is what `_linked_pair` pins.
    """
    fake = FakeGh(
        [_issue(10, "the problem", body="Handled by #20 once it lands.")],
        [_pr(20, "the fix", body="Closes #10.")],
        {
            10: [_refers_to(20, is_pr=True, body="Closes #10.")],
            20: [_refers_to(10, is_pr=False, body="Handled by #20 once it lands.")],
        },
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 0, out
    assert "#10 issue ok" in out and "#20 PR ok" in out
    assert fake.comments_calls == [10]


def test_the_claim_text_is_read_only_for_issues_that_reference_a_pr(
    mod, monkeypatch, capsys
) -> None:
    """The cost of the reading, pinned: one comments call per **referencing** issue.

    A claim is what GitHub cross-references, so an issue that claims a PR is always in
    some PR's reference set and the filter cannot miss one — while an issue nobody
    references is not read for text at all. That is what keeps the guard's calls
    proportional to the question rather than to the queue, and a later refactor that
    looped over every open issue would double the run and break nothing else, which is
    exactly why the count is asserted here.
    """
    fake = FakeGh(
        [_issue(10, "referenced"), _issue(11, "untouched"), _issue(12, "untouched")],
        [_pr(20, "the fix", body="")],
        {10: [], 11: [], 12: [], 20: [_refers_to(10, is_pr=False)]},
        comments={10: [_comment("Handled by #20")]},
    )
    _install(mod, monkeypatch, fake)

    _run(mod, capsys)

    assert sorted(fake.timeline_calls) == [10, 11, 12, 20]
    assert fake.comments_calls == [10]


def test_an_all_linked_queue_exits_zero_and_says_what_it_read(mod, monkeypatch, capsys) -> None:
    """Two linked pairs: every row `linked`, and the subject count is in the OK line.

    The count is the evidence that the verdict covered the queue rather than a single
    pair — a tool that read one subject and stopped would print the same words.
    """
    fake = FakeGh(
        [_issue(10, "one"), _issue(11, "two")],
        [_pr(20, "a", body="Closes #10."), _pr(21, "b", body="Closes #11.")],
        {
            10: [_refers_to(20, is_pr=True, body="Closes #10.")],
            11: [_refers_to(21, is_pr=True, body="Closes #11.")],
            20: [_refers_to(10, is_pr=False)],
            21: [_refers_to(11, is_pr=False)],
        },
        comments={10: [_comment("Handled by #20")], 11: [_comment("Taken by #21")]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 0, out
    assert "4 subject(s) read" in out
    assert out.count("ok") == 4


# --------------------------------------------------------------------------- #
# The boundary: unmeasurable is never a pass
# --------------------------------------------------------------------------- #


def test_a_gh_failure_exits_2_and_names_the_reason(mod, monkeypatch, capsys) -> None:
    """An unreadable queue is not a clean one.

    Exit 2, the reason on stderr, and — the half that matters — no `OK` anywhere on
    stdout, because a caller that greps for the verdict must not find one.
    """

    def boom(args: list[str]) -> str:
        raise RuntimeError("gh failed (rc=1): gh api repos/argszero/emrg/issues\nboom")

    monkeypatch.setattr(mod, "_gh", boom)

    rc = mod.main([])
    captured = capsys.readouterr()

    assert rc == 2
    assert "cannot determine the issue/PR links" in captured.err
    assert "boom" in captured.err
    assert "OK" not in captured.out


def test_a_payload_that_is_not_a_list_is_unmeasurable(mod, monkeypatch, capsys) -> None:
    """A payload that parses to an object is not a queue of zero rows.

    Treated as unmeasurable (2) rather than as an empty queue (0), which is the
    difference between "no links to report" and "nothing was read".
    """
    monkeypatch.setattr(mod, "_gh", lambda args: json.dumps({"message": "Not Found"}))

    rc = mod.main([])
    captured = capsys.readouterr()

    assert rc == 2
    assert "expected a list" in captured.err
    assert "OK" not in captured.out


def test_an_unparseable_timeline_is_unmeasurable(mod, monkeypatch, capsys) -> None:
    """A truncated timeline is not a timeline with no events.

    If the events could not be read, the tool would otherwise answer `unlinked` for a
    PR that may well declare an issue — a confident wrong verdict, which is the failure
    mode the exit-2 branch exists for.
    """

    def routed(args: list[str]) -> str:
        target = next(a for a in args if a.startswith("repos/"))
        if "/issues?" in target:
            return json.dumps([_pr(20), _issue(10)])
        return "not json at all"

    monkeypatch.setattr(mod, "_gh", routed)

    rc = mod.main([])
    captured = capsys.readouterr()

    assert rc == 2
    assert "OK" not in captured.out


# --------------------------------------------------------------------------- #
# The report's own shape
# --------------------------------------------------------------------------- #


def test_the_json_report_is_one_document_naming_the_repo(mod, monkeypatch, capsys) -> None:
    """`--json` stays parseable: one document, the subject in it, the state per row."""
    fake = FakeGh(
        [_issue(10, "the problem")],
        [_pr(20, "the fix", body="Closes #10.")],
        {10: [_refers_to(20, is_pr=True, body="Closes #10.")], 20: []},
    )
    _install(mod, monkeypatch, fake)

    rc = mod.main(["--json"])
    out = capsys.readouterr().out

    payload = json.loads(out)  # a second document on stdout would fail here
    assert rc == 1
    assert payload["repo"] == REPO
    assert payload["open_issues"] == 1 and payload["open_prs"] == 1
    assert payload["clean"] == 0
    states = {(row["kind"], row["number"]): row["state"] for row in payload["rows"]}
    assert states[("issue", 10)] == "one-way"
    assert states[("pr", 20)] == "one-way"
    assert payload["rows"][0]["title"] == "the problem"


def test_the_summary_lists_the_offenders_in_the_printed_order(mod, monkeypatch, capsys) -> None:
    """The summary line follows the body, so a reader can walk the report downwards.

    Asserted as a sequence rather than a set: the rows print issues (ascending) then
    PRs, and a summary sorted differently is the small friction that makes a reader
    re-scan a long report by hand.
    """
    fake = FakeGh(
        [_issue(10), _issue(11)],
        [_pr(20, body="Closes #11.")],
        {10: [], 11: [_refers_to(20, is_pr=True, body="Closes #11.")], 20: []},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    summary = [ln for ln in out.splitlines() if "subject(s) are not linked" in ln][0]
    assert summary.index("#10 unclaimed") < summary.index("#11 one-way")
    assert summary.index("#11 one-way") < summary.index("#20 one-way")


def test_the_repo_argument_reaches_every_gh_call(mod, monkeypatch, capsys) -> None:
    """`--repo` is honoured on the queue call and on every timeline call.

    A tool that read the queue from one repo and the timelines from another would
    produce one-sided rows for every subject; the assertion is over calls rather than
    over output for exactly that reason.
    """
    seen: list[str] = []

    def routed(args: list[str]) -> str:
        target = next(a for a in args if a.startswith("repos/"))
        seen.append(target)
        if "/issues?" in target:
            return json.dumps([_issue(10)])
        return json.dumps([])

    monkeypatch.setattr(mod, "_gh", routed)

    rc, out = _run(mod, capsys, ["--repo", "someone/else"])

    assert rc == 1
    assert out.splitlines()[0].startswith("repo: someone/else")
    assert seen, "no gh call was made"
    assert all(t.startswith("repos/someone/else/") for t in seen), seen


def test_age_is_reported_in_days_and_an_unreadable_timestamp_is_named(mod) -> None:
    """The age is what makes a backlog visible, so it must degrade into words.

    A subject whose `created_at` will not parse gets `age unreadable`, never a `0.0`
    that reads as "opened today".
    """
    import datetime as dt

    now = dt.datetime.fromisoformat(NOW)
    assert mod.age_days("2026-09-24T10:00:00Z", now) == pytest.approx(2.0)
    assert mod.age_days("not a timestamp", now) is None
    assert mod.age_days("", now) is None

    row = mod.Row("issue", 10, "t", "unclaimed", "d", None)
    assert "age unreadable" in mod.render(row)


def test_the_state_words_are_not_flattened_into_one_mark(mod) -> None:
    """Each state prints its own word, because the remedies differ."""
    marks = {
        state: mod.render(mod.Row("issue", 1, "t", state, "d", 0.5))
        for state in ("linked", "one-way", "unclaimed", "duplicate", "unlinked")
    }
    assert "ok" in marks["linked"]
    for state, text in marks.items():
        if state != "linked":
            assert state.upper() in text, (state, text)
            assert text != marks["linked"]


# --------------------------------------------------------------------------- #
# The chain's first joint: an issue's `Origin: rant <timestamp>` (R5)
#
# The link above is read from GitHub; this one is read from a **file**, because a rant
# timestamp is a host-local handle (`~/.emrg/rants.jsonl`). The questions are different:
# *does the handle resolve* (otherwise the issue's reader cannot reach the requirement the
# issue carries) and *is one rant claimed by two unlabelled issues* (otherwise a deliberate
# split cannot be told from a duplicate claim).
# --------------------------------------------------------------------------- #

RANT_TS = "2026-09-29T15:52:49.378845+08:00"


def _ledger(tmp_path, *timestamps: str):
    """A rant ledger at `tmp_path`, in the shape `submit_rant` writes (one JSON per line)."""
    path = tmp_path / "rants.jsonl"
    path.write_text(
        "".join(
            json.dumps(
                {
                    "timestamp": ts,
                    "project": "emrg",
                    "status": "pending",
                    "progress": "",
                    "completed": None,
                    "message": "a rant",
                }
            )
            + "\n"
            for ts in timestamps
        ),
        encoding="utf-8",
    )
    return path


def _issue_with_origin(number: int, origin: str, body: str = "") -> dict:
    return _issue(number, f"issue {number}", body=f"Origin: rant {origin}\n\n{body}")


def _linked_issues(issues: list[dict]) -> FakeGh:
    """N issues, each declared by its own PR and naming it back — every link reads `ok`.

    The origin reading is a check *beside* the link reading, so the tests that isolate it
    hold the link clean: otherwise a row's state is the link's fault and the origin fault
    only rides along in the detail, which would let a broken origin reading pass by looking
    like a broken link. With the pairs linked, the only thing that can make these rows
    non-`linked` is the origin itself.
    """
    prs: list[dict] = []
    timelines: dict[int, list[dict]] = {}
    comments: dict[int, list[dict]] = {}
    for issue in issues:
        number = int(issue["number"])
        pr_number = number + 100
        prs.append(_pr(pr_number, f"the fix for {number}", body=f"Closes #{number}."))
        timelines[number] = [_refers_to(pr_number, is_pr=True, body=f"Closes #{number}.")]
        timelines[pr_number] = [_refers_to(number, is_pr=False)]
        comments[number] = [_comment(f"Handled by #{pr_number}")]
    return FakeGh(issues, prs, timelines, comments)


def test_an_origin_the_ledger_holds_is_not_a_fault(mod, monkeypatch, capsys, tmp_path) -> None:
    """The positive case: the handle resolves, so the first joint of the chain is intact."""
    _install(mod, monkeypatch, _linked_issues([_issue_with_origin(10, RANT_TS)]))

    rc, out = _run(mod, capsys, ["--rants", str(_ledger(tmp_path, RANT_TS))])

    assert rc == 0, out
    assert "origin an issue declares resolves in the rant ledger" in out, out


def test_an_origin_the_ledger_does_not_hold_is_a_fault(mod, monkeypatch, capsys, tmp_path) -> None:
    """An unresolvable handle is the chain broken at its first joint.

    The row keeps the remedy on it and names the ledger it read — a reader told "no such
    rant" has to be told *where* it was looked for before the sentence is actionable.
    """
    _install(mod, monkeypatch, _linked_issues([_issue_with_origin(10, RANT_TS)]))

    rc, out = _run(mod, capsys, ["--rants", str(_ledger(tmp_path, "2026-01-01T00:00:00+08:00"))])

    assert rc == 1, out
    assert "#10 issue ORIGIN-UNRESOLVED" in out, out
    detail = _detail(out, "#10 issue ORIGIN-UNRESOLVED")
    assert RANT_TS in detail and "rants.jsonl" in detail and "verbatim" in detail, detail


def test_a_near_match_names_the_ledgers_own_spelling(mod, monkeypatch, capsys, tmp_path) -> None:
    """A citation that drops the microseconds is fixed by writing the stored spelling.

    The reading does not silently accept the prefix — two rants can share a second, so
    accepting one would resolve a handle to the wrong record. What it does instead is name
    the record the writer was reaching for, which is the difference between a remedy and a
    complaint.
    """
    _install(
        mod,
        monkeypatch,
        _linked_issues([_issue_with_origin(10, "2026-09-29T15:52:49+08:00")]),
    )

    rc, out = _run(mod, capsys, ["--rants", str(_ledger(tmp_path, RANT_TS))])

    assert rc == 1, out
    detail = _detail(out, "#10 issue ORIGIN-UNRESOLVED")
    assert RANT_TS in detail and "write it verbatim" in detail, detail


def test_two_unlabelled_issues_on_one_origin_are_a_duplicate(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """One rant, one issue: two unlabelled claims of the same handle cannot be told apart.

    Both links are clean here, which is the point — without the origin clause this queue
    reads `OK`, so the row's state is entirely the origin reading's verdict.
    """
    _install(
        mod,
        monkeypatch,
        _linked_issues([_issue_with_origin(10, RANT_TS), _issue_with_origin(11, RANT_TS)]),
    )

    rc, out = _run(mod, capsys, ["--rants", str(_ledger(tmp_path, RANT_TS))])

    assert rc == 1, out
    for subject in ("#10 issue ORIGIN-DUPLICATE", "#11 issue ORIGIN-DUPLICATE"):
        detail = _detail(out, subject)
        assert RANT_TS in detail and "Part:" in detail, detail


def test_one_issue_writing_its_origin_twice_is_not_a_duplicate(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """The duplicate state counts **issues**, not occurrences of the origin line.

    A body can spell its origin line more than once — at the top and again in a provenance
    section, or because an edit appended where it meant to replace — and that is one issue
    declaring one origin. The rule it is measured against says "two or more open issues", so
    a single issue can never be a duplicate of itself; grouping by occurrence made this queue
    fail with a row reading *"#10 name the same rant origin ... and #10 #10 carry no
    `Part: n/N`"*, three wrong numbers in the sentence a reader acts on.

    The queue is otherwise clean (`_linked_issues` holds every link `ok` and the ledger holds
    the timestamp), so `OK` here is the whole verdict: nothing but the origin reading can
    make it fail.
    """
    _install(
        mod,
        monkeypatch,
        _linked_issues(
            [_issue_with_origin(10, RANT_TS, body=f"Origin: rant {RANT_TS}")]
        ),
    )

    rc, out = _run(mod, capsys, ["--rants", str(_ledger(tmp_path, RANT_TS))])

    assert rc == 0, out
    assert "OK" in out, out
    assert "DUPLICATE" not in out, out


def test_a_labelled_split_of_one_rant_is_allowed(mod, monkeypatch, capsys, tmp_path) -> None:
    """R5's escape hatch: a rant carried by two issues, each saying which part it is.

    Without this the reading would forbid the split the design explicitly permits when a
    rant's requirements are independent — and a rule that cannot be satisfied is one a cycle
    answers by editing the reading instead of the work.
    """
    _install(
        mod,
        monkeypatch,
        _linked_issues(
            [
                _issue_with_origin(10, RANT_TS, body="Part: 1/2"),
                _issue_with_origin(11, RANT_TS, body="Part: 2/2"),
            ]
        ),
    )

    rc, out = _run(mod, capsys, ["--rants", str(_ledger(tmp_path, RANT_TS))])

    assert rc == 0, out
    assert "OK" in out, out


def test_one_labelled_and_one_unlabelled_part_is_still_a_duplicate(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """The escape hatch has to be used by **every** part, not by one of them.

    Otherwise "labelled" would depend on which issue a reader looked at first, and the
    unlabelled one would still be indistinguishable from a duplicate claim.
    """
    _install(
        mod,
        monkeypatch,
        _linked_issues(
            [
                _issue_with_origin(10, RANT_TS, body="Part: 1/2"),
                _issue_with_origin(11, RANT_TS),
            ]
        ),
    )

    rc, out = _run(mod, capsys, ["--rants", str(_ledger(tmp_path, RANT_TS))])

    assert rc == 1, out
    assert "#11 issue ORIGIN-DUPLICATE" in out, out
    assert "#10 issue ORIGIN-DUPLICATE" not in out, out


def test_a_queue_that_declares_no_origin_never_reads_the_ledger(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """The check is lazy, and that is what keeps it from reddening unrelated queues.

    A host with no ledger — a fork, a fresh machine — must not have its link reading turned
    into exit 2 by a clause nobody invoked. The path given here does not exist, so any read
    at all would be measurable as a failure rather than as silence.
    """
    _install(mod, monkeypatch, _linked_pair())

    rc, out = _run(mod, capsys, ["--rants", str(tmp_path / "absent.jsonl")])

    assert rc == 0, out
    assert "OK" in out


def test_a_ledger_that_cannot_be_read_is_unmeasurable(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """An issue declares an origin and the ledger is not there: exit 2, never a pass.

    `origin-unresolved` would be a confident claim that the rant never existed, which is not
    something a missing file can support — the family's rule is that a question the tool
    cannot answer is reported unanswered.
    """
    _install(mod, monkeypatch, FakeGh([_issue_with_origin(10, RANT_TS)], [], {10: []}))

    rc = mod.main(["--rants", str(tmp_path / "absent.jsonl")])
    captured = capsys.readouterr()

    assert rc == 2
    assert "rant ledger could not be read" in captured.err
    assert "OK" not in captured.out


def test_a_fenced_origin_line_declares_nothing(mod, monkeypatch, capsys, tmp_path) -> None:
    """An issue that documents the convention in a fence is not declaring an origin.

    R5's text and this tool's own docstring both spell `Origin: rant <ts>` while explaining
    it, so that quotation is the family's normal input. The path does not exist, so a read
    at all would be measurable: `origin-unresolved` here would mean the fence leaked.
    """
    _install(
        mod,
        monkeypatch,
        FakeGh(
            [
                _issue(
                    10,
                    "an issue that quotes the rule",
                    body="R5 asks for a first line like\n\n```\n"
                    "Origin: rant 1999-01-01T00:00:00+08:00\n```\n",
                )
            ],
            [],
            {10: []},
        ),
    )

    rc, out = _run(mod, capsys, ["--rants", str(tmp_path / "absent.jsonl")])

    assert rc == 1, out  # unclaimed, which is this issue's real state
    assert "UNRESOLVED" not in out.upper(), out


def test_the_origin_fault_is_folded_into_the_issues_own_row(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """One row per subject: a faulty link keeps the state, the origin fault rides along.

    The report's shape is how it is read at a glance, so an issue does not get a second row.
    What must not happen is the origin fault *disappearing* when the link is already broken
    — that is the state where a reader is most likely to be fixing the issue anyway.
    """
    _install(mod, monkeypatch, FakeGh([_issue_with_origin(10, RANT_TS)], [], {10: []}))

    rc, out = _run(mod, capsys, ["--rants", str(_ledger(tmp_path, "2026-01-01T00:00:00+08:00"))])

    assert rc == 1, out
    assert out.count("#10 issue") == 1, out
    assert "#10 issue UNCLAIMED" in out, out  # the link fault keeps the state
    assert RANT_TS in _detail(out, "#10 issue UNCLAIMED"), out


def test_declared_origins_reads_the_bodys_own_line_and_masks_only_fences(mod) -> None:
    """The unit-level reading, driven directly so the masking is pinned where it lives.

    Four shapes, each settled against the live queue rather than invented: a plain line
    declares; a line **inside a fence** does not (a body documenting the convention, which
    is what this tool's docstring and R5's text both do); a whole line in backticks is not a
    declaration either, because the anchor cannot see it; and a backticked **timestamp with
    prose after it** does declare — that is #1745's real first line, and a reading that
    silently ignored it would miss the very fault it exists to report.
    """
    declared = mod.declared_origins(
        [
            _issue(1, body="Origin: rant 2026-09-29T15:52:49.378845+08:00\n\nthe body"),
            _issue(2, body="```\nOrigin: rant 1999-01-01T00:00:00+08:00\n```\n"),
            _issue(3, body="`Origin: rant 1999-01-01T00:00:00+08:00`\n"),
            _issue(
                4,
                body=(
                    "Origin: rant `2026-09-29T15:52:43.442676+08:00` (a re-scoped rant), "
                    "which re-scoped itself from `2026-09-27T19:41:13`.\n"
                ),
            ),
            _issue(5, body="nothing here"),
            _issue(6, body="R5 asks for a first line like `Origin: rant <timestamp>`.\n"),
        ]
    )

    assert declared == {
        1: ["2026-09-29T15:52:49.378845+08:00"],
        4: ["2026-09-29T15:52:43.442676+08:00"],
    }, declared


# --- the ledger, the one reader both scripts rest on ------------------------
#
# The reader was widened for a second consumer — `scripts/review-queue.py` renders a row
# per open rant (memory `queue-renders-no-row-for-a-pending-rant.md`) — so what is pinned
# here is that the file format has **one** answer: the rows view and the set view of one
# ledger agree, and both keep the distinction the tool's docstring is built on.


def _ledger_lines(tmp_path, *lines, name="rants.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_ledger_reader_returns_rows_and_the_set_view_agrees(mod, tmp_path):
    """Two views of one file, and the set view is *derived* from the rows view: a second
    parse of the same format is a second answer to "what is a rant row", and the copy
    that drifts is the one nobody reads."""
    path = _ledger_lines(
        tmp_path,
        json.dumps({"timestamp": "2026-09-30T09:17:54+08:00", "status": "pending"}),
        "not json at all",
        json.dumps({"timestamp": "2026-09-30T09:35:04+08:00", "status": "completed"}),
        json.dumps(["a list, not a row"]),
        json.dumps({"status": "pending", "no_timestamp": True}),
        "   ",
    )

    rows = mod.load_rant_rows(path)
    assert [row.get("timestamp") for row in rows] == [
        "2026-09-30T09:17:54+08:00",
        "2026-09-30T09:35:04+08:00",
        None,
    ], rows
    assert mod.load_rants(path) == {
        "2026-09-30T09:17:54+08:00",
        "2026-09-30T09:35:04+08:00",
    }


def test_a_ledger_that_is_not_there_is_not_an_empty_one(mod, tmp_path):
    """`origin-unresolved` for every issue, or "no rants" for the queue, is a confident
    wrong verdict about a queue that may be perfectly fine — so the reader raises and the
    caller reports exit 2."""
    with pytest.raises(RuntimeError):
        mod.load_rant_rows(tmp_path / "absent.jsonl")


def test_the_ledger_path_is_overridable(mod, monkeypatch):
    """`--rants`, then `$EMRG_RANTS`, then the host's own store — the order a host with a
    ledger elsewhere depends on, and the seam `review-queue.py` passes through."""
    assert mod.rants_path("/tmp/elsewhere.jsonl") == Path("/tmp/elsewhere.jsonl")
    monkeypatch.setenv("EMRG_RANTS", "/tmp/from-env.jsonl")
    assert mod.rants_path() == Path("/tmp/from-env.jsonl")
    assert mod.rants_path("/tmp/wins.jsonl") == Path("/tmp/wins.jsonl")
