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

Three honest boundaries, stated rather than implied:
* the symlink shape needs a privilege Windows may withhold (the arm in
  `tests/test_bash_v2_policy.py` skips for the same reason). It lives in **its own test**, so
  that skip cannot swallow the directory and file shapes — those carry the same defect, need no
  privilege, and run on every leg, which is what keeps a Windows reading a real one;
* "ignored" is read from `git check-ignore -q`'s exit code (0 matched, 1 unmatched), which
  answers for paths that exist and for ones that do not. The control arm in each test is what
  proves this instrument discriminates, since `True` is also what a broken call returns;
* `git check-ignore` also consults the **machine's** excludes file, so an unpinned reading can
  call a name ignored while the repository's own rule is still slashed — the defect, reported
  green. The probe pins `core.excludesFile` to an empty file on the command line, where it
  outranks the global and system config, and
  `test_the_reading_is_the_repositorys_rules_and_not_the_machines_global_file` both installs a
  conflicting global file and asserts the two readings disagree, so the pin cannot decay into
  decoration (measured on cycle `cyc20261009-081002`).
"""

from __future__ import annotations

import os
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


def _pinned_excludes(repo: Path) -> Path:
    """An empty excludes file the probe pins, so the reading is the repository's rules alone.

    `git check-ignore` consults `core.excludesFile` and `$GIT_DIR/info/exclude` as well as the
    repository's `.gitignore`, so without a pin the verdict depends on the machine the guard runs
    on: a host whose global excludes file carries a **slashless** `node_modules` reports the name
    as ignored while the repository's own rule is still slashed — the defect this file exists to
    catch, reported green — measured on cycle `cyc20261009-074228` on an isolated copy of this
    file: with the trailing slash restored on both rules, an environment whose global excludes
    file carries the bare names turns both rows green. `-c`
    sits above the global and system files in git's config precedence, so the probe reads what the
    docstring claims it reads wherever it runs. The file lives beside the repository rather than
    inside `.git/`, because it is the test's fixture and not part of the subject.
    `$GIT_DIR/info/exclude` needs no pin: `git init` writes it with comments only.
    """
    path = repo.parent / "emrg-empty-excludes"
    if not path.exists():
        path.write_text("", encoding="utf-8")
    return path


def _probe(repo: Path, relative: str, *, pinned: bool, env: dict | None = None) -> bool:
    """Ask `git check-ignore -q` about `relative`, exit 0 ⇒ matched, 1 ⇒ unmatched.

    `-q` answers for a path that does not exist, so no shape has to be created to ask about it.
    `pinned` decides whether the machine's own excludes file is neutralised; the two wrappers
    below name each reading, and only the premise test uses the unpinned one.
    """
    cmd = ["git", "-C", str(repo)]
    if pinned:
        cmd += ["-c", f"core.excludesFile={_pinned_excludes(repo)}"]
    cmd += ["check-ignore", "-q", relative]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
        env=env,
    )
    return proc.returncode == 0


def _check_ignore(repo: Path, relative: str, *, env: dict | None = None) -> bool:
    """True when `repo`'s **own** ignore rules match `relative` — the guard's only reading.

    The machine's excludes file is pinned away (see `_pinned_excludes`): the subject is the
    repository's rules, and a global one that happens to cover the same name is not evidence
    about them. `env` lets the premise test install a hostile global file for this probe alone.
    """
    return _probe(repo, relative, pinned=True, env=env)


def _unpinned_check_ignore(repo: Path, relative: str, env: dict | None = None) -> bool:
    """The same question with the machine's excludes file left in force — a diagnostic, not a verdict.

    Used only by the test that proves the pin is doing work: it shows the hostile global file this
    test installs really does reach `git check-ignore`, so the pinned reading beside it is
    measuring the pin rather than an inert setup.
    """
    return _probe(repo, relative, pinned=False, env=env)


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


def test_the_reading_is_the_repositorys_rules_and_not_the_machines_global_file(tmp_path):
    """`git check-ignore` reads the machine's excludes file too, so a narrow rule can read green.

    The two tests above assert that a name *is* ignored, and `git check-ignore` answers that from
    three sources at once — the repository's `.gitignore`, `$GIT_DIR/info/exclude`, and the
    machine's `core.excludesFile`. Only the first is the guard's subject, so on a machine whose
    global ignore carries the **bare** name (`node_modules`, no slash) a later edit taking the
    slash **back** — the exact defect this file exists to catch — still reads as ignored here:
    a false green, and one this repository's own history produced (measured on cycle
    `cyc20261009-074228` on an isolated copy of this file: 2 passed with the trailing slash
    restored, once an environment whose global excludes carried the bare names was in force).

    So the guard pins the source (`_pinned_excludes`), and this test is what keeps the pin from
    being decoration: it builds a scratch repository carrying the **defective** slashed rules, puts
    a conflicting global file in force for the probe alone (`GIT_CONFIG_GLOBAL`, so the real host
    configuration is neither read nor written), and asserts the two readings disagree —

    * unpinned, git calls the name ignored, which is the false green; and
    * pinned, git calls it unignored, so the guard still reddens on the defect.

    Both halves are required. Without the first the test would pass on a machine where the pin
    faces nothing, proving nothing about the defect class; without the second the pin could be
    inert and every assertion above would still pass here.
    """
    repo = tmp_path / "scratch"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "."], cwd=repo, check=True,
        capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
    )
    #: The defect, verbatim: the slashed rule the file's docstring names as the cause.
    (repo / ".gitignore").write_text("node_modules/\n.venv/\n", encoding="utf-8")

    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / "ignore").write_text("node_modules\n.venv\n", encoding="utf-8")
    (hostile / "gitconfig").write_text(
        f"[core]\n\texcludesFile = {hostile / 'ignore'}\n", encoding="utf-8"
    )
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(hostile / "gitconfig")}

    relative = "emrg/gui/node_modules"
    #: Bound to locals before the assertions: pytest renders an inline call's arguments in the
    #: failure message, and `env` is this machine's whole environment — which carries API keys.
    #: A guard that prints the host's secrets when it reddens is a second defect.
    unpinned = _unpinned_check_ignore(repo, relative, env)
    pinned = _check_ignore(repo, relative, env=env)

    assert unpinned is True, (
        "the global excludes file this test installs did not reach `git check-ignore`, so the "
        "pinned reading below would prove nothing about the defect class — the premise is that "
        "the machine's configuration can answer for a name the repository's slashed rule does not"
    )
    assert pinned is False, (
        f"with the slashed rules above in force, git still reports {relative} as ignored, so the "
        "probe is not reading the repository's own rules and the two tests in this file would "
        "pass on a machine whose global excludes cover the name — which is the false green the "
        "pin exists to remove. `-c core.excludesFile=…` must stay on the command line, where it "
        "outranks the global and system files."
    )
