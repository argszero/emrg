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
import re
import subprocess
import time
import sys
from pathlib import Path

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


def _run_tool(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--base", "master"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
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
