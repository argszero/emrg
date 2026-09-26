"""The issue/PR link reading: both directions, and the states that are not `linked`.

Host 2026-09-26T18:52:57 stated the rule (`scripts/check-issue-links.py` quotes it
verbatim in its docstring): one issue is finished by exactly one PR, each names the
other, and a rejected or change-requested PR is updated in place. This file is the
evidence that the reading built for it answers *that* question.

The tests are built around the one mistake the tool can make and still look right
-------------------------------------------------------------------------------
Both directions of the link are read from GitHub, but **not from the same place**: an
issue's timeline records the PRs that referenced it, and a PR's timeline records the
issues that referenced it. Read the wrong map for a side and every one-sided link
becomes bidirectional — the report says `linked` everywhere and the tool is worthless
while looking healthy. So the fixtures below are built as three shapes that are
indistinguishable if the two maps are confused, and each is asked about explicitly:

* `test_a_pr_side_mention_does_not_make_an_issue_name_it_back` — the issue names the PR
  and the PR does not name the issue: the PR's `one-way`, never `linked`;
* `test_an_issue_naming_a_pr_that_does_not_reply_is_one_way_not_linked` — the mirror
  case on the issue side (the PR's timeline carries the issue's mention, the issue's
  timeline carries nothing from the PR);
* `test_an_issue_reference_is_not_counted_as_a_pr_claim` — an issue whose timeline shows
  a *cross-reference from another issue* has no claim on it at all: only a source that
  carries `pull_request` counts.

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


def _issue(number: int, title: str = "an issue", created: str = "2026-09-24T10:00:00Z") -> dict:
    return {"number": number, "title": title, "created_at": created}


def _pr(number: int, title: str = "a pr", created: str = "2026-09-26T05:00:00Z") -> dict:
    """An open PR as `/issues` reports it — the discriminator is `pull_request`."""
    return {
        "number": number,
        "title": title,
        "created_at": created,
        "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"},
    }


def _refers_to(number: int, *, is_pr: bool, state: str = "open") -> dict:
    """A `cross-referenced` event whose source is `number`.

    `is_pr` is the whole test: GitHub records whether the *referrer* was a PR by
    putting a `pull_request` object on the source. Everything the tool decides is
    downstream of reading this key correctly, which is why the fixtures build it
    explicitly rather than deriving it from the number.
    """
    source: dict = {"number": number, "state": state}
    if is_pr:
        source["pull_request"] = {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}
    return {"event": "cross-referenced", "source": {"issue": source}}


class FakeGh:
    """`_gh` replaced by a routing table; every call recorded.

    Two payloads are served, both as the JSON *text* `gh` would print, because the
    tool parses its own output: the open queue (one `/issues` call) and a per-subject
    timeline. An unexpected query is an assertion failure rather than an empty answer,
    so a typo in a path cannot read as "no links".

    `pages` reproduces the shape measured on this machine (2026-09-26): with no `--jq`,
    `gh api --paginate` prints **one merged array on one line**, so N pages are one
    document with N copies of the rows. That is a fact about gh, not a convenience —
    the sibling guards' per-line parsing is for a `--jq`-filtered call, which is a
    different shape (pinned separately below).
    """

    def __init__(
        self,
        issues: list[dict],
        prs: list[dict],
        timelines: dict[int, list[dict]],
    ):
        self.issues = issues
        self.prs = prs
        self.timelines = timelines
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


def _install(mod, monkeypatch, fake: FakeGh) -> None:
    monkeypatch.setattr(mod, "_gh", fake)


def _run(mod, capsys, argv: list[str] | None = None):
    rc = mod.main(argv or [])
    out = capsys.readouterr().out
    return rc, out


# --------------------------------------------------------------------------- #
# The link, as both sides record it
# --------------------------------------------------------------------------- #


def test_a_linked_pair_exits_zero_and_names_both_directions(mod, monkeypatch, capsys) -> None:
    """The positive case, and the words it is reported in.

    Issue #10's timeline carries PR #20; PR #20's timeline carries issue #10. Both
    rows read `linked`, the exit code is 0, and the details name the counterpart
    rather than merely asserting that something was found.
    """
    fake = FakeGh(
        [_issue(10, "the problem")],
        [_pr(20, "the fix")],
        {10: [_refers_to(20, is_pr=True)], 20: [_refers_to(10, is_pr=False)]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 0, out
    assert "OK:" in out
    assert "#10 issue ok" in out and "#20 PR ok" in out
    assert "#20 references it and this issue names #20 back" in out
    assert "belongs to #10, named back in the issue" in out
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
    fake = FakeGh(
        [_issue(10)],
        [_pr(20)],
        {10: [_refers_to(20, is_pr=True)], 20: [_refers_to(10, is_pr=False)]},
    )
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

    * **unfiltered** — this tool's call — merges every page into **one JSON array on
      one line**. That is the shape the fake serves everywhere else in this file, and
      the assertion here is that it reads as exactly the rows it contains (one issue,
      one PR) with a single queue call.
    * **`--jq`-filtered** — the shape `check-vote-count.py` records — is one JSON
      object per line, which is not one document. A call site that adds a filter must
      not turn the queue into an "unreadable payload" false alarm, so the per-line
      branch is pinned here.
    """
    fake = FakeGh(
        [_issue(10)],
        [_pr(20)],
        {10: [_refers_to(20, is_pr=True)], 20: [_refers_to(10, is_pr=False)]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 0, out
    assert "1 open issue(s), 1 open PR(s)" in out
    assert len([c for c in fake.calls if any("/issues?" in a for a in c)]) == 1

    # The filtered shape: each line its own JSON value, so the stream is not one
    # document and the per-line branch is the one that must answer.
    filtered = FakeGh(
        [],
        [],
        {10: [_refers_to(20, is_pr=True)], 20: [_refers_to(10, is_pr=False)]},
    )

    def routed(args: list[str]) -> str:
        target = next(a for a in args if a.startswith("repos/"))
        if "/issues?" in target:
            return "\n".join(json.dumps(row) for row in [[_issue(10)], [_pr(20)]])
        number = int(target.split("/issues/")[1].split("/")[0])
        return json.dumps(filtered.timelines[number])

    monkeypatch.setattr(mod, "_gh", routed)
    rc, out = _run(mod, capsys)

    assert rc == 0, out
    assert "1 open issue(s), 1 open PR(s)" in out


# --------------------------------------------------------------------------- #
# The states that are not `linked`
# --------------------------------------------------------------------------- #


def test_a_pr_side_mention_does_not_make_an_issue_name_it_back(mod, monkeypatch, capsys) -> None:
    """The PR names no issue, and the issue names the PR: `one-way`, never `linked`.

    This is the transposition trap. The mention is recorded on the **PR's** timeline
    only, so a tool that answered the PR's question from the issue's map would find
    the (nonexistent) reverse mention and report `linked`. The assertion is the state
    *and* the remedy that names the PR body, because that is the half that is missing.
    """
    fake = FakeGh(
        [_issue(10, "the problem")],
        [_pr(20, "the fix")],
        {10: [], 20: [_refers_to(10, is_pr=False)]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#10 issue ONE-WAY" in out
    assert "#20 PR ONE-WAY" in out
    assert "names it and this PR names no issue" in out
    assert "`Closes #N` where the PR finishes it" in out
    assert "#10 issue ok" not in out


def test_an_issue_naming_a_pr_that_does_not_reply_is_one_way_not_linked(
    mod, monkeypatch, capsys
) -> None:
    """The mirror: the PR references the issue, the issue never says so.

    Same rule, different reader — a reader of the issue is the one left looking for
    the work, so the remedy is a comment on the issue.
    """
    fake = FakeGh(
        [_issue(10, "the problem")],
        [_pr(20, "the fix")],
        {10: [_refers_to(20, is_pr=True)], 20: []},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#10 issue ONE-WAY" in out
    assert "#20 PR ONE-WAY" in out
    assert "#20 references it, and this issue never names #20" in out
    assert "gh issue comment 10" in out


def test_an_unclaimed_issue_with_no_references_says_nothing_was_opened(mod, monkeypatch, capsys) -> None:
    """An open issue nothing claims, and no merged PR ever claimed.

    The detail must not borrow the "landed and never closed" wording: there is no
    landed work to close it with, and the two remedies are different (open a PR vs
    close the issue).
    """
    fake = FakeGh([_issue(10, "untouched")], [], {10: []})
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#10 issue UNCLAIMED" in out
    assert "nothing has been opened for it" in out
    assert "landed and was never closed" not in out


def test_an_unclaimed_issue_names_the_merged_prs_that_already_referenced_it(
    mod, monkeypatch, capsys
) -> None:
    """The "landed and never closed" shape, which is the live queue's most common one.

    `#30` and `#40` are *closed* PRs that referenced the issue — the state that reads
    identically to "nothing was ever done" unless the tool distinguishes the two. It
    must name them, and the remedy must be the two-sided one.
    """
    fake = FakeGh(
        [_issue(10, "landed but open")],
        [],
        {10: [_refers_to(30, is_pr=True, state="closed"), _refers_to(40, is_pr=True, state="merged")]},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#30 #40 do(es)" in out
    assert "landed and was never closed" in out
    assert "nothing has been opened for it" not in out


def test_two_open_prs_on_one_issue_is_reported_as_duplicate(mod, monkeypatch, capsys) -> None:
    """The shape the host's third clause forbids: a second PR where an update belongs.

    Both PR numbers must appear, because the remedy is to fold one into the other and
    a reader cannot do that from a count.
    """
    fake = FakeGh(
        [_issue(10, "the problem")],
        [_pr(20, "one attempt"), _pr(21, "another attempt")],
        {
            10: [_refers_to(20, is_pr=True), _refers_to(21, is_pr=True)],
            20: [_refers_to(10, is_pr=False)],
            21: [_refers_to(10, is_pr=False)],
        },
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#10 issue DUPLICATE" in out
    assert "more than one open PR references it (#20, #21)" in out
    assert "updated in place, never replaced by a second one" in out


def test_a_pr_that_names_no_issue_is_unlinked(mod, monkeypatch, capsys) -> None:
    """An open PR belonging to no tracked problem, in both directions.

    Not `one-way`: there is nothing on either side, so the remedy is to name the issue
    rather than to answer a mention.

    The fixture carries the measured shape that makes this decidable: a PR's timeline
    really does hold `cross-referenced` events whose source is **another PR** (PR #1638
    carried #1633/#1637/#1640). Those are PR-to-PR mentions and say nothing about which
    issue owns this PR, so the reading must ignore them — a `pull_request` key read as
    "an issue names it back" would turn every cross-referenced PR into a `one-way` row.
    """
    fake = FakeGh(
        [],
        [_pr(20, "an orphan"), _pr(21, "a related PR")],
        {20: [_refers_to(21, is_pr=True)], 21: []},
    )
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#20 PR UNLINKED" in out
    assert "belongs to no tracked problem" in out
    assert "ONE-WAY" not in out


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
    assert "nothing has been opened for it" in out


def test_the_two_directions_are_read_from_opposite_sources_and_never_transposed(mod) -> None:
    """The unit-level pin on the trap the fixtures above are built around.

    Both helpers read `cross-referenced` events, and they must agree about which
    *kind* of source each is looking for: a source carrying `pull_request` is a PR
    referencing an issue, and one without it is an issue referencing a PR. Given the
    same two events, each helper must return exactly one of them - a helper that
    returned both would make every one-sided link read as mutual.

    `named_by_issue` is asserted separately because it is the inversion, and the
    inversion is where the two maps can be swapped without either helper being wrong:
    the issue-side verdict reads `named_by_issue(stated_by_pr)[issue]`, so an inverse
    that returned `{20: {10}}` instead of `{10: {20}}` would answer "does this issue
    name its PR?" from the wrong subject's row.
    """
    events = [_refers_to(20, is_pr=True, state="open"), _refers_to(10, is_pr=False)]

    assert mod.referencing_prs(events) == {20: "open"}
    assert mod.referencing_issues(events) == {10}
    assert mod.named_by_issue({20: {10}}) == {10: {20}}
    # A PR that referenced nothing is `{}`, not `{0: ...}` or a row of `None`.
    assert mod.referencing_prs([]) == {}
    assert mod.referencing_issues([]) == set()
    assert mod.named_by_issue({}) == {}


def test_a_pr_referenced_by_nothing_reads_unlinked_and_the_queue_is_named(
    mod, monkeypatch, capsys
) -> None:
    """The boundary in the tool's docstring, stated as an assertion.

    The subject is the **open** queue: a PR whose only mention was in an issue that
    has since closed leaves no event on any subject this run reads, so it reads
    `unlinked` rather than silently counting as linked-by-a-closed-issue. The remedy
    text is asserted too, because "unlinked" without a next action is just a label.
    """
    fake = FakeGh([], [_pr(20, "the fix")], {20: []})
    _install(mod, monkeypatch, fake)

    rc, out = _run(mod, capsys)

    assert rc == 1, out
    assert "#20 PR UNLINKED" in out
    assert "0 open issue(s), 1 open PR(s)" in out
    assert "name the issue it belongs to in the PR body" in out


def test_an_all_linked_queue_exits_zero_and_says_what_it_read(mod, monkeypatch, capsys) -> None:
    """Two linked pairs: every row `linked`, and the count of subjects is in the OK line.

    The count is the evidence that the verdict covered the queue rather than a
    single pair — a tool that read one subject and stopped would print the same words.
    """
    fake = FakeGh(
        [_issue(10, "one"), _issue(11, "two")],
        [_pr(20, "a"), _pr(21, "b")],
        {
            10: [_refers_to(20, is_pr=True)],
            11: [_refers_to(21, is_pr=True)],
            20: [_refers_to(10, is_pr=False)],
            21: [_refers_to(11, is_pr=False)],
        },
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
    PR that may well name an issue — a confident wrong verdict, which is the failure
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
        [_issue(10)],
        [_pr(20)],
        {10: [_refers_to(20, is_pr=True)], 20: []},
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
    assert payload["rows"][0]["title"] == "an issue"


def test_the_summary_lists_the_offenders_in_the_printed_order(mod, monkeypatch, capsys) -> None:
    """The summary line follows the body, so a reader can walk the report downwards.

    Asserted as a sequence rather than a set: the rows print issues (ascending) then
    PRs, and a summary sorted differently is the small friction that makes a reader
    re-scan a 13-row report by hand.
    """
    fake = FakeGh(
        [_issue(10), _issue(11)],
        [_pr(20)],
        {10: [], 11: [_refers_to(20, is_pr=True)], 20: []},
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
