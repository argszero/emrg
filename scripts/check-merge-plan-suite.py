#!/usr/bin/env python3
"""Run the repository's own test suite on the tree a *plan* would land.

The question this exists for
----------------------------
The other gates in this family answer a question about *one* PR, or about *one*
merge: are the votes current, can the base reach master, is the CI verdict fresh,
which other PRs would this dirty, does merging this PR alone produce a tree the
guards accept. None of them answers the question that decides whether master is
healthy a minute after a *plan* lands: **does the tree those PRs produce together
pass the repository's own tests?**

Per-PR CI cannot answer it either, and not by accident: a `pull_request` run
builds `Merge <head> into <merge-base>`, so a PR's CI contains master and that PR
and nothing else. A rule that arrives in one PR and the code it rejects in
another are invisible to every per-PR signal - each side is green, and the tree
that reaches master is red.

Measured instance (cycle cyc20260913-154837)
--------------------------------------------
Seven open PRs, each `MERGEABLE`, each CI double-green. Every ordered pair merged
cleanly (`git merge-tree --write-tree --merge-base=...`: 21/21 clean), and the
doc-count guard accepted every step of the resulting plan. The tree the seven
produce together, however, failed the suite:

    1 failed, 1738 passed
    FAILED tests/test_script_decode_is_locale_independent.py::test_every_text_mode_subprocess_pins_its_encoding
       tests/test_check_merge_sequence.py:318 subprocess.run(..., text=True) has no encoding= ...

The guard shipped in #1136 rejects three calls shipped in #1172. Neither PR's CI
can see the other's files. That is why this check takes *several* PRs, judges the
tree they produce together, and runs the suite rather than one guard script: one
guard script is exactly what was green while the tree was red.

Why a worktree and not `git archive`
------------------------------------
The tree is materialised with `git worktree add --detach`, never extracted from an
archive. Measured: an archive has no `.git`, this repo's tests resolve paths
through git, and an archive harness therefore reported 8 failures on *master's own
tree* as well - identical on every input, a device measuring itself instead of the
plan. A real worktree distinguishes the two states (control 1600 passed, planned
tree 1643 passed, same harness).

Why the interpreter that is running this script
-----------------------------------------------
A freshly added worktree has no populated `.venv` - `uv run` inside one creates an
empty environment, measured repeatedly in this repo - so the suite is run with
`sys.executable`, i.e. the interpreter running this tool (under
`uv run --no-sync`, the project environment), with the worktree as cwd.

The base is fetched, then named
-------------------------------
A plan is built *onto* a base, and this tool fetched every PR head and no base, so
the two halves of one answer came from different points in time and from different
refs. Measured hermetic (`cyc20260914-002731`: a bare `origin`, real git, no
network) on a clone whose `refs/remotes/origin/master` was left at the older commit
while the bare origin held the true master, and, in a second arm family, with a
stray local branch `refs/heads/origin/master` present to shadow it:

    PRE   --base origin/master  base ec7ce11a (origin/master)     tree 5427c8ecb011
    POST  --base origin/master  base 3fbd101d (refs/remotes/...)  tree 43752830eb32
    PRE   (shadow present)      base 9c8b3410 (origin/master)     tree 088f2299677e

Three different trees were judged for one question, and the header named
`origin/master` in all three cases - the wrong-tree defect this family exists to
remove, one level up from the per-PR gates. So the base is **fetched first**, and
then **taken by full name**: `git rev-parse` consults `refs/heads/<name>` before
`refs/remotes/<name>`, so one stray local branch of that name replaces the remote
ref, while `refs/remotes/origin/<name>` means exactly one ref. Both halves are
asked of `check-merge-sequence.py`'s `_refresh_base`/`_qualify_ref` rather than
reimplemented here, so the rule cannot drift between the gates that ask about it.
A SHA, a local branch name or a written refspec is taken literally; a
remote-tracking name that cannot be fetched, or one that denotes only a local
branch, is a measurement error (exit 2) rather than a base nobody verified.

The conflicted paths are read from the stage block
--------------------------------------------------
Exit code 3 tells the caller to resolve a conflict, so the *names* in that refusal
are what the caller acts on: a path that is not the conflicted one sends the
resolution to a file that does not collide. Measured (2026-09-14,
`cyc20260914-062927`, git 2.50.1) the names used to come from "the text after the
first tab on any line of the report", and two shapes break that:

* the report continues past the stage block with prose that embeds the path
  (`Auto-merging f<TAB>tab.txt`), and a path containing a tab makes those prose
  lines tab-separated too - `tab.txt` came back as a conflicted path, a file that
  collides with nothing and does not exist;
* git quotes a path holding a non-ASCII byte (the default `core.quotePath=true`),
  so `中文.txt` arrives as `"\\344\\270\\255\\346\\226\\207.txt"` - the caller is
  told to resolve a name that is not the file's.

So the paths come from the stage block (`<mode> <blob> <stage>\\t<path>` lines up
to the blank line git writes after it) and the quoted spelling is decoded.

Usage
-----
    uv run --no-sync python3 scripts/check-merge-plan-suite.py 1136 1152 1185
    uv run --no-sync python3 scripts/check-merge-plan-suite.py      # all open, ascending
    uv run --no-sync python3 scripts/check-merge-plan-suite.py --steps 1187 1188

`--steps` judges every intermediate tree instead of only the final one, which is
the other half of the same question and the open half of issue #1161: a plan whose
last PR fixes what an earlier PR broke is green at the end and red on the way, and
each of those in-between trees is master's tree for a while when the plan is landed
one PR at a time. It costs one suite run per step, hence opt-in.

Exit codes
----------
    0  the plan's final tree was built and its suite passed (with `--steps`: every
       step's tree passed)
    1  the plan's final tree was built and its suite FAILED - the finding (with
       `--steps`: at least one step's tree failed, and the step is named)
    2  the question could not be answered (git/gh failure, a base that names only
       a local branch or cannot be fetched, or the suite could not be run at all):
       fail loud, never report health that was not measured
    3  the plan has no final tree - a step conflicts. Not a health verdict: there
       is no tree to judge. That question belongs to `check-merge-sequence.py`.

The plan and the tree that was measured are named in the output. "Which tree
answered?" is the defect this family exists to remove.

The tree answers, and its sources are the only copy that answers
---------------------------------------------------------------
A worktree run must be an answer about the tree under test, so the run is pinned in
the two ways a second copy of that tree can creep in - both measured, not assumed:

* *another tree on `sys.path`.* This machine's environment exports
  `PYTHONPATH=/Users/argszero/.emrg/install/source:...`, i.e. a second, installed
  copy of this package. Whichever copy `sys.path` resolves first is the one that
  answers, and a suite green against the installed copy says nothing about the
  tree that would land. The worktree is therefore **prepended** to `PYTHONPATH` for
  the run, so the tree under test wins every name it defines.
* *a bytecode cache inside the tree.* A `.pyc` is a second copy of a source, and
  CPython prefers it whenever the header's `(int(mtime), size)` pair still matches
  the source - a cache written by an earlier revision can therefore answer for the
  revision under test. Measured (`cyc20260914-085416`): a *kept* worktree of
  #1211's landing tree reported `PROJECT_CONTEXT_MAX_CHARS` as `7000` while its own
  tracked `emrg/server/daemon.py` said `8000`; deleting that worktree's
  `__pycache__` flipped the suite from failing to passing. Intermittent, silent, and
  a verdict about a copy: the caches under the worktree are removed before the run,
  and `PYTHONDONTWRITEBYTECODE=1` is set for it (and for anything it spawns) so the
  measurement leaves none behind.

Which rows the run blames, and why it is asked twice
----------------------------------------------------
A red tree is attributed *by row*: the plan's own failing rows are re-run on the base, and
a row the base fails too belongs to no PR (issue #1378). Those rows are therefore what the
base tree is asked, and reading them out of the text report is lossy, because pytest puts
the id and the failure's message on one line and separates them with `" - "` - a substring
that node ids themselves contain. Measured on this repository's own suite (2026-09-18,
`cyc20260918-212523`): `pytest tests/ --collect-only -q` listed 3227 node ids and **22 of
them contain `" - "`**, all of them in two files, e.g.

    tests/test_stdin_message_readers.py::test_a_message_readers_body_is_not_scanned[git commit -q -F - <<'EOF']

whose text form is cut to `…[git commit -q -F` (issue #1386). The cut row is one the base
tree does not contain, and a row a tree does not contain cannot fail there - so a row the
base fails too is laid at a PR's door with "re-push the PR that owns the failure", which is
precisely the harm #1378 removed, restored for those rows.

So the row list comes from the run's own **machine-readable** report: the suite is invoked
with `--junitxml` and `-o junit_family=xunit1` (the family that writes `file`, without
which a `classname` cannot be turned back into a path), and every `<testcase>` carrying a
`<failure>` or `<error>` becomes a node id (`_row_id`). Two shapes are pinned by tests,
both measured against this machine's pytest (9.1.1):

* an id containing `" - "` - carried whole in the junit `name` attribute;
* an id containing a **newline** (produced only with
  `disable_test_id_escaping_and_forfeit_all_rights_to_community_support = true`) - the text
  report breaks the line and the id is unrecoverable from it, while junit escapes it as
  `&#10;` and the parser returns it intact.

A report that cannot be parsed, or a failing record whose node id cannot be built, is exit
2's "could not measure" rather than a guessed row. A run that writes no report at all -
`SUITE` replaced by a caller - falls back to the text parse and says so on stderr: that is
the one path where the weakness above is still reachable, and it is named rather than
silent.

The same class of loss is in the other text channel this tool reads, the base run's
`not found:` report, which says which rows the base does not contain - and a row a tree does
not contain cannot fail there, so a row missed here is a row the base is never asked about.
Measured (`cyc20260918-212523`): pytest prints the argument as it was given, node id and
all, and reading it up to the first whitespace returned `set()` for a missing
`…::test_dashed_id[git commit -q -F - <<EOF]` - every row parametrized with a space, which
is how the 22 come back after the junit report fixed the other end of the same walk. The
argument is now read to the end of its line; a row containing a *newline* is still beyond
a line-anchored reader, and there the tool answers "could not measure" instead of guessing.

What a worktree run is not
--------------------------
The suite runs in a worktree of the tree under test, so it runs the suite a *fresh
clone* of that tree would run: the tree's **tracked** files and nothing else. One
test is environment-dependent, and it is skipped here but passes in a developer's
checkout - `tests/test_check_node_test_count.py` skips itself with "no node_modules
... cannot ask the runners", since `node_modules/` is untracked. Measured
(`cyc20260913-203027`, master `947377b`): a worktree of that tree reported
`1761 passed, 2 skipped` while the same tree in a populated checkout reported
`1771 passed, 1 skipped` after nine new tests - the two extra numbers are this skip
and those tests, not a difference in the trees. Compare worktree runs with worktree
runs, and never read a skip/pass delta between the two harnesses as a regression.

Keeping the measured tree for further checks (`--keep DIR`)
----------------------------------------------------------
Reviewing a PR properly means running *your own* probe on the tree this tool measured,
and that tree disappears when the run ends. Rebuilding the merge by hand is how a
verdict about a different tree gets published: the checkout has to be re-derived
(`git merge --no-commit` on a fetched `refs/pull/<N>/head`) and its `git write-tree`
compared with the sha printed here, or the arms describe something else. `--keep DIR`
materialises the worktree at `DIR`, still purges its bytecode caches, still pins the run
to that tree, and leaves it in place - the line it prints names the path, the tree it
answers for, and how to remove it. The run itself is unchanged, so a kept worktree
answers for the same tree the default run would have deleted.

That line also names the two things true of every fresh worktree, because both have
already been reported as defects (one measured in a hand-built landing-tree worktree,
and both re-measured on a real landing tree the next cycle): it has **no `.venv`**, so
`uv run pytest` there reports that no suite ran, and it has **no `node_modules`**, so the
GUI Node suite fails two files - the spawn-args test (`python=python3 (expected
.venv/bin/python)`) and `test/integration.test.js` (`Cannot find module 'ws'`), because
the GUI's dependencies (`ws` among them) live in `emrg/gui/node_modules`, not in the
repository root's. The node link therefore names the **GUI's own** directory: it was
first written as the repository root's, which is empty, so the line that was supposed to
fix the suite fixed nothing (measured 2026-09-19, `cyc20260919-060712`: on landing tree
`f96d6515c734`, 125 passed / 2 failed with no link, 126 / 1 with the python link alone,
and 126 / 0 / 8 skipped with both - identical with the root link, because it links an
empty directory). Neither failure is a verdict on the tree, so the note prints the
two commands that fix it (the main checkout's interpreter with `PYTHONPATH` pointed at
the worktree; the main checkout's `emrg/gui/node_modules` and `.venv` linked in), and says
to compare worktree runs with worktree runs.

What the run leaves behind
--------------------------
Nothing. A PR head has to be fetched into a ref before it can be folded, so each
planned PR parks one under `refs/emrg-plan-suite/` - and a ref is state: it keeps
the commit reachable, so `git gc` can never prune it. Those refs are therefore
removed when the run ends, on every path (`_drop_fetched_refs`), the same way a
worktree is: measured 2026-09-17 (`cyc20260917-142057`), leaving them in place had
grown the family to 88 refs pinning 283 commits unreachable from master, plus 29
more from three earlier tools with the same habit. A run that ends with a verdict
nobody can act on is a bad run; a run that ends having quietly moved the object
database is a worse one, because nothing about the verdict tells you it happened.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

# The shared module's directory, so `import merge_tree` works however this file is
# loaded: as `python3 scripts/check-merge-plan-suite.py` it is already `sys.path[0]`,
# but the test suite loads these tools by file path (`spec_from_file_location`), where
# it is not. `merge_tree.py` is a module of this repo, not a dependency.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_tree  # noqa: E402  (needs the path above)

TIP_REF = "refs/emrg-plan-suite/tip"
# Where a fetched PR head is parked while the plan is built. Temp by intent: the
# plan needs the *commit* `_fetch_head` resolves, never the ref, so the ref is
# removed when the run ends (`_drop_fetched_refs`). Nothing else may depend on it.
PLAN_REF_PREFIX = "refs/emrg-plan-suite/pr"
SUITE = ["-m", "pytest", "tests/", "-q", "--no-header"]

# The sibling tool that owns the base rule, loaded from its file rather than
# imported by name: the scripts in this directory are not importable modules
# (hyphenated names, no package), and this is the same loader the test suite
# already uses for them. Taken from the sibling rather than copied so the *rule*
# has one implementation - a base name must not mean different things to the
# gates that ask about it.
_SIBLING = Path(__file__).resolve().parent / "check-merge-sequence.py"


def _load_sibling():
    spec = importlib.util.spec_from_file_location("check_merge_sequence", _SIBLING)
    if spec is None or spec.loader is None:  # pragma: no cover - file is in this repo
        raise RuntimeError(f"could not load {_SIBLING}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seq = _load_sibling()


# The sibling's error class, aliased rather than declared: the base's half of the
# question is asked *of the sibling*, and a second class of the same name would let
# its refusals travel past `except MeasurementError` - a base that denotes only a
# local branch would then reach the caller as a traceback instead of as the
# measurement error it is. The first draft of this change did exactly that, and the
# test that asserts exit 2 for such a base is what caught it.
#   "The question could not be answered. Never a verdict."
MeasurementError = seq.MeasurementError


class PlanConflict(Exception):
    """A step conflicts, so the plan has no final tree. Never a verdict."""

    def __init__(self, step: int, number: int, paths: list[str]) -> None:
        super().__init__(
            f"step {step} (#{number}) conflicts on {', '.join(sorted(set(paths)))}"
        )
        self.step = step
        self.number = number
        self.paths = paths


def _run(
    argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command with the decoding pinned.

    `encoding`/`errors` are pinned for the reason recorded in
    `check-doc-count.py`: a locale mismatch leaves `stdout` as `None` after the
    reader thread swallows the decode error, and the `None` surfaces later as a
    bare `TypeError` past every handler.
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


# The date carried by every synthetic plan commit - and by every other synthetic
# fold in this family - is `merge_tree.PLAN_COMMIT_DATE`, one constant in one place.
# It is pinned rather than read from the clock because a fold must be a function of
# its inputs, and a commit's sha includes its committer date: an unpinned fold
# produced a *different* sha for the *same* plan whenever two folds straddled a
# second boundary. Measured (cyc20260913-194108, Windows CI run 34754517824 on
# #1190): the test comparing `build_plan_tip` against the last step tree failed on
# exactly that - the two folds differed and nothing was wrong with either tree. It
# also makes a `--steps` tree sha comparable between runs, which is the point of
# printing one.
#
# This file used to declare its own copy, with three siblings, and
# `tests/test_synthetic_fold_date.py` existed to check the copies agreed. One
# constant needs no such guard: `tests/test_merge_tree_is_the_only_reading.py`
# fails on a second declaration.
_commit_env = merge_tree.commit_env


def _rev_parse(ref: str) -> str:
    """Resolve a ref to a commit SHA.

    Every ref that reaches `merge-tree` goes through here first: a *name* is
    mutable in a way a SHA is not (`fetch` rewrites `FETCH_HEAD`, and a local
    branch of the same name shadows `origin/master`), and this family has already
    answered about the wrong tree because of it.
    """
    proc = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"])
    if proc.returncode != 0:
        raise MeasurementError(
            f"could not resolve {ref!r} to a commit: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _tree_id(tree_sha: str) -> str:
    """A tree's identity as this tool reports it: abbreviated to scan, complete to verify.

    The abbreviation alone cannot be *reused*. A reader who wants to check that a
    printed sha really names a tree has to ask git, and `git rev-parse <40-hex>`
    echoes any 40-hex string handed to it - a control of `deadbeef` came back
    unchanged, rc 0 - so the check is `git cat-file -t`, which needs all 40
    characters. Measured (cyc20260918-000146): the `--steps` lines printed 12, the
    cycle that needed step 2's tree could not verify the reading it already had in
    hand, and paid another whole plan run (~116s) to recover the sha it had already
    been shown. One shape in one place, because the defect is one line drifting from
    the others: the short form stays readable, the full one is what a later reader
    compares against, and a tree identity is the thing a verdict here is bound to.
    """
    return f"{tree_sha[:12]} ({tree_sha})"


def _tree_of(commit: str, what: str) -> str:
    """The tree a commit carries, named as the caller will have to describe it.

    One implementation for both trees this tool measures (the plan's and the base's),
    because the two are compared with each other: a difference in how they are resolved
    would be read as a difference between the trees.
    """
    proc = _run(["git", "rev-parse", f"{commit}^{{tree}}"])
    if proc.returncode != 0:
        raise MeasurementError(f"could not read {what}: {proc.stderr}")
    return proc.stdout.strip()


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
        raise MeasurementError("no open PRs reported - nothing to judge")
    return sorted(numbers)


def _fetch_head(number: int) -> str:
    """Fetch a PR's real head into a temp ref and return the commit it resolves to.

    The refspec is forced (`+`): PR heads here are routinely re-pushed to a commit
    that is not a descendant of the previous one (every conflict resolution does),
    and a rejected fetch would leave the *stale* ref in place, so the plan would
    silently be built from a tree that is no longer the PR.

    The ref is a temp ref, and the caller must dispose of it: the plan needs the
    commit, not the name, so `_drop_fetched_refs` removes it when the run is over.
    """
    ref = f"{PLAN_REF_PREFIX}{number}"
    proc = _run(["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise MeasurementError(f"could not fetch PR #{number}: {detail}")
    return _rev_parse(ref)


def _fetch_heads(numbers: list[int], fetched: list[int]) -> list[tuple[int, str]]:
    """Fetch every planned PR's head, recording which temp refs now need removing.

    `fetched` is appended to in fetch order rather than being derived from
    `numbers`: a run that dies on PR 3 of 5 has created two refs, and those two are
    exactly the ones that need clearing.
    """
    heads: list[tuple[int, str]] = []
    for number in numbers:
        heads.append((number, _fetch_head(number)))
        fetched.append(number)
    return heads


def _drop_fetched_refs(fetched: list[int]) -> None:
    """Delete the per-PR temp refs this run fetched - on every return path.

    Measured 2026-09-17 (`cyc20260917-142057`): `_fetch_head` left its ref behind, so
    `refs/emrg-plan-suite/` had grown to **88** refs - one per planned PR per run -
    pinning **283 commits / 1405 objects** unreachable from master, which therefore
    can never be pruned by `git gc`. Three older families (`refs/cdrain/`,
    `refs/drain/`, `refs/tmp/`; 29 refs between them) are still sitting in this
    clone from tools that no longer exist in the tree, which is what "nothing ever
    cleans this" looks like. A ref nobody reads is state, and a guard's state leak
    is the same defect class as a guard's wrong tree: the artifact outlives the
    reason it was made.

    The ref is keyed by PR number, so two runs planning the same PR share one ref.
    Removing it is safe for the other run: it has already resolved the commit it
    needs (that is what `_fetch_head` returns), and the ref is temp for it too - the
    worst case is that it deletes an already-absent ref, which is why deleting is
    best-effort rather than a `MeasurementError`. A failure to delete says nothing
    about the measurement, so it is never allowed to become one.
    """
    for number in fetched:
        _run(["git", "update-ref", "-d", f"{PLAN_REF_PREFIX}{number}"])


def _merge_tree(ours: str, theirs: str) -> tuple[str | None, list[str]]:
    """The tree of the clean merge of two commits, or the conflicted paths.

    The measurement and the reading of the conflicted paths are `merge_tree.fold`'s;
    this function is the mapping from its three answers to *this* tool's discipline,
    which raises rather than returning an unevidenced answer:

    * no named tree (rc 0 or 1) - "I could not merge these two inputs" - is a
      measurement error, never a plan conflict: measured (`git merge-tree
      --write-tree <commit> <blob>` in this repo), a genuine conflict and a failure
      to merge the inputs both exit 1, and only the first names a tree. Reading the
      code alone sends the caller off to resolve a conflict that does not exist.
    * a conflict that names no path is the same unevidenced answer one field over -
      the report would say the step "conflicts on " and name nothing - so it is
      also an error rather than a `PlanConflict` with an empty list.
    """
    answer = merge_tree.fold(ours, theirs, run=_run)
    if answer.verdict == "clean":
        return answer.tree, []
    if answer.verdict == "unmeasured":
        # The code is not the verdict (see above) but it is what tells the host
        # which way the merge failed, so it is quoted rather than flattened.
        if answer.code == 0:
            raise MeasurementError(
                "merge-tree reported success without naming the merged tree: "
                + answer.diagnosis
            )
        if answer.code != 1:
            raise MeasurementError("merge-tree failed: " + answer.diagnosis)
        raise MeasurementError(
            "merge-tree exited 1 without naming a merged tree (a failure to merge "
            "the inputs, not a conflict): " + answer.diagnosis
        )
    if not answer.paths:
        raise MeasurementError(
            "merge-tree exited 1 naming a merged tree but no conflicted path, so "
            "which files collide cannot be named: " + answer.diagnosis
        )
    return None, list(answer.paths)
def _commit_tree(tree: str, parents: list[str], message: str) -> str:
    """Create a commit for a merged tree, so the next step can merge onto it.

    The pinned identity and date are `merge_tree.commit_env`'s - one implementation
    for the whole family's synthetic folds, rather than one per tool.
    """
    return merge_tree.commit_tree(tree, parents, message, run=_run, env=_commit_env())


def build_plan_steps(base: str, heads: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """Fold the plan, keeping every intermediate commit: (step, PR, commit).

    The fold is the same shape as landing the plan with squash merges - each head
    is merged onto the tree the previous steps built - and it never touches the
    working tree or a branch, only the object store.

    The intermediate commits are *kept* rather than discarded (they used to be a
    local variable): a plan can produce a final tree that passes while a step on
    the way is red, and the second half of issue #1161 is precisely that no gate
    looked at the steps. Merging each head onto the accumulated commit is what
    makes the step commits the trees that would exist if the plan were landed one
    PR at a time, so they are the right thing to judge with `--steps`.
    """
    accumulated = base
    steps: list[tuple[int, int, str]] = []
    for step, (number, head) in enumerate(heads, start=1):
        tree, paths = _merge_tree(accumulated, head)
        if tree is None:
            raise PlanConflict(step, number, paths)
        accumulated = _commit_tree(
            tree, [accumulated, head], f"plan step {step}: #{number}"
        )
        steps.append((step, number, accumulated))
    return steps


def build_plan_tip(base: str, heads: list[tuple[int, str]]) -> str:
    """The final commit of the plan (the whole plan's tree, as one object)."""
    steps = build_plan_steps(base, heads)
    return steps[-1][2] if steps else base


# A red tree has to be evidenced by the suite's own report: `rc == 1` alone is not a
# verdict, because a pytest that never started exits 1 too. Measured on this machine
# with the `SUITE` invocation above (`-q --no-header`) - a failing test prints
# `FAILED tests/test_bad.py::test_bad - assert 1 == 2` and ends
# `1 failed, 1 passed in 0.01s`; an error raised in a fixture teardown prints
# `ERROR <nodeid> - ...` and ends `3 passed, 1 error in 0.01s`, which is why the
# summary form is matched anywhere in the line rather than at its start. An
# invocation that cannot import pytest prints `No module named pytest` and neither
# form. Reading that as a red tree is a health finding about a tree no test ever ran
# on, and it is the one misreading a caller cannot see: it looks like a finding.
SUITE_FAILURE = re.compile(
    r"^(?:FAILED|ERROR) \S|\b\d+ (?:failed|error)s?\b.*\bin \d+\.\d+s", re.M
)


def _last_line(out: str, default: str) -> str:
    """The suite's own summary line - the last non-empty thing it printed."""
    return next(
        (line.strip() for line in reversed(out.splitlines()) if line.strip()), default
    )


def _no_suite_verdict(out: str) -> str:
    """Why `rc == 1` here is not a finding, and what to do about it.

    Two causes, and the obvious one is not the only one: a bare host `python3` has
    no pytest, while an unsynced worktree's `.venv` is empty - so the invocation
    this tool documents fails byte-identically there, and answering "use the project
    interpreter" sends the reader in a circle (`check-doc-count.py` measured exactly
    that, cyc20260913-122923). Both causes are named, and so is the command to run:
    a remedy that names no invocation leaves the reader where they were. This is
    what the exit code 2 the caller gets already promises - *the question could not
    be answered*.
    """
    invocation = f"uv run --no-sync python3 scripts/{Path(__file__).name}"
    tail = out[-1000:].strip()
    if "No module named pytest" in out:
        return (
            "the suite could not be run: pytest is not installed in the interpreter "
            f"running this tool ({sys.executable}), so nothing judged the tree:\n"
            + tail
            + "\n\nRun it with the project environment instead:\n"
            f"    {invocation} <PR> [<PR> ...]\n"
            "A fresh worktree or clone gets an empty `.venv`, where that same command "
            "fails identically - there, `uv sync` first."
        )
    return (
        "the suite exited 1 without a failure line in its own output, so this is not "
        "a verdict about the tree:\n" + tail + "\n\nCheck the invocation with "
        f"`{sys.executable} -m pytest --version`, then run this tool with the "
        f"project environment:\n    {invocation} <PR> [<PR> ...]"
    )


# The two forms pytest's short summary uses to blame a row, and the reason the rows are
# read out of the report rather than reconstructed: they are what the base comparison
# re-runs, so a name this module invented would ask the base tree a different question
# than the run answered. `FAILED <nodeid> - <message>` is a failing test, `ERROR <nodeid>
# - <message>` a fixture or teardown that failed; `ERROR: not found: <path>` - a
# collection error - matches neither, which the `" "` after the verb excludes. The id
# runs up to the `" - "` separator, which is what keeps an id parametrized with a space
# (`test_x[a b]`) whole.
#
# It is *not* the source of truth for a row, and issue #1386 measured why: an id that
# itself contains `" - "` is cut at its first occurrence, and the cut row is then one the
# base tree does not contain - which `_still_red_on` maps to *a row a tree does not
# contain cannot fail there*, i.e. to a row the plan's tree is blamed for. The machine-
# readable report below is the source; this expression stays for the two places that have
# no better instrument (the summary line, and a run that wrote no junit report).
BLAMED_ROW = re.compile(r"^(?:FAILED|ERROR) (\S.*?)(?: - |$)", re.M)

# What pytest prints for an argument its tree does not contain:
#     ERROR: not found: /tmp/emrg-plan-suite-x/base/tests/test_a.py::test_b
#     ERROR: file or directory not found: tests/test_a.py::test_b[a b]
# Both spellings have been seen on this machine (pytest 9.1.1 prints the second), so the
# line is matched by the phrase and the argument is the rest of it. *The rest of the line*,
# not `\S+`: an argument is a node id, and a node id contains spaces - `[a b]` parameters
# are everywhere and 22 ids in this suite contain `" - "` - so stopping at the first
# whitespace loses a row the run itself named. Measured (`cyc20260918-212523`): with `\S+`,
# asking this repository for a missing `…::test_dashed_id[git commit -q -F - <<EOF]` gave
# `set()`, which `_still_red_on` reads as "this report named nothing" and turns into
# "could not measure" for a question it had the answer to.
NOT_FOUND = re.compile(r"not found: (\S.*)", re.M)


def _failing_rows(out: str) -> list[str]:
    """Every node id the suite's own report blames, in the order it printed them."""
    return [match.group(1).strip() for match in BLAMED_ROW.finditer(out)]


# The machine-readable row list. `--junitxml` alone is not enough: the default `xunit2`
# family writes `classname` and `name` only, and `tests.test_a.TestKlass` does not name the
# file it came from - so the row could not be handed back to pytest for the base run, which
# is the one thing these rows are for. `xunit1` adds `file` (measured: pytest 9.1.1,
# `_pytest/junitxml.py`, `families["xunit1"]`), and it is passed with `-o` on the command
# line so a `junit_family` in the tree's own config cannot change the shape of the report
# this tool reads. Written outside the worktree, in the harness's scratch directory, so the
# run leaves nothing in the tree it measures.
JUNIT_FAMILY = ["-o", "junit_family=xunit1"]


def _row_id(file_attr: str, classname: str, name: str) -> str | None:
    """The node id a junit record names, or `None` when it cannot name one.

    pytest derives `classname` from the node id the same way for every id
    (`mangle_test_address`): `/` -> `.`, then a trailing `.py` dropped. Undoing that needs
    the file the record names - the attribute only the `xunit1` family writes - and the
    remainder of the classname is the class path. A classname whose module is not that
    file's dotted path is a shape this tool does not understand; `_junit_rows` reports it
    rather than guessing a path that might name a different test.
    """
    path = (file_attr or "").replace("\\", "/")
    module = re.sub(r"\.py$", "", path).replace("/", ".")
    if not path or not module or not name:
        return None
    if classname == module:
        classes: list[str] = []
    elif classname.startswith(module + "."):
        classes = classname[len(module) + 1 :].split(".")
    else:
        return None
    return "::".join([path, *classes, name])


def _junit_rows(report: Path) -> list[str] | None:
    """Every node id the run's junit report blames, in its order - or `None` for no report.

    `None` is not "nothing failed": it is "this run left no machine-readable list", which
    only the fallback in `_suite_verdict` reads. A report that is there and cannot be
    parsed, or a failing record whose node id cannot be built, is a measurement error
    instead: a row list that had to be guessed is exactly what issue #1386 is about, so an
    unreadable one is exit 2's "could not measure" rather than a list to attribute.
    """
    if not report.is_file():
        return None
    try:
        root = ET.parse(report).getroot()
    except ET.ParseError as exc:
        raise MeasurementError(f"the run's junit report could not be read: {exc}") from exc
    rows: list[str] = []
    for case in root.iter("testcase"):
        if not any(child.tag in ("failure", "error") for child in case):
            continue
        row = _row_id(
            case.get("file") or "", case.get("classname") or "", case.get("name") or ""
        )
        if row is None:
            raise MeasurementError(
                "the run's junit report names a failure that cannot be turned back into a "
                f"node id (file={case.get('file')!r}, classname={case.get('classname')!r}, "
                f"name={case.get('name')!r}), so the rows this tree is blamed for cannot be "
                "read without guessing one"
            )
        rows.append(row)
    return rows


def _unfound_ids(out: str, rows: list[str]) -> set[str]:
    """Which of `rows` the tree does not contain, as the run itself reported it.

    pytest aborts the whole invocation when one argument cannot be resolved - measured
    on this machine: one existing and one missing id print *two* `not found` lines and
    run `no tests`, rc 4 - so a missing argument has to be removed from the invocation
    rather than read as a result. The answer is not in doubt, though: a row a tree does
    not contain cannot fail there.

    Separators are normalised on both sides, which is not cosmetic: pytest names the
    argument by its *path* (the platform's separator) while a node id always uses `/`.
    Measured while writing this, on the same string with the two spellings -
    `/tmp/base/tests/test_a.py::test_b` matched, `C:\\Temp\\base\\tests\\test_a.py::test_b`
    matched **nothing**. Blind there, every row the base does not contain would have
    been read as "the base could not be measured" (rc 2) instead of as the answer.

    The argument is taken to the end of its line (`NOT_FOUND`), because it is a node id and
    a node id contains spaces: reading only up to the first whitespace loses every row
    parametrized with one, and losing it here *is* "could not measure" - measured
    (`cyc20260918-212523`) on a real missing `…[git commit -q -F - <<EOF]`, which came back
    as `set()`. A row holding a newline is still beyond this instrument: the run prints it
    across two lines and nothing line-anchored can carry it, so `_still_red_on` answers rc 2
    there - fail-closed, never a guessed attribution.
    """
    named = {match.group(1).strip().replace("\\", "/") for match in NOT_FOUND.finditer(out)}
    return {
        row
        for row in rows
        if any(n == row or n.endswith("/" + row.replace("\\", "/")) for n in named)
    }


# A populated environment belonging to the *harness* rather than to the tree under
# test: purged of caches for speed, never a source of the answer. A fresh worktree
# has none of these; a re-used one can.
_NOT_THE_TREES_OWN = (".venv", "node_modules", ".git")


def _purge_bytecode(root: Path) -> list[str]:
    """Delete the bytecode caches under ``root``; return what was removed.

    A `.pyc` is a second copy of a source and CPython prefers it whenever the
    header's `(int(mtime), size)` pair still matches, so a cache left in a tree by
    an earlier revision - or by a mutant, when the edit keeps the file's length and
    lands in the same second - can answer for the revision under test. Measured
    once that way in a kept worktree (`cyc20260914-085416`, see the header).

    Directories that belong to the harness (`.venv`, `node_modules`) are skipped:
    their caches cannot shadow the tree's own modules, and walking them costs more
    than the run they precede.

    The names are spelled with `/` on every platform (`as_posix`), because the
    native spelling is not a fact about the tree: measured by the Windows job of
    #1214 (`cyc20260914-092057`), `str(path.relative_to(root))` answers
    `emrg\\server\\__pycache__` there and `emrg/server/__pycache__` here, so the same
    purge reported two different names - and a name that means "the same cache"
    only on one platform is the same class of defect this file exists to remove.
    The suite this feeds runs on Windows too (`test-windows`), which is the only
    place the difference is visible at all.
    """
    removed: list[str] = []
    for path in sorted(root.rglob("*")):
        if any(part in _NOT_THE_TREES_OWN for part in path.relative_to(root).parts):
            continue
        if path.is_dir() and path.name == "__pycache__":
            # A stray `.pyc` *inside* a `__pycache__` is removed with it, so only
            # the directory is reported - two entries for one cache would make the
            # returned list a count of files rather than of caches.
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path.relative_to(root).as_posix())
        elif path.is_file() and path.suffix == ".pyc":
            path.unlink(missing_ok=True)
            removed.append(path.relative_to(root).as_posix())
    return removed


