"""Tests for scripts/check-merge-plan-suite.py - is the *plan's* tree healthy?

Background (cycle cyc20260913-154837)
-------------------------------------
Seven PRs, each `MERGEABLE`, each CI double-green. Every ordered pair merged
cleanly (21/21), and the doc-count guard accepted every step of the plan. The tree
the seven produced together failed the suite:

    FAILED tests/test_script_decode_is_locale_independent.py::
        test_every_text_mode_subprocess_pins_its_encoding

A guard that arrived in one PR (#1136) rejected three calls that arrived in
another (#1172). Each PR's CI builds `Merge <head> into <merge-base>`, so neither
run contains the other's files: both green, and the landing tree red.

The tests below reproduce that mechanism rather than a fixed string: a guard test
asserts that no file under `data/` or `src/` contains a token, and the token is
delivered by a *different* PR. Neither PR alone can fail - one has no token to
find, the other has no guard to be found by - and the plan of the two fails. The
same harness, the same three invocations, three different verdicts: that is what
makes the device discriminating rather than merely permissive.

Pinned in both directions (#455 - never infer from the failure case alone):

* a plan whose final tree passes the suite is HEALTHY;
* a plan whose final tree fails it is UNHEALTHY, and the failing test is named;
* a step that conflicts means there is no final tree to judge (exit 3), which is
  not a health verdict;
* the conflicted paths named in that exit-3 refusal are the real files: read from
  the stage block (not from any line of the report that happens to contain a tab,
  which is where the prose after the block injects a name that does not exist) and
  decoded out of git's `core.quotePath` spelling, so "conflicts on ..." names a
  file the caller can open;
* a suite that cannot be run at all is a measurement error, never "healthy" - the
  asymmetry that matters, since an unanswerable question reported as health is how
  a broken tree reaches master;
* the suite runs in a real worktree, not an extracted archive (an archive has no
  `.git`, and this repo's tests resolve paths through git, so the harness would
  report failures on every input - a device measuring itself).

Hermetic: real local git repos, a local bare remote exposing `refs/pull/<N>/head`,
no network, no GitHub.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import time
import sys
from pathlib import Path, PureWindowsPath

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-merge-plan-suite.py"

# Assembled at runtime: the guard file must not contain the literal it searches
# for, or it would find itself in every tree including master's.
TOKEN = "FORB" + "IDDEN"

GUARD_TEST = f'''import pathlib

TOKEN = "FORB" + "IDDEN"


def test_no_token_under_data_or_src():
    hits = []
    for name in ("data", "src"):
        root = pathlib.Path(name)
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and TOKEN in path.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(path))
    assert not hits, hits
'''

GIT_METADATA_TEST = '''"""A check that only a checked-out tree can satisfy."""

import pathlib
import subprocess
import time


def test_the_tree_has_git_metadata():
    root = pathlib.Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, "the tree was extracted rather than checked out"
'''


KEPT_MARKER_SENTINEL = "KEPT-TREE-ONLY-MARKER-6b53c0d"

# Placed in a *kept* tree only, so a collection can be attributed rather than assumed.
# Deliberately failing: `-q` reports a failing test by name and a passing one as a dot,
# and the name is the evidence - the whole defect is a command that looks pinned to one
# tree while answering about another, where the count difference alone is easy to explain
# away.
KEPT_MARKER_TEST = f'''"""Exists only in the kept tree, so what collected it can be named."""


def test_the_kept_tree_only_marker():
    assert False, "{KEPT_MARKER_SENTINEL}"
'''


def _load_module():
    spec = importlib.util.spec_from_file_location("check_merge_plan_suite", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")


def _write(repo: Path, relpath: str, body: str) -> None:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _two_branch_conflict_on(repo: Path, name: str) -> None:
    """Branches `ours` and `theirs` of `repo` that both edit `name`, so merging conflicts.

    Both branches touch one line of one file, which is the smallest real conflict; the
    file's *name* is the variable under test, so it is the only thing the caller picks.
    """
    _init_repo(repo)
    _write(repo, name, "a\nb\nc\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "ours")
    _write(repo, name, "a\nOURS\nc\n")
    _git(repo, "commit", "-qam", "ours")
    _git(repo, "checkout", "-q", "-b", "theirs", "master")
    _write(repo, name, "a\nTHEIRS\nc\n")
    _git(repo, "commit", "-qam", "theirs")


@pytest.fixture
def queue(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout on `master` plus a local bare remote, as GitHub would be.

    PR heads are fetched by the tool from `refs/pull/<N>/head`, so the remote has
    to expose exactly that, whatever the branch is called locally.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "tests/test_seed.py", "def test_seed():\n    assert True\n")
    _write(repo, "README.md", "base\n")
    _commit(repo, "base")

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(origin)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "master")
    return repo, origin


def _branch_with(repo: Path, name: str, files: dict[str, str]) -> None:
    """A branch off master carrying `files`, as a PR branch does."""
    _git(repo, "checkout", "-q", "-b", name, "master")
    for relpath, body in files.items():
        _write(repo, relpath, body)
    _commit(repo, f"add {name}")
    _git(repo, "checkout", "-q", "master")


def _publish(repo: Path, origin: Path, number: int, branch: str) -> str:
    """Expose a branch as PR #number on the remote."""
    _git(repo, "push", "-q", "origin", f"{branch}:refs/heads/{branch}")
    sha = _git(repo, "rev-parse", f"refs/heads/{branch}")
    _git(origin, "update-ref", f"refs/pull/{number}/head", sha)
    return sha


def _run_tool(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--base", "master"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=None if env is None else {**os.environ, **env},
    )


def test_a_guard_in_one_pr_and_its_violation_in_another_are_green_alone_but_not_together(
    queue: tuple[Path, Path],
) -> None:
    """The measured defect shape, reproduced: per-PR green, plan red."""
    repo, origin = queue
    _branch_with(repo, "guard", {"tests/test_no_token_under_data_or_src.py": GUARD_TEST})
    _branch_with(repo, "violator", {"data/payload.txt": f"contains {TOKEN}\n"})
    _publish(repo, origin, 1, "guard")
    _publish(repo, origin, 2, "violator")

    alone_guard = _run_tool(repo, "1")
    alone_violator = _run_tool(repo, "2")
    together = _run_tool(repo, "1", "2")

    # Both sides are green on their own - this is why no per-PR signal can be
    # asked to notice, and why the plan has to be built to be judged.
    assert alone_guard.returncode == 0, alone_guard.stdout + alone_guard.stderr
    assert alone_violator.returncode == 0, alone_violator.stdout + alone_violator.stderr

    assert together.returncode == 1, together.stdout + together.stderr
    assert "test_no_token_under_data_or_src" in (together.stdout + together.stderr)
    assert "plan: #1 -> #2" in together.stdout
    # The verdict names the tree it measured: "which tree answered?" is the defect
    # this family of tools exists to remove.
    assert re.search(r"final tree [0-9a-f]{12} \([0-9a-f]{40}\)", together.stdout)


def test_a_reordered_plan_is_reported_in_the_order_it_was_given(
    queue: tuple[Path, Path],
) -> None:
    """Order is the caller's decision and is echoed, never silently sorted."""
    repo, origin = queue
    _branch_with(repo, "first", {"notes.md": "first\n"})
    _branch_with(repo, "second", {"other.md": "second\n"})
    _publish(repo, origin, 7, "first")
    _publish(repo, origin, 3, "second")

    proc = _run_tool(repo, "7", "3")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "plan: #7 -> #3" in proc.stdout


def test_a_step_that_conflicts_leaves_no_final_tree(queue: tuple[Path, Path]) -> None:
    """A conflict is not a health verdict: there is no tree to judge."""
    repo, origin = queue
    _branch_with(repo, "left", {"README.md": "left\n"})
    _branch_with(repo, "right", {"README.md": "right\n"})
    _publish(repo, origin, 1, "left")
    _publish(repo, origin, 2, "right")

    proc = _run_tool(repo, "1", "2")
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "README.md" in proc.stderr
    assert "step 2 (#2)" in proc.stderr

    # The fold follows the order it was given - reversing the plan moves which
    # step is the one that cannot be built. A tool that sorted its input would
    # answer about a plan the caller never asked for, and would report the same
    # step both times.
    reversed_proc = _run_tool(repo, "2", "1")
    assert reversed_proc.returncode == 3, reversed_proc.stdout + reversed_proc.stderr
    assert "step 2 (#1)" in reversed_proc.stderr


def test_the_suite_runs_in_a_real_worktree_not_an_extracted_archive(
    queue: tuple[Path, Path],
) -> None:
    """Pins the materialisation: an archive has no `.git`, and this repo's tests
    resolve paths through git, so an archive harness fails on every input - a
    device measuring itself rather than the plan."""
    repo, origin = queue
    _branch_with(repo, "gitmeta", {"tests/test_the_tree_has_git_metadata.py": GIT_METADATA_TEST})
    _publish(repo, origin, 1, "gitmeta")

    proc = _run_tool(repo, "1")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _worktree_listing(repo: Path) -> str:
    """`git worktree list --porcelain` - one `worktree <path>` line per worktree.

    Porcelain rather than the decorative form, because the decorative line separates
    its fields with spaces and a path may contain one (a pytest temp directory under
    `C:/Users/<name with a space>/...` is an ordinary case).
    """
    return _git(repo, "worktree", "list", "--porcelain")


#: The GUI's dependencies live here, not in the repository root's `node_modules`.
_GUI_NODE_MODULES = "emrg/gui/node_modules"


def _node_remedy(note: str) -> tuple[str, str]:
    """The `(source, destination)` of the node remedy the note prints."""
    for line in note.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0] == "node:" and parts[1:3] == ["ln", "-sfn"]:
            return parts[3], parts[4]
    raise AssertionError(f"the note prints no node remedy:\n{note}")


def _node_remedy_links_the_guis_own_deps(note: str) -> bool:
    """True when the node remedy links the *GUI's* `node_modules`, not the root's.

    The source is the load-bearing half, and the destination cannot discriminate it:
    the root form (`ln -sfn <main>/node_modules <kept>/emrg/gui/node_modules`) contains
    the string `emrg/gui/node_modules` too, which is why the assertion that stood here
    passed for a link that fixed nothing. Measured 2026-09-19 (`cyc20260919-060712`) on
    landing tree `f96d6515c734`: the repository root's `node_modules` is empty (0
    entries), so linking it is indistinguishable from linking nothing - the GUI suite
    reports 126 passed / 1 failed (`test/integration.test.js`, `Cannot find module
    'ws'`) either way, and 126 / 0 / 8 skipped once the GUI's own directory is linked.
    """
    source, destination = _node_remedy(note)
    return source.endswith(_GUI_NODE_MODULES) and destination.endswith(_GUI_NODE_MODULES)


