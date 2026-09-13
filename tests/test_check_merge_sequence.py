"""Tests for scripts/check-merge-sequence.py - does a merge *plan* land healthy trees?

Background (cycle cyc20260912-174026)
-------------------------------------
Every sibling gate answers a question about one PR or one merge. The queue is
stuck on a question none of them asks: given a plan ("merge these PRs in this
order"), does every step still land a tree the repo's guards accept?

Measured on master `02e43c8`, on the queue as it actually stood:

    master + #1167              -> CLEAN, documents 1541, collects 1541   ok
    master + #1166              -> CLEAN, documents 1541, collects 1541   ok
    master + #1167 then #1166   -> CLEAN, documents 1541, collects 1560   GUARD FAILS

Both PRs held the same count value, so the second merge rewrote a line that was
already equal: no conflict, one copy kept, and the stale number rode into master
with the guard going red afterwards, on master, where nobody was looking.

The property that makes this worth a tool rather than a note: the danger runs
*inverse* to the signal. Two PRs with different count values always conflict
(which is safe - it makes someone stop); two with the same value always merge
silently. `check-merge-order.py` ranks a pair by how little it dirties others,
which is exactly the silent case, so "choose the cheapest order" reads as
advice to take the unsafe step.

What is pinned here, in both directions (#455 - never infer from one side):
* the DANGER case: a clean step whose tree fails the guard, exit 1;
* the OK case: a clean step whose tree passes, exit 0, no warning;
* the conflict case: no tree, no verdict - a conflict is not a finding, so it is
  exit 3, not 1; and it is not health either, so it is not 0: 0 is defined as
  "every step measured and passing", and a stopped plan measured a *prefix*
  (on this queue, usually none of it).

The git orchestration is faked (no git, no pipeline runs in CI): `_merge_commit`
and `_guard_verdict` are replaced, and the replacements are asserted to have been
called with the right commits - a test that passes because the code under test
was never invoked proves nothing.

The measurement itself is NOT faked (cycle cyc20260912-201557)
--------------------------------------------------------------
The orchestration tests above stub `_guard_verdict` in every case, which left the
whole mapping from the guard's exit code to a verdict uncovered. Measured, not
assumed: replacing that function with a body that returns `(True, "guard OK")`
*without consulting the guard at all* kept all five tests above green. That is a
**fail-open** mutant - the tool would print `OK` for every plan, including the
dangerous one it exists to catch, and this suite would not notice. Fail-open is
precisely the defect class this family of gates is written to prevent, so the
measurement layer is now driven for real, in both directions, below.

Why the real thing is affordable: `check-doc-count.py` collects through
`sys.executable -m pytest --collect-only`, so a two-line tree gives a real verdict
in a fraction of a second, with no `uv`, no network and no dependency on the
project's own suite. The child guard is the repository's real file (copied
byte-for-byte), because a stub of the guard would only re-test this suite's belief
about it - the exact failure the mutant above demonstrates.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-merge-sequence.py"
CHILD_GUARD = REPO_ROOT / "scripts" / "check-doc-count.py"

BASE = "a" * 40
C1 = "b" * 40
C2 = "c" * 40
C3 = "d" * 40


def _load_module():
    spec = importlib.util.spec_from_file_location("check_merge_sequence", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _plan(mod, monkeypatch, heads, verdicts, base=BASE, prs=(1, 2)):
    """Drive main() with faked head resolution, merges and guard verdicts.

    `heads` maps PR number -> commit sha. `verdicts` maps PR number ->
    (commit this step produces, (passed, report)), or None for a conflict.
    The chain is built from those commits, so a step's merge input is the
    previous step's output - the property the tool exists for.
    """
    calls: list[tuple[str, str]] = []
    chain: dict[tuple[str, str], str | None] = {}
    prev = base
    for n in prs:
        produced = None if verdicts[n] is None else verdicts[n][0]
        chain[(prev, heads[n])] = produced
        if produced is None:
            break
        prev = produced

    monkeypatch.setattr(mod, "_rev_parse", lambda ref: base)
    monkeypatch.setattr(mod, "_fetch_head", lambda n: heads[n])

    def fake_merge(a, b):
        calls.append((a, b))
        return chain.get((a, b))

    monkeypatch.setattr(mod, "_merge_commit", fake_merge)

    verdict_map = {v[0]: v[1] for v in verdicts.values() if v is not None}
    monkeypatch.setattr(mod, "_guard_verdict", lambda tree, workdir: verdict_map[tree])

    # `git rev-parse <commit>^{tree}` - echo the commit back as its own tree so
    # the verdict map can be keyed by the produced commit.
    monkeypatch.setattr(
        mod,
        "_run",
        lambda argv, cwd=None: _FakeProc(argv[-1].removesuffix("^{tree}")),
    )
    rc = mod.main([str(n) for n in prs])
    return rc, calls


class _FakeProc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


# --- the finding -----------------------------------------------------------


def test_a_clean_step_that_lands_an_unhealthy_tree_is_reported(mod, monkeypatch, capsys):
    """The whole reason the tool exists: exit 1 and the failing numbers named."""
    heads = {1: C1, 2: C2}
    verdicts = {
        1: (C1, (True, "documents 1541")),
        2: (C2, (False, "documents 1541 but 1560 are collected")),
    }
    rc, calls = _plan(mod, monkeypatch, heads, verdicts)
    out = capsys.readouterr().out

    assert rc == 1, "an unhealthy step must not exit 0"
    assert "DANGER" in out
    assert "#2" in out
    assert "1560" in out, "the report must name the actual numbers"
    # The merge chain must be a chain: the second step merges onto the *first
    # step's output*, not onto the base. A tool that measured every step against
    # master would miss the resonance it was written for.
    assert calls == [(BASE, C1), (C1, C2)]


def test_every_step_merges_onto_the_previous_step(mod, monkeypatch, capsys):
    """Three PRs: each merge's first parent is the previous merge's commit."""
    heads = {1: C1, 2: C2, 3: C3}
    verdicts = {
        1: (C1, (True, "documents 1")),
        2: (C2, (True, "documents 2")),
        3: (C3, (True, "documents 3")),
    }
    rc, calls = _plan(mod, monkeypatch, heads, verdicts, prs=(1, 2, 3))

    assert rc == 0
    assert calls == [(BASE, C1), (C1, C2), (C2, C3)]


# --- must not cry wolf -----------------------------------------------------


def test_a_healthy_plan_exits_zero(mod, monkeypatch, capsys):
    heads = {1: C1, 2: C2}
    verdicts = {1: (C1, (True, "documents 1530")), 2: (C2, (True, "documents 1541"))}
    rc, _ = _plan(mod, monkeypatch, heads, verdicts)
    out = capsys.readouterr().out

    assert rc == 0
    assert "DANGER" not in out
    assert "OK" in out


def test_a_conflict_is_not_a_finding(mod, monkeypatch, capsys):
    """No tree means no verdict - and no verdict must not be spelled "verified".

    On this queue most PRs conflict on the count line, so a tool that failed on
    conflicts would be red by default and read as noise: the step must not be
    reported as a failure (exit 1). But exit 0 is documented as "every step of
    the plan was measured and landed a tree that passes the guards", and a plan
    stopped at step 1 measured nothing: measured on the live queue
    (`cyc20260913-084752`), the *default* invocation - every open PR ascending -
    stopped at `#1136` and exited 0 having judged no tree at all, a verdict
    byte-identical to a fully verified plan. So the state is its own: exit 3.
    """
    heads = {1: C1, 2: C2}
    verdicts = {1: None, 2: (C2, (True, "documents 1541"))}
    rc, calls = _plan(mod, monkeypatch, heads, verdicts)
    out = capsys.readouterr().out

    assert rc == 3, "a plan that stopped is not exit 0 (verified) nor 1 (a finding)"
    assert rc != 0, "0 promises every step was measured and passed"
    assert rc != 1, "a conflict is not a finding - nothing was judged wrong"
    assert "CONFLICT" in out
    assert "DANGER" not in out
    # The reader must be told how much of the plan went unjudged, not just that
    # something stopped: "plan stopped" alone reads as "the rest was fine".
    assert "0 of 2 step(s) were measured" in out, out
    assert "the remaining 2 were not judged" in out, out
    # Stopped at the conflict: step 2 must NOT have been measured against a
    # phantom tree.
    assert calls == [(BASE, C1)]


def test_a_danger_before_a_conflict_is_still_the_finding(mod, monkeypatch, capsys):
    """Exit 1 dominates exit 3: a measured bad step outranks an unmeasured tail."""
    heads = {1: C1, 2: C2}
    verdicts = {1: (C1, (False, "documents 1541 but 1560 are collected")), 2: None}
    rc, _ = _plan(mod, monkeypatch, heads, verdicts)
    out = capsys.readouterr().out

    assert rc == 1, "the finding must survive a later conflict"
    assert "DANGER" in out
    assert "CONFLICT" in out


def test_a_fully_measured_healthy_plan_is_the_only_zero(mod, monkeypatch, capsys):
    """Both directions of the same predicate, so 0 cannot be reached by stopping.

    A test that only asserted "healthy -> 0" would stay green if a stopped plan
    also returned 0 (which it did) - the code would be unverified in the one
    direction that matters for a gate.
    """
    heads = {1: C1, 2: C2, 3: C3}
    healthy = {
        1: (C1, (True, "documents 1")),
        2: (C2, (True, "documents 2")),
        3: (C3, (True, "documents 3")),
    }
    assert _plan(mod, monkeypatch, heads, healthy, prs=(1, 2, 3))[0] == 0
    capsys.readouterr()

    stopped = {1: (C1, (True, "documents 1")), 2: None, 3: (C3, (True, "documents 3"))}
    rc = _plan(mod, monkeypatch, heads, stopped, prs=(1, 2, 3))[0]
    out = capsys.readouterr().out

    assert rc == 3, "one conflict in the plan and 0 is no longer reachable"
    assert "1 of 3 step(s) were measured" in out, out
    assert "the remaining 2 were not judged" in out, out


def test_an_unmeasurable_step_is_not_a_pass(mod, monkeypatch, capsys):
    """A merge-tree failure is exit 2, never a reassuring 'healthy'."""
    heads = {1: C1}
    monkeypatch.setattr(mod, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod, "_fetch_head", lambda n: C1)

    def boom(a, b):
        raise mod.MeasurementError("merge-tree failed")

    monkeypatch.setattr(mod, "_merge_commit", boom)
    rc = mod.main(["1"])
    err = capsys.readouterr().err

    assert rc == 2
    assert "could not measure" in err


# --- the measurement layer, driven for real ---------------------------------
#
# The tests above replace `_guard_verdict`, so none of them reads the guard's exit
# code. A mutant that never consulted the guard passed all of them, so the real
# path is exercised here: a tiny git tree whose own copy of the guard actually
# runs. Two directions, because the whole point of the function is to tell the
# two apart - a test that only proved "it can return False" would not show that a
# healthy tree comes back True.


def _tree_with(repo: Path, documented: int | None, tests: int) -> str:
    """Build a commit whose guard verdict is decided by whether a count is stored.

    Returns the commit's tree sha, which is what `_guard_verdict` takes.

    `documented=None` writes no count into `Agent.md`; any integer writes one. Since
    #1181 the guard's rule is "no tracked file states the Python test count", so the
    accepted shape is *no stored count* and any stored count is rejected whatever the
    collected figure is - the pre-#1181 fixture (accept iff `documented == tests`) no
    longer describes either direction, which is why both tests below were rewritten
    when this branch was re-applied.
    """
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir()
    # The real guard, byte for byte: `_guard_verdict` runs the tree's *own* copy,
    # and a stand-in would test this fixture instead of the tool.
    (repo / "scripts" / "check-doc-count.py").write_bytes(CHILD_GUARD.read_bytes())
    count = "" if documented is None else f" ({documented})"
    (repo / "Agent.md").write_text(
        f"# Doc\nPython: `uv run pytest tests/ -v`{count}\n", encoding="utf-8"
    )
    (repo / "tests" / "test_x.py").write_text(
        "".join(f"def test_{i}():\n    assert True\n" for i in range(tests)),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    for argv in (["git", "init", "-q", "."], ["git", "add", "-A"],
                 ["git", "commit", "-qm", "i"]):
        subprocess.run(argv, cwd=repo, check=True, env=env, capture_output=True)
    out = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo,
                         check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout.strip()


def test_the_real_guard_verdict_accepts_a_tree_that_stores_no_count(mod, tmp_path, monkeypatch):
    """The OK direction through the real child guard.

    Without this, a `_guard_verdict` that reported every tree as failing would
    pass every orchestration test above (they inject the verdicts), and the tool
    would be useless in the opposite direction from the mutant: it would flag the
    whole queue. Both directions are needed; each is blind to the other's defect.

    The accepted shape is "no tracked file states the count" (#1181): the figure is
    measured where it is needed, so a tree that documents none is healthy at any
    collected count - which is why this tree collects two and states nothing.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    tree = _tree_with(repo, documented=None, tests=2)
    # `_guard_verdict` archives the tree with `git archive`, which resolves
    # objects from the process cwd - the tool runs inside the repo, so the test
    # must too.
    monkeypatch.chdir(repo)

    ok, report = mod._guard_verdict(tree, tmp_path / "extract")

    assert ok is True, report
    assert "no stored count" in report, report


def test_the_real_guard_verdict_rejects_a_tree_that_stores_the_count(mod, tmp_path, monkeypatch):
    """The DANGER direction through the real child guard.

    The tree states a count and the repo's rule is that no tracked file may - the
    shape that rides into master on a merge git reported clean (a stored count is
    stale the moment another PR adds a test). Fails open if the guard's exit code is
    read wrongly, which is what the surviving mutant did.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    tree = _tree_with(repo, documented=2, tests=1)
    monkeypatch.chdir(repo)

    ok, report = mod._guard_verdict(tree, tmp_path / "extract")

    assert ok is False
    assert "state the test count" in report, report


def test_a_tree_without_the_guard_is_a_measurement_error(mod, tmp_path, monkeypatch):
    """No guard in the tree means "could not measure", never a pass.

    The tool judges the merged tree's own copy of the guard, so a tree that
    cannot be judged must surface as exit 2. A missing guard reported as healthy
    would be the most dangerous reading available: it would cover exactly the
    trees whose health is unknown.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    tree = _tree_with(repo, documented=1, tests=1)
    (repo / "scripts" / "check-doc-count.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "drop the guard"], cwd=repo, check=True,
                   capture_output=True)
    out = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo,
                         check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tree = out.stdout.strip()
    monkeypatch.chdir(repo)

    with pytest.raises(mod.MeasurementError) as excinfo:
        mod._guard_verdict(tree, tmp_path / "extract")

    assert "not present" in str(excinfo.value)


# --- the base is refreshed, not taken on faith ------------------------------
#
# Every PR head is fetched from the network, so the tool always answers about the
# PRs as they are *now*. The base was not, which made the two halves of one
# question come from different points in time. Measured on the real repo: with
# `origin/master` left two commits behind, the tool printed
#
#     base 02e43c82 (origin/master)      <- 02e43c8 is not master; 3dbc2f1 is
#
# and over 25 plans, 16 changed verdict between a stale and a fresh base - the
# plan below reads as 2 DANGER steps against the stale base and as a conflict
# (safe: no tree, no verdict) against the live one. A stale base is not merely
# conservative: it makes the tool answer about a tree nobody asked about.


def test_a_remote_tracking_base_is_refreshed_before_use(mod, monkeypatch):
    """`origin/<branch>` is fetched, so a stale ref cannot be used as the base."""
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(mod, "_run", lambda argv, cwd=None: (calls.append(argv), _Done())[1])

    mod._refresh_base("origin/master")

    assert len(calls) == 1, calls
    assert calls[0][0] == "git" and "fetch" in calls[0]
    joined = " ".join(calls[0])
    # Fully qualified destination: a bare `origin/master` makes git create a local
    # branch of that name, shadowing the remote-tracking ref (measured).
    assert "refs/heads/master:refs/remotes/origin/master" in joined, calls[0]
    assert "+" in joined, "the refspec must be forced, as for PR heads"


def test_a_sha_or_local_ref_base_is_never_fetched(mod, monkeypatch):
    """Only a remote-tracking name is refreshed; a SHA and a local branch are literal.

    Fetching on a SHA would be meaningless (it is immutable), and treating a local
    branch as remote would overwrite the caller's own ref with a same-named remote
    one. Both are silent ways to measure a tree the caller did not name.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(
        mod, "_run", lambda argv, cwd=None: (calls.append(argv), None)[1]
    )

    for ref in ("0" * 40, "localbase", "refs/heads/x", "FETCH_HEAD"):
        mod._refresh_base(ref)

    assert calls == [], calls


def test_main_refreshes_the_base_before_measuring(mod, monkeypatch, capsys):
    """`main` must actually call `_refresh_base` - a correct helper nobody calls is dead.

    Pinned separately because the three tests above exercise the helper directly:
    deleting the call from `main` leaves them all green while the defect this file
    documents (a stale base reported as `origin/master`) returns in full. The
    measured mutant that did exactly that survived all three.
    """
    seen: list[str] = []
    monkeypatch.setattr(mod, "_refresh_base", lambda ref: seen.append(ref))

    class _Done:
        returncode = 0
        stdout = "0" * 40
        stderr = ""

    monkeypatch.setattr(mod, "_run", lambda argv, cwd=None: _Done())
    monkeypatch.setattr(mod, "_open_pr_numbers", lambda repo: [1])
    monkeypatch.setattr(mod, "_fetch_head", lambda n: "1" * 40)
    monkeypatch.setattr(mod, "_merge_commit", lambda a, b: None)  # conflict: stops early

    rc = mod.main(["1"])

    assert seen == ["origin/master"], seen
    # 3, not 0: the single step conflicts, so no tree was judged and "every step was
    # measured and healthy" (0) would be a claim about nothing (#1174). The refresh
    # assertion above is this test's subject; the exit code is pinned so a future
    # merge cannot quietly restore the old "a conflict is success" reading.
    assert rc == 3, rc


def test_a_base_that_cannot_be_refreshed_is_a_measurement_error(mod, monkeypatch):
    """A failed fetch is exit 2, never a quiet fall-back to the stale commit.

    This is the fail-open shape the sibling tests pin for the guard: continuing
    against a base that could not be verified is exactly how the tool would answer
    about the wrong tree while reporting a number.
    """

    class _Fail:
        returncode = 128
        stdout = ""
        stderr = "fatal: couldn't find remote ref"

    monkeypatch.setattr(mod, "_run", lambda argv, cwd=None: _Fail())

    with pytest.raises(mod.MeasurementError) as excinfo:
        mod._refresh_base("origin/master")

    assert "could not refresh" in str(excinfo.value)


# --- the base is not just fresh, it is the ref you named ---------------------
#
# Refreshing the base fixed *when* it is read; the next defect was *what* the
# name points at. `origin/master` is ambiguous - git's precedence list consults
# `refs/heads/<name>` before `refs/remotes/<name>` - and git creates exactly such
# a local branch when a fetch destination is left unqualified, which is the trap
# `_refresh_base`'s own docstring records. Measured on the real repo
# (`cyc20260913-072845`) with a stray `refs/heads/origin/master` at `02e43c8`
# while the remote-tracking ref was `245125e`:
#
#     base 02e43c82 (origin/master)      <- master was 245125e; the tool measured
#                                           a two-cycle-old tree and said "master"
#
# The age of the commit is not the defect - the *identity* of the ref is. These
# tests therefore use real repositories with real refs: a stubbed `_run` cannot
# show which ref git would have picked, because the defect lives in git's own
# resolution rules, and that is the thing under test.


def _repo_with_two_refs(repo: Path, shadow: bool) -> tuple[str, str]:
    """A real repo where `origin/master` is genuinely ambiguous-or-not.

    Returns `(old, new)`: the commit a shadowing local branch is left at, and the
    commit the remote-tracking ref is left at.
    """
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*argv: str) -> str:
        out = subprocess.run(["git", *argv], cwd=repo, check=True, env=env,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        return out.stdout.strip()

    git("init", "-q", ".")
    (repo / "f").write_text("old\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "old")
    old = git("rev-parse", "HEAD")
    (repo / "f").write_text("new\n", encoding="utf-8")
    git("commit", "-qam", "new")
    new = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/master", new)
    if shadow:
        # The stray branch, exactly as git leaves it when a fetch destination is
        # written unqualified - behind the remote-tracking ref, so the two
        # answers are distinguishable.
        git("update-ref", "refs/heads/origin/master", old)
    return old, new


def test_a_shadowing_local_branch_does_not_win(mod, tmp_path, monkeypatch, capsys):
    """The commit returned is the remote-tracking one, not the shadowing branch.

    Both refs exist, so a bare `git rev-parse origin/master` answers from the
    local branch: the tool would report a base that is not master while printing
    `origin/master`. Fails if the short name is resolved by git's precedence
    instead of by full name.
    """
    repo = tmp_path / "repo"
    old, new = _repo_with_two_refs(repo, shadow=True)
    monkeypatch.chdir(repo)

    assert mod._rev_parse("origin/master") == new, f"shadow won (old={old[:8]})"
    # ... and the user is told about the stray ref, because it misleads every
    # other short-name reader (`git checkout origin/master` included).
    assert "ambiguous" in capsys.readouterr().err


def test_without_the_shadow_the_same_name_resolves_identically(mod, tmp_path, monkeypatch, capsys):
    """The negative state: no stray branch, same answer, and no warning.

    The pair is what makes the first test mean something - a warning printed
    unconditionally would be noise, and an assertion that only ever sees the
    ambiguous case could not tell "handled" from "always warns".
    """
    repo = tmp_path / "repo"
    _old, new = _repo_with_two_refs(repo, shadow=False)
    monkeypatch.chdir(repo)

    assert mod._rev_parse("origin/master") == new
    assert capsys.readouterr().err == ""


def test_a_name_that_denotes_only_a_local_branch_is_refused(mod, tmp_path, monkeypatch):
    """No remote-tracking ref at all: exit 2, never that branch's commit.

    Here nothing is ambiguous about git's answer - it is unambiguously the wrong
    ref. A local branch that happens to be called `origin/master` is not remote
    master, so the measurement is refused rather than labelled.
    """
    repo = tmp_path / "repo"
    old, _new = _repo_with_two_refs(repo, shadow=True)
    subprocess.run(["git", "update-ref", "-d", "refs/remotes/origin/master"],
                   cwd=repo, check=True, capture_output=True)
    monkeypatch.chdir(repo)

    with pytest.raises(mod.MeasurementError) as excinfo:
        mod._rev_parse("origin/master")

    message = str(excinfo.value)
    assert "ambiguous" in message and "refs/heads/origin/master" in message, message
    assert old[:8] not in message, "the refusal must not look like a resolution"


def test_a_sha_or_qualified_ref_is_passed_through(mod, monkeypatch):
    """Only short remote-tracking names are rewritten.

    A SHA has no name to disambiguate, and a fully-qualified ref already selects
    exactly one ref. Both must reach `rev-parse` unchanged - rewriting either
    would invent a ref name that the caller never wrote.
    """
    seen: list[list[str]] = []
    monkeypatch.setattr(
        mod, "_run", lambda argv, cwd=None: (seen.append(argv), None)[1]
    )

    for ref in ("0" * 40, "refs/remotes/origin/master", "FETCH_HEAD", "localbase"):
        assert mod._qualify_ref(ref) == ref, ref

    assert seen == [], "a name that cannot be ambiguous costs no git call"



def test_an_empty_open_pr_list_is_refused_at_its_source(mod, monkeypatch, capsys):
    """The reachable form of "zero measured steps is never a verdict".

    The tool cannot print "all 0 step(s) ... pass the guards" because the default
    plan source refuses an empty list before any step count exists: `numbers =
    args.prs or _open_pr_numbers(...)` with `prs` declared `nargs="*"` means either
    positional numbers were given, or the source raised.

    Measured 2026-09-13 (`cyc20260913-114142`) with the real script and an empty
    open-PR list (a stub `gh` on PATH printing nothing, exiting 0) - on master
    `2017d8f` and on this branch alike:

        could not measure: no open PRs reported - nothing to check
        --- exit code: 2 ---

    The refusal is as old as the tool (it is in `3dbc2f1`, and in `6456a98`), so
    this pins existing behaviour rather than adding a guard. An earlier revision of
    this branch tested the vacuous-pass shape instead, by replacing `_open_pr_numbers`
    with a lambda returning `[]` - that substitution *removes* the refusal, which is
    why it produced a state the program cannot enter (a measurement of the stub).
    """
    monkeypatch.setattr(mod, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(
        mod, "_run", lambda argv, cwd=None: _FakeProc(stdout="", returncode=0)
    )

    # The source itself refuses - this is what makes an empty plan unreachable.
    with pytest.raises(mod.MeasurementError) as excinfo:
        mod._open_pr_numbers("argszero/emrg")
    assert "no open PRs reported" in str(excinfo.value)

    rc = mod.main([])
    captured = capsys.readouterr()

    assert rc == 2, "an unanswerable question must not be reported as health"
    assert "no open PRs reported" in captured.err, captured.err
    # The false claim itself must be gone, not merely accompanied by a warning.
    assert "pass the guards" not in captured.out, captured.out


def test_the_default_plan_source_still_measures_a_real_plan(mod, monkeypatch, capsys):
    """The other direction: the refusal above must not fire on a real plan.

    Without this, "return 2 whenever no positional arguments were given" would pass
    the test above while breaking the tool's documented default ("all open, ascending").
    """
    monkeypatch.setattr(mod, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod, "_open_pr_numbers", lambda repo: [1])
    monkeypatch.setattr(mod, "_fetch_head", lambda n: C1)
    monkeypatch.setattr(mod, "_merge_commit", lambda a, b: C1)
    monkeypatch.setattr(mod, "_guard_verdict", lambda tree, workdir: (True, "documents 1"))
    monkeypatch.setattr(
        mod,
        "_run",
        lambda argv, cwd=None: _FakeProc(argv[-1].removesuffix("^{tree}")),
    )
    rc = mod.main([])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "all 1 step(s) landed trees that pass the guards" in out, out


# --- the default plan: what it plans, and what it names as left out ----------


def _open_prs_with_one_conflict(mod, monkeypatch):
    """Three open PRs; #2 conflicts with the base, #1 and #3 merge cleanly."""
    heads = {1: C1, 2: C2, 3: C3}
    monkeypatch.setattr(mod, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod, "_open_pr_numbers", lambda repo: [1, 2, 3])
    monkeypatch.setattr(mod, "_fetch_head", lambda n: heads[n])

    def fake_merge(a, b):
        return None if b == C2 else b + "-merged"

    monkeypatch.setattr(mod, "_merge_commit", fake_merge)
    monkeypatch.setattr(mod, "_guard_verdict", lambda tree, workdir: (True, "documents 1"))
    monkeypatch.setattr(
        mod,
        "_run",
        lambda argv, cwd=None: _FakeProc(argv[-1].removesuffix("^{tree}")),
    )


def test_the_default_plan_leaves_out_prs_that_conflict_and_names_them(
    mod, monkeypatch, capsys
):
    """A default plan that stops at step 1 answers nothing, so it plans what merges.

    Measured 2026-09-13 (`cyc20260913-120524`): 13 of 14 open PRs conflicted with
    the base, and the literal default plan - every open PR, ascending - reported
    `plan stopped at a conflict, 0 of 13 steps measured`. It could not reach the
    second half of any danger pair, which is the only thing this tool is for.

    Both directions are pinned here: the conflicting PR is *absent from the plan*
    (so its step is never measured), and it is *named in the disclosure* (so
    omitting it is not silent).
    """
    _open_prs_with_one_conflict(mod, monkeypatch)
    rc = mod.main([])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "plan: #1 -> #3" in out, out
    assert "excluded as conflicting: #2" in out, out
    assert "#2: OK" not in out and "#2: DANGER" not in out, out
    assert "all 2 step(s) landed trees that pass the guards" in out, out


def test_all_plans_every_open_pr_conflicting_ones_included(mod, monkeypatch, capsys):
    """`--all` is the literal old default, and it still stops at the conflict.

    Without this, "filter the plan" could quietly become "never report a conflict",
    which is the opposite of the tool's job: a conflicting step is a real answer
    about the plan (exit 3), not something to drop.
    """
    _open_prs_with_one_conflict(mod, monkeypatch)
    rc = mod.main(["--all"])
    out = capsys.readouterr().out

    assert rc == 3, out
    assert "plan: #1 -> #2 -> #3" in out, out
    assert "#2: CONFLICT" in out, out
    assert "plan source: every open PR (--all)" in out, out


def test_a_queue_where_nothing_merges_is_not_a_pass(mod, monkeypatch, capsys):
    """No mergeable PR means the question was not answered - exit 2, never a pass.

    This is the state the live queue was in on 2026-09-13 (13 of 14 open PRs
    conflicting). Reporting `all 0 step(s) landed trees that pass the guards` here
    would be the vacuous pass this file already refuses for an empty open-PR list,
    one step further in: the list is not empty, the plan is.
    """
    monkeypatch.setattr(mod, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod, "_open_pr_numbers", lambda repo: [1, 2])
    monkeypatch.setattr(mod, "_fetch_head", lambda n: C1 if n == 1 else C2)
    monkeypatch.setattr(mod, "_merge_commit", lambda a, b: None)
    monkeypatch.setattr(
        mod,
        "_run",
        lambda argv, cwd=None: _FakeProc(argv[-1].removesuffix("^{tree}")),
    )
    rc = mod.main([])
    captured = capsys.readouterr()

    assert rc == 2, captured.out
    assert "conflict with" in captured.err, captured.err
    assert "pass the guards" not in captured.out, captured.out


def test_the_default_plan_is_built_against_the_tree_the_steps_build(
    mod, monkeypatch, capsys
):
    """A pairwise-clean candidate set is not a sequence, and the filter must know it.

    The defect this pins (measured 2026-09-13, `cyc20260913-144807`, on the live
    queue): the default filter kept every open PR that merges cleanly onto `base`,
    while the loop merges each step onto *the tree the previous step produced*. On
    that day's queue 11 of 13 PRs were pairwise-clean against master, yet the plan
    still stopped at step 4 (`#1152: CONFLICT`, `3 of 11 step(s) were measured`,
    exit 3) - the default invocation answering nothing, one indirection further in
    than the case the filter was written for.

    Here #2 merges cleanly onto the base and conflicts with the tree #1 builds, so
    the two behaviours differ in a way the assertions can see: the base-only filter
    plans `#1 -> #2 -> #3` and stops, while the cumulative one plans `#1 -> #3` and
    measures every step it planned.
    """
    heads = {1: C1, 2: C2, 3: C3}
    monkeypatch.setattr(mod, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod, "_open_pr_numbers", lambda repo: [1, 2, 3])
    monkeypatch.setattr(mod, "_fetch_head", lambda n: heads[n])

    def fake_merge(a, b):
        # #2 is clean against the base and conflicts with the tree #1 produces.
        return None if (b == C2 and a != BASE) else b + "-merged"

    monkeypatch.setattr(mod, "_merge_commit", fake_merge)
    monkeypatch.setattr(mod, "_guard_verdict", lambda tree, workdir: (True, "documents 1"))
    monkeypatch.setattr(
        mod,
        "_run",
        lambda argv, cwd=None: _FakeProc(argv[-1].removesuffix("^{tree}")),
    )
    rc = mod.main([])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "plan: #1 -> #3" in out, out
    assert "plan source: open PRs that can be merged in this order (2 of 3)" in out, out
    assert "excluded as conflicting: #2" in out, out
    assert "all 2 step(s) landed trees that pass the guards" in out, out
    # The step the filter could not see must not be reported as measured, and the
    # plan must not walk into it: "#2: CONFLICT" here would be the old behaviour.
    assert "#2: OK" not in out and "#2: CONFLICT" not in out, out


def test_a_candidate_that_conflicts_only_with_the_accumulated_tree_is_still_named(
    mod, monkeypatch, capsys
):
    """Skipping must be disclosed, and must not become a silent drop.

    The cumulative filter makes exclusions *more* likely than the base-only one (it
    now also excludes PRs that only conflict with their predecessors), so the
    disclosure is what keeps the plan from hiding an omission. `--all` is the
    escape hatch and must still show the step.
    """
    heads = {1: C1, 2: C2}
    monkeypatch.setattr(mod, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod, "_open_pr_numbers", lambda repo: [1, 2])
    monkeypatch.setattr(mod, "_fetch_head", lambda n: heads[n])
    monkeypatch.setattr(
        mod, "_merge_commit", lambda a, b: None if a != BASE else b + "-merged"
    )
    monkeypatch.setattr(mod, "_guard_verdict", lambda tree, workdir: (True, "documents 1"))
    monkeypatch.setattr(
        mod,
        "_run",
        lambda argv, cwd=None: _FakeProc(argv[-1].removesuffix("^{tree}")),
    )

    rc = mod.main([])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "plan: #1" in out, out
    assert "excluded as conflicting: #2" in out, out

    # The same queue under --all is planned literally, and stops at the step that
    # cannot be taken - which is how the reader sees it at all.
    rc_all = mod.main(["--all"])
    out_all = capsys.readouterr().out

    assert rc_all == 3, out_all
    assert "plan: #1 -> #2" in out_all, out_all
    assert "#2: CONFLICT" in out_all, out_all
def _queue_where_nothing_merges(mod, monkeypatch, conflict_line: str) -> None:
    """Two open PRs, neither mergeable, conflicting in whatever `conflict_line` says.

    The merge-tree output is faked at the `_run` layer rather than by replacing
    `_conflict_paths`, so the path parsing under test actually runs - a stub of the
    function being tested would pass whatever it was told to.
    """
    monkeypatch.setattr(mod, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod, "_open_pr_numbers", lambda repo: [1, 2])
    monkeypatch.setattr(mod, "_fetch_head", lambda n: C1 if n == 1 else C2)

    def fake_run(argv, cwd=None):
        if argv[:3] == ["git", "merge-tree", "--write-tree"]:
            return _FakeProc(conflict_line, returncode=1)
        return _FakeProc(argv[-1].removesuffix("^{tree}"))

    monkeypatch.setattr(mod, "_run", fake_run)


def test_an_empty_plan_names_the_conflicting_paths_and_the_way_out(
    mod, monkeypatch, capsys
):
    """The refusal must describe the state it measured and offer a remedy that fixes it.

    Measured 2026-09-13 (`cyc20260913-125509`): with 13 open PRs, all 13 conflicted
    and every one of them in `Agent.md` (10 in that file alone). The refusal used to
    offer "pass PR numbers explicitly, or use --all", and neither resolves that
    state - an explicitly named conflicting PR still conflicts. So the summary is
    computed from the merge output, and the remedy is printed for the path that has
    one: the repo re-measures the derived count rather than choosing a side.

    The remedy also had to stop *assuming* that path carries a count (issue #1184):
    the sentence is printed for a base measured to state one, and the measurement
    itself is pinned separately below.
    """
    _queue_where_nothing_merges(
        mod, monkeypatch, "CONFLICT (content): Merge conflict in Agent.md\n"
    )
    monkeypatch.setattr(mod, "_base_states_a_count", lambda base: True)
    rc = mod.main([])
    err = capsys.readouterr().err

    assert rc == 2, err
    assert "Conflicting paths over those 2 PR(s): Agent.md x2." in err, err
    assert "--resolve-conflict" in err, err
    assert "push" in err, err
    assert "pass PR numbers explicitly" not in err, "the old, non-resolving remedy"


def test_a_base_that_states_no_count_gets_no_count_line_remedy(
    mod, monkeypatch, capsys
):
    """Issue #1184: an `Agent.md` conflict is not evidence of a count line.

    Since #1181 the count is measured, not stored, so today's `Agent.md` conflicts
    are between the documentation lines the PRs add - and `--resolve-conflict`
    clears a count-line-only difference, so on this state it is advice that cannot
    run. Measured on master `c9a7d8a` while re-applying six PRs: the refusal told
    the reader `Agent.md` carries the count, on a tree whose own guard said no
    tracked file states it.
    """
    _queue_where_nothing_merges(
        mod, monkeypatch, "CONFLICT (content): Merge conflict in Agent.md\n"
    )
    monkeypatch.setattr(mod, "_base_states_a_count", lambda base: False)
    rc = mod.main([])
    err = capsys.readouterr().err

    assert rc == 2, err
    assert "Conflicting paths over those 2 PR(s): Agent.md x2." in err, err
    assert "states no derived Python test count" in err, err
    assert "read the two sides" in err, "the reader is left with the sides"
    assert "the way out is to merge the base in" not in err, "the inapplicable remedy"
    assert "is measured, not stored" in err, err


def test_an_unmeasurable_base_says_so_rather_than_guessing(mod, monkeypatch, capsys):
    """The third state: nothing was read, so nothing is claimed either way.

    Collapsing this into "no count" would print a sentence about a tree the guard
    did not manage to describe - the same defect as the one this clause was
    written for, one step further along. The paths *were* measured, so they stay.
    """
    _queue_where_nothing_merges(
        mod, monkeypatch, "CONFLICT (content): Merge conflict in Agent.md\n"
    )
    monkeypatch.setattr(mod, "_base_states_a_count", lambda base: None)
    rc = mod.main([])
    err = capsys.readouterr().err

    assert rc == 2, err
    assert "Conflicting paths over those 2 PR(s): Agent.md x2." in err, err
    assert "could not be measured" in err, err
    assert "no remedy is offered" in err, err
    assert "--resolve-conflict" not in err, err
    assert "states no derived Python test count" not in err, err


def test_a_conflict_elsewhere_gets_no_count_line_advice(mod, monkeypatch, capsys):
    """The remedy is printed only for the path it applies to.

    This is the arm that keeps the previous test from passing on a hardcoded
    sentence: if the count-line remedy were printed for every conflict, advice for
    a conflict in some unrelated file would name a command that cannot fix it -
    the same defect as a hint that cannot run, one tool further along.
    """
    _queue_where_nothing_merges(
        mod, monkeypatch, "CONFLICT (content): Merge conflict in emrg/tools/bash_tool.py\n"
    )
    rc = mod.main([])
    err = capsys.readouterr().err

    assert rc == 2, err
    assert "emrg/tools/bash_tool.py x2." in err, err
    assert "--resolve-conflict" not in err, err


GUARD_SOURCE = REPO_ROOT / "scripts" / "check-doc-count.py"


def _git(mod, root: Path, *args: str) -> None:
    """Run git through the tool's own runner, so the pinning is not re-decided here.

    `mod._run` is the same subprocess call the tool uses for every git command it
    runs (capture, text, encoding pinned), so the fixture adds no second decoding
    policy for the class guard to find.
    """
    proc = mod._run(["git", *args], cwd=str(root))
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"


def _tiny_checkout(mod, root: Path, agent_md: str, tracked: list[str]) -> None:
    """A one-commit repo holding the files the guard resolves its root by.

    The guard's own copy is written from this repo and then `tracked` decides what
    the commit contains: `git archive` only sees committed files, so a guard left
    untracked on disk is exactly the shape where the *checkout* has one and the
    extracted tree does not.
    """
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "check-doc-count.py").write_text(
        GUARD_SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "Agent.md").write_text(agent_md, encoding="utf-8")
    _git(mod, root, "init", "-q")
    for path in tracked:
        _git(mod, root, "add", path)
    _git(
        mod, root, "-c", "user.email=fixture@example.com", "-c", "user.name=fixture",
        "commit", "-q", "-m", "fixture",
    )


