#!/usr/bin/env python3
"""Cast a review vote on a pull request, and prove the vote counter counted it.

The class this exists for
------------------------
`check-merge-freshness.py` prices a stale verdict and, when refreshing would
spend the votes a branch already has, tells the reader to record the landing-tree
reading as a **review** — reviews are the only channel `check-vote-count.py`
reads. That advice is right, and following it exactly was still a way to lose the
vote, twice over, on 2026-09-16 (`cyc20260916-020149`):

    gh pr review 1255 --comment --body-file review1255.md    # prints nothing, rc 0
    scripts/check-vote-count.py 1255
    #   2026-09-15T18:19:15Z VOID (no cycle id) - no cycle id in the vote body

The review was posted, the command succeeded, and the count did not move. The
counter reads *the cycle* out of the body (`_CYCLE_RE`), so a body without one
describes a vote nobody cast: it is excluded from the run and the PR reads short.
Nothing in the posting path says so — `gh pr review` prints nothing on success,
and the reading that voids it is only visible if the reader re-runs the counter
afterwards, which is exactly what a reader who trusts "rc 0" does not do.

The rule, made structural
-------------------------
Two properties are needed for a posted review to be a vote, and neither is
visible at the moment of posting:

* **it carries a cycle id** — the counter's only handle on who voted. A body with
  none is void; a body with *two* is worse, because the counter silently takes the
  first and the vote's owner becomes an accident of prose order.
* **the counter counts it** — a valid vote can still fail to count (a second vote
  from a cycle already in the run contributes nothing).

So this tool refuses to post a body the counter cannot read, and then **reads the
counter back** rather than assuming the post worked. Exit 1 is reserved for the
one state the caller cannot detect on its own: posted, and not counted.

What it does NOT do
-------------------
It does not write the vote, does not decide what the vote says, and does not cast
a veto on anyone's behalf — the body is the caller's reading and is passed through
byte for byte. It also does not replace the counter's own refusal to report a
count it could not read (`--json` / exit 2 there remains the authority).

Usage
-----
    uv run --no-sync python3 scripts/cast-vote.py <PR> --body-file <path>
    uv run --no-sync python3 scripts/cast-vote.py <PR> --body-file -            # body on stdin
    uv run --no-sync python3 scripts/cast-vote.py <PR> --body-file <path> --cycle cyc20260916-020149
    uv run --no-sync python3 scripts/cast-vote.py <PR> --body-file <path> --dry-run

The cycle id is read from the body. `--cycle` is optional and exists to *check*
that reading: given one that does not appear in the body (or that is malformed),
the tool refuses rather than posting a vote under a cycle it was not told to use.

The abstention clause, asked where the vote is spent
----------------------------------------------------
The merge rules say what a vote *is*; until 2026-09-18 nothing said **who may cast
one** (issue #1408). The clause is *a cycle does not vote on a head it pushed*, and
the cycle immediately before this one counts as one's own — because every cycle on a
host is the same instance running again. `review-queue.py` owns that reading, and
`scripts/review-queue.py --cycle <id>` reports such a head as an `abstain` row.

That instrument answers the question when a cycle asks it. A cycle that does not ask
spends the vote anyway, and un-spending it is a hand edit of the review body, because
`check-vote-count.py` — the only authority on whether a vote counted — never asks who
pushed the head, and a review cannot be un-posted. Measured 2026-09-21
(`cyc20260921-114528`): a vote was cast on a head pushed inside the previous cycle's
window, and the sibling reported `abstain` for it minutes later.

So the clause is applied here too, from the same reading, and refuses to post. The
window is the previous cycle's start when it can be found (`--prev-cycle`, else the
newest cycle record that sorts before this cycle's id in `--cycles-log`, default
`$EMRG_CYCLES_LOG`, else the records beside the checkout) and this cycle's own start
when it cannot — narrowed and said so, never abandoned and never widened by a guess.
A head whose push time fell back to the commit date is **refused rather than judged**:
a lower bound cannot decide a window, so nothing is posted and the refusal names the
remedy that same missing run already has — get a run for the head, then ask again. The
counter calls such a head blocking for that same missing run, so the cost is a delay
rather than a vote nobody can recount. The other uncertain input — no previous cycle to
widen the window from — is *narrowed* rather than refused: the head is judged over this
cycle's own start and the narrowing is reported, because a clause applied over a smaller
window is a weaker reading than the rule states and must not pass as the stronger one.
Only a cycle id that names no instant at all leaves the clause inapplicable, and that is
a refusal.

Why `-` exists (issue #1462)
----------------------------
A **read-only** cycle can still vote — voting is a network action, not a file
write — but that is the tier forced exactly when the tree holds *unique* work
(community issue #979), and there it could not use this tool at all: every route
to a body file was refused, so the vote fell back to raw `gh pr review`, which is
the unsafe path this tool exists to replace (measured in cycle
`cyc20260920-104640`: that very cycle's pre-flight refused a body naming three
cycle ids, a real defect raw `gh` would have posted). So `--body-file -` names
stdin: the body is read once into memory, decoded as UTF-8 explicitly (the locale
a cycle runs in is not this tool's to assume, and the same defect class — a
reader with no `encoding=` — has already cost this repo a CI round), the existing
checks run on those bytes, and the **same text is fed to `gh pr review --body-file -`
on stdin**. Nothing is written to disk, and the checks are not weakened:
`/dev/stdin` as a *path* does not work here and never did — this tool reads the
body itself for its check, so gh would find stdin at EOF and answer
`body cannot be blank for comment review`, which is only visible at the real post.

Exit codes
----------
    0  the review is posted and `check-vote-count.py` counts it. A **veto** counts
       here too: the counter reads a counted veto as `NO ... counts - resets the
       run`, i.e. as a verdict that resets the run rather than as a vote for the
       PR, and it is neither a lost vote nor a reason to re-post. `0` does **not**
       by itself mean a review went out: `--dry-run` runs every check, posts
       nothing, and exits `0`
    1  the review is posted and does NOT count — the vote was spent for nothing;
       the counter's own reason is printed, because the remedy depends on it. The
       counter never showing the review at all is reported separately, as
       **unmeasurable** rather than as a wrong vote: the review is on GitHub and
       cannot be un-posted, so the reader re-reads before spending it
    2  nothing was posted, so nothing has to be rolled back. Grouped by the check
       that refused, not one line per `return`, and each cause carries a stable
       slug a wrapper can branch on (`body-unreadable`: the body could not be read
       from `--body-file` — a path, or `-` for stdin, and an undecodable body
       counts here rather than crashing; `cycle-id`: the body has no cycle id, or more than one,
       or `--cycle` disagrees with it; `count-unreadable`: the vote count could
       not be read; `already-voted`: this cycle already has a counted vote or a
       veto here; `own-head-window`: the abstention clause is why nothing was
       posted — either the head was pushed by this cycle or by the one immediately
       before it, or the head itself could not be judged because the window cannot
       be decided from it (no CI run for the head, so its push time is the commit
       date, a lower bound), or there was no window to apply at all;
       `gh-failed`: `gh` failed). Fail loud, and never report a posted
       vote for a review that was never sent

`gh` is required, and so is network access to GitHub: the question is about a
remote review, and every local guess would be about a different thing than the
count the merge gate reads.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = "argszero/emrg"

# `--body-file -` means stdin, the convention `gh` itself uses for the same flag.
# Named rather than written as a literal in two places, because the read path and
# the post path must agree on it: if they disagreed, the tool would read a body
# from one place and hand gh a path of "-" — the defect this constant exists to
# make impossible.
STDIN_BODY = "-"

# The gate this repo merges on, passed to the sibling counter explicitly rather
# than defaulted on both sides, so a change to the gate has one place to land.
_VOTES_NEEDED = 3

# The shape `check-vote-count.py` reads a cycle out of a review body with. Kept
# as its own copy rather than imported so the refusal is decided *before* a
# subprocess is spawned, and pinned equal to the sibling's by a test - a helper
# that accepted a different shape than the counter reads would post bodies that
# are void by construction, which is the defect it exists to prevent.
_CYCLE_RE = re.compile(r"cyc\d{8}-\d{6}")

# Every `return 2` declares which of these it is, as `# cause: <slug>` on the
# return itself, and each slug is named in the exit-code table above. The three
# copies are joined by `tests/test_cast_vote.py` in **both** directions rather
# than trusted: issue #1309 was the table and the code drifting apart silently,
# and a guard that only checks the list it already knows cannot notice a refusal
# path that is *new*. A slug is stable, so a wrapper can branch on it; that is
# also why the table names them.
RC2_CAUSES = (
    "body-unreadable",   # --body-file (a path, or `-` for stdin) could not be read
    "cycle-id",          # no cycle id, several of them, or --cycle disagrees
    "count-unreadable",  # the sibling counter raised
    "already-voted",     # this cycle already has a counted vote or a veto here
    "own-head-window",   # the head is this cycle's own, or its window cannot be decided
    "gh-failed",         # `gh pr review` itself failed
)

_SIBLING = Path(__file__).resolve().parent / "check-vote-count.py"
_sibling: object | None = None


def votes_counter():
    """The sibling module that owns "is this vote still about this head?".

    Loaded from its file rather than imported by name: the scripts in this
    directory are not importable modules (hyphenated names, no package), and this
    is the same loader `check-merge-freshness.py` uses for the same sibling.
    Imported rather than reimplemented because a second reading of the vote rule
    would be a second answer to "how many votes does this PR have", and the whole
    point here is to report the *counter's* verdict, not a lookalike.
    """
    global _sibling
    if _sibling is None:
        spec = importlib.util.spec_from_file_location("check_vote_count", _SIBLING)
        if spec is None or spec.loader is None:  # pragma: no cover - the file is in this repo
            raise RuntimeError(f"could not load {_SIBLING}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _sibling = module
    return _sibling


#: The sibling that owns the abstention window (issue #1408; the reading itself landed
#: with PR #1498). The clause — *a cycle does not vote on a head it pushed*, with the
#: window immediately before this one counted as one's own — is defined there: its two
#: ends, its narrowed form when the previous cycle cannot be found, and why an inexact
#: push time never decides it. This tool asks for that reading rather than deriving a
#: second one, because a second reading of "whose head is this" is a second answer to
#: one question, and the two would disagree exactly when it matters.
_QUEUE = Path(__file__).resolve().parent / "review-queue.py"
_queue: object | None = None


def review_queue():
    """`review-queue.py`, loaded by file for the abstention clause it owns."""
    global _queue
    if _queue is None:
        spec = importlib.util.spec_from_file_location("review_queue", _QUEUE)
        if spec is None or spec.loader is None:  # pragma: no cover - the file is in this repo
            raise RuntimeError(f"could not load {_QUEUE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _queue = module
    return _queue


def own_head_window(
    cycle: str,
    verdict: object,
    prev_cycle: str | None = None,
    cycles_log: str | None = None,
) -> tuple[str, str]:
    """The abstention clause, applied at the moment the vote would be spent.

    Answers `(refusal, note)`: a non-empty `refusal` means **nothing was posted**,
    and `note` is what to say when it is empty. `note` is also where a *narrowed*
    window is reported — a clause applied over this cycle alone is a strictly weaker
    reading than the one the rule states, so it says so rather than passing as it.

    Why this is here and not only in `review-queue.py`: that tool answers "may this
    cycle vote here" **when a cycle asks it**, and a cycle that does not ask spends
    the vote anyway. Un-spending it afterwards is a hand edit of the review body,
    because `check-vote-count.py` — the only authority on whether a vote counted —
    never asks who pushed the head. Measured 2026-09-21 (`cyc20260921-114528`): a vote
    went out on a head pushed inside the previous cycle's window, and the sibling
    reported `abstain` for that same head minutes later, from the same window this
    refuses on.

    **An undecidable window is a refusal, not a pass** — that is the sibling's own
    rule (`an unresolved window is never reported as a pass`) and it is the only
    direction that costs a delay instead of a vote nobody can recount. A head with no
    CI run has a push time that is the *commit date*, a lower bound that cannot decide
    the window; the counter already calls such a head blocking for the same missing
    run, and the remedy is the same here — get the run, then ask again.
    """
    queue = review_queue()
    if prev_cycle:
        previous, where = prev_cycle, "named by --prev-cycle"
    else:
        log = Path(
            cycles_log
            or os.environ.get("EMRG_CYCLES_LOG")
            or queue.DEFAULT_CYCLES_LOG
        )
        previous, where = queue.previous_cycle(cycle, log)
    window = queue.abstain_window(cycle, previous, where)

    note = ""
    if window.unresolved:
        narrowed = (
            f"only pushes at or after {window.window_start_text()} were checked"
            if window.applied
            else "and this cycle's own id names no instant"
        )
        note = (
            "the abstention window could not be widened to the cycle before this one "
            f"({window.unresolved}); it was applied as {window.source or 'nothing'} - "
            f"{narrowed}"
        )

    if not window.applied:
        return (
            f"the abstention clause cannot be applied: {note}. Nothing was posted - a "
            "vote that may be this cycle's own work is not worth spending, and the "
            "clause is the reason the question is asked at all (issue #1408)"
        ), note

    head = str(getattr(verdict, "head_sha", ""))
    push_time = str(getattr(verdict, "push_time", ""))
    if not getattr(verdict, "push_time_exact", False):
        return (
            f"the window cannot be decided from head {head[:8]}: it has no CI run, so "
            f"its push time ({push_time}) is the commit date - a lower bound - and a "
            "lower bound cannot tell whether the push fell inside the window this "
            "cycle treats as its own. Nothing was posted. The counter already calls "
            "such a head blocking for the same missing run, and the queue gives it the "
            "same remedy (`unblock`, not `abstain`): re-trigger a run for the head "
            "(`scripts/re-trigger-ci.sh <branch>`), then ask again - "
            "`scripts/review-queue.py --cycle <id>` reads the same head the same way"
        ), note

    pushed = queue.instant(push_time)
    if pushed is None:
        return (
            f"the window cannot be decided from head {head[:8]}: its push time "
            f"({push_time!r}) is not an instant. Nothing was posted"
        ), note

    if pushed >= window.start:
        return (
            f"the head {head[:8]} was pushed {push_time}, inside the window this cycle "
            f"treats as its own ({window.source}) - a cycle does not vote on a head it "
            "pushed, and the window immediately before this one counts as its own as "
            "well, because every cycle on a host is the same instance running again. "
            "Nothing was posted. The next vote here has to come from a later cycle "
            "(the same head is an `abstain` row in "
            "`scripts/review-queue.py --cycle <id>`); if the head is stale and its "
            "votes are at risk, measure the tree the merge would land instead of "
            "refreshing it - a push voids the votes it was meant to preserve "
            "(`scripts/check-merge-plan-suite.py <PR>`)"
        ), note

    return "", note


def _gh(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run `gh` with the program name prepended, capturing both streams.

    The program name is added here so no call site can forget it — measured
    2026-09-11 in the sibling tool: a call site that omitted it ran the POSIX `pr`
    utility instead, which reported `pr: cannot open view`, a message naming
    neither gh nor the real mistake.

    `stdin` is how a body handed to this tool on stdin reaches gh: `gh pr review
    --body-file -` reads the body from *its* stdin, and by then ours has been
    consumed by the check above, so the same text is passed down rather than
    re-read (issue #1462). The encoding is pinned for the same reason it is pinned
    on the way in.
    """
    return subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def read_stdin_body() -> str:
    """The review body when `--body-file -` names stdin instead of a path.

    Decoded as UTF-8 explicitly rather than through `sys.stdin`'s locale codec: a
    vote body is prose and may carry a ✅ or a Chinese sentence, and the locale of
    the environment a cycle runs in is not this tool's to assume. An undecodable
    body is a `UnicodeDecodeError` the caller reports as `body-unreadable` — the
    same verdict as an unreadable path — rather than a traceback after which the
    reader cannot tell whether anything was posted.

    The binary arm is taken whenever it exists; under a test `sys.stdin` is an
    `io.StringIO`, which has no `.buffer`, so the text arm is the fallback rather
    than the primary path.
    """
    binary = getattr(sys.stdin, "buffer", None)
    if binary is None:
        return sys.stdin.read()
    return binary.read().decode("utf-8")


