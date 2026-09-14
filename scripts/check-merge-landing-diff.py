#!/usr/bin/env python3
"""Show the change merging a PR lands, and what its diff-to-master is *not*.

The class this exists for
-------------------------
The gates in this family ask, in the order a queue is drained: do the votes still
apply (`check-vote-count.py`), can a merge here reach master (`check-pr-base.py`),
is the green verdict about the tree that merges (`check-merge-freshness.py`),
whom would this dirty (`check-merge-order.py`), does the landed tree pass the
guards (`check-merge-tree-health.py`), does a whole plan's tree pass the suite
(`check-merge-plan-suite.py`).

None of them answers the question a review *starts* with: **what change does
merging this PR make?** The obvious substitute - `git diff master <head>` - is not
that change. It is the landing change **plus** every commit master gained after
the branch point, with the branch's older copy of those lines shown as a deletion.
Where the two are mixed, the diff reads as a PR that undoes work it never touched,
and reviewing a diff that is not the change is how a reviewer reaches the wrong
conclusion about a correct PR.

Measured instance (cycle `cyc20260913-203027`, while reviewing #1190)
--------------------------------------------------------------------
#1190's head sat one commit behind master: #1189 had rewritten
`scripts/classify-conflict.py` after #1190's branch point.

    git diff master 967a57a            -> 5 paths
    landing (merge-tree 9b755ec 967a57a = a290a46c63e8) -> 2 paths

The other three - `scripts/classify-conflict.py`, `tests/test_classify_conflict.py`
and `Agent.md` - are #1189's own additions, and the two-tree diff prints them as
276 removed lines: it reads as "this PR reverts the mid-line conflict classifier".
It does not. In the landed tree those three blobs are byte-identical to master's,
which is the fact that decided the review, and it is printed by this tool instead
of having to be reconstructed by hand.

Frequency, measured in this clone over the 24 PR-head refs still present
(`refs/drain/*`, `refs/cdrain/*`, `refs/tmp/*`): **24 of 24** read backwards on at
least one path, 1001 paths in total. Those heads are old, so the count overstates
a typical queue - but a head at 2/3 routinely waits while other PRs land, and the
condition is simply "the base moved", which is the normal state of a queued PR.

Why the landing tree and not a model
------------------------------------
The landing tree is `git merge-tree --write-tree <base> <head>` - the same fold the
sibling tools use - materialised as a commit (identity and date pinned, for the
reason `check-merge-plan-suite.py` records) so that both diffs are
commit-to-commit. Nothing is inferred from the two sides: the printed change is
`git diff` of a tree the merge actually produces.

Why the reading is compared per path, and not per path *name*
-------------------------------------------------------------
Comparing the two path *lists* is not enough, and the gap is one level below where
this tool used to look (measured `cyc20260913-215412` on a fixture with both arms):

    the PR changes line 1 of src/app.py; master changes line 30 of the same file
    merge-tree <base> <head>            -> clean, one path, no conflict
    diff(base, head)      lists M src/app.py    (two hunks: line 1 and line 30 back)
    diff(base, landing)   lists M src/app.py    (one hunk: line 1)
    the old rule                        -> backwards == []  => "clean"

The path is in *both* lists, so the name-set rule sees nothing wrong, while
`diff(base, head)` for it still prints master's own later hunk as a deletion - the
reading this tool exists to prevent, inside a single file. So every shared path is
compared by its *reading*: `diff(base, head, P)` against `diff(base, landing, P)`.
When they differ, the base's own hunks on P are in the reading and not in the
landing, and P is named as reading backwards - inside the path rather than in place
of it.

Reach of the shape, honestly: the fixture proves it, and the condition for it (a head
behind the base which also touched one of the head's files) is the ordinary state of a
queued PR - but in this clone's live population no *unflagged* instance was found
(measured over the head refs against the master tips they were behind: the shared-path
cases there conflict outright, or are caught by the name-set rule already). It is an
arm that fires on a shape the two-list rule cannot see, not a repair of an observed
false "clean".

The path names here are the real ones
-------------------------------------
Both halves of the measurement need the *name git means*, so the path list is read
with `-z` rather than from the default line-per-path spelling, which quotes a path
holding a quote, a backslash, a control byte, or any non-ASCII byte. The quoted
form is a pathspec matching nothing: measured (`cyc20260914-082347`) on this file's
own fixture with the file named `中文.txt`, the tool answered *clean* where the
same repo names `src/app.py` as reading backwards inside, and printed a name no
resolver can open. See `_changed_paths`.

Exit codes
----------
    0  nothing reads backwards: every path `diff(base, head)` lists is both a path the
       landing changes *and* one whose reading there is the landing
    1  at least one path reads backwards (either the landing does not change it at all,
       or the reading inside it is not the landing), and it is named. This is a
       *reading* hazard, not a defect in the PR and not a blocker - the change the PR
       really lands is printed above it
    2  the question could not be answered (git/gh failure): fail loud, never report
       health that was not measured
    3  the merge conflicts, so there is no landing tree to diff. Not a verdict:
       that question belongs to `check-merge-sequence.py`
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# The shared module's directory, so `import merge_tree` works however this file is
# loaded: as `python3 scripts/check-merge-landing-diff.py` it is already `sys.path[0]`,
# but the test suite loads these tools by file path (`spec_from_file_location`), where
# it is not. `merge_tree.py` is a module of this repo, not a dependency.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_tree  # noqa: E402  (needs the path above)

# Pinned, not read from the clock: a commit's sha contains its committer date, so an
# unpinned synthetic commit is not a function of its inputs. The constant is
# `merge_tree.PLAN_COMMIT_DATE` - one place, one instant, for every synthetic fold in
# this family. This file used to declare its own copy, and
# `tests/test_synthetic_fold_date.py` existed to check the copies agreed: one constant
# needs no agreement guard, only a guard against a second declaration
# (`tests/test_merge_tree_is_the_only_reading.py`).
PLAN_COMMIT_DATE = merge_tree.PLAN_COMMIT_DATE

# The question could not be answered. Never a verdict. The shared module's own class,
# aliased rather than re-declared: a second class of the same name would let the
# module's refusals travel past `except MeasurementError` here and reach the caller as
# a traceback instead of as the measurement error it is.
MeasurementError = merge_tree.MeasurementError


class Conflict(MeasurementError):
    """The merge conflicts, so there is no landing tree - not a verdict either.

    A subclass rather than a message test: "no landing tree" has its own exit code
    (3) and its own owner (`check-merge-sequence.py`), and a caller that had to
    pattern-match prose to tell it from a measurement failure is how the two get
    conflated.
    """


def _run(
    argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command with the decoding pinned.

    `encoding`/`errors` are pinned for the reason recorded in `check-doc-count.py`:
    a locale mismatch leaves `stdout` as `None` after the reader thread swallows
    the decode error, and that `None` surfaces later as a bare `TypeError` past
    every handler.
    """
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _rev_parse(ref: str) -> str:
    """Resolve a ref to a commit SHA.

    Every ref that reaches `merge-tree` goes through here first: a *name* is
    mutable in a way a SHA is not (`fetch` rewrites `FETCH_HEAD`, and a local
    branch of the same name shadows `origin/master`), and this family has already
    answered about the wrong tree because of it.

    A name is also *ambiguous* in a way a SHA is not, so `_qualify_ref` runs first: the
    commit returned is the one the caller's name denotes, not the one git's precedence
    rules would pick. Without it, `_refresh_base` writes `refs/remotes/origin/master`
    and this function then measures a stray `refs/heads/origin/master` instead - the
    refresh appears to have no effect (measured, `cyc20260913-212500`).
    """
    proc = _run(["git", "rev-parse", "--verify", f"{_qualify_ref(ref)}^{{commit}}"])
    if proc.returncode != 0:
        raise MeasurementError(
            f"could not resolve {ref!r} to a commit: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _ref_exists(name: str) -> bool:
    """Is `name` a ref, given it fully?"""
    return _run(["git", "show-ref", "--verify", "--quiet", name]).returncode == 0


def _qualify_ref(ref: str) -> str:
    """The full name a short ref resolves to, so the header names what was measured.

    A header that reports `origin/master` for a commit that is not master is how the
    wrong-tree defect stays invisible: the spelling typed and the ref measured differ
    whenever a short name is ambiguous (a local `origin/master` branch shadows
    `refs/remotes/origin/master`). A SHA has no symbolic name, so it is returned as is.

    Naming the ref correctly is only half of that defect: the other half is *when*
    the name was last read, which is `_refresh_base`.

    **The reporting is also not the resolving.** `rev-parse --symbolic-full-name` on the
    ambiguous spelling prints *nothing* and exits 0, so the first version returned the
    typed name in exactly the case this docstring is about - measured
    (`cyc20260913-212500`) in a clone with a stray `refs/heads/origin/master` at
    `a2ac6f98` while the remote-tracking ref was `0998ed95`:

        rev-parse origin/master                 -> a2ac6f98   (git precedence: local wins)
        rev-parse --symbolic-full-name origin/master -> (empty, rc 0) -> header prints "origin/master"

    So the remote-tracking ref is now looked up **by its full name**, where precedence
    does not apply - the sibling `check-merge-sequence.py` records the reasoning and the
    original measurement (`cyc20260913-072845`, where a stray branch made it answer
    about a two-cycle-old tree). A short name that denotes only a local branch is
    refused rather than measured: it is not the remote branch, whatever it is called.
    The shadow case *warns* instead of failing, because the qualified lookup makes the
    answer right either way, and the warning is not cosmetic - the same stray branch
    silently misleads every other short-name reader, `git checkout origin/master`
    included.
    """
    if not ref.startswith("origin/") or ref.count("/") != 1:
        proc = _run(["git", "rev-parse", "--symbolic-full-name", ref])
        name = proc.stdout.strip()
        return name if proc.returncode == 0 and name else ref
    qualified = f"refs/remotes/{ref}"
    if _ref_exists(qualified):
        if _ref_exists(f"refs/heads/{ref}"):
            print(
                f"warning: {ref} is ambiguous - a local branch shadows it; "
                f"measuring {qualified}. Delete the shadow: git branch -D {ref}",
                file=sys.stderr,
            )
        return qualified
    if _ref_exists(f"refs/heads/{ref}"):
        raise MeasurementError(
            f"{ref!r} is ambiguous and denotes only the local branch "
            f"refs/heads/{ref}: no {qualified} exists"
        )
    return ref


def _refresh_base(base: str) -> None:
    """Bring the base up to date when it names a remote-tracking branch.

    Every PR head is fetched from the network, so this tool always answers about the
    heads as they are *now*. The base was not, which makes the two halves of one
    question come from different points in time - a gap inherited from the sibling
    this tool was built beside, caught in review of this PR (`cyc20260913-210255`):

        `refs/remotes/origin/master` moved back one commit (947377b, master 2f9c552):
            this tool:  base 947377b3 (refs/remotes/origin/master), 1 PR(s) checked  rc 0
            sibling:    base 2f9c5524 (refs/remotes/origin/master)                    # refreshed

    The verdict is not freshness-neutral either: over the 26 head refs in that clone, 2
    changed state between the stale base and the true one - and one of them was the
    live PR under review, reading `clean`/exit 0 against the stale base and
    `backwards`/exit 1 against master. Answering a different question because a local
    ref is behind is the same wrong-tree defect the header guards against, one level
    down; the reasoning is recorded in full in `check-merge-sequence.py`'s
    `_refresh_base`.

    A **remote-tracking** ref is refreshed, in either spelling the caller may write it:
    `origin/<branch>` and `refs/remotes/origin/<branch>` denote the same mutable ref, and
    the fully-qualified one is not exotic - `check-merge-pairs.py::_resolve_base` passes
    `refs/...` through untouched and its refusal text tells callers to "pass the
    fully-qualified ref you mean", so the family advertises it. Accepting only the short
    spelling meant that one answered from whatever the ref happened to be, with rc 0 and
    the stale commit in the header - the wrong-tree shape one level below the one
    `_qualify_ref` fixes (measured `cyc20260913-221656`, in a clone whose
    `refs/remotes/origin/master` sat one commit behind: `--base origin/master` refreshed
    to the true base and reported the reversal, `--base refs/remotes/origin/master` read
    the stale commit and reported `clean`, rc 0).

    Anything else is taken literally: a SHA is immutable by construction, a local branch
    is not the remote ref whatever it is called, and a refspec the caller already wrote
    (`origin/x:dest`) is passed to git as given. The destination is written **fully
    qualified**, because a bare `origin/master` as a fetch destination makes git create a
    *local branch* of that name (`refs/heads/origin/master`), which then shadows the
    remote-tracking ref and makes every later `origin/master` ambiguous - the trap the
    sibling measured.

    A `<remote>/HEAD` spelling is resolved through its symref first: it names a remote
    branch only by pointing at one, and a fetch *into* a symref cannot be locked (git
    refuses and leaves the symref unchanged, measured). Refreshing the *target* is what
    makes `--base origin/HEAD` mean "origin's default branch as it is now" - and it is
    the natural spelling for a checkout whose default branch is not `master`, which used
    to fail with a fetch of the non-existent `refs/heads/HEAD`. Under `refs/remotes/` the
    only symbolic ref git creates is `<remote>/HEAD`, so the probe is confined to that
    name and an ordinary branch spelling still costs exactly one call.

    Which ref it *is* is decided by git, not by that name: `<branch>/HEAD` is a legal
    branch name (`git check-ref-format --branch feature/HEAD` accepts it), so
    `refs/remotes/origin/feature/HEAD` is an ordinary remote-tracking branch that merely
    ends in `/HEAD`. Keying the *refusal* on the suffix refused a base that needed no
    resolving at all - and it was a regression, since the spelling was fetched correctly
    before the `/HEAD` handling existed. Measured in a hermetic clone holding a
    `feature/HEAD` branch one commit ahead of its tracking ref (`cyc20260913-225642`):

        --base origin/feature/HEAD               -> MeasurementError, ref left stale
        --base refs/remotes/origin/feature/HEAD  -> MeasurementError, ref left stale

    A name git does not report as symbolic is therefore fetched like any other branch; a
    name that resolves to something outside `origin`'s tracking refs is still a
    measurement error.

    A fetch failure, or a `<remote>/HEAD` spelling whose symref leads outside `origin`'s
    tracking refs, is a measurement error, never a quiet continuation against a base that
    could not be verified.
    """
    if ":" in base:
        return
    if base.startswith("origin/"):
        dest = f"refs/remotes/origin/{base[len('origin/'):]}"
    elif base.startswith("refs/remotes/origin/"):
        dest = base
    else:
        return
    if dest.endswith("/HEAD"):
        link = _run(["git", "symbolic-ref", "--quiet", dest])
        if link.returncode == 0:
            target = link.stdout.strip()
            if not target.startswith("refs/remotes/origin/"):
                raise MeasurementError(
                    f"could not refresh {base}: {dest} is a symbolic ref to "
                    f"{target}, which is not a remote-tracking branch of origin"
                )
            dest = target
    branch = dest[len("refs/remotes/origin/"):]
    proc = _run(["git", "fetch", "--quiet", "origin", f"+refs/heads/{branch}:{dest}"])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise MeasurementError(f"could not refresh {base}: {detail}")


def _open_pr_numbers(repo: str) -> list[int]:
    """The open PR numbers, ascending - the default subject of the check."""
    proc = _run(
        [
            "gh",
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
            "--jq",
            ".[].number",
        ]
    )
    if proc.returncode != 0:
        raise MeasurementError(f"gh pr list failed: {proc.stderr.strip()}")
    numbers = [int(line) for line in proc.stdout.split() if line.strip()]
    if not numbers:
        raise MeasurementError("no open PRs reported - nothing to check")
    return sorted(numbers)


def _fetch_head(number: int) -> str:
    """Fetch a PR's real head into a temp ref and return its SHA.

    The refspec is forced (`+`): PR heads here are routinely re-pushed to a commit
    that is not a descendant of the previous one (every conflict resolution does),
    and a rejected fetch would leave the *stale* ref in place, so the reading would
    be about a head that is no longer the PR.
    """
    ref = f"refs/emrg-landing-diff/pr{number}"
    proc = _run(["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise MeasurementError(f"could not fetch PR #{number}: {detail}")
    return _rev_parse(ref)
def _diagnosis(proc: subprocess.CompletedProcess[str]) -> str:
    """What git said, from both streams - a failure must not report itself as empty."""
    detail = (proc.stdout[-500:] + proc.stderr[-500:]).strip()
    return detail or f"no output (exit {proc.returncode})"


def _merge_tree(base: str, head: str) -> str | None:
    """The tree landing `head` on `base` produces, or None when it conflicts.

    The same measurement every gate in this family asks: **the exit code is not the
    signal, the named tree is** - a genuine conflict exits 1 *and* names the merged
    tree on the first line, while a failure to merge the two *inputs* exits 1 with
    empty stdout, and reading only the code would report "conflict" for an unanswered
    question. `merge_tree.fold` is where that rule lives; what stays here is this
    tool's refusal to proceed without a tree.
    """
    answer = merge_tree.fold(base, head, run=_run)
    if not answer.answered:
        raise MeasurementError(
            "merge-tree did not name a merged tree: " + answer.diagnosis
        )
    return None if answer.verdict == "conflict" else answer.tree


# Author/committer for the synthetic landing commit, independent of git config - the
# whole family's, from `merge_tree.commit_env`. Measured in this family
# (`cyc20260913-200715`): with no ambient identity and `user.useConfigOnly = true`,
# `git commit-tree` refuses and the tool reports a *false* "could not measure" about a
# question the machine's git config has no bearing on.
_commit_env = merge_tree.commit_env


def _commit_tree(tree: str, parents: list[str], message: str) -> str:
    """Create a commit for a merged tree, so both diffs are commit-to-commit."""
    return merge_tree.commit_tree(
        tree, parents, message, run=_run, env=_commit_env()
    )


def _changed_paths(a: str, b: str) -> list[tuple[str, str]]:
    """`git diff --name-status -z a b`, as (status, path) pairs, in git's order.

    `-z` because the paths have to be **the real ones**, and both halves of this
    tool need that: the report prints them, and `_path_reading` hands them back to
    git as a pathspec.

    Without `-z` git *quotes* a path that holds a quote, a backslash, a control
    byte, or - with the default `core.quotePath=true` - any non-ASCII byte:
    `中文.txt` arrives as the text `"\\344\\270\\255\\346\\226\\207.txt"` and
    `f<TAB>tab.txt` as `"f\\ttab.txt"` (measured 2026-09-14, `cyc20260914-082347`,
    git 2.50.1, in a scratch repo, both spellings). Such a quoted path is not only
    unreadable in the report - it is a pathspec that matches **nothing**, so
    `_path_reading` answered "" for both sides of a shared path and the
    "reads backwards inside" arm went blind: measured on the fixture this file's
    tests use, the same repo reported the hazard for `src/app.py` and reported
    *clean* for `中文.txt`, whose reading does print the base's later hunk as a
    deletion. A false "clean" is the one answer this tool may not give.

    The siblings read merge-tree's *report*, where there is no `-z` spelling, so
    they decode git's quoting instead (`merge_tree.unquote_path`, the one reading
    every `check-merge-*.py` now asks for). Here the question can be put to git
    directly: `-z` separates the fields with NUL and quotes nothing, so the bytes
    arrive as they are and can be used as a pathspec unchanged.
    """
    proc = _run(["git", "diff", "--name-status", "--no-renames", "-z", a, b])
    if proc.returncode != 0:
        raise MeasurementError(
            f"could not diff {a[:8]}..{b[:8]}: {_diagnosis(proc)}"
        )
    # `-z` writes NUL after every field, and the status is a field of its own, so
    # the payload is `status\0path\0status\0path\0...` (and "no changes" is empty).
    fields = proc.stdout.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        # A shape this tool does not know is a measurement failure, never a short
        # list: silently dropping the odd field would answer about a change that
        # was not read.
        raise MeasurementError(
            f"diff {a[:8]}..{b[:8]} named {len(fields)} field(s), not status/path "
            f"pairs: {proc.stdout!r}"
        )
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields), 2)]