def test_the_node_remedy_reader_rejects_the_root_form() -> None:
    """The instrument's control: the wrong link source must read as wrong.

    Without this, `_node_remedy_links_the_guis_own_deps` could return True for anything
    (the shape the previous assertion effectively had) and the arm it now backs would
    still pass. Three shapes, because the two halves fail independently: the right
    source with the right destination, the root source, and a destination that is not
    the GUI's directory at all.
    """
    def note(source: str, destination: str = "/kept/emrg/gui/node_modules") -> str:
        return f"    node:   ln -sfn {source} {destination}\n"

    assert _node_remedy_links_the_guis_own_deps(note("/main/emrg/gui/node_modules"))
    assert not _node_remedy_links_the_guis_own_deps(note("/main/node_modules"))
    assert not _node_remedy_links_the_guis_own_deps(
        note("/main/emrg/gui/node_modules", "/kept/node_modules")
    )


def _worktree_listing_names(listing: str, path: Path) -> bool:
    """Does this listing name `path` as one of its worktrees?

    Compared as forward-slash spellings, because the two sides disagree about the
    separator on Windows: git prints `C:/Users/.../kept` while `str(Path.resolve())`
    is `C:\\Users\\...\\kept`. Measured on the `windows-2025` leg (2026-09-17) - the
    first version asserted `str(path.resolve()) in listing` and failed with the kept
    worktree plainly present in the listing, and in the same test its negative twin
    (`not in`) *passed* for the same wrong reason, so that arm was vacuous there.

    The same two-entry-point class as `tests/test_conflict_markers.py` (git's own
    spelling, and `str(Path)`); the fix is the same shape: normalise, never compare
    raw spellings. Pinned in both directions by
    `test_the_worktree_listing_is_matched_across_separators`, which replays the
    Windows shape with `PureWindowsPath` so the instrument is discriminating on POSIX.
    """

    def normalise(text: str) -> str:
        return text.replace("\\", "/")

    wanted = normalise(path.as_posix())
    named = (
        line[len("worktree ") :]
        for line in listing.splitlines()
        if line.startswith("worktree ")
    )
    return any(normalise(name) == wanted for name in named)