def cycles_in(body: str) -> list[str]:
    """Every distinct cycle id in `body`, in order of first appearance."""
    seen: list[str] = []
    for match in _CYCLE_RE.finditer(body):
        found = match.group(0)
        if found not in seen:
            seen.append(found)
    return seen


def preflight(body: str, cycle: str | None) -> tuple[str | None, str]:
    """`(the cycle id to vote with, "")`, or `(None, why nothing may be posted)`.

    Decided from the body alone, before any network call: a body the counter
    cannot attribute is not worth sending, and sending it is what makes the loss
    silent (`gh pr review` prints nothing, so the caller sees success).
    """
    if cycle is not None and not _CYCLE_RE.fullmatch(cycle):
        return None, (
            f"--cycle {cycle!r} is not a cycle id (`cycYYYYMMDD-HHMMSS`) - the flag "
            "would be checked against the body, and a malformed one would refuse "
            "every body containing the real id"
        )
    found = cycles_in(body)
    if not found:
        return None, (
            "the body carries no cycle id - `check-vote-count.py` reads the voting "
            "cycle out of the body (`cycYYYYMMDD-HHMMSS`) and excludes a review "
            "without one, while `gh pr review` prints nothing either way, so the "
            "vote would be spent in silence"
        )
    if cycle is not None and cycle not in found:
        return None, (
            f"--cycle {cycle} does not appear in the body (found {', '.join(found)}) - "
            "the counter reads the id from the body, so the flag cannot speak for it"
        )
    if len(found) > 1:
        return None, (
            "the body names more than one cycle id ("
            + ", ".join(found)
            + ") - the counter reads *every* id the body names and gives a body with "
            "several no owner at all, so the vote would count for none of them "
            "rather than for whichever id came first; a vote body must name exactly "
            "one, so leave exactly one"
        )
    return found[0], ""


