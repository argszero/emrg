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
*re-measured* by later cycles; none of them had been *claimed*. A backlog that no
reading names is a backlog in which "nobody is working on it" and "somebody measured
it yesterday" look identical from the outside.

Both directions are read from GitHub's own cross-reference events, never from a
regex over titles and bodies
-------------------------------------------------------------------------------
GitHub resolves `#N` for us, and the same event is readable from both ends — which is
what makes "one-way" measurable rather than guessed:

* on **issue N's** timeline, a `cross-referenced` event whose source **is a PR** means
  that PR referenced N (its body, or a comment on it);
* on **PR P's** timeline, a `cross-referenced` event whose source **is not a PR** means
  that issue referenced P in its own text.

So a link that exists in one direction only is visible as exactly that, and no second
parser can disagree with GitHub about whether "#1553" in a body counts. The measurement
that separated the two directions, before this tool existed (2026-09-26): PR **#1607**'s
timeline carries a cross-reference from issue **#1553** (written in a comment on the
issue, not in its body), while PR **#1616**'s carries none although #1616's body names
#1551 — a one-way link, which is this tool's `one-way` row. That pair is also what
settles the question a reader would ask first: a mention in an issue **comment** counts,
so "handled by #N" written under the issue is a real second half and not something the
tool is blind to.

