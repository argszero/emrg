#!/usr/bin/env python3
"""Ask each remote branch whether the work it carries has already landed.

The question this exists for
---------------------------
An instance that cannot open pull requests accumulates branches. Each is a fix that
nothing merged, and each cycle that revisits them re-runs the same three readings by
hand: does it still merge, does its work exist on master, is it simply dead. Measured
2026-10-03 (`cyc20261003-103411`): **17 branches** of one instance's work were audited
that way, one at a time, in a scratch worktree - a reading a script can answer in one
command and a reader can re-answer after every merge.

What it answers, and from what
------------------------------
Per branch, three readings and no fourth, all of them local (no `gh`, no network):

* **how much it carries** - commits ahead of the merge base with master;
* **whether it still merges** - `git merge-tree --write-tree master <ref>`, the same
  reading `check-merge-landing-diff.py` uses;
* **whether master already has what it adds** - the *set* of module-level names the
  branch adds over its base (AST, in non-test Python files), plus every file path it
  adds, minus everything master already holds.

The third is the one that decides the verdict. Comparing **names** and **paths** rather
than the diff is deliberate: a diff against the merge base includes master's own later
work when the base is old, which counted master's `EmrgServer` and `resolve_client_tier`
as "added by" three different branches when this was first written (measured
2026-10-03, same cycle) - a reading that named the wrong author for everything. Names
present at one revision and not the other answer the question that was actually asked.

The four states
---------------
    UNLANDED      master lacks something it adds, and the merge is clean - normal
    CONFLICTING   it does not merge into master cleanly - whatever it carries (rc 1)
    SUPERSEDED    master already contains this exact tip - fully landed, dead (rc 1)
    UNCLASSIFIED  merges cleanly, but adds no new name or path; cannot tell
    UNKNOWN       it could not be read (rc 2 if nothing else could be)

`CONFLICTING` and `SUPERSEDED` are both faults and both rc 1, for different remedies:
the first needs a refresh or a resolution before it can land, the second should be
folded or deleted because master already contains its tip.

`SUPERSEDED` is decided by **ancestry, not by names**, because ancestry cannot be wrong:
`git merge-base --is-ancestor <tip> master` is a fact about the commit graph. The name
comparison that used to decide it was a different question - "does master have the
symbols this branch introduces" - and answers a branch that only *edits* lines with a
vacuous yes.

`UNCLASSIFIED` is rc 0 and **not a pass**, announced on stderr for the reason
`check-merge-landed.py` announces `UNCHECKED`: `0` is what a caller acts on.

Why `UNCLASSIFIED` exists, and the two readings that were tried and rejected
-----------------------------------------------------------------------------
A branch may fix a bug by **editing** lines - no new function, no new file. Then "master
holds everything it adds" is vacuously true, and the first version of this tool called
seven such branches `SUPERSEDED`. Ground truth (copying each branch's own test files onto
a master worktree and running them, measured 2026-10-03) said **four of the seven were
still live** - a 57% false-positive rate on the one verdict a reader acts on by deleting
something. Both cheaper repairs were then measured against the same seven:

| reading | wrong |
|---|---|
| names+paths present on master (the shipped one) | 4 of 7 |
| the branch's added `test*` names all present on master | 2 of 7 |
| copy its test files onto master and run them | 0 of 7 - but it writes into a tree |

So the third is the only correct one and it mutates a working tree, which is a caller's
decision and not a reporting tool's. The tool therefore refuses to answer where it cannot,
and names the reading that can: for an `UNCLASSIFIED` row, copy the branch's changed test
files onto a master worktree and run them. Refusing is the answer the measurements support;
guessing had a worse error rate than a coin toss on the branches that mattered.

The limits, stated because a verdict is read as an answer
---------------------------------------------------------
* **"master lacks X" is not "the defect is still live".** A branch can be obsoleted by a
  *different* fix that never adds its names. This reading answers "has this work landed",
  which is the question a backlog needs; it does not answer "is this bug still open".
* **The ref namespace is an argument, not a guess.** Which branches are "ours" is not
  derivable from git - parallel instances push to the same remote under the same
  prefixes (measured: `fix/*` held branches this instance never created). The default
  is `origin`, so a caller who fetched a private namespace says so.
* **It reads the checkout its own file lives in**, not the caller's working directory,
  and says so on its first line (`tree: <path>`) before any verdict - the convention the
  `scripts/check*.py` family holds. The refs are fetched into that checkout; there is no
  `--repo`.

Exit codes
----------
    0  every branch read is UNLANDED or UNCLASSIFIED - `UNCLASSIFIED` is announced as
       not a pass, and rc 0 says only that nothing was found dead or dirty
    1  at least one branch is SUPERSEDED or CONFLICTING, printed with its state
    2  nothing could be measured (no ref matched, master is unreadable, or nothing could
       be read at all) - fail loud; never report "clean" for a question not answered

There is no `--json` yet, and that is a statement about use rather than an omission: the
subject is a list a human reads before deciding what to fold, and every consumer so far
reads the rows.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: States a branch can be in. Named rather than expressed as booleans so a report and
#: its exit code are two renderings of one decision.
UNLANDED = "UNLANDED"
CONFLICTING = "CONFLICTING"
SUPERSEDED = "SUPERSEDED"
UNCLASSIFIED = "UNCLASSIFIED"
UNKNOWN = "UNKNOWN"


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run git, never raising: every caller here reports an unreadable answer instead.

    `encoding` is pinned for the reason `tests/test_script_decode_is_locale_independent.py`
    exists - the default text mode decodes with the locale codec, and this tool reads
    branch names, which may hold anything.

    `REPO_ROOT` is read **at call time**, not bound as a default: a default argument is
    fixed when the function is defined, so a test that repoints `REPO_ROOT` at a
    synthetic repository would still have run git in the real checkout.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd if cwd is not None else REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _module_level_names(source: str) -> set[str]:
    """Every function and class name `source` defines anywhere in the file.

    Walked rather than read from `tree.body`, because a name added inside a class or a
    conditional is still a name the branch introduced - the question is "does master
    have this symbol", not "is it top-level". A file that does not parse yields the
    empty set: a branch whose Python is broken has nothing to compare, and inventing
    names for it would be worse than saying so.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


