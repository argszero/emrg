"""What the structural guard judges: the directory it was pointed at.

Community issue #1507: a task directory that is *inside* a repository but not at its
root was read as "not a repository", because the guards tested
``os.path.isdir(<dir>/.git)`` — a marker that is absent for every directory that is not
the root of a checkout. The reported consequence is the loss scenario of issue #979 one
shape over: the probe answered "clean", so the tier was never held, and the host's
uncommitted work sat in the very directory the task was allowed to write in (measured
here: `rm -f unsaved.txt` is BLOCK at `read-only` and ALLOW at `workspace-write`).

Three readers decide together — the probe, the loss criterion, and the recovery that
converges a reconstructible tree — and they are the same reading of the same scope:
**the repository git reports, and the directory the guard was pointed at inside it**.
A task directory at the root passes no pathspec and is judged exactly as before, so the
narrowing only ever applies one level down, where the guard previously did not fire.

The scope is also what keeps the fix from paying remedy #1's price. Judging the *root*
instead (the issue's first suggestion) would hold a cycle read-only for dirt it cannot
reach — writes outside its workspace are blocked by the sandbox — and, measured on this
host, would hand the recovery a **repository-wide** `git stash push`, which moved the
parent repository's untracked file as well.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from emrg.protocol import InstanceIdentity
from emrg.server.git_utils import ensure_local_exclude, repo_scope, runtime_exclude_entry
from emrg.server.scheduler import TaskHandler


def _git(repo: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    assert cp.returncode == 0, f"git {args} failed: {cp.stderr}"
    return cp.stdout


def _status(repo: Path, prefix: str | None = None) -> str:
    """The repository's own status, read *at the root* (so paths are root-relative),
    optionally limited to one subtree — the same reading the guard's readers make."""
    args = ["status", "--porcelain"]
    if prefix is not None:
        args += ["--", prefix]
    return _git(repo, *args)


def _parent_and_task(tmp_path: Path) -> tuple[Path, Path]:
    """A repository with a committed tree and a working directory one level down."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, timeout=20)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    task = repo / "work" / "clone"
    task.mkdir(parents=True)
    (task / "base.txt").write_text("base", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (task / ".git").exists() and pytest.fail("precondition: the task dir is not a checkout")
    return repo, task


def _handler(path: Path) -> TaskHandler:
    return TaskHandler(
        name="emrg-task", config={"path": str(path)}, interval=60,
        identity=InstanceIdentity(),
    )


def _exclude_file(repo: Path) -> Path:
    out = _git(repo, "rev-parse", "--git-path", "info/exclude").strip()
    p = Path(out)
    return p if p.is_absolute() else repo / p


# ── the probe: is *this* directory in a tree, and is it dirty? ──────────────


def test_the_probe_fires_for_a_directory_inside_a_repository(tmp_path):
    """The reported defect, in the shape it was reported: the work is inside the task's
    own directory — the only place its writes can reach — and the marker test missed it.
    """
    repo, task = _parent_and_task(tmp_path)
    (task / "unsaved.txt").write_text("the host's work", encoding="utf-8")

    assert not (task / ".git").exists(), "precondition: no marker, so no marker test"
    assert _status(repo, "work/clone").strip() == "?? work/clone/unsaved.txt", (
        "precondition: the dirt is inside the task's own directory"
    )

    assert TaskHandler._is_dirty_tree_sync(str(task)) is True


def test_the_sandbox_holds_that_directory_read_only(tmp_path):
    """The consequence the issue names: with the probe blind, the tier was never held and
    `rm -f unsaved.txt` was permitted in the directory holding the host's work."""
    repo, task = _parent_and_task(tmp_path)
    (task / "notes.txt").write_text("work that exists nowhere else", encoding="utf-8")

    assert asyncio.run(_handler(task)._effective_sandbox()) == "read-only"
    assert (task / "notes.txt").read_text(encoding="utf-8") == (
        "work that exists nowhere else"
    )


def test_a_parent_repositorys_own_dirt_is_not_the_tasks_dirt(tmp_path):
    """Remedy #1's price, declined: dirt *outside* the task's directory is work the task
    cannot write to (the sandbox blocks writes outside its workspace), so holding the
    cycle read-only for it would spend the tier on somebody else's tree — the shape that
    cost 38 cycles when the guard judged the wrong object."""
    repo, task = _parent_and_task(tmp_path)
    (repo / "parent-unsaved.txt").write_text("the host's work", encoding="utf-8")

    assert _status(repo).strip() == "?? parent-unsaved.txt", "precondition: only the parent is dirty"
    assert TaskHandler._is_dirty_tree_sync(str(repo)) is True, "precondition: the parent is dirty"

    assert TaskHandler._is_dirty_tree_sync(str(task)) is False
    assert asyncio.run(_handler(task)._effective_sandbox()) == "workspace-write"


