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

The measurement itself is faked (no git, no pipeline runs in CI): `_merge_commit`
and `_guard_verdict` are replaced, and the replacements are asserted to have been
called with the right commits - a test that passes because the code under test
was never invoked proves nothing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-merge-sequence.py"

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
