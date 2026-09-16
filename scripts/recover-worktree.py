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
  clean *and* keeps every byte in the stash, so the action is undoable — with
  `git stash apply --index stash^{/<stash message>}`, the spelling the receipt
  names, and *not* with a bare `git stash pop` (it takes the newest stash, brings a
  staged change back unstaged, and consumes the stash; issue #1284). **No branch is
  reset and no commit is dropped** — this never moves `HEAD`.

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
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.server.scheduler import TaskHandler, recovery_recipe  # noqa: E402  (needs the path above)


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


def _receipt_recipe(receipt: str | None, detail: str) -> str | None:
    """This run's reversal recipe, read from the receipt it wrote; None if unusable.

    The recipe is not restated here — it is the action's own string, so the manual
    route and the daemon cannot drift apart about how to undo the move (issue #1284).

    `detail` is what makes "this run's" checkable rather than assumed: the receipt's
    own `stash_message` has to appear in the detail the action just returned. A
    receipt left over from an earlier recovery names a *different* stash, and a reader
    who follows it would apply the wrong one — worse than the paraphrase this replaced.
    """
    if not receipt or not os.path.isfile(receipt):
        return None
    try:
        with open(receipt, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    message = data.get("stash_message")
    recipe = data.get("reversible_with")
    if not message or not isinstance(recipe, str) or message not in detail:
        return None
    return recipe


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
    # One owner for the spelling too (issue #1284): the receipt's `reversible_with` is
    # the recipe, so it is *printed* rather than paraphrased. The two used to disagree
    # — the receipt said `apply --index`, this line said a bare `git stash pop` — and
    # the spelling a reader saw here is the one that costs them the staged side and the
    # stash itself. `None` means the receipt is unreadable or belongs to an earlier
    # recovery, and there is then no *this run's* message to name, so the fallback
    # prints the same recipe with the placeholder the reader substitutes from
    # `git stash list` — the same function, not a third copy of it.
    recipe = _receipt_recipe(receipt, detail)
    if recipe:
        print(f"reversible: {recipe}")
    else:
        print(f"reversible: {recovery_recipe('<message>')}")
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
