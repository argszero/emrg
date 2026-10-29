#!/usr/bin/env python3
"""Does the staged index restore the content of a commit this history already holds?

Usage
-----
    uv run --no-sync python3 scripts/check-staged-inverse.py
    uv run --no-sync python3 scripts/check-staged-inverse.py --repo <PATH>

The class this exists for
-------------------------
`git status` reports a staged path as a staged *modification*. It never reports
the modification's **direction**, and the direction is the whole danger: an index
left holding the content of an older commit is the inverse of every landing that
came after it, so `git commit` in that state silently undoes work that has
already been reviewed, voted on and merged — while `git status` shows a tidy list
of staged files and says nothing against it.

Measured on this host, 2026-10-30 (`cyc20261030-002325`):

    index tree      783fba29937fd049864f75db4e8c341b74ff8083
    b0a6f7f^{tree}  783fba29937fd049864f75db4e8c341b74ff8083
    HEAD            79eb0211   tree 5fc083ac
    staged          11 paths, 9 insertions(+), 115 deletions(-)

— the exact inverse of three commits that had landed hours earlier. Nothing in
this tree read the index at all: `scripts/` carries a guard per merge gate, and
not one of them looks at what is staged. Every occurrence so far was caught
because a cycle happened to compare two trees by hand.

What the question reduces to
----------------------------
`git write-tree` names the index's tree. A commit's tree is stored as a tree
object, so "would this index restore an earlier commit's content?" is exactly
"is the index's tree equal to the tree of a commit this repository holds?" — a
question about trees, answerable from the objects alone, with no working-tree
inspection and no diffing.

The naive form of this probe answers "clean" on a repository it could not read
------------------------------------------------------------------------------
The obvious implementation, `git write-tree` intersected with
`git log --all --format=%T`, is a false negative wherever a ref's history has a
missing object, and this repository was such a place:

    $ git log --all --format=%T > /dev/null ; echo $?
    128
    $ git log --all --format=%T | wc -l
    152

`git log --all` walks every ref, and one unhappy ref makes it exit 128 **after**
it has already printed whatever it reached. A probe that reads stdout without
reading the status takes a truncated history for the whole one and reports no
inverse. That is why the standard here is not "answer" but "answer or refuse":
a silence this tool did not measure is the one output it may not produce.

So the whole-history walk is tried first, as one process. If it fails, its
stdout is **discarded** rather than trusted, and each ref is walked on its own:
the refs that answer are measured, and the ones that do not are named in the
refusal. The two refs that were broken here —
`refs/heads/80789e97` and `refs/remotes/origin/80789e97`, two of 1028 — are what
that fallback exists for. (Their missing parent was fetched through the API and
the one-shot walk now answers in 0.25 s, which is why the fallback costs 87 s
here only on a host that really is broken.)

What it prints, and what it does not
------------------------------------
The tree it read, first, before any verdict — every guard here names its subject.
Then one of four readings:

* the index is `HEAD`'s tree → nothing is staged, exit 0;
* no commit's tree matches → the index holds content of its own, exit 0;
* a commit's tree matches → **INVERSE**, naming that commit and the staged
  paths, exit 1;
* a ref could not be walked and no match was found → **could not measure**,
  naming the refs, exit 2 — never the words "no inverse".

Exit codes: `0` clean, `1` inverse found, `2` could not measure. A match found
through the refs that did answer is still reported as a match; the unreadable
refs are then named as a caveat rather than used to withhold the finding.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

CLEAN = 0
INVERSE = 1
CANNOT_MEASURE = 2


def _git(repo: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    """One git command in `repo`, never raising: the caller reads `returncode`."""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _pairs(text: str) -> dict[str, str]:
    """`<sha> <tree>` lines → {sha: tree}, ignoring anything not in that shape."""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 40 and len(parts[1]) == 40:
            pairs[parts[0]] = parts[1]
    return pairs


def refs_of(repo: str | Path) -> list[str]:
    """Every ref name, or an empty list when the repository has none."""
    proc = _git(repo, "for-each-ref", "--format=%(refname)")
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def commit_trees(repo: str | Path) -> tuple[dict[str, str], list[str], int]:
    """{commit: tree} reachable from any ref, the refs that would not walk, and refs read.

    The one-process walk is used when it succeeds — it is the cheap reading
    (`0.25 s` measured over 3372 commits here). When it fails its stdout is
    discarded, because a failed `git log --all` has already printed the part of
    the history it reached and that output looks exactly like a whole one. The
    fallback walks each ref separately so that the unreadable ones can be named
    instead of the question being abandoned.
    """
    proc = _git(repo, "log", "--all", "--format=%H %T")
    if proc.returncode == 0:
        pairs = _pairs(proc.stdout)
        return pairs, [], len(refs_of(repo))

    pairs = {}
    broken: list[str] = []
    every = refs_of(repo)
    for ref in every:
        one = _git(repo, "log", ref, "--format=%H %T")
        if one.returncode != 0:
            broken.append(ref)
            continue
        pairs.update(_pairs(one.stdout))
    return pairs, broken, len(every) - len(broken)


def staged_paths(repo: str | Path) -> list[str]:
    """The paths the index differs from HEAD in, as `git diff --cached` spells them."""
    proc = _git(repo, "diff", "--cached", "--name-status")
    if proc.returncode != 0:
        return []
    return [line.rstrip() for line in proc.stdout.splitlines() if line.strip()]


def subjects(repo: str | Path, shas: list[str]) -> dict[str, str]:
    """`{full sha: "<short sha> <subject>"}`, so a finding names a commit a reader can recognise.

    Keyed by the full sha because that is what `commit_trees` hands back: keying by
    the abbreviated form would make every lookup miss and print the bare sha, which
    is the one output that looks like the subject never arrived.
    """
    if not shas:
        return {}
    proc = _git(repo, "log", "--no-walk", "--format=%H %s", *shas)
    if proc.returncode != 0:
        return {}
    found = {}
    for line in proc.stdout.splitlines():
        full, _, subject = line.partition(" ")
        if full:
            found[full] = f"{full[:12]} {subject}".strip()
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check whether the staged index holds the tree of a commit already in "
            "this history - the shape that makes a commit undo landed work"
        )
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="the working tree to read (default: the directory this shell stands in)",
    )
    args = parser.parse_args(argv)

    # This tool prints a `tree:` line and then may write to stderr: unbuffered, a
    # piped reader sees the refusal before the subject it is about.
    sys.stdout.reconfigure(line_buffering=True)

    start = Path(args.repo).resolve() if args.repo else Path.cwd()
    if not start.is_dir():
        print(
            f"could not measure: {start} is not a directory, so there is no working tree "
            "to read an index from",
            file=sys.stderr,
        )
        return CANNOT_MEASURE
    top_proc = _git(start, "rev-parse", "--show-toplevel")
    if top_proc.returncode != 0:
        detail = (top_proc.stderr or top_proc.stdout).strip().splitlines()
        print(
            f"could not measure: {start} is not a git working tree"
            + (f" ({detail[0]})" if detail else ""),
            file=sys.stderr,
        )
        return CANNOT_MEASURE
    top = Path(top_proc.stdout.strip())

    # Before anything else: which tree answered.
    print(f"tree: {top}")

    tree_proc = _git(top, "write-tree")
    if tree_proc.returncode != 0:
        detail = (tree_proc.stderr or "").strip().splitlines()
        print(
            "could not measure: `git write-tree` failed, so the index's tree is unknown"
            + (f" ({detail[0]})" if detail else ""),
            file=sys.stderr,
        )
        return CANNOT_MEASURE
    index_tree = tree_proc.stdout.strip()

    head_proc = _git(top, "rev-parse", "HEAD^{tree}")
    head_tree = head_proc.stdout.strip() if head_proc.returncode == 0 else None

    if head_tree is not None and index_tree == head_tree:
        print(
            f"index tree {index_tree[:12]} is HEAD's tree - nothing is staged "
            "(a working tree that differs from HEAD without being staged is not this check's subject)"
        )
        return CLEAN

    pairs, broken, refs_read = commit_trees(top)
    matches = sorted(sha for sha, tree in pairs.items() if tree == index_tree)

    if matches:
        named = subjects(top, matches)
        paths = staged_paths(top)
        print("INVERSE: the index holds the tree of a commit in this history")
        for sha in matches:
            print(f"  {named.get(sha, sha)}")
        print(f"  index tree {index_tree}")
        if paths:
            print(f"staged paths ({len(paths)}):")
            for line in paths:
                print(f"  {line}")
        print(
            "Committing this index would restore that content, undoing every landing "
            "between it and HEAD. If that is not intended:"
        )
        print("  git reset             # unstages it; the working tree keeps the content")
        print("  git reset --hard HEAD # unstages it and discards the working tree as well")
        if broken:
            print(
                f"note: {len(broken)} ref(s) could not be walked, so a twin reachable only "
                f"through them would not be listed: {', '.join(broken)}",
                file=sys.stderr,
            )
        return INVERSE

    if broken:
        for ref in broken:
            print(
                f"could not measure: {ref} could not be walked - its history names an "
                "object this clone does not have",
                file=sys.stderr,
            )
        print(
            f"could not measure: {len(broken)} of {len(broken) + refs_read} ref(s) could not be "
            "walked, so a twin reachable only through them would go unseen; "
            "'no inverse' would be a claim about a history this reading never saw",
            file=sys.stderr,
        )
        return CANNOT_MEASURE

    print(
        f"index tree {index_tree[:12]} matches no commit's tree among {len(pairs)} "
        f"commit(s) reachable from {refs_read} ref(s) - the index holds content of its own"
    )
    return CLEAN


if __name__ == "__main__":
    sys.exit(main())