def _state_of(verdict: object, cycle: str) -> tuple[str, str]:
    """How the counter reads this cycle's existing vote: `(state, why)`.

    `state` is one of `"none"`, `"counted"`, `"void"`, `"veto"`. Read from the
    counter's own `counted` column rather than recomputed here: a valid vote can
    still fail to count (a second vote from a cycle already in the run), and that
    distinction is the counter's to make.

    Every vote this cycle ever cast is examined, not just the first one. A cycle
    can own several: a review submitted before a head push is void, and the same
    cycle may then cast the counted one after it — the state this tool's own
    defect produced. Returning on the first match would report `"void"` for the
    cycle that has since been counted, and would keep reporting it forever.
    """
    mine = [
        (index, vote) for index, vote in enumerate(verdict.votes) if vote.cycle == cycle
    ]
    if not mine:
        return "none", ""
    for index, vote in mine:
        counted = verdict.counted[index] if index < len(verdict.counted) else vote.valid
        if counted and vote.kind == "veto":
            return "veto", f"this cycle already vetoed #{verdict.pr} at this head"
    for index, vote in mine:
        counted = verdict.counted[index] if index < len(verdict.counted) else vote.valid
        if counted:
            return "counted", (
                f"this cycle already has a counted vote on #{verdict.pr} "
                f"(head {verdict.head_sha[:8]}) - counting is per cycle, so a second "
                "one would contribute nothing"
            )
    last = mine[-1][1]
    return "void", f"this cycle's reviews on #{verdict.pr} are void ({last.why})"


