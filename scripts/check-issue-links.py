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
  that issue referenced P in its own text (body or comment) — and, as on the PR side, a
  reference is the link's second half only when that text **claims** P (`issue_claims`),
  because the citation/claim ambiguity is symmetric (issue #1660).

So a link that exists in one direction only is visible as exactly that. The measurement
that separated the two directions, before this tool existed: PR **#1607**'s timeline
carries a cross-reference from issue **#1553** (written in a comment on the issue, not
in its body), while PR **#1616**'s carries none although #1616's body names #1551 — a
one-way link. That pair is also what settles the first question a reader asks: an issue's
**comment** is read, not only its body, so "handled by #N" written under the issue is a
real second half and not something the tool is blind to. The comment is *fetched* rather
than read off the event, because a `cross-referenced` event carries its source's **body**
and never the comment that made the reference (measured 2026-09-27, issue #1660: #1655's
timeline carries a reference from #1654 whose source body is #1654's body, while the claim
`Taken by **#1655**` is a comment) — see `issue_text`.

The two shapes the rule itself produces, both in the live queue on the day it was read:
an issue whose work **landed and was never closed** (#1553, #1554, #1556, #1560, #1598,
each referenced by a merged PR and by no open one), and an issue with **two open PRs
declaring it** — the shape the rule forbids outright, since a rejected or
change-requested PR is updated in place rather than answered by a second one.

What it reads, and the boundary of that reading
-----------------------------------------------
One call lists the open issues and the open PRs together (`/issues` returns both, and
the `pull_request` key is the discriminator), then one timeline call per open issue and
per open PR, and one comments call per issue that some open PR references (the claim text
costs a call — see `issue_text`). Boundary facts, stated rather than implied:

* the subject is the **open** queue. A PR that references only a *closed* issue reads
  `unlinked`, and an issue naming a *closed* PR is not counted as naming a live one —
  both say so in the row rather than claiming the number is absent;
* a claim phrase is read from an issue's body **and** its comments, with code spans and
  fenced blocks blanked out first, so an issue that *quotes* `handled by #N` is not read
  as making that claim — the same direction, for the same reason, as the PR side;
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
    origin-unresolved
                 an open issue declares its origin (`Origin: rant <timestamp>`, R5) and the
                 rant ledger does not hold that timestamp, so the chain's first joint is
                 broken: the issue's reader cannot reach the requirement the issue exists to
                 carry
    origin-duplicate
                 two or more open issues declare the same rant origin and at least one
                 carries no `Part: n/N` — which is what makes a deliberate split of one rant
                 across several issues distinguishable from a duplicate claim of it

The last two read a **file**, not GitHub, and that difference is why they exist at all
----------------------------------------------------------------------------------------
Everything above is read from the API, and the rule it enforces (host 2026-09-26T18:52:57)
is about PRs and issues. R5 adds the link that comes *before* both — a cycle that takes up a
rant files the issue it will be finished by, naming the rant's ISO timestamp on the issue's
first line — and nothing read it, which is measurable rather than theoretical: this cycle
found its own issue #1747 had been opened without the line (found and fixed during a host
turn, minutes after R5's template half landed on master as part of #1738).

A rant timestamp is a **host-local** handle, so the resolution is a local file read
(`~/.emrg/rants.jsonl`, overridable with `--rants` / `$EMRG_RANTS` because the tests must not
read the host's live ledger) and it is attempted **only when an open issue declares an origin**:
a queue where nobody declares one costs no file read, and a host without a ledger does not
have a linked queue turned into exit 2 by a check nobody asked for.

What the resolution cannot decide, stated rather than implied: `submit_rant cleanup` keeps all
pending and in-progress rants plus the **ten most recent completed** ones, so an origin whose
rant completed and was pruned reads `origin-unresolved` exactly like one never written. The
row says so, because the reader's next move differs (worth the ledger's absence or not) and a
row that hid the difference would be asking for a timestamp no file holds.

Exit codes
----------
0  every open issue has exactly one open PR declaring it, each declaration is named back
   in the issue, every open PR declares an open issue, and every origin an issue declares
   resolves in the rant ledger
1  at least one row is in a state above that is not `linked`
2  the question could not be answered (gh failed, a payload did not parse, or an issue
   declared an origin and the ledger named to resolve it could not be read) — never
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

