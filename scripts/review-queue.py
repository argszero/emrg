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
  — the ancestry, and the six ways a verdict can fail to be current.

So the number printed here is the counter's number and the staleness here is the
freshness tool's *kind*, not a local re-derivation of either. That matters more than
it looks: `behind_by > 0` is a plausible stand-in for "stale", and it is not the same
question — the freshness tool asks whether master's tip is an *ancestor* of the head
(a graph property), and it names four distinct states where a count comparison would
have produced one word. A second implementation of the vote rule would be a second
answer to "may we merge this", which is the number every decision below turns on.

Three things the output never does
----------------------------------
* **A count it could not read is not a zero.** `0/3 votes` is the line that says
  "vote freely"; printing it without having read it spends votes in the direction
  that cannot be undone. An unreadable count is reported as `?` with the reason, and
  the action becomes "read it first".
* **A CI half it could not read is not a branch state.** Each PR is two reads - the
  count and the sibling gate's ancestry/run half - and the row's later branches are
  decisions about the second one. When that half raises, the row is `read-first` with
  the reason, not the branch-state row that falls next: measured 2026-10-07
  (`cyc20261007-203559`), where a gh timeout in the compare call printed `unblock` -
  a remedy for a state of the branch - for a head that was merely waiting for CI. The
  reason is carried on the row and by the `--json` half, and the exit code counts it.
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

The rant half of the report prints this task's rows by default and **counts** the rest
----------------------------------------------------------------------------------------
`open_rant_rows` returns every open rant — the reading stays whole, and the JSON below keeps
every row too, each labelled `rendered` — but the prose renders only the rows this repo can
act on, and says how many it withheld. The rule is `rendered_here`: this task's own rants
(by either spelling), plus the ones naming **no** project. Withheld are the rows naming
another project, which is the whole of the bulk and none of the work: measured 2026-10-04 on
this host, 40 open rants of which 39 were `silicon-science-cs`, ~8.7KB of the report's
~9.7KB, printed every cycle for a reader the template has already told those rows are not
its own. `--all-rants` prints every row.