def _suite_env(worktree: Path) -> dict[str, str]:
    """The environment the suite runs with: the tree under test, and no caches.

    `PYTHONPATH` is *prepended* rather than replaced: the point is that the tree
    wins every name it defines, not that the caller's environment is discarded -
    this machine exports an installed copy of the package
    (`/Users/argszero/.emrg/install/source`), and whichever copy `sys.path` reaches
    first is the one whose answer the run reports.

    `PYTHONDONTWRITEBYTECODE` is set for the run and inherited by everything it
    spawns, so a measurement cannot leave a cache that a later run would read.

    One variable is *removed* rather than passed through, because it changes what
    the suite says about a tree rather than what the suite can see:
    `EMRG_TASK_DIRTY_OVERRIDE`. It is the evolution cycle's own escape hatch - a
    cycle whose tree is dirty exports it to keep working, and `scheduler.py` reads
    it from the environment - while `tests/test_scheduler.py` has four tests that
    assert the *unoverridden* verdict for a dirty tree, reaching the real project
    rather than the worktree. Measured 2026-09-17 (`cyc20260917-142057`): with a
    dirty caller tree and the override exported, those four fail and the run reports
    `suite FAILED` for a plan whose tree is green; with the override unset they pass,
    dirty tree and all. That is a verdict about the caller's working tree, which is
    the one thing this harness must never report. Dropping it cannot cost the run its
    own answer: a fresh worktree is clean, so the exception the override grants is
    not in play there - and a measurement must not inherit the caller's reason for
    making an exception.
    """
    existing = os.environ.get("PYTHONPATH", "")
    pinned = str(worktree) + (os.pathsep + existing if existing else "")
    env = {**os.environ, "PYTHONPATH": pinned, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("EMRG_TASK_DIRTY_OVERRIDE", None)
    return env


def _suite_verdict(
    tip: str, scratch: Path, keep: Path | None = None
) -> tuple[bool, str, str, list[str]]:
    """Run the repository's suite in a worktree of the planned tree.

    Returns (passed, suite summary, tree sha, failing rows). The tree sha is returned and
    reported because the family's recurring defect is a verdict about a tree the
    caller was not looking at. The failing rows are the run's own, returned so the
    caller can ask the base tree the *same* question rather than a reconstructed one
    (issue #1378: a plan whose tree inherits a red base was reported as a combination
    failure and pointed at a PR that owns nothing).

    The rows are read from the run's **junit report**, not from its text summary, because
    the text form is lossy for an id: it puts the id and the failure's message on one line
    and separates them with `" - "`, which is also a substring of 22 node ids in this
    repository's own suite (issue #1386, measured in the header). A run that wrote no
    report - a caller replacing `SUITE` with something that is not pytest - falls back to
    the text form and says so, since a silently unverified row list is the defect.

    `keep` materialises the worktree there and leaves it in place for the caller, who
    then owns its removal; nothing else about the run changes (same caches purged, same
    interpreter and `PYTHONPATH` pinned to the tree), so a kept worktree answers for the
    same tree the default run would have deleted.
    """
    tree_sha = _tree_of(tip, "the planned tree")

    updated = _run(["git", "update-ref", TIP_REF, tip])
    if updated.returncode != 0:
        raise MeasurementError(f"could not mark the plan tip: {updated.stderr.strip()}")
    worktree = keep if keep is not None else scratch / "tree"
    # Beside the worktree, never inside it: the report is the harness's, and a file this
    # tool writes into the tree it measures is a file the suite could see (`untracked`
    # checks, `git status` assertions) and would have to be removed again.
    junit = scratch / "plan-junit.xml"
    try:
        added = _run(["git", "worktree", "add", "--detach", str(worktree), TIP_REF])
        if added.returncode != 0:
            raise MeasurementError(
                "could not materialise the planned tree: " + added.stderr.strip()
            )
        if not (worktree / "tests").is_dir():
            raise MeasurementError("the planned tree has no tests/ directory")
        _purge_bytecode(worktree)
        proc = _run(
            [sys.executable, *SUITE, "--junitxml", str(junit), *JUNIT_FAMILY],
            cwd=str(worktree),
            env=_suite_env(worktree),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            return True, _last_line(out, "suite passed"), tree_sha, []
        if proc.returncode == 1:
            failures = [
                line.split(" ", 1)[1].strip()
                for line in out.splitlines()
                if line.startswith("FAILED ")
            ]
            if not failures and not SUITE_FAILURE.search(out):
                raise MeasurementError(_no_suite_verdict(out))
            rows = _junit_rows(junit)
            if rows is None:
                # Reachable only when the run wrote no junit report at all - a replaced
                # `SUITE`, or a pytest whose junit plugin is disabled. Said out loud,
                # because an unverified row list is what #1386 is about and silence is
                # how it got attributed in the first place.
                print(
                    "the suite left no junit report, so the rows it names are read from "
                    "its text summary and not verified against a node id list: see "
                    "_junit_rows for what that costs.",
                    file=sys.stderr,
                )
                rows = _failing_rows(out)
            summary = "; ".join(rows[:5]) if rows else _last_line(out, "suite FAILED")
            return False, summary, tree_sha, rows
        # 2 interrupted, 3 internal error, 4 usage error, 5 no tests collected. None
        # of these is "the suite passed" - and rc 1 reaches here only with a failure
        # report in hand, since a report is what the branch above asks for.
        raise MeasurementError(
            f"the suite could not be run (rc={proc.returncode}):\n" + out[-1000:].strip()
        )
    finally:
        if keep is None:
            _run(["git", "worktree", "remove", "--force", str(worktree)])
        _run(["git", "update-ref", "-d", TIP_REF])


def _pytest_rows(worktree: Path, rows: list[str], junit: Path) -> tuple[int, str]:
    """Run only these rows in that tree, with the same interpreter and pinning.

    Running the rows rather than the whole suite is what makes a second measurement
    affordable - seconds against the ~140s a full run costs here - and it is also what
    makes the two runs comparable: the base is asked exactly the question the plan's run
    answered, not a wider one that merely contains it. Same `cwd` and `PYTHONPATH` pin
    as the suite run (`_suite_env`), because a base tree measured through a different
    path is a different tree's answer.

    The junit report is written to `junit` for the same reason the plan's run writes one:
    this run's *text* summary truncates a row whose id contains `" - "` (issue #1386), and
    matching a truncated row against the rows that were asked for is how a row the base
    really fails gets read as one it does not contain - which is the attribution this
    whole step exists to avoid. Rows are asked for exactly, so the answer is a set
    intersection, not a parse.
    """
    proc = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            *rows,
            "-q",
            "--no-header",
            "--junitxml",
            str(junit),
            *JUNIT_FAMILY,
        ],
        cwd=str(worktree),
        env=_suite_env(worktree),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _still_red_on(tip: str, rows: list[str], scratch: Path) -> set[str]:
    """Which of `rows` also fail on the tree at `tip`, measured the way the plan was.

    The question a red plan leaves open is *whose* failure it is, and the plan's own run
    cannot answer it: `_suite_verdict` measures one tree. Issue #1378 measured the cost
    of that gap - plans `#1373` and `#1375` were reported as "the tree they produce
    together fails the suite … re-push the PR that owns the failure (a push voids its
    votes)" while the same five rows failed on the base tree too (the harness checks the
    tree out under the OS temp root, itself an allowed write root, so the unpinned
    write-root-dependent rows flip). Neither PR touches the files that fail: the remedy
    pointed at a PR that owns nothing, and a cycle that believes it either re-pushes its
    own untouched PR - voiding valid votes and re-running CI for a tree that is not the
    one failing - or goes looking inside a diff for a failure that is not there.

    Everything about the materialisation is the plan's: a detached worktree of the base
    commit, bytecode purged, the interpreter and `PYTHONPATH` pinned to that tree. Rows
    the base does not contain cannot fail there, and pytest runs nothing at all when one
    argument is unresolvable (measured), so a `not found` report is that answer rather
    than a failed measurement and the run is repeated without those rows. Anything else
    the base run reports - an interpreter without pytest, rc 3 - is a measurement error:
    an unanswerable question is rc 2, never a verdict about a PR.

    The base's own failing rows are read from its junit report and intersected with the
    rows that were asked for, so an id containing `" - "` is matched rather than truncated
    (issue #1386: truncated, it is a row this tree does not contain, which reads as *the
    base fails nothing* and lays the row at a PR's door). Only a run that wrote no report
    falls back to matching its text summary against the rows.
    """
    worktree = scratch / "base"
    # Outside the base worktree, like the plan's report: nothing this tool writes may land
    # in the tree whose suite is running.
    junit = scratch / "base-junit.xml"
    added = _run(["git", "worktree", "add", "--detach", str(worktree), tip])
    if added.returncode != 0:
        raise MeasurementError(
            "could not materialise the base tree: " + added.stderr.strip()
        )
    try:
        _purge_bytecode(worktree)
        rc, out = _pytest_rows(worktree, rows, junit)
        if rc == 4:
            missing = _unfound_ids(out, rows)
            if not missing:
                raise MeasurementError(
                    "the base tree could not be asked these rows (rc=4):\n"
                    + out[-1000:].strip()
                )
            rows = [row for row in rows if row not in missing]
            if not rows:
                return set()
            rc, out = _pytest_rows(worktree, rows, junit)
        if rc == 0:
            return set()
        if rc == 1:
            reported = _junit_rows(junit)
            blamed = (
                set(_failing_rows(out)) & set(rows)
                if reported is None
                else set(reported) & set(rows)
            )
            if blamed:
                return blamed
        raise MeasurementError(
            f"the base tree could not be measured (rc={rc}):\n" + out[-1000:].strip()
        )
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)])


