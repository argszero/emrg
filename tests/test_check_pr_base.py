"""Tests for scripts/check-pr-base.py - PRs based on a branch that cannot reach master.

The defect (`cyc20260911-210746`): #1148's base was the branch of #1147, which was
*squash*-merged. The base branch therefore is not an ancestor of master, and
`gh pr merge` - which merges into the PR's **base branch** - would have landed the
fix in a dead end. The PR would have read MERGED with three valid ✅ votes while
master's classifier kept answering `KEEP BOTH (concatenate)` on the blocks the PR
fixes. Every existing gate passes this state: CI is green (against the base), the
vote helper counts the votes (it never reads the base), and the PR is MERGEABLE.

The tests below pin the *classification*, with the two shapes that must come out
differently - which is the whole reason the check cannot just be "base == master":

* a stacked PR whose parent is still live on master is **fine** (that is the
  workflow #1149 exists to serve, and flagging it would make the check noise);
* a base that is gone, or whose head is not on master after a squash merge, is a
  dead end and must be flagged.

The measurement predicate itself is verified: `_ref_is_on_master` reads the
compare API's `status`, and the fixtures cover every status GitHub can return -
including the two that mean "on master" (`identical`, `behind`), because a version
that treated any non-`ahead` status as fine would pass a `diverged` base.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-pr-base.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_pr_base", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


class TestCompareStatusIsReadCorrectly:
    """`_ref_is_on_master` must read the compare status, not infer from a count."""

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("identical", True),   # the commit *is* master's head
            ("behind", True),      # master has moved on; the commit is its ancestor
            ("ahead", False),      # the commit is not in master at all
            ("diverged", False),   # forked off before master's tip
        ],
    )
    def test_every_compare_status_maps_to_the_right_answer(
        self, mod, monkeypatch, status, expected
    ) -> None:
        def fake_gh_json(*args):
            assert any("compare/master..." in a for a in args), args
            return {"status": status}

        monkeypatch.setattr(mod, "_gh_json", fake_gh_json)
        assert mod._ref_is_on_master("deadbeef", "owner/repo") is expected

    def test_a_missing_status_is_an_error_not_a_pass(self, mod, monkeypatch) -> None:
        """Unreadable state must fail loud - guessing 'OK' hides the silent case."""
        monkeypatch.setattr(mod, "_gh_json", lambda *a: {"commits": []})
        with pytest.raises(RuntimeError, match="no status"):
            mod._ref_is_on_master("deadbeef", "owner/repo")


class TestClassifyBase:
    def test_master_is_always_ok(self, mod) -> None:
        verdict, why = mod.classify_base("master", {}, "owner/repo")
        assert verdict == "OK"
        assert "master" in why

    def test_a_deleted_base_branch_is_dead(self, mod) -> None:
        """No branch at all: a merge would have nowhere to land."""
        verdict, why = mod.classify_base("feature/gone", {}, "owner/repo")
        assert verdict == "DEAD"
        assert "no longer exists" in why

    def test_a_base_not_on_master_is_dead(self, mod, monkeypatch) -> None:
        """The measured #1148 shape: the branch exists, its head is not on master."""
        monkeypatch.setattr(mod, "_ref_is_on_master", lambda sha, repo: False)
        verdict, why = mod.classify_base(
            "feature/conflict-classifier-multiline-count",
            {"feature/conflict-classifier-multiline-count": "fb5a4e99" * 5},
            "owner/repo",
        )
        assert verdict == "DEAD"
        assert "not on master" in why

    def test_a_live_stacked_base_is_ok(self, mod, monkeypatch) -> None:
        """A stacked PR whose parent is still on master is legitimate, not a defect.

        This is the other half of the discrimination: if everything non-master were
        flagged, the check would cry wolf on the normal stacked workflow.
        """
        monkeypatch.setattr(mod, "_ref_is_on_master", lambda sha, repo: True)
        verdict, why = mod.classify_base(
            "feature/live-parent", {"feature/live-parent": "abc12345" * 5}, "owner/repo"
        )
        assert verdict == "OK"
        assert "already on master" in why


class TestMainExitCodes:
    def _wire(self, mod, monkeypatch, prs, branches, on_master):
        monkeypatch.setattr(mod, "_open_prs", lambda repo: prs)
        monkeypatch.setattr(mod, "_branch_heads", lambda repo: branches)
        monkeypatch.setattr(mod, "_ref_is_on_master", lambda sha, repo: on_master(sha))

    def test_exit_1_when_a_pr_is_based_on_a_dead_end(self, mod, monkeypatch) -> None:
        prs = [
            {"number": 1148, "baseRefName": "feature/dead", "headRefName": "h1"},
            {"number": 1151, "baseRefName": "master", "headRefName": "h2"},
        ]
        self._wire(mod, monkeypatch, prs, {"feature/dead": "s1"}, lambda sha: False)
        assert mod.main(["--repo", "owner/repo"]) == 1

    def test_exit_0_when_every_base_can_reach_master(self, mod, monkeypatch) -> None:
        prs = [
            {"number": 1151, "baseRefName": "master", "headRefName": "h1"},
            {"number": 1152, "baseRefName": "feature/live", "headRefName": "h2"},
        ]
        self._wire(mod, monkeypatch, prs, {"feature/live": "s2"}, lambda sha: True)
        assert mod.main(["--repo", "owner/repo"]) == 0

    def test_exit_2_when_the_state_cannot_be_read(self, mod, monkeypatch) -> None:
        """A check that reports OK when it could not look is worse than none."""
        def boom(repo):
            raise RuntimeError("gh failed (rc=1): network down")

        monkeypatch.setattr(mod, "_open_prs", boom)
        assert mod.main(["--repo", "owner/repo"]) == 2

    def test_selecting_one_pr_filters_the_rest(self, mod, monkeypatch, capsys) -> None:
        prs = [
            {"number": 1, "baseRefName": "feature/dead", "headRefName": "h1"},
            {"number": 2, "baseRefName": "master", "headRefName": "h2"},
        ]
        self._wire(mod, monkeypatch, prs, {"feature/dead": "s1"}, lambda sha: False)
        rc = mod.main(["--repo", "owner/repo", "2"])
        out = capsys.readouterr().out
        assert rc == 0, "PR 2 is fine; PR 1 was not asked about"
        assert "#2" in out and "#1" not in out


class TestRealInvocationSurface:
    """The script must be runnable as a tool, and must not silently pass on error."""

    def test_help_exits_zero(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert "base branch" in proc.stdout

    def test_unreachable_gh_exits_2_not_0(self) -> None:
        """With a bogus repo gh fails; the tool must not report a clean bill."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", "argszero/definitely-not-a-repo-xyz"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(Path.home())},
        )
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
