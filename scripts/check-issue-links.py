#!/usr/bin/env python3
"""Check that every open issue and every open pull request are linked to each other.

The rule, and where it came from
--------------------------------
Host 2026-09-26T18:52:57 (`submit_rant`, project `emrg`):

> 每个issue应该在一个pr里处理完毕。每个pr都应该明确对应哪个issue，并在对应的issue里
> 说明。也就是双方应该有明确的双向连接。发生pr被拒绝或者要求更正时，应该还是在这个pr里更新。

*One issue is finished by exactly one PR; every PR says which issue it belongs to,
and says so in that issue; the link is bidirectional; a rejected or change-requested
PR is updated in place.* This tool is the reading of that rule, because nothing read
it and the consequence was measurable: measured 2026-09-26 (`cyc20260926-165037`),
the repo went **43 cycles / 32.3 hours** merging **28 PRs** while closing **0
issues**, with four issues 99 cycles old. Every one of those issues had been
*re-measured* by later cycles; none of them had been *claimed*.

A claim is not a mention, and the first version of this tool got that wrong
--------------------------------------------------------------------------
The first version treated *any* `cross-referenced` event from a PR as that PR claiming
the issue. Measured on this tool's own pull request (#1643, the day it was written): its
body cites #1551, #1553, #1598 and #1606 as evidence while claiming only #1642, and the
reading reported **#1553, #1598 and #1606 as claimed by #1643** — a PR that finishes
none of them. A citation in prose and a claim are different acts, and only one of them
is what the rule is about.

What separates them is not a heuristic invented here: it is GitHub's own **closing
keyword** contract (`close`/`closes`/`closed`, `fix`/`fixes`/`fixed`,
`resolve`/`resolves`/`resolved`, followed by the reference) — the form GitHub itself
acts on, and honours by closing the issue when the PR merges. So the rule this tool
enforces is: *a PR claims an issue when its body carries a closing keyword for it.* A
body written that way is one GitHub will close the issue for; a body that merely
mentions a number is not, and neither reading nor platform is guessing.

Measured against the live queue the same day, which is why the distinction matters
rather than being pedantic: of the two open PRs naming #1606, **#1638's body ends
`Closes #1606.`** while **#1641's body says `Issue #1606:` and declares no claim** — a
difference no mention-based reading can see, and the difference between the issue being
claimed and merely discussed.

Both directions are read from GitHub's own resolved cross-references
---------------------------------------------------------------------
GitHub resolves `#N` for us, so no second parser can disagree about whether a number
counts as a reference — and the same event is readable from both ends, which is what
makes "one-way" measurable rather than guessed:

* on **issue N's** timeline, a `cross-referenced` event whose source **is a PR** means
  that PR referenced N; the event carries the referrer's full body, so whether it
  *declared* the claim is decided from the same event with no extra call;
* on **PR P's** timeline, a `cross-referenced` event whose source **is not a PR** means
  that issue referenced P in its own text (body or comment).

So a link that exists in one direction only is visible as exactly that. The measurement
that separated the two directions, before this tool existed: PR **#1607**'s timeline
carries a cross-reference from issue **#1553** (written in a comment on the issue, not
in its body), while PR **#1616**'s carries none although #1616's body names #1551 — a
one-way link. That pair is also what settles the first question a reader asks: a mention
in an issue **comment** counts, so "handled by #N" written under the issue is a real
second half and not something the tool is blind to.

The two shapes the rule itself produces, both in the live queue on the day it was read:
an issue whose work **landed and was never closed** (#1553, #1554, #1556, #1560, #1598,
each referenced by a merged PR and by no open one), and an issue with **two open PRs
declaring it** — the shape the rule forbids outright, since a rejected or
change-requested PR is updated in place rather than answered by a second one.

What it reads, and the boundary of that reading
-----------------------------------------------
One call lists the open issues and the open PRs together (`/issues` returns both, and
the `pull_request` key is the discriminator), then one timeline call per open issue and
per open PR. Boundary facts, stated rather than implied:

* the subject is the **open** queue. A PR that references only a *closed* issue reads
  `unlinked`, and an issue naming a *closed* PR is not counted as naming a live one —
  both say so in the row rather than claiming the number is absent;
* a timeline is read to 100 events per page, paginated, and truncation is not silently
  treated as the whole history. What `--paginate` actually prints was measured rather
  than assumed (see `_paged_json`);
* **commit messages are not read.** GitHub also honours a closing keyword in a commit,
  so a PR whose claim lives only in a commit message reads as claiming nothing here. The
  remedy is the same as for any other missing declaration (put it in the body), and this
  limit is stated because the tool would otherwise look like it had read something it
  did not.

States
------
    linked       the reading is complete in both directions
    unclaimed    no open PR declares the issue. The row names what it can see instead:
                 closed PRs that declared it (the "landed and never closed" shape), or
                 PRs that referenced it without declaring (a mention is not a claim)
    duplicate    more than one open PR declares the same open issue
    one-way      one side names the other and is not named back. Both directions get it,
                 because they have different readers: a PR that declares no issue leaves
                 the issue's reader looking, and an issue that never names its PR leaves
                 the PR's reader looking
    unlinked     an open PR that points at no open issue: it declares nothing, or it
                 declares a number that is not among the open issues (a closed issue, or
                 a PR number with the wrong keyword form). The row says which

Exit codes
----------
0  every open issue has exactly one open PR declaring it, each declaration is named back
   in the issue, and every open PR declares an open issue
1  at least one row is in a state above that is not `linked`
2  the question could not be answered (gh failed, a payload did not parse) — never
   reported as a pass, because a clean queue and an unreadable one are different
   answers and only one of them is evidence

What a non-declaring reference is, and why it is three readings rather than one
--------------------------------------------------------------------------------
A reference that does not declare is evidence of attention, never of ownership — but
"attention" is not one fact, and folding the three together was a defect this tool
shipped (issue #1644, fixed on this branch). It printed *"nothing has been opened for
it"* for five of the nine open issues while **merged** PRs referenced them (#1553 by
#1565/#1569/#1607, #1554 by six, #1556 by #1614, #1560 by #1589, #1598 by
#1599/#1600), so an idle issue and one whose work had landed on master read
identically. So each referrer is classified by what it is, from the same event:

* **open** — a PR in flight, and the sentence above is exactly right for it;
* **landed** (`pull_request.merged_at` set) — work on master carrying this number that
  never closed the issue: either close it with that reading, or state what remains;
* **abandoned** (closed, never merged) — an attempt that ended, which the host's rule
  answers in its own PR rather than with a second one.

`merged_at` is the discriminator and `state` is not, because `state` is `closed` for a
merge, for an abandoned PR and for a closed *issue* alike — the three are distinguished
by the merge alone, and reading the state would call abandoned work landed.

A **quoted** keyword declares nothing
-------------------------------------
Code spans and fenced blocks are blanked out before a claim is read, because a body that
quotes a closing keyword is not making one — and this is measured, not theoretical: this
tool's own pull request (#1643) quotes `Closes #1606.` while explaining the
claim/mention distinction, and the quotation made issue **#1606 read `DUPLICATE`**,
claimed by #1643 as well as by #1638. A tool that documents the syntax it reads quotes
it, so the input is the family's normal case. The masking stops early, the same direction
as the list and negation bounds: a missed claim is a loud row a reader can fix, an
invented one is silent.

A note on this docstring
------------------------
It quotes the host verbatim, so it is not ASCII. That is safe only because the script
never prints it: the argparse description is an explicit ASCII string and the token it
would take to print a docstring appears nowhere in this file. If someone ever wires it
up as help text, `tests/test_script_output_ascii.py` is the rule that will say so — and
the fix is to move the quote, not to delete the rule.

Usage
-----
    uv run --no-sync python3 scripts/check-issue-links.py
    uv run --no-sync python3 scripts/check-issue-links.py --repo owner/name --json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

#: The repo's own name, so a bare run answers about the queue a cycle is standing in.
DEFAULT_REPO = "argszero/emrg"

#: Events are read a page at a time; a timeline longer than this is paginated rather
#: than truncated, and the cap exists only so a pathological issue cannot loop forever.
_TIMELINE_PER_PAGE = 100

#: GitHub's closing keywords, and the reference list that follows one. This is the
#: platform's own auto-close contract, quoted rather than approximated: GH links and
#: closes on exactly these verbs. A bare mention is not matched, because a mention is
#: not a claim (the incident is in the module docstring).
_CLOSING_KEYWORD = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]*((?:#[0-9]+[\s,]*)+)"
)

#: A keyword the clause *negates* declares nothing, and this is the one place the
#: reading departs from a plain regex over the body: "it is not closed: #1 has no
#: handler" carries both a keyword and a reference, and counting it would report a link
#: that does not exist — the silent direction, which is the failure this tool exists to
#: prevent. The window is deliberately small and syntactic: a negation within two words
#: of the keyword, in the same clause (a comma, stop or newline ends it). That bound is
#: stated rather than implied — "we should not forget that this closes #1" reads as a
#: claim here, and it is one a reader can see in the report rather than a silent
#: invention, so the reading errs toward claiming only when a writer insisted.
_NEGATED_KEYWORD = re.compile(
    r"(?i)(?:\bnot\b|\bnever\b|\bno\b|\bwithout\b|\bcannot\b|\bcan't\b|\bwon't\b"
    r"|\bdon't\b|\bdoesn't\b|\bisn't\b|\baren't\b)\s+(?:\w+\s+){0,2}$"
)

#: Code spans and fenced blocks are masked out before a claim is read, because a body
#: that *quotes* a closing keyword is not making one. This is the second live
#: self-caught defect (issue #1644's family): while this tool's own pull request
#: (#1643) explained the claim/mention distinction it wrote the sentence "of the two
#: open PRs naming #1606, #1638's body ends `Closes #1606.` while …" — and the reading
#: reported #1643 as **declaring** #1606, making issue #1606 read `DUPLICATE` (claimed
#: by both #1638 and #1643) when the second claim existed only inside backticks. A
#: quotation is the shape a tool's own documentation takes, so this is not an exotic
#: input for this family.
#:
#: The direction is deliberate and is the same one the list and negation bounds take:
#: masking **stops early**, which costs a loud `UNCLAIMED` row on a PR that does claim
#: the issue, while reading the quotation **invents a link** — a silent falsehood, and
#: the one direction this tool exists to prevent. What is genuinely unmeasured is what
#: GitHub's own parser does with a keyword inside a code span; if it honours it, this
#: reading under-claims, and the symptom is visible on the row rather than hidden. The
#: masking is length-preserving so the negation window and match offsets keep pointing
#: at the same characters.
_FENCED_BLOCK = re.compile(r"^[ \t]*(?:```|~~~).*?(?:^[ \t]*(?:```|~~~)[ \t]*$|\Z)", re.S | re.M)
_INLINE_CODE = re.compile(r"`[^`\n]*`|``.*?``", re.S)


def _without_code(text: str) -> str:
    """`text` with inline code spans and fenced blocks blanked out, same length."""
    masked = _FENCED_BLOCK.sub(lambda m: " " * len(m.group(0)), text)
    return _INLINE_CODE.sub(lambda m: " " * len(m.group(0)), masked)


@dataclass
class Row:
    """One subject's verdict: what it is, what state it is in, and why."""

    kind: str  # "issue" | "pr"
    number: int
    title: str
    state: str
    detail: str
    age_days: float | None = None

    @property
    def clean(self) -> bool:
        return self.state == "linked"