def test_a_directory_in_no_repository_is_still_left_alone(tmp_path):
    """Fail-open, unchanged: no repository means no tree to protect, and the loss
    criterion answers in the same direction as the probe rather than guessing."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "notes.txt").write_text("not under version control at all", encoding="utf-8")

    assert repo_scope(str(plain)) is None
    assert TaskHandler._is_dirty_tree_sync(str(plain)) is False
    loses, why = TaskHandler._dirty_tree_would_lose_work_sync(str(plain))
    assert loses is False and "not in a git repository" in why


# ── the exclusion: whose `.emrg/` is it, and relative to what? ─────────────


def test_the_entry_names_the_runtime_dirs_path_in_the_repository(tmp_path):
    """The runtime data is EMRG's own in this shape too, and the entry has to name where
    it actually sits: `/.emrg/` covers a runtime dir at the root, not one a directory
    down — the same "which directory is this about?" confusion, one gate over."""
    repo, task = _parent_and_task(tmp_path)
    (task / ".emrg" / "sessions").mkdir(parents=True)
    (task / ".emrg" / "sessions" / "history.jsonl").write_text("{}\n", encoding="utf-8")

    assert TaskHandler._is_dirty_tree_sync(str(task)) is True, "precondition: EMRG's own dirt"
    assert ensure_local_exclude(str(task)) == "added"

    assert runtime_exclude_entry("work/clone") in _exclude_file(repo).read_text(encoding="utf-8")
    assert TaskHandler._is_dirty_tree_sync(str(task)) is False, (
        "EMRG's own bookkeeping must stop being the task's dirt here as well"
    )
    assert _status(repo, "work/clone").strip() == ""
    assert not (repo / ".gitignore").exists(), "the project's .gitignore is not ours"


def test_the_sandbox_stops_reading_emrgs_own_runtime_data_as_the_hosts(tmp_path):
    """The end-to-end shape of the cost this fix is about, one level down: the task's
    only dirt is EMRG's own bookkeeping, and the tier it costs must be the ordinary one.

    Measured for issue #1507: the exclusion was skipped entirely here, because the
    handler's own marker test answered "not a repository" for a directory that has no
    `.git` — so the one directory EMRG really does dirty kept its cycle read-only
    forever, and the recovery that would clear it was gated behind the same tier.
    """
    repo, task = _parent_and_task(tmp_path)
    (task / ".emrg" / "sessions").mkdir(parents=True)
    (task / ".emrg" / "sessions" / "history.jsonl").write_text("{}\n", encoding="utf-8")

    assert _status(repo, "work/clone").strip() == "?? work/clone/.emrg/", (
        "precondition: the only dirt in the repository is the runtime dir"
    )

    assert asyncio.run(_handler(task)._effective_sandbox()) == "workspace-write"
    assert runtime_exclude_entry("work/clone") in _exclude_file(repo).read_text(encoding="utf-8"), (
        "the entry the tier rests on was actually written"
    )
    assert _status(repo).strip() == "", "and the repository is clean as a result"


def test_an_unanchored_entry_already_covers_a_nested_runtime_dir(tmp_path):
    """Git reads an unanchored `.emrg` at *any* depth, so an exclude file that already
    says `.emrg` already covers `/work/clone/.emrg/` — rewriting it would append a
    second line saying the same thing in another hand's style."""
    repo, task = _parent_and_task(tmp_path)
    exclude = _exclude_file(repo)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".emrg\n", encoding="utf-8")
    before = exclude.read_bytes()

    assert ensure_local_exclude(str(task)) == "present"
    assert exclude.read_bytes() == before, "the file must not be rewritten to say it twice"

    (task / ".emrg").mkdir()
    (task / ".emrg" / "history.jsonl").write_text("{}\n", encoding="utf-8")
    assert _status(repo, "work/clone").strip() == "", "and git really does cover it"


def test_a_prefixed_entry_is_written_once(tmp_path):
    """Idempotent in the new shape too: a second call recognises the entry it wrote."""
    repo, task = _parent_and_task(tmp_path)

    assert ensure_local_exclude(str(task)) == "added"
    first = _exclude_file(repo).read_bytes()
    assert ensure_local_exclude(str(task)) == "present"
    assert _exclude_file(repo).read_bytes() == first


def test_the_runtime_dir_of_the_root_is_still_only_its_own(tmp_path):
    """The root's entry stays `/.emrg/`: excluding a nested `.emrg/` would be a claim
    about somebody else's directory, and the prefixed form must not have loosened it."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, timeout=20)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    nested = repo / "vendor" / ".emrg"
    nested.mkdir(parents=True)
    (nested / "keep.txt").write_text("not ours", encoding="utf-8")

    assert ensure_local_exclude(str(repo)) == "added"

    assert _status(repo).strip() == "?? vendor/", (
        "an anchored root entry must not cover a nested directory"
    )


# ── the recovery: it converges, and only what it owns ─────────────────────


def test_the_recovery_moves_only_the_tasks_own_dirt(tmp_path):
    """`git stash push` is repository-wide — measured for the issue: run in a task
    directory one level down it moved the parent repository's untracked file too. So the
    scope the verdict is read in is also the scope the convergence acts in, and the
    post-check is asked there as well: the parent's surviving dirt is not a failed
    convergence."""
    repo, task = _parent_and_task(tmp_path)
    (task / "base.txt").unlink()  # a deletion: loses nothing, so it is recoverable
    (repo / "parent-unsaved.txt").write_text("the host's work", encoding="utf-8")

    status, detail = TaskHandler._recover_dirty_tree_sync(str(task))

    assert status == "recovered", detail
    assert _status(repo, "work/clone").strip() == "", "the task's own dirt is converged"
    assert (repo / "parent-unsaved.txt").read_text(encoding="utf-8") == "the host's work", (
        "the parent repository's work must not be moved by this task's recovery"
    )
    assert _status(repo) == "?? parent-unsaved.txt\n", "still the parent's, still untracked"
    assert _git(repo, "stash", "list").strip(), "the convergence is reversible"


def test_the_recovery_refuses_a_subtree_that_holds_unique_work(tmp_path):
    """The safety counterpart: scoping the judgment must not make unique work in the
    task's own directory recoverable — nothing is touched there."""
    repo, task = _parent_and_task(tmp_path)
    (task / "unsaved.txt").write_text("work that exists nowhere else", encoding="utf-8")

    status, detail = TaskHandler._recover_dirty_tree_sync(str(task))

    assert status == "refused", detail
    assert (task / "unsaved.txt").read_text(encoding="utf-8") == (
        "work that exists nowhere else"
    )
    assert not _git(repo, "stash", "list").strip(), "a refusal touches nothing"
