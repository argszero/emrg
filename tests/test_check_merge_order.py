"""Tests for scripts/check-merge-order.py — the merge-cascade forecast.

Background (cycle 20260911-225712)
----------------------------------
Eleven open PRs were each individually `MERGEABLE/CLEAN` and green, and each was
also ahead of master, so **any** one could be merged. Merging one turned the other
ten `CONFLICTING/DIRTY` on a single shared `Agent.md` line: no CI, no merge, and -
because resolving forces a push - **every vote on them voided**. Three PRs one vote
from landing went back to 0/3. The cost was real and, until this tool, invisible.

These tests pin the parts that are easy to get subtly wrong, in both directions
(#455 lesson - never infer a discriminator from one state):

* a clean merge and a conflicting merge must be told apart, and a *failure to
  measure* must be neither (it must not be reported as a conflict);
* the conflicted **paths** are read from `merge-tree`'s output, so the report names
  what collides rather than only that something did;
* **no mutable ref name reaches `merge-tree`** - the invariant this tool shipped
  without, which made its first live run answer about the wrong base.

Everything runs against `monkeypatched` subprocess runners or synthetic git
history under `tmp_path`; nothing here touches the network or the working tree.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-merge-order.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_merge_order", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


class TestTheMergeQuestionIsAnswered():
    """`merge-tree` rc 0 = clean, 1 = conflict, anything else = not answered."""

    def test_a_clean_merge_yields_no_paths(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 0, "", ""),
        )
        assert mod._conflict_paths("a", "b") == []

    def test_a_conflicting_merge_names_the_paths(self, mod, monkeypatch) -> None:
        out = (
            "100644 f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0 1\tAgent.md\n"
            "100644 e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0 2\tAgent.md\n"
            "100644 d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0 3\tAgent.md\n"
            "\n"
            "Auto-merging Agent.md\n"
            "CONFLICT (content): Merge conflict in Agent.md\n"
        )
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 1, out, ""),
        )
        assert mod._conflict_paths("a", "b") == ["Agent.md"]

    def test_two_conflicted_paths_are_both_reported_and_not_duplicated(
        self, mod, monkeypatch
    ) -> None:
        out = (
            "100644 f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0 1\tAgent.md\n"
            "100644 f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0 1\ttests/test_x.py\n"
            "100644 e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0 2\tAgent.md\n"
            "100644 e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0 2\ttests/test_x.py\n"
            "100644 d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0 3\tAgent.md\n"
            "100644 d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0 3\ttests/test_x.py\n"
        )
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 1, out, ""),
        )
        assert mod._conflict_paths("a", "b") == ["Agent.md", "tests/test_x.py"]

    def test_a_bad_ref_is_not_reported_as_a_conflict(self, mod, monkeypatch) -> None:
        """rc 128 (unknown revision) must be "not answered", never "conflict".

        The distinction is load-bearing: reporting a failed measurement as a
        conflict would invent a cascade that does not exist, and the caller would
        spend a resolution on it.
        """
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(
                argv, 128, "", "fatal: Not a valid object name"
            ),
        )
        assert mod._conflict_paths("nope", "b") is None

    def test_a_conflict_with_no_parseable_block_is_also_not_answered(
        self, mod, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 1, "garbage\n", ""),
        )
        assert mod._conflict_paths("a", "b") is None


class TestNoMutableRefNameReachesMergeTree:
    """The invariant whose absence produced a plausible, wrong live answer.

    `git fetch <anything>` rewrites `FETCH_HEAD`, so holding the *name*
    `FETCH_HEAD` across the PR-head fetches made every pair get measured against
    the last head fetched. Nothing crashed: the tool confidently reported one PR
    mergeable against "master" and everything else conflicting, because the base
    had silently become that PR's own head.
    """

    def test_the_base_is_resolved_to_a_sha_before_heads_are_fetched(
        self, mod, monkeypatch
    ) -> None:
        """`forecast` must rev-parse the base first, and measure with the SHA."""
        sha_by_ref = {
            "FETCH_HEAD^{commit}": "aaaa1111",
            "refs/emrg-forecast/pr1^{commit}": "bbbb2222",
        }
        measured: list[tuple[str, str]] = []

        def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(argv, 0, sha_by_ref[argv[-1]], "")
            if argv[:2] == ["git", "merge-tree"]:
                measured.append((argv[-2], argv[-1]))
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(mod, "_run", fake_run)
        monkeypatch.setattr(mod, "_fetch_head", lambda repo, n: f"refs/emrg-forecast/pr{n}")
        report = mod.forecast("FETCH_HEAD", [1], "argszero/emrg")

        assert report["base"] == "aaaa1111"
        # The point: the name never appears, only the resolved commit.
        assert measured == [("aaaa1111", "bbbb2222")]
        assert all("FETCH_HEAD" not in pair for pair in measured)

    def test_a_ref_that_does_not_resolve_fails_loudly(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(
                argv, 128, "", "fatal: Needed a single revision"
            ),
        )
        # The module raises the builtin; there is no module-local exception type.
        with pytest.raises(RuntimeError, match="could not resolve"):
            mod._rev_parse("FETCH_HEAD")

    def test_the_shipped_source_passes_only_commits_to_merge_tree(self, mod) -> None:
        """Static backstop: `_conflict_paths` is always called with resolved SHAs.

        The behavioural test above pins `forecast`'s ordering; this one stops a
        future edit from reintroducing a name at a call site that test does not
        cover. Both are needed - a single mocked path is exactly how the original
        defect would evade a behavioural-only suite.
        """
        source = SCRIPT.read_text(encoding="utf-8")
        assert "_rev_parse(base)" in source, "the base must be resolved before use"
        assert "_rev_parse(_fetch_head(" in source, "heads must be resolved too"
        for line in source.splitlines():
            if "_conflict_paths(" in line and not line.strip().startswith("def "):
                assert "base_sha" in line or "heads[" in line, (
                    f"a merge question is asked with something other than a "
                    f"resolved commit: {line.strip()}"
                )


class TestTheReportNamesWhatCollides():
    def test_a_pr_that_dirties_nothing_says_so(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_run", lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))
        monkeypatch.setattr(mod, "_fetch_head", lambda repo, n: f"refs/emrg-forecast/pr{n}")
        report = mod.forecast("base", [1, 2], "argszero/emrg")
        assert all(entry["dirtied"] == [] for entry in report["prs"].values())
        assert report["base_conflicts"] == []

    def test_a_shared_conflicting_path_is_attributed_to_both(self, mod, monkeypatch) -> None:
        """A cascade is symmetric: each PR must list the other in `dirtied`."""
        out = (
            "100644 f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0 1\tAgent.md\n"
            "100644 e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0 2\tAgent.md\n"
        )

        def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(argv, 0, "c" * 40, "")
            if argv[:2] == ["git", "merge-tree"]:
                return subprocess.CompletedProcess(argv, 1, out, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(mod, "_run", fake_run)
        monkeypatch.setattr(mod, "_fetch_head", lambda repo, n: f"refs/emrg-forecast/pr{n}")
        report = mod.forecast("base", [7, 9], "argszero/emrg")
        assert report["prs"][7]["dirtied"] == [{"pr": 9, "paths": ["Agent.md"]}]
        assert report["prs"][9]["dirtied"] == [{"pr": 7, "paths": ["Agent.md"]}]

    def test_the_exit_code_is_not_zero_when_a_pr_conflicts_with_the_base(
        self, mod, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(mod, "_run", lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))
        monkeypatch.setattr(
            mod,
            "forecast",
            lambda base, numbers, repo: {
                "base": "abc",
                "prs": {1: {"paths": ["Agent.md"], "dirtied": []}},
                "base_conflicts": [1],
            },
        )
        assert mod.main(["1"]) == 1

    def test_a_clean_queue_exits_zero(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_run", lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))
        monkeypatch.setattr(
            mod,
            "forecast",
            lambda base, numbers, repo: {"base": "abc", "prs": {}, "base_conflicts": []},
        )
        assert mod.main(["1"]) == 0

    def test_a_failed_measurement_exits_two_and_writes_nothing_to_stdout(
        self, mod, monkeypatch, capsys
    ) -> None:
        def boom(base, numbers, repo):
            raise RuntimeError("could not classify")

        monkeypatch.setattr(mod, "_run", lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))
        monkeypatch.setattr(mod, "forecast", boom)
        assert mod.main(["1"]) == 2
        assert capsys.readouterr().out == ""


class TestAgainstRealGitHistory:
    """One end-to-end case on a synthetic repo, so the argv really is accepted."""

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
        return proc.stdout.strip()

    def test_a_real_conflict_is_detected_and_a_real_clean_merge_is_not(
        self, tmp_path, monkeypatch
    ) -> None:
        repo = tmp_path / "r"
        repo.mkdir()
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "t")
        (repo / "f.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "base")

        # `clean` touches an unrelated file, so it cannot conflict.
        self._git(repo, "checkout", "-q", "-b", "clean")
        (repo / "g.txt").write_text("other\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "add another file")

        # `grow` diverges from the same base and edits the same line as main will.
        self._git(repo, "checkout", "-q", "-b", "grow", "main")
        (repo / "f.txt").write_text("grow side\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "grow rewrites the line")

        self._git(repo, "checkout", "-q", "main")
        (repo / "f.txt").write_text("main side\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "main rewrites the line")

        monkeypatch.chdir(repo)
        mod = _load_module()
        assert mod._conflict_paths("main", "clean") == []
        assert mod._conflict_paths("main", "grow") == ["f.txt"]