@dataclass
class Queue:
    """The open queue, as the API returned it."""

    issues: list[dict] = field(default_factory=list)
    prs: list[dict] = field(default_factory=list)

    @property
    def issue_numbers(self) -> set[int]:
        return {int(row["number"]) for row in self.issues}


@dataclass
class Refs:
    """What one issue's timeline says about the PRs that touched it.

    Three disjoint readings of the same event stream, split apart because they carry
    three different meanings and only the first one is a claim:

    * `declared_open` - open PRs whose body carries a closing keyword for this issue:
      the PRs that claim it, and the only set that decides the issue's state;
    * `declared_closed` - closed or merged PRs that declared it. A merged declarer whose
      issue is still open is the anomaly worth naming (the keyword should have closed
      it), and a closed unmerged declarer is simply abandoned work;
    * `mentioned_open`, `mentioned_landed`, `mentioned_abandoned` - PRs that referenced
      the issue **without** declaring it, split three ways because the reader's next move
      differs for each, and folding them together is a defect this tool shipped (issue
      #1644): "nothing has been opened for it" was printed for five issues that merged
      PRs referenced. Evidence of attention, never of ownership - but "attention" is not
      one fact. An **open** referrer is a PR in flight; a **landed** one is work on
      master that never closed the issue, which is the shape that has to be either closed
      with a reading or answered with what remains; an **abandoned** one is an attempt
      that was closed unmerged, and the host's rule of 2026-09-26 names what should
      happen to it (a rejected or change-requested PR is updated in place, never
      replaced).
    """

    declared_open: set[int] = field(default_factory=set)
    declared_closed: set[int] = field(default_factory=set)
    mentioned_open: set[int] = field(default_factory=set)
    mentioned_landed: set[int] = field(default_factory=set)
    mentioned_abandoned: set[int] = field(default_factory=set)


