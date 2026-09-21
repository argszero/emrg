"""The repository-local exclude for EMRG's own runtime directory.

Rant 2026-09-21T10:12:01: an open-source task's clone carried this instance's
own `.emrg/` (sessions, memory, the client log) as untracked and unignored
dirt, so the structural dirty-tree guard — which asks whether a tree holds work
that exists nowhere else — answered "yes" about EMRG's own bookkeeping and
forced 38 consecutive cycles to `read-only`, and the read-only tier is what
refused the git verbs that would have converged the tree.

The dirt is EMRG's, not the repository's, so it belongs in the repository's
*local* ignore file: `.git/info/exclude` is per-clone, is never committed, and
leaves the upstream `.gitignore` (which belongs to the project's maintainers)
alone. These tests hold both halves: the runtime directory stops being dirt,
and every other kind of dirt still is.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from emrg.protocol import InstanceIdentity
from emrg.server.git_utils import EXCLUDE_ENTRY, ensure_local_exclude
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


def _repo(path: Path) -> Path:
    """A repository with one commit, at `path`."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], capture_output=True, timeout=20)
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    (path / "base.txt").write_text("base", encoding="utf-8")
    _git(path, "add", "base.txt")
    _git(path, "commit", "-qm", "base")
    return path


def _status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain")


def _exclude_file(repo: Path) -> Path:
    out = _git(repo, "rev-parse", "--git-path", "info/exclude").strip()
    p = Path(out)
    return p if p.is_absolute() else repo / p


def _runtime_dir(repo: Path) -> None:
    """What this instance writes into a directory it works in."""
    (repo / ".emrg" / "sessions" / "emrg-evolution-emrg-task").mkdir(parents=True)
    (repo / ".emrg" / "sessions" / "emrg-evolution-emrg-task" / "history.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )


def _runtime_file(repo: Path) -> str:
    """One path under the runtime directory, for `git check-ignore` to judge.

    A file rather than the bare directory: git ignores what it can see, and an
    empty directory is invisible to it either way.
    """
    return str(repo / ".emrg" / "sessions" / "emrg-evolution-emrg-task" / "history.jsonl")


def _ignored(repo: Path, path: str) -> bool:
    """git's own verdict, asked of the repository the path lives in."""
    cp = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )
    return cp.returncode == 0


def test_the_guard_stops_seeing_this_instances_own_runtime_dir(tmp_path):
    """The acceptance the rant asks for: the dirt is EMRG's, so the probe must not
    judge the tree dirty for it — and the probe is what the tier decision reads."""
    repo = _repo(tmp_path / "clone")
    _runtime_dir(repo)

    assert TaskHandler._is_dirty_tree_sync(str(repo)) is True, "precondition: real dirt"

    assert ensure_local_exclude(str(repo)) == "added"

    assert TaskHandler._is_dirty_tree_sync(str(repo)) is False, (
        "git status — the reading the dirty-tree guard makes — must no longer "
        "report this instance's own runtime directory"
    )
    assert _status(repo).strip() == ""
    assert EXCLUDE_ENTRY in _exclude_file(repo).read_text(encoding="utf-8")


def test_the_projects_own_gitignore_is_not_touched(tmp_path):
    """The upstream `.gitignore` belongs to the project's maintainers: this fix is
    local-only and must not appear in anything that could be committed."""
    repo = _repo(tmp_path / "clone")
    before = sorted(p.name for p in repo.iterdir())

    assert ensure_local_exclude(str(repo)) == "added"

    assert sorted(p.name for p in repo.iterdir()) == before
    assert not (repo / ".gitignore").exists()
    assert _status(repo).strip() == "", "a tracked file would show here as dirt"


def test_the_second_call_changes_nothing(tmp_path):
    """Registered, then touched again on every connect: the write is idempotent,
    and a file this helper did not write is not reformatted by it."""
    repo = _repo(tmp_path / "clone")

    assert ensure_local_exclude(str(repo)) == "added"
    first = _exclude_file(repo).read_bytes()

    assert ensure_local_exclude(str(repo)) == "present"
    assert _exclude_file(repo).read_bytes() == first