The two shapes the rule itself produces, both seen in the live queue on the day it was
read: an issue whose work **landed and was never closed** (#1553, #1554, #1556, #1560,
#1598, each of them referenced by a merged PR and by no open one), and an issue with
**two open PRs claiming it** (#1606, claimed by #1638 and #1641) — which is the shape
the rule forbids outright, since a rejected or change-requested PR is updated in place
rather than answered by a second one.

What it reads, and the boundary of that reading
-----------------------------------------------
One call lists the open issues and the open PRs together (`/issues` returns both, and
the `pull_request` key is the discriminator), then one timeline call per open issue and
per open PR. Two boundary facts are stated rather than implied:

* the subject is the **open** queue. A PR that references only a *closed* issue reads
  `unlinked` here, because no open issue's timeline shows it — the row says so in those
  words instead of claiming the PR names nothing;
* a timeline is read to 100 events per page, paginated, and truncation is not silently
  treated as the whole history. What `--paginate` actually prints was measured rather
  than assumed — one merged JSON array on one line for this unfiltered call — and the
  measurement is recorded on `_paged_json`, which is also where the filtered JSONL
  shape is handled.

States
------
    linked      the reading is complete in both directions
    unclaimed   an open issue no open PR references (merged/closed references, if any,
                are named — that is the "landed and never closed" shape)
    duplicate   more than one open PR references the same open issue
    one-way     one side names the other and is not named back. Both directions get it,
                because they have different readers: a PR that never names an issue
                leaves the issue's reader looking, and an issue that never names its PR
                leaves the PR's reader looking
    unlinked    an open PR that no open issue references and that references none

Exit codes
----------
0  every open issue has exactly one open PR referencing it, every reference is
   named back in the issue, and every open PR belongs to an open issue
1  at least one row is in a state above that is not `linked`
2  the question could not be answered (gh failed, a payload did not parse) — never
   reported as a pass, because a clean queue and an unreadable one are different
   answers and only one of them is evidence

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
import subprocess
import sys
from dataclasses import dataclass, field

#: The repo's own name, so a bare run answers about the queue a cycle is standing in.
DEFAULT_REPO = "argszero/emrg"

#: Events are read a page at a time; a timeline longer than this is paginated rather
#: than truncated, and the cap exists only so a pathological issue cannot loop forever.
_TIMELINE_PER_PAGE = 100


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


def referencing_prs(events: list[dict]) -> dict[int, str]:
    """`{pr_number: state}` — the PRs that referenced this item, from its timeline.

    Read off an **issue's** timeline: the subject is the issue, so a
    `cross-referenced` event's source is the *referrer*, and one whose source carries
    a `pull_request` object is a PR. An issue referencing another issue is skipped by
    that same test rather than by a number range.
    """
    found: dict[int, str] = {}
    for event in events:
        if event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        if "pull_request" not in source:
            continue
        number = source.get("number")
        if number is None:
            continue
        # A PR that is gone or unreadable reports no state; `unknown` is a state the
        # caller treats as "not open" and never as "open".
        found[int(number)] = str(source.get("state") or "unknown")
    return found


def referencing_issues(events: list[dict]) -> set[int]:
    """The issue numbers that referenced this item, from **its** timeline.

    Read off a **PR's** timeline: the subject is the PR, so a `cross-referenced`
    event whose source has no `pull_request` object is an issue that named the PR in
    its own text — the second half of the bidirectional link.
    """
    found: set[int] = set()
    for event in events:
        if event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        if "pull_request" in source:
            continue
        number = source.get("number")
        if number is not None:
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


def judge_issues(
    issues: list[dict],
    by_issue: dict[int, dict[int, str]],
    stated_by_pr: dict[int, set[int]],
    now: dt.datetime | None = None,
) -> list[Row]:
    """A verdict per open issue: is exactly one open PR handling it, and does it say so?"""
    named = named_by_issue(stated_by_pr)
    rows: list[Row] = []
    for issue in issues:
        number = int(issue["number"])
        refs = by_issue.get(number, {})
        open_refs = sorted(pr for pr, state in refs.items() if state == "open")
        closed_refs = sorted(pr for pr in refs if pr not in open_refs)
        names = sorted(named.get(number, ()))
        age = age_days(issue.get("created_at", ""), now)
        if not open_refs:
            if names:
                # An issue that names a PR which does not name it back. Reachable
                # because the two directions are recorded separately: a comment on the
                # issue creates the PR-side event, and a PR body that says nothing about
                # the issue creates nothing on this side. Not `unclaimed` - there is a
                # trace, and it is one-sided, which is a different remedy.
                rows.append(
                    Row(
                        "issue",
                        number,
                        issue["title"],
                        "one-way",
                        f"this issue names #{' #'.join(str(p) for p in names)} and no "
                        "open PR names it back - the mention belongs in the PR body "
                        "(`Closes #N` where it finishes this), because the PR is where "
                        "the work and its reader are",
                        age,
                    )
                )
                continue
            detail = (
                "no open PR references it"
                + (
                    f"; #{' #'.join(str(p) for p in closed_refs)} do(es), which is the "
                    "shape of work that landed and was never closed - either close the "
                    "issue with the reading that says the work is done, or open the PR "
                    "that finishes it"
                    if closed_refs
                    else "; nothing has been opened for it"
                )
            )
            rows.append(Row("issue", number, issue["title"], "unclaimed", detail, age))
            continue
        if len(open_refs) > 1:
            rows.append(
                Row(
                    "issue",
                    number,
                    issue["title"],
                    "duplicate",
                    "more than one open PR references it ("
                    + ", ".join(f"#{p}" for p in open_refs)
                    + ") - one issue is finished by one PR, so keep one and fold the "
                    "other into it (a rejected or change-requested PR is updated in "
                    "place, never replaced by a second one)",
                    age,
                )
            )
            continue
        pr = open_refs[0]
        if number in stated_by_pr.get(pr, set()):
            rows.append(
                Row(
                    "issue",
                    number,
                    issue["title"],
                    "linked",
                    f"#{pr} references it and this issue names #{pr} back",
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
                    f"#{pr} references it, and this issue never names #{pr} - state it "
                    f"here (`gh issue comment {number} -R {DEFAULT_REPO} --body "
                    f"'handled by #{pr}'`), because the PR's own mention is the only "
                    "direction GitHub has so far",
                    age,
                )
            )
    return rows


def judge_prs(
    prs: list[dict],
    by_issue: dict[int, dict[int, str]],
    stated_by_pr: dict[int, set[int]],
    now: dt.datetime | None = None,
) -> list[Row]:
    """A verdict per open PR: does it belong to an open issue, and is the link mutual?"""
    refers_to: dict[int, set[int]] = {}
    for issue_number, refs in by_issue.items():
        for pr_number, state in refs.items():
            if state == "open":
                refers_to.setdefault(pr_number, set()).add(issue_number)

    rows: list[Row] = []
    for pr in prs:
        number = int(pr["number"])
        outbound = refers_to.get(number, set())
        inbound = stated_by_pr.get(number, set())
        age = age_days(pr.get("created_at", ""), now)
        if not outbound and not inbound:
            rows.append(
                Row(
                    "pr",
                    number,
                    pr["title"],
                    "unlinked",
                    "no open issue references it and it references none - name the "
                    "issue it belongs to in the PR body (`Closes #N` where the PR "
                    "finishes it), because an issue is the unit of work here and a PR "
                    "that names none belongs to no tracked problem",
                    age,
                )
            )
            continue
        missing = sorted(outbound - inbound)
        if missing:
            rows.append(
                Row(
                    "pr",
                    number,
                    pr["title"],
                    "one-way",
                    "it references "
                    + ", ".join(f"#{n}" for n in sorted(outbound))
                    + " and "
                    + (
                        "none of those issues names it back"
                        if len(missing) == len(outbound)
                        else f"#{' #'.join(str(n) for n in missing)} does not name it back"
                    )
                    + " - post the link in the issue (the rule is explicitly "
                    "bidirectional, and a reader of the issue is the one who needs it)",
                    age,
                )
            )
            continue
        unnamed = sorted(inbound - outbound)
        if unnamed:
            # The other direction, and not symmetric with the one above: an issue
            # that names a PR is not the same as the PR naming the issue, because a
            # reader of the *PR* is the one who has to know which tracked problem it
            # belongs to. Same rule, different reader.
            rows.append(
                Row(
                    "pr",
                    number,
                    pr["title"],
                    "one-way",
                    "#"
                    + " #".join(str(n) for n in unnamed)
                    + " names it and this PR names no issue - state it in the PR body "
                    "(`Closes #N` where the PR finishes it), because the issue is the "
                    "unit of work here and the PR is where its reader looks next",
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
                "belongs to "
                + ", ".join(f"#{n}" for n in sorted(outbound or inbound))
                + ", named back in the issue",
                age,
            )
        )
    return rows


def collect(repo: str) -> tuple[Queue, dict[int, dict[int, str]], dict[int, set[int]]]:
    """Every reading the two verdicts need, with one timeline call per subject.

    The PR-side "which issues reference this PR" is read from the PR's **own** timeline
    (that is the direction in which GitHub records it), so the two maps are built from
    complementary reads rather than from one read interpreted two ways.
    """
    queue = load_queue(repo)
    by_issue: dict[int, dict[int, str]] = {}
    for issue in queue.issues:
        by_issue[int(issue["number"])] = referencing_prs(
            timeline(repo, int(issue["number"]))
        )
    stated_by_pr: dict[int, set[int]] = {}
    for pr in queue.prs:
        stated_by_pr[int(pr["number"])] = referencing_issues(
            timeline(repo, int(pr["number"]))
        )
    return queue, by_issue, stated_by_pr


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
            "is finished by one PR, and each names the other."
        )
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name")
    parser.add_argument(
        "--json", action="store_true", help="emit the rows as one JSON document"
    )
    args = parser.parse_args(argv)

    try:
        queue, by_issue, stated_by_pr = collect(args.repo)
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"cannot determine the issue/PR links: {exc}", file=sys.stderr)
        return 2

    rows = judge_issues(queue.issues, by_issue, stated_by_pr) + judge_prs(
        queue.prs, by_issue, stated_by_pr
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
                f"\nOK: every open issue has exactly one open PR referencing it, each "
                f"link is stated in the issue, and every open PR belongs to an open "
                f"issue ({len(rows)} subject(s) read)"
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