def _ownership_lines(
    base_tree: str, failing: list[str], inherited: set[str]
) -> tuple[str, str]:
    """Who owns the failing rows: the paragraphs that say so, or ``""`` for neither.

    The plan's paragraph and the base's are built together so that they are halves of one
    split - a row appears in at most one of them, and the union is `failing` - which is
    the property that keeps the remedy honest. A row the base tree also fails must not
    reach the sentence that tells the caller to re-push a PR, and a row only the plan's
    tree fails must, restricted to those rows.

    A pure function of the split, so the wording is asserted without a git run; mutating
    it into "the base owns everything" is what the combination arm of the test pair
    catches (`--steps` mode keeps the older wording: it measures a different tree per
    step, so its owner is a step, not this split).
    """
    own = [row for row in failing if row not in inherited]
    plan = ""
    if own:
        plan = (
            "\nThe plan's steps are individually clean and the per-PR signals are green, "
            "but the tree they produce together fails the suite. Fix it on the merged "
            "tree and re-push the PR that owns the failure (a push voids its votes).\n"
            "  rows this plan's tree owns: " + ", ".join(own)
        )
    base = ""
    if inherited:
        base = (
            f"\n{len(inherited)} of the {len(failing)} failing row(s) fail on the base "
            f"tree {_tree_id(base_tree)} too, measured the same way (same interpreter, "
            "PYTHONPATH pinned to that tree, caches purged): "
            + ", ".join(sorted(inherited))
            + "\nNo PR in this plan owns those rows, so re-pushing one would void its "
            "votes and re-run CI for a tree that is not the one failing: fix them on the "
            "base instead."
        )
    return plan, base