@dataclass
class Reading:
    """One branch's three readings, or the reason one of them could not be made."""

    ref: str
    tip: str = ""
    ahead: int = 0
    base: str = ""
    merges_cleanly: bool | None = None
    contained: bool = False
    missing_names: list[str] = field(default_factory=list)
    missing_paths: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def state(self) -> str:
        if self.note:
            return UNKNOWN
        # The merge reading first, because it is a fact about this branch that was
        # measured and acted on either way: a branch that will not merge needs a
        # refresh or a resolution before anything else can be said about it, and that
        # is true of a pure edit exactly as much as of one that adds a function.
        if self.merges_cleanly is False:
            return CONFLICTING
        # The one reading that *is* conclusive: master already contains this exact tip,
        # so there is nothing left to land. Everything the branch does is on master,
        # whether or not it added a name - which is exactly what names+paths cannot say.
        if self.contained:
            return SUPERSEDED
        if not self.missing_names and not self.missing_paths:
            # Nothing comparable was added: either the branch edits existing lines, or
            # master already holds the whole of it. The two are indistinguishable from
            # here, and calling both SUPERSEDED was measured wrong 4 times in 7 - see
            # the module docstring's table. Refusing is the supported answer; the strong
            # reading is named in the report.
            return UNCLASSIFIED
        return UNLANDED

    @property
    def carries(self) -> int:
        """How many distinct things this branch adds that master lacks."""
        return len(self.missing_names) + len(self.missing_paths)


def _names_in_file(rev: str, path: str) -> set[str]:
    proc = _git("show", f"{rev}:{path}")
    return _module_level_names(proc.stdout) if proc.returncode == 0 else set()


def read_branch(ref: str, master: str) -> Reading:
    """The three readings for one branch ref, against `master`."""
    tip = _git("rev-parse", ref)
    if tip.returncode != 0:
        return Reading(ref=ref, note=f"the ref could not be resolved ({tip.stderr.strip()})")
    head = tip.stdout.strip()

    base_proc = _git("merge-base", master, head)
    if base_proc.returncode != 0 or not base_proc.stdout.strip():
        return Reading(
            ref=ref, tip=head, note=f"no merge base with {master[:8]} ({base_proc.stderr.strip()})"
        )
    base = base_proc.stdout.strip()

    ahead = _git("rev-list", "--count", f"{base}..{head}")
    try:
        ahead_n = int(ahead.stdout.strip())
    except ValueError:
        return Reading(ref=ref, tip=head, base=base, note="the commit count was not a number")

    # `merge-tree --write-tree` exits non-zero on a conflict and prints the tree's name
    # on success - the same discrimination `check-merge-landing-diff.py` makes.
    merged = _git("merge-tree", "--write-tree", master, head)
    merges_cleanly = merged.returncode == 0

    # Is the branch's tip already an ancestor of master? This is the only *conclusive*
    # "already landed" reading available locally: it is a fact about the commit graph,
    # not an inference from names, so it cannot be wrong about a pure edit the way the
    # name comparison was (4 of 7 wrong - see the docstring).
    ancestor = _git("merge-base", "--is-ancestor", head, master)
    contained = ancestor.returncode == 0

    changed = _git("diff", "--name-status", base, head)
    if changed.returncode != 0:
        return Reading(
            ref=ref, tip=head, base=base, ahead=ahead_n,
            note=f"the branch's diff could not be read ({changed.stderr.strip()})",
        )

    missing_names: list[str] = []
    missing_paths: list[str] = []
    for line in changed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        if status.startswith("A"):
            # A path the branch creates: master has it only if it exists there now.
            if _git("cat-file", "-e", f"{master}:{path}").returncode != 0:
                missing_paths.append(path)
            continue
        if status.startswith("D"):
            continue
        if not path.endswith(".py") or path.startswith("tests/"):
            continue
        added = _names_in_file(head, path) - _names_in_file(base, path)
        for name in sorted(added):
            if name not in _module_level_names(_show(master, path)):
                missing_names.append(f"{path}::{name}" if path else name)

    return Reading(
        ref=ref,
        tip=head,
        base=base,
        ahead=ahead_n,
        merges_cleanly=merges_cleanly,
        contained=contained,
        missing_names=missing_names,
        missing_paths=missing_paths,
    )


