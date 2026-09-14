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
import sys
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

    tree, landed, apparent, backwards, reversed_inside = mod.landing_reading(base, head)

    assert landed == [("A", "src/feature.txt")]
    assert apparent == [("A", "src/feature.txt"), ("M", "src/shared.txt")]
    assert backwards == [("M", "src/shared.txt")]
    # Here the shared file is untouched by the head, so nothing reads backwards
    # *inside* a path - the name-set rule has this arm covered on its own.
    assert reversed_inside == []
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

    tree, landed, apparent, backwards, reversed_inside = mod.landing_reading(base, head)

    assert landed == apparent == [("A", "src/feature.txt")]
    assert backwards == []
    assert reversed_inside == []
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

    _, landed, apparent, backwards, reversed_inside = mod.landing_reading(base, head)

    assert landed == apparent == [("M", "src/keep.txt")]
    assert backwards == []
    assert reversed_inside == []


def _same_file_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Both sides change **the same file**, far enough apart that the merge is clean.

    The PR rewrites line 1; master rewrites line 30 after the branch point. The path
    name is therefore in both lists - diff(base, head) lists it *and* the landing
    changes it - while the reading of that path in diff(base, head) still prints
    master's own later hunk as a deletion.
    """
    repo = tmp_path / "same-file"
    _init_repo(repo)
    lines = [f"line {i}" for i in range(1, 31)]
    _write(repo, "src/app.py", "\n".join(lines) + "\n")
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    pr_lines = list(lines)
    pr_lines[0] = "CHANGED BY THE PR"
    _write(repo, "src/app.py", "\n".join(pr_lines) + "\n")
    head = _commit(repo, "the PR changes line 1")

    _git(repo, "checkout", "-q", "master")
    master_lines = list(lines)
    master_lines[-1] = "CHANGED BY MASTER LATER"
    _write(repo, "src/app.py", "\n".join(master_lines) + "\n")
    base = _commit(repo, "master moves on inside the same file")
    return repo, base, head


def test_a_shared_path_can_still_read_backwards_inside(mod, tmp_path, monkeypatch) -> None:
    """The shape a path-name comparison cannot see.

    The merge is clean and both lists contain src/app.py, so the name-set rule says
    clean - yet the reading of that path is not the landing: master's own later hunk
    is printed there as a deletion, which is the reading this tool exists to prevent.
    """
    repo, base, head = _same_file_repo(tmp_path)
    monkeypatch.chdir(repo)

    tree, landed, apparent, backwards, reversed_inside = mod.landing_reading(base, head)

    assert landed == apparent == [("M", "src/app.py")]
    assert backwards == []  # the name-set rule is blind here: the path *is* landed
    assert reversed_inside == [("M", "src/app.py")]

    # The evidence, read from git rather than from the tool: the reading of the path
    # prints master's hunk as a deletion, and the landing does not.
    reading = _git(repo, "diff", base, head, "--", "src/app.py")
    assert "-CHANGED BY MASTER LATER" in reading
    landing_commit = _git(
        repo, "commit-tree", tree, "-p", base, "-p", head, "-m", "landing"
    )
    landed_diff = _git(repo, "diff", base, landing_commit, "--", "src/app.py")
    assert "CHANGED BY MASTER LATER" not in landed_diff


def test_the_report_names_a_path_that_reads_backwards_inside(
    mod, tmp_path, monkeypatch
) -> None:
    """What the reviewer is handed: the landing first, then the path to distrust."""
    repo, base, head = _same_file_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mod, "_fetch_head", lambda number: head)

    state, report = mod.check_pr(1, base)

    assert state == "backwards"
    assert "reads backwards inside: 1 of the 1 path(s)" in report
    change, _, hazard = report.partition("  reads backwards inside:")
    assert "M\tsrc/app.py" in change
    assert "in diff(base, head) are landed, but the reading" in hazard
    assert "base's own later hunks on them appear as deletions" in hazard
    assert "M\tsrc/app.py" in hazard
    # The name-set wording must not be used for this shape: the landing *does* change
    # the path, so saying "are the base's own later changes" of it would be false.
    assert "reads backwards:" not in report


def test_both_shapes_are_reported_separately(mod, tmp_path, monkeypatch) -> None:
    """A head carrying both hazards: one path the landing ignores, one it shares."""
    repo = tmp_path / "both"
    _init_repo(repo)
    lines = [f"line {i}" for i in range(1, 31)]
    _write(repo, "src/app.py", "\n".join(lines) + "\n")
    _write(repo, "src/shared.txt", "one\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    pr_lines = list(lines)
    pr_lines[0] = "CHANGED BY THE PR"
    _write(repo, "src/app.py", "\n".join(pr_lines) + "\n")
    head = _commit(repo, "the PR changes src/app.py")
    _git(repo, "checkout", "-q", "master")
    master_lines = list(lines)
    master_lines[-1] = "CHANGED BY MASTER LATER"
    _write(repo, "src/app.py", "\n".join(master_lines) + "\n")
    _write(repo, "src/shared.txt", "one\ntwo\n")
    base = _commit(repo, "master moves on in both files")
    monkeypatch.chdir(repo)

    tree, landed, apparent, backwards, reversed_inside = mod.landing_reading(base, head)

    assert landed == [("M", "src/app.py")]
    assert apparent == [("M", "src/app.py"), ("M", "src/shared.txt")]
    assert backwards == [("M", "src/shared.txt")]
    assert reversed_inside == [("M", "src/app.py")]


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


# --- the base is refreshed, not taken on faith ----------------------------------
#
# Every PR head is fetched from the network, so this tool answers about the heads as
# they are *now*. The base was not, which makes the two halves of one question come
# from different points in time - caught in review of this PR (`cyc20260913-210255`),
# which is why the sibling `check-merge-sequence.py` has had `_refresh_base` since
# `cyc20260912-203927`. Measured on the real repo, with
# `refs/remotes/origin/master` moved back one commit (947377b; master 2f9c552):
#
#     this tool:  base 947377b3 (refs/remotes/origin/master), 1 PR(s) checked   rc 0
#     sibling:    base 2f9c5524 (refs/remotes/origin/master)                    # refreshed
#
# A header reporting `origin/master` for a commit that is not master is the sentence
# `_qualify_ref` was written against, and the verdict is not freshness-neutral: over
# the 26 head refs in that clone, 2 changed state between the stale base and the true
# one, and one of them was the live PR under review - `clean`/exit 0 against the stale
# base, `backwards`/exit 1 against master.


def test_a_remote_tracking_base_is_refreshed_before_use(mod, monkeypatch) -> None:
    """`origin/<branch>` is fetched, so a stale ref cannot be read as the base."""
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, cwd=None, env=None):
        calls.append(argv)
        return _Done()

    monkeypatch.setattr(mod, "_run", fake_run)

    mod._refresh_base("origin/master")

    assert len(calls) == 1, calls
    assert calls[0][0] == "git" and "fetch" in calls[0]
    joined = " ".join(calls[0])
    # Fully qualified destination: a bare `origin/master` makes git create a local
    # branch of that name, shadowing the remote-tracking ref (the sibling measured it).
    assert "refs/heads/master:refs/remotes/origin/master" in joined, calls[0]
    assert "+" in joined, "the refspec must be forced, as for PR heads"


def test_a_sha_or_local_ref_base_is_never_fetched(mod, monkeypatch) -> None:
    """Only a remote-tracking name is refreshed; a SHA and a local branch are literal.

    Fetching on a SHA would be meaningless (it is immutable), and treating a local
    branch as remote would overwrite the caller's own ref with a same-named remote one.
    Both are silent ways to measure a tree the caller did not name.
    """
    calls: list[list[str]] = []

    def fake_run(argv, cwd=None, env=None):
        calls.append(argv)
        return None

    monkeypatch.setattr(mod, "_run", fake_run)

    for ref in (
        "0" * 40,
        "localbase",
        "refs/heads/x",
        "FETCH_HEAD",
        "origin/x:dest",
        # a local branch that merely *looks* like a remote-tracking name: the fetch
        # destination is written fully qualified, so this is the stray that shadows
        # `origin/master` - refreshing "it" would overwrite the caller's own branch
        "refs/heads/origin/master",
        # another remote, and a tag: `origin` is the only remote this tool fetches from
        "refs/remotes/upstream/master",
        "refs/tags/v1.0.0",
    ):
        mod._refresh_base(ref)

    assert calls == [], calls


def test_refreshing_the_base_moves_a_stale_remote_tracking_ref(mod, tmp_path, monkeypatch) -> None:
    """The real thing, with real git: the local ref follows the remote after the call.

    Not mocked, because a mocked `_run` cannot tell a fetch that updates the ref from
    one that does not: a helper that computes the right arguments and never moves the
    ref passes every assertion on its argv. Here the remote is a local bare repo, so
    the arm stays hermetic (no network) while doing an actual fetch.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "master")

    seed = tmp_path / "seed"
    _init_repo(seed)
    _write(seed, "a.txt", "one\n")
    _commit(seed, "first")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-q", "origin", "master")

    repo = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(repo)],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    stale = _git(repo, "rev-parse", "refs/remotes/origin/master")

    _write(seed, "a.txt", "one\ntwo\n")
    advanced = _commit(seed, "the remote moves on")
    _git(seed, "push", "-q", "origin", "master")

    assert _git(repo, "rev-parse", "refs/remotes/origin/master") == stale, "precondition"

    monkeypatch.chdir(repo)
    mod._refresh_base("origin/master")

    assert _git(repo, "rev-parse", "refs/remotes/origin/master") == advanced
    assert _git(repo, "rev-parse", "master") == stale, "no local branch may be created"