COUNT_LINE = "Python: `uv run pytest tests/ -v` (1599) - import check: x\n"
NO_COUNT_LINE = "Python: `uv run pytest tests/ -v` - count is measured, not stored\n"
BOTH = ["Agent.md", "scripts/check-doc-count.py"]


def test_the_base_question_is_measured_on_real_trees(mod, monkeypatch, tmp_path):
    """Both states, asked of real trees with the real guard - no stub of the answer.

    A stub would prove only that the sentence follows the stub. This extracts the
    tree, runs the checkout's guard against it and reads the guard's report, which
    is the whole claim: the sentence now rests on a measurement of the base.
    """
    stores = tmp_path / "stores"
    stores.mkdir()
    _tiny_checkout(mod, stores, COUNT_LINE, BOTH)
    monkeypatch.chdir(stores)
    assert mod._base_states_a_count("HEAD") is True

    clean = tmp_path / "clean"
    clean.mkdir()
    _tiny_checkout(mod, clean, NO_COUNT_LINE, BOTH)
    monkeypatch.chdir(clean)
    assert mod._base_states_a_count("HEAD") is False


def test_the_base_question_refuses_a_tree_the_guard_cannot_name(mod, monkeypatch, tmp_path):
    """A third answer for "the guard read some other tree".

    Measured 2026-09-13: run with a working directory that has no `scripts/`, the
    guard falls back to its own checkout and reports on *that* tree - `tree:
    <this repo>` - in the same words it would use about the tree that was asked
    about. Accepting such a report is the wrong-tree failure this repo keeps
    producing (a consistent-looking answer about a checkout nobody named), so the
    reported `tree:` must be the extracted one, and it is not guessed when the
    guard names a different one.
    """
    root = tmp_path / "guard-not-in-tree"
    root.mkdir()
    _tiny_checkout(mod, root, NO_COUNT_LINE, ["Agent.md"])  # guard exists, untracked
    monkeypatch.chdir(root)
    assert mod._base_states_a_count("HEAD") is None


UNKNOWN_SHAPE_GUARD = (
    "import os, sys\n"
    "print('tree:', os.getcwd())\n"
    "print('a report this tool has never seen')\n"
    "sys.exit(1)\n"
)


def test_the_base_question_does_not_read_an_unknown_report_as_no_count(
    mod, monkeypatch, tmp_path
):
    """The other way to reach the third answer: a report in neither shape.

    The guard names the right tree here, so the naming check passes and this is
    only about the report. Reading an unrecognised report as "no count" would
    print a *claim* about the base ("states no derived Python test count") off the
    back of a report nobody could read - the same defect as the one this clause
    exists to remove, so it is answered as unmeasured instead.
    """
    root = tmp_path / "other-shape"
    root.mkdir()
    _tiny_checkout(mod, root, NO_COUNT_LINE, BOTH)
    (root / "scripts" / "check-doc-count.py").write_text(
        UNKNOWN_SHAPE_GUARD, encoding="utf-8"
    )
    monkeypatch.chdir(root)
    assert mod._base_states_a_count("HEAD") is None
