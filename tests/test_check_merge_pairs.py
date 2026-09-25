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
* a pair blocked by a conflict is *answered* (exit 0, counted as blocked), not a failure -
  and since 2026-09-25 (`cyc20260925-191034`) it says **which** conflict and **where**:
  a pair-level conflict is counted and named with its paths, while a PR that cannot land on
  the base at all is counted apart and named once, because the two have different repairs;
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


def _scan(mod, monkeypatch, chain, verdicts, prs=(1, 2), base=BASE, paths=None):
    """Drive main() with faked head resolution, merges and guard verdicts.

    `chain` maps (onto, head) -> the commit the merge produces, or None for a conflict.
    `verdicts` maps the final commit -> (passed, report).
    `paths` maps the same (onto, head) key -> the paths that conflict, for the pairs and
    first steps the chain marks as conflicts; anything unlisted reads as a conflict whose
    report named no path.
    """
    calls: list[tuple[str, str]] = []
    conflict_paths: dict[tuple[str, str], list[str]] = dict(paths or {})
    heads = {n: c for n, c in zip((1, 2, 3), (C1, C2, C3))}

    monkeypatch.setattr(mod.seq, "_rev_parse", lambda ref: base)
    monkeypatch.setattr(mod.seq, "_fetch_head", lambda n: heads[n])
    # The base is refreshed for real in `main` (a `git fetch`), so it is stubbed here:
    # a test that reaches the network is not a test of this tool. The call itself is
    # asserted in `test_the_base_is_refreshed_between_the_probe_and_the_read`.
    monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: None)

    def fake_merge(a, b):
        calls.append((a, b))
        return chain.get((a, b))

    monkeypatch.setattr(mod.seq, "_merge_commit", fake_merge)
    # The real reader folds the merge a second time to decode git's stage block, so it is
    # faked alongside `_merge_commit`: with fake SHAs it could only fail, and what these
    # tests are about is what the run *prints*, not git's quoting.
    monkeypatch.setattr(
        mod.seq, "_conflict_paths", lambda a, b: list(conflict_paths.get((a, b), []))
    )
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


def test_the_verdict_names_the_guard_that_answered(mod, monkeypatch, capsys):
    """A clean run says *which* guard cleared it - in the header and in the verdict.

    That sentence is one guard's reading, and the run used to print it without naming
    that guard: measured 2026-09-19 (`cyc20260919-212912`), the same tool's
    `_guard_verdict` returned `no stored count` for a pair tree whose own suite was red
    (1 of 53 tests), and the summary was read as "the pair is safe to land". The name is
    taken from the sibling (`seq.GUARD`) rather than written here, so a run cannot
    announce a guard other than the one it ran.
    """
    chain = {(BASE, C1): C1, (BASE, C2): C2, (C1, C2): C2, (C2, C1): C1}
    verdicts = {C1: (True, "no stored count"), C2: (True, "no stored count")}
    rc, _ = _scan(mod, monkeypatch, chain, verdicts)
    out = capsys.readouterr().out

    assert rc == 0
    header = next(l for l in out.splitlines() if l.startswith("pairs:"))
    summary = next(l for l in out.splitlines() if "failing tree" in l)
    assert header.endswith(f"judged by {mod.seq.GUARD}"), header
    assert f"judged by {mod.seq.GUARD} alone" in summary, summary


def test_a_conflicting_pair_is_answered_not_a_finding(mod, monkeypatch, capsys):
    """A pair git blocks cannot land, so it cannot land badly - and it is not a failure.

    This is why a stopped scan is exit 0 here while a stopped *plan* is 3 in
    `check-merge-sequence.py`: every pair was answered, whereas a plan stopped at a
    conflict leaves the steps behind it unmeasured.

    The word this asserts changed on 2026-09-25: the summary used to say "blocked by a
    conflict" for both a pair-level conflict and a PR that cannot land on the base, so the
    count named the wrong cause for one of them (see the test below). It now says which.
    """
    chain = {(BASE, C1): C1, (BASE, C2): C2, (C1, C2): None, (C2, C1): None}
    verdicts = {}
    rc, _ = _scan(mod, monkeypatch, chain, verdicts)
    out = capsys.readouterr().out

    assert rc == 0
    assert "DANGER" not in out
    assert "2 blocked by a pair conflict" in out, out