def _gh(args: list[str]) -> str:
    """Run `gh`, failing loud: an unreadable queue is not an empty one."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:  # pragma: no cover - the CI legs install gh
        raise RuntimeError(f"gh is not available on this machine ({exc})") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh failed (rc={proc.returncode}): gh {' '.join(args)}\n"
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def declared_claims(body: str | None) -> set[int]:
    """The issue numbers a body declares it closes, by GitHub's closing keywords.

    `None` (a body GitHub reports as null) declares nothing, which is the same answer
    as an empty body and is why this takes `str | None` rather than making every caller
    coalesce it.

    Public on purpose: this is the reading every caller must agree with, and the tests
    drive it directly so that "a citation is not a claim" is pinned at the unit level
    and not only through a whole report.

    The text a keyword is read from has its code spans and fenced blocks blanked out
    first (`_without_code`), because a quoted keyword is not a claim — the second live
    defect this reading caught in itself, and the reason both the masking and the
    negation window are asserted in the tests rather than described.
    """
    text = _without_code(body or "")
    claims: set[int] = set()
    for match in _CLOSING_KEYWORD.finditer(text):
        if _NEGATED_KEYWORD.search(text[: match.start()]):
            continue
        claims.update(int(n) for n in re.findall(r"#(\d+)", match.group(1)))
    return claims


def _paged_json(args: list[str]) -> list[dict]:
    """Parse `gh api --paginate` output.

    **Measured, because the family's comment on this shape is about a filtered call
    and does not hold for this one.** With no `--jq`, gh merges the pages of a list
    endpoint into a **single JSON array on a single line**: measured 2026-09-26,
    `gh api "repos/argszero/emrg/issues?state=open&per_page=2" --paginate` (4 pages
    over 12 open subjects) printed one 83,032-byte line that `json.loads` reads as one
    array of 12. So one document is the real shape here.

    The per-line branch is kept for the *other* measured shape
    (`check-vote-count.py::_gh_json_paginated` records it): `--paginate` with a
    `.[] | {…}` filter emits one JSON object per line, because concatenated page
    arrays filtered item-by-item are not a JSON document. This tool passes no filter
    today, and a call site that adds one must not turn the queue into "unreadable
    payload" - which is exit 2, a false alarm, and worth one defensive branch. A line
    that is itself an array is flattened rather than kept whole, since `--jq` omits the
    `.[]` just as often as it is given it, and a list of lists would then reach the
    caller as rows that are not issue rows.

    A payload that parses to neither is not "no rows": it raises, and `main` reports
    it unmeasurable rather than as an empty queue.
    """
    out = _gh([*args, "--paginate"])
    try:
        payload: object = json.loads(out)
    except json.JSONDecodeError:
        rows: list[dict] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            doc = json.loads(line)
            rows.extend(doc if isinstance(doc, list) else [doc])
        payload = rows
    if not isinstance(payload, list):
        raise RuntimeError(f"expected a list from gh {' '.join(args)}")
    return payload


def load_queue(repo: str) -> Queue:
    """The open issues and open PRs, from the one endpoint that returns both."""
    rows = _paged_json(
        [
            "api",
            f"repos/{repo}/issues?state=open&per_page={_TIMELINE_PER_PAGE}",
        ]
    )
    queue = Queue()
    for row in rows:
        for field_ in ("number", "title", "created_at"):
            if field_ not in row:
                raise RuntimeError(f"an issue row is missing {field_!r}: {row}")
        if "pull_request" in row:
            queue.prs.append(row)
        else:
            queue.issues.append(row)
    return queue


def timeline(repo: str, number: int) -> list[dict]:
    """Every event on issue-or-PR `number`'s timeline (paginated)."""
    return _paged_json(
        ["api", f"repos/{repo}/issues/{number}/timeline?per_page={_TIMELINE_PER_PAGE}"]
    )


