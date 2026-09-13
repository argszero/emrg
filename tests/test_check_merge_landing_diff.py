"""Tests for scripts/check-merge-landing-diff.py - what does merging a PR change?

Background (cycle cyc20260913-203027)
-------------------------------------
Reviewing #1190, its head sat one commit behind master (#1189 had rewritten
`scripts/classify-conflict.py` after the branch point), and `git diff master head`
listed 5 paths where the landing changes 2. The other three were #1189's own
additions, printed as 276 deleted lines - the diff read as "this PR reverts the
conflict classifier". The fact that decided the review was the landing tree's
blobs being identical to master's, reconstructed by hand; `scripts/` now answers it
in one command.

Pinned in both directions (#455 - never infer from one state alone):

* a head that is merely *behind* the base has paths that read backwards - the
  finding, and they are named;
* a head that *contains* the base tip reads cleanly;
* a PR that genuinely removes lines the base just added is **not** a reversal:
  the same removal appears in the landing, so nothing reads backwards. Without
  this arm a tool that equated "a removal" with "reads backwards" would pass;
* a conflict is its own state, not a reading and not a verdict;
* a git failure is a measurement error, never health.

Hermetic: real local git repos, cheap, no network and no GitHub.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-merge-landing-diff.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_merge_landing_diff", SCRIPT)
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


def _behind_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A base that moved on after the branch point: the state the trap needs.

    Returns (repo, base, head) with `base` a descendant of the branch point, so the
    head does not contain master's later change to src/shared.txt.
    """
    repo = tmp_path / "behind"
    _init_repo(repo)
    _write(repo, "src/shared.txt", "one\n")
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/feature.txt", "new\n")
    head = _commit(repo, "the PR adds its own file")

    _git(repo, "checkout", "-q", "master")
    _write(repo, "src/shared.txt", "one\ntwo\n")
    base = _commit(repo, "master moves on without the PR")
    return repo, base, head


def _fresh_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A head that contains the base tip: the two readings agree by construction."""
    repo = tmp_path / "fresh"
    _init_repo(repo)
    _write(repo, "src/shared.txt", "one\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/feature.txt", "new\n")
    head = _commit(repo, "the PR adds its own file")
    base = _git(repo, "rev-parse", "master")
    return repo, base, head


# --- the finding: the reading and the change are different sets -----------------


def test_a_head_behind_the_base_has_paths_that_read_backwards(
    mod, tmp_path, monkeypatch
) -> None:
    """The measured instance: 5 paths in the diff, 2 in the change.

    src/shared.txt is master's own later commit. It appears in diff(base, head)
    because the head still holds the old copy - so the diff prints master's change
    reversed, as if the PR were undoing it. The landing does not touch it.
    """
    repo, base, head = _behind_repo(tmp_path)
    monkeypatch.chdir(repo)

    tree, landed, apparent, backwards = mod.landing_reading(base, head)

    assert landed == [("A", "src/feature.txt")]
    assert apparent == [("A", "src/feature.txt"), ("M", "src/shared.txt")]
    assert backwards == [("M", "src/shared.txt")]
    # The landing tree is a tree of the merge, not the head's tree: if the tool had
    # answered about the head, landed would equal apparent and backwards would be [].
    assert tree != _git(repo, "rev-parse", f"{head}^{{tree}}")


def test_the_report_names_the_landing_change_and_the_backwards_paths(
    mod, tmp_path, monkeypatch
) -> None:
    """What the reviewer is handed: the change first, the misleading paths named."""
    repo, base, head = _behind_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mod, "_fetch_head", lambda number: head)

    state, report = mod.check_pr(1, base)

    assert state == "backwards"
    change, _, backwards = report.partition("  reads backwards:")
    assert "A\tsrc/feature.txt" in change
    assert "src/shared.txt" not in change
    assert "1 of the 2 path(s)" in backwards
    assert "M\tsrc/shared.txt" in backwards


def test_a_head_containing_the_base_tip_reads_cleanly(mod, tmp_path, monkeypatch) -> None:
    """The control: with nothing to be behind, there is nothing to distrust."""
    repo, base, head = _fresh_repo(tmp_path)
    monkeypatch.chdir(repo)

    tree, landed, apparent, backwards = mod.landing_reading(base, head)

    assert landed == apparent == [("A", "src/feature.txt")]
    assert backwards == []
    assert tree != ""


def test_a_removal_the_pr_really_makes_is_not_a_reversal(
    mod, tmp_path, monkeypatch
) -> None:
    """Direction that a sloppy tool gets wrong.

    Here the PR removes lines the base had *and contains the base tip*, so the
    removal is the PR's own. `diff(base, head)` shows a removal and the landing shows
    the same removal - nothing reads backwards. A tool that treated any removal as a
    reversal would report the trap here.
    """
    repo = tmp_path / "revert"
    _init_repo(repo)
    _write(repo, "src/keep.txt", "a\nb\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/keep.txt", "a\n")
    head = _commit(repo, "the PR removes the second line")
    base = _git(repo, "rev-parse", "master")
    monkeypatch.chdir(repo)

    _, landed, apparent, backwards = mod.landing_reading(base, head)

    assert landed == apparent == [("M", "src/keep.txt")]
    assert backwards == []


def _conflict_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Both sides change the same line: the merge cannot produce a landing tree."""
    repo = tmp_path / "conflict"
    _init_repo(repo)
    _write(repo, "src/shared.txt", "base\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/shared.txt", "theirs\n")
    head = _commit(repo, "branch version")
    _git(repo, "checkout", "-q", "master")
    _write(repo, "src/shared.txt", "ours\n")
    base = _commit(repo, "master version")
    return repo, base, head


