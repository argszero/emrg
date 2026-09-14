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
from pathlib import Path

TIP_REF = "refs/emrg-plan-suite/tip"
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


# The date carried by every synthetic plan commit. Pinned, not read from the
# clock: a fold must be a function of its inputs, and a commit's sha includes its
# committer date, so an unpinned fold produced a *different* sha for the *same*
# plan whenever two folds straddled a second boundary. Measured
# (cyc20260913-194108, Windows CI run 34754517824 on #1190): the test comparing
# `build_plan_tip` against the last step tree failed on exactly that - the two
# folds differed and nothing was wrong with either tree. It also makes a `--steps`
# tree sha comparable between runs, which is the point of printing one.
PLAN_COMMIT_DATE = "2000-01-01T00:00:00 +0000"


def _commit_env() -> dict[str, str]:
    """Author/committer for the synthetic plan commits, independent of git config.

    Identity *and* date are pinned, so the same plan folds to the same commits on
    every machine and at every speed (see `PLAN_COMMIT_DATE`).
    """
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "emrg-plan-suite",
        "GIT_AUTHOR_EMAIL": "plan-suite@emrg.invalid",
        "GIT_COMMITTER_NAME": "emrg-plan-suite",
        "GIT_COMMITTER_EMAIL": "plan-suite@emrg.invalid",
        "GIT_AUTHOR_DATE": PLAN_COMMIT_DATE,
        "GIT_COMMITTER_DATE": PLAN_COMMIT_DATE,
    }


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
    """Fetch a PR's real head into a temp ref and return the ref name.

    The refspec is forced (`+`): PR heads here are routinely re-pushed to a commit
    that is not a descendant of the previous one (every conflict resolution does),
    and a rejected fetch would leave the *stale* ref in place, so the plan would
    silently be built from a tree that is no longer the PR.
    """
    ref = f"refs/emrg-plan-suite/pr{number}"
    proc = _run(["git", "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}"])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise MeasurementError(f"could not fetch PR #{number}: {detail}")
    return _rev_parse(ref)


#: A conflicted path as the *stage block* writes it: `<mode> <blob> <stage>\t<path>`.
#: The block is the report's first block - one line per side per conflicted path,
#: stages 1/2/3 - and it ends at the first blank line, after which git writes prose
#: ("Auto-merging <path>", "CONFLICT (content): Merge conflict in <path>"). The mode
#: is `[0-7]{6}` so a symlink (120000) or a gitlink (160000) is named like any other
#: path. See `_conflict_block_paths` for why the shape is matched instead of the tab.
_CONFLICT_LINE = re.compile(r"^[0-7]{6} [0-9a-f]+ [123]\t(?P<path>.+)$")

#: The escapes git's path quoting uses - `quote_c_style`'s set, as bytes.
_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
    "\\": 0x5C,
    '"': 0x22,
}