The issue side has no closing keyword, so it reads a claim **phrase**
--------------------------------------------------------------------
GitHub defines no syntax an issue can use to declare that a PR finishes it, and the first
version of this tool therefore counted *any* reference from an issue as the issue naming
its handler. Measured 2026-09-27 (issue #1660, cycle `cyc20260927-194212`, master
`d177c982`), on this tool's own live queue: issue **#1650**'s reopen comment cited
**#1653** — pointing at the branch a new guard lived on — and the reading reported #1650
as naming #1653, printing a `one-way` row whose remedy asked an unrelated PR to declare
the issue.

So the issue side reads a **claim phrase**: a closing verb in the passive voice (`closed
by`, `fixed by`, `resolved by`) or one of the two verbs this project actually writes under
an issue (`handled by`, `taken by`), followed by the reference. The vocabulary is measured
rather than invented — the live rows that must keep reading `ok` are #1658/#1661/#1663/
#1665 (`Handled by #N`, all four in a **comment**) and #1654 (`Taken by **#1655**`,
with the emphasis between the verb and the number) — and `handled by` is in it for a
second reason: it is the phrase *this tool prints as its remedy*, so a reader who follows
the printed instruction is read back the same way.

The direction of error is the PR side's, and it is why the list stays short: a claim
phrased some other way leaves a **loud** `one-way` row a reader can fix by writing the
phrase the remedy names, while reading a citation as a claim **invents** a link in silence.
A claim is also read *for* a particular PR — an issue claiming `#20` does not make `#30`
claimed — which is the second way a citation can leak into a row, and `claiming_issues` is
where both are closed.

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
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

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

