#!/usr/bin/env python3
"""Recover a working tree whose dirt is **reconstructible** (issue #1237).

Why this exists
---------------
`scheduler._effective_sandbox` forces a cycle whose source tree is dirty down to
`read-only`, and `read-only` refuses every git verb that could clean it: measured
`checkout`/`reset`/`restore`/`switch`/`merge`/`stash`/`update-ref`/`symbolic-ref`/
`commit`/`push` = **10/10 BLOCK**. The state that triggered the downgrade therefore
disabled the only exit from it, and the cost was measured: **33 consecutive
zero-commit cycles**, with 40+ verified patches stranded outside the repository.

The criterion that makes acting decidable is *"would anything be lost?"*, not *"is
the tree dirty?"* — the two differ, and they differed expensively: of the dirt in
that stretch, git reported 2 modified files and **both were byte-identical to
upstream's own blobs**, so discarding them lost nothing at all.
`scheduler._dirty_tree_would_lose_work_sync` answers that question, and
`scheduler._recover_dirty_tree_sync` acts on it: a reconstructible tree is now
converged by the daemon itself at the start of a cycle, so the deadlock is not
something a human has to be present to break. This command is the **manual** route
— the same action, from a shell, for a tree no cycle is about to touch.

What it does
------------
Diagnose, then — only with `--apply` — converge, through the one implementation the
daemon also uses (this script owns no policy; if it disagreed with the guard about
what "reconstructible" means, the deadlock would come back through the script):

* the tree is already clean -> nothing to do;
* the dirt holds work that exists nowhere else -> **refuse**, name the paths, and
  touch nothing. Committing, stashing or copying that work out is a decision for
  whoever wrote it, and an agent that discards it is doing the thing the guard
  exists to prevent;
* the dirt is reconstructible -> `git stash push -u` it, which leaves the worktree
  clean *and* keeps every byte in the stash, so the action is undoable with
  `git stash pop`. **No branch is reset and no commit is dropped** — this never
  moves `HEAD`.

Every `--apply` writes a receipt into the **git state dir** (see
`scheduler._git_state_dir` — a location that cannot dirty the tree it just cleaned),
as the structural guard's contract requires of every release of a safety rule. It is
best-effort, so the stdout and the action's detail *say so* when one could not be
written rather than naming a path for a file that does not exist (issue #1284).

Exit codes
----------
``0``  nothing to do, or the tree was converged and verified (also: a dry run of a
       tree that *would* converge). ``1``  refused: the tree holds work found
       nowhere else, nothing was written. ``2``  the question could not be
       answered (not a git repo, an unreadable state) — never reported as a pass.

Usage
-----
    uv run --no-sync python scripts/recover-worktree.py --repo <dir>
    uv run --no-sync python scripts/recover-worktree.py --repo <dir> --apply
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.server.scheduler import TaskHandler  # noqa: E402  (needs the path above)


def _git(repo: Path, *args: str, timeout: int = 30):
    """Run git in `repo`. `-C` rather than chdir so the caller's cwd is untouched."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


def _status_lines(repo: Path) -> list[str] | None:
    """`git status --porcelain` lines, or None when it could not be read."""
    out = _git(repo, "status", "--porcelain", "--untracked-files=normal")
    if out.returncode != 0:
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def _rev(repo: Path, ref: str) -> str | None:
    out = _git(repo, "rev-parse", "--verify", "--quiet", ref)
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return out.stdout.strip()


def _receipt_path(repo: Path) -> str | None:
    """Where the daemon's own recovery writes its receipt, or None."""
    state = TaskHandler._git_state_dir(str(repo))
    return None if state is None else os.path.join(state, "emrg-recovery-receipt.json")


def recover(repo: Path, apply: bool) -> int:
    """Diagnose `repo`, and converge it when `apply` and it is safe. Returns an exit code."""
    if _rev(repo, "HEAD") is None:
        print(f"could not measure: {repo} is not a git repository with a commit")
        return 2

    entries = _status_lines(repo)
    if entries is None:
        print(f"could not measure: `git status` failed in {repo}")
        return 2
    if not entries:
        print(f"clean: {repo} has no uncommitted changes; nothing to recover")
        return 0

    # One owner for the criterion: the same question the guard asks, so the tool
    # and the tier decision can never disagree about what "reconstructible" means.
    loses, why = TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    if loses:
        print(f"refused: {repo} holds work that exists nowhere else: {why}")
        print("nothing was changed. Commit, stash or copy that work out first;")
        print("this tool will not discard it.")
        return 1

    if not apply:
        print(f"recoverable: {why}")
        print(f"{len(entries)} entr(y|ies) would be stashed (reversible); re-run with --apply")
        return 0

    # One owner for the action too: the daemon runs this exact function at the start
    # of a cycle, so what a human runs here and what the guard does cannot drift. It
    # accepts no verdict from a caller and re-measures the criterion itself, so a tree
    # that changed between the diagnosis above and this line is judged on its current
    # state rather than on the earlier answer.
    status, detail = TaskHandler._recover_dirty_tree_sync(str(repo))
    if status == "refused":
        print(f"refused: {repo} holds work that exists nowhere else: {detail}")
        print("nothing was changed. Commit, stash or copy that work out first;")
        print("this tool will not discard it.")
        return 1
    if status == "error":
        print(f"could not measure: {detail}")
        return 2
    if status == "clean":
        print(f"clean: {repo} has no uncommitted changes; nothing to recover")
        return 0

    receipt = _receipt_path(repo)
    print(f"recovered: {repo} converged to a clean tree; {detail}")
    print("reversible: `git stash list` -> the named stash (a bare `git stash pop`")
    print("takes the newest, which is this one only until the next stash is made)")
    # The path is not the receipt (issue #1284): `_receipt_path` computes where one
    # *would* be written, so the branch below used to be unreachable — it printed a
    # path for a file that an `OSError` had kept from existing. Ask the file — and ask
    # whether it is a *file*, because "something exists at this path" is also true of
    # the directory that the write failed against (the forced failure in the test).
    if receipt and os.path.isfile(receipt):
        print(f"receipt: {receipt}")
    else:
        print("receipt: could not be written")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Converge a working tree whose uncommitted changes are "
                    "reconstructible, reversibly, with a receipt.",
    )
    parser.add_argument("--repo", default=".", help="repository to inspect (default: .)")
    parser.add_argument(
        "--apply", action="store_true",
        help="perform the recovery; without it the tool only reports",
    )
    args = parser.parse_args(argv)
    return recover(Path(args.repo).resolve(), args.apply)


if __name__ == "__main__":
    sys.exit(main())