def _kept_note(path: Path, tree_sha: str | None = None) -> None:
    """What a kept worktree is good for, and the two traps it inherits.

    Printed only when the directory really is there, so the note cannot describe a
    worktree that failed to materialise. Both traps are *environment*, not the tree:
    naming them here is what keeps the next reader from reporting them as defects.
    """
    if not path.is_dir():
        return
    where = f"kept {path}"
    if tree_sha:
        where += f" (tree {_tree_id(tree_sha)})"
    main = _main_worktree() or "<main checkout>"
    print(f"\n{where} - run your own checks there, then remove it:")
    print(f"  git worktree remove --force {path}")
    print(
        "  It has no .venv and no node_modules, like any fresh worktree: `uv run pytest`\n"
        "  there reports that no suite ran, and the GUI Node suite fails two files - the\n"
        "  spawn-args test (python=python3, expected .venv/bin/python) and\n"
        "  test/integration.test.js (Cannot find module 'ws'). Measured on a real landing\n"
        "  tree (2026-09-19, f96d6515c734): the GUI suite is 125 passed / 2 failed with\n"
        "  neither link, 126 / 1 with the python link alone, 126 / 0 / 8 skipped with both.\n"
        "  The node link must point at the GUI's OWN node_modules: the repository root has\n"
        "  none at all (there is no root package.json either), so `ln -sfn` from it leaves a\n"
        "  dangling symlink - as indistinguishable from linking nothing as an empty directory\n"
        "  would be (126 / 1 either way). Both remedies, against this tree:"
    )
    # `cd` before the interpreter, not only `PYTHONPATH`: the harness's own `_suite_verdict`
    # passes `cwd=str(worktree)` *and* `_suite_env`'s pinned `PYTHONPATH` for the same
    # stated reason, and a remedy that keeps only the weaker pin measures the wrong tree.
    # For `-m pytest`, `sys.path[0]` is the process CWD and the positional `tests/`
    # resolves against it too, so run from the main checkout - where this note is printed
    # and therefore where it will be copied - the `PYTHONPATH` below pins nothing and the
    # reader gets a plausible green about the main tree (measured 2026-09-17,
    # `cyc20260917-142057`: 2797 collected from the main checkout against 2801 from the
    # kept tree, the marker test collecting only in the latter).
    print(
        f"    python: cd {path} && PYTHONPATH={path} "
        f"{main}/.venv/bin/python -m pytest tests/ -q"
    )
    print(f"    node:   ln -sfn {main}/emrg/gui/node_modules {path}/emrg/gui/node_modules")
    print(f"            ln -sfn {main}/.venv {path}/.venv")
    print("  Then compare worktree runs with worktree runs, never with main-checkout runs.")