def _unquote_path(path: str) -> str:
    """The path git *means*, from the path git wrote.

    `merge-tree` quotes a path that holds a quote, a backslash, a control byte, or
    - with the default `core.quotePath=true` - a non-ASCII byte, C-style:
    `"f\\ttab.txt"`, and `中文.txt` as `"\\344\\270\\255\\346\\226\\207.txt"`
    (measured 2026-09-14, `cyc20260914-062927`, git 2.50.1, in a scratch repo).
    Passed through as written, either names a file nobody has: the caller is told
    the step "conflicts on \"\\344\\270\\255...txt\"", which is not a path it can
    open or resolve.

    Decoding here also makes the path independent of the *reader's* locale: the
    escapes are ASCII, so the real bytes are reassembled by this function rather
    than by whatever encoding the subprocess reader happened to pin - the same
    class of defect as the `git status --porcelain` decode pinned in
    `emrg/server/scheduler.py`, where octal escapes of a CJK name under a GBK
    locale were measured to arrive as different characters.
    """
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        # Unquoted: git wrote the path's bytes as they are (with
        # `core.quotePath=false`, or a path that needed no quoting at all).
        return path
    body = path[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            i += 1
            continue
        i += 1
        if i >= len(body):
            # A lone trailing backslash: not a quoted path after all. Keep it.
            out.extend(b"\\")
            break
        escape = body[i]
        if escape in _C_ESCAPES:
            out.append(_C_ESCAPES[escape])
            i += 1
        elif escape in "01234567":
            # Octal, always three digits in git's output; a short tail is taken
            # as it comes rather than invented into a byte.
            digits = body[i : i + 3]
            i += len(digits)
            out.append(int(digits, 8) & 0xFF)
        else:
            out.extend(escape.encode("utf-8"))
            i += 1
    # `errors="replace"`: the bytes may be any encoding, and the reader that
    # produced `path` was already pinned to UTF-8 (see `_run`). A name outside
    # UTF-8 degrades the same way here as it would anywhere else in this tool.
    return out.decode("utf-8", errors="replace")


def _conflict_block_paths(lines: list[str]) -> list[str]:
    """The conflicted paths a conflict report's stage block names.

    **The stage block, not "every line with a tab in it".** The old reading took
    the text after the first tab from *any* line of the report, and the report
    continues past the block with prose that embeds the conflicted paths: measured
    (2026-09-14, `cyc20260914-062927`, git 2.50.1), a conflict in a file whose
    name contains a tab reports

        100644 <blob> 1\t"f\\ttab.txt"        <- the stage block: git's spelling
        ...
        Auto-merging f<TAB>tab.txt            <- prose: a real tab, not a path
        CONFLICT (content): Merge conflict in f<TAB>tab.txt

    and splitting those prose lines on their tab hands back `tab.txt` - a file
    that collides with nothing and does not exist, named in the report as if it
    did. The block ends at the blank line git writes after it, and every line in
    it has the stage shape, so both are used here: the tab is not the separator
    that identifies a path, the block is.

    The paths are returned as the real names (see `_unquote_path`), one per stage
    line, in the order git wrote them: stages 1/2/3 of the same path appear once
    per stage, which is what lets a rename conflict name all three sides, and
    callers dedupe before printing.
    """
    paths: list[str] = []
    for line in lines[1:]:  # lines[0] is the merged tree's name
        if not line.strip():
            break  # end of the stage block; the rest of the report is prose
        match = _CONFLICT_LINE.match(line)
        if match:
            paths.append(_unquote_path(match.group("path")))
    return paths


def _merge_tree(ours: str, theirs: str) -> tuple[str | None, list[str]]:
    """The tree of the clean merge of two commits, or the conflicted paths.

    **The exit code is not the signal; the output is.** Measured (`git merge-tree
    --write-tree`, this repo): a genuine conflict exits 1 and prints the merged
    tree's OID on the first line of stdout, but so does a failure to merge the two
    *inputs* - an unknown ref, or an object that dereferences to a blob - which
    exits 1 with **empty stdout** and a diagnostic on stderr. Reading only the exit
    code therefore reports "I could not merge these two inputs" as a plan conflict,
    with an empty path list, and sends the caller off to resolve a conflict that
    does not exist. A clean merge always prints its tree OID too, so an empty stdout
    is never an answer, whatever the code says.

    The conflicted paths are read out of the stage block and unquoted
    (`_conflict_block_paths`), and a conflict that names no path is a measurement
    error rather than a `PlanConflict` with an empty list - the report says the
    step "conflicts on " and names nothing, which is the same unevidenced answer
    one field over.
    """
    proc = _run(["git", "merge-tree", "--write-tree", ours, theirs])
    lines = proc.stdout.splitlines()
    if proc.returncode == 0:
        if not lines or not _is_object_name(lines[0].strip()):
            raise MeasurementError(
                "merge-tree reported success without naming the merged tree: "
                + _diagnosis(proc)
            )
        return lines[0].strip(), []
    if proc.returncode != 1:
        raise MeasurementError("merge-tree failed: " + _diagnosis(proc))
    if not lines or not _is_object_name(lines[0].strip()):
        raise MeasurementError(
            "merge-tree exited 1 without naming a merged tree (a failure to merge "
            "the inputs, not a conflict): " + _diagnosis(proc)
        )
    paths = _conflict_block_paths(lines)
    if not paths:
        raise MeasurementError(
            "merge-tree exited 1 naming a merged tree but no conflicted path, so "
            "which files collide cannot be named: " + _diagnosis(proc)
        )
    return None, paths


def _is_object_name(line: str) -> bool:
    """Whether a line is a bare object name (the merged tree's).

    Both the SHA-1 (40 hex) and SHA-256 (64 hex) object formats are accepted: the
    question is the *shape* of the answer, which must not depend on the object
    format of whichever clone happens to run this.
    """
    return bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", line))


def _diagnosis(proc: subprocess.CompletedProcess[str]) -> str:
    """What git said, from both streams - a failure must not report itself as empty."""
    detail = (proc.stdout[-500:] + proc.stderr[-500:]).strip()
    return detail or f"no output (exit {proc.returncode})"


def _commit_tree(tree: str, parents: list[str], message: str) -> str:
    """Create a commit for a merged tree, so the next step can merge onto it."""
    argv = ["git", "commit-tree", tree]
    for parent in parents:
        argv += ["-p", parent]
    argv += ["-m", message]
    proc = _run(argv, env=_commit_env())
    if proc.returncode != 0:
        raise MeasurementError(f"commit-tree failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


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
    """
    existing = os.environ.get("PYTHONPATH", "")
    pinned = str(worktree) + (os.pathsep + existing if existing else "")
    return {**os.environ, "PYTHONPATH": pinned, "PYTHONDONTWRITEBYTECODE": "1"}


def _suite_verdict(tip: str, scratch: Path) -> tuple[bool, str, str]:
    """Run the repository's suite in a worktree of the planned tree.

    Returns (passed, suite summary, tree sha). The tree sha is returned and
    reported because the family's recurring defect is a verdict about a tree the
    caller was not looking at.
    """
    tree_proc = _run(["git", "rev-parse", f"{tip}^{{tree}}"])
    if tree_proc.returncode != 0:
        raise MeasurementError(f"could not read the planned tree: {tree_proc.stderr}")
    tree_sha = tree_proc.stdout.strip()

    updated = _run(["git", "update-ref", TIP_REF, tip])
    if updated.returncode != 0:
        raise MeasurementError(f"could not mark the plan tip: {updated.stderr.strip()}")
    worktree = scratch / "tree"
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
            [sys.executable, *SUITE], cwd=str(worktree), env=_suite_env(worktree)
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            return True, _last_line(out, "suite passed"), tree_sha
        if proc.returncode == 1:
            failures = [
                line.split(" ", 1)[1].strip()
                for line in out.splitlines()
                if line.startswith("FAILED ")
            ]
            if not failures and not SUITE_FAILURE.search(out):
                raise MeasurementError(_no_suite_verdict(out))
            summary = (
                "; ".join(failures[:5]) if failures else _last_line(out, "suite FAILED")
            )
            return False, summary, tree_sha
        # 2 interrupted, 3 internal error, 4 usage error, 5 no tests collected. None
        # of these is "the suite passed" - and rc 1 reaches here only with a failure
        # report in hand, since a report is what the branch above asks for.
        raise MeasurementError(
            f"the suite could not be run (rc={proc.returncode}):\n" + out[-1000:].strip()
        )
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)])
        _run(["git", "update-ref", "-d", TIP_REF])


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
                passed, summary, tree_sha = _suite_verdict(commit, Path(tmp))
        except MeasurementError as exc:
            print(f"could not measure step {step} (#{number}): {exc}", file=sys.stderr)
            return 2
        state = "OK" if passed else "FAILED"
        print(f"step {step} (#{number}) tree {tree_sha[:12]} suite {state}: {summary}")
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
    args = parser.parse_args(argv)

    try:
        # The base, in the two dimensions it can be wrong by: *when* it was read
        # and *which* ref the name denotes. Both are owned by the sibling, so both
        # are asked of it rather than reimplemented here - a plan measured against
        # a base nobody named is the defect this whole family exists to remove.
        seq._refresh_base(args.base)
        base_ref = seq._qualify_ref(args.base)
        base = _rev_parse(base_ref)
        numbers = args.prs or _open_pr_numbers(args.repo)
        heads = [(number, _fetch_head(number)) for number in numbers]
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

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
    try:
        with tempfile.TemporaryDirectory(prefix="emrg-plan-suite-") as tmp:
            passed, summary, tree_sha = _suite_verdict(tip, Path(tmp))
    except MeasurementError as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2

    print(f"final tree {tree_sha[:12]} ({tree_sha})")
    if passed:
        print(f"suite OK: {summary}")
        return 0
    print(f"suite FAILED: {summary}")
    print(
        "\nThe plan's steps are individually clean and the per-PR signals are green, "
        "but the tree they produce together fails the suite. Fix it on the merged "
        "tree and re-push the PR that owns the failure (a push voids its votes).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