def test_a_conflict_is_its_own_state_not_a_reading(mod, tmp_path, monkeypatch) -> None:
    """No landing tree: an unanswerable reading, and never health."""
    repo, base, head = _conflict_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mod, "_fetch_head", lambda number: head)

    with pytest.raises(mod.Conflict):
        mod.landing_reading(base, head)

    state, report = mod.check_pr(1, base)
    assert state == "conflict"
    assert "no landing tree" in report


def test_a_git_failure_is_a_measurement_error_not_health(mod, tmp_path, monkeypatch) -> None:
    """An unanswerable question must never be reported as a clean reading."""
    repo, base, head = _fresh_repo(tmp_path)
    monkeypatch.chdir(repo)
    with pytest.raises(mod.MeasurementError):
        mod.landing_reading("no-such-ref-and-never-will-be", head)
    with pytest.raises(mod.MeasurementError):
        mod.landing_reading(base, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")


# --- the family invariant: no mutable ref name reaches merge-tree ---------------


def test_the_base_reaches_merge_tree_as_a_resolved_commit(
    mod, tmp_path, monkeypatch
) -> None:
    """`master` goes in; a 40-hex commit must come out, at every entry point."""
    repo, _, head = _fresh_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mod, "_fetch_head", lambda number: head)
    seen: dict[str, str] = {}
    real = mod._merge_tree

    def spy(base: str, other: str) -> str | None:
        seen["base"] = base
        seen["head"] = other
        return real(base, other)

    monkeypatch.setattr(mod, "_merge_tree", spy)
    state, _ = mod.check_pr(1, "master")

    assert state == "clean"
    assert re.fullmatch(r"[0-9a-f]{40}", seen["base"]), seen
    assert re.fullmatch(r"[0-9a-f]{40}", seen["head"]), seen


def test_the_header_names_the_ref_that_was_measured(mod, tmp_path, monkeypatch) -> None:
    """A short name is printed fully qualified; a SHA is printed as itself.

    The header spelling is how the wrong-tree defect hides: a local `origin/master`
    branch shadows `refs/remotes/origin/master`, and a header that repeats the typed
    spelling then reports the wrong commit under the right name.
    """
    repo, base, head = _fresh_repo(tmp_path)
    monkeypatch.chdir(repo)
    assert mod._qualify_ref("master") == "refs/heads/master"
    assert mod._qualify_ref("feature") == "refs/heads/feature"
    assert mod._qualify_ref(base) == base


# --- exit codes -----------------------------------------------------------------


def test_main_exit_codes(mod, tmp_path, monkeypatch, capsys) -> None:
    """0 = nothing reads backwards, 1 = the trap, 2 = could not measure, 3 = conflict."""
    repo, base, head = _behind_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mod, "_fetch_head", lambda number: head)
    assert mod.main(["1", "--base", base]) == 1
    assert "reads backwards" in capsys.readouterr().err

    repo2, base2, head2 = _fresh_repo(tmp_path)
    monkeypatch.chdir(repo2)
    monkeypatch.setattr(mod, "_fetch_head", lambda number: head2)
    assert mod.main(["1", "--base", base2]) == 0
    assert "no path reads backwards" in capsys.readouterr().out

    assert mod.main(["1", "--base", "no-such-ref"]) == 2
    assert "could not measure" in capsys.readouterr().err

    repo3, base3, head3 = _conflict_repo(tmp_path)
    monkeypatch.chdir(repo3)
    monkeypatch.setattr(mod, "_fetch_head", lambda number: head3)
    assert mod.main(["1", "--base", base3]) == 3
    assert "No landing tree" in capsys.readouterr().err

    def boom(number: int) -> str:
        raise mod.MeasurementError("gh pr list failed")

    monkeypatch.setattr(mod, "_fetch_head", boom)
    assert mod.main(["1", "--base", base3]) == 2
    assert "could not measure" in capsys.readouterr().err
