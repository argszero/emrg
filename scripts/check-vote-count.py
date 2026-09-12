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
  *pair*, so a `MERGEABLE` PR that is `UNSTABLE` (checks failing or unfinished),
  `BEHIND`, `BLOCKED` or `DRAFT` is also blocked. Reading only `mergeable` is what
  let a **draft** pull request - which no vote can merge - print `READY`.
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
is still about the tree that would merge (a question about *which* tree ran CI, not
about whether checks pass - `UNSTABLE` answers that one, and is read above). Likewise
`check-merge-tree-health.py` (PR #1155) asks whether the merged tree passes the
repo's own guards.

Push time, and the honest bound
-------------------------------
A vote counts only if it was submitted *after the head was pushed*. The push time is
taken from the earliest workflow run created for that exact SHA, because that is
when GitHub received the push event - precisely the moment the earlier votes stopped
being about the current head. When no run exists for the head, this falls back to
the head commit's committer date and **says so in the output**: a commit date can
precede the push, so the fallback is the optimistic direction and must not be
silently trusted. The fallback is also the case where the PR has no CI at all,
which is not mergeable anyway.

Usage
-----
    uv run --no-sync python3 scripts/check-vote-count.py <PR> [<PR> ...]
    uv run --no-sync python3 scripts/check-vote-count.py <PR> --json
    uv run --no-sync python3 scripts/check-vote-count.py <PR> --min-votes 2

Exit codes
----------
    0  every PR has >= --min-votes (default 3) valid votes **and** is
       `MERGEABLE`/`CLEAN`
    1  at least one PR is SHORT (too few votes) or BLOCKED (cannot be merged)
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
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

REPO = "argszero/emrg"

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

# The states in which the merge cannot proceed right now, each with the reason the
# reader needs. GitHub's `MergeStateStatus` vocabulary:
#
#   DIRTY       the merge conflicts
#   UNSTABLE    mergeable, but commit status is not passing  <- the CI conjunct
#   BEHIND      the head is out of date with the base branch
#   BLOCKED     GitHub blocks the merge (protection rules / required reviews)
#   DRAFT       the pull request is a draft
_NON_CLEAN_STATES = {
    "DIRTY": "the merge conflicts",
    "UNSTABLE": "checks are failing or have not finished",
    "BEHIND": "the head is behind the base branch",
    "BLOCKED": "GitHub reports the merge blocked (protection rules or required reviews)",
    "DRAFT": "the pull request is a draft",
}

# `HAS_HOOKS` ("merge commits are conditioned on hooks") is deliberately NOT in the
# map: whether it permits a merge is not something this tool can establish, and
# guessing either way would put an unverified verdict behind a gate. It falls to the
# fail-loud branch with every other unrecognised value.

# The runnable form, as Agent.md documents it. A constant (the same convention as
# check-doc-count.py) so the doc line and the guard that checks it cannot drift
# into agreeing on a string that no longer runs anything.
INVOCATION = "uv run --no-sync python3 scripts/check-vote-count.py"

# `cyc20260911-091230` - the cycle id the vote comments carry.
_CYCLE_RE = re.compile(r"cyc\d{8}-\d{6}")

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


def _verdict_line(body: str) -> str:
    """The body's first line that has content, with leading decoration stripped.

    Decoration-only lines are skipped, so a reviewer who opens with a `---` rule
    has still stated their verdict on the line after it.
    """
    for line in body.splitlines():
        text = line
        while True:
            reduced = _ORDERED_ITEM_RE.sub("", text.lstrip(_DECORATION), count=1)
            if reduced == text:
                break
            text = reduced
        if text.strip():
            return text
    return ""


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


def _classify(body: str) -> str:
    """`veto`, `approve` or `comment`, from the verdict mark on the first line.

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
    return "comment"


@dataclass
class Vote:
    at: str
    kind: str
    cycle: str | None
    valid: bool
    why: str


@dataclass
class Verdict:
    pr: int
    title: str
    head_sha: str
    push_time: str
    push_time_exact: bool
    mergeable: str = ""
    merge_state: str = ""
    votes: list[Vote] = field(default_factory=list)
    valid_count: int = 0
    needed: int = 3

    @property
    def short(self) -> bool:
        """Too few votes. A statement about review, not about mergeability."""
        return self.valid_count < self.needed

    @property
    def blocked(self) -> bool:
        """The merge cannot proceed: the gate's `MERGEABLE`/`CLEAN` is not satisfied.

        Kept distinct from `short` because the two call for opposite responses:
        short means "come back after more review", blocked means "review is done and
        this still cannot land". Collapsing them is what let six CONFLICTING PRs sit
        behind six `READY` lines, each looking like it was waiting on a formality.

        Covers every non-clean state, not only a conflict. An earlier version tested
        `mergeable == CONFLICTING` and so reported `READY` for `MERGEABLE`/`UNSTABLE`
        (CI not green), `/BEHIND`, `/BLOCKED` and `/DRAFT` - a draft PR, which nobody
        can merge at all. The gate's spelling is the pair, so the pair is tested.
        """
        return self.mergeable == _CONFLICTING or self.merge_state in _NON_CLEAN_STATES

    @property
    def block_reason(self) -> str:
        """Why this PR cannot merge, in the reader's terms."""
        if self.mergeable == _CONFLICTING:
            return "Git cannot merge the text (CONFLICTING)"
        reason = _NON_CLEAN_STATES.get(self.merge_state)
        if reason:
            return f"merge state is {self.merge_state} - {reason}"
        return ""

    @property
    def ok(self) -> bool:
        return not self.short and not self.blocked

    @property
    def mark(self) -> str:
        """`BLOCKED` whenever the text cannot merge, `SHORT` for a vote deficit.

        Blocked wins even when the votes are also short. The conflict is the
        blocking fact: it has to be resolved first, and resolving it pushes a new
        head, which voids every vote counted here. Reporting `SHORT` in that state
        would read as "come back after more review" and send a reviewer to do work
        that the next rebase throws away - the same misdirection as the original
        bug, one layer down.
        """
        if self.blocked:
            return "BLOCKED"
        if self.short:
            return "SHORT"
        return "READY"


def _head_push_time(head: str) -> tuple[str, bool]:
    """Earliest CI run creation time for this SHA, else the commit date.

    Returns `(timestamp, is_exact)`. The run's `createdAt` is the push event time;
    a commit date can precede the push, so the fallback is flagged rather than
    silently used.
    """
    runs = _gh_json(
        [
            "api",
            f"repos/{REPO}/actions/runs?head_sha={head}&per_page=100",
            "--jq",
            '{t: ([.workflow_runs[].created_at] | sort | .[0] // "")}',
        ]
    )
    assert isinstance(runs, dict)
    created = runs.get("t")
    if isinstance(created, str) and created:
        return created, True

    commit = _gh_json(["api", f"repos/{REPO}/commits/{head}", "--jq", "{t: .commit.committer.date}"])
    assert isinstance(commit, dict)
    committer_date = commit.get("t")
    if not isinstance(committer_date, str) or not committer_date:
        raise RuntimeError(f"cannot determine a push time for head {head[:8]}")
    return committer_date, False


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


def check_pr(number: int, needed: int) -> Verdict:
    view = _gh_json(
        [
            "pr",
            "view",
            str(number),
            "-R",
            REPO,
            "--json",
            "number,title,headRefOid,mergeable,mergeStateStatus",
        ]
    )
    assert isinstance(view, dict)
    head = str(view["headRefOid"])

    mergeable, merge_state = _merge_state(view)
    # GitHub computes mergeability lazily, so a freshly pushed head reports UNKNOWN
    # for a short while. That is a question not yet answered, and the failure to
    # avoid is answering it anyway - either direction would be a guess about whether
    # the text merges. Same for a value this version does not know.
    if mergeable not in {_MERGEABLE, _CONFLICTING}:
        raise RuntimeError(
            f"#{number}: mergeable={mergeable!r} (mergeStateStatus={merge_state!r}) is "
            "not a computed mergeability - GitHub reports UNKNOWN until it finishes "
            "computing, and this check will not guess a verdict from it"
        )
    # The second half of the gate's spelling. `MERGEABLE` alone is not `MERGEABLE`/
    # `CLEAN`: every other state either blocks the merge or says the CI conjunct is
    # unmet, and a state this version does not know is not evidence of cleanliness.
    if mergeable == _MERGEABLE and merge_state not in {_CLEAN, *_NON_CLEAN_STATES}:
        raise RuntimeError(
            f"#{number}: mergeStateStatus={merge_state!r} is not a state this check "
            f"knows (known: CLEAN, {', '.join(sorted(_NON_CLEAN_STATES))}). It is not "
            "read as permission - an unrecognised state may well block the merge, and "
            "reporting READY from it would be a verdict this tool has not verified"
        )

    push_time, exact = _head_push_time(head)

    # Every page, not the first 30: a truncated list drops the newest reviews,
    # which are exactly the votes that count (and the endpoint orders oldest
    # first, so the loss is invisible in the output - it just looks short).
    reviews = _gh_json_paginated(
        [
            "api",
            f"repos/{REPO}/pulls/{number}/reviews",
            "--jq",
            ".[] | {at: .submitted_at, body: .body}",
        ]
    )
    reviews.sort(key=lambda r: str(r.get("at") or ""))

    votes: list[Vote] = []
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
        kind = _classify(body)
        if kind == "comment":
            continue
        match = _CYCLE_RE.search(body)
        cycle = match.group(0) if match else None
        if at <= push_time:
            votes.append(
                Vote(at, kind, cycle, False, f"submitted before the head push ({push_time})")
            )
        elif cycle is None:
            votes.append(Vote(at, kind, cycle, False, "no cycle id in the vote body"))
        else:
            votes.append(Vote(at, kind, cycle, True, ""))

    # Walk the votes in order, resetting the run on a veto, and counting each
    # cycle at most once inside the trailing run.
    run = 0
    seen: set[str] = set()
    for v in votes:
        if v.kind == "veto":
            run = 0
            seen.clear()
        elif v.valid and v.cycle is not None and v.cycle not in seen:
            seen.add(v.cycle)
            run += 1
        # invalid approvals and repeat-cycle approvals leave the run untouched

    return Verdict(
        pr=number,
        title=str(view["title"]),
        head_sha=head,
        push_time=push_time,
        push_time_exact=exact,
        mergeable=mergeable,
        merge_state=merge_state,
        votes=votes,
        valid_count=run,
        needed=needed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-vote-count.py",
        description="Count the LGTM votes that are still about a PR's current head.",
    )
    parser.add_argument("prs", nargs="+", type=int, help="pull request number(s)")
    parser.add_argument("--min-votes", type=int, default=3, help="votes required (default 3)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    args = parser.parse_args(argv)

    try:
        verdicts = [check_pr(n, args.min_votes) for n in args.prs]
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
                        "valid_votes": v.valid_count,
                        "needed": v.needed,
                        "mergeable": v.mergeable,
                        "merge_state": v.merge_state,
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
            src = "" if v.push_time_exact else "  (no CI run: push time approximated by commit date)"
            print(
                f"#{v.pr} {mark} {v.valid_count}/{v.needed} valid votes "
                f"(head {v.head_sha[:8]}, pushed {v.push_time}){src}"
            )
            # The merge state is printed on its own line and always, so a reader
            # never has to infer it. `READY` with `CONFLICTING` underneath is the
            # contradiction this was fixed for, and it is worth being unable to
            # produce: `mark` already refuses to say READY in that case.
            print(f"    merge state: {v.mergeable}/{v.merge_state}")
            for vote in v.votes:
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
                    mark, note = "OK  ", "counts"
                else:
                    # An approval that predates the head push is a real ✅ and still
                    # does not count; rendering it "OK ... VOID" would contradict
                    # itself (measured 2026-09-11 on #1133: four lines read
                    # "OK - VOID").
                    mark, note = "VOID", vote.why
                print(f"    {vote.at} {mark} {vote.cycle or '(no cycle id)'} - {note}")

    # Two ways to fail, with different cures, so they are reported separately rather
    # than as one "not ready": a SHORT PR needs more review; a BLOCKED one needs the
    # conflict resolved, and no amount of further review will change that. A PR that
    # is both is listed under BLOCKED only - see `mark` for why that is the useful
    # classification rather than the pessimistic one.
    blocked = [v for v in verdicts if v.blocked]
    short = [v for v in verdicts if v.short and not v.blocked]

    if blocked:
        also_short = " (their votes are short too, but resolving the block " \
            "replaces the head and voids them - review after the rebase, not before)"
        reasons = "; ".join(
            f"#{v.pr}: {v.block_reason}" for v in blocked
        )
        print(
            f"\n{reasons}. More review does not fix this - the branch or the pull "
            "request has to be made mergeable first."
            + (also_short if any(v.short for v in blocked) else ""),
            file=sys.stderr,
        )
    if short:
        print(
            f"\nNot enough votes yet (need {args.min_votes} consecutive, from different "
            "cycles, none predating the head push). A rebase voids every earlier vote.",
            file=sys.stderr,
        )

    return 1 if (short or blocked) else 0


if __name__ == "__main__":
    raise SystemExit(main())