def test_existing_patterns_are_preserved(tmp_path):
    """The file may already be there — the host's own entries stay untouched and
    stay before ours."""
    repo = _repo(tmp_path / "clone")
    exclude = _exclude_file(repo)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    host_text = "# host's own\n*.local/\nbuild/\n"
    exclude.write_text(host_text, encoding="utf-8")

    assert ensure_local_exclude(str(repo)) == "added"

    after = exclude.read_text(encoding="utf-8")
    assert after.startswith(host_text), "existing content must be a byte prefix"
    assert after.endswith(EXCLUDE_ENTRY + "\n")
    assert after.count(EXCLUDE_ENTRY) == 1


def test_a_directory_that_is_not_a_repository_is_left_alone(tmp_path):
    """No git, no file: the helper is a no-op rather than an `os.makedirs` for
    `.git/info` in somebody's plain directory."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert ensure_local_exclude(str(plain)) == "not-a-repo"
    assert list(plain.iterdir()) == []
    assert ensure_local_exclude(str(tmp_path / "does-not-exist")) == "not-a-repo"


def test_the_entry_is_anchored_to_the_repository_root(tmp_path):
    """`/.emrg/` and not `.emrg/`: a nested `.emrg/` inside a subdirectory is some
    other tool's directory, and excluding it would be a claim this repository
    cannot make."""
    repo = _repo(tmp_path / "clone")
    _runtime_dir(repo)
    nested = repo / "vendor" / ".emrg"
    nested.mkdir(parents=True)
    # A file, not just the directory: git does not track empty directories, so a
    # bare `vendor/.emrg/` would leave the status empty and prove nothing.
    (nested / "keep.txt").write_text("not ours", encoding="utf-8")

    assert ensure_local_exclude(str(repo)) == "added"

    status = _status(repo)
    assert "vendor/" in status, (
        f"the anchored entry must not cover a nested directory: {status!r}"
    )
    assert status.replace("?? vendor/\n", "").strip() == "", (
        f"the root runtime directory must be the only thing excluded: {status!r}"
    )


def test_a_linked_worktree_is_covered_by_the_common_git_dir(tmp_path):
    """A worktree's `.git` is a file, and git reads its excludes from the *common*
    git dir — measured here: writing the per-worktree one
    (`<main>/.git/worktrees/<name>/info/exclude`) left the directory reported as
    untracked, so asking git for the path is the only correct implementation."""
    main = _repo(tmp_path / "main")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt), "HEAD")
    assert (wt / ".git").is_file(), "precondition: linked worktree"
    _runtime_dir(wt)

    assert TaskHandler._is_dirty_tree_sync(str(wt)) is True, "precondition: real dirt"

    assert ensure_local_exclude(str(wt)) == "added"
    assert TaskHandler._is_dirty_tree_sync(str(wt)) is False

    written = _exclude_file(wt)
    assert written == main / ".git" / "info" / "exclude"


def test_the_task_handler_excludes_before_it_judges(tmp_path):
    """The task-side hook: a cycle starting in a clone whose only dirt is EMRG's
    own runtime directory keeps its configured tier instead of spending itself
    read-only — which is the 38-cycle cost the rant measured."""
    repo = _repo(tmp_path / "clone")
    _runtime_dir(repo)

    handler = TaskHandler(
        name="emrg-task", config={"path": str(repo)}, interval=60,
        identity=InstanceIdentity(),
    )
    tier = asyncio.run(handler._effective_sandbox())

    assert tier == "workspace-write", (
        "dirt that is this instance's own bookkeeping must not cost the cycle "
        f"its tier (got {tier!r})"
    )
    assert EXCLUDE_ENTRY in _exclude_file(repo).read_text(encoding="utf-8")


def test_real_dirt_still_forces_read_only(tmp_path):
    """The safety counterpart of the test above: the hook removes EMRG's own
    directory from the verdict, not the verdict — a cycle in that same tree
    still loses its tier when the tree holds someone else's unsaved work."""
    repo = _repo(tmp_path / "clone")
    _runtime_dir(repo)
    (repo / "notes.txt").write_text("work that exists nowhere else", encoding="utf-8")

    handler = TaskHandler(
        name="emrg-task", config={"path": str(repo)}, interval=60,
        identity=InstanceIdentity(),
    )

    assert asyncio.run(handler._effective_sandbox()) == "read-only"
    assert (repo / "notes.txt").is_file(), "the guard leaves that work untouched"
    assert (repo / "notes.txt").read_text(encoding="utf-8") == (
        "work that exists nowhere else"
    )