def _behind_by(base: str, head: str) -> int:
    """How many commits of `base` the head does not contain - the reason for all this."""
    proc = _run(["git", "rev-list", "--count", f"{head}..{base}"])
    if proc.returncode != 0:
        raise MeasurementError(
            f"could not count {head[:8]}..{base[:8]}: {_diagnosis(proc)}"
        )
    try:
        return int(proc.stdout.strip() or "0")
    except ValueError as exc:
        raise MeasurementError(
            f"rev-list --count said {proc.stdout.strip()!r}"
        ) from exc


def _path_reading(a: str, b: str, path: str) -> str:
    """The diff text for one path between two commits - the *reading* of that path.

    Two readings of a path are the same change exactly when their text is the same:
    a path diff is a function of the two blobs (and their modes), nothing else. So
    comparing text is the definition, not a proxy for it - and it stays readable
    about what was compared.
    """
    proc = _run(["git", "diff", "--no-renames", a, b, "--", path])
    if proc.returncode != 0:
        raise MeasurementError(
            f"could not diff {a[:8]}..{b[:8]} for {path}: {_diagnosis(proc)}"
        )
    return proc.stdout


def landing_reading(base: str, head: str) -> tuple[
    str,
    list[tuple[str, str]],
    list[tuple[str, str]],
    list[tuple[str, str]],
    list[tuple[str, str]],
]:
    """The measurement, without the report.

    Returns `(landing_tree, landed, apparent, backwards, reversed_inside)`:

    * `landed`    - what merging the head on the base changes (the reviewable change)
    * `apparent`  - what `diff(base, head)` lists (the reading to distrust)
    * `backwards` - paths in `apparent` the landing does not change at all, i.e. the
      base's own later commits, shown there as reversals this PR does not make
    * `reversed_inside` - paths in `apparent` the landing *does* change, whose reading
      is nevertheless not the landing: the base's own later hunks on them appear in
      `diff(base, head)` as deletions this PR does not make (measured, docstring)

    A path list is not enough for the second shape: the path name is in both lists,
    which is why the comparison for a shared path is the *reading* of that path.

    Raises `Conflict` when the merge conflicts (no landing tree to diff) and
    `MeasurementError` when git failed: neither is a reading, and neither is a
    verdict.
    """
    # Resolved here, not only by the caller: a *name* is mutable (`fetch` rewrites
    # `FETCH_HEAD`; a local branch shadows `origin/master`), so no entry point is
    # allowed to hand one to `merge-tree`.
    base = _rev_parse(base)
    head = _rev_parse(head)
    tree = _merge_tree(base, head)
    if tree is None:
        raise Conflict(
            "the merge conflicts, so there is no landing tree to diff "
            "(that question belongs to check-merge-sequence.py)"
        )
    landing = _commit_tree(tree, [base, head], f"landing {head[:8]} on {base[:8]}")
    landed = _changed_paths(base, landing)
    apparent = _changed_paths(base, head)
    landed_paths = {path for _, path in landed}
    backwards: list[tuple[str, str]] = []
    reversed_inside: list[tuple[str, str]] = []
    for status, path in apparent:
        if path not in landed_paths:
            backwards.append((status, path))
        elif _path_reading(base, head, path) != _path_reading(base, landing, path):
            reversed_inside.append((status, path))
    return tree, landed, apparent, backwards, reversed_inside