def referencing_prs(events: list[dict], issue_number: int) -> Refs:
    """The PRs that referenced this issue, split into claims and the three mentions.

    Read off an **issue's** timeline: the subject is the issue, so a `cross-referenced`
    event's source is the *referrer*, and one whose source carries a `pull_request`
    object is a PR. An issue referencing another issue is skipped by that same test
    rather than by a number range.

    The source's `body` is what decides claim vs mention, and it is present on the
    event - measured on a live event, not assumed - so the split costs no extra call.
    A source with no body at all (or no `state`) is treated as a mention and as not
    open, never as a claim: an unreadable referrer must not be able to claim an issue.

    A non-claiming referrer is then classified by what it *is*, from the same event
    (measured 2026-09-26 on `issues/1553/timeline`: the source carries
    `pull_request.merged_at` - `#1565` → `2026-09-24T05:24:54Z`, `#1569` →
    `2026-09-24T10:48:24Z`, `#1607` → `2026-09-25T06:07:15Z` - and it is `null` for a
    referrer that was only closed). `merged_at` is the discriminator rather than
    `state`, because `state` is `closed` for an abandoned PR and for a closed *issue*
    alike, while only a merge says the change is on master.
    """
    refs = Refs()
    for event in events:
        if event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        pull = source.get("pull_request")
        if not isinstance(pull, dict):
            continue
        number = source.get("number")
        if number is None:
            continue
        pr = int(number)
        # A PR that is gone or unreadable reports no state; `unknown` is treated as
        # "not open" and never as "open".
        open_pr = str(source.get("state") or "unknown") == "open"
        if issue_number in declared_claims(source.get("body")):
            (refs.declared_open if open_pr else refs.declared_closed).add(pr)
        elif open_pr:
            refs.mentioned_open.add(pr)
        elif pull.get("merged_at"):
            refs.mentioned_landed.add(pr)
        else:
            refs.mentioned_abandoned.add(pr)
    return refs