def test_a_kept_worktree_is_the_tree_the_run_measured(
    queue: tuple[Path, Path], tmp_path: Path
) -> None:
    """`--keep DIR` leaves behind the tree this run answered *for*, not a rebuild of it.

    Reviewing a PR means running your own probe on the tree whose sha was published;
    rebuilding that tree by hand is how a verdict about a different tree gets written
    (measured this cycle: two hand-built landing-tree worktrees, each needing its
    `git write-tree` checked against the printed sha before the arms meant anything).
    So the kept worktree must hash to the printed sha, must contain both PRs, and the
    run that keeps it must reach the same verdict as the run that deletes it - keeping
    is a side effect of the measurement, never a second measurement.
    """
    repo, origin = queue
    _branch_with(repo, "guard", {"tests/test_no_token_under_data_or_src.py": GUARD_TEST})
    _branch_with(repo, "violator", {"data/payload.txt": f"contains {TOKEN}\n"})
    _publish(repo, origin, 1, "guard")
    _publish(repo, origin, 2, "violator")

    plain = _run_tool(repo, "1", "2")
    kept_dir = tmp_path / "kept"
    kept = _run_tool(repo, "1", "2", "--keep", str(kept_dir))

    # Same verdict and same tree with and without --keep.
    assert plain.returncode == 1, plain.stdout + plain.stderr
    assert kept.returncode == 1, kept.stdout + kept.stderr
    assert "test_no_token_under_data_or_src" in kept.stdout
    match = re.search(r"final tree [0-9a-f]{12} \(([0-9a-f]{40})\)", kept.stdout)
    assert match, kept.stdout
    assert match.group(1) in plain.stdout

    # The directory left behind *is* that tree, checked out, with both PRs in it.
    assert kept_dir.is_dir()
    assert _git(kept_dir, "write-tree") == match.group(1)
    assert (kept_dir / "tests" / "test_no_token_under_data_or_src.py").is_file()
    assert (kept_dir / "data" / "payload.txt").is_file()
    assert _worktree_listing_names(_worktree_listing(repo), kept_dir)

    # The note names the path, the tree, and the two traps every fresh worktree has -
    # no `.venv` (so `uv run pytest` there reports that no suite ran) and no
    # `node_modules` (so the GUI suite reds the spawn-args test and
    # `test/integration.test.js`, which cannot resolve `ws`). Both were reported
    # as defects before, which is why the tool says them out loud - and it prints the
    # remedy for each, because a warning without one costs the next reader the same
    # discovery. Shape-matched, not path-matched: the note prints git's spelling of the
    # main checkout, which is a forward-slash path on Windows too.
    assert "kept " in kept.stdout and kept_dir.name in kept.stdout
    assert ".venv" in kept.stdout and "node_modules" in kept.stdout
    assert f"PYTHONPATH={kept_dir}" in kept.stdout
    assert "-m pytest tests/ -q" in kept.stdout
    assert _node_remedy_links_the_guis_own_deps(kept.stdout), kept.stdout

    # The removal line it prints is the one that works.
    _git(repo, "worktree", "remove", "--force", str(kept_dir))
    assert not kept_dir.exists()
    assert not _worktree_listing_names(_worktree_listing(repo), kept_dir)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the note's remedies are POSIX-shaped (`<main>/.venv/bin/python`, `ln -sfn`), so the "
        "line it prints cannot be executed on the windows leg; the shape assertions in "
        "test_a_kept_worktree_is_the_tree_the_run_measured still run there"
    ),
)
def test_the_printed_python_remedy_measures_the_kept_tree(
    queue: tuple[Path, Path], tmp_path: Path
) -> None:
    """The remedy the note prints has to answer about the tree the note is about.

    The line is printed in the main checkout and carries `PYTHONPATH=<kept>`, which looks
    pinned and is not: for `-m pytest`, `sys.path[0]` is the process CWD and the
    positional `tests/` resolves against it, so copied verbatim from where it is printed
    the line measures the *main* checkout - the injury this tool exists to prevent, printed
    by the tool. Measured 2026-09-17 (`cyc20260917-142057`) in both directions: as printed,
    `2797 deselected` of the main suite, the kept tree's marker test not collected, `import
    emrg` from `<main>/emrg/__init__.py`; with `cd <kept> &&` in front, the marker
    collected and `import emrg` from the kept tree. `_suite_verdict` pins both (its
    `cwd=str(worktree)`, `_suite_env`'s `PYTHONPATH`), so the printed remedy keeping only
    the weaker of the two is the whole defect.

    Attribution, not spelling: a sentinel test file that exists *only* in the kept tree,
    the printed line run verbatim from the checkout it is printed in (the harness's own
    cwd, which `_main_worktree` also names), and the sentinel required to be named in the
    output. The shape of the line is asserted too, because the failing behaviour is not a
    wrong string but a missing `cd` - and a regression would otherwise be caught only by a
    slow arm (the main suite) that happens to red.

    The fixture's `repo` is the main checkout here, so it gets the `.venv/bin/python` the
    note names: every other arm of this file asserts the note's *text*, and this is the one
    that runs it. Created *after* the harness run, deliberately - the fixtures commit with
    `git add -A`, and whether a `.venv` directory is committed depends on the machine's
    gitignore configuration. Measured on the `ubuntu-latest` leg (run for head `0d28ff9e`):
    with the symlink in place first, `_branch_with`'s `git add -A` committed it on the
    branch, the fixture's `git checkout master` then removed it, and the printed line failed
    with `/bin/sh: .../repo/.venv/bin/python: not found`. The same arm passed on the
    development machine, which has a global `.venv/` ignore (this repo ignores `.venv/`
    too) - so the difference was scaffolding, not the line under test. Hence the explicit
    `exists()` assertion as well: if the scaffold is what breaks, it has to say so itself.

    The scaffold is an *exec wrapper*, not a symlink to this suite's interpreter. Measured
    on the `ubuntu-latest` leg (run for head `631944fd`): with `<repo>/.venv/bin/python` a
    symlink to `sys.executable`, the line ran and answered `No module named pytest`.
    CPython locates a venv through the directory of the path it was invoked *as*
    (measured here both ways: `<repo>/.venv/bin/python -c "import sys; print(sys.prefix)"`
    through a symlink prints the base interpreter's prefix, because the invoked directory
    carries no `pyvenv.cfg`, while an `exec` of that same target prints the venv's) - and
    on the runner the base interpreter has no pytest. The arm stayed green on the
    development machine only because *this host's* uv base python happens to have pytest
    in its own site-packages (`.../uv/python/cpython-3.13.3-.../site-packages/pytest/`),
    which is an accident of the host and not a property of the line. Executing the same
    interpreter by its own path reproduces the environment it was launched with, venv or
    not, so the arm measures the remedy and not the runner's interpreter layout.
    """
    repo, origin = queue
    _branch_with(repo, "guard", {"tests/test_no_token_under_data_or_src.py": GUARD_TEST})
    _publish(repo, origin, 1, "guard")

    kept_dir = tmp_path / "kept"
    kept = _run_tool(repo, "1", "--keep", str(kept_dir))
    assert kept.returncode == 0, kept.stdout + kept.stderr

    remedy = next(
        line.strip().removeprefix("python: ")
        for line in kept.stdout.splitlines()
        if line.strip().startswith("python: ")
    )

    # A wrapper, not a symlink: see the docstring (head `631944fd`, `ubuntu-latest`, where
    # the symlink ran as the base interpreter and reported `No module named pytest`).
    interpreter = repo / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
    )
    interpreter.chmod(0o755)
    can_run = subprocess.run(
        [str(interpreter), "-m", "pytest", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert can_run.returncode == 0, (
        "the scaffolding interpreter cannot run pytest, so this arm measures nothing: "
        + (can_run.stdout or "")
        + (can_run.stderr or "")
    )

    _write(kept_dir, "tests/test_kept_tree_only_marker.py", KEPT_MARKER_TEST)

    proc = subprocess.run(
        remedy,
        shell=True,
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert KEPT_MARKER_SENTINEL in out, out[-2000:]
    # The count, because `-q` names failing tests only: the kept tree collects its own
    # `tests/test_seed.py` *and* the PR's `test_no_token_under_data_or_src.py` *and* the
    # marker, so 3 collected / 1 failed. The main checkout would collect `test_seed.py` and
    # the marker (2 collected) - which is what a missing `cd` measures.
    assert "1 failed, 2 passed" in out, out[-2000:]

    # The shape is asserted *after* the run, deliberately: this is the secondary pin, and a
    # test whose first assertion is the spelling would red on a missing `cd` without ever
    # exercising the behaviour it exists to measure (measured on the mutation arm: with the
    # `cd` deleted, the behavioural assertions above fail on their own - the sentinel is not
    # collected and the count is the main checkout's).
    assert remedy.startswith(f"cd {kept_dir} && PYTHONPATH={kept_dir} "), remedy


def test_the_worktree_listing_is_matched_across_separators() -> None:
    """The kept-tree assertions must not depend on the platform's separator.

    `git worktree list` prints forward slashes on every platform (measured on the
    `windows-2025` runner: `C:/Users/runneradmin/.../kept 1541ddc (detached HEAD)`),
    while `str(Path.resolve())` prints backslashes on Windows. The Windows leg caught
    the naive `str(path) in listing`, and - the part that made it a real defect rather
    than a portability nit - the *negative* assertion in the same test passed there for
    the same wrong reason, so the arm proved nothing on that platform.

    Driving the matcher with a Windows-shaped listing and a `PureWindowsPath` is what
    makes the instrument discriminating **here**, on POSIX, where no real `git` run can
    produce the failing shape.
    """
    windows_listing = (
        "worktree C:/Users/a b/Temp/pytest-0/repo\n"
        "HEAD 6b53c0d0000000000000000000000000000000000\n"
        "branch refs/heads/master\n"
        "\n"
        "worktree C:/Users/a b/Temp/pytest-0/kept\n"
        "HEAD 1541ddc0000000000000000000000000000000000\n"
        "detached\n"
        "\n"
    )
    kept = PureWindowsPath("C:/Users/a b/Temp/pytest-0/kept")
    absent = PureWindowsPath("C:/Users/a b/Temp/pytest-0/other")

    assert _worktree_listing_names(windows_listing, kept)
    assert not _worktree_listing_names(windows_listing, absent)

    # The path contains a space, which is why the matcher reads whole `worktree <path>`
    # lines instead of splitting the decorative listing on whitespace.
    assert " " in kept.as_posix() and "worktree " in windows_listing

    # And the spelling this replaced: on Windows `str(...)` is the backslashed form, so
    # the old assertion was false there for a worktree that was really listed. Pinned as
    # a fact so the next reader cannot "simplify" the matcher back into the defect.
    assert str(kept) not in windows_listing


def test_the_note_names_a_main_checkout_that_exists(mod) -> None:
    """The remedy lines are commands only if the path in them is real.

    `_main_worktree` reads the first `worktree` line of `git worktree list --porcelain`,
    which is the main worktree on every invocation (measured: the same first line from
    the main tree and from a linked one). Asserted against this repository - the tests
    run in it - so the pinned fact is "the lookup answers with a checkout that has this
    project in it", not a spelling.
    """
    found = mod._main_worktree()
    assert found is not None, "git could not name the main worktree"
    main = Path(found)
    assert main.is_dir()
    assert (main / "pyproject.toml").is_file(), f"{main} is not the main checkout"


def test_keep_refuses_the_two_ways_it_could_mislead(
    queue: tuple[Path, Path], tmp_path: Path
) -> None:
    """A combined `--steps --keep` and a stale directory are refusals, not surprises.

    `--steps` measures a different tree per step, so "the tree to keep" has no single
    answer; and a directory that already exists is not the tree this run measured, so
    leaving a kept worktree there would attach the caller's later checks to the wrong
    tree - the defect class this family of tools exists to remove.
    """
    repo, _origin = queue

    combined = tmp_path / "combined"
    proc = _run_tool(repo, "1", "--steps", "--keep", str(combined))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "cannot be combined" in proc.stderr
    assert not combined.exists()

    stale = tmp_path / "stale"
    stale.mkdir()
    proc = _run_tool(repo, "1", "--keep", str(stale))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "already exists" in proc.stderr
    # Both sides are `str(Path.resolve())` on the platform under test - the tool prints
    # the `--keep` argument it resolved, this line resolves the same directory - so this
    # one agreement does not cross separators and needs no normalising (unlike the
    # worktree listing above, where one side is git's own spelling). It passed on the
    # `windows-2025` leg, which is the evidence for that claim.
    assert proc.stderr.count(str(stale.resolve()))


def _plan_refs(repo: Path) -> list[str]:
    """The temp refs the harness parks fetched PR heads in, if any are left."""
    listed = _git(repo, "for-each-ref", "--format=%(refname)", "refs/emrg-plan-suite/")
    return [line for line in listed.splitlines() if line.strip()]


def test_a_successful_run_leaves_no_fetched_pr_ref_behind(
    queue: tuple[Path, Path],
) -> None:
    """The temp ref a fetched PR needs must not outlive the run.

    Measured 2026-09-17 (`cyc20260917-142057`): `_fetch_head` left one ref per
    planned PR behind, so `refs/emrg-plan-suite/` had reached 88 refs pinning 283
    commits unreachable from master - unreachable, so `git gc` could never prune
    them, and every re-pushed PR head added another. A ref nobody reads is state,
    and this run's verdict is the only thing it was ever for.
    """
    repo, origin = queue
    _branch_with(repo, "one", {"a.md": "a\n"})
    _branch_with(repo, "two", {"b.md": "b\n"})
    _publish(repo, origin, 1, "one")
    _publish(repo, origin, 2, "two")

    assert _plan_refs(repo) == [], "the fixture started with refs the run did not make"

    proc = _run_tool(repo, "1", "2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _plan_refs(repo) == []
    # …and the cleanup did not cost the run its answer.
    assert re.search(r"final tree [0-9a-f]{12} \([0-9a-f]{40}\)", proc.stdout)


def test_the_run_does_not_hand_the_suite_the_callers_dirty_tree_override(
    queue: tuple[Path, Path],
) -> None:
    """The run's verdict must not depend on the caller's working tree.

    `EMRG_TASK_DIRTY_OVERRIDE` is the evolution cycle's own escape hatch: a cycle
    whose tree is dirty exports it to keep working, and `scheduler.py` reads it from
    the environment. The suite observes it through the *real* project, not through
    the worktree, so four `tests/test_scheduler.py` dirty-tree tests assert the
    unoverridden verdict and fail when the variable is present. Measured 2026-09-17
    (`cyc20260917-142057`): a plan run from a dirty caller tree with the override
    exported reported `suite FAILED` for a tree that is green, while the same run
    with the override unset reported it green.

    The fixture's own test is the instrument: it fails if the variable reaches it,
    so this asserts the behaviour rather than the shape of an environment dict.
    """
    repo, origin = queue
    _branch_with(
        repo,
        "reports-env",
        {
            "tests/test_zz_env_seen_by_the_suite.py": (
                "import os\n"
                "\n"
                "OVERRIDE = 'EMRG_TASK_DIRTY_OVERRIDE'\n"
                "\n"
                "\n"
                "def test_the_suite_was_not_handed_the_override():\n"
                "    seen = os.environ.get(OVERRIDE)\n"
                "    assert seen is None, OVERRIDE + ' reached the suite: ' + repr(seen)\n"
            )
        },
    )
    _publish(repo, origin, 1, "reports-env")

    proc = _run_tool(repo, "1", env={"EMRG_TASK_DIRTY_OVERRIDE": "emrg-task"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "suite OK" in proc.stdout


def test_a_plan_that_conflicts_leaves_no_fetched_pr_ref_behind_either(
    queue: tuple[Path, Path],
) -> None:
    """The cleanup covers the paths that never reach a suite.

    A plan that conflicts (rc 3) fetched its heads exactly as a successful run did,
    and a cleanup wired into the success path - or into `_suite_verdict`'s finally -
    leaks on precisely the runs a reader is most likely to repeat while resolving
    the conflict. Both directions are pinned by these two tests: drop the `finally`
    and both fail; move it to the success path and only this one does.
    """
    repo, origin = queue
    _branch_with(repo, "left", {"README.md": "left\n"})
    _branch_with(repo, "right", {"README.md": "right\n"})
    _publish(repo, origin, 1, "left")
    _publish(repo, origin, 2, "right")

    proc = _run_tool(repo, "1", "2")
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert _plan_refs(repo) == []
    # The conflict is still reported by name, so the cleanup ran after the verdict
    # was reached rather than instead of reaching it.
    assert "step 2 (#2)" in proc.stderr


def test_a_fetch_that_fails_partway_still_leaves_no_ref_behind(
    queue: tuple[Path, Path],
) -> None:
    """The refs already parked before the failing fetch are the ones a leak keeps.

    A run that fetches PR 1 and then dies on PR 2 exits 2 without ever reaching a
    suite, a plan or a conflict - so a cleanup wired to any of those paths misses
    exactly the run that created a ref and asked for nothing else. Measured
    2026-09-17 by @how2how2how2-arch on the previous revision of this fix, where the
    fetch sat in a `try` whose `except` returned before the cleanup's `finally`:
    `check-merge-plan-suite.py 1323 999999` reported rc 2 and left
    `refs/emrg-plan-suite/pr1323` behind. `_fetch_heads`' own docstring names this
    case ("a run that dies on PR 3 of 5 has created two refs"), so the promise is
    what this test holds the code to.
    """
    repo, origin = queue
    _branch_with(repo, "one", {"a.md": "a\n"})
    _publish(repo, origin, 1, "one")

    assert _plan_refs(repo) == [], "the fixture started with refs the run did not make"

    # #999999 does not exist, so the fetch raises after #1's ref is already there.
    proc = _run_tool(repo, "1", "999999")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "could not measure" in proc.stderr
    assert _plan_refs(repo) == [], "the ref fetched before the failure outlived the run"


def _fake_run(monkeypatch, mod, stdout: str, stderr: str, rc: int) -> None:
    monkeypatch.setattr(
        mod,
        "_run",
        lambda argv, cwd=None, env=None: subprocess.CompletedProcess(argv, rc, stdout, stderr),
    )


# Measured on this machine (`git merge-tree --write-tree`, git 2.4x): a failure to
# merge two *inputs* is reported with **exit code 1**, the same code a conflict
# uses. The exit code therefore does not discriminate - the output does.
MERGE_TREE_INPUT_FAILURES = [
    "merge-tree: no-such-branch - not something we can merge\n",
    (
        "error: 45cf141ba67d59203f02a54f03162f3fcef57830: expected commit type, "
        "but the object dereferences to blob type\n"
        "merge-tree: 45cf141ba67d59203f02a54f03162f3fcef57830 - not something we can merge\n"
    ),
]

REAL_CONFLICT_STDOUT = "\n".join(
    [
        "a99bc22e7c9f58ab0d501ebf64d1e9e0b440f21c",
        "100644 df967b96a579e45a18b8251732d16804b2e56a55 1\tREADME.md",
        "100644 45cf141ba67d59203f02a54f03162f3fcef57830 2\tREADME.md",
        "100644 c376d892e8b105bd712d06ec5162b5f31ce949c3 3\tREADME.md",
        "",
        "Auto-merging README.md",
        "CONFLICT (content): Merge conflict in README.md",
    ]
)


@pytest.mark.parametrize("stderr", MERGE_TREE_INPUT_FAILURES)
def test_a_merge_tree_failure_with_exit_code_one_is_not_read_as_a_conflict(
    mod, monkeypatch, stderr: str
) -> None:
    """`rc == 1` is not the conflict signal; an empty stdout is the failure signal.

    Measured: "not something we can merge" (an unknown ref, or an object that
    dereferences to a blob) exits 1 with empty stdout, while a real conflict exits 1
    with the merged tree's OID on the first line. Reading only the exit code turns
    this failure into a `PlanConflict` with an empty path list, i.e. it tells the
    caller to go resolve a conflict that does not exist.
    """
    _fake_run(monkeypatch, mod, "", stderr, 1)
    with pytest.raises(mod.MeasurementError) as excinfo:
        mod._merge_tree("a" * 40, "b" * 40)
    # The diagnostic has to survive into the message, or the caller cannot tell why.
    assert "not something we can merge" in str(excinfo.value)


def test_a_real_conflict_shape_still_parses_into_paths(mod, monkeypatch) -> None:
    """The fix must not stop reading actual conflicts (pinned in both directions)."""
    _fake_run(monkeypatch, mod, REAL_CONFLICT_STDOUT, "", 1)
    tree, paths = mod._merge_tree("a" * 40, "b" * 40)
    assert tree is None
    assert set(paths) == {"README.md"}


def test_a_clean_merge_without_a_tree_is_a_measurement_error_not_a_crash(
    mod, monkeypatch
) -> None:
    """An empty stdout with rc 0 used to raise IndexError, i.e. it exited 1.

    Exit 1 is this tool's "the plan's tree FAILED the suite" - the finding - so an
    unmeasured merge reported as a verdict is exactly the confusion this family of
    tools exists to remove.
    """
    _fake_run(monkeypatch, mod, "", "", 0)
    with pytest.raises(mod.MeasurementError):
        mod._merge_tree("a" * 40, "b" * 40)


# The conflicted *paths* are the other half of "name the tree, not the code": exit 3
# tells the caller to go resolve a conflict, and the names in that refusal are what
# the caller resolves. Measured 2026-09-14 (`cyc20260914-062927`, git 2.50.1) in a
# scratch repo, a conflict in `f<TAB>tab.txt` and one in `中文.txt` report:
#
#     100644 <blob> 1\t"f\\ttab.txt"                          <- stage block, quoted
#     100644 <blob> 1\t"\\344\\270\\255\\346\\226\\207.txt"
#     <blank line>
#     Auto-merging f<TAB>tab.txt                              <- prose, a real tab
#     CONFLICT (content): Merge conflict in f<TAB>tab.txt
#
# So "the text after the first tab on any line" reads `tab.txt` out of the prose - a
# path that collides with nothing and does not exist - and passes the quoted
# spelling through as the name. Both answers are wrong in the direction that costs
# a resolution round, so both are pinned here, in the same file as the ordinary
# conflict they must not break.

# The stage block git writes for a path whose name contains a tab: the name is
# quoted and its tab escaped, while the prose below it carries a real tab.
_TAB_NAME_STDOUT = "\n".join(
    [
        "a99bc22e7c9f58ab0d501ebf64d1e9e0b440f21c",
        '100644 df967b96a579e45a18b8251732d16804b2e56a55 1\t"f\\ttab.txt"',
        '100644 45cf141ba67d59203f02a54f03162f3fcef57830 2\t"f\\ttab.txt"',
        '100644 c376d892e8b105bd712d06ec5162b5f31ce949c3 3\t"f\\ttab.txt"',
        "",
        "Auto-merging f\ttab.txt",
        "CONFLICT (content): Merge conflict in f\ttab.txt",
    ]
)

# The same conflict for a non-ASCII name, as the default `core.quotePath=true`
# spells it: the UTF-8 bytes escaped to octal.
_QUOTED_NAME_STDOUT = "\n".join(
    [
        "a99bc22e7c9f58ab0d501ebf64d1e9e0b440f21c",
        '100644 df967b96a579e45a18b8251732d16804b2e56a55 1\t"\\344\\270\\255\\346\\226\\207.txt"',
        '100644 45cf141ba67d59203f02a54f03162f3fcef57830 2\t"\\344\\270\\255\\346\\226\\207.txt"',
        '100644 c376d892e8b105bd712d06ec5162b5f31ce949c3 3\t"\\344\\270\\255\\346\\226\\207.txt"',
        "",
        "Auto-merging 中文.txt",
        "CONFLICT (content): Merge conflict in 中文.txt",
    ]
)


class TestTheConflictedPathsAreTheRealFiles:
    """Every name in "conflicts on ..." must be a file that really collides."""

    def test_the_prose_after_the_block_is_not_a_path(self, mod, monkeypatch) -> None:
        """`tab.txt` came out of "Auto-merging f<TAB>tab.txt" - it is not a file.

        The name containing the tab is the conflicted one; the prose that reports it
        is tab-separated for the same reason, so a reader that treats the tab as the
        path separator invents a second, non-existent path.
        """
        _fake_run(monkeypatch, mod, _TAB_NAME_STDOUT, "", 1)
        _, paths = mod._merge_tree("a" * 40, "b" * 40)
        assert set(paths) == {"f\ttab.txt"}
        assert "tab.txt" not in paths

    def test_a_quoted_name_is_decoded_to_the_file_it_names(
        self, mod, monkeypatch
    ) -> None:
        """`"\\344\\270\\255...txt"` is `中文.txt`: the caller cannot open the escapes."""
        _fake_run(monkeypatch, mod, _QUOTED_NAME_STDOUT, "", 1)
        _, paths = mod._merge_tree("a" * 40, "b" * 40)
        assert set(paths) == {"中文.txt"}
        assert not any(path.startswith('"') for path in paths)

    def test_an_ordinary_conflict_still_parses(self, mod, monkeypatch) -> None:
        """The rule must not stop reading the common shape (pinned both ways)."""
        _fake_run(monkeypatch, mod, REAL_CONFLICT_STDOUT, "", 1)
        _, paths = mod._merge_tree("a" * 40, "b" * 40)
        assert set(paths) == {"README.md"}

    def test_a_clashing_path_is_named_once_per_stage_line(
        self, mod, monkeypatch
    ) -> None:
        """A rename conflict names all three sides - the block is read as written."""
        out = "\n".join(
            [
                "a99bc22e7c9f58ab0d501ebf64d1e9e0b440f21c",
                "100644 df967b96a579e45a18b8251732d16804b2e56a55 1\tf.txt",
                "100644 df967b96a579e45a18b8251732d16804b2e56a55 2\trenamed-ours.txt",
                "100644 df967b96a579e45a18b8251732d16804b2e56a55 3\trenamed-theirs.txt",
                "",
                "CONFLICT (rename/rename): f.txt renamed to renamed-ours.txt in ours "
                "and to renamed-theirs.txt in theirs.",
            ]
        )
        _fake_run(monkeypatch, mod, out, "", 1)
        _, paths = mod._merge_tree("a" * 40, "b" * 40)
        assert set(paths) == {"f.txt", "renamed-ours.txt", "renamed-theirs.txt"}

    def test_a_conflict_that_names_no_path_is_not_a_named_conflict(
        self, mod, monkeypatch
    ) -> None:
        """A tree and no paths: "conflicts on " names nothing.

        The report would read `step 1 (#12) conflicts on ` - a refusal that tells the
        caller to resolve a conflict it cannot locate. Unanswered, like every other
        refusal this family emits when the evidence is missing.
        """
        out = "a99bc22e7c9f58ab0d501ebf64d1e9e0b440f21c\n\nsome prose\n"
        _fake_run(monkeypatch, mod, out, "", 1)
        with pytest.raises(mod.MeasurementError):
            mod._merge_tree("a" * 40, "b" * 40)

    def test_a_non_ascii_name_comes_back_as_the_file_on_disk(
        self, tmp_path, mod, monkeypatch
    ) -> None:
        """Measure the decode against a real report, not only a fixture.

        A fixture is free to be a shape git never writes. Here git is asked for a
        conflict on `中文.txt` and the path the tool returns is checked against the
        file on disk. This arm runs on every platform: a non-ASCII filename is legal
        everywhere (`test_the_shapes_git_really_prints` covers the tab-named file,
        which Windows cannot create — see its reason).
        """
        repo = tmp_path / "cjk-conflict"
        cjk_name = "中文.txt"
        _two_branch_conflict_on(repo, cjk_name)
        monkeypatch.chdir(repo)

        raw = subprocess.run(
            ["git", "merge-tree", "--write-tree", "ours", "theirs"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert raw.returncode == 1, raw.stdout + raw.stderr
        escaped = "".join(f"\\{byte:03o}" for byte in cjk_name.encode("utf-8"))
        # The stage block names the path; whether git spells its bytes escaped
        # (`core.quotePath=true`, the default) or raw is git's choice of spelling, and
        # the tool must return the real name either way.
        assert f'"{escaped}"' in raw.stdout or cjk_name in raw.stdout, raw.stdout

        tree, paths = mod._merge_tree("ours", "theirs")
        assert tree is None
        assert set(paths) == {cjk_name}
        assert (repo / cjk_name).exists()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "Windows rejects a filename holding a control byte: CreateFile fails with "
            "OSError [Errno 22] before git is involved (measured, test-windows run "
            "34787962250). The rule it pins is still covered on Windows by the fixture "
            "tests above, which are strings and platform-independent."
        ),
    )
    def test_the_shapes_git_really_prints(self, tmp_path, mod, monkeypatch) -> None:
        """The real report for the one name that breaks the old reading.

        A tab in the name makes the stage block quote it *and* makes the prose below
        the block tab-separated, which is the whole defect: measured here on real
        git, with the old reading run over the same bytes as a control arm.
        """
        repo = tmp_path / "tab-conflict"
        tab_name = "f\ttab.txt"
        _two_branch_conflict_on(repo, tab_name)
        monkeypatch.chdir(repo)

        raw = subprocess.run(
            ["git", "merge-tree", "--write-tree", "ours", "theirs"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert raw.returncode == 1, raw.stdout + raw.stderr
        assert '"f\\ttab.txt"' in raw.stdout, "the tab name must be quoted in the block"
        assert "Auto-merging f\ttab.txt" in raw.stdout, "the prose carries a real tab"

        tree, paths = mod._merge_tree("ours", "theirs")
        assert tree is None
        assert set(paths) == {tab_name}
        for path in paths:
            assert (repo / path).exists(), f"{path!r} is not a file on disk"

        # The control arm: the reading this change replaces, run over the same real
        # report, names a file that does not exist. Without this the test would only
        # show that the new reading works, not what it is a fix for.
        old_reading = [
            line.split("\t", 1)[1] for line in raw.stdout.splitlines() if "\t" in line
        ]
        assert "tab.txt" in old_reading, old_reading
        assert not (repo / "tab.txt").exists()


def test_a_failing_merge_tree_makes_the_run_unanswerable_not_conflicted(
    queue: tuple[Path, Path], mod, monkeypatch
) -> None:
    """The caller reads exit codes: 2 is "could not measure", 3 is "no final tree"."""
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")
    monkeypatch.chdir(repo)

    real_run = mod._run

    def failing_merge_tree(argv, cwd=None, env=None):
        if "merge-tree" in argv:
            return subprocess.CompletedProcess(
                argv, 1, "", "merge-tree: deadbeef - not something we can merge\n"
            )
        return real_run(argv, cwd=cwd, env=env)

    monkeypatch.setattr(mod, "_run", failing_merge_tree)
    # 3 would tell the caller to resolve a conflict / reorder the plan. Nothing was
    # conflicted; the question could not be answered.
    assert mod.main(["1", "--base", "master"]) == 2


def test_a_suite_that_cannot_run_is_not_reported_healthy(
    queue: tuple[Path, Path], mod, monkeypatch
) -> None:
    """`I could not check` must never surface as `healthy`."""
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(mod, "SUITE", ["-m", "pytest", "--definitely-not-an-option"])
    assert mod.main(["1", "--base", "master"]) == 2

    # The same plan with a runnable suite is healthy, so the arm above fails for
    # the reason claimed (the suite could not run) and not for a broken fixture.
    monkeypatch.setattr(mod, "SUITE", ["-m", "pytest", "tests/", "-q", "--no-header"])
    assert mod.main(["1", "--base", "master"]) == 0


# --- rc 1 is a finding only if the suite's own report says so ------------------
#
# A pytest that never *started* exits 1, exactly like a failing run does. The two
# are told apart by the report, never by the code: measured on this machine, an
# interpreter without pytest prints `No module named pytest` and no per-test line,
# while a real failure prints `FAILED <nodeid> - ...` and ends with a summary
# (`1 failed, 1 passed in 0.01s`; an error in a fixture teardown ends
# `3 passed, 1 error in 0.01s`). Reading the first as a red tree produces a health
# finding about a tree no test ever ran on - invisible to the caller, because it
# looks exactly like a finding.


def test_an_interpreter_without_pytest_is_not_a_red_tree(
    queue: tuple[Path, Path], mod, monkeypatch, capsys
) -> None:
    """The arm that made this guard necessary, as a real subprocess.

    `-m <missing module>` is the shape of "pytest is not installed here": rc 1 with
    nothing that looks like a test report. Exit 2 with the failure text on stderr,
    and no `suite FAILED` on stdout - that phrase is a claim about the tree.
    """
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")
    monkeypatch.chdir(repo)

    monkeypatch.setattr(mod, "SUITE", ["-m", "emrg_no_such_pytest_here"])
    assert mod.main(["1", "--base", "master"]) == 2
    captured = capsys.readouterr()
    assert "suite FAILED" not in captured.out
    assert "could not measure" in captured.err
    # The remedy names an invocation, and it is the one the docstring's Usage block
    # gives: a reader who is told "use the project interpreter" without being told
    # which command is no better off than before.
    assert "uv run --no-sync" in captured.err

    # Both states, same plan: with a runnable suite the tree is healthy, so the arm
    # above measured the missing report rather than a broken fixture.
    monkeypatch.setattr(mod, "SUITE", ["-m", "pytest", "tests/", "-q", "--no-header"])
    assert mod.main(["1", "--base", "master"]) == 0
    assert "suite OK" in capsys.readouterr().out


def test_a_pytest_missing_from_the_interpreter_names_both_causes(
    queue: tuple[Path, Path], mod, monkeypatch, capsys
) -> None:
    """A bare host `python3` and an unsynced `.venv` fail identically.

    Measured in `check-doc-count.py` (cyc20260913-122923): a fresh worktree's `.venv`
    is empty, so the documented invocation fails with this same text. Advising only
    "use the project interpreter" there sends the reader in a circle, so the message
    has to carry the second cause as well.
    """
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")
    monkeypatch.chdir(repo)

    # The exact text an interpreter without pytest prints, from a real subprocess.
    monkeypatch.setattr(
        mod,
        "SUITE",
        ["-c", "import sys; sys.stderr.write('No module named pytest\\n'); sys.exit(1)"],
    )
    assert mod.main(["1", "--base", "master"]) == 2
    err = capsys.readouterr().err
    assert "pytest is not installed" in err
    assert "uv run --no-sync" in err  # cause 1: the wrong interpreter
    assert "uv sync" in err  # cause 2: a worktree whose `.venv` is empty


@pytest.mark.parametrize(
    "report,quoted",
    [
        # A failing test: short-summary line plus the count summary. The node id is
        # what the caller needs, so that is what is quoted.
        (
            "FAILED tests/test_fine.py::test_fine - assert True is False\n1 failed in 0.01s\n",
            "tests/test_fine.py::test_fine",
        ),
        # An error in a fixture teardown, measured: no line starts with FAILED or
        # ERROR but the summary names it, so the summary form is matched anywhere in
        # the line. Anchoring it to the first word would have called this a missing
        # report - and a missing report is now exit 2, i.e. a real failure silently
        # downgraded to "could not measure".
        ("3 passed, 1 error in 0.01s\n", "1 error in 0.01s"),
    ],
)
def test_a_failure_the_report_names_is_still_the_finding(
    queue: tuple[Path, Path], mod, monkeypatch, capsys, report: str, quoted: str
) -> None:
    """The guard must not swallow the failures it exists to deliver.

    Both shapes a red run can arrive in, and in both the tool quotes the report
    rather than replacing it with a placeholder: the caller has to be able to read
    which test failed off this tool's own output.
    """
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")
    monkeypatch.chdir(repo)

    src = "import sys; sys.stdout.write(%r); sys.exit(1)" % report
    monkeypatch.setattr(mod, "SUITE", ["-c", src])
    assert mod.main(["1", "--base", "master"]) == 1
    out = capsys.readouterr().out
    assert "suite FAILED" in out
    assert quoted in out


def test_a_real_failing_suite_is_reported_with_its_node_id(
    queue: tuple[Path, Path], mod, monkeypatch, capsys
) -> None:
    """The same rule against a real pytest run, not a simulated report.

    The arm above pins the discriminator; this one pins that a genuine red tree still
    arrives as exit 1 with the failing test named, through the real `SUITE`
    invocation.
    """
    repo, origin = queue
    _branch_with(
        repo,
        "red",
        {"tests/test_red.py": "def test_red():\n    assert 1 == 2\n"},
    )
    _publish(repo, origin, 1, "red")
    monkeypatch.chdir(repo)

    assert mod.main(["1", "--base", "master"]) == 1
    out = capsys.readouterr().out
    assert "suite FAILED" in out
    assert "tests/test_red.py::test_red" in out


def test_steps_sees_a_red_step_that_the_final_tree_hides(
    queue: tuple[Path, Path],
) -> None:
    """Issue #1161's open half, as a measurement rather than an argument.

    The shape: PR 1 lands a violation of a guard that is already on master, and PR 2
    relaxes that guard. Merged together the tree passes - so the plan's *final* tree
    is perfectly healthy, and the default invocation says so, correctly. But the
    tree after PR 1 exists on master for as long as it takes PR 2 to land, and it is
    red; landing the plan step by step is what produces it.

    Both verdicts are pinned here, because "the tool now fails more often" is not
    the finding - the finding is that it answers about a different tree when asked.
    """
    repo, origin = queue
    _write(repo, "tests/test_no_token_under_data_or_src.py", GUARD_TEST)
    _commit(repo, "the guard, already on master")
    _git(repo, "push", "-q", "origin", "master")

    # PR 1: adds the file the guard rejects. Master + PR 1 is a red tree.
    _branch_with(repo, "violator", {"data/payload.txt": f"contains {TOKEN}\n"})
    # PR 2: relaxes the guard so that file is allowed. Built off master, so it
    # knows nothing about PR 1's file - only about the guard it amends.
    # Relaxed in exactly one place: the violation is no longer recorded, so the
    # guard still runs and still finds the file - it just stops rejecting it.
    relaxed = GUARD_TEST.replace(
        "                hits.append(str(path))", "                pass"
    )
    assert relaxed != GUARD_TEST
    _branch_with(repo, "relaxer", {"tests/test_no_token_under_data_or_src.py": relaxed})
    _publish(repo, origin, 1, "violator")
    _publish(repo, origin, 2, "relaxer")

    # The default question - is the plan's final tree healthy? - is answered yes,
    # and that is not a bug: together the two PRs do produce a green tree.
    final_only = _run_tool(repo, "1", "2")
    assert final_only.returncode == 0, final_only.stdout + final_only.stderr
    assert "suite OK" in final_only.stdout

    # The step question finds the red one, names it, and does not claim the plan is
    # unsafe as a whole (the final tree is still the default verdict).
    every_step = _run_tool(repo, "1", "2", "--steps")
    assert every_step.returncode == 1, every_step.stdout + every_step.stderr
    assert "step 1 (#1)" in every_step.stdout
    assert "FAILED" in every_step.stdout
    assert "test_no_token_under_data_or_src" in (every_step.stdout + every_step.stderr)
    # Step 2's tree is the final tree: the same one the default run measured.
    assert "step 2 (#2)" in every_step.stdout
    assert "suite OK" in every_step.stdout


def test_steps_is_healthy_when_every_step_is(queue: tuple[Path, Path]) -> None:
    """The control arm: `--steps` must not be a permanent failure.

    Without this, "names the red step" and "always red" are the same evidence.
    """
    repo, origin = queue
    _branch_with(repo, "one", {"notes.md": "one\n"})
    _branch_with(repo, "two", {"other.md": "two\n"})
    _publish(repo, origin, 1, "one")
    _publish(repo, origin, 2, "two")

    proc = _run_tool(repo, "1", "2", "--steps")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "step 1 (#1) tree" in proc.stdout
    assert "step 2 (#2) tree" in proc.stdout
    assert "every step healthy (2 suite run(s))" in proc.stdout


def test_every_step_publishes_a_tree_sha_a_later_reader_can_verify(
    queue: tuple[Path, Path], mod, monkeypatch
) -> None:
    """A step's tree identity is printed complete, because a prefix cannot be checked.

    The `--steps` line is the only reading a cycle gets of an intermediate tree, and
    that tree is what its verdict is about. Abbreviated to 12 characters the reading
    cannot be reused: `git rev-parse <40-hex>` echoes any 40-hex string it is handed -
    a control of `deadbeef` came back unchanged, rc 0 - so the way to ask git whether a
    published sha names an object is `git cat-file -t`, which needs all 40 characters.
    Measured (cyc20260918-000146): a cycle holding step 2's `4e0d146538fc` from an
    earlier run could not verify it, and re-ran the whole plan (~116s) to recover the
    sha it had already been shown. The fold's date is pinned for exactly this reason -
    so a step tree sha is comparable between runs, "which is the point of printing one".
    """
    repo, origin = queue
    _branch_with(repo, "one", {"notes.md": "one\n"})
    _branch_with(repo, "two", {"other.md": "two\n"})
    _publish(repo, origin, 1, "one")
    _publish(repo, origin, 2, "two")

    proc = _run_tool(repo, "1", "2", "--steps")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # The parenthesis is the assertion: a 12-character prefix alone fails to match.
    printed = re.findall(
        r"step (\d+) \(#(\d+)\) tree ([0-9a-f]{12}) \(([0-9a-f]{40})\)", proc.stdout
    )
    assert [step for step, _, _, _ in printed] == ["1", "2"], proc.stdout

    monkeypatch.chdir(repo)
    base = _git(repo, "rev-parse", "master")
    heads = [(n, mod._fetch_head(n)) for n in (1, 2)]
    steps = mod.build_plan_steps(base, heads)
    for (_, _, short, full), (_, _, commit) in zip(printed, steps):
        assert full.startswith(short)
        # The published identity is the step's own tree ...
        assert _git(repo, "rev-parse", f"{commit}^{{tree}}").strip() == full
        # ... and a tree git will name, which the prefix alone could not establish.
        assert _git(repo, "cat-file", "-t", full).strip() == "tree"


def test_steps_still_refuses_to_call_a_conflict_unhealthy(
    queue: tuple[Path, Path],
) -> None:
    """A conflicting step is exit 3 under `--steps` too, not "a red step"."""
    repo, origin = queue
    _branch_with(repo, "left", {"README.md": "left\n"})
    _branch_with(repo, "right", {"README.md": "right\n"})
    _publish(repo, origin, 1, "left")
    _publish(repo, origin, 2, "right")

    proc = _run_tool(repo, "1", "2", "--steps")
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "no final tree" in proc.stderr


def test_the_step_commit_is_the_tree_the_steps_would_leave(
    queue: tuple[Path, Path], mod, monkeypatch
) -> None:
    """The step commits are merge commits of the accumulated tree, in order.

    Asserted against real git rather than against the shape of a return value:
    the point of `--steps` is *which tree* is judged, so a change that returned the
    right number of commits describing the wrong trees would be the defect this
    family keeps finding (a verdict about a tree the caller was not looking at).
    """
    repo, origin = queue
    _branch_with(repo, "one", {"notes.md": "one\n"})
    _branch_with(repo, "two", {"other.md": "two\n"})
    _publish(repo, origin, 1, "one")
    _publish(repo, origin, 2, "two")

    monkeypatch.chdir(repo)
    base = _git(repo, "rev-parse", "master")
    # Heads are fetched by the tool itself; go through it rather than guessing refs.
    heads = [(n, mod._fetch_head(n)) for n in (1, 2)]
    steps = mod.build_plan_steps(base, heads)
    assert [step for step, _, _ in steps] == [1, 2]
    assert [number for _, number, _ in steps] == [1, 2]

    # Step 1's commit holds the first PR's file and the second's does too, and
    # step 2's commit is the plan tip the other path computes.
    assert _git(repo, "cat-file", "-p", f"{steps[0][2]}:notes.md").strip() == "one"
    assert _git(repo, "cat-file", "-p", f"{steps[1][2]}:other.md").strip() == "two"
    assert _git(repo, "merge-base", "--is-ancestor", steps[0][2], steps[1][2]) == ""
    assert mod.build_plan_tip(base, heads) == steps[-1][2]


def test_the_same_plan_folds_to_the_same_commits_even_when_a_second_passes(
    queue: tuple[Path, Path], mod, monkeypatch
) -> None:
    """A fold must be a function of its inputs, not of the clock.

    Measured defect (cyc20260913-194108, Windows CI run 34754517824 on this PR):
    the synthetic commits carried the wall clock, and a commit's sha contains its
    committer date, so two folds of *one* plan produced different shas whenever
    they straddled a second boundary. The test above asserts
    `build_plan_tip(...) == steps[-1][2]` - two independent folds - so it failed on
    a slow runner with nothing wrong with either tree, and it is the same reason a
    reader could not compare a `--steps` tree sha between two runs.

    Pinned in both directions: the date the fold carries is the constant (so a
    removed pin is caught even on a fast machine), and a fold deliberately delayed
    past a second boundary is the *same commit* (so the property, not just the
    mechanism, is asserted).
    """
    repo, origin = queue
    _branch_with(repo, "one", {"notes.md": "one\n"})
    _publish(repo, origin, 1, "one")

    monkeypatch.chdir(repo)
    base = _git(repo, "rev-parse", "master")
    heads = [(1, mod._fetch_head(1))]

    first = mod.build_plan_tip(base, heads)
    # %at/%ct are epoch seconds: 946684800 is the constant's 2000-01-01T00:00:00Z,
    # asserted this way so the test does not depend on how git spells a date.
    assert _git(repo, "log", "-1", "--format=%at %ct", first) == "946684800 946684800"

    time.sleep(1.1)  # the boundary the unpinned fold used to trip over
    assert mod.build_plan_tip(base, heads) == first


# --- the base: fetched first, then taken by the name it was written as ---------
#
# The plan is built *onto* a base, and the heads were fetched while the base was
# not, so one answer came from two points in time; and `git rev-parse` consults
# `refs/heads/<name>` before `refs/remotes/<name>`, so one stray local branch
# spelled `origin/master` replaces the remote ref. Both were measured
# (cyc20260914-002731) as *different trees judged under one name*:
#
#     PRE  --base origin/master  base ec7ce11a (origin/master)     tree 5427c8ecb011
#     POST --base origin/master  base 3fbd101d (refs/remotes/...)  tree 43752830eb32
#
# The tests below pin the mechanism (the ref is fetched; the qualified name is the
# one measured) and the property (the judged tree really contains the base's new
# commit), because a header assertion alone would pass on a tool that fetched the
# ref and then resolved the name by precedence anyway.


def _advance_origin_elsewhere(origin: Path, tmp_path: Path, marker: str) -> str:
    """Move the remote's `master` without the checkout under test noticing.

    A commit pushed *from* `repo` also moves `repo`'s own remote-tracking ref, which
    is the state this test has to create rather than avoid - so the commit is made
    in a second clone of the same bare remote, exactly as upstream moves in reality.
    """
    other = tmp_path / marker
    subprocess.run(
        ["git", "clone", "-q", "-b", "master", str(origin), str(other)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "t")
    _git(other, "config", "commit.gpgsign", "false")
    _write(other, "later.txt", "later\n")
    sha = _commit(other, "later")
    _git(other, "push", "-q", "origin", "master")
    return sha


def test_the_base_is_fetched_before_the_plan_is_built(
    queue: tuple[Path, Path], tmp_path: Path, mod, monkeypatch, capsys
) -> None:
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")
    stale = _git(repo, "rev-parse", "refs/remotes/origin/master")

    true_master = _advance_origin_elsewhere(origin, tmp_path, "elsewhere")
    # Preconditions, asserted rather than assumed: the ref is behind, it is behind
    # the commit the remote actually holds, and the stale tree is missing the file
    # the fresh one carries - without these the arm below could pass on a fixture
    # that never created the state under test.
    assert _git(repo, "rev-parse", "refs/remotes/origin/master") == stale
    assert _git(origin, "rev-parse", "master") == true_master != stale
    assert "later.txt" not in _git(repo, "ls-tree", "-r", "--name-only", stale)

    monkeypatch.chdir(repo)
    assert mod.main(["1", "--base", "origin/master"]) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith(
        f"base {true_master[:8]} (refs/remotes/origin/master)"
    )
    # The mechanism and the thing it is for: the ref moved, and the tree the suite
    # judged is the base's new tree rather than the stale one.
    assert _git(repo, "rev-parse", "refs/remotes/origin/master") == true_master
    tree = re.search(r"final tree [0-9a-f]{12} \(([0-9a-f]{40})\)", out)
    assert tree, out
    assert "later.txt" in _git(repo, "ls-tree", "-r", "--name-only", tree.group(1))


def test_a_local_branch_shadowing_the_base_name_does_not_replace_it(
    queue: tuple[Path, Path], mod, monkeypatch, capsys
) -> None:
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")

    # A stray local branch spelled like the remote-tracking ref, at another commit.
    _write(repo, "stray.txt", "stray\n")
    stray = _commit(repo, "stray")
    _git(repo, "branch", "origin/master", stray)
    remote_tip = _git(repo, "rev-parse", "refs/remotes/origin/master")
    # Precondition: a bare-name resolution really does reach the shadow, so the run
    # below is a measurement of precedence rather than of nothing.
    assert _git(repo, "rev-parse", "origin/master") == stray != remote_tip

    monkeypatch.chdir(repo)
    assert mod.main(["1", "--base", "origin/master"]) == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].startswith(
        f"base {remote_tip[:8]} (refs/remotes/origin/master)"
    )
    # The shadow is a local misconfiguration, not a reason to refuse: it is named so
    # the reader can delete it, and it cannot change the answer either way.
    assert "warning: origin/master is ambiguous" in captured.err


def test_a_base_that_cannot_be_fetched_is_a_measurement_error(
    queue: tuple[Path, Path], tmp_path: Path, mod, monkeypatch, capsys
) -> None:
    """A base the tool could not verify is exit 2, never a base it kept anyway.

    Two arms, because they fail at different places and only the second one is
    about the refresh: a name with no ref at all (`origin/nope`), and - the
    dangerous one - an *existing* remote-tracking ref whose remote cannot be
    reached. A tool that swallowed the fetch failure would answer about the ref it
    could not verify, which is the defect the refresh exists to remove. The head
    fetch is stubbed in that arm so the base's fetch is the only thing left that can
    fail: otherwise the run exits 2 on the head instead, and a swallowed base
    failure would pass this test by accident (it did, until the stub was added).
    """
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")
    head = _git(origin, "rev-parse", "refs/pull/1/head")
    monkeypatch.chdir(repo)

    assert mod.main(["1", "--base", "origin/nope"]) == 2
    captured = capsys.readouterr()
    assert "base " not in captured.out
    assert "could not measure" in captured.err

    # Precondition of the second arm: the ref to be read is there, so what fails is
    # the fetch and not the lookup - without this the arm below could pass for the
    # first arm's reason.
    assert _git(repo, "rev-parse", "--verify", "refs/remotes/origin/master")
    monkeypatch.setattr(mod, "_fetch_head", lambda number: head)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    assert mod.main(["1", "--base", "origin/master"]) == 2
    captured = capsys.readouterr()
    assert "base " not in captured.out
    assert "could not measure" in captured.err


def test_a_sha_base_is_taken_literally_and_never_fetched(
    queue: tuple[Path, Path], tmp_path: Path, mod, monkeypatch, capsys
) -> None:
    """Only a remote-tracking name is refreshed; a SHA is immutable by construction.

    Discriminating in both directions: the same unreachable remote that makes the
    `origin/master` arm exit 2 leaves the SHA arm at exit 0, so the arm above is
    measuring the fetch rather than an unrelated failure.
    """
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")
    sha = _git(repo, "rev-parse", "master")
    # the PR ref lives on the remote, not in this checkout
    head = _git(origin, "rev-parse", "refs/pull/1/head")
    # The heads are stubbed so the *only* network operation left is the base's.
    monkeypatch.setattr(mod, "_fetch_head", lambda number: head)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    monkeypatch.chdir(repo)

    assert mod.main(["1", "--base", sha]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith(f"base {sha[:8]} ({sha})")

    assert mod.main(["1", "--base", "origin/master"]) == 2
    assert "could not measure" in capsys.readouterr().err


# --- the tree answers, and its sources are the only copy that answers ----------
#
# Two second copies of a tree can answer in place of it, and both were measured on
# this machine (`cyc20260914-085416`), not assumed:
#
# * another tree on `sys.path` - the environment exports
#   `PYTHONPATH=/Users/argszero/.emrg/install/source:...`, an installed copy of this
#   package, and whichever copy `sys.path` reaches first is the one whose answer the
#   run reports;
# * a bytecode cache inside the tree - a `.pyc` is a second copy of a source and
#   CPython prefers it when the header's `(int(mtime), size)` pair still matches. A
#   *kept* worktree of #1211's landing tree reported `PROJECT_CONTEXT_MAX_CHARS` as
#   `7000` while its own tracked `emrg/server/daemon.py` said `8000`; deleting that
#   worktree's `__pycache__` flipped the suite from failing to passing.


def test_the_suite_env_puts_the_tree_first_and_writes_no_bytecode(
    mod, monkeypatch, tmp_path: Path
) -> None:
    """Both halves of "the tree answers": it wins the name, the run leaves no copy."""
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else/install/source")
    # This machine's own environment already exports 1 (the daemon's heredity); the
    # pin has to be the run's, not the caller's, or a machine that exports nothing
    # silently stops being covered.
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    env = mod._suite_env(tmp_path)
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert entries[0] == str(tmp_path), "the tree under test must be reached first"
    assert "/somewhere/else/install/source" in entries, (
        "the caller's environment is prepended to, not discarded"
    )
    assert env["PYTHONDONTWRITEBYTECODE"] == "1", (
        "a run that writes bytecode leaves a cache the next run could read"
    )

    # Without an inherited PYTHONPATH there is nothing to append, and no empty entry
    # is left behind pointing at the process's cwd.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert mod._suite_env(tmp_path)["PYTHONPATH"] == str(tmp_path)


def test_the_purge_removes_the_trees_caches_and_not_the_harnesss(
    mod, tmp_path: Path
) -> None:
    """The purge's two directions: the tree's own caches go, a populated `.venv` stays."""
    cache = tmp_path / "emrg" / "server" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "daemon.cpython-313.pyc").write_bytes(b"stale")
    source = tmp_path / "emrg" / "server" / "daemon.py"
    source.write_text("X = 1\n", encoding="utf-8")
    stray = tmp_path / "stray.pyc"
    stray.write_bytes(b"stale")

    keeper = tmp_path / ".venv" / "lib" / "__pycache__"
    keeper.mkdir(parents=True)
    (keeper / "y.pyc").write_bytes(b"x")

    removed = mod._purge_bytecode(tmp_path)

    assert not cache.exists()
    assert not stray.exists()
    assert source.exists(), "sources are what is measured and are never touched"
    assert keeper.exists(), "the harness's own environment is not the tree under test"
    # Caches are reported, not the files inside them: the list is a count of caches.
    assert sorted(removed) == ["emrg/server/__pycache__", "stray.pyc"]
    # …and spelled the same way on every platform. The assertion above is what the
    # Windows job of #1214 answered `emrg\\server\\__pycache__` to: the *source* was
    # wrong there (it returned the native spelling) while this test was right, so the
    # arm that discriminates it is `test-windows` - locally the mutant that restores
    # `str(...)` is equivalent, and saying so is cheaper than pretending otherwise.
    # This line catches that mutant wherever the native separator is not `/`.
    assert not any("\\" in name for name in removed)


def test_a_cache_in_the_tree_cannot_answer_for_it(
    queue: tuple[Path, Path], mod, monkeypatch, capsys
) -> None:
    """The discriminating arm: a cache in the tree is gone before the suite runs.

    `git worktree add` produces a fresh tree, so the cache is planted at the one
    moment it could exist - as the worktree is materialised. The suite is then asked
    the question the cache would otherwise answer: does this tree still carry one?
    Exit 0 is only reachable if the purge ran first, so a run that skips the purge
    reports a red tree here - which is what a stale cache does to a real suite.
    """
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")
    monkeypatch.chdir(repo)

    real_run = mod._run
    planted: list[Path] = []

    def run_and_plant(argv, cwd=None, env=None):
        proc = real_run(argv, cwd=cwd, env=env)
        if list(argv[:3]) == ["git", "worktree", "add"]:
            cache = Path(argv[-2]) / "emrg" / "__pycache__"
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "daemon.cpython-313.pyc").write_bytes(b"stale")
            planted.append(cache)
        return proc

    monkeypatch.setattr(mod, "_run", run_and_plant)
    monkeypatch.setattr(
        mod,
        "SUITE",
        [
            "-c",
            "import pathlib, sys;"
            "sys.exit(1 if list(pathlib.Path('.').rglob('__pycache__')) else 0)",
        ],
    )

    assert mod.main(["1", "--base", "master"]) == 0, capsys.readouterr().err
    assert planted, "the cache was never planted, so this arm measures nothing"


# --- a red plan says who owns the failure, and it is not always a PR -------------
#
# Issue #1378, measured (cyc20260918-164110): plans `#1373` and `#1375` were reported as
# "the tree they produce together fails the suite … re-push the PR that owns the failure (a
# push voids its votes)" while the same five rows failed on the base tree too - the harness
# checks the tree out under the OS temp root, itself an allowed write root, so the unpinned
# write-root-dependent rows flip there. Neither PR touches the files that fail, so the
# remedy pointed at a PR that owns nothing: a cycle that believes it either re-pushes its
# own untouched PR (voiding valid votes and re-running CI for a tree that is not the one
# failing) or goes looking inside a diff for a failure that is not there.


def _base_with_a_red_row(repo: Path, origin: Path) -> None:
    """A base whose own suite is red, as the harness materialises it.

    The measured shape is a row whose verdict depends on where the tree is checked out;
    what the reporting has to get right is the same either way, so this fixture plants the
    red row in the base - where the tool will see it in both runs, which is the property
    under test.
    """
    _write(
        repo,
        "tests/test_red_in_the_base.py",
        "def test_red_in_the_base():\n    assert False, 'red in the base'\n",
    )
    _commit(repo, "a red base")
    _git(repo, "push", "-q", "origin", "master")


def test_a_failure_the_plan_inherits_is_not_laid_at_a_prs_door(
    queue: tuple[Path, Path],
) -> None:
    """The base owns it: no PR in the plan does, so no PR may be told to re-push."""
    repo, origin = queue
    _base_with_a_red_row(repo, origin)
    _branch_with(repo, "unrelated", {"notes.md": "unrelated\n"})
    _publish(repo, origin, 4, "unrelated")

    proc = _run_tool(repo, "4")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "test_red_in_the_base" in proc.stderr
    assert "fail on the base tree" in proc.stderr
    assert "No PR in this plan owns those rows" in proc.stderr
    # Named by tree, so the reader can check the claim - the same reason the plan's own
    # verdict prints "final tree <short> (<full>)".
    assert re.search(r"base tree [0-9a-f]{12} \([0-9a-f]{40}\)", proc.stderr)
    # The sentence that made #1378 expensive. A PR is not the owner here, so the remedy
    # must not be offered at all.
    assert "re-push the PR that owns the failure" not in proc.stderr


def test_a_failure_the_plan_produces_is_still_laid_at_its_door(
    queue: tuple[Path, Path],
) -> None:
    """The other half: green alone, red together, and the plan really owns the row.

    It also covers the case where the base does not contain the row at all: pytest runs
    *nothing* when one argument is unresolvable, so the base answers `not found`, and that
    has to be read as "cannot fail there" rather than as an inherited failure.
    """
    repo, origin = queue
    _branch_with(repo, "guard", {"tests/test_no_token_under_data_or_src.py": GUARD_TEST})
    _branch_with(repo, "violator", {"data/payload.txt": f"contains {TOKEN}\n"})
    _publish(repo, origin, 1, "guard")
    _publish(repo, origin, 2, "violator")

    proc = _run_tool(repo, "1", "2")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "re-push the PR that owns the failure" in proc.stderr
    assert (
        "rows this plan's tree owns: "
        "tests/test_no_token_under_data_or_src.py::test_no_token_under_data_or_src"
        in proc.stderr
    )
    assert "fail on the base tree" not in proc.stderr


def test_a_base_that_cannot_be_measured_is_not_a_verdict(
    queue: tuple[Path, Path], mod, monkeypatch, capsys
) -> None:
    """Fail closed: the question could not be answered, so no owner is named.

    The plan's tree *is* red, but "whose failure is it" is the question this run has to
    answer before it can say what to do, and an unanswerable question is rc 2 in this
    family - never a verdict that names the wrong owner.
    """
    repo, origin = queue
    _base_with_a_red_row(repo, origin)
    _branch_with(repo, "unrelated", {"notes.md": "unrelated\n"})
    _publish(repo, origin, 4, "unrelated")
    monkeypatch.chdir(repo)

    def unmeasurable(*args, **kwargs):
        raise mod.MeasurementError("the base tree could not be measured (rc=1): boom")

    monkeypatch.setattr(mod, "_still_red_on", unmeasurable)
    assert mod.main(["4", "--base", "master"]) == 2
    err = capsys.readouterr().err
    assert "could not measure the base" in err
    assert "re-push the PR that owns the failure" not in err


def test_the_rows_are_read_from_the_report_and_not_invented(mod) -> None:
    """The rows are the run's own, because they are what the base is asked to re-run."""
    out = (
        "FAILED tests/test_a.py::test_b - assert 1 == 2\n"
        "FAILED tests/test_c.py::test_d[a b] - assert x\n"
        "ERROR tests/test_e.py::test_f - teardown blew up\n"
        "ERROR: not found: /tmp/base/tests/test_g.py::test_h\n"
        "1 failed, 2 passed in 0.01s\n"
    )
    assert mod._failing_rows(out) == [
        "tests/test_a.py::test_b",
        "tests/test_c.py::test_d[a b]",
        "tests/test_e.py::test_f",
    ]
    # `ERROR: not found:` is a collection error, not a blamed row, and it is matched by
    # the absolute path pytest prints rather than by the id the caller passed.
    assert mod._unfound_ids(
        out, ["tests/test_g.py::test_h", "tests/test_a.py::test_b"]
    ) == {"tests/test_g.py::test_h"}
    # On Windows the argument is named with the platform's separator while the node id
    # keeps `/`. Measured while writing this: an early revision matched the POSIX
    # spelling and nothing else, which on that platform would have read every row the
    # base does not contain as "the base could not be measured" (rc 2).
    assert mod._unfound_ids(
        "ERROR: not found: C:\\Temp\\emrg-plan-suite-a\\base\\tests\\test_g.py::test_h\n",
        ["tests/test_g.py::test_h"],
    ) == {"tests/test_g.py::test_h"}
    # The argument runs to the end of its line, because a node id contains spaces. Measured
    # against a real missing row (`cyc20260918-212523`, pytest 9.1.1 prints the second
    # spelling): read up to the first whitespace, this came back `set()`, and an empty
    # answer here is "could not measure" for a row the run had just named.
    spaced = "tests/test_d.py::test_e[git commit -q -F - <<EOF]"
    for line in (
        f"ERROR: file or directory not found: {spaced}\n",
        f"ERROR: not found: /tmp/emrg-plan-suite-a/base/{spaced}\n",
    ):
        assert mod._unfound_ids(line + "no tests ran in 0.00s\n", [spaced]) == {spaced}


def test_the_two_paragraphs_split_the_rows_and_no_row_is_in_both(mod) -> None:
    """The split is the whole fix: a shared row must not reach the re-push sentence."""
    tree = "ab" * 20
    plan, base = mod._ownership_lines(tree, ["a", "b", "c"], {"b"})
    assert plan.split("owns: ")[1] == "a, c"
    assert "1 of the 3 failing row(s)" in base
    assert tree in base
    # The base paragraph lists the base's rows and only those: a row the plan's tree owns
    # must not reach the sentence that tells the reader to fix the base.
    assert base.split("purged): ")[1].split("\n")[0] == "b"

    # Everything inherited: nothing is asked of a PR at all.
    assert mod._ownership_lines(tree, ["a", "b"], {"a", "b"})[0] == ""
    # Nothing inherited: nothing is said about the base.
    assert mod._ownership_lines(tree, ["a", "b"], set())[1] == ""


# --- issue #1386: a row is read from the machine-readable report ------------------
#
# A red tree is attributed *by row*: the plan's failing rows are re-run on the base, and a
# row the base fails too belongs to no PR (#1378). The text summary is a lossy carrier for
# those rows - it separates the id from the failure's message with `" - "`, which 22 node
# ids in this repository's own suite contain - so the cut row is one the base does not
# contain, which reads as "the base fails nothing", and the row is laid at a PR's door with
# the re-push sentence. The arms below pin the reader, the two id shapes, and both halves
# of the ownership split against a real pytest run.

# A parameter that makes the node id contain the text report's separator.
DASHED_ID_TEST = '''import pytest


@pytest.mark.parametrize("argv", ["git commit -q -F - <<EOF"])
def test_dashed_id(argv):
    assert False, "red with the separator inside the id"
'''

DASHED_ID_ROW = "tests/test_dashed_id.py::test_dashed_id[git commit -q -F - <<EOF]"

# The same, with a *newline* in the parameter. pytest escapes it to `\\n` in an id unless
# the tree turns that escaping off, and with it off the text report really does break the
# line - measured here, which is why the ini option is part of the fixture.
NEWLINE_ID_TEST = '''import pytest


@pytest.mark.parametrize("argv", ["git commit\\n-q -F"])
def test_newline_id(argv):
    assert False, "red with a newline inside the id"
'''

NEWLINE_ID_ROW = "tests/test_newline_id.py::test_newline_id[git commit\n-q -F]"

ESCAPING_OFF = (
    "[tool.pytest.ini_options]\n"
    "disable_test_id_escaping_and_forfeit_all_rights_to_community_support = true\n"
)


def _base_with(repo: Path, origin: Path, files: dict[str, str], message: str) -> None:
    """A base commit carrying `files`, published as `master`, as the tool will fetch it."""
    for relpath, body in files.items():
        _write(repo, relpath, body)
    _commit(repo, message)
    _git(repo, "push", "-q", "origin", "master")


def test_a_base_row_whose_id_holds_the_separator_is_not_laid_at_a_prs_door(
    queue: tuple[Path, Path],
) -> None:
    """Issue #1386, on the path where it did harm: the base's own row, cut by the parse.

    The base is red with a row whose id contains `" - "`, and the only PR in the plan is
    unrelated. Cut at the first `" - "`, the row becomes one the base does not contain, so
    the plan's tree is blamed and the caller is told to re-push a PR that owns nothing.
    """
    repo, origin = queue
    _base_with(repo, origin, {"tests/test_dashed_id.py": DASHED_ID_TEST}, "a dashed row")
    _branch_with(repo, "unrelated", {"notes.md": "unrelated\n"})
    _publish(repo, origin, 4, "unrelated")

    proc = _run_tool(repo, "4")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert DASHED_ID_ROW in proc.stderr
    assert "fail on the base tree" in proc.stderr
    assert "No PR in this plan owns those rows" in proc.stderr
    assert "re-push the PR that owns the failure" not in proc.stderr


def test_a_base_row_whose_id_holds_a_newline_is_read_the_same_way(
    queue: tuple[Path, Path],
) -> None:
    """The second shape: no line-anchored parser can carry this id at all.

    With the escaping off, `pytest` prints the id across two lines, so the text report
    cannot even hold the row; junit escapes the newline as `&#10;` and the id survives
    whole. Everything else is the arm above, because the requirement is the same: the base
    owns the row, so no PR may be told to re-push.
    """
    repo, origin = queue
    _base_with(
        repo,
        origin,
        {"pyproject.toml": ESCAPING_OFF, "tests/test_newline_id.py": NEWLINE_ID_TEST},
        "a row with a newline in its id",
    )
    _branch_with(repo, "unrelated", {"notes.md": "unrelated\n"})
    _publish(repo, origin, 4, "unrelated")

    proc = _run_tool(repo, "4")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert NEWLINE_ID_ROW in proc.stderr
    assert "No PR in this plan owns those rows" in proc.stderr
    assert "re-push the PR that owns the failure" not in proc.stderr


def test_a_plan_owns_a_row_whose_id_holds_the_separator(queue: tuple[Path, Path]) -> None:
    """The other half: the row is the plan's, and it is named whole when it is.

    Truncated, the row was attributed to the plan for the wrong reason - it was one the
    base does not contain. The sentence is the same; the row that reaches it has to be the
    one that failed, or the reader cannot find the test they are told to fix.
    """
    repo, origin = queue
    _branch_with(repo, "dashed", {"tests/test_dashed_id.py": DASHED_ID_TEST})
    _publish(repo, origin, 7, "dashed")

    proc = _run_tool(repo, "7")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"rows this plan's tree owns: {DASHED_ID_ROW}" in proc.stderr
    assert "fail on the base tree" not in proc.stderr


def test_a_run_without_a_report_says_the_rows_are_unverified(
    queue: tuple[Path, Path], mod, monkeypatch, capsys
) -> None:
    """The one path left where the text parse is the source - and it is named, not silent.

    `SUITE` replaced by something that is not pytest writes no junit report, and this is
    the fallback the tool documents for that case. A row list nobody verified is what
    issue #1386 is about, so it is reported on stderr rather than left implicit.
    """
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        mod,
        "SUITE",
        [
            "-c",
            "import sys; sys.stdout.write('FAILED tests/test_fine.py::test_fine - boom\\n"
            "1 failed in 0.01s\\n'); sys.exit(1)",
        ],
    )

    assert mod.main(["1", "--base", "master"]) == 1
    captured = capsys.readouterr()
    assert "no junit report" in captured.err
    assert "tests/test_fine.py::test_fine" in captured.out


def test_the_rows_come_from_the_machine_readable_report(mod, tmp_path: Path) -> None:
    """The reader, against the two shapes and the records that are not rows."""
    report = tmp_path / "junit.xml"
    report.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite>'
        '<testcase classname="tests.test_a" name="test_b" file="tests/test_a.py" line="1">'
        '<failure message="x">boom</failure></testcase>'
        # An id containing the separator the text report splits on. `<` arrives escaped
        # because it is XML, which is also why the id survives: no line to break.
        '<testcase classname="tests.test_a" name="test_d[git commit -q -F - &lt;&lt;EOF]"'
        ' file="tests/test_a.py" line="2"><failure/></testcase>'
        # A real newline in the id: XML escapes it, so the parser returns it whole where
        # the text report would have broken the line.
        '<testcase classname="tests.test_a" name="test_d[x&#10;y]" file="tests/test_a.py"'
        ' line="3"><error/></testcase>'
        # A class-qualified row (the class path is what is left of `classname` after the
        # file's dotted path), and one testcase that did not fail.
        '<testcase classname="tests.test_a.TestK" name="test_in" file="tests/test_a.py"'
        ' line="4"><failure/></testcase>'
        '<testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py" line="5"/>'
        # A skipped test is not a blamed row either, and it is the `<skipped>` child that
        # says so rather than the absence of one.
        '<testcase classname="tests.test_a" name="test_skip" file="tests/test_a.py"'
        ' line="6"><skipped/></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert mod._junit_rows(report) == [
        "tests/test_a.py::test_b",
        "tests/test_a.py::test_d[git commit -q -F - <<EOF]",
        "tests/test_a.py::test_d[x\ny]",
        "tests/test_a.py::TestK::test_in",
    ]
    # No report at all is not "nothing failed": the caller has to be able to tell the two
    # apart, which is what the fallback in `_suite_verdict` keys on.
    assert mod._junit_rows(tmp_path / "absent.xml") is None


def test_a_report_that_cannot_name_a_row_is_unmeasurable(mod, tmp_path: Path) -> None:
    """Three shapes that must not become a row list, and what each one costs.

    A guessed row is worse than no row: it is re-run on the base and, not being there, is
    read as *the base fails nothing* - the attribution error #1386 is about. So every one
    of these is a measurement error (exit 2's family) instead.
    """
    # The default `xunit2` family: no `file`, and a classname alone does not name a path.
    (tmp_path / "xunit2.xml").write_text(
        '<?xml version="1.0"?><testsuites><testsuite>'
        '<testcase classname="tests.test_a" name="test_b"><failure/></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    with pytest.raises(mod.MeasurementError) as exc:
        mod._junit_rows(tmp_path / "xunit2.xml")
    assert "without guessing one" in str(exc.value)

    # A classname that does not belong to the file it names: the class path would have to
    # be guessed, and a guess can name a different test in the same file.
    (tmp_path / "mismatched.xml").write_text(
        '<?xml version="1.0"?><testsuites><testsuite>'
        '<testcase classname="tests.test_other.TestK" name="test_b" file="tests/test_a.py">'
        "<failure/></testcase>"
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    with pytest.raises(mod.MeasurementError):
        mod._junit_rows(tmp_path / "mismatched.xml")

    # A report that is there and cannot be parsed.
    (tmp_path / "broken.xml").write_text("<testsuites>", encoding="utf-8")
    with pytest.raises(mod.MeasurementError) as exc:
        mod._junit_rows(tmp_path / "broken.xml")
    assert "could not be read" in str(exc.value)


def test_the_run_asks_for_the_family_that_writes_the_file_attribute(
    queue: tuple[Path, Path], mod, monkeypatch
) -> None:
    """`-o junit_family=xunit1`, and the report outside the tree it measures.

    Both halves are load-bearing. Without the family the report has no `file` and the rows
    cannot be rebuilt at all; and a report written *inside* the worktree is a file the
    measured suite could itself see - this repository's own suite asserts things about its
    tree's contents.
    """
    repo, origin = queue
    _branch_with(repo, "fine", {"tests/test_fine.py": "def test_fine():\n    assert True\n"})
    _publish(repo, origin, 1, "fine")
    monkeypatch.chdir(repo)

    seen: list[list[str]] = []
    real_run = mod._run

    def spy(argv, cwd=None, env=None):
        seen.append(list(argv))
        return real_run(argv, cwd=cwd, env=env)

    monkeypatch.setattr(mod, "_run", spy)
    assert mod.main(["1", "--base", "master"]) == 0

    runs = [argv for argv in seen if "pytest" in argv]
    assert len(runs) == 1, runs
    suite = runs[0]
    assert "junit_family=xunit1" in suite
    report = Path(suite[suite.index("--junitxml") + 1])
    worktrees = [Path(argv[-2]) for argv in seen if list(argv[:3]) == ["git", "worktree", "add"]]
    assert worktrees, "no worktree was materialised, so this arm measures nothing"
    for worktree in worktrees:
        assert report.parent == worktree.parent
        assert worktree not in report.parents
