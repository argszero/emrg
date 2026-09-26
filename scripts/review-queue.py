#!/usr/bin/env python3
"""What may a cycle do about each open PR? One row per PR, one next action.

Issue #1340 (2026-09-17). Every part of this reading was already mechanical, and
none of it was assembled: a cycle's Step 1 had to hold four rules at once, each
living in a different tool, docstring, or nowhere —

1. **counted** votes are the votes that postdate the head push (a push voids them);
2. a head that no longer contains master is **not** unvotable: the remedy is a vote
   on the *landing tree* (`check-merge-plan-suite.py <PR>`), and that does **not**
   move the head, so the votes already standing survive — and the reading the review
   is *of* is that landing change too (`check-merge-landing-diff.py <PR>`), because
   `diff(master, head)` on such a head shows the base's own later commits as
   reversals the PR does not make; a review of that diff is a review of the wrong
   change, which is the one way following this row could still produce a wrong vote;
3. a head whose CI run is red, still running, or absent has three *different*
   remedies, none of which is "cast a vote and move on";
4. a standing ❌ at the current head makes a vote wrong rather than useless — that
   PR needs a **fix push**, not more review.

Measured cost of the misreading (cycle `cyc20260917-190356`): seven open PRs
received no votes in a cycle where nothing prevented them. Once the rules were
re-derived by hand, five votes were cast in one cycle and none of them needed a git
write. At the observed cadence — one cycle every ~20 minutes, ~8 open PRs, 3 votes
each — "one vote per cycle" is ~24 cycles where "a vote on every PR it can" is 2–3.

Where each fact comes from, and why not from a lookalike
--------------------------------------------------------
The two halves of this reading are **owned by two tools** and neither is
re-implemented here:

* `check-vote-count.py` owns "how many votes are still about this head?" — the
  count, the per-cycle rule, and the mergeability clause;
* `check-merge-freshness.py` owns "is the green CI about the tree that would land?"
  — the ancestry, and the four ways a verdict can fail to be current.

So the number printed here is the counter's number and the staleness here is the
freshness tool's *kind*, not a local re-derivation of either. That matters more than
it looks: `behind_by > 0` is a plausible stand-in for "stale", and it is not the same
question — the freshness tool asks whether master's tip is an *ancestor* of the head
(a graph property), and it names four distinct states where a count comparison would
have produced one word. A second implementation of the vote rule would be a second
answer to "may we merge this", which is the number every decision below turns on.

Two things the output never does
--------------------------------
* **A count it could not read is not a zero.** `0/3 votes` is the line that says
  "vote freely"; printing it without having read it spends votes in the direction
  that cannot be undone. An unreadable count is reported as `?` with the reason, and
  the action becomes "read it first".
* **Silence is not an answer.** A failed `gh pr list` and an empty queue are
  different facts, so a failed listing exits 2 and says so, where `no open PRs in
  argszero/emrg - nothing to review` is printed only for a queue actually read.

Cost: two `gh pr view` calls and roughly six more API calls per PR, because each
sibling is asked for its own half — measured at ~12 seconds per PR, so a queue of
eight is ~1.5 minutes. When only one PR matters, name it — positionally — to ask
about it alone.

`--mergeability-wait` is the one flag with a non-zero default, and the reason is
the shape of the failure rather than a preference: GitHub computes mergeability
lazily and reports `UNKNOWN` until it has, so the *newest* PR in the queue — the
one that was pushed seconds ago, which is exactly the state this tool exists to
classify — answers "not computed yet". Reported as `read-first` that is a false
`?` on the row a cycle most wants answered. The budget is spent re-asking the same
question and nothing else: after it runs out the counter's own refusal stands
unchanged, and the row still reads `?` with the reason. A small budget rather than
the freshness gate's 60s because this loop pays it per unread PR, while that gate
pays it once for a merge it is about to make.

Usage
-----
    uv run --no-sync python3 scripts/review-queue.py                    # the whole queue
    uv run --no-sync python3 scripts/review-queue.py 1342 1343          # just these
    uv run --no-sync python3 scripts/review-queue.py --cycle cyc20260917-221117
    uv run --no-sync python3 scripts/review-queue.py --json

`--cycle` is what turns "may this PR be voted on" into "may *this cycle* still vote
here" — the counter counts per cycle, so a cycle that has already voted at a head
must get its next vote from another cycle. Without it the tool reports the first
question only, and says so: `window_note`'s `no --cycle was given` line is printed
ahead of the rows, because a reader who has already reached a row has already copied
its command. Until 2026-09-26 that sentence had no carrier — the prose of an
unflagged run was identical in shape to a windowed one — so the one tool a cycle runs
first gave a wrong reading (`vote` for a head its own clause answers `abstain`) with
nothing in the report to say the two questions had become one. The refusal that
catches it lives one command later, in `cast-vote.py`'s `own-head-window`; a report
whose rows are right is cheaper than a backstop that has to disagree with them.

The other half of "may this cycle vote here" is the clause the counter cannot see
-------------------------------------------------------------------------------
Issue #1408. The clause is **a cycle does not vote on a head it pushed** (nor merge
it) and, because every cycle on a host is the same instance running again, the
**immediately preceding cycle's window counts as one's own** — the applied precedent
is `cyc20260917-125823`, which refused to endorse `#1315`'s head 25 minutes after
`cyc20260917-122459` pushed it. It has been applied by hand every cycle since, and
two measured cycles show the cost in both directions: `cyc20260919-065231` cast a
vote on the head it had just refreshed and had to withdraw it by hand, and
`cyc20260920-214143` found this tool answering `vote` for two heads the cycle
immediately before it had pushed.

*Who pushed a head* is not a fact GitHub records, so the clause has to be read off
the clock, and a cycle id **is** its start time in the host's local zone
(`cyc20260917-221117` began at 22:11:17 local). So the window is decidable from two
datums this tool already has — the push time (the counter prints it) and the cycle's
id — plus one it does not: **the previous cycle**, which `--prev-cycle` supplies and
which is otherwise read from the cycle records in either directory the evolution
template may name them in (`--cycles-log`, default `DEFAULT_CYCLES_LOGS` — the
project memory root inside the checkout, which D9 made the template's path, and
the evolution root beside it, which holds the corpus). A head pushed inside
`[previous cycle's start, now)` is reported `abstain` instead of `vote` or `merge`.

When the previous cycle cannot be found, `--cycle` is not silently ignored: the
window shrinks to this cycle's own start, a `vote` row for an earlier push comes
with the push time in view, and the queue prints what it could not determine. An
unresolved window is never reported as a pass — the whole point of the clause is
that the wrong direction is the one that spends a vote nobody can recount.

Exit codes
----------
    0  every PR was read and classified (the classification itself may say "someone
       has to push a fix"; that is an answer, not a failure)
    2  the queue could not be listed, or a PR in it could not be read - the family's
       contract, and the reason for it here: on the unreadable row the honest
       output is `?`, and a caller that took the whole run for a green light would
       act on a count nobody has. Fail loud rather than a silently short queue.

`gh` and network access to GitHub are required; there is no offline mode.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = "argszero/emrg"

#: How the family's tools are invoked (`Agent.md`, "Test Commands"). Printed
#: commands carry the runner the docstrings and the docs prescribe — a bare
#: `scripts/x.py` is not executable on this host, so printing one would hand the
#: reader a command that fails.
RUNNER = "uv run --no-sync python3"

SCRIPTS_DIR = Path(__file__).resolve().parent

#: Where the evolution task writes its cycle records — one `cycle-<date>-<time>.md`
#: per cycle, the cycle's id without its `cyc` prefix in the filename, which is why
#: the id is rebuilt rather than read off.
#:
#: **Two roots, because the template's path has moved and the corpus has not.**
#: `SCRIPTS_DIR` is `<source_dir>/scripts`, so `SCRIPTS_DIR.parent` is the checkout and
#: `SCRIPTS_DIR.parent.parent` is the evolution root beside it. The template **in this
#: tree** names `{{ source_dir }}/.emrg/memory/cycle-{{ timestamp }}.md` — the checkout,
#: where D9 (PR #1555) re-based the memory roots onto the project root the daemon loads.
#: The template an installed daemon **delivers** still names the other one
#: (`{{ evolution_cwd }}/.emrg/memory/...`), because the daemon renders the *installed*
#: template and the hosts have not reinstalled since D9. So the corpus sits on one side
#: and the next records will land on the other — measured 2026-09-25: 1,354 `cycle-*.md`
#: beside the checkout, 0 inside it.
#:
#: Both roots are searched and the newest record that sorts before this cycle's id
#: **across the union** is the answer. Reading only the checkout root leaves the window
#: unresolvable for every cycle until records accumulate there; reading only the
#: evolution root pins it, after the reinstall, to the last cycle before the reinstall —
#: a window that never advances. A guard ties this set to a template's path
#: (`tests/test_review_queue.py::test_the_prompt_writes_its_cycle_records_where_the_queue_reads_them`):
#: it renders this tree's `evolution_prompt.md` and requires every cycle-record path in
#: it to be one of these roots, so reader and template cannot drift apart silently again.
#:
#: Overridable by `--cycles-log` / `EMRG_CYCLES_LOG` (one directory, or several joined by
#: `os.pathsep`); a directory that is not there is reported as unreadable, never guessed at.
DEFAULT_CYCLES_LOGS: tuple[Path, ...] = (
    SCRIPTS_DIR.parent / ".emrg" / "memory",          # this tree's template, and D9's root
    SCRIPTS_DIR.parent.parent / ".emrg" / "memory",   # the installed template's root, and the corpus
)


def resolve_cycle_logs(override: str | None = None) -> tuple[Path, ...]:
    """The cycle-record directories to search: the override, else both defaults.

    `--cycles-log` and `EMRG_CYCLES_LOG` keep their old meaning for a single directory
    and accept several joined by `os.pathsep`, so a caller can name the pair
    explicitly instead of relying on the defaults.
    """
    raw = override or os.environ.get("EMRG_CYCLES_LOG")
    if raw:
        return tuple(Path(part) for part in raw.split(os.pathsep) if part)
    return DEFAULT_CYCLES_LOGS


# ── the abstention window ────────────────────────────────────────────────────
#
# "May this cycle vote here?" has two halves. The counter owns the first — how many
# counted votes are still about this head. The second is *whose head is it*, and it
# is read off the clock, because GitHub does not attribute a push to a cycle.

_CYCLE_ID = re.compile(r"cyc(\d{8})-(\d{6})")
#: A cycle record's *filename* is `cycle-<date>-<time>.md` — the same instant as the
#: id without its `cyc` prefix, so the id has to be rebuilt rather than read off.
_CYCLE_RECORD = re.compile(r"cycle-(\d{8}-\d{6})\.md")


def cycle_start(cycle: str) -> datetime | None:
    """The instant a cycle id names, as an aware datetime in the local zone.

    A cycle id is its start time *in local time* (`cyc20260917-221117` began at
    22:11:17 local), while a push time arrives as UTC. Comparing the two without
    converting is an error of whole hours that still reads as an answer, so the
    conversion happens once, here.
    """
    match = _CYCLE_ID.fullmatch(cycle or "")
    if not match:
        return None
    try:
        naive = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return naive.astimezone()


def instant(text: str) -> datetime | None:
    """A GitHub timestamp as an aware datetime; `None` when it is not one."""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def previous_cycle(cycle: str, cycles_logs) -> tuple[str, str]:
    """`(the cycle immediately before `cycle`, where it was found)`.

    The newest cycle record whose id sorts before this cycle's; ids are fixed width,
    so string order is time order, and a file's `<date>-<time>` stem is turned back
    into an id here because the filename does not carry the `cyc` prefix. Several
    directories are searched (`DEFAULT_CYCLES_LOGS`), and the newest record wins
    *across* them, so a corpus that spans two roots — the state after the prompt's
    record path moved — is read whole rather than half.

    `("", reason)` when there is none — an unresolvable window is the one input this
    reading must not invent, because inventing it spends a vote. A directory that
    cannot be read is named in the answer rather than passed over: an answer drawn
    from the readable half is still an answer, but a reader has to know which half
    it came from.
    """
    logs = tuple(Path(entry) for entry in (cycles_logs or ()))
    if not logs:
        return "", "no cycle-record directory was named"
    found: dict[str, Path] = {}
    unreadable: list[str] = []
    for log in logs:
        try:
            names = [entry.name for entry in log.iterdir()]
        except OSError as exc:
            unreadable.append(f"{log} ({exc.strerror or exc})")
            continue
        for name in names:
            match = _CYCLE_RECORD.fullmatch(name)
            if match:
                found.setdefault("cyc" + match.group(1), log)
    named = ", ".join(str(log) for log in logs)
    if not found:
        if len(unreadable) == len(logs):
            return "", f"{'; '.join(unreadable)} could not be read"
        detail = f" ({'; '.join(unreadable)} could not be read)" if unreadable else ""
        return "", f"no cycle record before {cycle} in {named}{detail}"
    earlier = sorted(entry for entry in found if entry < cycle)
    where = (
        str(found[earlier[-1]])
        if earlier
        else f"no cycle record before {cycle} in {named}"
    )
    if unreadable:
        where += f" ({'; '.join(unreadable)} could not be read)"
    return (earlier[-1], where) if earlier else ("", where)


@dataclass
class Window:
    """The span of cycles whose heads this cycle must neither vote on nor merge.

    `start` is the earliest push instant that counts as one's own; no `start` means
    the clause could not be applied at all. `unresolved` names what is missing, so a
    window shrunk to this cycle alone can say so rather than reading as the full one.
    """

    start: datetime | None = None
    source: str = ""
    unresolved: str = ""

    @property
    def applied(self) -> bool:
        return self.start is not None

    def window_start_text(self) -> str:
        """The window's start as one readable instant (empty when unapplied)."""
        return self.start.isoformat(timespec="seconds") if self.start else ""


