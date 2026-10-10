#!/usr/bin/env python3
"""Count a PR's *valid* LGTM votes: the ones that are still about the current head.

The class this exists for
------------------------
The merge rule for this repo is "3 consecutive ✅ LGTMs from different evolution
cycles with no ❌ in between". Reading that off the comment history is misleading,
and every recent cycle has re-derived it by hand and got it wrong at least once:

* **A vote that predates the head push is void.** When a cycle re-bases and pushes,
  every earlier ✅ becomes a statement about a commit that no longer exists. The
  comment history still shows five or six "✅ LGTM" lines, so the PR *looks* ready
  while it has zero counting votes. Measured on 2026-09-11: #1133/#1134/#1136/#1137
  each showed 4-6 LGTMs and each had **0 valid votes** after being unblocked.
* **A ❌ resets the run.** Three ✅, then a "needs fix", then a ✅ is one vote, not
  four.
* **One cycle voting twice is one vote.** Votes are counted per cycle, not per
  comment, or a single cycle could carry a PR to the threshold alone.

So the counting is mechanical, and doing it by hand is exactly the kind of repeated
work that should be a tool. This is the companion to `check-merge-freshness.py`:
that one asks "is the CI verdict still about the tree that would merge?", this one
asks "do we have the votes to merge it at all?".

How a vote is recognised
------------------------
By the **first line** of the review body, because the review `state` cannot be used:
every vote in this repo is posted with `gh pr review --comment`, so GitHub records
`COMMENTED` for both "✅ LGTM" and "❌ needs fix". The state field is useless here,
which is worth knowing before writing something that trusts it.

* the verdict mark is read at the first content character **after markdown
  decoration** (`**❌`, `- ❌`, `> ❌`, `## ❌`, `1. ❌`), so a decorated veto is
  still a veto;
* that **leading** mark then decides the line: ❌ -> a veto, ✅ -> an approval. A
  ✅ line that later mentions ❌ is still an approval, because that mention is
  prose *about* the veto ("no ❌ at this head", "0 ❌ at this head") and the ways
  of saying "none" are an open set no word list can cover;
* a line with no leading mark is a verdict written as prose, and there a refusal
  ("Not LGTM", "can't LGTM this") is a veto, a claim of LGTM is an approval, and
  a non-negated ❌ is a veto ("Result: ❌ needs fix");
* anything else -> an ordinary comment, ignored

Getting this wrong is not symmetric. Reading an approval as a veto **under**counts
(the failure the first version shipped with, which made an 11-vote PR look like 9 -
and "not ready yet" is a plausible enough state that nobody investigates). Reading
a veto as a comment does not merely undercount either: comments are skipped, so the
veto never resets the run and stale approvals in front of it still read READY 3/3 -
the tool would call a PR mergeable on reviews a ❌ had already answered.

The cycle id is read from the body (`cyc20260911-091230`); a vote without one is
reported as unattributable rather than counted, since distinctness cannot be shown.
**Every** id in the body is read, and a body naming several is unattributable too:
the counter used to take the first one, which filed a rejection under a cycle that
had never written it (measured 2026-09-16, `cyc20260917-014155` — a body naming the
two approvals it was voiding alongside its own id was recorded as a `NO` by
`cyc20260917-005148`). Which cycle wrote such a body is not derivable from it, and a
mis-attributed vote is worse than a missing one: it can credit distinctness a PR does
not have. `cast-vote.py` refuses to post a body like that; this is the reading side.

Who may cast one of those votes
-------------------------------
The rule counts *different cycles* and never said who is allowed to be one of them.
The missing half — **a cycle abstains on a head it pushed**, and on the head pushed by
the cycle immediately before it, because every cycle on a host is the same instance
running again — is refused at post time by `cast-vote.py` (issue #1408). That refusal
is not where the count is read, so it left a hole of exactly the shape this file
exists to close: a vote posted straight with `gh pr review` (the form the template
tells a Committer to use) was counted by the only authority on whether a vote counted.

So the clause is asked here too, of every vote: **the window belongs to the voting
cycle**, and the head counts as that cycle's own if it was pushed at or after the
window's start. Per vote rather than per PR, because each vote names its own cycle —
which is also why `--cycles-log` exists: the answer is read from the cycle records on
disk, and a verdict about a vote must not silently depend on which records happen to
sit on the reading host.

Asked of *every* vote, though, it read a corpus the voting cycle never wrote. The
clause is a self-review guard — "every cycle on a host is the same instance running
again" — so it belongs to the instance whose records are being read, and this repo is
reviewed by more than one: measured 2026-10-05 on #1851, the only open PR, two ✅
LGTMs from **`pm25coder`** (whose cycle says its host is Windows; this counter's
records are macOS) were voided as "cast inside the window this vote's cycle treats as
its own", on the strength of a `previous cycle cyc20261005-080717` those cycles never
saw. The PR read `0/3` with both approvals standing. So the clause is now asked of a
vote cast by **this** instance's login, and a vote by any other author is counted
subject to the conditions that need no window (posted after the head push, exactly one
cycle id, no intervening ❌) — with the note saying so on the line that reports the
count. An author the payload does not carry, or a login that cannot be read, keeps the
clause applied: neither can credit a self-review (issue #1856, and the mirror half —
our own vote on a head we did not push — is named there as out of scope).

Measured before this clause landed, over the last 30 merged PRs: **0 of 90** counted
votes fell inside their head's own window. The clause changed no history; what it
removes is the possibility of one, on the path that never asked.

Two inputs are *not* decided by guessing, and both are named in the void's reason:

* a head with no CI run has a push time that is a lower bound (the commit date), and a
  lower bound cannot decide a window — the clause is not applied, because the same
  missing run already makes the PR `BLOCKED`, and reporting its votes as void would
  name a review deficit that is not the blocker;
* a cycle id that names no instant leaves no window to apply, and an unresolved window
  is not reported as a pass — the vote is void, and its cycle is named.

A veto inside its own head's window is reported the same way and still resets the run:
`valid` is the counter's bookkeeping, not the objection's (this file already refuses to
let an attribution problem make an objection inert — see the multi-id veto).

Votes are necessary, not sufficient: the mergeable clause
---------------------------------------------------------
The merge rule has three conjuncts - 3 consecutive ✅ from different cycles, the PR
is `MERGEABLE`/`CLEAN`, and CI is green. Counting votes answers only the first, and
until now this tool printed `READY` on the strength of it alone. Measured
2026-09-12: **six** PRs (#1152/#1151/#1145/#1142/#1141/#1136) each printed
`READY 3/3` while `mergeable` was `CONFLICTING` and `mergeStateStatus` was `DIRTY` -
every one of them a merge `gh pr merge` refuses. The queue read as six PRs waiting
on a formality; it was a deadlock, and the one tool built to answer "can we merge
this" was the thing saying yes.

So the mergeable clause is read here, from `gh pr view --json
mergeable,mergeStateStatus`, and it can only ever **downgrade** a verdict:

* `CONFLICTING` - Git cannot merge the text, so the answer is no regardless of the
  votes. Reported as `BLOCKED`, which is a *different* state from `SHORT`: short
  means "come back after more review", blocked means "review is done and this still
  cannot land" - the state that sat invisible behind six `READY` lines.
* `MERGEABLE`, but any `mergeStateStatus` other than `CLEAN` - the gate is the
  *pair*, so a `MERGEABLE` PR that is `UNSTABLE`, `BEHIND`, `BLOCKED` or `DRAFT` is
  also blocked. Reading only `mergeable` is what let a **draft** pull request -
  which no vote can merge - print `READY`. `UNSTABLE` is the one of the four that is
  not decided by the state alone: it says the head's check rollup is not all green
  without saying which check-run is not, and GitHub's rollup keeps a check-run from a
  run that a newer run on the same head has already superseded (measured 2026-10-06
  on #1865, `50dea4e8`: the rollup carries the 20:52 run's `cancelled` `test` while
  the 23:47 run's `test` on the same commit concluded `success`, and no newest
  check-run of any name is non-green). So an `UNSTABLE` head's check-runs are read,
  and the state blocks exactly when they are not all green - a check still running, a
  check that concluded red, a check that never concluded at all (measured on #1861,
  head `7409741c`: the newest `test-windows` check-run is `cancelled` with 0 steps,
  superseding the older one whose runner was lost), or a list that could not be read.
  The gloss this bullet used to carry ("checks failing or unfinished") was the
  sentence that sent a reader to fix a tree nothing was wrong with.
* `UNKNOWN` - GitHub has not computed mergeability yet (usual right after a push).
  Not a yes and not a no, so this fails loud (exit 2) rather than printing either.
  A `mergeStateStatus` this version does not recognise fails loud for the same
  reason: an unknown state is not evidence of cleanliness. (Enumerating the states
  and refusing the rest buys the rot-resistance that "never branch on the field" was
  reaching for, without also passing every state that enumeration covers.)
* `MERGEABLE`/`CLEAN` - the text merges and GitHub is not withholding the merge.
  This is deliberately **not** taken as evidence that merging is safe: as
  `check-merge-freshness.py` documents, GitHub answers "does this textually merge",
  and a clean auto-merge of two same-valued count lines is the *dangerous* case, not
  the safe one. The clause is used only in the direction where it is decisive (a
  conflict or a withheld merge is a hard no), and the states are printed so a reader
  sees them without being told they are fine.

One sibling question stays with its own tool, named here so this one does not
quietly pretend to answer it: `check-merge-freshness.py` asks whether the CI verdict
is still about the tree that would merge (a question about *which* tree ran CI). This
tool answers the narrower "did this head's checks pass", and the two are read from the
same object - the runs and check-runs of one commit - which is why an `UNSTABLE` head
consults its check-runs rather than trusting the state's summary of them. Likewise
`check-merge-tree-health.py` (PR #1155) asks whether the merged tree passes the
repository's own guard, `scripts/check-doc-count.py` - that guard alone, the same
bound every gate in this family states about itself.

Push time, and the honest bound
-------------------------------
A vote counts only if it was submitted *after the head was pushed*. The push time is
taken from the earliest workflow run created for that exact SHA, because that is
when GitHub received the push event - precisely the moment the earlier votes stopped
being about the current head. When no run exists for the head, this falls back to
the head commit's committer date and **says so in the output**: a commit date can
precede the push, so the fallback is the optimistic direction and must not be
silently trusted. The fallback is also the case where the PR has no CI at all - and
that is the third conjunct failing, so it **blocks** rather than just being disclosed.

The run lookup is asked **twice** before an empty answer is taken as the answer, and
the re-ask that finds the run is printed. Not a precaution: measured 2026-09-24, this
exact query answered empty for a head that has a run (issue #1582 was reported as "no
CI run exists for the head commit", the fallback visible as `12:45:21Z` against the
run's `12:45:38Z`), answered correctly on the next run of the tool, and was correct on
all eight asks made in the minute after - so the empty answer had been served stale
and nothing in the response says which of the two it is. Because an empty answer here
is *used* (it sets `BLOCKED` with that sentence), it is a verdict-shaped reading and
gets the sibling treatment: re-ask, bounded. A head that really ran nothing stays
empty through both asks and still falls back, still flagged - the retry cannot invent
a run.

An earlier version of this note claimed such a PR "is not mergeable anyway" and used
the fallback for the count alone. Measured 2026-09-12: the claim is false.
`MergeStateStatus` is computed from *required* checks, and this repo has no branch
protection and no rulesets, so a head with **zero** checks is not `PENDING` or
`UNSTABLE` - it is `CLEAN`, indistinguishable in the merge state from a double-green
head. Three historical PRs have exactly that shape (heads `c0860a35`, `af2e0efd`,
`5358d294`; zero workflow runs each, all three reported `MERGEABLE`/`CLEAN`), and a
probe with that payload reached `READY`/exit 0 on three votes. So a missing run is not
a nuance about the count - it is the CI conjunct unverified, and it is treated as
blocking.

Usage
-----
    uv run --no-sync python3 scripts/check-vote-count.py <PR> [<PR> ...]
    uv run --no-sync python3 scripts/check-vote-count.py <PR> --json
    uv run --no-sync python3 scripts/check-vote-count.py <PR> --min-votes 2
    uv run --no-sync python3 scripts/check-vote-count.py <PR> --mergeability-wait 60
    uv run --no-sync python3 scripts/check-vote-count.py <PR> --cycles-log DIR

`--mergeability-wait` is for the transient `UNKNOWN`: GitHub computes
mergeability lazily, so a head pushed a moment ago reports "not answered yet",
and this tool refuses rather than guess. With a budget it re-asks the same
question until GitHub answers or the budget runs out - the refusal is unchanged
when the budget is exhausted, and the default (`0`) asks exactly once.

Exit codes
----------
    0  every PR has >= --min-votes valid votes **and** is
       `MERGEABLE`/`CLEAN` **and** has a CI run for its head commit
       (`--min-votes` defaults to `DEFAULT_MIN_VOTES`; `--help` prints it, so this
       spec states the name rather than a second copy of the number)
    1  at least one PR is SHORT (too few votes) or BLOCKED (cannot be merged:
       conflicting, a non-clean merge state, an `UNSTABLE` head whose check-runs are
       not all green, or no CI run for the head)
    2  the check could not be made (gh failed, unparseable response, mergeability
       not computed yet, merge state not recognised) - fail loud; never report a
       count for a question that was not answered

The count is printed either way, so a PR that is BLOCKED still reports its votes;
`--json` carries `valid_votes` and the merge states as separate fields for a
caller that wants to act on them. There is deliberately no flag that drops the
mergeable clause: a mode in which this tool says "ready" about a PR that cannot
merge is the exact reading it was just fixed for.


`gh` and network access to GitHub are required; there is no offline mode.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

REPO = "argszero/emrg"

_SCRIPTS_DIR = Path(__file__).resolve().parent

# GitHub's three-valued mergeability, as `gh pr view --json mergeable` reports it.
# Named rather than compared inline so an unrecognised value (a new GitHub state)
# falls through to the fail-loud branch instead of being read as one of these.
#
# `UNKNOWN` is what GitHub returns before it has computed mergeability at all -
# usual for a minute or two after a push. It is neither a yes nor a no, so it is
# exit 2 (could not check) rather than either verdict.
_MERGEABLE = "MERGEABLE"
_CONFLICTING = "CONFLICTING"
_UNKNOWN_MERGEABILITY = "UNKNOWN"

# GitHub's pull-request lifecycle state, as `gh pr view --json state` reports it.
#
# Measured 2026-10-03 (`cyc20261003-224625`): for a **merged** PR GitHub reports
# `mergeable=UNKNOWN` / `mergeStateStatus=UNKNOWN`, and keeps reporting it - eleven
# merged PRs spanning ~30 hours were all UNKNOWN. Mergeability is not computed for a
# PR that is already merged, so UNKNOWN there is not "not answered yet", it is the
# terminal answer, and reading it as transient sends a caller into a wait that cannot
# end (measured: `--mergeability-wait 5` really elapsed 6.2 s and failed identically).
#
# A **closed-unmerged** PR is a different case and keeps the value it had: #1710
# reports `CONFLICTING`/`DIRTY`. So the question asked below is the state itself, and
# the terminal reading does not depend on what mergeability says.
_OPEN = "OPEN"
_MERGED = "MERGED"
_CLOSED = "CLOSED"
# Public, and deliberately so: this is the one spelling of "the PR is over" in the
# family. `check-merge-freshness.py` asks it rather than keeping a second copy - the
# two tools read the same GitHub field for different questions (is there a vote left?
# is there a merge left?), and they have to agree on which values mean "finished".
TERMINAL_STATES = (_MERGED, _CLOSED)

# How long one poll of a not-yet-computed mergeability waits before asking again.
# Only the *gap* is fixed here; how long to keep asking is the caller's decision
# (`--mergeability-wait`, or `check_pr(..., mergeability_wait=…)`), because the
# right budget differs between a gate a reader is watching and a script that
# scans a queue.
_MERGEABILITY_POLL_SECONDS = 5.0

# How many times the run lookup is asked before an empty answer is taken as *the*
# answer, and the gap between the asks. The second question this tool puts to GitHub
# has the same shape as the first one: a lazy answer that must not be reported as a
# verdict. Measured 2026-09-24 (cycle cyc20260924-213009): `review-queue.py` reported
# #1582 as "no CI run exists for the head commit" - with the commit-date fallback
# visible in its own line as `pushed 2026-09-24T12:45:21Z`, where the run GitHub
# holds was created `12:45:38Z` - and the identical query answered correctly on the
# next run of the tool and on all eight asks made in the minute after that. So the
# empty answer was served stale, not true, and nothing in the response distinguishes
# the two.
#
# That asymmetry is why an empty answer is re-asked rather than reported: unlike a
# missing mergeability (which is safety-neutral and simply fails loud), "no runs" is
# *used* - it is reported as the measured fact "no CI run exists for the head commit"
# and it sets `BLOCKED`, so a stale empty answer does not read as a doubt, it reads
# as a verdict. The retry is bounded at one re-ask because the fallback path is not
# going away: a head that really ran nothing (a dropped push, a fork PR) stays empty
# through both asks and still falls back to the commit date, flagged inexact. A
# genuinely run-less head therefore pays the gap once per reader, which matters
# because a cycle's queue scan reads this for every open PR.
_RUN_LOOKUP_ATTEMPTS = 2
_RUN_LOOKUP_DELAY_SECONDS = 2.0

# The merge gate is spelled **`MERGEABLE`/`CLEAN`** - two fields - and an earlier
# version of this tool read only the first, on the theory that `mergeStateStatus` is
# "a finer-grained view of the same fact" and branching on it would rot as GitHub
# adds states. That theory was wrong twice over, and the second time was measured
# (cyc20260912-190602): with three valid votes the tool printed
# **`READY` and exited 0** for `MERGEABLE`/`UNSTABLE` (checks failing or still
# running), `/BEHIND`, `/BLOCKED` and `/DRAFT`. A **draft** pull request cannot be
# merged by anyone, and `UNSTABLE` is precisely "CI is not green" - the third
# conjunct this docstring claims only a sibling tool checks. So the second field is
# read, and the spelling is the gate's own.
#
# Rot-resistance is bought the other way round from before: rather than *ignoring*
# the field, the known states are enumerated and anything unrecognised **fails loud**
# (exit 2). A state GitHub adds later is then reported as "could not check" instead
# of silently passing as permission - which is what "never branch on it" was
# actually trying to buy, at the cost of the false READY above.
_CLEAN = "CLEAN"

# `UNSTABLE` is read separately from the states below, because it is the one non-clean
# state whose *cause* is a second reading rather than the state itself: GitHub reports
# it whenever the head's check rollup is not all green, and the rollup can be held down
# by a check-run from a run that a newer run on the same head has already superseded.
# Measured 2026-10-06 on #1865 (`50dea4e8`): the state reads `UNSTABLE` while every
# newest check-run on that head concluded `success` - the rollup still carries the
# `cancelled` `test` check-run of the 20:52 run after the 23:47 run on the same commit
# passed both jobs. The gloss this map used to carry ("checks are failing or have not
# finished") was therefore false about that head, and it is the sentence that sends a
# reader to fix a tree nothing is wrong with. So the state is kept, the gloss is not:
# the head's check-runs decide it (`Verdict.checks_green`), which is the object the
# question - "did this head's checks pass?" - is actually about.
_UNSTABLE = "UNSTABLE"

# The states in which the merge cannot proceed right now, each with the reason the
# reader needs. GitHub's `MergeStateStatus` vocabulary:
#
#   DIRTY       the merge conflicts
#   BEHIND      the head is out of date with the base branch
#   BLOCKED     GitHub blocks the merge (protection rules / required reviews)
#   DRAFT       the pull request is a draft
_NON_CLEAN_STATES = {
    "DIRTY": "the merge conflicts",
    "BEHIND": "the head is behind the base branch",
    "BLOCKED": "GitHub reports the merge blocked (protection rules or required reviews)",
    "DRAFT": "the pull request is a draft",
}

# What clears each of those states, in the reader's terms. Separate from the map above
# because the two answer different questions - that one says *why* the state holds the
# merge back, this says *what to do about it* - and because the answers differ in the
# one way a voter needs: `DRAFT` and `BLOCKED` clear without publishing a commit, so
# they leave every vote standing on the head valid, while a conflict and a `BEHIND`
# refresh both publish one and void them. `DIRTY` is here and is also what a
# `mergeable == CONFLICTING` head gets, because that is the same fact: two spellings of
# "git cannot merge the text" land on one sentence rather than two.
_NON_CLEAN_CURES = {
    "DIRTY": (
        "git cannot merge the text, so the branch has to be resolved against the base "
        "and pushed; that publishes a new head and voids every vote standing on this one"
    ),
    "BEHIND": (
        "the head is behind the base branch and has to be refreshed; that publishes a "
        "new head and voids every vote standing on this one"
    ),
    "BLOCKED": (
        "GitHub reports the merge blocked by protection rules or required reviews, so "
        "what clears it is a review - that publishes no commit"
    ),
    "DRAFT": (
        "the pull request is a draft: mark it ready for review, which publishes no "
        "commit"
    ),
}

# Every `MergeStateStatus` this version knows how to read, so the fail-loud branch
# below still refuses a state GitHub adds later. `UNSTABLE` is here and not in the map
# above: dropping it from the vocabulary would make the one state whose reading changed
# the one state that reads as unknown.
_KNOWN_STATES = frozenset({_CLEAN, _UNSTABLE, *_NON_CLEAN_STATES})

# What a check-run's conclusion has to be for the check to have held the head back.
# `neutral` and `skipped` count as green for the same reason `gh pr checks` renders
# them as passes: neither is a statement that the check failed. A check-run that has
# not completed has no conclusion at all and is never green - the "checks still
# running" case this clause was added for (cyc20260912-190602) stays blocked.
_GREEN_CHECK_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

# How many check-runs GitHub can list for one commit in a single page. Every check-run
# of a head is needed to tell a superseded `cancelled` from a live one, so a head that
# overflows this would be read from a truncated list - named rather than silent, and
# reported by `Verdict.checks_truncated`.
_CHECK_RUNS_PAGE = 100

# `HAS_HOOKS` ("merge commits are conditioned on hooks") is deliberately NOT in the
# map: whether it permits a merge is not something this tool can establish, and
# guessing either way would put an unverified verdict behind a gate. It falls to the
# fail-loud branch with every other unrecognised value.

# The runnable form, as Agent.md documents it. A constant (the same convention as
# check-doc-count.py) so the doc line and the guard that checks it cannot drift
# into agreeing on a string that no longer runs anything.
INVOCATION = "uv run --no-sync python3 scripts/check-vote-count.py"

# The merge gate: how many consecutive valid votes a PR needs. One spelling, read
# by the CLI default, the `Verdict` default and the `--help` line — a number
# written twice is a number that can disagree with itself, and this one decides
# whether a PR may land. (Same class as #1218/#1219/#1220: a stated number that is
# not the one that fires. Measured 2026-09-14: `--min-votes`'s help text spelled
# "(default 3)" beside the `default=3` it was describing.)
DEFAULT_MIN_VOTES = 3

# `cyc20260911-091230` - the cycle id the vote comments carry.
#
# The trailing `(?!\d)` is a **token boundary on the right**, and it is here because
# without it the reader returns an id the body did not write. Measured 2026-10-05
# (`cyc20261005-234557`), driving the files' own functions: a body stating
# `cyc20261005-2345571` read as `['cyc20261005-234557']` - the id is a *prefix* of a
# longer run of digits, so the search takes the shorter one and every later reading
# (the abstention window, the distinctness rule) is made about a cycle that did not
# cast the vote. The same string is refused as a `--cycle` value by `cast-vote.py`
# (`_CYCLE_RE.fullmatch`), so before this the family held two verdicts for one
# string. Both copies of this pattern carry the boundary; they are described in both
# files as one shared pattern, and a boundary on only one of them would be that
# drift. The **left** side is deliberately unbounded: `xcyc20261005-234557` states an
# id contained in a longer token, and the id returned is the one that was written -
# a trailing digit is the case where the id read is not the id written.
_CYCLE_RE = re.compile(r"cyc\d{8}-\d{6}(?!\d)")


def distinct_cycle_ids(body: str) -> list[str]:
    """Every *distinct* cycle id `body` **states**, in order of first appearance.

    The one reading of "which cycle(s) does this body name", and distinct rather
    than per occurrence: the question a vote body answers is *which cycle wrote
    it*, and a body repeating one id still names exactly one candidate. The
    difference is not academic (measured 2026-09-17, PR #1310): this reader
    counted occurrences while `cast-vote.py`'s preflight
    (`cycles_in()`) deduped, so a body quoting a transcript that contained its
    own id was **accepted** by the poster and **voided** by the counter -
    `VOID (2 cycle ids) - the vote body names 2 cycle ids
    (cyc20260917-075555, cyc20260917-075555)`, i.e. one id, twice.

    The same shape one level out, fixed here (measured 2026-09-27, cycle
    `cyc20260927-123036`, on master `846c232d`): the two readings of a body in this
    file disagreed about *quotation*. `classify` had fenced regions dropped - "a
    mark inside a quotation is not the reviewer stating it" (`_fence_flags`) - while
    this reader took ids out of the raw body, so

        ✅ LGTM — cycle cyc20260927-123036
        ```
        the row read: previous cycle cyc20260927-113700 pushed this head
        ```

    read as **two** ids and counted for none: the vote was lost for quoting the tool
    output a review is made of, and `review-queue.py` prints exactly such lines. The
    reading now comes from the same place as the verdict's (`_decorated_lines`), so
    the body has one answer to "what does it state" rather than two. Inline spans are
    left as they were - that is a separate question, unchanged for verdicts too.

    Named and exported so the two scripts can be *asserted* to agree on this
    axis, not just on the pattern (`_CYCLE_RE`) they share - the presence-only
    check they had kept passing while the readings diverged.
    """
    return list(dict.fromkeys(_CYCLE_RE.findall("\n".join(_decorated_lines(body)))))


# ── the abstention window: was this head the voting cycle's own? ─────────────
#
# The rule's other half. "3 consecutive ✅ from different cycles" says nothing about
# *who may cast one*, and the clause that carries it — a cycle does not vote on a PR
# whose head it pushed, nor on the head pushed by the cycle immediately before it —
# is refused at post time by `cast-vote.py` (issue #1408). This is the reading side
# of the same clause, which was missing: the count is what a cycle reads when it
# decides to merge, so a vote that must not have been cast has to read as uncounted.
# Both halves ask one question — "were the head and the vote cast by the same
# cycle?" — and neither can read the answer off GitHub, which attributes a push to
# nobody; the clock is the only witness, and the window is the machinery that reads
# it (`scripts/review-queue.py`, where the two ends and the narrowed form live).
#
# The window belongs to the *voting* cycle, so this is asked per vote rather than
# once per PR, and the source directory is a parameter: a verdict about a vote may
# not silently depend on which cycle records happen to sit on the reading host.

_review_queue: object | None = None


# ── whose window is it? ─────────────────────────────────────────────────────
#
# The clause above is a **self**-review guard, and its own premise says so: "a cycle
# does not vote on a head it pushed, and on the head pushed by the cycle immediately
# before it, because every cycle on a host is the same instance running again". The
# cycles of a host are the records on that host — so the question can be asked only
# of a vote cast by the instance those records belong to.
#
# Asked of every vote it is not merely wider than the rule: it is a verdict read off
# the wrong corpus. Measured 2026-10-05 on #1851, the repo's only open PR: two ✅
# LGTMs cast after the head push, each naming exactly one cycle id, were reported
# `VOID - cast inside the window this vote's cycle treats as its own ... (previous
# cycle cyc20261005-080717)` — a window built from *this* host's records, justified
# by *this* host's cycles, for votes `gh api repos/.../pulls/1851/reviews` attributes
# to `pm25coder`, whose cycle 110820 measures its own host as Windows. The counter
# therefore read `0/3` on a PR with two approvals standing, which is the direction
# that strands work: an under-count looks like "not ready yet" and every cycle parks.
#
# Two inputs are not decided by guessing, and both keep the clause applied, because
# refusing is the direction the clause itself prefers: a payload that does not carry
# an author (a projection that did not apply — the failure this file already hit once
# with `at`), and a login that cannot be read. Neither can *credit* a vote, so neither
# can turn a self-review into a counted one.

_UNSET: object = object()
_own_logins: object = _UNSET

#: The account a remote URL names, in the forms a GitHub remote is written in: the
#: scp-like `git@github.com:owner/repo.git` and the URL forms
#: `https://github.com/owner/repo.git` / `ssh://git@github.com:22/owner/repo.git`.
#: The host must carry a dot, so a local-path remote — this checkout's own `origin`
#: is a directory — names nobody rather than being read as an account.
_REMOTE_OWNER = re.compile(
    r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::[0-9]+)?[:/]"
    r"(?P<owner>[^/]+)/[^/]+?/?$"
)


def remote_owners(listing: str) -> frozenset[str]:
    """Every account a `git remote -v` listing names, or the empty set for none.

    A host can push under more than one login, and `gh api user` reports only the one
    its **token** belongs to: GitHub attributes a push to the account that
    authenticated, so a host whose remote is `git@github.com:owner/repo` has its
    pushes recorded as `owner` while its token answers something else. Measured
    2026-10-08 on this repo: head `d31870d6` (#1893), pushed by a peer's own cycle,
    carries run `37623098954` with actor `argszero`, while that host's `gh api user`
    answers `how2how2how2-arch`; the same split shows on #1899's head `b6130da4`
    (actor `argszero`). Reading a single login un-refused that instance's own pushes —
    the one direction this clause exists to close — which is what the review on #1900
    measured (cycle `cyc20261008-013454`).

    The URL's account is not a proof of identity — a member pushing to `someorg/repo`
    is not `someorg` — but it is the account the URL names, and *over*-inclusion is
    this clause's safe direction: a login wrongly read as one's own keeps the clause
    and costs a delay, where one wrongly excluded credits a self-review.
    """
    owners: set[str] = set()
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        url = parts[1]
        if "://" in url:
            url = url.split("://", 1)[1]
        match = _REMOTE_OWNER.match(url)
        owner = match.group("owner") if match else ""
        if owner:
            owners.add(owner)
    return frozenset(owners)


def _git_remote_listing() -> str:
    """`git remote -v` from the checkout this script lives in, or `""`.

    Run against the script's own repository rather than the working directory: the
    counter is invoked from a session directory, and `git remote -v` there answers
    about whatever repository happens to sit above that cwd. `""` parses to no owners,
    which leaves the token login alone — the reading this tool had before it could
    read a remote at all, and the one that errs toward keeping the clause.
    """
    try:
        proc = subprocess.run(
            ["git", "remote", "-v"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def instance_logins() -> frozenset[str]:
    """Every login this instance pushes under, or the empty set when none can be read.

    The token's login (`gh api user`) **and** the accounts this checkout's remotes name
    (`git remote -v`), because a host can hold two accounts and GitHub attributes a push
    to the one that authenticated rather than to the one the counter happened to ask
    about — `remote_owners` carries the measurement.

    Asked only when the clause would otherwise decide something (`own_login` below is
    its only caller), so a run with nothing inside a window makes the same number of
    calls it always did. An empty set is not a failure of the reading: it keeps the
    clause applied, which is this file's safe direction, and so does a login that
    cannot be read.
    """
    global _own_logins
    if _own_logins is _UNSET:
        logins: set[str] = set()
        try:
            payload = _gh_json(["api", "user", "--jq", "{login: .login}"])
        except (RuntimeError, ValueError):
            payload = None
        login = payload.get("login") if isinstance(payload, dict) else None
        if isinstance(login, str) and login:
            logins.add(login)
        logins |= remote_owners(_git_remote_listing())
        _own_logins = frozenset(logins)
    return _own_logins  # type: ignore[return-value]


def own_login(author: str | None) -> bool:
    """Was this vote cast by the instance whose cycle records the window came from?

    `True` when it cannot be told apart — an author the payload does not carry, or no
    login that can be read — so an undecided voter keeps the clause, which is the
    direction that costs a delay rather than crediting a self-review (issue #1856).

    **Membership, not equality.** A host may push under more than one login — the
    remote's account and the token's — and every one of them is this instance's own:
    reading a single login un-refused this instance's own pushes made under its other
    one, which is what the review on #1900 measured on 2026-10-08 (a run whose actor is
    `argszero` against a token answering `how2how2how2-arch`). The author is tested
    *first* so that a payload without one costs no call, and so that a run which never
    compares two logins makes none at all.
    """
    if not author:
        return True
    mine = instance_logins()
    if not mine:
        return True
    return author in mine


def review_queue():
    """The sibling that owns the window: `abstain_window`, `previous_cycle`, `instant`.

    Loaded by path, the way this family loads its siblings — the scripts here are
    hyphenated and are not importable modules — and registered in `sys.modules`
    before it is executed, because the loaded file declares dataclasses and
    dataclasses resolves its annotations through `sys.modules[cls.__module__]`.
    """
    global _review_queue
    if _review_queue is None:
        spec = importlib.util.spec_from_file_location(
            "check_vote_count_abstain", _SCRIPTS_DIR / "review-queue.py"
        )
        # No `spec is None` guard: `spec_from_file_location` returns a spec and a
        # loader even for a path that does not exist (measured 2026-10-06), so that
        # branch could never fire. A sibling that is missing or does not compile
        # raises out of `exec_module`, and `_entry` reports that as `2`.
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _review_queue = module
    return _review_queue


def own_head_window(
    cycle: str,
    *,
    push_time: str,
    push_time_exact: bool,
    pusher: str = "",
    cycles_log: str | None = None,
) -> tuple[bool, str]:
    """Is the head inside the window this vote's own cycle treats as its own?

    Answers `(inside, why)`. `inside` means the vote must not count, and `why` is
    what the reader is told about it; `(False, "")` is the ordinary case, and a
    non-empty `why` with `inside` False is the one exemption that was decided by a
    reading rather than by the clock — the row prints it so a reader can see which
    datum decided.

    Four inputs, and the same rule as the posting side for each of them:

    * **Whose push it is.** The clause is about a head *this* instance pushed, and the
      window is only a proxy for that: a head the peer pushed inside a gap between this
      host's cycles fell inside the window and was voided as this instance's own work
      (measured 2026-10-06, `cyc20261006-122605`; #1856's mirror half). So a `pusher`
      that reads as a login **outside the set this instance pushes under** exempts the
      head outright (the set, because a host holds more than one login: `remote_owners`).
      An unknown one - an empty string, or no login that can be read - keeps the clause,
      so this can only ever un-void a push that is provably someone else's, never credit
      a self-review.
    * **The window.** The previous cycle's start when its record can be found, this
      cycle's own start when it cannot — `abstain_window`'s narrowed form, and the
      reason says which of the two was applied, because a window that could not be
      widened is a strictly weaker reading and must not pass as the stronger one.
    * **An unresolvable window is not passed.** A cycle id that names no instant
      leaves nothing to compare, and the direction that costs a delay is preferred
      to the one that spends a vote (the sibling's rule, `cast-vote.py`'s refusal).
    * **No CI run for the head means the clause is not applied.** The push time is
      then the commit date — a lower bound — and a lower bound cannot decide a
      window. That case is already answered *above* this clause: `blocked` refuses
      to merge such a head for the same missing run, and reporting the votes as void
      on top of it would name a review deficit that is not the blocker.

    A veto is reported the same way, and keeps its force: `valid` is the counter's
    bookkeeping, not the objection's (see the multi-id veto, the same rule).
    """
    if not push_time_exact:
        return False, ""
    if not cycle:  # a caller error, not a reading: only a resolved cycle is asked about
        raise ValueError("own_head_window needs the vote's cycle id")
    queue = review_queue()
    previous, where = queue.previous_cycle(cycle, queue.resolve_cycle_logs(cycles_log))
    window = queue.abstain_window(cycle, previous, where)
    if not window.applied:
        return True, (
            "the head's own window cannot be decided from this vote's cycle "
            f"({window.unresolved or f'{cycle!r} names no instant'}) - an unresolved "
            "window is not passed, in either tool, and `cast-vote.py` refuses to post "
            "in this state (issue #1408)"
        )
    pushed = queue.instant(push_time)
    if pushed is None:
        return True, (
            f"the head's own window cannot be decided from push time {push_time!r}, "
            "which is not an instant (issue #1408)"
        )
    if pushed < window.start:
        return False, ""
    # The clause is about a head *this* instance pushed, and the window is only a proxy
    # for that: a head the peer pushed inside a gap between this host's cycles fell inside
    # the window and was reported as this one's own work (measured 2026-10-06,
    # `cyc20261006-122605`; #1856's mirror half). A positive reading of a login *outside
    # the set this instance pushes under* exempts the head; an unknown one keeps the
    # clause, so this can only ever un-void a push that is provably someone else's. The
    # set, rather than one login, because a host holds two of them — see `remote_owners`
    # for the measurement, and `own_login` for the direction each mistake costs. Asked
    # *here* - after the window has said the head is inside it - so a run whose heads are
    # all outside their windows still makes no identity call, the laziness
    # `test_a_vote_by_another_instance_is_not_voided_by_this_hosts_window` pins.
    if pusher and not own_login(pusher):
        return False, (
            f"counts - the head was pushed by {pusher}, which is not a login this "
            "instance pushes under, so the own-head clause is not asked of it"
        )
    narrowed = (
        f"; the window could not be widened to the cycle before this one ({window.unresolved})"
        if window.unresolved
        else ""
    )
    return True, (
        f"cast inside the window this vote's cycle treats as its own - the head was "
        f"pushed {push_time}, at or after {window.window_start_text()} ({window.source}"
        f"{narrowed}); a cycle does not vote on a head it pushed, and the window "
        "immediately before it counts as its own, so `cast-vote.py` refuses to post "
        "this vote (issue #1408)"
    )


# A **leading** veto wins over everything on the line: "❌ needs fix" is a request
# for changes no matter what follows it. A leading ✅ is the mirror image, and it
# keeps the whole line: every real approving body that also mentions ❌ mentions it
# to say there *isn't* one ("(no ❌ at this head)", "(0 ❌ at this head)"), and the
# phrasing of that is an open set no negation list can cover. Only when the line
# has no leading mark is the veto scan below applied, where it catches a veto
# written as prose - and there a veto still wins, because prose gives the line no
# mark to be read through.
_VETO_MARK = "\u274c"  # ❌
_LGTM_MARK = "\u2705"  # ✅

# Markdown decoration that can precede the mark when a reviewer bolds, bullets,
# quotes or heads their first line: "**❌ Needs fix**", "- ❌ ...", "> ❌ ...",
# "## ❌ ...". None of these characters can begin a verdict on their own, so
# stripping them cannot hide one.
_DECORATION = "*_~`#>|[]()- \t"

# "1. ❌ ..." / "2) ✅ ..." - an ordered list item, which `_DECORATION` misses.
_ORDERED_ITEM_RE = re.compile(r"^\d+[.)]\s*")

# A mark preceded by a negation is prose *about* the mark, not a statement of it.
# `[^\w]{0,4}` bounds the gap by non-word characters, so this cannot cross a word:
# "not bad, LGTM" is an approval, "not LGTM" is not.
_NEGATION_WORDS = (
    r"\b(?:not|no|never|without|nothing|isn'?t|aren'?t|wasn'?t|weren'?t|"
    r"don'?t|doesn'?t|didn'?t|cannot|can'?t|won'?t)\b"
)


def _negation(exclude: str = "") -> str:
    """The negation pattern, optionally refusing to cross the given characters.

    `_refuses` passes the marks: a line that negates ❌ is precisely a line that is
    *not* negating LGTM, so a window that can reach across "❌, " would read the
    prose approval "Results: no ❌, LGTM" as a refusal. Deriving both from one
    vocabulary keeps them from drifting apart.
    """
    return _NEGATION_WORDS + r"[^\w" + exclude + r"]{0,4}"


_NEGATION = _negation()


def _negated(line: str, mark: str) -> bool:
    """Is `mark` negated on this line - is its absence being described?"""
    return re.search(_NEGATION + re.escape(mark), line, re.IGNORECASE) is not None


def _refuses(line: str) -> bool:
    """A negated LGTM: the reviewer is declining, not approving.

    "Not LGTM" must not fall through to the plain "LGTM" substring test - that
    would count a refusal as a *vote*, and as an *approval*, so the earlier votes
    it was written to answer would still read as the run.

    The negation may not cross a **mark**, because a line that negates ❌ is
    precisely a line that is *not* negating LGTM. The un-narrowed window
    (`[^\\w]{0,4}`) reaches past "❌, " to the LGTM, so "Results: no ❌, LGTM" - a
    prose approval - was read as the refusal "no ❌, LGTM" and vetoed (measured
    this cycle). A refusal is written about LGTM itself, so the window excludes
    both marks.
    """
    window = _negation(exclude="\u274c\u2705")
    return re.search(window + "LGTM", line, re.IGNORECASE) is not None


# An opening or closing code fence, indented up to 3 spaces (the markdown limit).
# ``` and ~~~ are both valid fence markers; the run length matters (see below).
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


def _fence_flags(lines: list[str]) -> list[bool]:
    """Per-line: is this line inside a *balanced* fenced code region?

    A quote is not a statement. `_decorated_lines` strips backticks as decoration,
    so without this a line-opening mark inside a fenced block is indistinguishable
    from prose - and a body that *quotes* a veto (a reproduction snippet, a table
    of example verdicts) was read as *stating* one. Measured 2026-09-11
    (cyc20260911-171843) through this tool's own `check_pr`: three approvals
    followed by a review that documents a veto inside a fence gave

        master -> comment  (the body is skipped, run stays 3)
        head   -> veto     (run resets to 0, every approval discarded)

    so quoting the shape was the one way to void the run the tool exists to
    protect. `master` was right here by accident: it never looked past line one.

    Fences nest by *length*, per CommonMark: a fence opened with N backticks is
    closed only by a fence of the same character and at least N of them, with
    nothing but whitespace after. A first version toggled a boolean on any fence
    line, which broke on exactly the bodies this exists for - a ``` example quoted
    inside a ```` block (measured on a real review body: the inner ``` flipped the
    flag and the quoted veto came back as prose). Inside a longer fence the shorter
    marker is content, as a reader sees it.

    Unbalanced fences (a stray opener) are treated as ordinary text. An odd marker
    must not silently hide the rest of a body - that direction drops a *real* veto
    and leaves stale approvals live, which is the failure this module documents at
    length. Failing toward "read it as prose" keeps the honest reading of a body
    whose formatting is broken.

    That fallback covers only the region *from* the unmatched opener onward, not
    the whole body. An earlier version returned `[False] * len(lines)`, which
    re-opened every fence in the body - including the ones already balanced and
    closed, which are not ambiguous at all. Measured 2026-09-11 (cyc20260911-180347),
    reported by a contributor on the PR and reproduced here: a body that quotes a
    veto inside a *closed* fence and then leaves one stray opener anywhere after it

        Reviewed on Windows.

        ```              <- closed, balanced
        ❌ Needs fix: quoted example
        ```

        Note on formatting.
        ```              <- stray opener

    came back as a *stated* veto under the body-wide fallback (`master` read it as
    a comment). Driven end-to-end through this module's own run-walk, three genuine
    approvals followed by that body gave `master -> run 3` but the body-wide version
    `-> run 0`: the quoted mark discarded the run this tool exists to protect, which
    is the same outcome as the bug the fence fix was written for. Restricting the
    fallback to the tail fixes it and moves nothing else - measured over 477 real
    bodies, 0 change class.
    """
    flags: list[bool] = []
    open_char = ""
    open_len = 0
    open_at = 0
    for line in lines:
        m = _FENCE_RE.match(line)
        if m:
            char, rest = m.group(1)[0], m.group(2)
            if open_len == 0:
                # not inside a fence: this opens one
                open_char, open_len = char, len(m.group(1))
                open_at = len(flags)
                flags.append(True)  # the marker line itself is not content
                continue
            if char == open_char and len(m.group(1)) >= open_len and not rest.strip():
                # a real closing fence
                open_char, open_len = "", 0
                flags.append(True)
                continue
            # a shorter (or otherwise non-closing) marker inside a fence: content
            flags.append(True)
            continue
        flags.append(open_len != 0)
    if open_len != 0:
        # Only the unmatched tail is ambiguous; the closed regions above it keep
        # the reading they already earned.
        for i in range(open_at, len(flags)):
            flags[i] = False
    return flags


def _decorated_lines(body: str) -> list[str]:
    """Every content line, decorated form stripped, in order.

    Decoration-only lines are dropped, so a reviewer who opens with a `---` rule
    has still stated their verdict on the line after it. Fenced code is dropped
    too: a mark inside a quotation is not the reviewer stating it (see
    `_fence_flags`).
    """
    raw = body.splitlines()
    flags = _fence_flags(raw)
    out: list[str] = []
    for line, fenced in zip(raw, flags):
        if fenced:
            continue
        text = line
        while True:
            reduced = _ORDERED_ITEM_RE.sub("", text.lstrip(_DECORATION), count=1)
            if reduced == text:
                break
            text = reduced
        if text.strip():
            out.append(text)
    return out


def _verdict_line(body: str) -> str:
    """The body's first line that has content, with leading decoration stripped."""
    lines = _decorated_lines(body)
    return lines[0] if lines else ""


def _gh_json(args: list[str]) -> object:
    """Run `gh` and parse JSON, failing loud rather than guessing.

    `args` excludes the program name, which is prepended here so no call site can
    omit it (a call site that passed bare gh arguments once ran the POSIX `pr`
    utility instead, whose error names neither gh nor the mistake).
    """
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh failed (rc={proc.returncode}): gh {' '.join(args)}\n{proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def _gh_json_paginated(args: list[str]) -> list:
    """Every page of a list endpoint, as one list.

    Needed because a single request truncates silently: the reviews endpoint
    returns 30 by default and orders **oldest first**, so a PR with more than 30
    reviews would drop its *newest* votes - precisely the ones that count, since
    the whole rule is about votes cast after the head push. #1134 already carries
    7 reviews and #1136 has been through three unblocks; this is a merge gate, so
    the count must not depend on how busy a PR has been.

    `--paginate` with a `.[] | {…}` filter emits one JSON object per line across
    pages; parsed per line rather than as one document, since concatenated page
    arrays are not valid JSON.

    **The filter belongs to the caller.** An earlier version appended
    `--jq ".[]"` here, so a call site that also passed `--jq` gave gh two of them:
    the later flag won, the projection was dropped, and every review came back
    with its raw field names. The tool then read `at` as `None` for every vote, and
    since `"" <= push_time` is true it voided **all** of them - a PR with two valid
    votes reported 0/3. It was invisible in the tests because the fake returns
    dicts directly and never models the jq contract, and invisible in the count
    (which only looked short, a plausible state). Found by running it against the
    live PRs after the change; the caller now owns the filter, and the shape is
    asserted instead of assumed.
    """
    proc = subprocess.run(
        ["gh", *args, "--paginate"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh failed (rc={proc.returncode}): gh {' '.join(args)} --paginate\n"
            f"{proc.stderr.strip()}"
        )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def classify(body: str) -> str:
    """`veto`, `approve` or `comment`, from the verdict mark on the first line.

    **Public on purpose, like `distinct_cycle_ids` beside it**: this is the reading
    the *posting* side must agree with. `cast-vote.py` refuses to post a body this
    answers `comment` for, because the loop below skips such a body entirely
    (`if kind == "comment": continue`), so the review posts, `gh pr review` exits 0,
    and the vote is spent in silence. Measured 2026-09-25 (`cyc20260925-135307`):
    three votes were posted with the ✅ on the *last* line and read as plain
    comments — the count did not move and nothing on the posting side said so.

    Two wrong versions came before this one, and they failed in opposite
    directions. Both are pinned by tests; both mistakes are worth naming because
    the second is the more tempting one.

    **First: "a mark anywhere in the first line".** Measured 2026-09-11, this read
    a real approval as a veto -

        ✅ **LGTM — third vote at this head** ... (two prior ✅; no ❌ at this head)

    The body begins with ✅ (it is a vote), and the ❌ is prose *about the absence*
    of a veto. The failure direction is bad: it silently voids a real vote, so an
    11-vote PR looks like it has 9 - and "not ready yet" is a plausible enough
    state that nobody investigates.

    **Second: "read the body's first character"** (the version this replaces). It
    fixed the above by refusing to look past the first character, which is correct
    only when the mark is literally first. It therefore recognised a veto *only* in
    the exact shape this repo happened to use, and read every decorated veto as an
    ordinary comment:

        **❌ Needs fix:** ...     -> comment        - ❌ needs fix   -> comment
        > ❌ needs fix           -> comment        ## ❌ Needs fix  -> comment

    A veto classified as a comment is not merely uncounted: `check_pr` skips
    comments entirely, so the run is never reset and three *stale* approvals in
    front of it still read as READY 3/3. The tool would report a PR as mergeable on
    the strength of reviews that a later ❌ had already answered. It is also
    asymmetric - "**✅ LGTM**" still reached approval through the LGTM fallback,
    which is what made the bug easy to miss - so the fix has to handle *both*
    marks, not just add a veto branch.

    So: the mark is looked for at the **first content character after decoration**
    (bold/list/quote/heading markers, which cannot themselves begin a verdict), and
    a mark preceded by a negation ("no ❌", "not LGTM") is prose about the mark and
    not a statement of it.

    **Third, and the one that took a review to find: a leading mark decides the
    line, and the line-wide veto scan is only a fallback.** The version below this
    one ran the veto scan *first*, over the whole line, which made any ✅ body that
    merely *mentions* ❌ a veto unless the negation list recognised the phrasing:

        ✅ LGTM - cycle c (0 ❌ at this head)      -> veto    (it is an approval)
        ✅ LGTM - cycle c (no prior ❌)            -> veto    (it is an approval)

    "0 ❌" is *zero vetoes*, i.e. the strongest possible approval. Reading it as a
    veto resets the run and discards every approval before it - measured: a PR with
    three approving cycles reported `SHORT 1/3` where master reported `READY`. The
    failure direction is again the quiet one ("not ready yet"), and it is not
    fixable by extending the word list: the list is bounded by `[^\\w]{0,4}`, so
    "no prior ❌", "no single ❌", "not a single ❌" and even five spaces defeat it.
    Any negation vocabulary is an open set that an approval can outrun.

    So the precedence is: the **leading** mark decides (that is this repo's stated
    convention, and it is the only shape that is unambiguously a verdict); the
    line-wide veto scan applies only to a line that does *not* open with a mark,
    where it catches verdicts written as prose ("Result: ❌ needs fix"). A leading
    ✅ with a later ❌ is therefore an approval, which is the reading that survives
    every negation phrasing - and it is what master already did, so the change
    cannot void a vote that was being counted.

    **Fourth: the first line was the only line.** Reading just it means a veto
    whose mark sits *below* a prose intro ("Checked all three fixes.\\n\\n❌ Needs
    fix: …") classifies as `comment`, and `check_pr` skips comments - so the run is
    never reset, the same dangerous direction as the decorated-mark bug, reached by
    a different route (the mark is not decorated; it is simply not on line one).
    Found in cyc20260911-153707 by probing this function. A body whose first line
    states no verdict at all now scans its later lines for a **stated** veto - the
    mark must open the line, so this repo's approvals that *describe* a resolved
    veto ("The earlier ❌ was resolved by pushing the fix myself") stay approvals.
    The first line still decides whenever it states anything, so the third fix's
    rule is untouched. Measured: 0 of 381 bodies on the 60 most recent PRs change
    class under this addition - the shape it catches is real but currently unused.

    **Fifth: the later-line scan read a *quotation* as a *statement*.** The fourth
    fix introduced this, and it is the one live regression in the series - the
    first three were all pre-existing. `_decorated_lines` strips backticks as
    decoration, so a mark inside a fenced code block became indistinguishable from
    prose, and a review that *documents* a veto (a reproduction snippet, a table of
    example verdicts) was classified as *stating* it. Driven through `check_pr`:

        three approvals, then a review quoting a veto in a fence
        master -> run 3   (the body is a comment, skipped)
        head   -> run 0   (a veto, so the run and every approval in front of it go)

    That is the exact failure the fourth fix exists to prevent, reached backwards:
    the tool would discard a genuine three-approval run because someone quoted the
    shape it looks for. Found independently by two outside contributors on the PR
    (how2how2how2-arch, pm25coder) and reproduced here before accepting it - it is
    also why this cycle did *not* merge the PR at 2/3.

    Fixed by dropping fenced regions in `_decorated_lines` (see `_fence_flags`).
    The rule this preserves is that a mark counts only when the *reviewer* states
    it: in a fence the mark opens its line, but the body is quoting, not stating.
    Measured: 0 of 309 corpus bodies change class under the fence fix alone (the
    two affected bodies already sat on the right side), so it removes the hazard
    without moving any existing verdict.

    **Not adopted.** A contribution on the PR also proposed scanning past a later
    ✅ (so a stated veto anywhere wins) and answering `approve` for a stated ✅
    below a prose intro. Measured on the same 309 bodies: that widening flips 6
    bodies, among them 4 approvals into `comment`/`approve` churn on bodies whose
    first line states a verdict - more motion for no demonstrated defect. Fence
    awareness is the cheap half and is needed either way, so only that half ships.
    """
    line = _verdict_line(body)
    if not line:
        return "comment"

    # The leading mark decides. Nothing after it can retract it, because prose
    # *about* the other mark is exactly what an approving body contains - and the
    # phrasing of that prose is an open set no negation list can enumerate.
    if line.startswith(_VETO_MARK):
        return "veto"
    if line.startswith(_LGTM_MARK):
        return "approve"

    # No leading mark: a verdict written as prose, so read the line.
    #   1. A declined approval is a veto - "Not LGTM", "can't LGTM this".
    #   2. A prose claim of LGTM is an approval, and any mark on that line is a
    #      *mention* of the other mark - the same reasoning that lets a leading ✅
    #      keep its line, applied to the shape prose actually takes. Running the
    #      veto scan before this turned "no ❌, LGTM" and "zero ❌ so LGTM" into
    #      vetoes: the negation list is bounded by `[^\w]{0,4}` and stops at the
    #      first punctuation, so every such phrasing defeated it.
    #   3. Otherwise a veto mark is a veto: "Result: ❌ needs fix".
    if _refuses(line):
        return "veto"
    if "LGTM" in line.upper():
        return "approve"
    if _VETO_MARK in line and not _negated(line, _VETO_MARK):
        return "veto"

    # The first line stated no verdict at all - it is neither a mark nor prose
    # about LGTM ("Checked all three fixes.", "Here is my review."). So look for a
    # **stated** veto further down: a later line whose own first character (after
    # decoration) is the mark.
    #
    # Why this is needed (found in cyc20260911-153707, by probing this very
    # function): reading only the first line means a veto whose mark sits below a
    # prose intro classifies as `comment`, and `check_pr` **skips comments**, so the
    # run is never reset - the exact dangerous direction this function exists to
    # close. Measured: `approve, approve, "Checked all three fixes.\n\n❌ Needs fix:
    # …"` leaves the run at 2, i.e. three stale approvals still read as live.
    #
    # Only a *stated* mark counts. A body that merely mentions ❌ in passing is how
    # this repo's approvals describe a veto they resolved ("The earlier ❌ was
    # resolved by pushing the fix myself"), and reading those as vetoes would reset
    # the run - the opposite error, equally costly. So the mark must open the line,
    # and a negated or mid-sentence mention is not one.
    #
    # The first line still decides whenever it states anything, so this cannot
    # revive the bug that let a leading ✅ lose to a later ❌ (see the docstring):
    # an approving body's first line is a ✅ or an "LGTM", and neither reaches here.
    for other in _decorated_lines(body)[1:]:
        if other.startswith(_VETO_MARK):
            return "veto"
        if other.startswith(_LGTM_MARK):
            return "comment"  # a later line states the opposite: not a veto
    return "comment"


@dataclass
class Vote:
    at: str
    kind: str
    cycle: str | None
    valid: bool
    why: str
    #: Every *distinct* cycle id the body named, in order of first appearance. Length 1
    #: is the normal case and is what `cycle` is set from; anything else is *why* `cycle`
    #: is None, and the label column has to say which of the two it is - "no cycle id"
    #: and "three cycle ids" are different facts about the body (see the reader loop).
    #: Distinct because the question is which cycle wrote the body: a body repeating one
    #: id has one candidate author, so it is not ambiguous (see the reader loop).
    ids: tuple[str, ...] = ()
    #: Why a *valid* vote counted when the reading above would otherwise have voided
    #: it — two cases, both "the own-head clause was not asked": the vote was cast by
    #: another instance (issue #1856), or the head was *pushed* by a login outside the
    #: set this instance pushes under (the mirror half, and the review on #1900, where
    #: a reader could not tell an identity's exemption from the clock's ordinary
    #: answer). Empty is the ordinary case, and the label column prints "counts" for it.
    note: str = ""


def _cycle_label(vote: Vote) -> str:
    """The label column: this vote's cycle, or the reason there is not one.

    Two different facts both leave `cycle` empty, and collapsing them into
    "(no cycle id)" would misreport the second: a body that named several ids
    *has* ids, it just has no single author. The label says how many, so a reader
    can tell "wrote none" from "wrote all of these" without reading the note.
    """
    if vote.cycle:
        return vote.cycle
    if len(vote.ids) > 1:
        return f"({len(vote.ids)} cycle ids)"
    return "(no cycle id)"


@dataclass(frozen=True)
class HeadCheck:
    """One check-run GitHub lists for the head commit.

    Every field is GitHub's own word for the check: `name` is what a reader sees on
    the PR page, `conclusion` is empty until the check completes, `status` is the
    lifecycle word (`completed`, `in_progress`, `queued`), and `run_id` is the
    Actions run the check belongs to, parsed out of the check-run's details URL so a
    reason can hand the reader a run to read rather than a name to search for.

    Frozen: `superseded_checks` asks which of these are *not* the newest of their
    name, and that question is a set membership rather than a field comparison.
    """

    name: str
    conclusion: str
    status: str
    run_id: str
    started_at: str
    #: The check-run's own id. The tie-break for "newest", because two check-runs of
    #: one name can share a start second - and GitHub orders by id, monotonically.
    id: int = 0

    @property
    def green(self) -> bool:
        """This check did not hold the head back.

        A check that has not completed is never green: it has no conclusion, and
        "still running" is precisely one of the two cases `UNSTABLE` used to cover.
        """
        return self.status == "completed" and self.conclusion in _GREEN_CHECK_CONCLUSIONS

    @property
    def word(self) -> str:
        """What this check says about itself, in one word, for a reason sentence."""
        return (self.conclusion or self.status or "unknown").lower()

    @property
    def order_key(self) -> tuple[str, int]:
        """Newest-first ordering: start time, then the check-run id as tie-break."""
        return (self.started_at, self.id)


@dataclass
class Verdict:
    pr: int
    title: str
    head_sha: str
    push_time: str
    push_time_exact: bool
    #: The login whose CI run fixed `push_time`, or `""` when there was no run to ask
    #: (the commit-date fallback) or the payload did not carry one. The abstention
    #: clause reads it to tell a head this instance pushed from one another instance
    #: did inside a gap between this host's cycles (#1856's mirror half, measured
    #: 2026-10-06 on `cyc20261006-122605`). Empty keeps the clause applied.
    pusher: str = ""
    mergeable: str = ""
    merge_state: str = ""
    #: GitHub's lifecycle state: `OPEN`, `MERGED` or `CLOSED`. A terminal one is a
    #: determinate reading (`terminal`), not a vote count - see `TERMINAL_STATES`.
    state: str = _OPEN
    #: When it was merged (empty for an open or closed-unmerged PR).
    merged_at: str = ""
    votes: list[Vote] = field(default_factory=list)
    # Parallel to `votes`: whether each one contributes to `valid_count`. A valid
    # approval whose cycle already appears earlier in the run does not, and the
    # label column must say so rather than calling it "counts".
    counted: list[bool] = field(default_factory=list)
    valid_count: int = 0
    needed: int = DEFAULT_MIN_VOTES
    #: Every check-run GitHub lists for this head, read **only when the merge state is
    #: `UNSTABLE`** - the one state whose cause is a second reading rather than the
    #: state itself (see `_UNSTABLE`). Empty on every other head, where the state
    #: already says what there is to say and the cost of a further read would be paid
    #: for nothing. `checks_read` is what tells "the reading was taken and found
    #: nothing" from "the reading was not taken", because `checks_green` is False in
    #: both and must never be read as a pass.
    checks: tuple[HeadCheck, ...] = ()
    checks_read: bool = False
    #: Whether GitHub's list was truncated at `_CHECK_RUNS_PAGE`. Reported rather than
    #: hidden: a cut list can lose the newest check-run of a name, which is exactly the
    #: one that decides the reading.
    checks_truncated: bool = False

    @property
    def terminal(self) -> bool:
        """The PR is over: merged, or closed without merging.

        Read from the state, never inferred from mergeability - a closed-unmerged PR
        keeps whatever value it had (measured: `#1710` is `CLOSED` and still reports
        `CONFLICTING`/`DIRTY`), so the two fields answer different questions and only
        the state answers this one.
        """
        return self.state in TERMINAL_STATES

    @property
    def short(self) -> bool:
        """Too few votes. A statement about review, not about mergeability.

        False for a terminal PR: there is no review left to do, and a line reading
        "not enough votes yet" would send a reviewer to work that cannot matter.
        """
        if self.terminal:
            return False
        return self.valid_count < self.needed

    @property
    def blocked(self) -> bool:
        """The merge cannot proceed: the gate's `MERGEABLE`/`CLEAN` is not satisfied.

        **False for a terminal PR**, and that is not a softening: `BLOCKED` is an
        instruction to fix a state of the branch, and a merged or closed PR is not in
        one. Measured 2026-10-03 (`cyc20261003-224625`) on `#1710`, the one
        closed-unmerged PR in the last hundred: it keeps `CONFLICTING`/`DIRTY`, which
        the conjuncts below would call blocked and the run would exit `1` over — a
        report that says "fix the conflict" about a PR that can no longer be merged.
        The state is asked first because it decides whether the question applies.

        Kept distinct from `short` because the two call for opposite responses:
        short means "come back after more review", blocked means "review is done and
        this still cannot land". Collapsing them is what let six CONFLICTING PRs sit
        behind six `READY` lines, each looking like it was waiting on a formality.

        Covers every non-clean state, not only a conflict. An earlier version tested
        `mergeable == CONFLICTING` and so reported `READY` for `MERGEABLE`/`UNSTABLE`
        (CI not green), `/BEHIND`, `/BLOCKED` and `/DRAFT` - a draft PR, which nobody
        can merge at all. The gate's spelling is the pair, so the pair is tested.

        **`UNSTABLE` is the exception, and it is read from the head's checks.** The
        state says the rollup is not all green; it does not say *which* check-run, and
        the rollup keeps a check-run from a run that a newer run on the same head has
        already superseded. Measured 2026-10-06 (`cyc20261006-091811`) on #1865: head
        `50dea4e8` reads `MERGEABLE`/`UNSTABLE` while the newest `test` and
        `test-windows` check-runs on that very commit both concluded `success` - what
        holds the state down is the `cancelled` `test` check-run of the 20:52 run, left
        behind after the 23:47 run on the same commit passed. A re-run does not clear
        it and master is already an ancestor of that head (`check-merge-freshness.py`:
        `ahead, behind_by=0`), so the remedy the old gloss implied - "fix the failure",
        or "refresh the branch" - is either about a failure that is not there or a
        no-op that publishes no new head. The honest reading of that head is that its
        checks passed; so when the check-runs are read and every newest one is green,
        `UNSTABLE` is reported and not treated as a block, and `checks_note` says what
        holds the state. Every other shape of `UNSTABLE` still blocks: a check still
        running or one that concluded red has a non-green newest check-run, and an
        unread list is not a pass (`checks_green` is False without a reading).

        A head with **no CI run** is blocking too, and it is the case the merge state
        cannot express: `MergeStateStatus` counts *required* checks, and with no
        branch protection a head that ran nothing is not `PENDING` or `UNSTABLE` but
        `CLEAN` - the same value a double-green head reports. Measured 2026-09-12 on
        three historical PRs (`c0860a35`, `af2e0efd`, `5358d294`: zero runs, all
        `MERGEABLE`/`CLEAN`). So the third conjunct is checked against the run
        itself, not against the state that is documented not to carry it.
        """
        return (
            False
            if self.terminal
            else (
                self.mergeable == _CONFLICTING
                or self.merge_state in _NON_CLEAN_STATES
                or (self.merge_state == _UNSTABLE and not self.checks_green)
                or not self.push_time_exact
            )
        )

    @property
    def newest_checks(self) -> tuple[HeadCheck, ...]:
        """The newest check-run of each name on the head, in first-seen order.

        Per *name*, because that is the question a check answers: two `test`
        check-runs on one commit are two runs of one check, and the head's verdict is
        the newer one's. GitHub keeps both in the rollup, which is the whole defect
        this reading exists for.
        """
        newest: dict[str, HeadCheck] = {}
        for check in self.checks:
            current = newest.get(check.name)
            if current is None or check.order_key > current.order_key:
                newest[check.name] = check
        return tuple(newest.values())

    @property
    def checks_green(self) -> bool:
        """Whether this head's checks have all passed.

        False without a reading, which is the fail-safe direction: an unread list is
        not evidence that anything passed, and every caller treats False as the
        strict answer.
        """
        newest = self.newest_checks
        return bool(newest) and all(check.green for check in newest)

    @property
    def checks_pending(self) -> bool:
        """A newest check-run on this head has not concluded yet.

        `queued`/`in_progress`: the head has no verdict because the run has not
        finished, which is exactly the shape for which `review-queue.py` prescribes a
        park. It is asked before any other cause because it is the one blocking shape
        whose remedy is *nothing to do* - and the sentence a blocked row used to be
        given ("the branch or the pull request has to be made mergeable first") reads
        as an instruction to push, which would publish a new head and void every vote
        standing on it.

        False without a reading, because `newest_checks` is empty then - and that case
        is not pending but unread, which `block_cure` says in its own words.
        """
        return any(check.status != "completed" for check in self.newest_checks)

    @property
    def checks_reason(self) -> str:
        """Why the checks are not green, naming each check that is not (or "")."""
        bad = [c for c in self.newest_checks if not c.green]
        if not bad:
            return ""
        named = ", ".join(f"{c.name}: {c.word}" for c in bad)
        return f"the head's checks are not all green ({named})"

    @property
    def superseded_checks(self) -> tuple[HeadCheck, ...]:
        """Green-readers' leftovers: check-runs a newer run of the same name replaced.

        Non-green *and* not the newest of its name. These are the ones the rollup can
        hold against a head that has since passed, so they are what `checks_note`
        names - and they are never what `blocked` is decided on, because the newer
        check-run of that name is.
        """
        newest = set(self.newest_checks)
        return tuple(c for c in self.checks if c not in newest and not c.green)

    @property
    def checks_note(self) -> str:
        """What a check-run reading adds to the merge state, or "" when it adds nothing.

        Two facts the state cannot show, both of them reasons the row a reader takes
        first was misleading: a head whose checks are green while the state is
        `UNSTABLE` (so what holds the state is a superseded run, not this tree), and a
        truncated list (so the reading may be missing the check that decides it). The
        non-green case needs no note here - a head in that shape is `blocked`, and
        `block_reason` is what the report prints for a blocked PR.
        """
        parts: list[str] = []
        if self.checks_truncated:
            parts.append(
                f"GitHub listed more than {_CHECK_RUNS_PAGE} check-runs for this head, "
                "so the newest of a name may not be in the list that was read"
            )
        if self.merge_state == _UNSTABLE and self.checks_green:
            stale = self.superseded_checks
            if stale:
                named = ", ".join(
                    f"{c.name}: {c.word} (run {c.run_id or 'unknown'})" for c in stale
                )
                parts.append(
                    "every newest check-run on this head is green, and what holds the "
                    f"state down is a superseded run's check-run - {named}; the rollup "
                    "keeps it until the head changes, and re-running this head does "
                    "not clear it, so the state is not a reading about the tree "
                    "(`scripts/check-merge-freshness.py` is the reading that is)"
                )
        return "; ".join(parts)

    @property
    def block_reason(self) -> str:
        """Why this PR cannot merge, in the reader's terms."""
        if self.mergeable == _CONFLICTING:
            return "Git cannot merge the text (CONFLICTING)"
        reason = _NON_CLEAN_STATES.get(self.merge_state)
        if reason:
            return f"merge state is {self.merge_state} - {reason}"
        if self.merge_state == _UNSTABLE and not self.checks_green:
            # The head's checks, not a gloss about them. `checks_reason` is empty only
            # when the list was not read at all, which is its own sentence - the one
            # thing this must never do is answer "checks are failing" for a head whose
            # checks were never looked at.
            return (
                f"merge state is {_UNSTABLE} - "
                + (
                    self.checks_reason
                    or "the head's check-runs could not be read, so which check holds "
                       "the state is not measured"
                )
            )
        if not self.push_time_exact:
            return (
                "no CI run exists for the head commit, so the CI conjunct is not "
                "verified (the merge state cannot show this: with no required checks "
                "a head that ran nothing still reads CLEAN)"
            )
        return ""

    @property
    def block_cure(self) -> str:
        """What actually clears this block, and whether that publishes a new head.

        Read in the same order as `block_reason` so the two always describe one cause,
        and kept per cause rather than as one sentence for every blocked row because
        the remedies differ in the way a voter cares about: a `DRAFT` clears by being
        marked ready, a review-required `BLOCKED` by a review, and a head held by an
        unconcluded check by the run finishing - none of which publishes a commit, so
        all three leave the votes standing on the head intact, while resolving a
        conflict, refreshing a `BEHIND` head or fixing a red check does publish one.

        A single universal sentence was printed here before, and it is false for most
        of those shapes: "More review does not fix this - the branch or the pull
        request has to be made mergeable first". On the shape measured 2026-10-11
        (`cyc20261011-002826`) - a queue whose two rows were both `UNSTABLE` with both
        check-runs still `pending` - it inverted the remedy outright, telling the
        reader to produce a mergeable head where the answer is to wait, and the push
        that would follow voids every vote standing on the head. The last sentence of
        the same paragraph warned about that exact push, so the output contradicted
        itself.
        """
        if self.mergeable == _CONFLICTING:
            return _NON_CLEAN_CURES["DIRTY"]
        cure = _NON_CLEAN_CURES.get(self.merge_state)
        if cure:
            return cure
        if self.merge_state == _UNSTABLE and not self.checks_green:
            if self.checks_pending:
                return (
                    "a check on this head has not concluded, so this clears when that "
                    "run finishes - it needs no new commit, and a push would void every "
                    "vote standing on this one"
                )
            if not [c for c in self.newest_checks if not c.green]:
                # No check-run to name as the cause, so the list came back with nothing
                # in it. (A reading that *failed* is a different path: it exits 2 and
                # never reaches a render.) Re-reading is the only cure that can settle
                # this, and it needs no new commit.
                return (
                    "the head's check-runs list came back empty, so no check can be "
                    "named as holding this head back - read them again, which publishes "
                    "no commit, rather than pushing, which voids every vote standing on "
                    "this one"
                )
            return (
                "a check on this head concluded red or was cancelled, so clear it by "
                "re-running the workflow for this head (a re-run publishes no commit; a "
                "push would void every vote standing on this one)"
            )
        if not self.push_time_exact:
            return (
                "no CI run exists for the head commit - the push event was dropped, so "
                "re-trigger CI on the same head (`gh workflow run test.yml --ref "
                "<branch>`, or `bash scripts/re-trigger-ci.sh <branch>`), which keeps "
                "the votes"
            )
        # Not reached by any blocking conjunct above; kept so a conjunct added later
        # cannot print an empty cure, which would read as "no remedy".
        return "the branch or the pull request has to be made mergeable first"

    @property
    def ok(self) -> bool:
        """Ready to merge - and never for a terminal PR.

        Spelled out rather than left to `not short and not blocked`: a merged PR is
        not "ready", and a property that answered `True` there would be the very
        confusion this reading exists to remove.
        """
        if self.terminal:
            return False
        return not self.short and not self.blocked

    @property
    def mark(self) -> str:
        """`MERGED`/`CLOSED` for a finished PR, else `BLOCKED`/`SHORT`/`READY`.

        The terminal word is deliberately not `READY` and not a failure: nothing is
        to be done here, and both of the live words would instruct a reader to do
        something - merge, or come back with more review - about a PR that is over.

        Blocked wins even when the votes are also short. The conflict is the
        blocking fact: it has to be resolved first, and resolving it pushes a new
        head, which voids every vote counted here. Reporting `SHORT` in that state
        would read as "come back after more review" and send a reviewer to do work
        that the next rebase throws away - the same misdirection as the original
        bug, one layer down.

        (Read as one docstring on purpose: the terminal paragraph was inserted
        *above* this one, which left the text below as a second, dead string
        statement - it was not the first statement of the body, so the interpreter
        kept the upper one as the member's docstring and evaluated the lower one and
        threw it away. No test asked this member for its docstring, so the loss
        survived review. `tests/test_no_dead_string_statement.py` now refuses the
        shape. The paragraph is worded without that attribute's own name because the
        static ASCII gate in `tests/test_script_output_ascii.py` read the name as a
        whole-file substring and treated this file as one whose docstring is printed
        - it is not, and the gate's reading is fixed on its own branch.)
        """
        if self.terminal:
            return "MERGED" if self.state == _MERGED else "CLOSED"
        if self.blocked:
            return "BLOCKED"
        if self.short:
            return "SHORT"
        return "READY"


def _earliest_run(head: str) -> tuple[str, str]:
    """The earliest CI run GitHub lists for this SHA, as `(created_at, actor_login)`.

    Re-asked when the answer is empty, because an empty answer is used as a fact
    (`block_reason` reads it as "no CI run exists", and `blocked` turns on it) - see
    `_RUN_LOOKUP_ATTEMPTS` for the measurement that made a single ask untenable. A
    head that really ran nothing stays empty and returns `("", "")`; the caller then
    falls back, still flagged inexact, so the retry cannot manufacture a run.

    The re-ask that *finds* the run is reported on stderr: a tool that silently
    repairs a stale answer hides the thing it repairs, and how often this happens is
    the only way a later reader can tell flakiness from a one-off.

    The run's **actor** comes back with its time because the abstention clause is about
    *whose* push a head is, and the window's start instant is only a proxy for it: from
    the time alone, a head another instance pushed inside a gap between this host's
    cycles was attributed to this host (measured 2026-10-06, `cyc20261006-122605`). An
    actor the payload does not carry reads as `""`, and `""` leaves the clause applied -
    the exemption is only ever bought by a positive reading of a non-own login. The
    login is taken from the run whose time is the answer, so the two cannot come from
    different runs.
    """
    for attempt in range(_RUN_LOOKUP_ATTEMPTS):
        payload = _gh_json(
            [
                "api",
                f"repos/{REPO}/actions/runs?head_sha={head}&per_page=100",
                "--jq",
                '{runs: [.workflow_runs[] | {t: (.created_at // ""), '
                'a: (.actor.login // "")}]}',
            ]
        )
        assert isinstance(payload, dict)
        raw = payload.get("runs")
        assert isinstance(raw, list), payload
        dated = [
            (str(run.get("t") or ""), str(run.get("a") or ""))
            for run in raw
            if isinstance(run, dict) and str(run.get("t") or "")
        ]
        if dated:
            created, actor = min(dated, key=lambda pair: pair[0])
            if attempt:
                print(
                    f"note: the runs API listed no run for head {head[:8]} and then "
                    f"returned {created} - the empty answer was served stale, so the "
                    "push time is exact after all",
                    file=sys.stderr,
                )
            return created, actor
        if attempt + 1 < _RUN_LOOKUP_ATTEMPTS:
            time.sleep(_RUN_LOOKUP_DELAY_SECONDS)
    return "", ""


def _head_check_runs(head: str) -> tuple[tuple[HeadCheck, ...], bool]:
    """Every check-run GitHub lists for this commit, and whether list was truncated.

    Asked of the commit, not of the PR: the check-runs endpoint answers per commit,
    which is the object the question is about ("did this head's checks pass?"), and
    asking the PR for its rollup is what made the state unreadable - GitHub's
    `statusCheckRollup` keeps **one** check-run per name and, measured 2026-10-06 on
    `50dea4e8`, kept the *older*: it carried the 20:52 run's `cancelled` `test` while
    the 23:47 run's `success` for the same name and commit was not in it at all. The
    raw endpoint returns all four, which is the whole difference.

    `run_id` is parsed out of the check-run's `details_url`
    (`…/actions/runs/<id>/job/<id>`), so a reason can name the run it is talking about
    and a reader can hand that id to `read-run-failure.py` without a search. A
    check-run from another app has no such URL and carries an empty id, which is
    reported as `unknown` rather than guessed.

    **Not caught.** A read that fails raises, and the caller reports "could not
    measure" rather than a verdict: the failure this reading guards against is a head
    that looks blocked for a cause that is not there, and swallowing the error would
    reinstate the same misreading one level down. Being asked only for `UNSTABLE`
    heads, this costs a `gh` call exactly where the state alone cannot answer.
    """
    payload = _gh_json(
        [
            "api",
            f"repos/{REPO}/commits/{head}/check-runs?per_page={_CHECK_RUNS_PAGE}",
            "--jq",
            "{total: .total_count, checks: [.check_runs[] | "
            "{name, conclusion, status, startedAt: .started_at, id, "
            "url: (.details_url // \"\")}]}",
        ]
    )
    assert isinstance(payload, dict)
    raw = payload.get("checks")
    assert isinstance(raw, list), payload
    checks: list[HeadCheck] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "")
        run_id = ""
        if "/actions/runs/" in url:
            run_id = url.split("/actions/runs/", 1)[1].split("/", 1)[0]
        checks.append(
            HeadCheck(
                name=str(entry.get("name") or ""),
                conclusion=str(entry.get("conclusion") or ""),
                status=str(entry.get("status") or ""),
                run_id=run_id,
                started_at=str(entry.get("startedAt") or ""),
                id=int(entry.get("id") or 0),
            )
        )
    total = payload.get("total")
    truncated = isinstance(total, int) and total > len(checks)
    return tuple(checks), truncated


def _head_push_time(head: str) -> tuple[str, bool, str]:
    """Earliest CI run creation time for this SHA, else the commit date.

    Returns `(timestamp, is_exact, pusher_login)`. The run's `createdAt` is the push
    event time; a commit date can precede the push, so the fallback is flagged rather
    than silently used. `pusher_login` is the actor of the run the timestamp came from,
    and is `""` on the commit-date fallback - there is no run there to ask whose push
    the head was, which keeps the abstention clause applied rather than guessing.
    """
    created, actor = _earliest_run(head)
    if created:
        return created, True, actor

    commit = _gh_json(["api", f"repos/{REPO}/commits/{head}", "--jq", "{t: .commit.committer.date}"])
    assert isinstance(commit, dict)
    committer_date = commit.get("t")
    if not isinstance(committer_date, str) or not committer_date:
        raise RuntimeError(f"cannot determine a push time for head {head[:8]}")
    return committer_date, False, ""


def _merge_state(view: dict) -> tuple[str, str]:
    """`(mergeable, mergeStateStatus)` from a `gh pr view` payload, checked.

    `gh pr view --json mergeable` prints `MERGEABLE` / `CONFLICTING` / `UNKNOWN`.
    The fields are required rather than defaulted: a payload that lost them (a
    projection that did not apply, the failure mode this file already hit once with
    `at`) must fail loud, because the absent value would read as "not blocking".
    """
    mergeable = str(view.get("mergeable") or "")
    state = str(view.get("mergeStateStatus") or "")
    if not mergeable or not state:
        raise RuntimeError(
            f"PR payload has no mergeability (mergeable={mergeable!r}, "
            f"mergeStateStatus={state!r}) - refusing to report a verdict, since an "
            "absent conflict is indistinguishable from no conflict"
        )
    return mergeable, state


def _pr_view(number: int) -> dict:
    """One `gh pr view` payload for `number`, as GitHub answers it right now.

    `state` is part of the projection because a terminal PR's mergeability is never
    computed, so the state - not the mergeability - is what answers the question there.
    """
    view = _gh_json(
        [
            "pr",
            "view",
            str(number),
            "-R",
            REPO,
            "--json",
            "number,title,state,mergedAt,headRefOid,mergeable,mergeStateStatus",
        ]
    )
    assert isinstance(view, dict)
    return view


def _view_with_computed_mergeability(number: int, wait: float) -> dict:
    """Ask again until GitHub has computed mergeability, or until `wait` runs out.

    Why this exists: `UNKNOWN` is not an answer, it is "not answered yet", and
    GitHub answers it lazily - a head pushed or merged moments ago reports it for
    up to a couple of minutes. Treating that as the final word makes the *caller*
    poll by hand: measured 2026-09-16, three separate cycles hit the refusal
    (`cast-vote.py` twice on one vote, `check-merge-freshness.py` on the count it
    needs to price a stale branch) and each of them slept and re-ran the tool.

    The retry re-asks the question, it does not soften the answer: `wait` seconds
    of polling and then the same fail-loud refusal as before, so a mergeability
    this tool could not read is still never reported as a verdict. Default `0.0`
    asks exactly once, which is the behaviour everything that does not opt in
    keeps.

    **A terminal PR is not polled at all.** For a merged PR the value is `UNKNOWN`
    forever (see `TERMINAL_STATES`), so every further ask is a question GitHub will
    never answer: measured 2026-10-03, `--mergeability-wait 5` on a merged PR really
    slept 6.2 s and then failed identically - a `--mergeability-wait 60` costs a full
    minute for nothing. The state comes back in the same payload, so this returns on
    the first ask and lets `check_pr` report the terminal reading.
    """
    if wait <= 0:
        return _pr_view(number)
    started = time.monotonic()
    while True:
        view = _pr_view(number)
        mergeable = str(view.get("mergeable") or "")
        if mergeable in {_MERGEABLE, _CONFLICTING}:
            return view
        # A terminal PR is asked exactly once: the question below is one GitHub will
        # never answer for it, so polling would spend the caller's budget on a value
        # that is `UNKNOWN` by construction. The check sits *inside* this loop rather
        # than as a read before it, so an open PR's polling costs the same number of
        # `gh` calls it always did (pinned in the suite's own read counts).
        if str(view.get("state") or "") in TERMINAL_STATES:
            return view
        remaining = wait - (time.monotonic() - started)
        if remaining <= 0:
            return view
        time.sleep(min(_MERGEABILITY_POLL_SECONDS, remaining))


def check_pr(
    number: int,
    needed: int,
    *,
    mergeability_wait: float = 0.0,
    cycles_log: str | None = None,
) -> Verdict:
    view = _view_with_computed_mergeability(number, mergeability_wait)
    head = str(view["headRefOid"])

    state = str(view.get("state") or "")
    if state in TERMINAL_STATES:
        # The PR is over, and that is a determinate reading rather than an
        # unmeasurable one: the votes it counted are history, and there is nothing
        # here to vote on, to merge, or to refresh. Measured 2026-10-03
        # (`cyc20261003-224625`): without this branch a merged PR fell into the
        # UNKNOWN-mergeability refusal below - exit 2 "could not measure" - and the
        # queue printed a `--mergeability-wait 60` for it that provably cannot
        # succeed. The live question ("how many votes does this head still have?")
        # has no subject once the branch has landed, so it is answered by saying so.
        return Verdict(
            pr=int(view.get("number") or number),
            title=str(view.get("title") or ""),
            head_sha=head,
            push_time=str(view.get("mergedAt") or ""),
            push_time_exact=True,
            mergeable=str(view.get("mergeable") or ""),
            merge_state=str(view.get("mergeStateStatus") or ""),
            state=state,
            merged_at=str(view.get("mergedAt") or ""),
        )

    mergeable, merge_state = _merge_state(view)
    # GitHub computes mergeability lazily, so a freshly pushed head reports UNKNOWN
    # for a short while. That is a question not yet answered, and the failure to
    # avoid is answering it anyway - either direction would be a guess about whether
    # the text merges. Same for a value this version does not know.
    if mergeable not in {_MERGEABLE, _CONFLICTING}:
        raise RuntimeError(
            f"#{number}: mergeable={mergeable!r} (mergeStateStatus={merge_state!r}) is "
            f"not a computed mergeability of an open PR (state={state!r}) - GitHub "
            "computes it lazily, so retry with --mergeability-wait; this check will not "
            "guess a verdict from a value it could not read"
        )
    # The second half of the gate's spelling. `MERGEABLE` alone is not `MERGEABLE`/
    # `CLEAN`: every other state either blocks the merge or says the CI conjunct is
    # unmet, and a state this version does not know is not evidence of cleanliness.
    if mergeable == _MERGEABLE and merge_state not in _KNOWN_STATES:
        raise RuntimeError(
            f"#{number}: mergeStateStatus={merge_state!r} is not a state this check "
            f"knows (known: {', '.join(sorted(_KNOWN_STATES))}). It is not "
            "read as permission - an unrecognised state may well block the merge, and "
            "reporting READY from it would be a verdict this tool has not verified"
        )

    push_time, exact, pusher = _head_push_time(head)

    # The head's check-runs, asked **only** for `UNSTABLE`: it is the one state whose
    # cause the state cannot express, and on every other head the extra `gh` call would
    # buy nothing (a `CLEAN` head's checks passed by GitHub's own reading, a `DIRTY`
    # one's cannot matter until the text merges). An `UNSTABLE` head whose checks are
    # read and all green is not blocked - see `blocked` - so this is the read that
    # decides the verdict rather than a note beside it.
    checks: tuple[HeadCheck, ...] = ()
    checks_read = False
    checks_truncated = False
    if merge_state == _UNSTABLE:
        checks, checks_truncated = _head_check_runs(head)
        checks_read = True

    # Every page, not the first 30: a truncated list drops the newest reviews,
    # which are exactly the votes that count (and the endpoint orders oldest
    # first, so the loss is invisible in the output - it just looks short).
    reviews = _gh_json_paginated(
        [
            "api",
            f"repos/{REPO}/pulls/{number}/reviews",
            "--jq",
            ".[] | {at: .submitted_at, body: .body, author: .user.login}",
        ]
    )
    reviews.sort(key=lambda r: str(r.get("at") or ""))

    votes: list[Vote] = []
    # One resolve per cycle, not per vote: the window is read from the cycle records
    # on disk, and a PR whose votes come from three cycles would otherwise scan that
    # directory three times over for the same three answers.
    windows: dict[str, tuple[bool, str]] = {}
    for r in reviews:
        assert isinstance(r, dict)
        # The projection must have applied: without it the fields arrive under
        # their raw names and `at` reads as "", which makes `"" <= push_time` true
        # and voids every vote - a full PR reporting 0/3, indistinguishable in the
        # count from a genuinely unvoted one. Cheap to check, and it is the exact
        # failure the caller-owns-the-filter rule above was written for.
        if not r.get("at"):
            raise RuntimeError(
                f"review payload for #{number} has no `at` field (keys: "
                f"{sorted(r)[:5]}) - the --jq projection did not apply, so vote "
                "times are unknown; refusing to report a count"
            )
        body = str(r.get("body") or "")
        at = str(r.get("at"))
        # Absent is not an error here, and is not read as "someone else": a payload
        # that lost the field keeps the own-head clause applied (`own_login`), which
        # is the direction that cannot credit a self-review. Unlike `at` above, the
        # loss therefore cannot turn into a *higher* count.
        author = str(r.get("author") or "")
        kind = classify(body)
        if kind == "comment":
            continue
        # Every *distinct* id the body names, not the first one. One id is the handle a
        # vote is counted under; several are not a *weaker* handle but an unusable one,
        # and the difference is not academic: taking the first id in the text
        # mis-recorded a rejection under a cycle that never wrote it (measured
        # 2026-09-16, cyc20260917-014155 - a body that named the two approvals it was
        # voiding alongside its own id was filed as `NO cyc20260917-005148`, a veto by a
        # cycle whose review said ✅). Which cycle wrote a body that names several is not
        # derivable from the body, so the honest verdict is "not measurable" and it
        # counts for none of them. `cast-vote.py` refuses to *post* such a body; this is
        # the reading-side guard for a review posted with `gh pr review` directly.
        #
        # Distinct, not occurrences: the ambiguity above is about *which* cycle wrote the
        # body, and a body that repeats one id has exactly one candidate author, so there
        # is nothing to be ambiguous about. Counting occurrences voided such a vote while
        # the poster accepted the very same body (`cast-vote.py`'s `cycles_in()` is already
        # distinct, and its refusal says "more than one cycle id"), so a body that quotes a
        # transcript containing its own id - which is what a review of the vote tooling
        # looks like - was posted by the one instrument and discarded by the other.
        # Measured 2026-09-17 on PR #1310: the counter's own output read
        # `VOID (2 cycle ids) - the vote body names 2 cycle ids
        # (cyc20260917-075555, cyc20260917-075555)`, i.e. one id, twice.
        # The reading itself lives in `distinct_cycle_ids`, next to the pattern, so
        # `tests/test_cast_vote.py` can assert the two scripts agree on it.
        ids = distinct_cycle_ids(body)
        cycle = ids[0] if len(ids) == 1 else None
        if at <= push_time:
            votes.append(
                Vote(at, kind, cycle, False, f"submitted before the head push ({push_time})",
                     tuple(ids))
            )
        elif len(ids) > 1:
            votes.append(Vote(at, kind, None, False,
                f"the vote body names {len(ids)} cycle ids ({', '.join(ids)}) - which "
                "cycle wrote it cannot be measured, so it counts for none of them; a "
                "vote body must name exactly one", tuple(ids)))
        elif cycle is None:
            votes.append(Vote(at, kind, cycle, False, "no cycle id in the vote body"))
        else:
            # The clause `cast-vote.py` refuses on, asked of a vote that is already
            # posted. It sits *after* the three reasons above so that each vote is
            # reported for its most basic defect: a body with no cycle id has no
            # window to ask about, and one that names several has no author to ask
            # for. `at <= push_time` wins for the same reason - it needs no window.
            if cycle not in windows:
                windows[cycle] = own_head_window(
                    cycle,
                    push_time=push_time,
                    push_time_exact=exact,
                    pusher=pusher,
                    cycles_log=cycles_log,
                )
            inside, why = windows[cycle]
            # …and only for a vote cast by the instance the window belongs to. The
            # clause is a self-review guard read from this host's cycle records, so
            # asking it of another instance's vote decides a question about records
            # that instance never wrote (issue #1856: two ✅ from `pm25coder` were
            # voided this way on #1851, and the PR read 0/3 with them standing).
            # `own_login` is asked *here* rather than per vote so a run whose votes
            # are all outside their windows makes no extra `gh` call.
            if inside and not own_login(author):
                votes.append(Vote(
                    at, kind, cycle, True, "", tuple(ids),
                    f"counts - cast by {author}, not this instance, so the own-head "
                    "clause is not asked of it",
                ))
            elif inside:
                votes.append(Vote(at, kind, cycle, False, why, tuple(ids)))
            else:
                # `why` is usually empty here — the head is simply outside the window —
                # but the one exemption that was decided by an *identity* reading instead
                # of by the clock carries its note, and the vote line prints it, so a
                # reader can see which datum decided (the review on #1900 asked for it).
                # It goes in `note`, which is the column a *valid* vote renders, not in
                # `why`, which only a voided one does.
                votes.append(Vote(at, kind, cycle, True, "", tuple(ids), why))

    # Walk the votes in order, resetting the run on a veto, and counting each
    # cycle at most once inside the trailing run.
    run = 0
    seen: set[str] = set()
    # Which votes `run` is actually made of. The label column says "counts" only for
    # these, because a valid approval can still fail to count: counting is per
    # *cycle*, so a second vote from a cycle already in the run is valid (it is
    # about this head, and it carries a cycle id) but contributes nothing.
    counted: list[bool] = []
    for v in votes:
        if v.kind == "veto":
            run = 0
            seen.clear()
            counted.append(True)
        elif v.valid and v.cycle is not None and v.cycle not in seen:
            seen.add(v.cycle)
            run += 1
            counted.append(True)
        else:
            # invalid approvals and repeat-cycle approvals leave the run untouched
            counted.append(False)

    return Verdict(
        pr=number,
        title=str(view["title"]),
        head_sha=head,
        push_time=push_time,
        push_time_exact=exact,
        pusher=pusher,
        mergeable=mergeable,
        merge_state=merge_state,
        state=state,
        merged_at=str(view.get("mergedAt") or ""),
        votes=votes,
        counted=counted,
        valid_count=run,
        needed=needed,
        checks=checks,
        checks_read=checks_read,
        checks_truncated=checks_truncated,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-vote-count.py",
        description="Count the LGTM votes that are still about a PR's current head.",
        # The default is rendered by argparse from `default=` below, so the help
        # line cannot go stale the way a hand-written "(default 3)" did.
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("prs", nargs="+", type=int, help="pull request number(s)")
    parser.add_argument(
        "--min-votes",
        type=int,
        default=DEFAULT_MIN_VOTES,
        help="votes required",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    parser.add_argument(
        "--cycles-log",
        default=None,
        help="directory (or os.pathsep-joined directories) of cycle records the "
             "abstention window is read from (default: $EMRG_CYCLES_LOG, else both the "
             "project's and the corpus's .emrg/memory)",
    )
    parser.add_argument(
        "--mergeability-wait",
        type=float,
        default=0.0,
        help="seconds to keep re-asking while GitHub has not computed mergeability "
        "(0 = ask once and refuse, the behaviour every non-opted-in caller keeps)",
    )
    args = parser.parse_args(argv)

    try:
        verdicts = [
            check_pr(
                n,
                args.min_votes,
                mergeability_wait=args.mergeability_wait,
                cycles_log=args.cycles_log,
            )
            for n in args.prs
        ]
    except (RuntimeError, KeyError, ValueError, AssertionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "pr": v.pr,
                        "head": v.head_sha,
                        "push_time": v.push_time,
                        "push_time_exact": v.push_time_exact,
                        "head_pusher": v.pusher,
                        "valid_votes": v.valid_count,
                        "needed": v.needed,
                        "mergeable": v.mergeable,
                        "merge_state": v.merge_state,
                        "state": v.state,
                        "merged_at": v.merged_at,
                        "terminal": v.terminal,
                        "ci_ran": v.push_time_exact,
                        "checks_read": v.checks_read,
                        "checks_green": v.checks_green,
                        "checks_note": v.checks_note,
                        "checks": [
                            {
                                "name": c.name,
                                "conclusion": c.conclusion,
                                "status": c.status,
                                "run_id": c.run_id,
                                "newest_of_name": c in set(v.newest_checks),
                            }
                            for c in v.checks
                        ],
                        "blocked": v.blocked,
                        "verdict": v.mark,
                        "enough_votes": not v.short,
                        "ready": v.ok,
                    }
                    for v in verdicts
                ],
                indent=2,
            )
        )
    else:
        for v in verdicts:
            mark = v.mark
            if v.terminal:
                # A terminal PR gets its own line shape. The live one's parenthetical
                # is "head … pushed <time>", and for a merged PR that timestamp is the
                # *merge* time - printing it as a push would misdate the head. The
                # merge state is omitted for the same reason: its `UNKNOWN/UNKNOWN` is
                # the permanent value for a merged PR (see `TERMINAL_STATES`), not a
                # reading, and showing it beside MERGED would invite it to be read as
                # one. The votes are still listed below: they are history, and a cycle
                # locating its own past votes is a real use of this report.
                when = f", merged {v.merged_at}" if v.merged_at else ""
                print(f"#{v.pr} {mark} (head {v.head_sha[:8]}{when})")
                if v.state == _MERGED:
                    print(
                        "    merged PR: this head has landed, so there is nothing here "
                        "to vote on, merge or refresh - the votes below are the history "
                        "of how it was reviewed"
                    )
                else:
                    print(
                        "    closed without merging: this PR is out of play, so there is "
                        "nothing here to vote on or merge - the votes below are its "
                        "history"
                    )
                for index, vote in enumerate(v.votes):
                    print(f"    {vote.at}      {_cycle_label(vote)} - {vote.why}")
                continue
            # A missing run is both a caveat about the count *and* the CI conjunct
            # unverified, so the line says which: `mark` is already BLOCKED here,
            # and a reader should not have to infer why from a parenthetical.
            src = (
                ""
                if v.push_time_exact
                else "  (no CI run: push time approximated by commit date; blocked)"
            )
            print(
                f"#{v.pr} {mark} {v.valid_count}/{v.needed} valid votes "
                f"(head {v.head_sha[:8]}, pushed {v.push_time}){src}"
            )
            # The merge state is printed on its own line and always, so a reader
            # never has to infer it. `READY` with `CONFLICTING` underneath is the
            # contradiction this was fixed for, and it is worth being unable to
            # produce: `mark` already refuses to say READY in that case.
            print(f"    merge state: {v.mergeable}/{v.merge_state}")
            # Printed with the state rather than only in the blocked summary, because
            # the case it exists for is a head that is *not* blocked: `UNSTABLE` with
            # every newest check-run green. A reader seeing `UNSTABLE` and nothing else
            # has to guess, and `gh pr checks` guesses the other way - it renders the
            # superseded run's `cancelled` check-run as `fail`.
            note = v.checks_note
            if note:
                print(f"    check-runs: {note}")
            for index, vote in enumerate(v.votes):
                # Whether *this* vote is part of the count `run` -- not whether it
                # could be: `valid` only says it is about this head. The two come
                # apart for a cycle's second vote, which counts once.
                contributes = (
                    v.counted[index] if index < len(v.counted) else vote.valid
                )
                # The mark column answers *counting*, except that a veto is never
                # rendered "OK": a veto submitted at the current head is `valid`
                # (it is about this head, and it carries a cycle id) while meaning
                # the opposite of approval. Measured 2026-09-11 (cycle
                # cyc20260911-130120): the valid-branch-first ordering printed all
                # five real vetoes as "OK ... counts" - an objection in the
                # approval column, and the one line a reader checks before merging.
                # Counting is still reported, in the note.
                if vote.kind == "veto":
                    mark = "NO  "
                    note = "counts - resets the run" if vote.valid else vote.why
                elif vote.valid:
                    mark = "OK  "
                    if contributes:
                        # A valid vote usually counts silently; the note is set only
                        # where the count needs saying out loud (issue #1856: a vote
                        # inside this host's window that was *not* voided).
                        note = vote.note or "counts"
                    else:
                        note = f"valid, but cycle {vote.cycle} already counted"
                else:
                    # An approval that predates the head push is a real ✅ and still
                    # does not count; rendering it "OK ... VOID" would contradict
                    # itself (measured 2026-09-11 on #1133: four lines read
                    # "OK - VOID").
                    mark, note = "VOID", vote.why
                print(f"    {vote.at} {mark} {_cycle_label(vote)} - {note}")

    # Two ways to fail, with different cures, so they are reported separately rather
    # than as one "not ready": a SHORT PR needs more review; a BLOCKED one needs the
    # conflict resolved, and no amount of further review will change that. A PR that
    # is both is listed under BLOCKED only - see `mark` for why that is the useful
    # classification rather than the pessimistic one.
    blocked = [v for v in verdicts if v.blocked]
    short = [v for v in verdicts if v.short and not v.blocked]

    if blocked:
        # One sentence per state, because the states have different cures and the
        # single sentence this replaces ("resolving the block replaces the head and
        # voids them - review after the rebase, not before") was false for most of
        # them: a `DRAFT` clears when the PR is marked ready and a `BLOCKED` clears
        # with a review, neither of which publishes a commit; an `UNSTABLE` whose
        # binding check-run belongs to a superseded run does not clear at all, and a
        # `BEHIND` one clears by a refresh that *does* move the head. What the reader
        # needs is which of those they are in, and `block_reason` already says.
        reasons = "; ".join(
            f"#{v.pr}: {v.block_reason}" for v in blocked
        )
        print(f"\n{reasons}.", file=sys.stderr)
        # The *remedy* is printed per cause too, and that half was still one sentence
        # for every row ("the branch or the pull request has to be made mergeable
        # first") - true for a conflict, and for a head whose check-runs have not
        # concluded it inverts the remedy: the answer there is to wait, and the push
        # the sentence invites voids every vote standing on the head, which the
        # sentence immediately after it warns about. Measured 2026-10-11
        # (`cyc20261011-002826`) on a queue whose two open rows were both `UNSTABLE`
        # with both checks still running. Grouped, so PRs sharing a cause are named
        # once rather than repeated per row.
        cures: dict[str, list[int]] = {}
        for v in blocked:
            cures.setdefault(v.block_cure, []).append(v.pr)
        for cure, prs in cures.items():
            naming = ", ".join(f"#{p}" for p in prs)
            print(f"{naming}: {cure}.", file=sys.stderr)
        if any(v.short for v in blocked):
            print(
                "Their votes are short too - where clearing this state means "
                "publishing a new head, that voids every vote standing here.",
                file=sys.stderr,
            )
    if short:
        print(
            f"\nNot enough votes yet (need {args.min_votes} consecutive, from different "
            "cycles, none predating the head push and none cast inside the voting "
            "cycle's own head window). A rebase voids every earlier vote.",
            file=sys.stderr,
        )

    return 1 if (short or blocked) else 0


def _entry() -> int:
    """`main`, with an unexpected failure reported as this tool's unmeasurable answer.

    Python exits `1` for an unhandled exception, and `1` is a **verdict** in this tool's
    exit table, while `2` is the code for "the question could not be answered". A caller
    that checks the code - which is how this family composes, one gate running another or
    reading its `rc` - would otherwise read a crash as a verdict. Byte-identical in every
    tool of the family, and `tests/test_a_crash_is_a_measurement_error.py` pins that.
    """
    try:
        return main()
    except Exception as exc:  # noqa: BLE001 - reported as unmeasurable, never swallowed
        traceback.print_exc()
        print(
            f"{Path(__file__).name}: could not measure - {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2  # cause: tool-failed


if __name__ == "__main__":
    raise SystemExit(_entry())
