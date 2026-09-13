"""Tests for scripts/check-merge-pairs.py - is any *pair* silently dangerous?

Background (cycle cyc20260913-091152)
-------------------------------------
Measured on the six PRs that were `MERGEABLE`/`CLEAN` on 2026-09-13, all 15 pairs in both
orders: **14 ordered pairs conflict** (git blocks them - the safe outcome) and **one pair
merges silently into a broken tree**:

    1173 -> 1174:  DANGER - clean merge, but the tree FAILS: documents 1564 but 1566 are collected

Both PRs wrote `1564` (each was re-measured against the same master), so git keeps one copy
of the line with no conflict, while the merged tree collects `1566`. Both PRs were
`MERGEABLE`/`CLEAN`, both double-green, and each was individually safe. Only the pair is not,
and only a measurement of the pair shows it - which is what this tool automates.

What is pinned here, in both directions (#455 - never infer from one side):
* the finding: a pair whose clean merge lands a failing tree is reported and exits 1;
* the non-finding: a pair whose clean merge lands a healthy tree exits 0 with no warning;
* a pair blocked by a conflict is *answered* (exit 0, counted as blocked), not a failure;
* both orders are measured as distinct pairs, and exactly one order can be the dangerous one;
* an unmeasurable pair is exit 2, never a reassuring "no dangerous pair".

The measurement is faked (no git, no pipeline runs in CI): the sibling tool's
`_merge_commit` / `_guard_verdict` / `_fetch_head` are replaced, and the replacements are
asserted to have been called with the right commits - a test that passes because the code
under test was never invoked proves nothing. The first-step merge is asserted to be reused
across pairs, so the quadratic cost stays m merges + m * (m - 1) guard runs.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-merge-pairs.py"

BASE = "a" * 40
C1 = "b" * 40
C2 = "c" * 40
C3 = "e" * 40


def _load_module():
    spec = importlib.util.spec_from_file_location("check_merge_pairs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


class _FakeProc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _scan(mod, monkeypatch, chain, verdicts, prs=(1, 2), base=BASE):
    """Drive main() with faked head resolution, merges and guard verdicts.

    `chain` maps (onto, head) -> the commit the merge produces, or None for a conflict.
    `verdicts` maps the final commit -> (passed, report).
    """
    calls: list[tuple[str, str]] = []
    heads = {n: c for n, c in zip((1, 2, 3), (C1, C2, C3))}

    monkeypatch.setattr(mod.seq, "_rev_parse", lambda ref: base)
    monkeypatch.setattr(mod.seq, "_fetch_head", lambda n: heads[n])

    def fake_merge(a, b):
        calls.append((a, b))
        return chain.get((a, b))

    monkeypatch.setattr(mod.seq, "_merge_commit", fake_merge)
    monkeypatch.setattr(mod.seq, "_guard_verdict", lambda tree, workdir: verdicts[tree])
    # `git rev-parse <commit>^{tree}` - echo the commit back as its own tree.
    monkeypatch.setattr(
        mod.seq,
        "_run",
        lambda argv, cwd=None: _FakeProc(argv[-1].removesuffix("^{tree}")),
    )
    rc = mod.main([str(n) for n in prs])
    return rc, calls


# --- the finding -----------------------------------------------------------


def test_a_pair_that_merges_clean_into_a_failing_tree_is_reported(mod, monkeypatch, capsys):
    """The whole reason the tool exists: exit 1, the pair named, the numbers quoted."""
    chain = {(BASE, C1): C1, (C1, C2): C2}
    verdicts = {C2: (False, "documents 1564 but 1566 are collected")}
    rc, calls = _scan(mod, monkeypatch, chain, verdicts)
    out = capsys.readouterr().out

    assert rc == 1, "a silently dangerous pair must not exit 0"
    assert "DANGER" in out
    assert "#1 -> #2" in out, "the dangerous *pair* must be named, in order"
    assert "1566" in out, "the report must name the actual numbers"
    # The chain is a chain: B is merged onto master+A, not onto master.
    assert (C1, C2) in calls


def test_both_orders_are_measured_and_only_one_can_be_dangerous(mod, monkeypatch, capsys):
    """`A -> B` and `B -> A` are different questions; the output must say which failed."""
    chain = {(BASE, C1): C1, (BASE, C2): C2, (C1, C2): C2, (C2, C1): None}
    verdicts = {C2: (False, "documents 1564 but 1566 are collected")}
    rc, calls = _scan(mod, monkeypatch, chain, verdicts)
    out = capsys.readouterr().out

    assert rc == 1
    assert (BASE, C1) in calls and (BASE, C2) in calls, "both first steps were measured"
    assert (C1, C2) in calls and (C2, C1) in calls, "both orders were measured"
    # Keyed on the per-pair line, not on the word "DANGER": the summary block repeats the
    # pair, so counting occurrences would pass even if only the wrong order were named.
    assert "  #1 -> #2: DANGER" in out, out
    assert "  #2 -> #1: DANGER" not in out, out
    assert "DANGEROUS PAIRS: #1 -> #2\n" in out, out


# --- must not cry wolf -----------------------------------------------------


def test_a_clean_and_healthy_pair_is_not_reported(mod, monkeypatch, capsys):
    """The OK direction: healthy pairs exit 0 silently.

    Without this, a tool that reported every pair as dangerous would pass the test above,
    and it would be useless in the opposite direction from the mutant it guards against.
    """
    chain = {(BASE, C1): C1, (BASE, C2): C2, (C1, C2): C2, (C2, C1): C1}
    verdicts = {C1: (True, "documents 1564"), C2: (True, "documents 1564")}
    rc, _ = _scan(mod, monkeypatch, chain, verdicts)
    out = capsys.readouterr().out

    assert rc == 0
    assert "DANGER" not in out
    assert "no ordered pair merges cleanly into a failing tree" in out


def test_a_conflicting_pair_is_answered_not_a_finding(mod, monkeypatch, capsys):
    """A pair git blocks cannot land, so it cannot land badly - and it is not a failure.

    This is why a stopped scan is exit 0 here while a stopped *plan* is 3 in
    `check-merge-sequence.py`: every pair was answered, whereas a plan stopped at a
    conflict leaves the steps behind it unmeasured.
    """
    chain = {(BASE, C1): C1, (BASE, C2): C2, (C1, C2): None, (C2, C1): None}
    verdicts = {}
    rc, _ = _scan(mod, monkeypatch, chain, verdicts)
    out = capsys.readouterr().out

    assert rc == 0
    assert "DANGER" not in out
    assert "2 blocked by a conflict" in out, out


def test_a_pr_that_cannot_land_blocks_every_pair_that_starts_with_it(mod, monkeypatch, capsys):
    """If A already conflicts with master, no pair `A -> B` is reachable today.

    The pairs starting with the other PR are still measured - the scan must not stop at
    the first unusable candidate.
    """
    chain = {(BASE, C1): None, (BASE, C2): C2, (C2, C1): C1}
    verdicts = {C1: (True, "documents 1564")}
    rc, calls = _scan(mod, monkeypatch, chain, verdicts)
    out = capsys.readouterr().out

    assert rc == 0
    assert (BASE, C1) in calls, "the unreachable PR was tried and found blocking"
    assert (C2, C1) in calls, "the reachable order was still measured"
    # `1 -> 2` is blocked because 1 cannot land at all; `2 -> 1` is measured and healthy.
    assert "1 clean and healthy, 1 blocked by a conflict" in out, out


# --- cost and hygiene ------------------------------------------------------


def test_the_first_step_merge_is_reused_across_pairs(mod, monkeypatch, capsys):
    """`master + A` is materialised once per A, not once per pair.

    Asserted because the cost is the reason this is usable at all: recomputing it would
    double the merges for no new information, and nothing else would notice.

    **Three PRs, not two** - and that matters. With two PRs each one is the first element
    of exactly one ordered pair, so a per-pair recomputation calls `merge(base, A)` exactly
    once either way and the cache is unobservable. Measured: with two PRs, removing the
    cache kept this test green (the mutant survived). Three PRs make A the first element of
    two pairs, which is where the duplicate call shows up.
    """
    chain = {
        (BASE, C1): C1, (BASE, C2): C2, (BASE, C3): C3,
        (C1, C2): C2, (C1, C3): C3,
        (C2, C1): C1, (C2, C3): C3,
        (C3, C1): C1, (C3, C2): C2,
    }
    verdicts = {c: (True, "documents 1564") for c in (C1, C2, C3)}
    _, calls = _scan(mod, monkeypatch, chain, verdicts, prs=(1, 2, 3))
    capsys.readouterr()

    assert calls.count((BASE, C1)) == 1, calls
    assert calls.count((BASE, C2)) == 1, calls
    assert calls.count((BASE, C3)) == 1, calls
    assert len(calls) == 3 + 6, calls  # 3 first steps + 3 * 2 second steps


def test_a_repeated_number_does_not_pair_a_pr_with_itself(mod, monkeypatch, capsys):
    """Duplicates in argv are removed: a self-pair is not a question."""
    chain = {(BASE, C1): C1, (C1, C2): C2, (C2, C1): C1, (BASE, C2): C2}
    verdicts = {C1: (True, "documents 1564"), C2: (True, "documents 1564")}
    rc, calls = _scan(mod, monkeypatch, chain, verdicts, prs=(1, 1, 2))
    out = capsys.readouterr().out

    assert rc == 0
    assert (C1, C1) not in calls and (C2, C2) not in calls, calls
    assert "2 PR(s) -> 2 ordered pair(s)" in out, out


def test_an_unmeasurable_pair_is_not_a_pass(mod, monkeypatch, capsys):
    """A failure to measure exits 2 - never a reassuring 'no dangerous pair'."""

    def boom(a, b):
        raise mod.seq.MeasurementError("merge-tree failed")

    monkeypatch.setattr(mod.seq, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod.seq, "_fetch_head", lambda n: C1)
    monkeypatch.setattr(mod.seq, "_merge_commit", boom)
    rc = mod.main(["1", "2"])
    err = capsys.readouterr().err

    assert rc == 2
    assert "could not measure" in err


def test_the_guard_is_actually_consulted(mod, monkeypatch, capsys):
    """The verdict must come from the guard, not from the merge succeeding.

    A mutant that reported "healthy" whenever the merge was clean - without running the
    guard - is the fail-open shape this whole tool family exists to prevent, so the
    verdict is driven from a fake guard that records being asked.
    """
    asked: list[str] = []
    chain = {(BASE, C1): C1, (BASE, C2): C2, (C1, C2): C2, (C2, C1): C1}

    monkeypatch.setattr(mod.seq, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod.seq, "_fetch_head", lambda n: {1: C1, 2: C2}[n])
    monkeypatch.setattr(mod.seq, "_merge_commit", lambda a, b: chain.get((a, b)))
    monkeypatch.setattr(
        mod.seq,
        "_guard_verdict",
        lambda tree, workdir: (asked.append(tree) or (False, "documents 1564 but 1566 are collected")),
    )
    monkeypatch.setattr(
        mod.seq, "_run", lambda argv, cwd=None: _FakeProc(argv[-1].removesuffix("^{tree}"))
    )
    rc = mod.main(["1", "2"])
    capsys.readouterr()

    assert rc == 1, "both pairs are clean merges, so only the guard can make this a finding"
    assert len(asked) == 2, asked