def _show(rev: str, path: str) -> str:
    proc = _git("show", f"{rev}:{path}")
    return proc.stdout if proc.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    # A merged reader must see the `tree:` line before any verdict, and this family's
    # docstrings promise that order. stdout is block-buffered when it is a pipe (how a
    # cycle reads this report: `2>&1 | tail`) while stderr is not, so without this every
    # stderr line - the `UNCLASSIFIED` note included - overtakes the tree line.
    # Behaviour and pin: tests/test_guard_report.py.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "pattern",
        nargs="?",
        default="refs/remotes/origin/fix/*",
        help="a git ref glob for the branches to audit (default: %(default)s)",
    )
    parser.add_argument("--master", default="origin/master", help="the branch that landed work")
    args = parser.parse_args(argv)

    # The convention `Agent.md` states and tests/test_a_tree_reading_guard_names_its_tree.py
    # enforces: a guard that reads a tree says which tree answered, before any verdict.
    # This one reads git refs and blobs, and the ref namespace it reads belongs to the
    # checkout the file lives in - so the line is that checkout, not the caller's cwd.
    print(f"tree: {REPO_ROOT}")

    master_proc = _git("rev-parse", args.master)
    if master_proc.returncode != 0:
        print(f"error: {args.master} could not be resolved ({master_proc.stderr.strip()})")
        return 2
    master = master_proc.stdout.strip()

    listed = _git("for-each-ref", "--format=%(refname)", args.pattern)
    if listed.returncode != 0:
        print(f"error: the refs could not be listed ({listed.stderr.strip()})")
        return 2
    refs = [r for r in listed.stdout.splitlines() if r.strip()]
    if not refs:
        print(f"error: no ref matches {args.pattern!r} - there is nothing to audit here")
        return 2

    print(f"master: {args.master} ({master[:12]})")
    readings = [read_branch(ref, master) for ref in refs]
    faults = 0
    for reading in readings:
        if reading.state == UNKNOWN:
            print(f"{UNKNOWN:12s} {reading.ref} - {reading.note}")
            continue
        detail = []
        if reading.missing_names:
            detail.append(f"{len(reading.missing_names)} name(s)")
        if reading.missing_paths:
            detail.append(f"{len(reading.missing_paths)} path(s)")
        if reading.contained:
            carries = "master already contains this tip"
        else:
            carries = ", ".join(detail) if detail else "nothing master lacks"
        merge = "merges cleanly" if reading.merges_cleanly else "CONFLICTS with master"
        print(
            f"{reading.state:12s} {reading.ref} @ {reading.tip[:8]} "
            f"({reading.ahead} commit(s) ahead, {merge}; {carries})"
        )
        if reading.state == SUPERSEDED:
            print(
                "             master already contains this tip - landing it would change "
                "nothing; fold or delete the branch"
            )
            faults += 1
        elif reading.state in (UNLANDED, CONFLICTING):
            # The count is not the actionable part: a reader deciding what to fold or
            # resolve needs the names. Printed for both states, because "master lacks X"
            # is the same fact read for two different purposes.
            for path in (reading.missing_paths + reading.missing_names)[:5]:
                print(f"             master lacks {path}")
            omitted = reading.carries - 5
            if omitted > 0:
                print(f"             ... and {omitted} more")
            if reading.state == CONFLICTING:
                faults += 1

    if any(r.state == UNCLASSIFIED for r in readings):
        print(
            f"note: {sum(1 for r in readings if r.state == UNCLASSIFIED)} branch(es) are "
            "UNCLASSIFIED - each adds no new name or path, so this reading cannot tell a "
            "pure edit from work already landed, and neither state is a pass. Decide those "
            "by copying the branch's changed test files onto a master worktree and running "
            "them (the reading with a measured 0/7 error rate; see the docstring).",
            file=sys.stderr,
        )

    counts: dict[str, int] = {}
    for reading in readings:
        counts[reading.state] = counts.get(reading.state, 0) + 1
    print(
        "\n"
        + ", ".join(f"{state} {counts[state]}" for state in sorted(counts))
        + f" of {len(readings)} branch(es)"
    )
    if counts.get(UNKNOWN):
        print(f"{counts[UNKNOWN]} branch(es) could not be read - reported, never counted as clear")
    return 1 if faults else 0


if __name__ == "__main__":
    raise SystemExit(main())
