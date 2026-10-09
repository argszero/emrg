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

Half 2 reads *which* rule ignores the name, not only whether one does
-----------------------------------------------------------------------
`git check-ignore` answers from three sources at once: the repository's own
`.gitignore` files, the clone's `$GIT_DIR/info/exclude`, and the **machine's**
`core.excludesFile` (a global excludes file is a common setup). Only the first is
this guard's subject — the rule has to hold on every clone, and CI runs on a
machine with none of the other two.

Measured 2026-10-09 on master `3cdb5613`, in a detached worktree of that commit so
the working tree was never touched: with `*.orig`/`*.rej`/`*.bak` removed from the
**worktree's** copy of `.gitignore`, this file read `1 failed, 2 passed`; the same
broken tree with a global excludes file listing those three suffixes, injected
through `GIT_CONFIG_GLOBAL` naming a temp file, read **`3 passed`** — the defect
this file exists for, reported as a pass. `git check-ignore -v` names the rule that
decided, and it is the whole difference: `/tmp/…/global-ignore:1:*.orig` unpinned,
`.gitignore:55:*.orig` where the rules belong.

So `_ignoring_rule` asks for the source and the assertion requires it to be a file
**this repository tracks**: a machine-global or `info/exclude` match is then
reported as the failure it is, and a nested `sub/.gitignore` — the repository's own
rules too — still passes. The same read in the two sibling carriers
(`tests/test_scratch_roots_are_gitignored.py::_unignored`, and
`tests/test_local_exclude.py`, where a machine-global `.emrg/` reddens 5 of its
tests against a healthy tree) is issue #1973's measured boundary, not fixed here.

A comment cannot enforce either half, which is why the rule is a test: the
`.gitignore` entry states the intent, and this file is the mechanic that keeps a
new site — a new suffix, or a reverted ignore rule — from reintroducing it.
"""

from __future__ import annotations

import os
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


def _git(
    *args: str, repo: Path = REPO_ROOT, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run git in `repo` — this checkout by default, a scratch repository when a test passes one.

    `env=` replaces the child's environment whole, so a caller that redirects git's
    configuration has to hand over `os.environ` with the change applied.

    `encoding=` is pinned: the child emits UTF-8 whatever the host locale is, and the
    locale codec cannot decode it (the class
    `tests/test_script_decode_is_locale_independent.py` keeps out of `scripts/`).
    """
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _tracked_paths(repo: Path = REPO_ROOT) -> list[str]:
    """The index's paths: what a rule has to belong to before it is this repository's own.

    Half 1 reads the same list, and its emptiness is asserted there rather than here,
    because a scan over nothing is the failure this file reports and not a helper's.
    """
    result = _git("ls-files", repo=repo)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def _ignoring_rule(repo: Path, name: str, env: dict[str, str] | None = None) -> str | None:
    """The file whose rule ignores `name`, or `None` when no rule does.

    `git check-ignore -q` answers only yes/no, and yes is also what a machine-global
    excludes file (`core.excludesFile`) or a clone's `$GIT_DIR/info/exclude` says —
    neither is this guard's subject. `-v` prints `<source>:<lineno>:<pattern>` as its
    first tab-separated field, and exactly one line, the rule that decided; the source
    is taken from the right, since a path may carry a colon of its own on Windows.
    """
    result = _git("check-ignore", "-v", "--no-index", "--", name, repo=repo, env=env)
    if result.returncode != 0:
        return None
    return result.stdout.split("\t", 1)[0].rsplit(":", 2)[0]


def test_the_scan_recognises_the_shape_it_exists_for() -> None:
    """Positive control: a scan that sees nothing must not pass as clean."""
    assert looks_like_a_patch_backup(MEASURED_OFFENDER)
    assert looks_like_a_patch_backup("x.rej")
    assert not looks_like_a_patch_backup("emrg/tools/bash_tool.py")
    assert not looks_like_a_patch_backup("tests/test_patch_backups_are_gitignored.py")


def test_no_tracked_path_is_a_patch_backup() -> None:
    """The index carries no `patch` backup — the state CI can actually check."""
    tracked = _tracked_paths()
    assert tracked, "`git ls-files` returned nothing: this scan measured nothing"
    offenders = sorted(p for p in tracked if looks_like_a_patch_backup(p))
    assert offenders == [], (
        "a patch backup is tracked: "
        + ", ".join(offenders)
        + " — `git rm` it and leave the ignore rules in `.gitignore` to do the work"
    )


