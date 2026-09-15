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
    uv run --no-sync python3 scripts/cast-vote.py <PR> --body-file <path> --cycle cyc20260916-020149
    uv run --no-sync python3 scripts/cast-vote.py <PR> --body-file <path> --dry-run

The cycle id is read from the body. `--cycle` is optional and exists to *check*
that reading: given one that does not appear in the body (or that is malformed),
the tool refuses rather than posting a vote under a cycle it was not told to use.

Exit codes
----------
    0  the review is posted and `check-vote-count.py` counts it
    1  the review is posted and does NOT count — the vote was spent for nothing;
       the counter's own reason is printed, because the remedy depends on it
    2  nothing was posted: the body has no cycle id (or more than one), `--cycle`
       disagrees with it, this cycle already has a counted vote here, or `gh`
       failed — fail loud, and never report a posted vote for a review that was
       never sent

`gh` is required, and so is network access to GitHub: the question is about a
remote review, and every local guess would be about a different thing than the
count the merge gate reads.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = "argszero/emrg"

# The gate this repo merges on, passed to the sibling counter explicitly rather
# than defaulted on both sides, so a change to the gate has one place to land.
_VOTES_NEEDED = 3

# The shape `check-vote-count.py` reads a cycle out of a review body with. Kept
# as its own copy rather than imported so the refusal is decided *before* a
# subprocess is spawned, and pinned equal to the sibling's by a test - a helper
# that accepted a different shape than the counter reads would post bodies that
# are void by construction, which is the defect it exists to prevent.
_CYCLE_RE = re.compile(r"cyc\d{8}-\d{6}")

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


def _gh(args: list[str]) -> subprocess.CompletedProcess:
    """Run `gh` with the program name prepended, capturing both streams.

    The program name is added here so no call site can forget it — measured
    2026-09-11 in the sibling tool: a call site that omitted it ran the POSIX `pr`
    utility instead, which reported `pr: cannot open view`, a message naming
    neither gh nor the real mistake.
    """
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


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
            + ") - the counter takes the first match, which makes the vote's owner an "
            "accident of prose order; leave exactly one"
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


def existing_vote(pr: int, cycle: str, needed: int) -> tuple[str, str]:
    """This cycle's vote state on `pr`, read from the counter."""
    return _state_of(votes_counter().check_pr(pr, needed), cycle)


def confirm(pr: int, cycle: str, needed: int, attempts: int, delay: float) -> tuple[bool, str]:
    """Read the counter back until this cycle's review is visible, then judge it.

    A retry exists for one reason: GitHub registers a review a moment after the
    POST returns, so a single read can miss it and report "never appeared" for a
    review that did arrive. The retry is bounded, and a *definite* answer (counted,
    or void with a reason) returns immediately — only the absence retries.
    """
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(max(0.0, delay))
        verdict = votes_counter().check_pr(pr, needed)
        state, why = _state_of(verdict, cycle)
        if state == "counted":
            return True, f"{verdict.valid_count}/{verdict.needed} valid votes"
        if state == "void":
            return False, why
    return False, (
        f"the review never appeared in the counter's reading of #{pr} after "
        f"{max(1, attempts)} attempt(s) - posted, but not readable as a vote"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cast-vote.py",
        description="Post a review vote and confirm the vote counter counted it.",
    )
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("--body-file", required=True, help="file holding the review body")
    parser.add_argument(
        "--cycle",
        default=None,
        help="the cycle id the vote is cast under; must be the one in the body",
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
        "--dry-run",
        action="store_true",
        help="run every check, post nothing",
    )
    args = parser.parse_args(argv)

    try:
        body = Path(args.body_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"could not read {args.body_file}: {exc}", file=sys.stderr)
        return 2

    cycle, why = preflight(body, args.cycle)
    if cycle is None:
        print(f"refusing to post: {why}", file=sys.stderr)
        return 2

    try:
        state, note = existing_vote(args.pr, cycle, args.min_votes)
    except Exception as exc:  # noqa: BLE001 - the counter fails loud; say why, post nothing
        print(f"refusing to post: the vote count could not be read ({exc})", file=sys.stderr)
        return 2
    if state in {"counted", "veto"}:
        print(f"refusing to post: {note}", file=sys.stderr)
        return 2
    if state == "void":
        print(f"note: {note}", file=sys.stderr)

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
        ]
    )
    if proc.returncode != 0:
        print(
            f"gh failed (rc={proc.returncode}): gh pr review {args.pr}\n"
            f"{proc.stderr.strip()}",
            file=sys.stderr,
        )
        return 2

    counted, note = confirm(args.pr, cycle, args.min_votes, args.attempts, args.delay)
    if counted:
        print(f"#{args.pr}: review posted as {cycle} and counted - {note}")
        return 0
    print(
        f"#{args.pr}: review POSTED and NOT counted - {note}\n"
        "The vote was spent for nothing. Nothing is rolled back by re-posting: a "
        "second review from this cycle contributes nothing either, so fix the body "
        "and let a later cycle vote.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