def test_a_blocked_pair_names_the_paths_it_conflicts_on(mod, monkeypatch, capsys):
    """A refusal that names no working way out is the defect, not the refusal.

    `check-merge-order.py` names the pair's file (`dirties 1 other PR(s) on Agent.md`) about
    the same pair, and this tool answered "blocked by a conflict" with no path: the reader
    deciding an order could not tell a one-line `Agent.md` collision from a product-code
    one. Measured 2026-09-25 (`cyc20260925-191034`) on the live queue, where `#1617` and
    `#1618` conflict on `Agent.md` alone.

    The path comes from the sibling's `_conflict_paths`, so the assertion is on the pair's
    line rather than on the word "Agent.md" appearing somewhere: the reader needs it beside
    the pair it belongs to.
    """
    chain = {(BASE, C1): C1, (BASE, C2): C2, (C1, C2): None, (C2, C1): None}
    rc, _ = _scan(
        mod,
        monkeypatch,
        chain,
        {},
        paths={(C1, C2): ["Agent.md"], (C2, C1): ["Agent.md", "emrg/server/daemon.py"]},
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "  #1 -> #2: blocked - conflicts on Agent.md\n" in out, out
    assert (
        "  #2 -> #1: blocked - conflicts on Agent.md, emrg/server/daemon.py\n" in out
    ), out


def test_a_conflict_whose_report_names_no_path_is_not_printed_as_silence(mod, monkeypatch, capsys):
    """`_conflict_paths` returns `[]` for a conflict git's report did not name paths for.

    `[]` is also what it returns for a *clean* merge, so the empty reading is only ever
    reached here for a pair `_merge_commit` already said cannot land - and printing nothing
    after "blocked - " would read as a conflict on no file, which is a different claim from
    a report that named none.
    """
    chain = {(BASE, C1): C1, (BASE, C2): C2, (C1, C2): None, (C2, C1): C1}
    verdicts = {C1: (True, "documents 1564")}
    rc, _ = _scan(mod, monkeypatch, chain, verdicts)
    out = capsys.readouterr().out

    assert rc == 0
    assert "  #1 -> #2: blocked - conflicts, but git's report names no path\n" in out, out


def test_a_pr_that_cannot_land_blocks_every_pair_that_starts_with_it(mod, monkeypatch, capsys):
    """If A already conflicts with master, no pair `A -> B` is reachable today.

    The pairs starting with the other PR are still measured - the scan must not stop at
    the first unusable candidate.

    **This is the half that the summary used to get wrong.** The pair `1 -> 2` was counted
    as "blocked by a conflict" - the same words, and the same number, as a conflict *between
    the two PRs* - while its cause is A alone, and its repair is a rebase rather than an
    order. The two are now counted apart, and this assertion is what pins that: the
    `#1 -> #2` pair is not in the pair-conflict count at all.
    """
    chain = {(BASE, C1): None, (BASE, C2): C2, (C2, C1): C1}
    verdicts = {C1: (True, "documents 1564")}
    rc, calls = _scan(mod, monkeypatch, chain, verdicts, paths={(BASE, C1): ["Agent.md"]})
    out = capsys.readouterr().out

    assert rc == 0
    assert (BASE, C1) in calls, "the unreachable PR was tried and found blocking"
    assert (C2, C1) in calls, "the reachable order was still measured"
    assert "  #1: cannot land on the base - conflicts on Agent.md" in out, out
    assert "  #1 -> #2: blocked" not in out, "A's own conflict is not a blocked pair"
    assert (
        "1 clean and healthy, 0 blocked by a pair conflict, "
        "1 unscanned because a PR conflicts with the base (#1)" in out
    ), out


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
    monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: None)
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
    monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: None)
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


# --- the base must be the base you named -----------------------------------


def test_a_remote_tracking_base_is_resolved_by_full_name(mod, monkeypatch, capsys):
    """`origin/master` must be measured as a *remote-tracking* ref, not by short name.

    `git rev-parse origin/master` searches `refs/heads/origin/master` before
    `refs/remotes/origin/master`, so a stray local branch of that name replaces the
    base. Asserted by the ref the tool asks for, not by a comment: the short name
    must never be what reaches the resolver.
    """
    asked: list[str] = []
    chain = {(BASE, C1): C1, (BASE, C2): C2, (C1, C2): C2, (C2, C1): C1}

    def fake_rev_parse(ref):
        asked.append(ref)
        if ref == "refs/remotes/origin/master":
            return BASE
        raise mod.seq.MeasurementError(f"no such ref {ref}")

    monkeypatch.setattr(mod.seq, "_rev_parse", fake_rev_parse)
    monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: None)
    monkeypatch.setattr(mod.seq, "_fetch_head", lambda n: {1: C1, 2: C2}[n])
    monkeypatch.setattr(mod.seq, "_merge_commit", lambda a, b: chain.get((a, b)))
    monkeypatch.setattr(mod.seq, "_guard_verdict", lambda tree, workdir: (True, "documents 1564"))
    monkeypatch.setattr(
        mod.seq, "_run", lambda argv, cwd=None: _FakeProc(argv[-1].removesuffix("^{tree}"))
    )
    rc = mod.main(["--base", "origin/master", "1", "2"])
    out = capsys.readouterr().out

    assert "refs/remotes/origin/master" in asked, asked
    assert "origin/master" not in asked, f"the shadowable short name was resolved: {asked}"
    assert rc == 0, out
    # The output names the ref actually measured, so a reader can check it.
    assert "refs/remotes/origin/master" in out, out