def abstain_window(cycle: str, previous: str, where: str) -> Window:
    """The window, from the previous cycle's id — or a narrower one that says so.

    The previous cycle's start is preferred because it is the wider window: a head
    pushed by the cycle immediately before this one is treated as one's own
    (precedent `cyc20260917-125823`). Without it the scan shrinks to this cycle's own
    start, which is a strictly weaker reading and must not be printed as the stronger
    one.
    """
    own = cycle_start(cycle)
    start = cycle_start(previous)
    if start is not None:
        return Window(
            start=start,
            source=f"previous cycle {previous}" + (f" ({where})" if where else ""),
        )
    if own is not None:
        return Window(
            start=own,
            source=f"this cycle only (started {own.isoformat(timespec='seconds')})",
            unresolved=where or "the previous cycle was not determined",
        )
    return Window(start=None, source="", unresolved=where or f"{cycle!r} is not a cycle id")


def _sibling(name: str, module_name: str):
    """Load a sibling script by file, the way the rest of this family does.

    The scripts in this directory are not importable modules (hyphenated names, no
    package), so the file is loaded by path and registered under a plain name.
    """
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / name)
    if spec is None or spec.loader is None:  # pragma: no cover - the file is in this repo
        raise RuntimeError(f"could not load {name}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: these modules declare dataclasses, and dataclasses
    # resolves annotations through sys.modules[cls.__module__] at class-creation
    # time. A module that is not registered there raises AttributeError from inside
    # dataclasses itself - an error that names neither the caller nor the cause.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_counted: object | None = None
_freshness: object | None = None


def vote_counter():
    """The sibling that owns "is this vote still about this head?"."""
    global _counted
    if _counted is None:
        _counted = _sibling("check-vote-count.py", "review_queue_vote_count")
    return _counted


def freshness():
    """The sibling that owns "is the green CI about the tree that would land?"."""
    global _freshness
    if _freshness is None:
        _freshness = _sibling("check-merge-freshness.py", "review_queue_freshness")
    return _freshness


def votes_needed() -> int:
    """The gate's threshold, read from the tool that enforces it.

    Not written here: a second copy of the number is a second answer to "how many
    votes does this PR still need", and the one that is read from a constant drifts
    silently when the constant moves.
    """
    return int(vote_counter().DEFAULT_MIN_VOTES)


def _gh_json(args: list[str]) -> object:
    """Run `gh` and parse JSON, failing loud rather than guessing.

    `args` excludes the program name, which is prepended here so no call site can
    omit it — a call site that passed bare gh arguments once ran the POSIX `pr`
    utility instead, whose error names neither gh nor the mistake.
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
            f"gh failed (rc={proc.returncode}): gh {' '.join(args)}: {proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def open_prs(repo: str = REPO) -> list[int]:
    """Every open PR number, in the order GitHub lists them (newest first).

    Raises rather than returning `[]` when the listing fails: an empty queue and an
    unreadable one are different facts, and the empty one reads as "nothing to
    review", which is the answer a cycle would act on.
    """
    raw = _gh_json(
        [
            "pr",
            "list",
            "-R",
            repo,
            "--limit",
            "100",
            "--state",
            "open",
            "--json",
            "number",
        ]
    )
    assert isinstance(raw, list)
    return [int(item["number"]) for item in raw]


# ── the reading ──────────────────────────────────────────────────────────────

@dataclass
class Reading:
    """One PR's evidence, from the tools that own each half.

    The optional fields are the ones a reader must not be able to mistake for a
    value: `votes=None` is "not read", never 0; `behind_by=None` is "ancestry not
    read", never "contains master"; `stale_kind=""` with `stale_read=False` is "not
    known", never "fresh". `unread` carries why, and is never empty when something
    could not be read.
    """

    pr: int
    head: str
    title: str = ""
    votes: int | None = None
    needed: int = 3
    mergeable: str = ""
    merge_state: str = ""
    block_reason: str = ""
    veto_at_head: bool = False
    voted_here: bool = False
    #: When the head was pushed, as the counter read it, and whether that reading is
    #: exact (an earliest CI run) or the commit date (which can precede the push).
    #: Carried because the abstention clause is a comparison against this instant.
    head_pushed_at: str = ""
    head_pushed_exact: bool = True
    #: The window the clause was applied over, and what it rests on. `window_start`
    #: empty means the clause could not be applied at all — never "no window needed".
    window_start: str = ""
    window_source: str = ""
    stale_read: bool = False
    stale: bool = False
    stale_kind: str = ""
    stale_reason: str = ""
    behind_by: int | None = None
    unread: str = ""

    @property
    def conflict(self) -> bool:
        """Git cannot merge the text — the one blocker a reader can act on directly."""
        return self.mergeable == "CONFLICTING"

    @property
    def blocked(self) -> bool:
        return bool(self.block_reason)


@dataclass
class Action:
    """What to do about one PR, and the command that does it."""

    kind: str
    why: str
    command: str = ""
    extra: list[str] = field(default_factory=list)


def _note(*parts: str) -> str:
    """Join the reasons several reads failed, on one line, bounded.

    One line because the row is one line per fact; bounded because an exception
    string can carry a whole `gh` stderr and the queue has to stay readable. The
    first 200 characters name the failure, and the full text is one call away.
    """
    return "; ".join(part for part in parts if part)[:400].replace("\n", " ")


def read_pr(pr: int, repo: str = REPO, cycle: str | None = None,
            needed: int = 3, mergeability_wait: float = 0.0,
            window: Window | None = None,
            cycles_log: str | None = None) -> Reading:
    """Assemble one PR's row from the counter and the freshness tool.

    Each half degrades on its own — an unreadable ancestry does not discard a
    readable count — but neither degrades *to a value*. What could not be read is
    left `None` / `False` and named in `unread`, so a caller can tell "the counter
    says zero" from "the counter did not answer".

    The vote half is first because everything else is a decision about its number,
    and because a failure there means no row is worth printing. `mergeability_wait`
    is passed through to the counter, which is the tool that owns the refusal: this
    one neither softens it nor invents a verdict if the budget runs out. `cycles_log`
    goes through for the same reason the flag exists: the counter reads the abstention
    window out of it, so a corpus named here and not there would have this row's window
    read from one collection of cycles and its *count* judged against another.
    """
    out = Reading(pr=pr, head="", needed=needed)

    try:
        verdict = vote_counter().check_pr(
            pr, needed, mergeability_wait=mergeability_wait, cycles_log=cycles_log
        )
    except Exception as exc:  # noqa: BLE001 - the point is to report, not to crash
        out.unread = _note(f"vote count unreadable: {type(exc).__name__}: {exc}")
        return out

    out.head = str(verdict.head_sha)
    out.title = str(verdict.title)
    out.votes = int(verdict.valid_count)
    out.mergeable = str(verdict.mergeable)
    out.merge_state = str(verdict.merge_state)
    out.head_pushed_at = str(verdict.push_time)
    out.head_pushed_exact = bool(verdict.push_time_exact)
    if window is not None:
        out.window_start = window.window_start_text()
        out.window_source = window.source
    # Only a veto *at this head* makes a vote wrong: an invalid one (submitted
    # before the head push, or carrying no cycle id) is already excluded from the
    # count, so treating it as a standing objection would stall a PR that has none.
    out.veto_at_head = any(
        vote.kind == "veto" and vote.valid for vote in verdict.votes
    )
    if cycle:
        out.voted_here = any(vote.cycle == cycle for vote in verdict.votes)
    if verdict.blocked:
        out.block_reason = str(verdict.block_reason)

    try:
        fresh = freshness().check_pr(pr)
    except Exception as exc:  # noqa: BLE001
        out.unread = _note(f"ancestry unreadable: {type(exc).__name__}: {exc}")
        return out

    out.stale_read = True
    out.stale = bool(fresh.stale)
    out.stale_kind = str(fresh.stale_kind)
    out.stale_reason = str(fresh.reason)
    out.behind_by = int(fresh.behind_by)
    return out


# ── the decision ─────────────────────────────────────────────────────────────

def next_action(reading: Reading, cycle: str | None = None, repo: str = REPO,
                window: Window | None = None) -> Action:
    """The next action for one PR, in priority order.

    The order is the whole value of the tool: each PR gets the one thing a cycle
    should do next, and the branches are ordered so that the *first* applicable one
    is also the one that makes the rest moot. Each step is a rule from the
    docstring:

    * an unreadable count is first, because every later branch is a decision about a
      number this one does not have;
    * a standing veto is "fix push", not "vote" — and it is checked before the
      count, because a veto has already reset the run: a `0/3` caused by a ❌ looks
      exactly like a `0/3` that was never reviewed, and only one of them is a PR a
      cycle may vote on;
    * a text conflict is next: more review does not fix it, and resolving it
      replaces the head and voids whatever votes exist;
    * then the three CI states the freshness tool distinguishes — red, absent,
      unfinished. None of them is votable, and their remedies differ, which is why
      they are not collapsed into "not fresh": a red run is read, an absent one is
      re-triggered, and an unfinished one is **parked** — deferred to a later cycle
      rather than waited on, because a window spent blocking on a run this cycle
      cannot vote on is a window not spent on a row it could move (host rant
      2026-09-24T14:46:10);
    * then a merge state that withholds the merge (draft, blocked, behind) — again
      not a review problem, and not curable by a vote;
    * **then the abstention** (issue #1408): a head pushed by this cycle or by the one
      immediately before it is not one this cycle may vote on or merge. It sits
      directly above the two branches that *spend* something, because every branch
      above it asks "what does this PR need?" — and a head one pushed may still need
      a fix push, a conflict resolved, or CI to finish, which is work for the pusher
      rather than a vote for anyone;
    * then the count decides: enough votes is "merge", otherwise "vote" — and a head
      that no longer contains master gets the landing-tree form of that vote, the
      reading whose absence stalled a cycle.
    """
    pr = reading.pr
    if reading.votes is None:
        # The command carries the counter's own wait flag because this branch is
        # where a not-yet-computed mergeability lands: the same question, asked with
        # a budget, is the way it gets answered.
        return Action(
            kind="read-first",
            why=reading.unread or "the vote count could not be read",
            command=f"{RUNNER} scripts/check-vote-count.py {pr} --mergeability-wait 60",
        )
    if reading.veto_at_head:
        return Action(
            kind="fix-push",
            why="a veto stands at this head: it needs a fix push, not another review, "
                 "and no vote counted here can outlive the answer it already has",
            command=f"{RUNNER} scripts/check-vote-count.py {pr}",
        )
    if reading.conflict:
        return Action(
            kind="resolve-conflict",
            why=reading.block_reason + " - resolving it moves the head, so every vote "
                 "standing here is spent on the fix (a fork PR is pushed to the fork, "
                 "never to origin)",
            command=f"gh pr checkout {pr} -R {repo} && git fetch origin master "
                    "&& git merge FETCH_HEAD",
            extra=[f"{RUNNER} scripts/classify-conflict.py --all"],
        )
    if reading.stale_read and reading.stale_kind == "failing":
        return Action(
            kind="ci-red",
            why=reading.stale_reason
            + " - not votable: a vote at this head would be a vote about a tree whose "
              "CI ran red, and a re-run only helps if the failure was a flake",
            command=f"gh pr checks {pr} -R {repo}",
        )
    if reading.stale_read and reading.stale_kind == "no_run":
        return Action(
            kind="retrigger-ci",
            why=reading.stale_reason
            + " - re-triggering fires a run on the same head, which keeps the votes a "
              "refresh would spend",
            command=f"{RUNNER} scripts/re-trigger-ci.sh <branch-of-{pr}>",
        )
    if reading.stale_read and reading.stale_kind == "running":
        # The verb is the instruction: "wait" told the reader to block until the run
        # concluded, which spends the whole window on a PR this cycle cannot vote on
        # anyway. "park" says the row is skipped this round and read again next one.
        return Action(
            kind="park",
            why=reading.stale_reason + " - parked for this cycle: a run that has not "
                                       "concluded is not votable, so blocking on it "
                                       "spends the window on a row this cycle cannot "
                                       "move. Read this PR again next cycle; neither a "
                                       "refresh nor a re-trigger answers a run that has "
                                       "not concluded",
            command=f"gh pr checks {pr} -R {repo}",
        )
    if reading.blocked:
        return Action(
            kind="unblock",
            why=reading.block_reason
            + " - a state of the branch, not of the review: no vote cast here changes "
              "it, and the branch has to remove it",
            command=f"gh pr view {pr} -R {repo} --json mergeable,mergeStateStatus",
        )
    if window is not None and window.applied:
        pushed = instant(reading.head_pushed_at)
        if pushed is not None and pushed >= window.start:
            return Action(
                kind="abstain",
                why=f"head pushed {reading.head_pushed_at}, inside the window this cycle "
                    f"treats as its own ({window.source}) - a cycle neither votes on nor "
                    "merges a head it pushed, and the window immediately before this one "
                    "counts as one's own as well, because every cycle on a host is the same "
                    "instance running again; the next vote here (and the merge) has to come "
                    "from a later cycle",
                command=f"{RUNNER} scripts/check-vote-count.py {pr}",
            )
    if reading.votes >= reading.needed:
        if reading.stale:
            return Action(
                kind="measure-then-merge",
                why=f"{reading.votes}/{reading.needed} votes, but "
                    + reading.stale_reason
                    + " - measure the landing tree before merging; the head does not "
                      "move, so the votes that carried it here stay valid",
                command=f"{RUNNER} scripts/check-merge-plan-suite.py {pr}",
                extra=[f"{RUNNER} scripts/check-merge-tree-health.py"],
            )
        return Action(
            kind="merge",
            why=f"{reading.votes}/{reading.needed} valid votes, none predating the head "
                "push, and the head's green run is about the tree that would land",
            command=f"gh pr merge {pr} -R {repo} --squash",
        )
    if reading.voted_here:
        return Action(
            kind="already-voted",
            why=f"this cycle already has a vote at this head ({reading.votes}"
                f"/{reading.needed}) - counting is per cycle, so the next vote here has "
                "to come from another cycle",
            command=f"{RUNNER} scripts/check-vote-count.py {pr}",
        )
    vote_cmd = (
        f"{RUNNER} scripts/cast-vote.py {pr}"
        f"{f' --cycle {cycle}' if cycle else ''} --body-file <body>"
    )
    if reading.stale:
        return Action(
            kind="measure-then-vote",
            why=f"{reading.votes}/{reading.needed} votes, and " + reading.stale_reason
                + " - measure the tree this merge would land and vote on that reading; "
                  "the head does not move, so the standing votes survive - and read the "
                  "landing diff before voting, because `diff(master, head)` on this head "
                  "shows the base's own later commits as reversals this PR does not make",
            command=f"{RUNNER} scripts/check-merge-plan-suite.py {pr}",
            extra=[
                f"{RUNNER} scripts/check-merge-landing-diff.py {pr}",
                vote_cmd,
            ],
        )
    return Action(
        kind="vote",
        why=f"{reading.votes}/{reading.needed} votes, a fresh head, no unanswered veto",
        command=vote_cmd,
    )


def rows(readings: list[Reading], cycle: str | None, repo: str,
         window: Window | None = None) -> list[tuple[Reading, Action]]:
    """(reading, action) per PR, in the order the PRs were given."""
    return [(reading, next_action(reading, cycle, repo, window)) for reading in readings]


def window_note(window: Window | None) -> str:
    """What the report owes the reader about the own-window clause, before its rows.

    Two states are strictly weaker readings than "the clause was applied", and each of
    them weakens a row that otherwise reads `vote`:

    * **no `--cycle`** — the clause was not applied at all. The rows answer the count
      question and nothing else, so a `vote` here may be a head this very cycle pushed.
      Nothing said so: the prose was byte-identical in shape to a windowed run, and the
      remedy the row prints (`cast-vote.py <PR> ...`, which omits `--cycle` because the
      tool was not given one) does not carry the correction either. Measured 2026-09-26
      (`cyc20260926-120320`): a first run without the flags answered `vote` for `#1636`,
      the head the cycle immediately before it had pushed, and only re-running with
      `--cycle` turned that row into `abstain`.

      The **cost, measured rather than carried over** — the vote itself is not spent:
      `cast-vote.py` reads the cycle id out of the body, resolves the previous cycle
      itself, and refuses with `own-head-window` (rc 2), which it did on that very head
      in this cycle's probe. What the unflagged run costs is a **wrong reading**: the
      first tool a cycle runs states `vote` where its own windowed answer is `abstain`,
      and a reader who acts on the row — writing the body, planning the merge, reading
      the state as "this head is votable" — has acted on a verdict the tool does not
      hold. The backstop is downstream, one command later, and it arrives as a refusal
      rather than as the `abstain` the report should have printed.

      The docstring above has promised "it reports the first question only, and says so"
      since the clause was written, and this line is that "so" — it had no carrier at
      all before.
    * **a window narrowed to this cycle alone** — the previous cycle could not be read,
      so a head *it* pushed is not reported as one's own. Already said, since
      `test_an_unresolvable_previous_cycle_narrows_the_window_and_says_so`; what moves
      is *where*, and the two notes move together because they are the same debt.

    The placement is the point, and it is this family's convention rather than a
    preference: a caveat that follows the row it weakens is read after the reader has
    already acted on it. Printed first, it also survives `| head`, which the tail
    placement did not — a queue of eight PRs is ~40 lines, and the note was line 39.

    Returns the note (empty when the clause was applied in full). Prose only: under
    `--json` the same fact is the documented `vote_window_start: null` /
    `vote_window_source: ""` pair, and a line ahead of the document would break it.
    """
    if window is None:
        return (
            "note: no --cycle was given, so the own-window clause was NOT applied - a "
            'row reading "vote" may be a head this cycle pushed, which the clause turns '
            "into `abstain`. Pass --cycle <this cycle's id> to have the clause applied."
        )
    if not window.unresolved:
        return ""
    where = (
        f"only pushes at or after {window.window_start_text()}"
        if window.applied
        else "and this cycle's own id could not be read, so the clause was not "
             "applied at all"
    )
    return (
        f"note: the abstention window could not be widened to the cycle before this "
        f"one ({window.unresolved}) - {where} were checked. Pass --prev-cycle or "
        "--cycles-log DIR to have the full window applied."
    )


def render(reading: Reading, action: Action) -> str:
    """One PR's block: what is true, then what to do, then the exact command."""
    head = reading.head[:8] if reading.head else "????????"
    votes = f"{reading.votes}/{reading.needed}" if reading.votes is not None else "?"
    marks = []
    if reading.stale_read and reading.stale:
        marks.append(f"stale:{reading.stale_kind}")
    if reading.veto_at_head:
        marks.append("veto")
    if reading.voted_here:
        marks.append("voted-here")
    if reading.head_pushed_at:
        # Printed on every row because it is the other half of "may this cycle vote
        # here": a reader can apply the abstention clause by eye from this datum even
        # when the tool could not resolve the window it belongs to.
        marks.append(f"pushed {reading.head_pushed_at}")
    suffix = f"  [{', '.join(marks)}]" if marks else ""
    lines = [f"#{reading.pr} {votes} votes  head {head}  {action.kind}{suffix}"]
    lines.append(f"    {action.why}")
    if action.command:
        lines.append(f"    $ {action.command}")
    for extra in action.extra:
        lines.append(f"    $ {extra}")
    return "\n".join(lines)


def local_tree() -> tuple[str, str, str]:
    """(this checkout, the branch it is on, its HEAD) — read, or said unreadable.

    The report is derived from *this* clone: the vote counter's ancestry reading and the
    freshness reading both come out of it, and every command printed below is meant to be
    run here. So the clone and its branch are stated before any verdict — a reader who
    then opens a file with `read`/`grep` is reading *this branch's* content, which is
    master's only when the branch says so.

    Measured 2026-09-26 (`cyc20260926-110148`): a cycle began with the tree still on the
    previous cycle's PR branch, and a file read from that working tree showed the
    *already-fixed* text of an unmerged PR while master still carried the defect the
    cycle was about to look for. Nothing in this report said which tree had answered, so
    the only thing that caught it was the content looking wrong — which is luck, not a
    reading. Naming the branch is what makes it a reading.
    """

    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=SCRIPTS_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    branch = git("symbolic-ref", "--short", "-q", "HEAD")
    if not branch:
        # `symbolic-ref` exits non-zero for a detached HEAD, which is a state and not a
        # failure — the same distinction the readings below keep between "not read" and
        # a value.
        branch = "(detached HEAD)"
    head = git("rev-parse", "HEAD") or "????????"
    return str(SCRIPTS_DIR.parent), branch, head


def _as_json(readings: list[tuple[Reading, Action]]) -> str:
    # The clone and its branch ride as *fields* on each reading, the way
    # `check-merge-landed.py` states its tree in `--json`: the document's shape is a
    # list, and a prose line ahead of it would be a second kind of line in a stream a
    # machine consumer parses.
    root, branch, _head = local_tree()
    return json.dumps(
        [
            {
                "tree": root,
                "branch": branch,
                "pr": reading.pr,
                "head": reading.head,
                "title": reading.title,
                "votes": reading.votes,
                "needed": reading.needed,
                "mergeable": reading.mergeable,
                "merge_state": reading.merge_state,
                "block_reason": reading.block_reason,
                "veto_at_head": reading.veto_at_head,
                "voted_by_this_cycle": reading.voted_here,
                "head_pushed_at": reading.head_pushed_at,
                "head_pushed_exact": reading.head_pushed_exact,
                "vote_window_start": reading.window_start or None,
                "vote_window_source": reading.window_source,
                "stale": reading.stale if reading.stale_read else None,
                "stale_kind": reading.stale_kind,
                "behind_by": reading.behind_by,
                "unread": reading.unread,
                "action": action.kind,
                "why": action.why,
                "command": action.command,
            }
            for reading, action in readings
        ],
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="review-queue.py",
        description="For every open PR: its counted votes, and the next action for a cycle.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "prs",
        nargs="*",
        type=int,
        help="only these PRs; default is every open PR",
    )
    parser.add_argument("--repo", default=REPO, help="GitHub owner/repo")
    parser.add_argument(
        "--cycle",
        default=None,
        help="this cycle's id (cycYYYYMMDD-HHMMSS); given, the queue also answers "
             "whether this cycle may still vote at each head",
    )
    parser.add_argument(
        "--prev-cycle",
        default=None,
        help="the cycle immediately before this one (cycYYYYMMDD-HHMMSS); a cycle id "
             "is its start time, so this is the far end of the abstention window - a "
             "head pushed by it counts as this cycle's own",
    )
    parser.add_argument(
        "--cycles-log",
        default=None,
        help="directory of `cycle-<id>.md` records, used to find the previous cycle "
             "when --prev-cycle is not given (default: $EMRG_CYCLES_LOG, else both "
             f"{os.pathsep}-joined directories the template may name: "
             f"{os.pathsep.join(str(d) for d in DEFAULT_CYCLES_LOGS)})",
    )
    parser.add_argument(
        "--min-votes",
        type=int,
        default=None,
        help="votes the gate requires (default: the counter's own DEFAULT_MIN_VOTES)",
    )
    parser.add_argument(
        "--mergeability-wait",
        type=float,
        default=30.0,
        help="seconds to keep re-asking GitHub for a lazily-computed mergeability "
             "before the counter refuses (only spent when it answers UNKNOWN)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the readings as JSON instead of prose"
    )
    args = parser.parse_args(argv)

    needed = args.min_votes if args.min_votes is not None else votes_needed()

    window: Window | None = None
    if args.cycle:
        if args.prev_cycle:
            previous, where = args.prev_cycle, "named by --prev-cycle"
        else:
            previous, where = previous_cycle(
                args.cycle, resolve_cycle_logs(args.cycles_log)
            )
        window = abstain_window(args.cycle, previous, where)

    try:
        queue = args.prs if args.prs else open_prs(args.repo)
    except Exception as exc:  # noqa: BLE001
        # Not an empty queue: `gh` failing means the question was not answered, and
        # the empty answer is the one a cycle would act on.
        print(f"error: could not list open PRs in {args.repo}: {exc}", file=sys.stderr)
        return 2

    readings = rows(
        [
            read_pr(pr, args.repo, args.cycle, needed, args.mergeability_wait, window,
                    args.cycles_log)
            for pr in queue
        ],
        args.cycle,
        args.repo,
        window,
    )

    unread = [reading.pr for reading, _ in readings if reading.votes is None]

    root, branch, head = local_tree()
    if not args.json:
        # The family's convention — a guard that reads a working tree names it before it
        # gives a verdict — and it applies here for the reason `local_tree` records: the
        # readings are this clone's, and so is any file the reader opens next.
        print(f"tree: {root} on {branch} ({head[:8]})")
        here = [reading.pr for reading, _ in readings if reading.head == head]
        if here:
            print(
                f"note: this working tree is at the head of "
                f"{', '.join(f'#{pr}' for pr in here)} - an open PR, so a file read from "
                "here is that PR's content, not master's"
            )

    if args.json:
        print(_as_json(readings))
    elif not queue:
        print(f"no open PRs in {args.repo} - nothing to review")
    else:
        # Before the first row, never after it: this says how strong the reading below
        # is, and a reader who has already copied a row's command has already acted.
        note = window_note(window)
        if note:
            print(note)
            print()
        for reading, action in readings:
            print(render(reading, action))
            print()
        tally: dict[str, int] = {}
        for _reading, action in readings:
            tally[action.kind] = tally.get(action.kind, 0) + 1
        summary = ", ".join(f"{kind} {count}" for kind, count in sorted(tally.items()))
        print(f"{len(readings)} PR(s): {summary}")
        if unread:
            print(
                f"unmeasurable: {', '.join(f'#{pr}' for pr in unread)} - "
                "read them before acting"
            )

    # The same verdict however the reading is rendered: a row nobody could read is
    # not a row that is fine.
    return 2 if unread else 0


if __name__ == "__main__":
    raise SystemExit(main())