def referencing_issues(events: list[dict], open_issues: set[int]) -> set[int]:
    """The **open** issues that named this PR, from the PR's own timeline.

    Read off a **PR's** timeline: the subject is the PR, so a `cross-referenced` event
    whose source has no `pull_request` object is an issue that named the PR in its own
    text - the second half of the bidirectional link.

    Restricted to the open issues this run read. A closed issue's mention of a PR is
    history: reporting it as "the issue names it back" would invent a live link out of
    an archived one, and reporting its absence as a fault would ask a reader to edit an
    issue that is already closed.
    """
    found: set[int] = set()
    for event in events:
        if event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        if "pull_request" in source:
            continue
        number = source.get("number")
        if number is not None and int(number) in open_issues:
            found.add(int(number))
    return found


def age_days(created_at: str, now: dt.datetime | None = None) -> float | None:
    """The subject's age in days, or `None` when its timestamp will not parse."""
    try:
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - created).total_seconds() / 86400.0


def named_by_issue(stated_by_pr: dict[int, set[int]]) -> dict[int, set[int]]:
    """Invert the PR-side reading: for each issue, the open PRs that issue names.

    The second direction of the link is recorded on the **PR's** timeline, so the
    issue-side verdict has to read it inverted. Kept as a named step rather than a
    comprehension inside `judge_issues`, because the inversion is the one place the two
    directions can be transposed by accident - and transposing them would make every
    link look bidirectional.
    """
    inverted: dict[int, set[int]] = {}
    for pr, issues in stated_by_pr.items():
        for issue in issues:
            inverted.setdefault(issue, set()).add(pr)
    return inverted