def _main_worktree() -> str | None:
    """The main worktree's path, so the remedies above are commands and not placeholders.

    `git worktree list --porcelain` lists the main worktree on its first `worktree`
    line and the linked ones after it. Only a hint in a printed note: if git cannot
    answer, the note says `<main checkout>` rather than guessing a path.
    """
    listed = _run(["git", "worktree", "list", "--porcelain"])
    if listed.returncode != 0:
        return None
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree ") :].strip()
    return None


def _judge_every_step(base: str, heads: list[tuple[int, str]]) -> int:
    """Run the suite on each intermediate tree, not only on the final one.

    Issue #1161's open half: the tool judged the plan's *final* tree, so a rule
    that breaks only at an intermediate step was invisible to it. That gap has a
    shape, and it is not exotic - a plan whose last PR is the one that fixes what
    an earlier PR broke is *green at the end and red on the way*, and the trees in
    between are the ones a caller who lands the plan step by step will actually
    have on master, one at a time, with CI reporting green for each.

    Cost is one suite run per step (`~55s` here), which is why it is opt-in: the
    final tree remains the default question, since that is what decides whether
    master is healthy a minute after the whole plan lands.
    """
    try:
        steps = build_plan_steps(base, heads)
    except PlanConflict as exc:
        print(f"no final tree: {exc}", file=sys.stderr)
        print(
            "\nA step of the plan conflicts, so the plan has no final tree to judge. "
            "That is not a health verdict - resolve the conflict (and re-push) or "
            "reorder with check-merge-sequence.py.",
            file=sys.stderr,
        )
        return 3
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    red: list[tuple[int, int, str]] = []
    for step, number, commit in steps:
        try:
            with tempfile.TemporaryDirectory(prefix="emrg-plan-step-") as tmp:
                passed, summary, tree_sha, _rows = _suite_verdict(commit, Path(tmp))
        except MeasurementError as exc:
            print(f"could not measure step {step} (#{number}): {exc}", file=sys.stderr)
            return 2
        state = "OK" if passed else "FAILED"
        print(
            f"step {step} (#{number}) tree {_tree_id(tree_sha)} "
            f"suite {state}: {summary}"
        )
        if not passed:
            red.append((step, number, summary))

    if not red:
        print(f"every step healthy ({len(steps)} suite run(s))")
        return 0
    detail = "; ".join(f"step {step} (#{number}): {summary}" for step, number, summary in red)
    print(f"suite FAILED at {len(red)} of {len(steps)} step(s): {detail}")
    print(
        "\nThe plan's final tree is not what this reports on: a step's tree is. Each "
        "of these would be master's tree for a while if the plan is landed in order, "
        "so fix it on the PR that owns the step (a push voids its votes). Landing the "
        "whole plan at once would land the final tree, which is judged by default.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the repository's test suite on the tree a plan of PRs would land."
        )
    )
    parser.add_argument(
        "prs", nargs="*", type=int, help="PR numbers in landing order (default: all open)"
    )
    parser.add_argument("--repo", default="argszero/emrg", help="owner/name")
    parser.add_argument(
        "--base",
        default="origin/master",
        help=(
            "the ref to plan onto; a remote-tracking ref is fetched first and "
            "taken by full name, anything else literally"
        ),
    )
    parser.add_argument(
        "--steps",
        action="store_true",
        help=(
            "judge every intermediate tree of the plan, not only the final one "
            "(one suite run per step; issue #1161's open half)"
        ),
    )
    parser.add_argument(
        "--keep",
        metavar="DIR",
        default=None,
        help=(
            "materialise the planned tree at DIR and leave it there for your own "
            "checks instead of removing it (same run, same tree; the caller removes it)"
        ),
    )
    args = parser.parse_args(argv)

    if args.keep is not None and args.steps:
        print(
            "could not measure: --keep names the one tree to leave behind, while "
            "--steps measures a different tree per step - they cannot be combined",
            file=sys.stderr,
        )
        return 2
    keep = Path(args.keep).expanduser().resolve() if args.keep else None
    if keep is not None and keep.exists():
        print(
            f"could not measure: --keep {keep} already exists - a stale worktree there "
            "would be confusing, and `git worktree add` refuses the path anyway. Remove "
            "it first (`git worktree remove --force`), or name an empty path.",
            file=sys.stderr,
        )
        return 2

    fetched: list[int] = []
    # Everything from the first fetch on runs with temp refs in the object database,
    # so it is all wrapped: a plan that conflicts (rc 3), a step that cannot be built,
    # or an unanswerable suite (rc 2) fetched its heads just the same, and a cleanup
    # that only ran on the path that reached the suite would leak on exactly the runs
    # that fail - the ones a reader is most likely to repeat.
    #
    # The fetch itself belongs *inside* this try, not before it. It is the first thing
    # that parks a ref, so a run that fetches PR 1 and then dies on PR 2 has already
    # created PR 1's ref - the case `_fetch_heads`' own docstring names ("those two are
    # exactly the ones that need clearing"). Measured 2026-09-17 by @how2how2how2-arch
    # on the previous revision of this fix: `check-merge-plan-suite.py 1323 999999`
    # reported rc 2 and left `refs/emrg-plan-suite/pr1323` behind, because the fetch sat
    # in a `try` whose `except` returned before the cleanup's `finally` was entered.
    try:
        # The base, in the two dimensions it can be wrong by: *when* it was read
        # and *which* ref the name denotes. Both are owned by the sibling, so both
        # are asked of it rather than reimplemented here - a plan measured against
        # a base nobody named is the defect this whole family exists to remove.
        seq._refresh_base(args.base)
        base_ref = seq._qualify_ref(args.base)
        base = _rev_parse(base_ref)
        numbers = args.prs or _open_pr_numbers(args.repo)
        heads = _fetch_heads(numbers, fetched)

        # The ref measured, not the spelling typed: they differ whenever a short name
        # is ambiguous, and a header that reports `origin/master` for a commit that is
        # not master is how the wrong-tree defect stays invisible.
        print(f"base {base[:8]} ({base_ref}), {len(numbers)} PR(s) planned")

        try:
            tip = build_plan_tip(base, heads)
        except PlanConflict as exc:
            print(f"no final tree: {exc}", file=sys.stderr)
            print(
                "\nA step of the plan conflicts, so the plan has no final tree to judge. "
                "That is not a health verdict - resolve the conflict (and re-push) or "
                "reorder with check-merge-sequence.py.",
                file=sys.stderr,
            )
            return 3
        except MeasurementError as exc:
            print(f"could not measure: {exc}", file=sys.stderr)
            return 2

        print("plan: " + " -> ".join(f"#{number}" for number, _ in heads))
        if args.steps:
            return _judge_every_step(base, heads)
        # Both are filled only when the final tree is red, and both are read only on that
        # path: a green run has no ownership question to answer.
        base_tree = ""
        inherited: set[str] = set()
        try:
            with tempfile.TemporaryDirectory(prefix="emrg-plan-suite-") as tmp:
                passed, summary, tree_sha, failing = _suite_verdict(tip, Path(tmp), keep)
                if not passed and failing:
                    # The same question asked of the other tree, before the verdict is
                    # attributed: see `_still_red_on` for why the plan's own run cannot
                    # answer it. A base that cannot be measured is reported as such and
                    # leaves as rc 2 - "the question could not be answered" - rather than
                    # as a verdict that names the wrong owner.
                    try:
                        base_tree = _tree_of(base, "the base tree")
                        inherited = _still_red_on(base, failing, Path(tmp))
                    except MeasurementError as exc:
                        print(f"could not measure the base: {exc}", file=sys.stderr)
                        if keep is not None:
                            _kept_note(keep)
                        return 2
        except MeasurementError as exc:
            print(f"could not measure: {exc}", file=sys.stderr)
            if keep is not None:
                _kept_note(keep)
            return 2

        print(f"final tree {_tree_id(tree_sha)}")
        if keep is not None:
            _kept_note(keep, tree_sha)
        if passed:
            print(f"suite OK: {summary}")
            return 0
        print(f"suite FAILED: {summary}")
        if not failing:
            print(
                "\nThe suite reported a red tree without naming an individual failing row, "
                "so nothing here says whether this plan or the base owns it. Re-run the "
                "suite verbosely (-v) to get the row, or compare the two trees by hand.",
                file=sys.stderr,
            )
            return 1
        plan_text, base_text = _ownership_lines(base_tree, failing, inherited)
        if plan_text:
            print(plan_text, file=sys.stderr)
        if base_text:
            print(base_text, file=sys.stderr)
        return 1
    except MeasurementError as exc:
        # Reached by everything that can fail before the suite does - the base, the
        # open-PR listing, and any fetch (the head fetched first is the ref the
        # `finally` below exists for). The inner `except` clauses keep their own
        # messages and run first; this is the same discipline one level out: an
        # unanswerable question is rc 2, never a verdict.
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2
    finally:
        _drop_fetched_refs(fetched)


if __name__ == "__main__":
    sys.exit(main())