def test_git_ignores_a_new_patch_backup() -> None:
    """`git add -A` must not be able to stage one of these names — and the rule that stops it
    has to be one **this repository** carries, not one this machine's git configuration supplies.
    """
    owned = set(_tracked_paths())
    assert owned, "`git ls-files` returned nothing: this scan measured nothing"
    for name in (
        MEASURED_OFFENDER,
        "tests/some_module.py.rej",
        "emrg/tools/bash_tool.py.bak",
    ):
        rule = _ignoring_rule(REPO_ROOT, name)
        assert rule is not None, (
            f"{name} is not ignored, so `git add -A` would stage it: "
            "add the suffix to `.gitignore`"
        )
        assert rule in owned, (
            f"{name} is ignored by {rule}, which this repository does not track — only its own "
            "rules count here. A machine-global excludes file (`core.excludesFile`, a common "
            "setup) or this clone's `.git/info/exclude` answers the same question for the same "
            "name, so a `.gitignore` that has lost the rule reads green here and on another "
            "clone `git add -A` stages the backup. The measurement behind this is in the module "
            "docstring."
        )


def test_the_ignoring_rule_is_read_from_the_repository_and_not_from_the_machine(
    tmp_path: Path,
) -> None:
    """The `-v` source is load-bearing: a machine-global rule must not read as the repository's.

    Driven against a scratch repository, so this checkout's rules are untouched, and with git's
    configuration redirected by environment variable to files under `tmp_path` — no host file is
    read or written. Both halves of the assertion above are exercised: the same name, ignored by
    the machine's rule alone and then by the repository's, so the second half also shows what
    `rule in owned` needs in order to be satisfiable at all.
    """
    repo = tmp_path / "scratch"
    repo.mkdir()
    _git("init", "-q", ".", repo=repo)
    # A tracked file that is not an ignore rule, so the machine rule below is rejected for what
    # it is rather than because this repository happens to track nothing.
    (repo / "unrelated.txt").write_text("", encoding="utf-8")

    excludes = tmp_path / "global-ignore"
    excludes.write_text("*.orig\n", encoding="utf-8")
    machine_config = tmp_path / "gitconfig"
    #: Written by `git config --file`, never by hand. A config file's value goes through git's
    #: own parser, and a hand-written **Windows** path is a syntax error for it — `\U` and `\A`
    #: are escapes — so git exits 128 with `fatal: bad config line 2`, the injected excludes
    #: file never takes effect, and the premise assertion below reddens. Measured on this test's
    #: own `test-windows` leg (run 37870491979, head `26719a7f`) and reproduced locally: the
    #: hand-written form exits 128, while `git config --file` writes
    #: `core.excludesFile = C:\\Users\\…` — the escaping that survives its own parser — and reads
    #: back the original value with rc 0. The sibling `tests/test_dependency_dirs_are_gitignored.py`
    #: was fixed the same way for the same reason.
    _git("config", "--file", str(machine_config), "core.excludesFile", str(excludes))
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(machine_config), "GIT_CONFIG_NOSYSTEM": "1"}
    _git("add", "unrelated.txt", repo=repo, env=env)

    machine_rule = _ignoring_rule(repo, "some_module.py.orig", env=env)
    assert machine_rule is not None, "the injected excludes file did not take effect"
    assert Path(machine_rule).name == "global-ignore", machine_rule
    assert machine_rule not in _tracked_paths(repo), (
        "a machine-global rule reads as this repository's own, which is the defect this file "
        "pins"
    )

    # The same name, now carrying the repository's rule — which outranks the machine's, so the
    # probe names it and the acceptance above is met.
    (repo / ".gitignore").write_text("*.orig\n", encoding="utf-8")
    _git("add", ".gitignore", repo=repo, env=env)
    repository_rule = _ignoring_rule(repo, "some_module.py.orig", env=env)
    assert repository_rule is not None, "the repository's own rule stopped matching"
    assert Path(repository_rule).name == ".gitignore", repository_rule
    assert repository_rule in _tracked_paths(repo), (
        f"{repository_rule} is not among the tracked paths, so the assertion above could not be "
        "satisfied by any rule"
    )