def test_a_base_name_shadowed_by_a_local_branch_is_refused(mod, monkeypatch, capsys):
    """A name that is *only* a local branch is refused, not measured.

    Measured live on 2026-09-13 (`cyc20260913-102231`) with master at `5f0ee34`: a
    local branch named `origin/master` at `633a777` made the tool print `base
    633a7779 (origin/master)` and report `#1173 -> #1174: DANGER` - the historical
    pair, on a base nobody named. The refusal replaces a *true answer about the wrong
    base*, and nothing below the resolver can detect it, so it has to happen here.
    """
    merges: list[tuple[str, str]] = []

    def fake_rev_parse(ref):
        if ref == "refs/heads/origin/master":
            return BASE  # the shadowing local branch exists
        raise mod.seq.MeasurementError(f"no such ref {ref}")

    monkeypatch.setattr(mod.seq, "_rev_parse", fake_rev_parse)
    monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: None)
    monkeypatch.setattr(mod.seq, "_fetch_head", lambda n: C1)
    monkeypatch.setattr(
        mod.seq, "_merge_commit", lambda a, b: merges.append((a, b)) or C1
    )
    rc = mod.main(["--base", "origin/master", "1", "2"])
    err = capsys.readouterr().err

    assert rc == 2, "measuring a local branch that merely looks like a remote is not a pass"
    assert "refs/heads/origin/master" in err and "refs/remotes/origin/master" in err, err
    assert merges == [], "nothing may be measured from a base that was not named"


def test_a_plain_base_is_left_alone(mod, monkeypatch, capsys):
    """The other direction: a branch name, tag or SHA is what the caller meant.

    Without this, a mutant that refused every `--base` would pass the test above.
    """
    asked: list[str] = []
    chain = {(BASE, C1): C1, (BASE, C2): C2, (C1, C2): C2, (C2, C1): C1}

    def fake_rev_parse(ref):
        asked.append(ref)
        return BASE

    monkeypatch.setattr(mod.seq, "_rev_parse", fake_rev_parse)
    monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: None)
    monkeypatch.setattr(mod.seq, "_fetch_head", lambda n: {1: C1, 2: C2}[n])
    monkeypatch.setattr(mod.seq, "_merge_commit", lambda a, b: chain.get((a, b)))
    monkeypatch.setattr(mod.seq, "_guard_verdict", lambda tree, workdir: (True, "documents 1564"))
    monkeypatch.setattr(
        mod.seq, "_run", lambda argv, cwd=None: _FakeProc(argv[-1].removesuffix("^{tree}"))
    )
    rc = mod.main(["--base", "master", "1", "2"])

    assert rc == 0
    assert asked == ["master"], asked


# --- ... and it must be the base *as it is now* -----------------------------


def test_the_base_is_refreshed_between_the_probe_and_the_read(mod, monkeypatch, capsys):
    """The resolved ref is refreshed, and the order is probe -> refresh -> read.

    Resolving a remote-tracking name *reads* it (does it exist?), so a "refresh first"
    assertion cannot be "no read happens before the refresh" - it is that the read which
    becomes the base comes after. Pinned on the sequence and on the ref: the short
    spelling never reaches the refresh, and the refresh is not skipped, which is what
    this tool did before (`cyc20260914-000319`).
    """
    events: list[tuple[str, str]] = []

    monkeypatch.setattr(
        mod.seq, "_refresh_base", lambda ref: events.append(("refresh", ref))
    )
    monkeypatch.setattr(
        mod.seq, "_rev_parse", lambda ref: events.append(("read", ref)) or BASE
    )
    monkeypatch.setattr(mod.seq, "_fetch_head", lambda n: C1)
    # No pair lands, so the pair loop needs neither a merge nor a guard.
    monkeypatch.setattr(mod.seq, "_merge_commit", lambda a, b: None)
    rc = mod.main(["--base", "origin/master", "1", "2"])
    capsys.readouterr()
    remote = "refs/remotes/origin/master"

    assert rc == 0
    assert events == [
        ("read", remote),  # `_resolve_base` probing that the remote-tracking ref exists
        ("refresh", remote),  # the base is brought up to date
        ("read", remote),  # and only now is its commit the base
    ], events

    events.clear()
    mod.main(["--base", "master", "1", "2"])
    capsys.readouterr()
    # No probe read: a plain name is not resolved by full name, so it goes through the
    # refresh (which returns it untouched) and is read once. The refresh is still called
    # for every base - it is what decides that a SHA or a branch is not a remote ref.
    assert events == [("refresh", "master"), ("read", "master")], events


