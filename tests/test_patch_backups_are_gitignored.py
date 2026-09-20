"""Guard: a `patch` backup must be ignored by git, and never tracked.

The exposure, measured in this repository
-----------------------------------------
`patch` writes `<file>.orig` beside its target, and `.rej` for a hunk it could not
apply. A cycle that applies a pending diff by hand therefore leaves one behind
without ever deciding to, and `git add -A` stages it like any other new file.
That is not hypothetical here: two branches of one stack committed
`emrg/tools/bash_tool.py.orig` — **349,773 bytes / 6218 lines** of a duplicated
file under edit, on `fix/fd-prefixed-redirect-operand` (head `37013d50`) and on
`fix/input-redirect-operand` (head `e514c0d9`, a branch stacked on the first), so
both PR diffs carried the whole copy and a reviewer had to notice it by eye. No
`.gitignore` rule and no guard named the shape.

Both halves of the rule, deliberately different in kind
------------------------------------------------------
1. **Nothing tracked may look like a patch backup.** Read from `git ls-files` —
   the index, not the worktree: an *untracked* backup is exactly what half 2 is
   for, and asking the worktree would conflate the two.
2. **Git must ignore those names.** Asked of git directly, for a synthesised
   name (`git check-ignore`, which answers for paths that do not exist), the way
   `tests/test_scratch_roots_are_gitignored.py` asks it for scratch roots. Half 1
   can only catch a backup that got in; half 2 is what stops `git add -A` from
   putting one in.

A comment cannot enforce either half, which is why the rule is a test: the
`.gitignore` entry states the intent, and this file is the mechanic that keeps a
new site — a new suffix, or a reverted ignore rule — from reintroducing it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The suffixes `patch` itself writes, plus the conventional `.bak` an editor
# leaves. Kept as one tuple so the positive control below reads the same source
# the assertions do.
PATCH_BACKUP_SUFFIXES = (".orig", ".rej", ".bak")

# The shape this file exists for, named so the scan cannot be vacuous: if the
# suffix list ever stops recognising it, the control fails rather than the scan
# silently passing over an empty class.
MEASURED_OFFENDER = "emrg/tools/bash_tool.py.orig"


def looks_like_a_patch_backup(path: str) -> bool:
    """Is this path a backup an edit leaves, rather than a deliverable?"""
    return path.endswith(PATCH_BACKUP_SUFFIXES)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    # `encoding=` is pinned: the child emits UTF-8 whatever the host locale is,
    # and the locale codec cannot decode it (the class
    # `tests/test_script_decode_is_locale_independent.py` keeps out of `scripts/`).
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_the_scan_recognises_the_shape_it_exists_for() -> None:
    """Positive control: a scan that sees nothing must not pass as clean."""
    assert looks_like_a_patch_backup(MEASURED_OFFENDER)
    assert looks_like_a_patch_backup("x.rej")
    assert not looks_like_a_patch_backup("emrg/tools/bash_tool.py")
    assert not looks_like_a_patch_backup("tests/test_patch_backups_are_gitignored.py")


def test_no_tracked_path_is_a_patch_backup() -> None:
    """The index carries no `patch` backup — the state CI can actually check."""
    result = _git("ls-files")
    assert result.returncode == 0, result.stderr
    tracked = result.stdout.splitlines()
    assert tracked, "`git ls-files` returned nothing: this scan measured nothing"
    offenders = sorted(p for p in tracked if looks_like_a_patch_backup(p))
    assert offenders == [], (
        "a patch backup is tracked: "
        + ", ".join(offenders)
        + " — `git rm` it and leave the ignore rules in `.gitignore` to do the work"
    )


def test_git_ignores_a_new_patch_backup() -> None:
    """`git add -A` must not be able to stage one of these names."""
    for name in (
        MEASURED_OFFENDER,
        "tests/some_module.py.rej",
        "emrg/tools/bash_tool.py.bak",
    ):
        result = _git("check-ignore", "-q", "--no-index", name)
        assert result.returncode == 0, (
            f"{name} is not ignored, so `git add -A` would stage it: "
            "add the suffix to `.gitignore`"
        )