A row naming no project is deliberately still printed, though no issue here can declare it:
undeclared is not another project's work, and the rows this section exists for were added
after a cycle read "nothing to review" while pending rants without issues sat in the ledger
(measured 2026-09-29) — so hiding an undeclared one would restore that defect for the row
most likely to be this task's.

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
it), and the window that decides it is that cycle's **own run**: from its start to the
start of the cycle that follows it. The host ruled the older, wider reading away on
2026-10-16T22:18:15 ("I think the requirement 'nor one pushed by the cycle immediately
before it' should not exist. That the cycle that pushed it cannot vote is already
enough" — translated from the Chinese original), so a head the immediately preceding
cycle pushed is no longer read as this cycle's own work.

*Who pushed a head* is not a fact GitHub records, so the clause has to be read off
the clock, and a cycle id **is** its start time in the host's local zone
(`cyc20260917-221117` began at 22:11:17 local). The near bound therefore needs no
corpus at all: it is this cycle's own id. The far one does — **the cycle that follows
it**, read from the cycle records in the directories the memory system writes them to
(`--cycles-log`, default `DEFAULT_CYCLES_LOGS` — the checkout's project memory root,
plus one memory directory per session under it). A head pushed inside `[this cycle's
start, the next cycle's start)` is reported `abstain` instead of `vote` or `merge`.

A cycle id that names no instant leaves the window unapplied, and the queue prints
what it could not determine rather than passing it. An unresolved window is never
reported as a pass — the whole point of the clause is that the wrong direction is the
one that spends a vote nobody can recount.

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
import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

REPO = "argszero/emrg"


def could_declare_here(project: str, repo: str = REPO) -> bool:
    """Whether an issue in `repo` could carry the handle that pairs a rant with it.

    A rant's declaring issue is filed by its own project's task, in that project's
    repository, so the pairing is the project name (`emrg`) or its `owner/repo` form
    (`argszero/emrg`) — the two spellings the template matches a rant to a task by. A rant
    naming no project belongs to nobody, and one naming another project is another
    repository's issue list: this repo can no more declare it than it can read it.

    This is the one place the pairing is code rather than prose, and it exists to bound a
    *reading*: see `open_rant_rows`, where the difference is 48.8s of `gh` against 5.8s.
    """
    return bool(project) and project in (repo, repo.rsplit("/", 1)[-1])


def rendered_here(project: str, repo: str = REPO) -> bool:
    """Whether this rant gets a row in the report **by default**.

    This task's own rants, and the ones naming **no** project at all. The withheld case is
    the one the prompt decides for us in as many words — a rant naming another project is
    not this instance's to act on — and on this host that is also the whole of the bulk
    (measured 2026-10-04: 40 open rants, 39 of them `silicon-science-cs`, ~8.7KB of the
    report's ~9.7KB, paid by every cycle).

    A row naming **no** project is deliberately *not* withheld, even though
    `could_declare_here` is false for it: that predicate is about whether an issue **in this
    repo** could carry the pairing, and a project-less row is not another
    project's work — it is undeclared, which is a thing the cycle has to look at. The rows
    this table prints exist because a cycle reading the queue *alone* concluded "nothing to
    review" while three pending rants had no issue (measured 2026-09-29), so hiding an
    undeclared rant would restore that defect for the rows most likely to be this task's.
    """
    return could_declare_here(project, repo) or not project

#: How the family's tools are invoked (`Agent.md`, "Test Commands"). Printed
#: commands carry the runner the docstrings and the docs prescribe — a bare
#: `scripts/x.py` is not executable on this host, so printing one would hand the
#: reader a command that fails.
#:
#: The rule is **by file type**, and the shell half was got wrong here: a `.py`
#: tool takes this runner, while a `.sh` tool takes `bash` and must never be given
#: to python. Measured 2026-10-04: `uv run --no-sync python3 scripts/re-trigger-ci.sh`
#: exits 1 with `SyntaxError: invalid syntax` on line 11 (`set -euo pipefail`) — the
#: re-trigger row used to print exactly that, so the one row whose remedy is
#: "re-trigger CI on the same head" handed over a command that could not run.
#:
#: Carrying the right runner is not the whole rule either: `bash` is a **host**
#: dependency, and the row is printed to whichever host is running the cycle. Measured
#: 2026-10-05 by a reviewer on a Windows host (cycle `cyc20261005-054639`):
#: `Get-Command bash` -> CommandNotFoundException, a git-bundled `bash.exe` present but
#: not on PATH, so `bash scripts/re-trigger-ci.sh` could not run there at all - the same
#: defect one rung on. `gh` is not optional in this family (every tool here reads GitHub
#: through it) and `test.yml` declares `workflow_dispatch`, so the re-trigger row now
#: **leads** with `gh workflow run test.yml --ref <branch>`, which is the single command
#: `re-trigger-ci.sh` itself runs, and keeps the script as the alternative beneath it.
RUNNER = "uv run --no-sync python3"

SCRIPTS_DIR = Path(__file__).resolve().parent

def session_memory_roots(checkout: Path) -> tuple[Path, ...]:
    """The memory directories of the sessions run inside `checkout`, in a stable order.

    A cycle record is an ordinary memory entry, so it lands where the memory
    instructions put one of its kind: the checkout's project memory root or the
    memory directory of the session that wrote it. This is the second of those
    levels, and it is **globbed off the filesystem rather than constructed** — a
    memory store mkdirs the directory it is handed, and a reading with a side effect
    is how a search comes to create the corpus it searches. `check-memory-index.py`
    globs `*/memory/MEMORY.md` beside the project index for the same reason.

    Empty when the tree carries no session memory, which is every fresh clone:
    `.emrg` is gitignored, so on a runner the default is the project root alone.
    """
    sessions = checkout / ".emrg" / "sessions"
    return tuple(sorted(path for path in sessions.glob("*/memory") if path.is_dir()))


#: Where the evolution task writes its cycle records — one `cycle-<date>-<time>.md`
#: per cycle, the cycle's id without its `cyc` prefix in the filename, which is why
#: the id is rebuilt rather than read off.
#:
#: **Two roots, because a tree's memory is not one directory.** `SCRIPTS_DIR` is
#: `<source_dir>/scripts`, so `SCRIPTS_DIR.parent` is the checkout. A record is a
#: memory entry like any other, and the memory instructions name two levels for it —
#: the checkout's project root (`.emrg/memory/`, the cross-session scope) and the
#: session that wrote it (`.emrg/sessions/*/memory/`, the session scope) — so which
#: level a record lands at is the writer's scope choice, not a path the instructions
#: fix. So both are live, and this search has to
#: read both: the project root holds the older records, and the session directories
#: hold the ones written since a cycle put its record under the session it belongs to.
#:
#: **The direction of the error is why this is worth a paragraph.** The newest record
#: decides the far bound — the cycle that follows this one — so a root that has stopped
#: growing *freezes* that bound at the last cycle that wrote there, and a frozen far
#: bound is an *open* one: the span then has no upper end, so a head pushed by a cycle
#: that followed is reported `abstain` when it is that later cycle's own work. Nothing is
#: printed as a fault — the tool names a window, names the cycle it resolved and exits
#: 0 — so only the row's cycle is wrong. Measured 2026-10-12: the project root's newest
#: record was a day older than the newest record the session directories held, and the
#: bound was resolved from the stale one.
#:
#: This is the shape this tool carried while the prompt's record path moved once
#: before (D9, PR #1555) and the corpus was split over two roots — it searched both
#: and took the newest across the union then, for the same reason.
#:
#: A guard ties this set to the writers
#: (`tests/test_review_queue.py::test_the_records_land_where_the_queue_reads_them`):
#: it measures the project root the loader derives and a session root this search
#: visits, so reader and writers cannot drift apart silently again — read
#: against the writer, not the rendered template, which no longer names a record's
#: path (the test's docstring carries that history).
#:
#: Overridable by `--cycles-log` / `EMRG_CYCLES_LOG` (one directory, or several joined by
#: `os.pathsep`); a directory that is not there is reported as unreadable, never guessed at.
DEFAULT_CYCLES_LOGS: tuple[Path, ...] = (
    SCRIPTS_DIR.parent / ".emrg" / "memory",          # the checkout's project memory root
    *session_memory_roots(SCRIPTS_DIR.parent),        # plus one per session that wrote
)


def resolve_cycle_logs(override: str | None = None) -> tuple[Path, ...]:
    """The cycle-record directories to search: the override, else the default.

    `--cycles-log` / `EMRG_CYCLES_LOG` accept one directory or several joined by
    `os.pathsep`, so a caller can name a corpus of its own without the default
    changing meaning — and the default is itself several directories, because the
    writers do not all use one root.
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


def _cycle_record_ids(cycles_logs) -> tuple[dict[str, Path], list[str]]:
    """Every cycle id the record corpus holds, and the roots that were **not listed**.

    The scan `following_cycle` reads for the window's far bound: the earliest id above this
    cycle's. Resolved from a second reading of the roots, the same head would read one way in
    the queue and another at the poster, which is the divergence this family has already paid
    for once (issue #2218).

    A root that cannot be listed is *named* rather than passed over: an answer drawn from
    the readable half is still an answer, but a reader has to know which half it came from.
    """
    found: dict[str, Path] = {}
    unreadable: list[str] = []
    for log in tuple(Path(entry) for entry in (cycles_logs or ())):
        try:
            names = [entry.name for entry in log.iterdir()]
        except OSError as exc:
            unreadable.append(f"{log} ({exc.strerror or exc})")
            continue
        for name in names:
            match = _CYCLE_RECORD.fullmatch(name)
            if match:
                found.setdefault("cyc" + match.group(1), log)
    return found, unreadable


def following_cycle(cycle: str, cycles_logs) -> tuple[str, str, str]:
    """`(the cycle that follows `cycle`, where it was found, why the corpus fell short)`.

    The **earliest** cycle record whose id sorts after this cycle's; ids are fixed width,
    so string order is time order. This is the window's far bound, and the clause needs it
    because a cycle's run ends where the next cycle begins: a head pushed at or after that
    instant was pushed by that later cycle, so it is not this one's own work however the
    clock reads (issue #2334).

    `("", reason, ...)` when there is none — the *ordinary* state here rather than an
    accident: the newest cycle is the one posting a vote and nothing follows it yet, so its
    span stays open at the far end. The third element is the roots that were **not listed**,
    and its direction is the safe one: a corpus nobody could
    read has not said there is no later cycle either, so the bound stays open, which leaves
    the window **wide** — a delay, never a vote credited.
    """
    found, unreadable = _cycle_record_ids(cycles_logs)
    named = ", ".join(str(log) for log in (cycles_logs or ()))
    shortfall = "; ".join(unreadable)
    later = sorted(entry for entry in found if entry > cycle)
    if later:
        where = str(found[later[0]])
        if unreadable:
            where += f" ({shortfall} could not be read)"
    elif unreadable and not found:
        # Nothing at all could be listed, so "no cycle record after X" is a claim the
        # corpus never supported: the reason is the read failure. The two are different
        # facts and only one of them is a gap in the record corpus.
        where = f"{shortfall} could not be read"
    else:
        where = (
            f"no cycle record after {cycle} in {named}"
            if named
            else "no cycle-record directory was named"
        )
        if unreadable:
            where += f" ({shortfall} could not be read)"
    return (later[0], where, shortfall) if later else ("", where, shortfall)


@dataclass
class Window:
    """The span of cycles whose heads this cycle must neither vote on nor merge.

    `start` is this cycle's own run start — a cycle id **is** its start instant — and
    `end` is the instant the **next** cycle began, or `None` while this cycle is the
    newest. The span is `[start, end)`: a head pushed before `start` belongs to somebody
    else, so the vote counts, and one pushed from `end` onward belongs to the cycle that
    followed (issue #2334).

    No `start` means the clause could not be applied at all — the caller's cycle id names
    no instant — and `unresolved` names that, so a window that was not applied says so
    rather than reading as an empty one.
    """

    start: datetime | None = None
    source: str = ""
    unresolved: str = ""
    #: The far bound of the span: the instant the **next** cycle started, or `None` while
    #: this cycle is the newest — its run has not ended, which is the state every cycle is
    #: in when it posts a vote. A head pushed at or after this instant was pushed by that
    #: later cycle, so it is not this cycle's own work however the clock reads (issue
    #: #2334).
    end: datetime | None = None

    @property
    def applied(self) -> bool:
        return self.start is not None

    def covers(self, pushed: datetime) -> bool:
        """Is this push inside the run of the cycle whose head-window this is?

        The clause is about a head **this** cycle pushed, and a cycle's run ends where the
        next cycle begins: the span is `[start, end)`, with `end` open while this cycle is
        the newest — nothing after it exists to take the run over, so every push since its
        start is its own. A push *before* `start` was pushed by somebody else, and that is
        the reading the host ruled for on 2026-10-16T22:18:15: "I think the requirement
        'nor one pushed by the cycle immediately before it' should not exist. That the
        cycle that pushed it cannot vote is already enough" (translated from the Chinese
        original). So the window is this cycle's own run, not the previous cycle's too.

        The far bound is needed because without one the span reaches *forward* without
        limit, and a head pushed by a later cycle was reported as this one's own work
        (issue #2334: `cyc20261016-181619` was voided on a head `cyc20261016-183420`
        pushed).

        One predicate, asked by all three carriers — the queue that prints the verdict,
        the counter that voids a posted vote, the poster that refuses to spend one — so
        the same head cannot read `vote` in one and be voided by another.
        """
        if self.start is None:
            return False
        return pushed >= self.start and (self.end is None or pushed < self.end)

    def window_start_text(self) -> str:
        """The window's start as one readable instant (empty when unapplied)."""
        return self.start.isoformat(timespec="seconds") if self.start else ""

    def window_end_text(self) -> str:
        """The window's far bound as one readable instant (empty when it has none).

        Empty in two states a reader must not confuse: a cycle whose span is still open
        because nothing followed it — the state every posting cycle is in — and a window
        that could not be applied at all. Both leave the span open above, which is the
        safe reading, so neither invents an instant.
        """
        return self.end.isoformat(timespec="seconds") if self.end else ""


def abstain_window(cycle: str, following: str = "") -> Window:
    """The window one cycle's own run defines: `[its start, the next cycle's start)`.

    A cycle id **is** its start instant, so the near bound needs no corpus: the clause is
    about a head *this* cycle pushed, and everything before its own start was pushed by
    somebody else. The host ruled the older, wider reading away on 2026-10-16T22:18:15
    ("I think the requirement 'nor one pushed by the cycle immediately before it' should
    not exist. That the cycle that pushed it cannot vote is already enough" — translated
    from the Chinese original) — a cycle abstained on any push from the *previous* cycle's
    start onward, which is "any head pushed while this cycle ran", not "the head this cycle
    pushed".

    `following` is the cycle *after* this one, and it closes the span at the far end: a
    head pushed from the next cycle's start onward was pushed by that cycle, not by this
    one, so the clause has nothing to say about it (issue #2334). It is `""` for the
    newest cycle — the state every posting cycle is in — and the span is then open above,
    which is right: nothing has taken the run over yet.

    A cycle id that names no instant is the one input this reading must not invent, so
    the window is left **unresolved** — no `start` at all, the state both tools already
    treat as "this vote cannot be counted" and "this vote must not be posted".
    """
    own = cycle_start(cycle)
    end = cycle_start(following)
    if own is None:
        return Window(
            start=None,
            source="",
            unresolved=f"{cycle!r} is not a cycle id",
            end=end,
        )
    return Window(
        start=own,
        end=end,
        source=f"this cycle's own run (started {own.isoformat(timespec='seconds')})",
    )


def own_window(cycle: str, cycles_log: str | None = None) -> Window:
    """The own-window — the single producer for a rule three tools act on.

    The near bound is this cycle's own start (`abstain_window`); the far one is the cycle
    that follows it, resolved from the record corpus because a later cycle's existence is
    a fact only the records hold. The two compose **here and nowhere else**, because a
    window is a rule and not an implementation detail: `review-queue.py` prints the
    verdict from it, `cast-vote.py` refuses to *post* on it, and `check-vote-count.py`
    voids a posted vote on it. Resolved from different corpora in different carriers, the
    same head reads `vote` in the queue and is refused at the poster — a vote lost to an
    arithmetic difference between two carriers rather than to the clause (issue #2218).

    :param cycles_log: the cycle-record corpus, `os.pathsep`-joined for several, or
        `None` for the default roots.
    :returns: the window to use.
    """
    roots = resolve_cycle_logs(cycles_log)
    # The far bound is read for every caller: it is a fact of the corpus, and a window
    # whose far end nobody looked for would report a later cycle's head as this one's own.
    following, _where, _shortfall = following_cycle(cycle, roots)
    return abstain_window(cycle, following=following)


def _sibling(name: str, module_name: str):
    """Load a sibling script by file, the way the rest of this family does.

    The scripts in this directory are not importable modules (hyphenated names, no
    package), so the file is loaded by path and registered under a plain name.
    """
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / name)
    # No `spec is None` guard: `spec_from_file_location` returns a spec and a
    # loader even for a path that does not exist (measured 2026-10-06), so that
    # branch could never fire. A sibling that is missing or does not compile
    # raises out of `exec_module`, and `_entry` reports that as `2`.
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
_issue_links: object | None = None


def issue_links():
    """The sibling that owns the rant↔issue handle (`issue #N` in the entry, R5).

    Also the fallback the older `Origin: rant <timestamp>` line resolves through, and the
    only reader of the ledger's file format — so both consumers ask one object.
    """
    global _issue_links
    if _issue_links is None:
        _issue_links = _sibling("check-issue-links.py", "review_queue_issue_links")
    return _issue_links


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
class Rant:
    """One open rant (`pending` / `in_progress`) and the issue it was paired with.

    Rendered because a rant appears in **no other row of this reading**: the PR rows,
    the vote counts, the freshness lines and the issue link states can all be clean
    while unstarted rants sit in the ledger, and this is the reading §1 of the
    evolution prompt points at for "is there a row to move". Measured 2026-09-29: the
    tool took no rant input at all (`grep -c rant` → 1, a quoted comment), and printed
    `3 PR(s): measure-then-vote 2, park 1` while three pending rants with no issue
    existed. A cycle trusting it concluded the honest-looking but wrong "nothing to
    evolve". §2's rant curation reads the ledger directly, which is why the trap bit
    only the cycles that read the queue alone.

    **The number comes from the entry's own `progress`** (R5, issue #2352): taking a rant
    on means writing `issue #N` there the moment the issue is opened, and that entry is
    where the pairing is durable — it is the one record that is per-installation *and*
    holds the whole history, while the issue is neither. The `Origin:` line an older issue
    carries is still resolved as a fallback, for entries filed before that rule; nothing
    new depends on it.

    Three states, because they are three instructions to the reader: `#N` (an open issue —
    work is filed), `#N closed` (its PR merged, or it was closed without one — read it
    before filing anything, the duplicate this row exists to prevent) and `no issue yet`.
    A number the API could not resolve is `#N unread`, never folded into `no issue yet`:
    "not there" and "could not be asked" are different answers. Several numbers mean
    several issues claim one rant, a fault `check-issue-links.py` reports as a duplicate
    rather than something to merge here, so this row names them all and picks none.

    `project` is the ledger row's own field, carried because it is what §2.2 decides on:
    a rant belongs to this cycle when it names this task's project or its `owner/repo`,
    and to somebody else when it names another one. Measured 2026-10-01 on this host, the
    queue rendered **39** open rants — every one of them `silicon-science-cs` — as work
    the emrg cycle owed an issue and a PR, while the emrg rows in that ledger were all
    `completed`; the field was in the row the parser had just read and was dropped here,
    so the reading that exists for a cycle which reads it *alone* could not be filtered by
    the rule the template gives that cycle.
    """

    timestamp: str
    status: str
    message: str = ""
    issues: list[int] = field(default_factory=list)
    closed_issues: list[int] = field(default_factory=list)
    unread_issues: list[int] = field(default_factory=list)
    project: str = ""


#: The handle R5 writes into a rant's `progress`: the word `issue` and the number. Anchored
#: on that word rather than on a bare `#N`, because the same line normally names the PR too
#: (`issue #2110 (closed) → PR #2111 merged`) and a PR number is not what this row reports.
#: The tail continues the list only in the forms a writer actually uses for it — `#1/#2`,
#: `#1, #2`, `#1 and #2` — so a sentence that mentions an unrelated number later stays out.
_PROGRESS_ISSUE = re.compile(r"(?i)\bissues?\s*#(\d+)((?:\s*(?:,|/|&|\band\b)\s*#\d+)*)")
_PROGRESS_MORE = re.compile(r"#(\d+)")


def _progress_issues(progress: object) -> list[int]:
    """The issue numbers an entry's `progress` names, in the order it names them.

    Every entry the tool has handled spells the pairing this way (`issue #1985 (closed
    COMPLETED) → PR #1986 merged`), and R5 now writes it as the *only* durable record of
    which issue a rant became — so this is the reading, not a convenience: a row that
    looked somewhere else would print `no issue yet` over work that is filed, and the
    cycle reading it would file it again.

    The first `issue #N` is the pairing; later ones belong to it only when they are
    written as its continuation. A `progress` that names no issue is not an error — it is
    a rant that has not been taken on (or one filed under the older convention, which the
    caller resolves from the issue side).
    """
    text = progress if isinstance(progress, str) else ""
    first = _PROGRESS_ISSUE.search(text)
    if not first:
        return []
    numbers = [int(first.group(1))]
    numbers.extend(int(n) for n in _PROGRESS_MORE.findall(first.group(2) or ""))
    seen: list[int] = []
    for number in numbers:
        if number not in seen:
            seen.append(number)
    return seen


def open_rant_rows(rants: str | None = None, repo: str = REPO) -> list[Rant]:
    """Every `pending` / `in_progress` rant, newest first, with the issue it was paired with.

    Only the ledger's open states are rows: `completed` is history (R6's cleanup keeps
    the ten most recent), and rendering it would bury the work that is actually left.

    The issue lookup is spent **only when an open rant exists**, because it costs a
    second `gh` call and a queue with nothing pending should not pay for it. Raises the
    sibling's `RuntimeError` when the ledger cannot be read — the caller reports that as
    unmeasurable (exit 2), never as "no rants", which is the same rule `open_prs` follows
    one direction over.

    **The pairing is read from the entry first** — `issue #N` in its own `progress`, the
    handle R5 writes at take-up — and from the issue side only when the entry names none:
    the `Origin:` line an issue carries is the older convention, and dropping the fallback
    would print `no issue yet` over every rant filed under it, inviting exactly the
    duplicate this row exists to prevent. The two readings are never mixed on one row: the
    issue a rant's own entry names is the one a cycle wrote down, and an issue-side
    declaration cannot disagree with it without one of the two being stale.
    """
    links = issue_links()
    rows = links.load_rant_rows(links.rants_path(rants))
    wanted = [
        row for row in rows if str(row.get("status", "")) in ("pending", "in_progress")
    ]
    if not wanted:
        return []

    queue = links.load_queue(repo)
    open_numbers = {
        int(row["number"]) for row in queue.issues if isinstance(row.get("number"), int)
    }
    named = {
        str(row.get("timestamp", "")): _progress_issues(row.get("progress"))
        for row in wanted
    }

    declared = (
        links.declared_origins(queue.issues)
        if any(not numbers for numbers in named.values())
        else {}
    )
    by_timestamp: dict[str, list[int]] = {}
    for number, stamps in declared.items():
        for stamp in stamps:
            by_timestamp.setdefault(stamp, []).append(number)

    # The fallback's second half: an issue filed under the older convention and closed by
    # the merge of its own PR is invisible to the open reading above, so its row would claim
    # `no issue yet` — the same words it prints for a rant that never had one. Measured
    # 2026-10-02: the release rant rendered `no issue yet` while issue #1807 existed,
    # declared the rant's timestamp, and had been closed when #1808 merged. R5 tells a cycle
    # that taking up a rant means filing its issue, so that reading invites a duplicate
    # issue for work already finished. The second reading is spent only when some open rant
    # lacks an open declaring issue, and only over the window those rants could have been
    # filed in.
    #
    # "Those rants" is this repo's rants, not the whole ledger. The bound below is the
    # oldest instant that can *place* a row, and another project's rants cannot be placed
    # here at any bound — so letting them set the window buys nothing and costs the whole
    # ledger's history. Measured 2026-10-02 on this host: 40 open rants, of which 39 were
    # `silicon-science-cs` with the oldest reaching back to 2026-09-11,
    # while the only `emrg` one was 8 hours old — the same reading cost **48.8s / 668
    # rows** at the ledger's bound and **5.8s / 6 rows** at this repo's.
    closed_by_timestamp: dict[str, list[int]] = {}
    unplaced = [
        str(row.get("timestamp", ""))
        for row in wanted
        if not named[str(row.get("timestamp", ""))]
        and not by_timestamp.get(str(row.get("timestamp", "")))
        and could_declare_here(str(row.get("project", "") or ""), repo)
    ]
    if unplaced:
        since = min(
            (parsed for parsed in (instant(stamp) for stamp in unplaced) if parsed),
            default=None,
        )
        if since is not None:
            # Spelled in UTC with a `Z`: the ledger's offset carries a `+`, and a query
            # string reads that as a space (`+08:00` arrives as ` 08:00`), so the instant
            # must not be passed in the form the ledger stores it in. Measured on this
            # repo: `since=<UTC Z form>` is one request returning 20 rows in ~1.2s.
            closed = links.load_queue(
                repo,
                "closed",
                since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            # `Origin:` is an issue's line, and the issues endpoint returns PRs too.
            closed_issues = [row for row in closed.issues if "pull_request" not in row]
            for number, stamps in links.declared_origins(closed_issues).items():
                for stamp in stamps:
                    closed_by_timestamp.setdefault(stamp, []).append(number)

    out: list[Rant] = []
    for row in wanted:
        stamp = str(row.get("timestamp", ""))
        numbers = named[stamp]
        open_here: list[int] = []
        closed_here: list[int] = []
        unread_here: list[int] = []
        for number in numbers:
            if number in open_numbers:
                open_here.append(number)
                continue
            # Not in the open listing, so the question is which of the two remaining
            # states it is — and that costs one call per closed number, not per row: a
            # rant whose issue is still open is answered by the listing already.
            state = links.issue_state(repo, number)
            if state == "open":
                open_here.append(number)
            elif state == "closed":
                closed_here.append(number)
            else:
                unread_here.append(number)
        if not numbers:
            open_here = sorted(by_timestamp.get(stamp, []))
            closed_here = sorted(closed_by_timestamp.get(stamp, []))
        out.append(
            Rant(
                timestamp=stamp,
                status=str(row.get("status", "")),
                message=str(row.get("message", "")),
                issues=sorted(open_here),
                closed_issues=sorted(closed_here),
                unread_issues=sorted(unread_here),
                project=str(row.get("project", "") or ""),
            )
        )
    return sorted(out, key=lambda rant: rant.timestamp, reverse=True)


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
    #: The login whose CI run fixed `head_pushed_at`, or `""` when there was none to
    #: ask. The abstention clause is about a head *this* instance pushed, and the
    #: window is only a proxy for that; empty keeps the clause applied.
    head_pusher: str = ""
    #: The window the clause was applied over, and what it rests on. `window_start`
    #: empty means the clause could not be applied at all — never "no window needed".
    window_start: str = ""
    window_source: str = ""
    stale_read: bool = False
    stale: bool = False
    stale_kind: str = ""
    stale_reason: str = ""
    #: The CI run the stale verdict is about, empty when there is none. The `ci-red`
    #: row's remedy is built from it: "read why it failed" is runnable as printed only
    #: if the row has the run, and the alternative - the id in the link `gh pr checks`
    #: prints - is a command the reader has to assemble by hand.
    ci_run_id: str = ""
    behind_by: int | None = None
    unread: str = ""
    #: GitHub's lifecycle state for the PR (`OPEN`, `MERGED`, `CLOSED`). Everything
    #: else on this row is a question about a *live* PR, so this is read before any
    #: of them is acted on — see `terminal`. Empty means "not read".
    state: str = ""
    merged_at: str = ""

    @property
    def terminal(self) -> bool:
        """The PR is over: merged, or closed without merging.

        Measured 2026-10-03 (`cyc20261003-224625`): a cycle hit a merge four seconds
        after its scan, and the row for that PR was built from the state of a live one
        — `read-first`, with a `--mergeability-wait 60` that can never answer, because
        GitHub computes no mergeability for a merged PR. The state is read from the
        counter's verdict, so a finished PR is answered here rather than mis-filled.

        The vocabulary is read from the counter too, for the reason `votes_needed`
        states one function up: a second copy of a list is a second answer to "which
        states mean the PR is over", and the copy that is not read drifts when the
        original moves. Measured 2026-10-03 (`cyc20261003-231313`): this property
        spelled `("MERGED", "CLOSED")` by hand while the counter's `TERMINAL_STATES`
        was introduced as "the one spelling of 'the PR is over' in the family" — the
        sibling it named (`check-merge-freshness.py`) asks it, and this file, which is
        the one that reads the state off the verdict, did not.

        `self.state` is asked first, and an empty one returns before the counter is
        touched: `""` means "not read", which is the state of a row whose count could
        not be read at all (`unread`). Asking the sibling there would turn one failure
        into two — measured 2026-10-03 (`cyc20261003-231313`) on this file's own
        `Boom` fake, which raises from `check_pr` and carries no vocabulary.
        """
        return bool(self.state) and self.state in tuple(vote_counter().TERMINAL_STATES)

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
    window's far bound out of it, so a corpus named here and not there would have this
    row's window read from one collection of cycles and its *count* judged against another.
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
    out.state = str(getattr(verdict, "state", "") or "")
    out.merged_at = str(getattr(verdict, "merged_at", "") or "")
    if out.terminal:
        # The freshness half is not read, and that is a decision rather than an
        # economy: it asks whether a *green CI verdict* would still transfer to the
        # tree this merge lands, and a finished PR has no merge to land. Measured
        # 2026-10-03 (`cyc20261003-224625`) on the merged #1836: it reports
        # `STALE (diverged, behind_by=2) - the head does not contain master` and
        # prices a branch refresh for a branch that is finished. `head_pushed_at` is
        # left empty for the same reason the counter's own line omits it: for a
        # merged PR the timestamp the counter carries there is the *merge* time, and
        # printing it as a push would misdate the head on the row that is about it.
        return out
    out.head_pushed_at = str(verdict.push_time)
    out.head_pushed_exact = bool(verdict.push_time_exact)
    out.head_pusher = str(getattr(verdict, "pusher", "") or "")
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
    out.ci_run_id = str(getattr(fresh, "run_id", "") or "")
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

    * **a finished PR is answered first** (issue #1837): a PR that is MERGED or CLOSED
      is nothing to vote on, merge or refresh, and *every* branch below asks a
      question about a live PR. Measured 2026-10-03 (`cyc20261003-224625`): a merge
      landed four seconds after a scan listed the PR open, so this tool read a
      finished PR as a live one and answered `read-first` with a
      `--mergeability-wait 60` that provably cannot succeed - GitHub computes no
      mergeability for a merged PR, so the wait is spent on a question with no
      answer. The row names no command at all: there is nothing here to run;
    * then an unreadable count, because every later branch is a decision about a
      number this one does not have;
    * a standing veto is "fix push", not "vote" — and it is checked before the
      count, because a veto has already reset the run: a `0/3` caused by a ❌ looks
      exactly like a `0/3` that was never reviewed, and only one of them is a PR a
      cycle may vote on;
    * a text conflict is next: more review does not fix it, and resolving it
      replaces the head and voids whatever votes exist;
    * then a CI half that could not be read, because the branches below are decisions
      about it or about the branch state that stands in for it: a row built there would
      prescribe a remedy for a reading nobody took (measured 2026-10-07,
      `cyc20261007-203559` - a gh timeout in the compare call printed the merge-state
      row `unblock` on a head that was only waiting for CI);
    * then the three CI states the freshness tool distinguishes — red, absent,
      unfinished. None of them is votable, and their remedies differ, which is why
      they are not collapsed into "not fresh": a red run is read, an absent one is
      re-triggered, and an unfinished one is **parked** — deferred to a later cycle
      rather than waited on, because a window spent blocking on a run this cycle
      cannot vote on is a window not spent on a row it could move (host rant
      2026-09-24T14:46:10);
    * then a merge state that withholds the merge (draft, blocked, behind) — again
      not a review problem, and not curable by a vote;
    * **then the abstention** (issue #1408): a head pushed inside this cycle's own run
      — its own start to the start of the cycle that follows it — is not one this cycle
      may vote on or merge. It sits
      directly above the two branches that *spend* something, because every branch
      above it asks "what does this PR need?" — and a head one pushed may still need
      a fix push, a conflict resolved, or CI to finish, which is work for the pusher
      rather than a vote for anyone;
    * then the count decides: enough votes is "merge", otherwise "vote" — and a head
      that no longer contains master gets the landing-tree form of that vote, the
      reading whose absence stalled a cycle.
    """
    pr = reading.pr
    if reading.terminal:
        # No command, because there is none that helps: the PR is over. `read-first`
        # below is what this used to answer - with a wait flag that cannot succeed -
        # for a PR merged seconds after the scan listed it (issue #1837).
        return Action(
            kind="terminal",
            why=(
                f"#{pr} is {reading.state}"
                + (f" (merged {reading.merged_at})" if reading.merged_at else "")
                + ": this PR is over, so there is nothing here to vote on, merge or "
                "refresh - and nothing to read first: the row would otherwise hand out "
                "a mergeability wait that no merged PR can ever answer"
            ),
        )
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
    if reading.unread:
        # The CI half was not read: every branch left is either that half (the run's
        # state) or the branch state that stands in for it, so a row built down there
        # prescribes a remedy for something nobody measured. Placed *after* the veto and
        # conflict rows, which are answered by the counter alone and both move the head
        # anyway - so neither becomes an unanswerable question by the CI half being
        # missing. Measured 2026-10-07 (`cyc20261007-203559`): the same head read twice a
        # minute apart gave `unblock` and then `park`, and the `unblock` was this path - a
        # gh timeout in the compare call left `stale_read` False and the merge-state row,
        # whose subject is the state of a branch, answered for a state nothing had read.
        # The remedy is the same re-ask the unreadable count gets: the reading exists, it
        # just has not been taken.
        return Action(
            kind="read-first",
            why=reading.unread,
            command=f"{RUNNER} scripts/check-merge-freshness.py {pr}",
        )
    if reading.stale_read and reading.stale_kind == "failing":
        return Action(
            kind="ci-red",
            why=reading.stale_reason
            + " - not votable: a vote at this head would be a vote about a tree whose "
              "CI ran red, and a re-run only helps if the failure was a flake. "
              "`gh pr checks` names the failing check, not its cause, so read the cause "
              "with the reading that answers - and before fixing anything, ask whether "
              "the row is the head's own, because a base-level failure turns every open "
              "PR red and `check-merge-plan-suite.py` reports the rows the base tree "
              "fails too",
            command=f"gh pr checks {pr} -R {repo}",
            extra=[
                f"{RUNNER} scripts/read-run-failure.py "
                f"{reading.ci_run_id or '<run-id from the link above>'}",
                f"{RUNNER} scripts/check-merge-plan-suite.py {pr}",
            ],
        )
    if reading.stale_read and reading.stale_kind == "no_run_yet":
        # The head is younger than a push-event run takes to appear, so the empty
        # run lookup is not yet evidence of anything and the row takes `running`'s
        # verb for the same reason that one does: the head is not votable until a
        # run concludes, and the re-trigger `no_run` prescribes would fire a
        # *second* run on a head whose first one is arriving (measured 2026-10-07:
        # a re-trigger does not cancel it - `test.yml` declares no concurrency
        # group - so the duplicate runs in parallel). Measured by the row that is
        # not here: cycle `cyc20261007-022540` read "no checks reported" from
        # `gh pr checks` seconds after pushing head `338a9a53`, while that head's
        # run existed and was registering.
        return Action(
            kind="park",
            why=reading.stale_reason
            + " - parked for this cycle: the head is too young for the empty lookup "
              "to mean the push was dropped, and a re-trigger here starts a second "
              "run beside the one arriving. Read this PR again next cycle; a head "
              "still run-less then is `no_run`, whose row does name the re-trigger",
            command=f"gh pr checks {pr} -R {repo}",
        )
    if reading.stale_read and reading.stale_kind == "no_run":
        return Action(
            kind="retrigger-ci",
            why=reading.stale_reason
            + " - re-triggering fires a run on the same head, which keeps the votes a "
              "refresh would spend",
            command=f"gh workflow run test.yml --ref <branch-of-{pr}>",
            extra=[f"bash scripts/re-trigger-ci.sh <branch-of-{pr}>"],
        )
    if reading.stale_read and reading.stale_kind == "no_verdict":
        # A run that stopped without judging the tree is the `ci-red` row's *other* half:
        # its remedy is `no_run`'s, not "read the failure". Measured 2026-10-06: this row
        # used to be `ci-red`, and handed over `read-run-failure.py <run>` - whose answer
        # for a cancelled job is "no failed job … nothing to explain", because there is no
        # cause to read. The verb stays `park` for the same reason `running` does: the
        # head is not votable until a run concludes, and the re-trigger starts one.
        return Action(
            kind="retrigger-ci",
            why=reading.stale_reason
            + " - re-triggering fires a run on the same head, which keeps the votes a "
              "refresh would spend, and park the PR until that run concludes",
            command=f"gh workflow run test.yml --ref <branch-of-{pr}>",
            extra=[
                f"bash scripts/re-trigger-ci.sh <branch-of-{pr}>",
                f"gh pr checks {pr} -R {repo}",
            ],
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
        # The row used to end "the branch has to remove it" and to hand the reader
        # `gh pr view --json mergeable,mergeStateStatus` - a command that reprints the
        # fact the row has just stated. Both were wrong in the same direction: the
        # non-clean states do *not* share one cure (a `DRAFT` clears when the PR is
        # marked ready, a `BLOCKED` with a review, a `BEHIND` by the refresh that moves
        # the head, and an `UNSTABLE` held by a superseded run's check-run not at all),
        # and the reading that answers "why is this not clean" is the counter's own
        # report, which now carries the head's check-runs. Measured 2026-10-06
        # (`cyc20261006-091811`) on #1865, whose row read `unblock` while its newest
        # check-runs were green and its head already contained master: there was
        # nothing for the prescribed remedy to publish.
        conflict = reading.mergeable == "CONFLICTING" or reading.merge_state == "DIRTY"
        command = (
            f"{RUNNER} scripts/classify-conflict.py --all"
            if conflict
            else f"{RUNNER} scripts/check-vote-count.py {pr}"
        )
        return Action(
            kind="unblock",
            why=reading.block_reason
            + " - a state of the branch, not of the review: no vote cast here changes "
              "it, and which move clears it is the state's own (the reason above names "
              "it; a refresh is only that move for a state that is about the tree)",
            command=command,
        )
    identity_lifted = ""
    if window is not None and window.applied:
        pushed = instant(reading.head_pushed_at)
        # Whose push is it? The window is only a proxy for "this instance pushed it",
        # and the proxy misreads the ordinary case: a head the peer pushed inside a gap
        # between this host's cycles lands inside the window and is not this cycle's
        # work at all (measured 2026-10-06, `cyc20261006-122605`: #1869's head, pushed
        # by the peer at 04:07:01Z, read `abstain`). A positive reading of a login
        # *outside the set this instance pushes under* exempts the head; an unknown one
        # keeps the clause, so the row can only un-abstain on a push that is provably
        # someone else's. The set rather than one login: a host may hold two (the
        # remote's account and the token's), and comparing one un-abstained this
        # instance's own pushes under the other — the review on #1900 measured it on
        # 2026-10-08 (actor `argszero` against a token answering `how2how2how2-arch`).
        other_pusher = bool(reading.head_pusher) and not vote_counter().own_login(
            reading.head_pusher
        )
        # Which datum decided, said on the row either way: a reader who cannot see it
        # cannot tell an abstention the clock produced from one an identity produced,
        # and those two are corrected in opposite directions.
        decided = (
            f"the head's pusher {reading.head_pusher} reads as one of this instance's "
            "own logins"
            if reading.head_pusher
            else "the head carries no pusher reading, so the clause is kept"
        )
        if pushed is not None and window.covers(pushed) and not other_pusher:
            return Action(
                kind="abstain",
                why=f"head pushed {reading.head_pushed_at}, inside the run this cycle "
                    f"treats as its own ({window.source}) - a cycle neither votes on nor "
                    "merges a head it pushed, so the next vote here (and the merge) has to "
                    f"come from a later cycle; {decided}",
                command=f"{RUNNER} scripts/check-vote-count.py {pr}",
            )
        if other_pusher and pushed is not None:
            identity_lifted = (
                f"; the head was pushed by {reading.head_pusher}, outside this "
                "instance's login set, so the own-window clause is not asked of it"
            )
    if reading.votes >= reading.needed:
        if reading.stale:
            return Action(
                kind="measure-then-merge",
                why=f"{reading.votes}/{reading.needed} votes, but "
                    + reading.stale_reason
                    + " - measure the landing tree before merging; the head does not "
                      "move, so the votes that carried it here stay valid"
                    + identity_lifted,
                command=f"{RUNNER} scripts/check-merge-plan-suite.py {pr}",
                extra=[f"{RUNNER} scripts/check-merge-tree-health.py"],
            )
        return Action(
            kind="merge",
            why=f"{reading.votes}/{reading.needed} valid votes, none predating the head "
                "push, and the head's green run is about the tree that would land"
                + identity_lifted,
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
                  "shows the base's own later commits as reversals this PR does not make"
                + identity_lifted,
            command=f"{RUNNER} scripts/check-merge-plan-suite.py {pr}",
            extra=[
                f"{RUNNER} scripts/check-merge-landing-diff.py {pr}",
                vote_cmd,
            ],
        )
    return Action(
        kind="vote",
        why=f"{reading.votes}/{reading.needed} votes, a fresh head, no unanswered veto"
            + identity_lifted,
        command=vote_cmd,
    )


def rows(readings: list[Reading], cycle: str | None, repo: str,
         window: Window | None = None) -> list[tuple[Reading, Action]]:
    """(reading, action) per PR, in the order the PRs were given."""
    return [(reading, next_action(reading, cycle, repo, window)) for reading in readings]


def window_note(window: Window | None) -> str:
    """What the report owes the reader about the own-window clause, before its rows.

    Two states are readings of that clause the rows cannot show on their own, and each of
    them moves a row that otherwise reads `vote`:

    * **no `--cycle`** — the clause was not applied at all. The rows answer the count
      question and nothing else, so a `vote` here may be a head this very cycle pushed.
      Nothing said so: the prose was byte-identical in shape to a windowed run, and the
      remedy the row prints (`cast-vote.py <PR> ...`, which omits `--cycle` because the
      tool was not given one) does not carry the correction either. Measured 2026-09-26
      (`cyc20260926-120320`): a first run without the flags answered `vote` for `#1636`,
      the head the cycle immediately before it had pushed, and only re-running with
      `--cycle` turned that row into `abstain`.

      The **cost, measured rather than carried over** — the vote itself is not spent:
      `cast-vote.py` reads the cycle id out of the body, resolves the window itself, and
      refuses with `own-head-window` (rc 2), which it did on that very head in this
      cycle's probe. What the unflagged run costs is a **wrong reading**: the first tool a
      cycle runs states `vote` where its own windowed answer is `abstain`, and a reader
      who acts on the row has acted on a verdict the tool does not hold.

    * **a window with no start** — the cycle id given names no instant, so the clause was
      not applied either, and every row is the count question alone.

    The placement is the point, and it is this family's convention rather than a
    preference: a caveat that follows the row it weakens is read after the reader has
    already acted on it. Printed first, it also survives `| head`, which the tail
    placement did not — a queue of eight PRs is ~40 lines, and the note was line 39.

    Returns the note (empty when the clause was applied). Prose only: under `--json` the
    same facts are the documented `vote_window_start` / `vote_window_source` pair, and a
    line ahead of the document would break every machine consumer.
    """
    if window is None:
        return (
            "note: no --cycle was given, so the own-window clause was NOT applied - a "
            'row reading "vote" may be a head this cycle pushed, which the clause turns '
            "into `abstain`. Pass --cycle <this cycle's id> to have the clause applied."
        )
    if window.unresolved:
        return (
            "note: the own-window clause was NOT applied - "
            f"{window.unresolved}, so the run this cycle owns was never delimitated and a "
            'row reading "vote" may be a head this cycle pushed. Pass --cycle with this '
            "cycle's own id to have the clause applied."
        )
    return ""


def render(reading: Reading, action: Action) -> str:
    """One PR's block: what is true, then what to do, then the exact command."""
    head = reading.head[:8] if reading.head else "????????"
    marks = []
    if reading.terminal:
        # The count slot carries the state where a live row carries `n/3 votes`: the
        # number a merged PR's row would print is the history of a review that is
        # over, and `0/3 votes` on a finished PR reads as work still to do. The
        # marks are left off for the same reason - `stale:` describes a verdict that
        # could fail to transfer, and `pushed …` would print the merge time as the
        # push (the counter carries the merge time in that field for a merged PR).
        count = reading.state
        if reading.merged_at:
            marks.append(f"merged {reading.merged_at}")
    else:
        count = (
            f"{reading.votes}/{reading.needed} votes" if reading.votes is not None
            else "? votes"
        )
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
    lines = [f"#{reading.pr} {count}  head {head}  {action.kind}{suffix}"]
    lines.append(f"    {action.why}")
    if action.command:
        lines.append(f"    $ {action.command}")
    for extra in action.extra:
        lines.append(f"    $ {extra}")
    return "\n".join(lines)


def render_rant(rant: Rant) -> str:
    """One rant's row: its handle, its state, the issue it was paired with, and its project.

    The excerpt is flattened to a single line and capped, because a rant body is prose
    with headings and newlines and this row's job is to be *recognisable* in a list —
    `submit_rant(action="list")` is the reading that shows it whole.

    The project is last and keyed rather than bare, so it cannot be read as part of the
    status or as an issue number, and so a row without one is visible as such: §2.2 makes
    a rant that names no project *(nor this task's)* one to ignore entirely.

    `no issue yet` means what it says: the entry names no issue and no issue in **either**
    state declares this rant. An issue that was closed — normally by the merge of its own
    PR — renders as `#1807 closed`, because the two are opposite instructions: the first
    says "file one", and following it for an issue that already exists creates the
    duplicate this repo's link reading exists to report. The row states that the issue is
    closed and stops there: a close by merge and a close without one are not
    distinguishable from here, so the cycle reads the issue rather than being told which
    it was. `#N unread` is the fourth answer and a different one again: the entry names a
    number the API would not resolve, which is a question for the reader, not an absence.
    """
    tokens = [f"#{n}" for n in rant.issues]
    tokens += [f"#{n} closed" for n in rant.closed_issues]
    tokens += [f"#{n} unread" for n in rant.unread_issues]
    where = ", ".join(tokens) if tokens else "no issue yet"
    excerpt = " ".join(rant.message.split())
    if len(excerpt) > 110:
        excerpt = excerpt[:109] + "…"
    lines = [
        f"rant {rant.timestamp}  {rant.status}  {where}  "
        f"project={rant.project or '(none)'}"
    ]
    if excerpt:
        lines.append(f"    {excerpt}")
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


def _as_json(
    readings: list[tuple[Reading, Action]],
    rants: list[Rant] | None = None,
    repo: str = REPO,
) -> str:
    # The clone and its branch ride as *fields* on each reading, the way
    # `check-merge-landed.py` states its tree in `--json`: the document's shape is a
    # list, and a prose line ahead of it would be a second kind of line in a stream a
    # machine consumer parses.
    #
    # Rant rows join the same list rather than becoming a second document, for the same
    # reason — one shape. `subject` is what tells the two apart, and it is on **every**
    # row rather than only the rants: a consumer that had to infer "no `pr` key means a
    # rant" would be reading a shape by absence.
    root, branch, _head = local_tree()
    rows = [
        {
            "subject": "pr",
            "tree": root,
            "branch": branch,
            "pr": reading.pr,
            "head": reading.head,
            "title": reading.title,
            "votes": reading.votes,
            "needed": reading.needed,
            "mergeable": reading.mergeable,
            "merge_state": reading.merge_state,
            # The lifecycle state and its consequence, on every row: a consumer that
            # read `votes: 0` off a finished PR would see the same number as a PR
            # nobody has reviewed yet, and the two call for different actions.
            "state": reading.state,
            "terminal": reading.terminal,
            "merged_at": reading.merged_at or None,
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
    ]
    rows.extend(
        {
            "subject": "rant",
            "tree": root,
            "branch": branch,
            "timestamp": rant.timestamp,
            "status": rant.status,
            "issues": rant.issues,
            # The other two answers a pair of numbers can be in, named as fields rather
            # than left to be inferred from an empty `issues`: a consumer that read only
            # `issues == []` would count a closed issue and a never-filed rant the same.
            "closed_issues": rant.closed_issues,
            "unread_issues": rant.unread_issues,
            "message": rant.message,
            "project": rant.project,
            # The prose rendering withholds another project's rows (a cycle's report is
            # the surface whose size is paid, and the prompt says those rows are not its
            # work), but the **document keeps every row** and labels it here instead: a
            # consumer that lost rows silently would be reading a short list as a whole
            # one, which is this family's "never a pass" defect in its JSON form. The
            # field is the prose's own predicate, spelled once - `rendered_here`.
            "rendered": rendered_here(rant.project, repo),
        }
        for rant in (rants or [])
    )

    return json.dumps(rows, indent=2)


def main(argv: list[str] | None = None) -> int:
    # The family's convention is claimed below ("names it before it gives a
    # verdict"), and this call is what makes it hold under a pipe: stdout is
    # block-buffered when it is not a tty while stderr is not, so the
    # `unmeasurable: the rant ledger could not be read` line would otherwise be
    # read *above* the `tree:` line - measured 2026-10-10 on this script.
    # Behaviour and pin: tests/test_guard_report.py.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        prog="review-queue.py",
        description="For every open PR: its counted votes, and the next action for a cycle.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        # No prefix matching, so `--cycle` (which is `cast-vote.py`'s flag, not this
        # tool's) cannot be bound to `--cycles-log` and read a cycle id as a
        # directory path - the misparse that made an unreadable corpus narrow the
        # abstention window into a *counted* vote (measured 2026-10-15, issue #2274).
        allow_abbrev=False,
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
        "--cycles-log",
        default=None,
        help="directory of `cycle-<id>.md` records, read for the cycle that follows "
             "this one - the window's far bound (default: $EMRG_CYCLES_LOG, else the "
             f"{os.pathsep}-joined directories the memory system writes records to: "
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
        "--rants",
        default=None,
        help="the rant ledger to read (default: $EMRG_RANTS, else ~/.emrg/rants.jsonl). "
             "Open rants are rendered as rows of their own - a queue that showed only "
             "PRs once read as 'nothing to move' while three pending rants had no issue",
    )
    parser.add_argument(
        "--all-rants",
        action="store_true",
        help="print every open rant's row, including other projects' - the default "
             "renders the rows `rendered_here` accepts (this repo's, and the rows "
             "naming no project: naming no project is still printed, because "
             "undeclared is not another project's work) and counts the rest",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the readings as JSON instead of prose"
    )
    args = parser.parse_args(argv)

    needed = args.min_votes if args.min_votes is not None else votes_needed()

    window: Window | None = None
    if args.cycle:
        # The window composes in exactly one place: `own_window` owns both bounds - this
        # cycle's own start and the start of the cycle that follows it - because the two
        # sibling tools that act on the same window (`cast-vote.py` at the poster and
        # `check-vote-count.py` at the counter) read it from there too (issue #2218).
        window = own_window(args.cycle, args.cycles_log)

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

    # Both halves of a PR's reading count as "could not be read": the count (its verdict
    # carries no votes at all) and the CI half (the ancestry/run call raised, and the
    # reason sits in `reading.unread`). The exit-code table above says "a PR in it could
    # not be read" and means the PR, not one of its two reads - measured 2026-10-07
    # (`cyc20261007-203559`): an ancestry timeout left rc 0 and no `unmeasurable:` line
    # while the row under it prescribed a remedy for a state nothing had read. A terminal
    # PR is excluded: its row is the lifecycle state, which was read, and a finished PR
    # has no ancestry question left to answer.
    unread = [
        reading.pr
        for reading, _ in readings
        if reading.votes is None or (reading.unread and not reading.terminal)
    ]

    # Read after the PR rows rather than before: an unreadable ledger is reported
    # alongside the PR reading, not instead of it, so a cycle still gets the half that
    # could be measured. `rants_unread` is why, and it is carried to the exit code the
    # same way an unreadable PR is — "could not measure" is never rendered as "clean".
    try:
        rants = open_rant_rows(args.rants, args.repo)
        rants_unread = ""
    except Exception as exc:  # noqa: BLE001
        rants, rants_unread = [], str(exc)

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
        print(_as_json(readings, rants, args.repo))
    else:
        if not queue:
            # Only claim "nothing" when there is nothing to report on — and that includes
            # the half that could not be read. An unreadable ledger leaves `rants` empty
            # for the same reason an unreadable `gh` does, so the sentence is gated on it
            # too: "nothing to review" printed over a ledger nobody could open is the
            # defect this file's sibling exists for, one level over.
            if not rants and not rants_unread:
                print(f"no open PRs in {args.repo} and no open rants - nothing to review")
        else:
            # Before the first row, never after it: this says how strong the reading
            # below is, and a reader who has already copied a row's command has acted.
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
            summary = ", ".join(
                f"{kind} {count}" for kind, count in sorted(tally.items())
            )
            print(f"{len(readings)} PR(s): {summary}")
            if unread:
                print(
                    f"unmeasurable: {', '.join(f'#{pr}' for pr in unread)} - "
                    "read them before acting"
                )

        if rants:
            # After the PR rows, and with its own header: a cycle that reads only the
            # `N PR(s)` line above must not come away thinking the queue was the whole
            # of what is open.
            print()
            counts: dict[str, int] = {}
            for rant in rants:
                name = rant.project or "(no project)"
                counts[name] = counts.get(name, 0) + 1
            across = ", ".join(
                f"{name} {count}"
                for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            print(
                f"{len(rants)} open rant(s) across {len(counts)} project(s) - {across} - "
                "each needs an issue and its PR (R5) once it is this task's; the prompt's "
                "rant section matches a rant to a task by `project` (this task's project, "
                "or its owner/repo), so a row naming another project is not this cycle's "
                "work and is withheld by default - one naming no project is undeclared "
                "rather than another project's, and is rendered (`rendered_here` decides "
                "both):"
            )
            # Rendered by default: the rows `rendered_here` accepts - this repo's, and the
            # ones naming no project (a second predicate, `could_declare_here`, is **not**
            # the rendering rule: it is false for a project-less row too, so it separates
            # nothing here). Another project's rows are **counted above and not printed** —
            # the header keeps the ledger whole, so nothing is hidden, and their bodies are
            # what this section's cost was made of (measured 2026-10-04 on this host: 40 open
            # rants, 39 of them `silicon-science-cs`, ~8.7KB of a ~9.7KB report, paid by a
            # cycle for whom the prompt itself says they are not its work). `--all-rants`
            # prints them all.
            rendered = rants if args.all_rants else [
                rant for rant in rants if rendered_here(rant.project, args.repo)
            ]
            withheld = len(rants) - len(rendered)
            if withheld:
                print(
                    f"{withheld} of them "
                    + ("names" if withheld == 1 else "name")
                    + " another project, so "
                    + ("its row is" if withheld == 1 else "their rows are")
                    + " counted above and not printed here: `rendered_here` withholds "
                    "another project's rows and only those - a row naming no project is "
                    "undeclared, not another project's, so it is not withheld either. "
                    f"`{RUNNER} scripts/review-queue.py --all-rants` prints every row."
                )
            print()
            for rant in rendered:
                print(render_rant(rant))
                print()

    if rants_unread:
        print(f"unmeasurable: the rant ledger could not be read - {rants_unread}",
              file=sys.stderr)

    # The same verdict however the reading is rendered: a row nobody could read is
    # not a row that is fine - and that includes the half that is not a PR.
    return 2 if (unread or rants_unread) else 0


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