def test_a_stale_remote_tracking_base_follows_the_remote_with_real_git(
    mod, monkeypatch, capsys, tmp_path
):
    """The defect's effect, with real git: the printed base is the remote's current tip.

    A hermetic clone whose `refs/remotes/origin/master` sits one commit behind the remote
    it is a clone of - the normal state of a checkout that has not fetched. The arm states
    the discriminating reading first (the ref the tool would have measured *is* the stale
    commit), then runs `main` with real git for the refresh, the resolution and the read,
    and fakes only the network-shaped parts (the PR heads, the merges, the guard), so the
    test neither reaches GitHub nor runs a pipeline.

    The shas are pinned (commit dates, `PLAN_COMMIT_DATE` in the sibling) so this is a
    measurement and not a timestamp.
    """
    import os
    import subprocess
    import tempfile

    pinned = {
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    env = {**os.environ, **pinned}

    def git(cwd, *args):
        out = subprocess.run(
            ["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        assert out.returncode == 0, (args, out.stdout, out.stderr)
        return out.stdout.strip()

    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare", "-b", "master")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "master")
    (seed / "a.txt").write_text("one\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "first")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "master")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(clone)],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    track = "refs/remotes/origin/master"
    stale = git(clone, "rev-parse", track)

    (seed / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "the remote moves on")
    git(seed, "push", "-q", "origin", "master")
    advanced = git(seed, "rev-parse", "master")
    assert stale != advanced, "precondition: the clone is behind the remote"

    monkeypatch.chdir(clone)
    # `main` makes a scratch dir for the guard; keep it inside the test's own tmp
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(mod.seq, "_fetch_head", lambda n: advanced)
    monkeypatch.setattr(mod.seq, "_merge_commit", lambda a, b: b)
    monkeypatch.setattr(mod.seq, "_guard_verdict", lambda tree, workdir: (True, "documents N"))

    assert git(clone, "rev-parse", track) == stale
    # The discriminating reading: resolving is not refreshing, so at this point the tool
    # would print - and measure everything against - the stale commit.
    assert git(clone, "rev-parse", mod.seq._rev_parse(mod._resolve_base("origin/master"))) == stale

    rc = mod.main(["--base", "origin/master", "1", "2"])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert f"base {advanced[:8]}" in out, out
    assert f"base {stale[:8]}" not in out, "the stale base is what the refresh exists to prevent"
    assert git(clone, "rev-parse", track) == advanced, "the remote-tracking ref was refreshed"
    assert git(clone, "rev-parse", "refs/heads/master") == stale, (
        "the refresh moves the remote-tracking ref only, never the local branch"
    )


def test_a_base_that_cannot_be_refreshed_is_not_answered(mod, monkeypatch, capsys):
    """A base that could not be verified is exit 2 - never measured from anyway.

    The refresh's failure is this tool's own loud failure (a fetch error, an unresolvable
    remote HEAD spelling): answering below it would report a verdict about whatever the
    ref happens to hold, which is the reading the refresh exists to remove.
    """
    merges: list[tuple[str, str]] = []

    def boom(ref):
        raise mod.seq.MeasurementError(f"could not refresh {ref}: fetch failed")

    monkeypatch.setattr(mod.seq, "_refresh_base", boom)
    monkeypatch.setattr(mod.seq, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod.seq, "_fetch_head", lambda n: C1)
    monkeypatch.setattr(mod.seq, "_merge_commit", lambda a, b: merges.append((a, b)) or C1)
    rc = mod.main(["--base", "origin/master", "1", "2"])
    err = capsys.readouterr().err

    assert rc == 2, "an unverifiable base is not a pass"
    assert "could not measure" in err, err
    assert merges == [], "nothing may be measured from a base that was not verified"


def test_a_refused_base_is_never_refreshed(mod, monkeypatch, capsys):
    """Resolving comes first, so a name refused as a stray is not refreshed into being.

    The refusal's whole point is that the caller named a remote ref that is not there
    while a *local* branch of that name is; refreshing before the refusal would fetch the
    remote-tracking ref and quietly answer from it instead. Both behaviours are defensible
    in isolation, so the order is pinned deliberately, and this test is what pins it.
    """
    refreshed: list[str] = []

    def fake_rev_parse(ref):
        if ref == "refs/heads/origin/master":
            return BASE  # the shadowing local branch exists
        raise mod.seq.MeasurementError(f"no such ref {ref}")

    monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: refreshed.append(ref))
    monkeypatch.setattr(mod.seq, "_rev_parse", fake_rev_parse)
    monkeypatch.setattr(mod.seq, "_fetch_head", lambda n: C1)
    monkeypatch.setattr(
        mod.seq, "_merge_commit", lambda a, b: pytest.fail("measured a refused base")
    )
    rc = mod.main(["--base", "origin/master", "1", "2"])
    err = capsys.readouterr().err

    assert rc == 2, err
    assert refreshed == [], "a base refused as a stray local branch must not be refreshed"


def test_a_requested_pr_whose_head_cannot_be_fetched_is_not_a_pass(mod, monkeypatch, capsys):
    """A lone unmeasurable number must not become a reassurance about zero pairs.

    Measured before the fix (`cyc20260919-173431`): `check-merge-pairs.py 99999` printed
    `pairs: 1 PR(s) -> 0 ordered pair(s)` and `no ordered pair merges cleanly into a
    failing tree`, exit 0. The number was taken verbatim, a single PR forms no pair, the
    loop never ran, and the summary line *became* the verdict - "no dangerous pair" about
    a PR that does not exist. The exit-code contract above rules that reading out, and the
    empty-pair case reaches it without a single `_fetch_head` call, which is why the head
    is resolved for every requested number before any pair is formed.

    The existing tests are the other direction: explicit numbers that *do* resolve are
    still measured, so this cannot be satisfied by refusing every explicit selection.
    """

    def missing(n):
        raise mod.seq.MeasurementError(f"could not fetch PR #{n}")

    monkeypatch.setattr(mod.seq, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: None)
    monkeypatch.setattr(mod.seq, "_fetch_head", missing)
    monkeypatch.setattr(mod.seq, "_merge_commit", lambda a, b: pytest.fail("no pair to merge"))
    rc = mod.main(["99999"])
    captured = capsys.readouterr()

    assert rc == 2, captured.out
    assert "no ordered pair" not in captured.out, (
        "a verdict about unformed pairs is the fail-open shape this tool forbids"
    )
    assert "99999" in captured.err, captured.err


def test_every_requested_head_is_resolved_once_before_any_pair(mod, monkeypatch, capsys):
    """The heads are fetched once per run, not once per pair - and before the pairs.

    Two properties in one measurement, because they are the same edit: the up-front
    resolution is what turns an unfetchable number into rc 2, and reusing its result is
    what keeps that from costing a fetch per pair. Asserted by count, with three PRs so
    each head is an element of more than one ordered pair.
    """
    fetched: list[int] = []
    chain = {
        (BASE, C1): C1, (BASE, C2): C2, (BASE, C3): C3,
        (C1, C2): C2, (C1, C3): C3, (C2, C1): C1, (C2, C3): C3, (C3, C1): C1, (C3, C2): C2,
    }
    heads = {1: C1, 2: C2, 3: C3}

    def fetch(n):
        fetched.append(n)
        return heads[n]

    monkeypatch.setattr(mod.seq, "_rev_parse", lambda ref: BASE)
    monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: None)
    monkeypatch.setattr(mod.seq, "_fetch_head", fetch)
    monkeypatch.setattr(mod.seq, "_merge_commit", lambda a, b: chain.get((a, b)))
    monkeypatch.setattr(mod.seq, "_guard_verdict", lambda tree, workdir: (True, "documents 1564"))
    monkeypatch.setattr(
        mod.seq, "_run", lambda argv, cwd=None: _FakeProc(argv[-1].removesuffix("^{tree}"))
    )
    rc = mod.main(["1", "2", "3"])
    capsys.readouterr()

    assert rc == 0, "three clean, healthy pairs"
    assert sorted(fetched) == [1, 2, 3], f"each head once, not once per pair: {fetched}"