def existing_vote(
    pr: int, cycle: str, needed: int, mergeability_wait: float
) -> tuple[str, str, object]:
    """This cycle's vote state on `pr`, with the verdict it was read from.

    The verdict travels back with the state because the abstention clause needs one
    more field of it — the head's push time — and reading the counter twice for one
    question would be two readings of a head that can move between them.
    """
    verdict = votes_counter().check_pr(
        pr, needed, mergeability_wait=mergeability_wait
    )
    state, why = _state_of(verdict, cycle)
    return state, why, verdict


def confirm(
    pr: int,
    cycle: str,
    needed: int,
    attempts: int,
    delay: float,
    mergeability_wait: float = 0.0,
) -> tuple[str, str]:
    """Read the counter back until this cycle's review is visible, then judge it.

    Returns the state the counter reported — `"counted"`, `"veto"`, `"void"` or
    `"none"` — with a note, because three of those are definite answers with three
    different remedies and the fourth is the absence.

    A retry exists for one reason: GitHub registers a review a moment after the
    POST returns, so a single read can miss it and report "never appeared" for a
    review that did arrive. The retry is bounded, and a *definite* answer (counted,
    a counted **veto**, or void with a reason) returns immediately — only the
    absence retries.

    A veto belongs in that list and was missing from it. The counter reads a
    counted veto as `NO ... counts - resets the run`, i.e. `counted=True`, so
    `_state_of` answers `"veto"` — but this function short-circuited only on
    `"counted"` and `"void"`, so a veto that *had* registered fell through the
    retry loop and was reported as a review that never appeared, with the advice
    to spend the vote (measured 2026-09-17, `cyc20260917-043948`, on #1303 and
    #1305: this tool said "not counted", the counter said `counts - resets the
    run` when re-read minutes later).

    `mergeability_wait` is passed through for the other transient: a read that
    raises because GitHub has not computed mergeability yet is not a definite
    answer either, so the counter re-asks within that budget before it refuses.
    """
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(max(0.0, delay))
        verdict = votes_counter().check_pr(
            pr, needed, mergeability_wait=mergeability_wait
        )
        state, why = _state_of(verdict, cycle)
        if state == "counted":
            return "counted", f"{verdict.valid_count}/{verdict.needed} valid votes"
        if state == "veto":
            return "veto", (
                f"the veto is on the record at head {verdict.head_sha[:8]} and the "
                "counter reads it as `counts - resets the run`"
            )
        if state == "void":
            return "void", why
    return "none", (
        f"the review never appeared in the counter's reading of #{pr} after "
        f"{max(1, attempts)} attempt(s) - posted, but not readable as a vote"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cast-vote.py",
        description="Post a review vote and confirm the vote counter counted it.",
    )
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument(
        "--body-file",
        required=True,
        help="file holding the review body, or `-` to read it from stdin",
    )
    parser.add_argument(
        "--cycle",
        default=None,
        help="the cycle id the vote is cast under; must be the one in the body",
    )
    parser.add_argument(
        "--prev-cycle",
        default=None,
        help="the cycle immediately before this one, whose start is the abstention "
        "window's start; read from the cycle records when omitted",
    )
    parser.add_argument(
        "--cycles-log",
        default=None,
        help="directory holding the `cycle-<date>-<time>.md` records the previous "
        "cycle is read from when --prev-cycle is not given (default: "
        "$EMRG_CYCLES_LOG, else the records beside the checkout)",
    )
    parser.add_argument("--repo", default=REPO, help="owner/name the PR lives in")
    parser.add_argument(
        "--min-votes",
        type=int,
        default=_VOTES_NEEDED,
        help="votes the gate requires (passed to the counter)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="how many times to re-read the counter while the review settles",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="seconds between those re-reads",
    )
    parser.add_argument(
        "--mergeability-wait",
        type=float,
        default=60.0,
        help="seconds the counter may keep re-asking while GitHub has not computed "
        "mergeability yet (a fresh push reports UNKNOWN for up to a couple of "
        "minutes; 0 asks once and refuses, which is how a vote is lost to timing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check, post nothing",
    )
    args = parser.parse_args(argv)

    from_stdin = args.body_file == STDIN_BODY
    try:
        body = (
            read_stdin_body()
            if from_stdin
            else Path(args.body_file).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError) as exc:
        print(f"could not read {args.body_file}: {exc}", file=sys.stderr)
        return 2  # cause: body-unreadable

    cycle, why = preflight(body, args.cycle)
    if cycle is None:
        print(f"refusing to post: {why}", file=sys.stderr)
        return 2  # cause: cycle-id

    try:
        state, note, verdict = existing_vote(
            args.pr, cycle, args.min_votes, args.mergeability_wait
        )
    except Exception as exc:  # noqa: BLE001 - the counter fails loud; say why, post nothing
        print(f"refusing to post: the vote count could not be read ({exc})", file=sys.stderr)
        return 2  # cause: count-unreadable
    if state in {"counted", "veto"}:
        print(f"refusing to post: {note}", file=sys.stderr)
        return 2  # cause: already-voted
    if state == "void":
        print(f"note: {note}", file=sys.stderr)

    refusal, window_note = own_head_window(
        cycle, verdict, prev_cycle=args.prev_cycle, cycles_log=args.cycles_log
    )
    if window_note:
        # Ahead of the refusal, because the note describes the *reading* rather than the
        # outcome: a window narrowed to this cycle alone is weaker than the rule states,
        # and the refusal it produces is exactly the case a reader has to know that for.
        print(f"note: {window_note}", file=sys.stderr)
    if refusal:
        print(f"refusing to post: {refusal}", file=sys.stderr)
        return 2  # cause: own-head-window

    if args.dry_run:
        print(f"dry run: would cast a review on #{args.pr} as {cycle}")
        return 0

    proc = _gh(
        [
            "pr",
            "review",
            str(args.pr),
            "-R",
            args.repo,
            "--comment",
            "--body-file",
            args.body_file,
        ],
        stdin=body if from_stdin else None,
    )
    if proc.returncode != 0:
        print(
            f"gh failed (rc={proc.returncode}): gh pr review {args.pr}\n"
            f"{proc.stderr.strip()}",
            file=sys.stderr,
        )
        return 2  # cause: gh-failed

    state, note = confirm(
        args.pr,
        cycle,
        args.min_votes,
        args.attempts,
        args.delay,
        args.mergeability_wait,
    )
    if state == "counted":
        print(f"#{args.pr}: review posted as {cycle} and counted - {note}")
        return 0
    if state == "veto":
        print(
            f"#{args.pr}: review posted as {cycle} and counted as a VETO - {note}\n"
            "A veto is not a lost vote: it is on the record and it resets the run, so "
            f"#{args.pr} now needs three consecutive LGTMs from other cycles. "
            "Re-posting contributes nothing - re-read it with "
            f"scripts/check-vote-count.py {args.pr}."
        )
        return 0
    if state == "void":
        print(
            f"#{args.pr}: review POSTED and NOT counted - {note}\n"
            "The vote was spent for nothing. Nothing is rolled back by re-posting: a "
            "second review from this cycle contributes nothing either, so fix the body "
            "and let a later cycle vote.",
            file=sys.stderr,
        )
        return 1
    print(
        f"#{args.pr}: review posted, and the counter never showed it - {note}\n"
        "That is unmeasurable, not a verdict: the review is on GitHub and cannot be "
        "un-posted, and it can register after this tool's bounded retries. Do not "
        "spend it and do not re-post - re-read the counter first "
        f"(scripts/check-vote-count.py {args.pr}) and act on what it says.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
