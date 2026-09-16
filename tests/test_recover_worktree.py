"""The reconstructible-dirt criterion and `scripts/recover-worktree.py` (#1237).

The guard's contract is "a cycle must not destroy work that exists nowhere else".
These pin the two halves: dirt that IS unique is refused (nothing touched), and
dirt that is NOT unique is converged **reversibly** — the scenario the measured
33-cycle deadlock was made of, built here as a real repository.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "recover-worktree.py"


def _load():
    spec = importlib.util.spec_from_file_location("recover_worktree", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
    )


def _status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain").stdout


def _new_repo(path: Path, content: str = "v1") -> None:
    """A one-commit repository. `-b master` because the criterion's upstream ref is
    the default branch, and a test whose branch name drifts would measure nothing."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text(content, encoding="utf-8")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "base")


def _with_upstream(tmp_path: Path):
    """The deadlock's exact shape: HEAD behind, worktree carrying upstream's bytes.

    Measures `(repo, head_sha)`: the tree is dirty (one modified tracked file) and
    holds **no unique work at all** — the file's bytes are the upstream tip's blob.
    Of the 33 lost cycles, this was the state of the working tree.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "master", str(origin)],
        capture_output=True, text=True, timeout=30,
    )
    work = tmp_path / "work"
    _new_repo(work, "v1")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "master")
    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    (work / "f.txt").write_text("v2", encoding="utf-8")
    _git(work, "commit", "-q", "-am", "v2")
    _git(work, "push", "-q", "origin", "master")
    _git(work, "reset", "-q", "--hard", head)      # HEAD back to v1 …
    (work / "f.txt").write_text("v2", encoding="utf-8")  # … worktree at upstream's v2
    assert _status(work).startswith(" M"), _status(work)
    return work, head


# ── the criterion itself, against real repositories ──────────────────────────


def test_dirt_carrying_upstreams_bytes_is_reconstructible(tmp_path):
    """False = nothing would be lost. This is the case that cost 33 cycles."""
    work, _ = _with_upstream(tmp_path)
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(work))
    assert loses is False, why
    assert "upstream" in why


def test_a_modification_found_nowhere_is_unique(tmp_path):
    """The real protection, in the other direction: nothing upstream holds these bytes."""
    _new_repo(tmp_path / "repo", "v1")
    repo = tmp_path / "repo"
    (repo / "f.txt").write_text("host's unreleased work", encoding="utf-8")
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True
    assert "f.txt" in why


def test_an_untracked_file_is_unique(tmp_path):
    """Untracked content exists in exactly one place, so it is never reconstructible."""
    _new_repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    (repo / "notes.md").write_text("only here", encoding="utf-8")
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True
    assert "notes.md" in why


def test_an_untracked_file_is_unique_even_when_upstream_has_those_bytes(tmp_path):
    """The clause that decides this case: "untracked == 0" is a precondition.

    Found by mutation (removing the `??` fast path changed no existing answer —
    the content comparison reaches the same verdict for a file git never tracked).
    That is not true here: when the path is *absent from HEAD* but present in the
    upstream tip, the content comparison finds a matching blob and would release
    the file, while the criterion says otherwise. An untracked file is content git
    has never tracked, so it is the host's by construction; releasing it because a
    copy was published once would discard a file the host created. Conservative by
    design, and the safe side: naming it unique only refuses.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "master", str(origin)],
        capture_output=True, text=True, timeout=30,
    )
    repo = tmp_path / "repo"
    _new_repo(repo, "v1")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "master")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "b.txt").write_text("published elsewhere", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "b")
    _git(repo, "push", "-q", "origin", "master")
    _git(repo, "reset", "-q", "--hard", head)   # b.txt now exists only upstream
    (repo / "b.txt").write_text("published elsewhere", encoding="utf-8")  # untracked again
    assert _status(repo).strip() == "?? b.txt", _status(repo)

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True, "untracked content is never reconstructible"
    assert "b.txt" in why


def test_a_deletion_loses_nothing(tmp_path):
    """A deleted tracked file is restored by discarding, so it is not unique work."""
    _new_repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    (repo / "f.txt").unlink()
    assert _status(repo).startswith(" D"), _status(repo)
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is False, why


