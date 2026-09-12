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
* the conflict case: no tree, no verdict, exit 0 - a conflict is not a finding,
  and reporting it as a failure would make the tool unusable on this queue.

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
    """No tree means no verdict.

    On this queue most PRs conflict on the count line, so a tool that failed on
    conflicts would be red by default and read as noise. The step is reported and
    the plan stops - the remaining steps cannot be measured against a tree that
    does not exist.
    """
    heads = {1: C1, 2: C2}
    verdicts = {1: None, 2: (C2, (True, "documents 1541"))}
    rc, calls = _plan(mod, monkeypatch, heads, verdicts)
    out = capsys.readouterr().out

    assert rc == 0
    assert "CONFLICT" in out
    assert "DANGER" not in out
    # Stopped at the conflict: step 2 must NOT have been measured against a
    # phantom tree.
    assert calls == [(BASE, C1)]


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


def _tree_with(repo: Path, documented: int, tests: int) -> str:
    """Build a commit whose guard is satisfied iff `documented == tests`.

    Returns the commit's tree sha, which is what `_guard_verdict` takes.
    """
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir()
    # The real guard, byte for byte: `_guard_verdict` runs the tree's *own* copy,
    # and a stand-in would test this fixture instead of the tool.
    (repo / "scripts" / "check-doc-count.py").write_bytes(CHILD_GUARD.read_bytes())
    (repo / "Agent.md").write_text(
        f"# Doc\nPython: `uv run pytest tests/ -v` ({documented})\n", encoding="utf-8"
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
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def test_the_real_guard_verdict_accepts_a_self_consistent_tree(mod, tmp_path, monkeypatch):
    """The OK direction through the real child guard.

    Without this, a `_guard_verdict` that reported every tree as failing would
    pass every orchestration test above (they inject the verdicts), and the tool
    would be useless in the opposite direction from the mutant: it would flag the
    whole queue. Both directions are needed; each is blind to the other's defect.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    tree = _tree_with(repo, documented=2, tests=2)
    # `_guard_verdict` archives the tree with `git archive`, which resolves
    # objects from the process cwd - the tool runs inside the repo, so the test
    # must too.
    monkeypatch.chdir(repo)

    ok, report = mod._guard_verdict(tree, tmp_path / "extract")

    assert ok is True, report
    assert "documents 2" in report, report


def test_the_real_guard_verdict_rejects_a_stale_count(mod, tmp_path, monkeypatch):
    """The DANGER direction through the real child guard.

    The tree documents two tests and collects one - the drift shape the tool
    exists to catch on a merge that git reported clean. Fails open if the guard's
    exit code is read wrongly, which is what the surviving mutant did.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    tree = _tree_with(repo, documented=2, tests=1)
    monkeypatch.chdir(repo)

    ok, report = mod._guard_verdict(tree, tmp_path / "extract")

    assert ok is False
    assert "documents 2" in report and "1 are collected" in report, report


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
                         check=True, capture_output=True, text=True)
    tree = out.stdout.strip()
    monkeypatch.chdir(repo)

    with pytest.raises(mod.MeasurementError) as excinfo:
        mod._guard_verdict(tree, tmp_path / "extract")

    assert "not present" in str(excinfo.value)