def test_registering_a_project_writes_the_exclude_too(tmp_path):
    """The daemon-side call site: `_touch_project` is where this instance takes a
    directory on as a project, so a later session in it never starts from a tree
    whose dirt is EMRG's own bookkeeping.

    Built with `object.__new__` on purpose — `_touch_project` reads one path
    attribute and writes one file, and a test of it has no business constructing
    (let alone starting) a server.
    """
    from emrg.server.daemon import EmrgServer

    repo = _repo(tmp_path / "clone")
    _runtime_dir(repo)
    assert TaskHandler._is_dirty_tree_sync(str(repo)) is True, "precondition: real dirt"
    server = object.__new__(EmrgServer)
    server._projects_log = tmp_path / "config" / "projects.yml"

    server._touch_project(str(repo))

    assert EXCLUDE_ENTRY in _exclude_file(repo).read_text(encoding="utf-8")
    assert TaskHandler._is_dirty_tree_sync(str(repo)) is False


def test_a_nested_project_directory_learns_to_ignore_its_own_runtime_dir(tmp_path):
    """The daemon-side call site, one directory shape over (issue #1527).

    `_touch_project` used to call the exclude helper only when `<cwd>/.git`
    existed — the *root* question, `repo_scope`'s other answer. A project
    registered at a directory inside a repository (a package in a monorepo, this
    instance's own `work/` tree, a clone nested in a larger checkout) therefore
    never learned to ignore its runtime data, and the dirt came back one gate
    over from issue #1507.

    Both directions are measured here rather than argued: the root form does not
    reach a nested `.emrg/`, and the scoped entry does.
    """
    from emrg.server.daemon import EmrgServer

    repo = _repo(tmp_path / "repo")
    nested = repo / "work" / "clone"
    nested.mkdir(parents=True)
    _runtime_dir(nested)
    runtime_file = _runtime_file(nested)
    assert not (nested / ".git").exists(), "precondition: in the repo, not at its root"

    # Control: the root's own entry is not a substitute for the scoped one.
    exclude = _exclude_file(repo)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(EXCLUDE_ENTRY + "\n", encoding="utf-8")
    assert not _ignored(repo, runtime_file), (
        "`/.emrg/` is anchored at the root and must not cover a nested `.emrg/` — "
        "which is why the scope has to be resolved, not assumed"
    )
    assert TaskHandler._is_dirty_tree_sync(str(nested)) is True, "precondition: real dirt"

    server = object.__new__(EmrgServer)
    server._projects_log = tmp_path / "config" / "projects.yml"
    server._touch_project(str(nested))

    entries = _exclude_file(repo).read_text(encoding="utf-8").splitlines()
    assert "/work/clone/.emrg/" in entries, entries
    assert _ignored(repo, runtime_file), "git check-ignore must agree with the entry"
    assert TaskHandler._is_dirty_tree_sync(str(nested)) is False


def test_touch_project_writes_nothing_for_a_directory_in_no_repository(tmp_path, caplog):
    """The same deletion's other direction (issue #1527).

    With the marker gate gone, the callee's own refusal is the guard: a directory
    in no repository gains no `.git/` tree, no exclude file and no "now ignores"
    line — while the registration this method exists for still happens.
    """
    from emrg.server.daemon import EmrgServer

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    _runtime_dir(plain)
    before = sorted(p.name for p in plain.iterdir())

    server = object.__new__(EmrgServer)
    server._projects_log = tmp_path / "config" / "projects.yml"
    with caplog.at_level("INFO"):
        server._touch_project(str(plain))

    assert sorted(p.name for p in plain.iterdir()) == before
    assert not (plain / ".git").exists(), "no git dir is conjured into a plain directory"
    assert "now ignores .emrg/" not in caplog.text, caplog.text
    registered = (tmp_path / "config" / "projects.yml").read_text(encoding="utf-8")
    assert str(plain) in registered, "the registration half must be unaffected"
