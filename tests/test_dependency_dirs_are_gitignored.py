"""Guard: the two dependency names the merge gate's own remedy links are ignored in *every* shape.

The exposure, measured 2026-10-09 on master `c9d7f4b7`
-----------------------------------------------------
`scripts/check-merge-plan-suite.py` prints, for a kept landing-tree worktree, the two remedies
that make the GUI and Python suites runnable there (`_kept_note`, the note's last two lines):

    node:   ln -sfn <main>/emrg/gui/node_modules <worktree>/emrg/gui/node_modules
            ln -sfn <main>/.venv               <worktree>/.venv

So the gate itself instructs the operator to create a **symlink** named `node_modules` and one
named `.venv`. `.gitignore` carried both names *with a trailing slash* (`node_modules/`,
`.venv/`), and a gitignore rule `<name>/` matches **directories only** — a symlink, or a plain
file, of that name is not matched at all. Measured in a scratch repository carrying those two
rules: `emrg/gui/node_modules` as a real directory was ignored, as a symlink was not, and the
same held for `.venv`. The consequence is not cosmetic — `git status --porcelain` lists the
link as `??`, so `git add -A` (which the merge gate's own `_branch_with` runs on a rebuilt
branch) stages it, it lands in a commit, and the first symptom is an unrelated test reddening:
the tracked-file scan reads the link's target and reports

    ... could not be read as UTF-8 text ([Errno 21] Is a directory

costing a cycle the time to find the *real* cause. This is the same trailing-slash lesson
`.gitignore`'s own `.emrg-*` block records (#1119, how2how2how2-arch): `.emrg-*/` missed the
plain file, `.emrg-*` covers both forms. The two rules now read `node_modules` and `.venv`.

Why a test rather than the comment
----------------------------------
A comment cannot fail. `.gitignore` says why each rule is shaped the way it is; this file is
the mechanic that keeps a later edit from re-adding the slash, and the same relation the two
sibling guards hold to their rules (`tests/test_patch_backups_are_gitignored.py`,
`tests/test_scratch_roots_are_gitignored.py` — both cited from the blocks they pin).

The rule
--------
For each of the two names, the repository's **own** `.gitignore` must make git ignore the name
in the shapes a worktree can take, and must **not** ignore a name the rules were never about.
The subject is the repository's real file, read here and applied in a scratch repository; the
shapes are real (`Path.mkdir`, `Path.write_text`, `Path.symlink_to`), not synthesised strings,
so nothing depends on how a future author spells the rule — only on what git does with it.
Because the check is behavioural, it also covers the return of the slash and any other
narrowing: a rule that stops matching anywhere those files land fails it.

Two honest boundaries, stated rather than implied:
* the symlink shape needs a privilege Windows may withhold (the arm in
  `tests/test_bash_v2_policy.py` skips for the same reason). It lives in **its own test**, so
  that skip cannot swallow the directory and file shapes — those carry the same defect, need no
  privilege, and run on every leg, which is what keeps a Windows reading a real one;
* "ignored" is read from `git check-ignore -q`'s exit code (0 matched, 1 unmatched), which
  answers for paths that exist and for ones that do not. The control arm in each test is what
  proves this instrument discriminates, since `True` is also what a broken call returns.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The two names the merge gate's note tells an operator to link into a worktree
#: (`scripts/check-merge-plan-suite.py`, `_kept_note`), and the two `.gitignore` rules this
#: file pins. Named here rather than derived from that printed note: the note is a stream of
#: `print` calls whose shape is not a contract, and a guard that parsed it would fail on a
#: rewording while the ignore rules were unchanged.
NAMES = ("node_modules", ".venv")

#: Where the gate's remedy prints each link, relative to the worktree root. A wrong nesting
#: would not matter to a slashless rule, but it does to a *slashed* one's successor, and these
#: are the paths an operator really creates.
LINK_PATHS = {"node_modules": "emrg/gui/node_modules", ".venv": ".venv"}


def _scratch_with_repository_rules(tmp_path: Path) -> Path:
    """A fresh git repository carrying this repository's **own** ignore rules.

    The subject of both tests is `REPO_ROOT/.gitignore`; nothing here restates a rule, so a
    narrowing of the real file is what turns these tests red.
    """
    rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert rules.strip(), (
        "the repository ships an empty .gitignore, so there is no rule to measure — this guard "
        "would pass by measuring nothing"
    )
    repo = tmp_path / "scratch"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "."], cwd=repo, check=True,
        capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
    )
    (repo / ".gitignore").write_text(rules, encoding="utf-8")
    return repo


def _check_ignore(repo: Path, relative: str) -> bool:
    """True when `repo`'s own ignore rules match `relative`.

    `git check-ignore -q` exits 0 for a matched path and 1 for an unmatched one, and answers
    for paths that do not exist — so no shape has to be created to ask about it.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", relative],
        capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0