def _numbers(values) -> str:
    return " ".join(f"#{n}" for n in sorted(values))


def _mention_detail(refs: Refs, number: int) -> str:
    """What a reader should do about each class of non-declaring referrer.

    The measured reason this is three clauses rather than one (issue #1644): folding
    them together printed *"nothing has been opened for it"* for five open issues that
    **merged** PRs referenced, so an idle issue and one whose work landed read
    identically - the ambiguity a backlog of never-closed issues is made of.

    The landed clause comes first because it is the one that changes what a reader does
    next: a merged referrer means the change is on master, so the question is whether
    that *was* the remedy (close it with the reading) or not (say what remains). The
    abandoned clause is second for the same reason from the other side - an attempt
    died, and the host's rule of 2026-09-26 says it is answered in its own PR rather
    than replaced. The open clause keeps the original sentence, which is still exactly
    right for a PR in flight.

    Every class present is named: an issue can carry all three at once, and a reader who
    is told only the first would close an issue over landed work that does not finish
    it.
    """
    clauses: list[str] = []
    if refs.mentioned_landed:
        clauses.append(
            f"{_numbers(refs.mentioned_landed)} referenced it and are merged, and "
            f"neither declares `Closes #{number}` - work has landed on master carrying "
            "this number, so either close this issue with the reading that says that "
            "was the remedy, or state here what it still leaves"
        )
    if refs.mentioned_abandoned:
        clauses.append(
            f"{_numbers(refs.mentioned_abandoned)} referenced it and were closed "
            f"without merging and without declaring `Closes #{number}` - an attempt "
            "that ended, which the rule answers in its own PR (a rejected or "
            "change-requested PR is updated in place, never replaced by a second one)"
        )
    if refs.mentioned_open:
        clauses.append(
            f"{_numbers(refs.mentioned_open)} referenced it without declaring "
            f"`Closes #{number}` - a mention is not a claim, so nothing has been "
            "opened for it by them"
        )
    return "; ".join(clauses)