def check_pr(number: int, base: str) -> tuple[str, str]:
    """One PR's landing change, plus the readings of `diff(base, head)` that are not it.

    Returns `(state, report)` with state in `{"clean", "backwards", "conflict"}`.
    """
    head = _fetch_head(number)
    try:
        tree, landed, apparent, backwards, reversed_inside = landing_reading(base, head)
    except Conflict as exc:
        return "conflict", f"  #{number}: {exc}"
    behind = _behind_by(base, head)

    lines = [
        f"  #{number} landing tree {tree[:12]} - merging it changes {len(landed)} "
        f"path(s) on the base:"
    ]
    lines += [f"    {status}\t{path}" for status, path in landed]
    if not landed:
        lines.append("    (nothing: this head adds no change to the base)")
    if backwards or reversed_inside:
        if backwards:
            lines.append(
                f"  reads backwards: {len(backwards)} of the {len(apparent)} path(s) in "
                f"diff(base, head) are the base's own later changes ({behind} commit(s) "
                f"the head does not contain), shown there as reversals this PR does not make:"
            )
            lines += [f"    {status}\t{path}" for status, path in backwards]
        if reversed_inside:
            lines.append(
                f"  reads backwards inside: {len(reversed_inside)} of the "
                f"{len(apparent)} path(s) in diff(base, head) are landed, but the reading "
                f"there is not the landing - base's own later hunks on them appear as "
                f"deletions this PR does not make:"
            )
            lines += [f"    {status}\t{path}" for status, path in reversed_inside]
        return "backwards", "\n".join(lines)
    lines.append(
        f"  diff(base, head) lists {len(apparent)} path(s), and every one of them is "
        f"landed ({behind} commit(s) of the base not in the head)"
    )
    return "clean", "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Show the change merging a PR lands, next to the diff-to-master reading "
            "that is not that change."
        )
    )
    parser.add_argument(
        "prs", nargs="*", type=int, help="PR numbers (default: all open, ascending)"
    )
    parser.add_argument("--repo", default="argszero/emrg", help="owner/name")
    parser.add_argument("--base", default="origin/master", help="the ref to land on")
    args = parser.parse_args(argv)

    try:
        # Refreshed before it is resolved, and before any head is fetched: a
        # remote-tracking base read from a checkout that has not fetched answers about
        # a base nobody asked for, and this tool's verdict depends on which base it is
        # (measured, `_refresh_base`).
        _refresh_base(args.base)
        base = _rev_parse(args.base)
        numbers = args.prs or _open_pr_numbers(args.repo)
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    print(f"base {base[:8]} ({_qualify_ref(args.base)}), {len(numbers)} PR(s) checked")
    states: list[tuple[int, str]] = []
    for number in numbers:
        try:
            state, report = check_pr(number, base)
        except MeasurementError as exc:
            print(f"  #{number}: could not measure: {exc}", file=sys.stderr)
            return 2
        print(report)
        states.append((number, state))

    conflicts = [number for number, state in states if state == "conflict"]
    backwards = [number for number, state in states if state == "backwards"]
    if conflicts:
        print(
            "\nNo landing tree for "
            + ", ".join(f"#{number}" for number in conflicts)
            + ": the merge conflicts, so this tool has nothing to diff. That is not a "
            "health verdict - the sequence question is check-merge-sequence.py's.",
            file=sys.stderr,
        )
        return 3
    if backwards:
        print(
            "\n"
            + ", ".join(f"#{number}" for number in backwards)
            + " reads backwards: reading diff(master, head) as the change this PR lands "
            "would review work the PR never touched. The landing change is printed above "
            "it and is what merges. This is a reading hazard, not a defect in the PR.",
            file=sys.stderr,
        )
        return 1
    print("\nno path reads backwards: diff(master, head) is the change this lands")
    return 0


if __name__ == "__main__":
    sys.exit(main())