def _assert_not_ignored(repo: Path, relative: str) -> None:
    """The discriminating direction: the instrument must be able to answer *no*.

    Without this, a `_check_ignore` that returned True unconditionally would make every
    assertion below vacuous — a control that cannot fail is not one.
    """
    assert not _check_ignore(repo, relative), (
        f"git reports {relative} as ignored, so this check cannot discriminate: the "
        "assertion above would pass for every shape whether or not a rule covers it. Either "
        "a rule in .gitignore was widened to a pattern the two dependency names were never "
        "about, or `_check_ignore` is reading the wrong subject."
    )


def test_the_ignore_rules_cover_a_dependency_name_as_a_directory_and_as_a_file(tmp_path):
    """The two portable shapes, for both names — the half that reads on every platform.

    A `node_modules` that is a plain file, or a directory, is exactly as stageable as one that
    is a symlink; a slashed rule misses the file too. Neither shape needs a privilege, so this
    test carries the defect's reading on a Windows leg as well.
    """
    repo = _scratch_with_repository_rules(tmp_path)

    unignored: list[str] = []
    for name in NAMES:
        (repo / "as-dir" / name).mkdir(parents=True)
        (repo / "as-file" / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / "as-file" / name).write_text("", encoding="utf-8")
        for relative in (f"as-dir/{name}", f"as-file/{name}"):
            if not _check_ignore(repo, relative):
                unignored.append(relative)

    assert not unignored, (
        "these names are not ignored by the repository's own .gitignore, so `git status` "
        f"lists them and `git add -A` stages them: {unignored}. A rule written as `<name>/` "
        "matches directories only — write it without the slash so it covers both shapes. The "
        "measurement behind this is in the module docstring."
    )

    for name in NAMES:
        (repo / "as-file" / f"{name}_backup").write_text("", encoding="utf-8")
        _assert_not_ignored(repo, f"as-file/{name}_backup")


def test_the_ignore_rules_cover_the_symlink_the_merge_gate_tells_an_operator_to_create(tmp_path):
    """The shape the gate's remedy prints, which is the one the slashed rules missed.

    `_kept_note` prints `ln -sfn … emrg/gui/node_modules` and `ln -sfn ….venv` against a kept
    worktree, so this is a path the repository's own tooling creates — and the first shape a
    `<name>/` rule fails to match. Skipped only where the host cannot make a directory symlink
    (Windows without the privilege); the portable half is the test above, so the skip costs the
    symlink arm alone rather than the whole reading.
    """
    repo = _scratch_with_repository_rules(tmp_path)

    unignored: list[str] = []
    for name in NAMES:
        target = repo / "_targets" / name.lstrip(".")
        target.mkdir(parents=True)
        link = repo / LINK_PATHS[name]
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows w/o privilege
            pytest.skip(
                f"this host cannot create a directory symlink ({exc}), so this shape is "
                "unmeasurable here"
            )
        assert link.is_symlink(), f"{link} is not a symlink, so this arm would be vacuous"
        relative = str(link.relative_to(repo))
        if not _check_ignore(repo, relative):
            unignored.append(relative)

    assert not unignored, (
        "the links the merge gate's own remedy prints are not ignored, so `git add -A` in a "
        f"rebuilt worktree stages them: {unignored}. A rule written as `<name>/` matches "
        "directories only, and a symlink is not one — write the rule without the slash. The "
        "measurement behind this is in the module docstring."
    )

    _assert_not_ignored(repo, "emrg/gui/node_modules_backup")