#: The issue side's claim form, and the mirror of `_CLOSING_KEYWORD`: a closing verb in
#: the **passive** voice — the form an issue can write about a PR — plus the two verbs
#: this project actually writes under an issue. Measured 2026-09-27 against the live
#: queue rather than invented, because the vocabulary decides which live rows stay `ok`:
#: `Handled by #1659` (#1658), `Handled by #1662` (#1661), `Handled by #1664` (#1663),
#: `Handled by #1666` (#1665) — all four in a **comment**, which is why the claim
#: text is fetched rather than read off the cross-reference event — and
#: `Taken by **#1655**` (#1654) / `Taken by #1653` (#1652), with the emphasis between the
#: verb and the number, which is what the separator class is for.
#:
#: `handled by` is in the list for a second reason beyond measurement: it is the phrase
#: this tool prints as its own remedy (`judge_issues`), so a reader who follows the
#: printed instruction is read back the same way. The list is deliberately short — see
#: the module docstring on which direction an omission errs in.
#:
#: Only the **participles** are matched. `\b` keeps `unhandled by` / `unfixed by` out
#: without a separate clause, since there is no word boundary between `un` and `handled`.
_CLAIMED_BY = re.compile(
    r"(?i)\b(?:closed|fixed|resolved|handled|taken)\s+by\b[\s:*_]*"
    r"((?:#[0-9]+[\s,]*)+)"
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
#:
#: The closer's trailing class carries ``\r``, and that is a measured defect rather than
#: symmetry for its own sake (issue #1697): the closer is anchored with ``$``, which under
#: ``re.M`` matches before ``\n`` — so in a **CRLF** body the ``\r`` sits between the fence
#: marker and the position ``$`` accepts, ``[ \t]*`` cannot reach past it, the closer never
#: fires, and the alternation takes ``\Z``. Every fence then masks to the end of the text,
#: which for this tool means **blanking every comment appended to the body**: issue #1696's
#: body is CRLF (written on Windows), so the `Handled by #1688` comment that answers it was
#: invisible and the row stayed `one-way` with a remedy that could not be followed — the
#: reading was wrong in the *loud* direction the masking comment above prefers, but it was
#: wrong about a live row, and it made its own printed instruction unexecutable.
#:
#: Measured 2026-09-28 through this module's own pattern: an identical body, LF against
#: CRLF, masks `13..25` of 45 under LF and `15..52` of 52 under CRLF, and the claim written
#: after the fence reads `{1234}` in the first and `set()` in the second. Only the closer's
#: class changed; the opener is unaffected, because ``^`` matches after ``\n`` whether or
#: not the line it starts ends with ``\r``.
_FENCED_BLOCK = re.compile(
    r"^[ \t]*(?:```|~~~).*?(?:^[ \t]*(?:```|~~~)[ \t\r]*$|\Z)", re.S | re.M
)
_INLINE_CODE = re.compile(r"`[^`\n]*`|``.*?``", re.S)
#: One fence **marker line** — opener or closer — and not the block it delimits. Used by
#: `_without_quote_marks`, which wants a fenced block's contents readable and its marks
#: gone, the opposite of what `_FENCED_BLOCK` is for.
_FENCE_MARK = re.compile(r"^[ \t]*(?:```|~~~).*$", re.M)

#: The rant ledger, and the **first link of the chain** this reading now checks: R5 tells
#: a cycle that takes up a rant to file the issue it will be finished by, with the issue's
#: first line naming its origin verbatim (`Origin: rant <the rant's ISO timestamp>`). A
#: timestamp is the rant's only handle, so an issue whose origin cannot be resolved is a
#: link broken at its first joint — the reader cannot reach the requirement from the work.
#:
#: Host-local, exactly like every rant timestamp: the store lives at `~/.emrg/rants.jsonl`
#: on the machine that wrote the citation (`check-rant-citations.py` measured what happens
#: on a second host — 0 of 24 resolve there). `--rants` and `$EMRG_RANTS` exist because the
#: tests must not read the host's live ledger, and because a fork's store is its own.
DEFAULT_RANTS = Path.home() / ".emrg" / "rants.jsonl"

#: An issue's origin line: the word `Origin:` (any case), the word `rant`, and a timestamp.
#: Anchored to the start of a line under `re.M`, because R5 asks for it on the issue's
#: **first line** — a mention of the shape inside a sentence is prose about the convention,
#: not a declaration of origin. Two deliberate choices in the pattern, both settled on the
#: live queue the day this was written:
#:
#: * the timestamp must be a well-formed ISO instant, not any word. `\S+` was the first
#:   version and it invents a handle out of the next token — #1745's line continues
#:   `(【P0 · 要求 5】…)` after its timestamp, and a bare token scan reads that as the origin,
#:   reporting a fault about a string no ledger could ever hold;
#: * up to two **inline-code marks** may precede it, and prose may follow it. R5 asks for the
#:   timestamp verbatim and #1745 spells it in backticks followed by an explanation, so a
#:   strict reading of a lenient writer is a silent gap — the fault is missed, which is the
#:   opposite of the direction this family prefers its errors to err in. What stays strict is
#:   the anchor plus the fence mask in `_origin_lines`;
#: * the **offset may be missing**. Measured on a live body 2026-09-30 (issue #1767): its
#:   first line reads `Origin: rant 2026-09-30T10:27:20` while the ledger spells
#:   `2026-09-30T10:27:20.573512+08:00`. Requiring `Z`/`±hh:mm` made `_origin_lines` return
#:   the **empty list** for that body, so `judge_origins` saw no origin to resolve and the row
#:   read `ok` — a broken first joint reported as intact, which is the silent gap this
#:   docstring's line above already argues against. Recognising the instant is what lets the
#:   fault surface with `_same_instant_spelling`'s remedy ("write it verbatim") instead of
#:   vanishing. Dropping the offset is a writer's slip, not a hole a ledger could fill: no
#:   comparison here treats a naive instant as equal to an offset one.
_ORIGIN = re.compile(
    r"(?im)^[ \t]*origin:[ \t]*rant[ \t:]+[`*_]{0,2}"
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)

#: The escape hatch R5 defines for a rant carried by more than one issue: `Part: 1/3`. Two
#: open issues naming the same rant are the shape the one-rant-one-issue default forbids,
#: *unless* each says which part it is — then the reading can tell a deliberate split from
#: a duplicate claim, which is the whole reason the clause exists.
_PART = re.compile(r"(?i)\bpart:[ \t]*\d+[ \t]*/[ \t]*\d+")


def _without_code(text: str) -> str:
    """`text` with inline code spans and fenced blocks blanked out, same length."""
    masked = _FENCED_BLOCK.sub(lambda m: " " * len(m.group(0)), text)
    return _INLINE_CODE.sub(lambda m: " " * len(m.group(0)), masked)


def _without_quote_marks(text: str) -> str:
    """`text` with the code **delimiters** removed and their contents kept, same length.

    The complement of `_without_code`: that one blanks what a span contains, so a keyword
    inside one stops being a claim; this one keeps the contents and blanks only the marks,
    which is what lets a keyword be read *and* have its negation window read with it. A
    `` ` `` is not a word, and leaving it in place breaks the window's end anchor — measured
    on this very function: "This does not `` `close #1718` ``." reads as a claim under a
    raw scan and as no claim at all once the mark is a space, because the negation pattern
    requires whitespace between the negation and the verb it governs.
    """
    spaced = _FENCE_MARK.sub(lambda m: " " * len(m.group(0)), text)
    return spaced.replace("`", " ")


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

    Each clause is written for the number of referrers it names (`_agrees`): the first
    revision of the fix above printed *"#1614 referenced it and are merged, and neither
    declares"* for one referrer and *"#1565 #1569 #1607 ... and neither declares"* for
    three, on the row the reader is meant to act on. That is the same failure as the
    defect it was fixing, one level down - the sentence must say what is actually there,
    and a reader who trusts a wrong number is where the ambiguity starts.
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


def _scan_claims(text: str) -> set[int]:
    """The issue numbers a closing keyword names in `text`, negation window applied.

    The one place the keyword-and-number vocabulary lives, so the masked reading
    (:func:`declared_claims`) and the masked-away one (:func:`quoted_claims`) cannot
    drift apart: they differ in the text they are handed, not in how they read it.
    """
    claims: set[int] = set()
    for match in _CLOSING_KEYWORD.finditer(text):
        if _NEGATED_KEYWORD.search(text[: match.start()]):
            continue
        claims.update(int(n) for n in re.findall(r"#(\d+)", match.group(1)))
    return claims


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
    return _scan_claims(_without_code(body or ""))


def quoted_claims(body: str | None) -> set[int]:
    """The numbers a closing keyword names where masking hides it from `declared_claims`.

    The difference between reading the body and reading it with code spans and fenced
    blocks blanked out: the numbers a keyword names *inside* them. They are not claims
    — that is the point of the masking — but a report that says "this PR declares no
    issue" while the body visibly carries a closing keyword sends its reader to the
    wrong remedy. Measured 2026-09-29 on PR #1715: its Tracking section read
    "`Closes #1718`." in backticks, so the row said the PR declares nothing and the
    author's own sentence — that the tool read the pair `ok` — was the only thing wrong
    with a body that had made the declaration it was being asked for.

    So the mask is a reading rule and stays one; what this adds is the sentence that
    names it. `_scan_claims` is the same reader over a text that keeps the span contents
    and drops only the marks (`_without_quote_marks`), so a keyword that is negated, or
    that names nothing, is not counted in either direction.

    Called by the `one-way` PR row, which is the state where the reader has a number to
    point at: an issue names the PR and the PR's declaration of it is masked.
    """
    return _scan_claims(_without_quote_marks(body or "")) - declared_claims(body)


def issue_claims(text: str | None) -> set[int]:
    """The PR numbers an issue's own text says it is **finished by**.

    The mirror of `declared_claims`, and deliberately the same machinery: code spans and
    fenced blocks are blanked out first (`_without_code`), a claim the clause negates is
    not one (`_NEGATED_KEYWORD`), and `None` declares nothing. What differs is the
    vocabulary, because GitHub defines none on this side — the closing verbs in the
    passive voice, plus the two this project writes (`_CLAIMED_BY`, whose comment carries
    the measurement).

    Public for the same reason its counterpart is: this is the reading the whole tool
    agrees on, and the tests drive it directly so that "a citation is not a claim" is
    pinned at the unit level and not only through a whole report. The live case it was
    written for is issue #1660: #1650's reopen comment cites `#1653`, and a bare citation
    must not read as #1650 naming its handler.
    """
    masked = _without_code(text or "")
    claims: set[int] = set()
    for match in _CLAIMED_BY.finditer(masked):
        if _NEGATED_KEYWORD.search(masked[: match.start()]):
            continue
        claims.update(int(n) for n in re.findall(r"#(\d+)", match.group(1)))
    return claims


def rants_path(override: str | None = None) -> Path:
    """The rant ledger: `--rants`, else `$EMRG_RANTS`, else the host's own store."""
    raw = override or os.environ.get("EMRG_RANTS")
    return Path(raw).expanduser() if raw else DEFAULT_RANTS


def load_rant_rows(path: Path) -> list[dict]:
    """Every row the ledger holds, in file order, or a `RuntimeError` when it cannot be read.

    The one reader of the ledger's file format. `load_rants` is the set view of it, and
    `scripts/review-queue.py` is the second consumer (it renders a row per open rant), so
    the parsing lives here once: a second copy of it is a second answer to "what is a rant
    row", and the copy that drifts is the one nobody reads.

    A store that is missing or unparseable is **not** "a store with no rants in it": the
    question both consumers ask is about what the ledger *holds*, and an unreadable ledger
    leaves it unanswered. Callers report that as unmeasurable (exit 2), which is the
    family's rule and the reason this raises instead of returning an empty list — an empty
    answer would print `origin-unresolved` for every issue (or "no rants") on a host whose
    ledger simply is not there, a confident wrong verdict about a queue that may be fine.

    Rows are read with `json.loads` per line, the shape `submit_rant` writes
    (`emrg/server/rants.py`). A line that is not JSON is skipped rather than fatal: the
    ledger is appended to by a tool, and a half-written last line is not evidence that the
    other timestamps are absent — the direction that matters here is the one that would
    *invent* a fault, and skipping errs the other way.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"the rant ledger could not be read ({path}): {exc}"
        ) from exc
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_rants(path: Path) -> set[str]:
    """Every timestamp the ledger holds, or a `RuntimeError` when it cannot be read.

    The set view of `load_rant_rows` — this is the question "does this handle resolve",
    which wants membership and nothing else.
    """
    return {
        row["timestamp"]
        for row in load_rant_rows(path)
        if isinstance(row.get("timestamp"), str)
    }


def _origin_lines(body: str | None) -> list[str]:
    """The rant timestamps `body` declares as its origin, in the order they appear.

    Fenced blocks are blanked out first, and only those: a fenced `Origin:` line is a body
    *documenting* the convention (this tool's own docstring and R5's text both spell it), so
    it must not read as a declaration. Inline code is deliberately **not** blanked here,
    which is where this reading parts company with the claim readers above: for a closing
    keyword, masking an inline span errs toward a loud `UNCLAIMED` row, but for an origin it
    would err toward **silence** — the fault the reading exists to report would simply not
    be seen. The line anchor does the work the span mask does there: a line inside backticks
    starts with a backtick, not with `origin:`.
    """
    text = _FENCED_BLOCK.sub(lambda m: " " * len(m.group(0)), body or "")
    return [m.group("ts") for m in _ORIGIN.finditer(text)]


def declared_origins(issues: list[dict]) -> dict[int, list[str]]:
    """Issue number -> the rant timestamps its **own text** declares as its origin.

    A list per issue, not a single timestamp: a body may carry more than one, and the
    duplicate check below has to see both to name them. Read through `_origin_lines`, whose
    docstring carries the masking decision and the live case that settled it.
    """
    declared: dict[int, list[str]] = {}
    for issue in issues:
        found = _origin_lines(issue.get("body"))
        if found:
            declared[int(issue["number"])] = found
    return declared


def _same_instant_spelling(stored: str, cited: str) -> bool:
    """Is `stored` the record a writer citing `cited` was reaching for?

    Only ever used to choose the remedy's wording — never to resolve a handle — so it may
    be generous where the verdict is not. The shape it exists for is measured: the ledger
    spells `2026-09-29T15:52:49.378845+08:00` and a writer dropped the microseconds into
    `2026-09-29T15:52:49+08:00`, which no prefix rule sees (the sixth character from the end
    differs) and which a reader cannot fix without being told the exact spelling. The
    comparison is on the seconds a writer cannot have dropped accidentally — a plain
    `startswith` either way, plus the `[:19]` instant, two rants in one second being what
    the ledger's microsecond precision exists to keep apart.
    """
    return (
        stored.startswith(cited)
        or cited.startswith(stored)
        or stored[:19] == cited[:19]
    )


def judge_origins(
    issues: list[dict], store: set[str], where: str
) -> dict[int, tuple[str, str]]:
    """Issue number -> `(state, detail)` for every origin fault; a clean origin is absent.

    Two faults, and they are different questions:

    * `origin-unresolved` — the issue names a rant origin the ledger does not hold, so the
      first joint of the chain `rant -> issue -> PR` is broken and a reader cannot reach
      the requirement the issue exists to carry. The detail names the ledger it read and,
      when a stored timestamp differs from the citation only by the precision a writer
      dropped, names that stored spelling — a remedy a writer can follow.
    * `origin-duplicate` — two or more **open** issues declare the same rant and at least
      one of them carries no `Part: n/N`. R5's default is one rant, one issue; the split is
      legitimate only when every part says which part it is, so an unlabelled one makes the
      two indistinguishable from a duplicate claim — which is the reading `--json` and a
      reviewer both need before they can tell deliberate from accidental.

      The subject is counted by **issue, not by occurrence**: one issue that writes its
      origin line twice (a body carrying the line at the top and again in a provenance
      section, or an edit that appends rather than replaces) has declared one origin, and
      the rule it is measured against speaks of "two or more open issues". Grouping by
      occurrence reported that as a duplicate of itself, in a row whose own detail read
      *"#N name the same rant origin ... and #N #N carry no `Part: n/N`"* — three wrong
      numbers in a sentence a reader is meant to act on, the same defect class the `_agrees`
      helper above exists to avoid. The list is therefore of distinct numbers, which is also
      what makes its `len` the issue count the threshold means.

      Precedence, because two faults can hold at once: when the ledger holds **neither**
      the cited instant nor a near spelling, every declaring issue already carries
      `origin-unresolved`, and that fault stands — the duplicate question is asked only of
      an origin the ledger resolved. The duplicate remedy (fold the issues together, or
      label each with its part) leaves an absent origin exactly as unresolved as it was, so
      reporting the sharing first would send a reader to fix something that cures nothing.

    The pruned-store limit, stated rather than implied: `submit_rant cleanup` keeps all
    pending and in-progress rants plus the ten most recent completed ones, so an issue
    whose rant completed and was then pruned reads `origin-unresolved` exactly like one
    whose origin was never written. That is the right direction here — an open issue whose
    rant is gone is a chain a reader cannot walk either way — and the detail says so, which
    is what keeps the row actionable.
    """
    faults: dict[int, tuple[str, str]] = {}
    by_ts: dict[str, set[int]] = {}
    labelled: dict[int, bool] = {}
    for issue in issues:
        number = int(issue["number"])
        body = issue.get("body") or ""
        labelled[number] = bool(_PART.search(_without_code(body)))
        for ts in _origin_lines(body):
            by_ts.setdefault(ts, set()).add(number)
            if ts in store:
                continue
            near = sorted(t for t in store if _same_instant_spelling(t, ts))
            remedy = (
                f"the ledger holds `{near[0]}` - write it verbatim"
                if near
                else "write the rant's own timestamp verbatim (the ledger is the only "
                "place it is spelled)"
            )
            faults[number] = (
                "origin-unresolved",
                f"the origin line names rant `{ts}`, which {where} does not hold - {remedy}. "
                "The ledger keeps every pending and in-progress rant and only the ten most "
                "recent completed ones, so a pruned origin reads the same as one never "
                "written: an open issue whose rant is gone is a chain no reader can walk",
            )
    for ts, numbers in by_ts.items():
        if len(numbers) < 2:
            continue
        unlabelled = sorted(n for n in numbers if not labelled[n])
        if not unlabelled:
            continue
        for number in unlabelled:
            # `setdefault`, not assignment: the chain's first joint is answered before the
            # claim about sharing it. An issue whose origin the ledger does not hold already
            # carries `origin-unresolved`, and the duplicate remedy (fold the issues into
            # one, or label each with its part) leaves that origin exactly as unresolved as
            # it was — so overwriting would send the reader to fix something that cures
            # nothing. The duplicate fault is recorded only where the origin resolved.
            faults.setdefault(
                number,
                (
                    "origin-duplicate",
                    f"{_numbers(numbers)} name the same rant origin `{ts}` and "
                    f"{_numbers(unlabelled)} carry no `Part: n/N` - one rant is one issue by "
                    "default, so either fold these into the one issue that finishes the rant, "
                    "or label each with the part it is (`Part: 1/2`) so a reader can tell a "
                    "deliberate split from a duplicate claim",
                ),
            )
    return faults


def apply_origins(rows: list[Row], faults: dict[int, tuple[str, str]]) -> None:
    """Fold each origin fault into that issue's own row, keeping one row per subject.

    The report's shape — one row per open subject, with the state and the remedy on it — is
    what makes it readable at a glance, so an origin fault does not get a second row:
    a row whose **link** is already faulty keeps its link state and carries the origin fault
    beside it (the link fault is what a reader acts on first), while an issue whose link is
    clean takes the origin state. Both are faults, so both are excluded from the `OK` count
    either way; what changes is only which one the state names.
    """
    for row in rows:
        if row.kind != "issue":
            continue
        fault = faults.get(row.number)
        if not fault:
            continue
        state, detail = fault
        if row.clean:
            row.state, row.detail = state, detail
        else:
            row.detail = f"{row.detail} - and the origin is faulty: {detail}"


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


def load_queue(repo: str, state: str = "open", since: str | None = None) -> Queue:
    """The issues and PRs in `state`, from the one endpoint that returns both.

    `state="open"` is this tool's own reading — the queue whose links it judges — and is
    the default so no caller gets a different queue by omission.

    `state="closed"` with a `since` is `review-queue.py`'s rant row, which needs the
    opposite thing: an issue that declared a rant and was then **closed by the merge of
    its own PR**. Measured 2026-10-02, that row rendered `no issue yet` for a rant whose
    issue #1807 existed and had been closed when #1808 merged — so a cycle was told to
    file an issue for work that was already done, which is the duplicate this tool exists
    to report. `since` bounds the reading by `updated_at`: it returns the issues that
    closed around the rants in question rather than every issue the repository ever
    closed, and a `since`-bounded call here measures one request against this repo.
    """
    query = f"repos/{repo}/issues?state={state}&per_page={_TIMELINE_PER_PAGE}"
    if since:
        query += f"&since={since}"
    rows = _paged_json(["api", query])
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


def issue_text(repo: str, number: int, body: str | None) -> str:
    """An issue's body **and** every one of its comments, as one text.

    Why the comments cost a call of their own: a `cross-referenced` event carries its
    source's **body**, so on a PR's timeline a reference from issue N hands back N's body
    and never the comment that made it — measured 2026-09-27, `issues/1655/timeline`
    carries a reference from #1654 whose source body is #1654's body (2,356 chars) while
    the claim `Taken by **#1655**` is a comment. A comment is where the claim normally
    lives — the remedy this tool prints is `gh issue comment … 'handled by #N'` — so the
    text has to be fetched rather than read off the event.

    `body` is passed in rather than fetched, because `/issues` already returned it.
    """
    comments = _paged_json(
        ["api", f"repos/{repo}/issues/{number}/comments?per_page={_TIMELINE_PER_PAGE}"]
    )
    bodies = [body or ""]
    bodies.extend(str(row.get("body") or "") for row in comments)
    return "\n\n".join(bodies)


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

    This is the **reference** reading and deliberately not the verdict: a reference is
    not a claim on this side either (issue #1660), so the set is filtered by
    `claiming_issues` before it is used as the link's second half. The two steps are kept
    apart because both answers are needed — `collect` reads the claim text only for the
    issues that appear here, so the reference set is what keeps that call proportional to
    the question.
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


def claiming_issues(
    referenced: set[int], claims_by_issue: dict[int, set[int]], pr_number: int
) -> set[int]:
    """Of the issues that referenced PR `pr_number`, the ones whose text **claims** it.

    A reference is evidence of attention; a claim is ownership, and only the second is
    the rule's second half. This is the issue-side half of the distinction the PR side
    already draws with closing keywords, and it is a named step rather than an inline
    comprehension because getting it wrong is silent: issue #1660's defect was that a
    bare citation in a comment — #1650 pointing at #1653's branch — was read as #1650
    naming its handler, which manufactured a `one-way` row asking an unrelated PR to
    declare the issue.

    An issue with no text read at all is not a claim: an unreadable referrer must not be
    able to claim a PR, which is the same rule `referencing_prs` applies to a PR source
    with no body.
    """
    return {number for number in referenced if pr_number in claims_by_issue.get(number, set())}


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


def _agrees(values) -> tuple[str, str]:
    """The verb and the "declares nothing" phrase a group of referrers takes.

    Measured on this tool's own output (2026-09-26, the same revision as #1644): a single
    merged referrer printed *"#1614 referenced it and are merged, and neither declares
    `Closes #1556`"* - three wrong numbers in one sentence, on a row a reader is meant to
    act on. `neither` is for exactly two and `none of them` for three or more; the corpus
    has both (one referrer for #1556, six for #1554).
    """
    if len(values) == 1:
        return "is", "and declares no"
    if len(values) == 2:
        return "are", "and neither declares"
    return "are", "and none of them declares"


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
        verb, neg = _agrees(refs.mentioned_landed)
        clauses.append(
            f"{_numbers(refs.mentioned_landed)} referenced it and {verb} merged, {neg} "
            f"`Closes #{number}` - work has landed on master carrying "
            "this number, so either close this issue with the reading that says that "
            "was the remedy, or state here what it still leaves"
        )
    if refs.mentioned_abandoned:
        verb, neg = _agrees(refs.mentioned_abandoned)
        clauses.append(
            f"{_numbers(refs.mentioned_abandoned)} referenced it and "
            f"{'was' if verb == 'is' else 'were'} closed "
            f"without merging {neg} `Closes #{number}` - an attempt "
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
        #
        # A body that makes the declaration *inside* a code span reads as declaring
        # nothing here, and the generic remedy then asks for a sentence the body already
        # has. Naming the mask is the difference between "write it" and "move it out of
        # the backticks" (measured on #1715, 2026-09-29: `Closes #1718` in backticks).
        #
        # Compared against `inbound` — the issues that named this PR — and not against
        # `number`, which is the PR's own: the masked reading is of *issue* numbers, so
        # the PR's number in the same test would never match (caught by the test below,
        # which is why the note is asserted on a real report rather than only as a unit).
        masked = quoted_claims(pr.get("body")) & inbound
        note = (
            " - and this body does carry a closing keyword for "
            + _numbers(masked)
            + " inside a code span or a fenced block, which this tool blanks out before "
            "it reads one, so the declaration is there and has to stand outside them"
            if masked
            else ""
        )
        rows.append(
            Row(
                "pr",
                number,
                pr["title"],
                "one-way",
                _numbers(inbound)
                + " names it and this PR declares no issue - state it in the PR body "
                "(`Closes #N` where the PR finishes it), because the issue is the unit "
                "of work here and the PR is where its reader looks next"
                + note,
                age,
            )
        )
    return rows


def collect(repo: str) -> tuple[Queue, dict[int, Refs], dict[int, set[int]]]:
    """Every reading the two verdicts need, with one timeline call per subject.

    The PR-side "which open issues name this PR" is read from each PR's **own** timeline
    (that is the direction in which GitHub records it), so the two maps come from
    complementary reads rather than from one read interpreted two ways.

    That direction is then filtered through the claim text, which costs one comments call
    per **referencing** issue — not per open issue. A claim is what GitHub cross-references,
    so an issue that claims a PR is always in some PR's reference set and the filter cannot
    miss one; an issue nobody references is not read for text at all, and its own row is
    decided by the issue timeline alone.
    """
    queue = load_queue(repo)
    refs_by_issue: dict[int, Refs] = {}
    for issue in queue.issues:
        number = int(issue["number"])
        refs_by_issue[number] = referencing_prs(timeline(repo, number), number)
    referencing_by_pr: dict[int, set[int]] = {}
    for pr in queue.prs:
        number = int(pr["number"])
        referencing_by_pr[number] = referencing_issues(
            timeline(repo, number), queue.issue_numbers
        )
    bodies = {int(issue["number"]): issue.get("body") for issue in queue.issues}
    referenced = (
        set().union(*referencing_by_pr.values()) if referencing_by_pr else set()
    )
    claims_by_issue = {
        number: issue_claims(issue_text(repo, number, bodies[number]))
        for number in sorted(referenced)
    }
    stated_by_pr: dict[int, set[int]] = {}
    for number, referenced_issues in referencing_by_pr.items():
        stated_by_pr[number] = claiming_issues(referenced_issues, claims_by_issue, number)
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
        "--rants",
        default=None,
        help=(
            "the rant ledger an `Origin: rant <timestamp>` line is resolved against "
            "(default: $EMRG_RANTS, else ~/.emrg/rants.jsonl). Read only when an open "
            "issue declares an origin"
        ),
    )
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

    # The first link of the chain (R5), read only when an issue actually declares an
    # origin: a queue where nobody does costs no file read, and — the half that matters —
    # a host whose ledger is absent does not turn a linked queue into exit 2. Read
    # failures are unmeasurable, never a pass, so they return before any verdict prints.
    declared = declared_origins(queue.issues)
    if declared:
        path = rants_path(args.rants)
        try:
            store = load_rants(path)
        except RuntimeError as exc:
            print(f"cannot determine the issue/PR links: {exc}", file=sys.stderr)
            return 2
        apply_origins(rows, judge_origins(queue.issues, store, f"the rant ledger ({path})"))

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
                f"declaration is named back in the issue, every open PR declares an "
                f"open issue, and every origin an issue declares resolves in the rant "
                f"ledger ({len(rows)} subject(s) read)"
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