def judge_issues(
    issues: list[dict],
    refs_by_issue: dict[int, Refs],
    stated_by_pr: dict[int, set[int]],
    now: dt.datetime | None = None,
) -> list[Row]:
    """A verdict per open issue: is exactly one open PR declaring it, and does it say so?"""
    named = named_by_issue(stated_by_pr)
    rows: list[Row] = []
    for issue in issues:
        number = int(issue["number"])
        refs = refs_by_issue.get(number, Refs())
        claiming = sorted(refs.declared_open)
        age = age_days(issue.get("created_at", ""), now)
        if not claiming:
            names = sorted(named.get(number, ()))
            if names:
                # An issue that names a PR which does not declare it back. Reachable
                # because the two directions are recorded separately: a comment on the
                # issue creates the PR-side event, and a PR body that declares nothing
                # creates nothing on this side. Not `unclaimed` - there is a trace, and
                # it is one-sided, which is a different remedy.
                rows.append(
                    Row(
                        "issue",
                        number,
                        issue["title"],
                        "one-way",
                        f"this issue names {_numbers(names)} and no open PR declares it "
                        "back - the declaration belongs in the PR body (`Closes #N` "
                        "where it finishes this), because that is the form GitHub acts "
                        "on and the form a reader looks for",
                        age,
                    )
                )
                continue
            if refs.declared_closed:
                detail = (
                    f"{_numbers(refs.declared_closed)} declared `Closes #{number}` and "
                    "are merged or closed, which is the shape of work that landed and "
                    "was never closed - either close this issue with the reading that "
                    "says the work is done, or open the PR that finishes it"
                )
            elif refs.mentioned_landed or refs.mentioned_abandoned or refs.mentioned_open:
                detail = _mention_detail(refs, number)
            else:
                detail = "nothing has been opened for it"
            rows.append(Row("issue", number, issue["title"], "unclaimed", detail, age))
            continue
        if len(claiming) > 1:
            rows.append(
                Row(
                    "issue",
                    number,
                    issue["title"],
                    "duplicate",
                    "more than one open PR declares it ("
                    + ", ".join(f"#{p}" for p in claiming)
                    + ") - one issue is finished by one PR, so keep one and fold the "
                    "other into it (a rejected or change-requested PR is updated in "
                    "place, never replaced by a second one)",
                    age,
                )
            )
            continue
        pr = claiming[0]
        if number in stated_by_pr.get(pr, set()):
            rows.append(
                Row(
                    "issue",
                    number,
                    issue["title"],
                    "linked",
                    f"#{pr} declares it and this issue names #{pr} back",
                    age,
                )
            )
        else:
            rows.append(
                Row(
                    "issue",
                    number,
                    issue["title"],
                    "one-way",
                    f"#{pr} declares it, and this issue never names #{pr} - state it "
                    f"here (`gh issue comment {number} -R {DEFAULT_REPO} --body "
                    f"'handled by #{pr}'`), because a reader of this issue is the one "
                    "who needs to reach the work",
                    age,
                )
            )
    return rows


def judge_prs(
    prs: list[dict],
    issue_numbers: set[int],
    stated_by_pr: dict[int, set[int]],
    now: dt.datetime | None = None,
) -> list[Row]:
    """A verdict per open PR: does it declare an open issue, and is the link mutual?"""
    rows: list[Row] = []
    for pr in prs:
        number = int(pr["number"])
        declared = declared_claims(pr.get("body"))
        outbound = declared & issue_numbers
        elsewhere = declared - issue_numbers
        inbound = stated_by_pr.get(number, set())
        age = age_days(pr.get("created_at", ""), now)
        if not outbound and not inbound:
            if elsewhere:
                # The PR does declare something - it is just not an open issue this run
                # read. Naming it is the difference between "belongs to no tracked
                # problem" and "belongs to a number the reader can go and look at".
                detail = (
                    f"it declares {_numbers(elsewhere)} and that is not among the open "
                    "issues, so it finishes nothing this reading can see (a closed "
                    "issue already closed by another PR, or a PR number written with "
                    "the closing form) - declare the open issue it finishes"
                )
            else:
                detail = (
                    "no open issue names it and it declares none - name the issue it "
                    "belongs to in the PR body (`Closes #N` where the PR finishes it), "
                    "because an issue is the unit of work here and a PR that declares "
                    "none belongs to no tracked problem"
                )
            rows.append(Row("pr", number, pr["title"], "unlinked", detail, age))
            continue
        if outbound:
            missing = sorted(outbound - inbound)
            if missing:
                rows.append(
                    Row(
                        "pr",
                        number,
                        pr["title"],
                        "one-way",
                        "it declares "
                        + _numbers(outbound)
                        + " and "
                        + (
                            "none of those issues names it back"
                            if len(missing) == len(outbound)
                            else f"{_numbers(missing)} does not name it back"
                        )
                        + " - post the link in the issue (the rule is explicitly "
                        "bidirectional, and a reader of the issue is the one who needs "
                        "it)",
                        age,
                    )
                )
                continue
            rows.append(
                Row(
                    "pr",
                    number,
                    pr["title"],
                    "linked",
                    "declares " + _numbers(outbound) + ", named back in the issue",
                    age,
                )
            )
            continue
        # No open issue is declared, and something named this PR: the mirror case, and
        # not symmetric with the one above - a reader of the *PR* is the one who has to
        # know which tracked problem it belongs to. Same rule, different reader.
        rows.append(
            Row(
                "pr",
                number,
                pr["title"],
                "one-way",
                _numbers(inbound)
                + " names it and this PR declares no issue - state it in the PR body "
                "(`Closes #N` where the PR finishes it), because the issue is the unit "
                "of work here and the PR is where its reader looks next",
                age,
            )
        )
    return rows