# --- both spellings of the same mutable ref, and the symref spelling ----------------
#
# Refreshing only `origin/<branch>` left the *same* remote-tracking ref unrefreshed when
# it was written fully qualified - and that spelling is not exotic: the sibling
# `check-merge-pairs.py::_resolve_base` passes `refs/...` through untouched and its
# refusal text tells callers to "pass the fully-qualified ref you mean". So the tool
# answered from whatever the ref happened to be, rc 0, with the stale commit in the
# header. Measured (`cyc20260913-221656`) in a hermetic clone whose
# `refs/remotes/origin/master` sat one commit behind:
#
#     --base origin/master                 -> base 02dfb130 (true master)  rc 1 backwards
#     --base refs/remotes/origin/master    -> base fed8d1b4 (stale)        rc 0  clean
#     --base origin/HEAD                   -> rc 2, fetch of refs/heads/HEAD fails
#     --base refs/remotes/origin/HEAD      -> base fed8d1b4 (stale)        rc 0  clean
#
# The last two are the same defect through a symref: `<remote>/HEAD` names a remote
# branch only *by pointing at one*, and neither a fetch of `refs/heads/HEAD` (absent
# upstream) nor a fetch *into* the symref (git refuses to lock it) can refresh it.


def _stale_clone(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    """A clone whose `refs/remotes/origin/master` is one commit behind the remote.

    Real git, no network: the remote is a local bare repo. Returns
    (bare, seed, repo, stale, advanced) - `stale` is what the clone holds, `advanced`
    what the remote moved on to, so any arm can be re-armed by writing `stale` back.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "master")

    seed = tmp_path / "seed"
    _init_repo(seed)
    _write(seed, "a.txt", "one\n")
    _commit(seed, "first")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-q", "origin", "master")

    repo = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(repo)],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    stale = _git(repo, "rev-parse", "refs/remotes/origin/master")

    _write(seed, "a.txt", "one\ntwo\n")
    advanced = _commit(seed, "the remote moves on")
    _git(seed, "push", "-q", "origin", "master")

    assert _git(repo, "rev-parse", "refs/remotes/origin/master") == stale, "precondition"
    return bare, seed, repo, stale, advanced


def test_every_remote_tracking_spelling_is_refreshed(mod, monkeypatch) -> None:
    """`refs/remotes/origin/<branch>` is the same mutable ref as `origin/<branch>`.

    Pinned at the argv level so both spellings are visible in one place: a predicate
    that admits only the short one is exactly the shape that was shipped, and it is
    invisible to a test that only ever passes the short spelling.
    """
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, cwd=None, env=None):
        calls.append(argv)
        return _Done()

    monkeypatch.setattr(mod, "_run", fake_run)

    for base in ("origin/master", "refs/remotes/origin/master", "refs/remotes/origin/main"):
        calls.clear()
        mod._refresh_base(base)
        assert len(calls) == 1, (base, calls)
        joined = " ".join(calls[0])
        branch = base.rsplit("/", 1)[-1]
        assert f"refs/heads/{branch}:refs/remotes/origin/{branch}" in joined, (base, calls)
        assert "+" in joined, "the refspec must be forced, as for PR heads"


def test_a_head_spelling_is_refreshed_through_its_symref(mod, monkeypatch) -> None:
    """`origin/HEAD` and `refs/remotes/origin/HEAD` are fetched at the ref they point to.

    Not at their own name: `refs/heads/HEAD` does not exist upstream (the fetch fails),
    and writing into a symref cannot be locked at all - git refuses and leaves the
    symref unchanged, so "refresh it in place" is not an option git offers.
    """
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, cwd=None, env=None):
        calls.append(argv)
        done = _Done()
        if "symbolic-ref" in argv:
            done.stdout = "refs/remotes/origin/trunk\n"
        return done

    monkeypatch.setattr(mod, "_run", fake_run)

    for base in ("origin/HEAD", "refs/remotes/origin/HEAD"):
        calls.clear()
        mod._refresh_base(base)
        probes = [c for c in calls if "symbolic-ref" in c]
        fetches = [c for c in calls if "fetch" in c]
        assert len(probes) == 1 and len(fetches) == 1, (base, calls)
        assert probes[0][-1].endswith("/HEAD"), (base, calls)
        assert "refs/heads/trunk:refs/remotes/origin/trunk" in " ".join(fetches[0]), (base, calls)


def test_a_branch_whose_name_ends_in_head_is_refreshed_not_refused(mod, monkeypatch) -> None:
    """`<branch>/HEAD` is a legal branch name, so that spelling is an ordinary ref.

    The discriminator is `git symbolic-ref`'s exit code, never the `/HEAD` suffix:
    `git check-ref-format --branch feature/HEAD` accepts the name, so
    `refs/remotes/origin/feature/HEAD` is a remote-tracking branch for a branch called
    `feature/HEAD`, and a predicate keyed on the name refused a base that only needed
    filling in - which makes that refusal a regression: the spelling was fetched
    correctly before the `/HEAD` handling existed.
    """
    calls: list[list[str]] = []

    class _Plain:
        """`symbolic-ref` fails - the ref is not a symref; `fetch` succeeds."""

    def fake_run(argv, cwd=None, env=None):
        calls.append(argv)
        done = _Plain()
        done.returncode = 1 if "symbolic-ref" in argv else 0
        done.stdout = ""
        done.stderr = ""
        return done

    monkeypatch.setattr(mod, "_run", fake_run)

    for base in ("origin/feature/HEAD", "refs/remotes/origin/feature/HEAD"):
        calls.clear()
        mod._refresh_base(base)
        probes = [c for c in calls if "symbolic-ref" in c]
        fetches = [c for c in calls if "fetch" in c]
        assert len(probes) == 1 and len(fetches) == 1, (base, calls)
        assert probes[0][-1] == "refs/remotes/origin/feature/HEAD", (base, calls)
        # Refreshed at its own name - not at `HEAD`, and not at a symref's target
        # (there is none).
        joined = " ".join(fetches[0])
        assert "+refs/heads/feature/HEAD:refs/remotes/origin/feature/HEAD" in joined, (
            base,
            calls,
        )


def test_a_head_spelling_that_resolves_outside_origin_is_a_measurement_error(
    mod, monkeypatch
) -> None:
    """A symref that does not lead to `origin`'s tracking refs is refused.

    The tool only knows how to refresh a remote-tracking branch of `origin`; a symref
    pointing at a local branch (or another remote) leaves it unable to name the branch
    to fill in, and answering anyway is the stale-base reading this helper exists to
    prevent.
    """

    class _Elsewhere:
        returncode = 0
        stdout = "refs/heads/master\n"
        stderr = ""

    monkeypatch.setattr(mod, "_run", lambda argv, cwd=None, env=None: _Elsewhere())

    for base in ("origin/HEAD", "refs/remotes/origin/HEAD"):
        with pytest.raises(mod.MeasurementError) as excinfo:
            mod._refresh_base(base)
        assert "symbolic ref" in str(excinfo.value), (base, excinfo.value)


def test_a_head_spelling_that_git_cannot_resolve_is_still_never_answered(
    mod, monkeypatch
) -> None:
    """Not symbolic is not the same as fine: the fetch still has to succeed.

    Dropping the name-keyed refusal must not open a quiet path: when the ref is not a
    symref and the fetch of that name fails (no such branch upstream), the caller gets a
    measurement error rather than the stale commit in a header that names it.
    """
    calls: list[list[str]] = []

    class _Fail:
        returncode = 128
        stdout = ""
        stderr = "fatal: couldn't find remote ref refs/heads/HEAD"

    def fake_run(argv, cwd=None, env=None):
        calls.append(argv)
        done = _Fail()
        if "symbolic-ref" in argv:
            done.returncode = 1
            done.stderr = ""
        return done

    monkeypatch.setattr(mod, "_run", fake_run)

    with pytest.raises(mod.MeasurementError) as excinfo:
        mod._refresh_base("origin/HEAD")

    assert "could not refresh" in str(excinfo.value)
    assert any("fetch" in c for c in calls), ("the fetch must be attempted", calls)

    # ...and through `main` it is exit 2 (could not measure), never a verdict
    monkeypatch.setattr(mod, "_open_pr_numbers", lambda repo: [1])
    assert mod.main(["--base", "refs/remotes/origin/HEAD"]) == 2


def test_a_stale_qualified_base_is_refreshed_with_real_git(mod, tmp_path, monkeypatch) -> None:
    """The defect's effect, with real git: the qualified spelling follows the remote.

    The mocked tests above pin the argv; this one pins that the ref the tool then
    *reads* is the remote's current commit. It is the arm that fails before the fix -
    `_refresh_base` returned immediately, so the ref stayed at `stale` and every
    reading below it was about a tree the caller did not name - and it is re-armed
    between spellings so one arm cannot hand the next an already-current ref.
    """
    _bare, _seed, repo, stale, advanced = _stale_clone(tmp_path)
    monkeypatch.chdir(repo)

    for base in ("refs/remotes/origin/master", "origin/HEAD", "refs/remotes/origin/HEAD"):
        _git(repo, "update-ref", "refs/remotes/origin/master", stale)
        assert _git(repo, "rev-parse", "refs/remotes/origin/master") == stale, base
        mod._refresh_base(base)
        assert _git(repo, "rev-parse", "refs/remotes/origin/master") == advanced, base
    assert _git(repo, "rev-parse", "master") == stale, (
        "the refresh moves the remote-tracking ref only, never a local branch"
    )


def _stale_head_branch_clone(tmp_path: Path) -> tuple[Path, str, str]:
    """A clone whose `refs/remotes/origin/feature/HEAD` is one commit behind the remote.

    The branch name ends in `/HEAD`, which is legal (`git check-ref-format --branch
    feature/HEAD` accepts it) - that is the point: its tracking ref is an ordinary ref,
    not the remote's symref. Returns (repo, stale, advanced).
    """
    bare = tmp_path / "head-remote.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "master")

    seed = tmp_path / "head-seed"
    _init_repo(seed)
    _write(seed, "a.txt", "one\n")
    _commit(seed, "first")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-q", "origin", "master")

    _git(seed, "checkout", "-q", "-b", "feature/HEAD")
    _write(seed, "a.txt", "one\nfeature\n")
    _commit(seed, "a branch whose last name is HEAD")
    _git(seed, "push", "-q", "origin", "feature/HEAD:refs/heads/feature/HEAD")
    _git(seed, "checkout", "-q", "master")

    repo = tmp_path / "head-clone"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(repo)],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    stale = _git(repo, "rev-parse", "refs/remotes/origin/feature/HEAD")

    _git(seed, "checkout", "-q", "feature/HEAD")
    _write(seed, "a.txt", "one\nfeature\ntwo\n")
    advanced = _commit(seed, "the remote moves on")
    _git(seed, "push", "-q", "origin", "feature/HEAD")
    _git(seed, "checkout", "-q", "master")

    assert stale != advanced, "precondition: the clone's ref is behind the remote"
    return repo, stale, advanced


def test_a_remote_branch_named_head_is_refreshed_with_real_git(
    mod, tmp_path, monkeypatch
) -> None:
    """The regression, with real git: `feature/HEAD` is a branch, not the symref.

    This is the arm that fails before the fix: keyed on the name, `_refresh_base` raised
    a measurement error and left the tracking ref at its stale commit, so every reading
    below it named a tree the caller did not ask about. Re-armed between spellings so one
    arm cannot hand the next an already-current ref.
    """
    repo, stale, advanced = _stale_head_branch_clone(tmp_path)
    # The name is not what makes a ref a symref - git is asked, and says no.
    check = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/feature/HEAD"],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
    )
    assert check.returncode != 0, "precondition: the tracking ref is not a symbolic ref"

    monkeypatch.chdir(repo)
    for base in ("origin/feature/HEAD", "refs/remotes/origin/feature/HEAD"):
        _git(repo, "update-ref", "refs/remotes/origin/feature/HEAD", stale)
        assert _git(repo, "rev-parse", "refs/remotes/origin/feature/HEAD") == stale, base
        mod._refresh_base(base)
        assert _git(repo, "rev-parse", "refs/remotes/origin/feature/HEAD") == advanced, base


def test_main_refreshes_the_base_before_measuring(mod, monkeypatch, capsys) -> None:
    """`main` must actually call `_refresh_base` - a correct helper nobody calls is dead.

    Pinned separately because the tests above exercise the helper directly: deleting
    the call from `main` leaves them all green while the defect returns in full (the
    sibling recorded the mutant that did exactly that and survived three tests).
    """
    seen: list[str] = []
    monkeypatch.setattr(mod, "_refresh_base", lambda ref: seen.append(ref))

    class _Done:
        returncode = 0
        stdout = "0" * 40
        stderr = ""

    monkeypatch.setattr(mod, "_run", lambda argv, cwd=None, env=None: _Done())
    monkeypatch.setattr(mod, "_open_pr_numbers", lambda repo: [1])
    monkeypatch.setattr(mod, "_fetch_head", lambda n: "1" * 40)
    monkeypatch.setattr(mod, "_merge_tree", lambda a, b: None)  # conflict: stops early

    rc = mod.main(["1"])

    assert seen == ["origin/master"], seen
    # 3, not 0: the merge conflicts, so no landing tree was read and "nothing reads
    # backwards" would be a claim about nothing. The exit code is pinned so a later
    # change cannot quietly restore a conflict-is-success reading.
    assert rc == 3, rc


def test_a_base_that_cannot_be_refreshed_is_a_measurement_error(mod, monkeypatch, capsys) -> None:
    """A failed fetch is exit 2, never a quiet fall-back to the stale commit.

    Continuing against a base that could not be verified is how a tool answers about
    the wrong tree while reporting a number.
    """

    class _Fail:
        returncode = 128
        stdout = ""
        stderr = "fatal: couldn't find remote ref"

    monkeypatch.setattr(mod, "_run", lambda argv, cwd=None, env=None: _Fail())

    with pytest.raises(mod.MeasurementError) as excinfo:
        mod._refresh_base("origin/master")

    assert "could not refresh" in str(excinfo.value)

    monkeypatch.setattr(mod, "_open_pr_numbers", lambda repo: [1])
    assert mod.main(["--base", "origin/master"]) == 2
    assert "could not measure" in capsys.readouterr().err


# --- the base is not just fresh, it is the ref you named -------------------------
#
# Refreshing the base fixed *when* the ref is read; the other half of the same defect
# is *what the name denotes*. `origin/master` is ambiguous - git consults
# `refs/heads/<name>` before `refs/remotes/<name>`, and git itself creates exactly a
# local branch of that name when a fetch destination is written unqualified, the trap
# `_refresh_base` documents. Measured in review (`cyc20260913-212500`) in a clone with
# a stray `refs/heads/origin/master` at `a2ac6f98` while the remote-tracking ref was
# `0998ed95`:
#
#     rev-parse origin/master                       -> a2ac6f98   (local wins)
#     rev-parse --symbolic-full-name origin/master  -> (empty, rc 0)
#
# so the first version of `_qualify_ref` returned the *typed* name in exactly the case
# its own docstring is about, and `_rev_parse` measured the stray - meaning
# `_refresh_base` wrote `refs/remotes/origin/master` and the next line read something
# else: the refresh appeared to have no effect. The sibling
# `check-merge-sequence.py` records the original measurement (`cyc20260913-072845`,
# a two-cycle-old tree reported as `origin/master`) and resolves by full name.


def _shadow_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A clone whose short `origin/master` is ambiguous: a stray local branch shadows it.

    Returns (repo, remote_tracking_head, stray_head) with the two deliberately different,
    so any reader that follows git's precedence instead of the full name lands on the
    stray. A bare local remote keeps it hermetic.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "master")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "master")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    _write(seed, "a.txt", "one\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "first")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-q", "origin", "master")
    stray = _git(seed, "rev-parse", "HEAD")

    repo = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(repo)],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    _write(seed, "a.txt", "one\ntwo\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "second")
    _git(seed, "push", "-q", "origin", "master")
    tracking = _git(seed, "rev-parse", "HEAD")

    _git(repo, "fetch", "-q", "origin")
    _git(repo, "update-ref", "refs/remotes/origin/master", tracking)
    # The stray: a local branch whose name is also a valid remote-tracking spelling.
    _git(repo, "update-ref", "refs/heads/origin/master", stray)

    assert _git(repo, "rev-parse", "refs/remotes/origin/master") == tracking
    assert _git(repo, "rev-parse", "refs/heads/origin/master") == stray
    return repo, tracking, stray


def test_an_ambiguous_base_resolves_to_the_ref_the_caller_named(
    mod, tmp_path, monkeypatch, capsys
) -> None:
    """`origin/master` means `refs/remotes/origin/master`, not whatever shadows it.

    Git's precedence list puts `refs/heads/<name>` first, so the short spelling can
    denote a stray local branch. Resolving by the full name is what makes the answer
    independent of that; the warning tells the reader the checkout is polluted.
    """
    repo, tracking, stray = _shadow_repo(tmp_path)
    monkeypatch.chdir(repo)

    assert mod._qualify_ref("origin/master") == "refs/remotes/origin/master"
    assert mod._rev_parse("origin/master") == tracking
    assert mod._rev_parse("origin/master") != stray

    err = capsys.readouterr().err
    assert "ambiguous" in err and "shadows" in err, err
    assert "git branch -D origin/master" in err, "the warning must name the fix"

    # Not a regression of the reporting half: a local name still reports itself.
    assert mod._qualify_ref("master") == "refs/heads/master"


def test_a_name_that_denotes_only_a_local_branch_is_refused(mod, tmp_path, monkeypatch) -> None:
    """Refused, not measured: `origin/x` that is only a local branch is not a remote.

    This is the fail-loud half. Measuring it would answer about a tree the caller did not
    name, and "the local branch happens to be at the same commit" is not knowable here.
    """
    repo = tmp_path / "refuse"
    _init_repo(repo)
    _write(repo, "a.txt", "one\n")
    _commit(repo, "first")
    _git(repo, "update-ref", "refs/heads/origin/master", _git(repo, "rev-parse", "HEAD"))
    monkeypatch.chdir(repo)

    with pytest.raises(mod.MeasurementError) as excinfo:
        mod._qualify_ref("origin/master")

    assert "denotes only the local branch" in str(excinfo.value)


def test_the_refresh_is_effective_even_with_a_shadowing_stray(mod, tmp_path, monkeypatch) -> None:
    """The end-to-end arm: refresh, then resolve, gets the refreshed commit.

    Each half passes on its own (the refresh moves the remote-tracking ref; the resolve
    looks up the full name) while their composition is what the caller depends on. With
    the shadow in place the unfixed pairing returned the stray: the refresh wrote one ref
    and the measurement read another.
    """
    repo, tracking, stray = _shadow_repo(tmp_path)
    monkeypatch.chdir(repo)

    mod._refresh_base("origin/master")
    resolved = mod._rev_parse("origin/master")

    assert resolved == tracking
    assert resolved != stray

    # …and it is the same ref the header names, so the printed spelling and the measured
    # commit agree - the property the whole file exists for.
    assert mod._qualify_ref("origin/master") == "refs/remotes/origin/master"


# --- the names are git's, not git's spelling of git's ---------------------------
#
# Both halves of the measurement need the path git *means*: the report prints it, and
# `_path_reading` hands it back to git as a pathspec. Without `-z` git quotes a path
# holding a quote, a backslash, a control byte, or any non-ASCII byte, and a quoted
# path is a pathspec that matches nothing - so the inside-the-path arm answered "" for
# both sides and reported *clean* (measured `cyc20260914-082347`).
#
# Pinned in both directions (#455): the non-ASCII arm and the control-byte arm are
# shown to name the real file *and* to report the hazard, with git itself supplying
# the evidence; the plain-name arm next door keeps passing unchanged for the control.


def _named_same_file_repo(tmp_path: Path, name: str, tag: str) -> tuple[Path, str, str]:
    """`_same_file_repo` with the shared file carrying `name`.

    Same fixture as `_same_file_repo` - both sides change the file far enough apart
    that the merge is clean, so the path is in both lists while its reading is not the
    landing - with the name as the only difference. The name is what the tool used to
    get wrong.
    """
    repo = tmp_path / tag
    _init_repo(repo)
    lines = [f"line {i}" for i in range(1, 31)]
    _write(repo, name, "\n".join(lines) + "\n")
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    pr_lines = list(lines)
    pr_lines[0] = "CHANGED BY THE PR"
    _write(repo, name, "\n".join(pr_lines) + "\n")
    head = _commit(repo, "the PR changes line 1")

    _git(repo, "checkout", "-q", "master")
    master_lines = list(lines)
    master_lines[-1] = "CHANGED BY MASTER LATER"
    _write(repo, name, "\n".join(master_lines) + "\n")
    base = _commit(repo, "master moves on inside the same file")
    return repo, base, head


def test_a_non_ascii_path_is_named_as_it_is(mod, tmp_path, monkeypatch) -> None:
    """The report must print the file's name, not git's octal spelling of it.

    Measured on this fixture with the file named 中文.txt: the default
    `--name-status` spelling is `"\\344\\270\\255\\346\\226\\207.txt"`. That names
    nothing a resolver can open, and as a pathspec it matches nothing - so the
    inside-the-path arm compared "" with "" and this repo reported *clean* where the
    same fixture named `src/app.py` as reading backwards inside.
    """
    name = "中文.txt"
    repo, base, head = _named_same_file_repo(tmp_path, name, "cjk")
    monkeypatch.chdir(repo)

    _, landed, apparent, backwards, reversed_inside = mod.landing_reading(base, head)

    assert landed == apparent == [("M", name)]
    assert backwards == []  # the name-set rule is blind here: the path *is* landed
    assert reversed_inside == [("M", name)]

    # The evidence, from git rather than from the tool: the reading of that path does
    # print the base's own later hunk as a deletion (so "reads backwards inside" is
    # true of it), while the unseparated spelling really is quoted - which is why the
    # old reading could never have matched it.
    assert "CHANGED BY MASTER LATER" in _git(repo, "diff", base, head, "--", name)
    quoted = _git(repo, "diff", "--name-status", "--no-renames", base, head)
    assert quoted.split("\t", 1)[1].startswith('"')
    assert "\\344" in quoted


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot create a file whose name holds a control byte (CreateFile)",
)
def test_a_tab_in_a_path_is_named_as_it_is(mod, tmp_path, monkeypatch) -> None:
    """The control-byte arm: a real tab arrives quoted as `"f\\ttab.txt"`.

    Without `-z` the old reading split the line on *tabs*, so this name was the one
    that produced a second, non-existent path in the sibling tools' reports; here it
    made the path unopenable and the inside-the-path arm blind.
    """
    name = "f\ttab.txt"
    repo, base, head = _named_same_file_repo(tmp_path, name, "tab")
    monkeypatch.chdir(repo)

    _, landed, apparent, _, reversed_inside = mod.landing_reading(base, head)

    assert landed == apparent == [("M", name)]
    assert reversed_inside == [("M", name)]
    assert "CHANGED BY MASTER LATER" in _git(repo, "diff", base, head, "--", name)


def test_the_report_names_a_non_ascii_path_as_it_is(mod, tmp_path, monkeypatch) -> None:
    """What the reviewer is handed: the real name, in the hazard line."""
    name = "中文.txt"
    repo, base, head = _named_same_file_repo(tmp_path, name, "cjk-report")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mod, "_fetch_head", lambda number: head)

    state, report = mod.check_pr(1, base)

    assert state == "backwards"
    assert "reads backwards inside: 1 of the 1 path(s)" in report
    assert f"M\t{name}" in report
    # The quoted spelling is not a name a resolver can use, so it must not be printed.
    assert "\\344" not in report


def test_a_payload_that_is_not_status_path_pairs_is_a_measurement_error(mod, monkeypatch) -> None:
    """A shape this tool does not know is unmeasurable, never a shorter list.

    `-z` writes `status\\0path\\0`; a reader that skipped an unpaired trailing field
    would answer about a change it never read, so the pairing is asserted and the
    failure is loud (the fake stands in for a payload git cannot be made to produce).
    """
    fake = subprocess.CompletedProcess(
        args=["git", "diff"], returncode=0, stdout="M\0only-a-path\0M\0", stderr=""
    )
    monkeypatch.setattr(mod, "_run", lambda *args, **kwargs: fake)

    with pytest.raises(mod.MeasurementError) as excinfo:
        mod._changed_paths("a" * 40, "b" * 40)

    assert "not status/path pairs" in str(excinfo.value)