def test_a_commit_only_on_this_branch_is_unique(tmp_path):
    """A branch reset would orphan it: dirty *and* ahead is never reconstructible."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "master", str(origin)],
        capture_output=True, text=True, timeout=30,
    )
    repo = tmp_path / "repo"
    _new_repo(repo)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "master")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "local only")
    (repo / "f.txt").write_text("v2", encoding="utf-8")
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True
    assert "only on this branch" in why


def test_a_clean_tree_is_not_dirt(tmp_path):
    _new_repo(tmp_path / "repo")
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(tmp_path / "repo"))
    assert loses is False
    assert "clean" in why


# ── the tool: refuse, converge, stay reversible ──────────────────────────────


def test_the_tool_refuses_unique_work_and_touches_nothing(tmp_path, capsys):
    """The refusal is the guard working: the file must survive the attempt."""
    repo = tmp_path / "repo"
    _new_repo(repo)
    (repo / "notes.md").write_text("only here", encoding="utf-8")

    assert _load().recover(repo, apply=True) == 1
    out = capsys.readouterr().out
    assert "refused" in out
    assert (repo / "notes.md").read_text(encoding="utf-8") == "only here"
    assert _git(repo, "stash", "list").stdout.strip() == ""
    assert not (Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
                / "emrg-recovery-receipt.json").exists()


def test_the_tool_converges_reconstructible_dirt_reversibly(tmp_path, capsys):
    """The deadlock's state, repaired: clean tree, HEAD unmoved, work recoverable."""
    work, head = _with_upstream(tmp_path)

    assert _load().recover(work, apply=False) == 0
    assert "recoverable" in capsys.readouterr().out
    assert _status(work).startswith(" M"), "a dry run must not change the tree"

    assert _load().recover(work, apply=True) == 0
    out = capsys.readouterr().out
    assert "recovered" in out and "reversible" in out
    assert _status(work).strip() == "", "the tool's whole job is a clean tree"
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head, "HEAD must not move"

    # Reversibility is the reason this action is allowed at all: every byte is
    # still there, one command away.
    _git(work, "stash", "pop")
    assert (work / "f.txt").read_text(encoding="utf-8") == "v2"


def test_the_tool_writes_a_receipt_of_what_it_moved(tmp_path):
    """The structural guard's contract: every release of a safety rule is a receipt."""
    work, head = _with_upstream(tmp_path)

    assert _load().recover(work, apply=True) == 0
    git_dir = Path(_git(work, "rev-parse", "--absolute-git-dir").stdout.strip())
    receipt = json.loads(
        (git_dir / "emrg-recovery-receipt.json").read_text(encoding="utf-8")
    )
    # The receipt must not re-dirty the tree it just cleaned — measured while
    # writing this tool, when the receipt lived beside it.
    assert _status(work).strip() == "", "the receipt itself must not be dirt"

    assert receipt["repo"] == str(work)
    assert receipt["head_before"] == head
    assert receipt["head_after"] == head, "the receipt must show HEAD did not move"
    assert any(line.startswith(" M") for line in receipt["status_before"])
    assert receipt["status_after"] == []
    assert "stash" in receipt["action"]
    assert "stash pop" in receipt["reversible_with"]
    assert "upstream" in receipt["reason"]


def test_the_action_asks_the_criterion_itself_before_touching_anything(tmp_path):
    """Defence in depth: the action refuses unique work without being told to.

    Called with no measured verdict (the way a future caller who has not read this
    module would call it), it must ask the criterion itself. Otherwise the safety of
    the guard would rest on every caller remembering to check first -- and the one
    that forgets would discard a host's unsaved work while the receipt called it a
    recovery.
    """
    repo = tmp_path / "repo"
    _new_repo(repo)
    (repo / "notes.md").write_text("only here", encoding="utf-8")

    ok, detail = _load().TaskHandler._recover_dirty_tree_sync(str(repo))
    assert ok is False and "refused" in detail, detail
    assert (repo / "notes.md").read_text(encoding="utf-8") == "only here"
    assert _git(repo, "stash", "list").stdout.strip() == "", "nothing may be moved aside"

    # The other direction, same entry point: reconstructible dirt does converge.
    work, head = _with_upstream(tmp_path)
    ok, detail = _load().TaskHandler._recover_dirty_tree_sync(str(work))
    assert ok is True and "stash pop" in detail, detail
    assert _status(work).strip() == ""


def test_a_clean_tree_is_a_no_op(tmp_path, capsys):
    repo = tmp_path / "repo"
    _new_repo(repo)

    assert _load().recover(repo, apply=True) == 0
    assert "nothing to recover" in capsys.readouterr().out
    assert _git(repo, "stash", "list").stdout.strip() == ""


def test_a_directory_that_is_not_a_repo_cannot_be_measured(tmp_path, capsys):
    """Exit 2 — a question that could not be answered is never reported as a pass."""
    plain = tmp_path / "plain"
    plain.mkdir()

    assert _load().recover(plain, apply=True) == 2
    assert "could not measure" in capsys.readouterr().out


def test_main_parses_the_repo_and_apply_flags(tmp_path, capsys):
    """The CLI wiring, so the documented invocation is the tested one."""
    work, _ = _with_upstream(tmp_path)

    assert _load().main(["--repo", str(work)]) == 0
    assert "recoverable" in capsys.readouterr().out
    assert _status(work).startswith(" M"), "without --apply nothing is written"