def collect(repo: str) -> tuple[Queue, dict[int, Refs], dict[int, set[int]]]:
    """Every reading the two verdicts need, with one timeline call per subject.

    The PR-side "which open issues name this PR" is read from each PR's **own** timeline
    (that is the direction in which GitHub records it), so the two maps come from
    complementary reads rather than from one read interpreted two ways.
    """
    queue = load_queue(repo)
    refs_by_issue: dict[int, Refs] = {}
    for issue in queue.issues:
        number = int(issue["number"])
        refs_by_issue[number] = referencing_prs(timeline(repo, number), number)
    stated_by_pr: dict[int, set[int]] = {}
    for pr in queue.prs:
        number = int(pr["number"])
        stated_by_pr[number] = referencing_issues(
            timeline(repo, number), queue.issue_numbers
        )
    return queue, refs_by_issue, stated_by_pr


def _age_text(row: Row) -> str:
    return f"{row.age_days:.1f}d" if row.age_days is not None else "age unreadable"


def render(row: Row) -> str:
    """One subject's block: what is true, then the remedy."""
    label = "issue" if row.kind == "issue" else "PR"
    mark = {"linked": "ok", "one-way": "ONE-WAY", "unclaimed": "UNCLAIMED",
            "duplicate": "DUPLICATE", "unlinked": "UNLINKED"}.get(row.state, row.state.upper())
    return (
        f"#{row.number} {label} {mark} ({_age_text(row)}) - {row.title}\n"
        f"    {row.detail}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check the bidirectional link between open issues and open PRs: one issue "
            "is finished by one PR, and each names the other. A claim is a GitHub "
            "closing keyword in the PR body, not a mention."
        )
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name")
    parser.add_argument(
        "--json", action="store_true", help="emit the rows as one JSON document"
    )
    args = parser.parse_args(argv)

    try:
        queue, refs_by_issue, stated_by_pr = collect(args.repo)
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"cannot determine the issue/PR links: {exc}", file=sys.stderr)
        return 2

    rows = judge_issues(queue.issues, refs_by_issue, stated_by_pr) + judge_prs(
        queue.prs, queue.issue_numbers, stated_by_pr
    )
    bad = [row for row in rows if not row.clean]

    if args.json:
        print(
            json.dumps(
                {
                    "repo": args.repo,
                    "open_issues": len(queue.issues),
                    "open_prs": len(queue.prs),
                    "clean": len(rows) - len(bad),
                    "rows": [
                        {
                            "kind": row.kind,
                            "number": row.number,
                            "state": row.state,
                            "title": row.title,
                            "detail": row.detail,
                            "age_days": row.age_days,
                        }
                        for row in rows
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        # The subject before any verdict, the family's convention for a report whose
        # reader has to know what was read: an empty queue and an unread one differ.
        print(
            f"repo: {args.repo}, {len(queue.issues)} open issue(s), "
            f"{len(queue.prs)} open PR(s)"
        )
        ordered = sorted(rows, key=lambda r: (r.kind != "issue", r.number))
        for row in ordered:
            print(render(row))
        if not bad:
            print(
                f"\nOK: every open issue has exactly one open PR declaring it, each "
                f"declaration is named back in the issue, and every open PR declares an "
                f"open issue ({len(rows)} subject(s) read)"
            )
        else:
            print(
                f"\n{len(bad)} of {len(rows)} subject(s) are not linked: "
                # The same order the rows were printed in, so a reader can follow
                # the summary down the report instead of re-sorting it by hand.
                + ", ".join(
                    f"#{row.number} {row.state}"
                    for row in ordered
                    if not row.clean
                )
            )
            print(
                "One issue is finished by one PR, and each names the other; a PR that "
                "is rejected or asked to change is updated in place, never replaced."
            )

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
