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
* both answers are read from the **merged tree's name** on `merge-tree`'s first
  line, never from its exit code - exit 0 with nothing printed is what `--quiet`
  does to a clean merge, and exit 1 with nothing printed is what an unmergeable
  input pair does (measured, `cyc20260914-055701`), so an unnamed tree is "not
  answered" whichever code came with it;
* the conflicted **paths** are read from `merge-tree`'s output, so the report names
  what collides rather than only that something did;
* **no mutable ref name reaches `merge-tree`** - the invariant this tool shipped
  without, which made its first live run answer about the wrong base.

Everything runs against `monkeypatched` subprocess runners or synthetic git
history under `tmp_path`; nothing here touches the network or the working tree.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-merge-order.py"

# The shape `git merge-tree --write-tree` actually prints, measured 2026-09-14
# (`cyc20260914-055701`) in a scratch repo: the merged tree's OID on the first line
# - for a clean merge *and* for a conflict - then the stage block, then a blank line
# and the messages. A fixture without that first line models a report git never
# writes, and after the change these tests pin, such a report is *not answered*
# rather than a verdict, so those fixtures would stop exercising the code they were
# written for. Hence every clean/conflict fixture below carries `_TREE` explicitly.
_TREE = "9" * 40


def _load_module():
    spec = importlib.util.spec_from_file_location("check_merge_order", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


class TestTheMergeQuestionIsAnswered():
    """Both answers come from the named tree; the exit code is not the answer.

    `merge-tree` exits 0 for a clean merge *and* for `--quiet` on the same merge
    (printing nothing), and exits 1 for a conflict *and* for a failure to merge
    the two inputs (printing nothing) - measured 2026-09-14 (`cyc20260914-055701`).
    So: "no merged tree named" = not answered, whatever the code says.
    """

    def test_a_clean_merge_yields_no_paths(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 0, _TREE + "\n", ""),
        )
        assert mod._conflict_paths("a", "b") == []

    def test_a_conflicting_merge_names_the_paths(self, mod, monkeypatch) -> None:
        out = (
            _TREE + "\n"
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
            _TREE + "\n"
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

    def test_a_clean_exit_with_no_named_tree_is_not_an_answer(
        self, mod, monkeypatch
    ) -> None:
        """rc 0 and nothing printed is a shape git really writes - `--quiet`.

        Measured 2026-09-14 (`cyc20260914-055701`) in a scratch repo:
        `git merge-tree --write-tree --quiet <a> <b>` on a *clean* merge exits 0
        with empty stdout, and on a conflict exits 1 with empty stdout. This tool
        passed no `--quiet`, but it read the clean answer from the code exactly as
        if it had: `[]` (= "the merge is clean") came out of an exit code, over a
        report it never looked at. `[]` is the one answer downstream re-checks
        nothing about - `forecast` then reports that PR as conflicting with
        nothing and may recommend an order it cannot take - so it has to be
        evidenced by the tree's name, like the conflict answer already was.
        """
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 0, "", ""),
        )
        assert mod._conflict_paths("a", "b") is None

    def test_a_conflict_block_without_a_named_tree_is_not_answered(
        self, mod, monkeypatch
    ) -> None:
        """The same rule on the conflict side: paths alone are not a verdict.

        A report whose first line is not the merged tree's name did not answer the
        merge question, so the paths in it are not evidence of one - they are read
        only once the tree is named. This is the shape a fabricated-looking
        fixture always had; it is asserted here so the rule covers both answers
        rather than only the reassuring one.
        """
        out = (
            "100644 f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0 1\tAgent.md\n"
            "100644 e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0 2\tAgent.md\n"
            "100644 d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0 3\tAgent.md\n"
        )
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 1, out, ""),
        )
        assert mod._conflict_paths("a", "b") is None

    def test_only_an_object_name_counts_as_the_named_tree(
        self, mod, monkeypatch
    ) -> None:
        """A first line that is merely *present* is not a named tree.

        Without this, the rule degenerates into "the report was non-empty": any
        diagnostic on the first line would be read as the merge's answer.
        """
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(
                argv, 0, "merge-tree: not something we can merge\n", ""
            ),
        )
        assert mod._conflict_paths("a", "b") is None

    def test_both_object_formats_name_a_tree(self, mod, monkeypatch) -> None:
        """SHA-256 clones exist; the shape of the answer must not depend on the clone."""
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 0, "a" * 64 + "\n", ""),
        )
        assert mod._conflict_paths("a", "b") == []

    def test_the_reading_decodes_the_names_git_writes(self, mod, monkeypatch) -> None:
        """The wiring arm: a pass-through copy of the shape answers a name nobody has.

        Measured 2026-09-14 (`cyc20260914-104220`, git 2.50.1, scratch repo): with the
        default `core.quotePath=true` a non-ASCII path arrives C-quoted in the stage
        block, and the reading this file used to have returned that spelling - which
        `(repo / that).exists()` measured `False`. Every plain-ASCII arm passes with
        either reading, so this is the one that tells them apart; it is also what makes
        "the tool asks `merge_tree.fold`" a *measured* claim rather than a comment.

        The fixture is the real report's shape (the merged tree's OID, the three stage
        lines, the blank line, the prose), so a reading that stops at the blank line
        and a reading that scans every line are both exercised here.
        """
        quoted = '"\\344\\270\\255\\346\\226\\207.txt"'
        report = (
            _TREE + "\n"
            f"100644 f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0 1\t{quoted}\n"
            f"100644 e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0 2\t{quoted}\n"
            f"100644 d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0 3\t{quoted}\n"
            "\n"
            "Auto-merging \u4e2d\u6587.txt\n"
            "CONFLICT (content): Merge conflict in \u4e2d\u6587.txt\n"
        )
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 1, report, ""),
        )
        assert mod._conflict_paths("a", "b") == ["\u4e2d\u6587.txt"]

    def test_the_dedupe_and_the_not_answered_answer_stay_this_tools(
        self, mod, monkeypatch
    ) -> None:
        """What this file adds on top of the sibling's reading, pinned separately.

        The sibling returns one entry per stage line - three for a content conflict -
        so the dedupe is this tool's; and an empty answer must stay `None` ("not
        answered") rather than collapse into the clean `[]` above it. Both are
        contract bits the sibling does not own, so delegating the reading must not
        quietly delegate them too.
        """
        repeated = (
            _TREE + "\n"
            "100644 f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0 1\tAgent.md\n"
            "100644 e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0 2\tAgent.md\n"
            "100644 d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0 3\tAgent.md\n"
        )
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 1, repeated, ""),
        )
        assert mod._conflict_paths("a", "b") == ["Agent.md"]

        blockless = _TREE + "\nCONFLICT (content): Merge conflict in Agent.md\n"
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 1, blockless, ""),
        )
        assert mod._conflict_paths("a", "b") is None, (
            "a conflict whose paths the report does not name is 'not answered', "
            "never the clean answer"
        )


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
                return subprocess.CompletedProcess(argv, 0, _TREE + "\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(mod, "_run", fake_run)
        monkeypatch.setattr(mod, "_fetch_head", lambda repo, n: f"refs/emrg-forecast/pr{n}")
        report = mod.forecast("FETCH_HEAD", [1], "argszero/emrg")

        assert report["base"] == "aaaa1111"
        # The point: the name never appears, only the resolved commit.
        assert measured == [("aaaa1111", "bbbb2222")]
        assert all("FETCH_HEAD" not in pair for pair in measured)

    def test_the_pairwise_question_is_also_asked_with_resolved_commits(
        self, mod, monkeypatch
    ) -> None:
        """The pairwise call is a *second* call site, and it was uncovered.

        Measured while reviewing #1153 (cycle cyc20260913-171619): replacing the
        pairwise call's first argument with the mutable ref name -

            paths = _conflict_paths(_fetch_head(repo, a), heads[b])

        which is the exact shape of the defect this module shipped with - left all
        19 tests green. The test above drives a single PR, so the pair loop never
        runs, and the static scan accepted any line that merely mentioned
        `heads[`. This drives a pair for real.
        """
        sha_by_ref = {
            "FETCH_HEAD^{commit}": "aaaa1111",
            "refs/emrg-forecast/pr1^{commit}": "bbbb2222",
            "refs/emrg-forecast/pr2^{commit}": "cccc3333",
        }
        measured: list[tuple[str, str]] = []

        def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(argv, 0, sha_by_ref[argv[-1]], "")
            if argv[:2] == ["git", "merge-tree"]:
                measured.append((argv[-2], argv[-1]))
                return subprocess.CompletedProcess(argv, 0, _TREE + "\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(mod, "_run", fake_run)
        monkeypatch.setattr(mod, "_fetch_head", lambda repo, n: f"refs/emrg-forecast/pr{n}")
        mod.forecast("FETCH_HEAD", [1, 2], "argszero/emrg")

        # Two questions against the base, then the pair: every argument a commit.
        assert measured == [
            ("aaaa1111", "bbbb2222"),
            ("aaaa1111", "cccc3333"),
            ("bbbb2222", "cccc3333"),
        ]
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
        """Static backstop: every `_conflict_paths` call is given a resolved SHA.

        By AST rather than by scanning the line's text. The scan this replaces
        required only `"base_sha" in line or "heads[" in line`, so a defective
        call whose *second* argument happened to be `heads[...]` satisfied it.
        Measured while reviewing #1153 (cycle cyc20260913-171619): with
        `_conflict_paths(_fetch_head(repo, a), heads[b])` in the file - the
        original defect, back at the pairwise call site - all 19 tests were green.

        The names are *derived*, not hard-coded: they are whatever the two
        assignments that call `_rev_parse` bind, so renaming `base_sha` is not a
        failure. That matters, because a rule keyed on a literal name is a rule
        that can be satisfied by an unrelated line (exactly how the scan it
        replaces went blind) or broken by a harmless rename.

        Both call sites are read out of the parsed source and the count is
        asserted to be at least the two the tool needs (a base question and a pair
        question), so deleting a call site cannot pass by having nothing left to
        inspect.
        """
        source = SCRIPT.read_text(encoding="utf-8")
        assert "_rev_parse(base)" in source, "the base must be resolved before use"
        assert "_rev_parse(_fetch_head(" in source, "heads must be resolved too"

        tree = ast.parse(source)

        def calls_to(name: str) -> list[ast.Call]:
            return [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name
            ]

        def is_rev_parse(node: ast.AST, argument: str) -> bool:
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_rev_parse"
                and any(
                    isinstance(arg, ast.Name) and arg.id == argument
                    for arg in node.args
                )
            )

        # `X = _rev_parse(base)` - the resolved base.
        base_names = [
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and is_rev_parse(node.value, "base")
            for target in node.targets
            if isinstance(target, ast.Name)
        ]
        # `Y = {n: _rev_parse(_fetch_head(repo, n)) for n in ...}` - the resolved heads.
        heads_names = [
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.DictComp)
            and isinstance(node.value.value, ast.Call)
            and isinstance(node.value.value.func, ast.Name)
            and node.value.value.func.id == "_rev_parse"
            for target in node.targets
            if isinstance(target, ast.Name)
        ]

        assert base_names, "no assignment resolves the base through _rev_parse"
        assert heads_names, "no assignment resolves the heads through _rev_parse"

        conflict_calls = calls_to("_conflict_paths")
        assert len(conflict_calls) >= 2, (
            "expected at least the base question and the pair question, found "
            f"{len(conflict_calls)} call(s) of _conflict_paths"
        )
        for call in conflict_calls:
            first = call.args[0] if call.args else None
            resolved = (
                isinstance(first, ast.Name)
                and first.id in base_names
                or isinstance(first, ast.Subscript)
                and isinstance(first.value, ast.Name)
                and first.value.id in heads_names
            )
            assert resolved, (
                "a merge question is asked with something other than a resolved "
                f"commit: {ast.unparse(call)}"
            )


class TestTheReportNamesWhatCollides():
    def test_a_pr_that_dirties_nothing_says_so(self, mod, monkeypatch) -> None:
        def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(argv, 0, "c" * 40, "")
            return subprocess.CompletedProcess(argv, 0, _TREE + "\n", "")

        monkeypatch.setattr(mod, "_run", fake_run)
        monkeypatch.setattr(mod, "_fetch_head", lambda repo, n: f"refs/emrg-forecast/pr{n}")
        report = mod.forecast("base", [1, 2], "argszero/emrg")
        assert all(entry["dirtied"] == [] for entry in report["prs"].values())
        assert report["base_conflicts"] == []

    def test_a_shared_conflicting_path_is_attributed_to_both(self, mod, monkeypatch) -> None:
        """A cascade is symmetric: each PR must list the other in `dirtied`."""
        out = (
            _TREE + "\n"
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

    def test_a_quoted_path_is_reported_as_the_real_name(self, tmp_path, monkeypatch):
        """The arm that tells a decoding reading from a pass-through one.

        Measured 2026-09-14 (`cyc20260914-104220`, git 2.50.1, scratch repo): with the
        default `core.quotePath=true` a non-ASCII path arrives C-quoted in the stage
        block - `"\\344\\270\\255\\346\\226\\207.txt"` for `中文.txt` - and the reading
        this file used to have returned that spelling, which `(repo / that).exists()`
        measured `False`. Because `forecast` prints the paths and attributes the
        cascade through them, the caller was told to resolve a file that is not there.
        Real git, because the quoting is git's behaviour rather than a fixture's.

        The same arm is why the reading is the sibling's: the shape alone cannot
        answer this one, only the shape plus the decoding.
        """
        name = "\u4e2d\u6587.txt"
        repo = tmp_path / "r"
        repo.mkdir()
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "t")
        (repo / name).write_text("base\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "base")

        self._git(repo, "checkout", "-q", "-b", "side")
        (repo / name).write_text("side\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "side rewrites it")

        self._git(repo, "checkout", "-q", "main")
        (repo / name).write_text("main\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "main rewrites it")

        monkeypatch.chdir(repo)
        mod = _load_module()
        paths = mod._conflict_paths("main", "side")
        assert paths == [name], paths
        assert (repo / paths[0]).exists(), "the report named something unopenable"

    def test_the_shapes_git_really_prints(self, tmp_path) -> None:
        """Measure the output this tool reads its verdict from, on real git.

        Every fixture above is a claim about what `merge-tree` prints, and a
        fixture is free to be a shape git never writes - which is how the previous
        version of this file modelled a clean merge as "exit 0, nothing printed"
        and a conflict as a stage block with no tree at all. This pins the real
        output instead: the merged tree's OID is the first line for a clean merge
        **and** for a conflict, and `--write-tree --quiet` (a documented flag that
        suppresses exactly that line) is the case where exit 0 and "nothing
        printed" arrive together - which is why the exit code cannot be the answer.
        """
        repo = tmp_path / "r"
        repo.mkdir()
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "t")
        (repo / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "base")
        base = self._git(repo, "rev-parse", "HEAD")

        # A clean merge, against itself - the same commit on both sides.
        proc = subprocess.run(
            ["git", "merge-tree", "--write-tree", base, base],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0
        assert re.fullmatch(r"[0-9a-f]{40}", proc.stdout.splitlines()[0])
        assert len(proc.stdout.splitlines()) == 1

        # The same clean merge with the tree name suppressed: exit 0, no output.
        quiet = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--quiet", base, base],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert (quiet.returncode, quiet.stdout) == (0, "")

        # A real conflict: exit 1, and the tree's name is still the first line.
        (repo / "f.txt").write_text("a\nOURS\nc\n", encoding="utf-8")
        self._git(repo, "commit", "-qam", "ours")
        self._git(repo, "checkout", "-q", "-b", "theirs", base)
        (repo / "f.txt").write_text("a\nTHEIRS\nc\n", encoding="utf-8")
        self._git(repo, "commit", "-qam", "theirs")
        conflict = subprocess.run(
            ["git", "merge-tree", "--write-tree", "theirs", "main"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert conflict.returncode == 1
        assert re.fullmatch(r"[0-9a-f]{40}", conflict.stdout.splitlines()[0])
        assert any("\tf.txt" in line for line in conflict.stdout.splitlines())


class TestARePushedHeadIsFetchedNotRejected:
    """A PR head routinely moves to a commit that is not its descendant.

    Every conflict resolution in this repo pushes a new head over the old one, so
    a second run of the tool against that PR finds a divergent head. The
    unforced refspec is rejected - and rejected *quietly*, because `--quiet`
    suppresses the diagnostic - leaving the stale ref in place, which is worse
    than an error: the run would measure the previous head as if it were current.
    """

    def test_the_refspec_is_forced(self, mod) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        assert (
            'f"+pull/{number}/head:{ref}"' in source
        ), "the PR-head refspec must be forced, or a re-pushed head is rejected"

    def test_a_failed_fetch_reports_the_process_diagnostic(self, mod, monkeypatch) -> None:
        """An empty stderr must not become an undiagnosable ``unknown error``.

        `--quiet` swallows the rejection text, which is how this shipped as
        "could not fetch PR #1151: unknown error" with nothing to act on.
        """
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(
                argv, 1, "", " ! [rejected]  pull/1/head -> refs/x  (non-fast-forward)"
            ),
        )
        with pytest.raises(RuntimeError, match="non-fast-forward"):
            mod._fetch_head("argszero/emrg", 1)

    def test_a_failed_fetch_falls_back_to_stdout(self, mod, monkeypatch) -> None:
        """Some git failures write to stdout; either stream is a real diagnostic."""
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 1, "fatal: could not read", ""),
        )
        with pytest.raises(RuntimeError, match="could not read"):
            mod._fetch_head("argszero/emrg", 1)

    def test_only_a_truly_silent_failure_says_unknown(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 1, "", ""),
        )
        with pytest.raises(RuntimeError, match="unknown error"):
            mod._fetch_head("argszero/emrg", 1)

    def test_a_real_re_pushed_head_is_fetched_twice(self, tmp_path, monkeypatch) -> None:
        """End-to-end on real git, driving the *helper itself* over real divergence.

        The mocked tests above cannot distinguish "the refspec is forced" from "the
        mock never modelled rejection" - and that gap is exactly the shipped bug.
        Here the origin exposes a real `refs/pull/1/head`, so `_fetch_head` runs
        unchanged. The head is then moved to a commit that does **not** descend from
        the first one (a sibling of it, as a rebase or a rewritten PR head produces):
        the unforced refspec is rejected and leaves the stale ref behind, while the
        tool must land on the true head.
        """
        origin = tmp_path / "origin"
        work = tmp_path / "work"
        origin.mkdir()

        def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        assert git(origin, "init", "-q", "-b", "main").returncode == 0
        git(origin, "config", "user.email", "t@example.com")
        git(origin, "config", "user.name", "t")
        (origin / "f.txt").write_text("base\n", encoding="utf-8")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "base")

        # Two siblings off the same root: neither descends from the other, which is
        # what a re-pushed (rebased) PR head looks like.
        git(origin, "checkout", "-q", "-b", "one")
        (origin / "a.txt").write_text("one\n", encoding="utf-8")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "head one")
        first_sha = git(origin, "rev-parse", "HEAD").stdout.strip()

        git(origin, "checkout", "-q", "-b", "two", "main")
        (origin / "b.txt").write_text("two\n", encoding="utf-8")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "head two")
        second_sha = git(origin, "rev-parse", "HEAD").stdout.strip()
        assert second_sha != first_sha
        assert (
            git(origin, "merge-base", "--is-ancestor", first_sha, second_sha).returncode
            != 0
        ), "the two heads must be divergent, or this test proves nothing"

        git(origin, "update-ref", "refs/pull/1/head", first_sha)

        assert git(tmp_path, "clone", "-q", str(origin), str(work)).returncode == 0
        git(work, "config", "user.email", "t@example.com")
        git(work, "config", "user.name", "t")

        monkeypatch.chdir(work)
        mod = _load_module()

        # Run 1: the helper fetches the PR head and returns the ref name it used.
        ref = mod._fetch_head("ignored", 1)
        assert git(work, "rev-parse", ref).stdout.strip() == first_sha

        # The PR head is re-pushed to the sibling commit.
        git(origin, "update-ref", "refs/pull/1/head", second_sha)

        # Run 2: the helper must land on the NEW head rather than leaving the stale
        # one - an unforced refspec fails here with rc 1 and keeps `first_sha`.
        ref2 = mod._fetch_head("ignored", 1)
        assert ref2 == ref
        assert git(work, "rev-parse", ref).stdout.strip() == second_sha, (
            "a re-pushed head must be fetched, not silently rejected - a stale ref "
            "would make the tool answer about the previous head"
        )


class TestTheBaseIsResolvedByItsFullName:
    """`--base origin/master` names two refs, and git prefers the local branch.

    Measured `cyc20260913-234157` in a scratch clone whose
    `refs/remotes/origin/master` sat at `5e45e3d` with a stray
    `refs/heads/origin/master` at `2f9c552`:

        check-merge-order.py 1196 --base origin/master
        base 2f9c552403f30882da7856db4f14702f4df508b9, 1 open PR(s), 0 of 0 pairs conflict

    That is the stray, reported under the caller's name: a *true* forecast about a
    base nobody asked for, and the whole ordering recommendation built on it. The
    printed SHA is the only part that exposes it, so nothing downstream could tell.

    The rule is the sibling's (`check-merge-pairs.py` documents the same incident
    from `cyc20260913-102231`); it is called here rather than copied, so a base
    name cannot come to mean two different things in two gates.
    """

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

    def _repo_with_a_stray(self, tmp_path: Path) -> tuple[Path, str, str]:
        """A repo where the name `origin/master` denotes two different commits.

        Real git, no network. Returns (repo, stray, tracking) - `stray` is what
        `refs/heads/origin/master` points at (what git would pick for the bare
        name), `tracking` what `refs/remotes/origin/master` holds (what the caller
        named). Git itself creates a local branch of that name when a fetch
        destination is written unqualified, so this is not a synthetic shape.
        """
        repo = tmp_path / "r"
        repo.mkdir()
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "t")
        (repo / "f.txt").write_text("one\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "one")
        tracking = self._git(repo, "rev-parse", "HEAD")

        self._git(repo, "update-ref", "refs/remotes/origin/master", tracking)
        (repo / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "two")
        stray = self._git(repo, "rev-parse", "HEAD")
        self._git(repo, "update-ref", "refs/heads/origin/master", stray)

        assert stray != tracking
        assert self._git(repo, "rev-parse", "origin/master") == stray, (
            "precondition: git resolves the bare name to the local branch - if this "
            "ever changes the defect is gone, and this test should go with it"
        )
        self._git(repo, "checkout", "-q", "main")
        return repo, stray, tracking

    def test_a_stray_local_branch_cannot_stand_in_for_the_base(
        self, mod, tmp_path, monkeypatch
    ) -> None:
        """The arm that fails before the fix: the measure is the ref that was named."""
        repo, stray, tracking = self._repo_with_a_stray(tmp_path)
        monkeypatch.chdir(repo)
        # This repo has no `origin` remote: the refresh is exercised for real in
        # `TestTheBaseIsRefreshedBeforeItIsRead`, and stubbed here so the question
        # under test stays the one this class asks (which commit the name denotes).
        monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: None)

        seen: dict[str, str] = {}

        def fake_forecast(base: str, numbers, repo_name: str) -> dict:
            seen["base"] = base
            seen["sha"] = mod._rev_parse(base)
            return {"base": seen["sha"], "prs": {}, "base_conflicts": []}

        monkeypatch.setattr(mod, "forecast", fake_forecast)
        rc = mod.main(["--base", "origin/master", "--json", "1"])

        assert rc == 0, rc
        assert seen["base"] == "refs/remotes/origin/master", seen
        assert seen["sha"] == tracking, (
            "the base measured must be the remote-tracking commit, not the stray"
        )
        assert seen["sha"] != stray

    def test_without_the_full_name_the_stray_is_what_gets_measured(
        self, mod, tmp_path, monkeypatch
    ) -> None:
        """The other half of the discrimination, driven through git itself.

        The same repo state, the base resolved the way the tool used to resolve it:
        `rev-parse origin/master` returns the stray. Taken together with the arm
        above, the pair proves the fix changes what is measured rather than merely
        the spelling of the name.
        """
        repo, stray, tracking = self._repo_with_a_stray(tmp_path)
        monkeypatch.chdir(repo)

        assert mod._rev_parse("origin/master") == stray
        assert mod._rev_parse("refs/remotes/origin/master") == tracking

    def test_a_base_that_denotes_only_a_local_branch_is_refused(
        self, mod, tmp_path, monkeypatch, capsys
    ) -> None:
        """Refused rather than measured - and nothing is measured before the refusal."""
        repo, _stray, _tracking = self._repo_with_a_stray(tmp_path)
        self._git(repo, "update-ref", "-d", "refs/remotes/origin/master")
        monkeypatch.chdir(repo)

        calls: list[tuple] = []
        refreshed: list[str] = []
        monkeypatch.setattr(mod, "forecast", lambda *a, **k: calls.append(a) or {})
        monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: refreshed.append(ref))
        rc = mod.main(["--base", "origin/master", "1"])

        assert rc == 2, rc
        assert not calls, "a refused base must not produce a forecast at all"
        assert refreshed == [], (
            "the refusal comes first: refreshing a name that denotes only a local "
            "branch would fetch the remote ref into existence and undo the refusal"
        )
        err = capsys.readouterr().err
        assert "refs/heads/origin/master" in err
        assert "refs/remotes/origin/master" in err

    def test_a_sha_a_tag_and_an_unambiguous_name_are_left_alone(self, mod, monkeypatch) -> None:
        """Only a remote-tracking spelling is rewritten; the rest are what was meant.

        A SHA is immutable by construction, `FETCH_HEAD` is not remote-tracking, and
        a plain name is the caller's own ref. Rewriting any of them (or asking git
        whether they exist) would invent a meaning the caller did not give.

        The *refresh* is called for every base - it is what decides that a SHA or a
        branch is not a remote ref - and it is the thing that must not do anything to
        them: only the remote-tracking spelling is refreshed, and it is refreshed here
        as a stubbed call, because a real one reaches the network.
        """
        seen: list[str] = []
        refreshed: list[str] = []
        monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: refreshed.append(ref))
        monkeypatch.setattr(
            mod,
            "forecast",
            lambda base, numbers, repo_name: seen.append(base)
            or {"base": "", "prs": {}, "base_conflicts": []},
        )

        for spelling in ("abc1234", "FETCH_HEAD", "v0.2.95", "refs/remotes/origin/master"):
            rc = mod.main(["--base", spelling, "--json", "1"])
            assert rc == 0, (spelling, rc)

        assert seen == ["abc1234", "FETCH_HEAD", "v0.2.95", "refs/remotes/origin/master"], seen
        assert refreshed == seen, (
            "the refresh is offered every explicit base and left to the sibling to "
            "decide which spellings it can act on"
        )

    def test_the_default_base_is_still_fetched_not_qualified(self, mod, monkeypatch) -> None:
        """No `--base`: the tool fetches master itself, and that path is untouched.

        The fix must not reach this path - `FETCH_HEAD` is not a remote-tracking
        name, so qualifying it is meaningless, and qualifying `None` before the
        fetch would crash the default invocation outright.
        """
        seen: list[str] = []
        refreshed: list[str] = []
        monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: refreshed.append(ref))
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv: subprocess.CompletedProcess(argv, 0, "", ""),
        )
        monkeypatch.setattr(
            mod,
            "forecast",
            lambda base, numbers, repo_name: seen.append(base)
            or {"base": "", "prs": {}, "base_conflicts": []},
        )

        rc = mod.main(["--json", "1"])

        assert rc == 0, rc
        assert seen == ["FETCH_HEAD"], seen
        assert refreshed == [], (
            "the default path fetches master itself; refreshing FETCH_HEAD or None "
            "would either touch a name the sibling does not own or crash outright"
        )


class TestTheBaseIsRefreshedBeforeItIsRead:
    """A ref that names the right thing can still hold the wrong commit.

    An explicit base used to be read at whatever moment this checkout last fetched,
    while every PR head below it is fetched as it is now - one question answered from
    two times. So the whole forecast, the base conflicts included, was measured
    against a tree the caller never named (`cyc20260914-014536`). The refresh is the
    sibling's `_refresh_base` (called, not copied), on the ref the caller named, after
    the refusal that protects a stray local branch and before anything is measured.
    """

    def test_the_short_name_never_reaches_the_refresh(
        self, mod, monkeypatch, capsys, tmp_path
    ) -> None:
        """`--base origin/master`: the refresh is called with the full name, not the short one."""
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(mod.seq, "_rev_parse", lambda ref: events.append(("read", ref)))

        def fake_forecast(base: str, numbers, repo_name: str) -> dict:
            # `forecast` is where the base is read, so record the read in the order it
            # actually happens rather than asserting on a stub that skips it.
            mod.seq._rev_parse(base)
            events.append(("forecast", base))
            return {"base": "", "prs": {}, "base_conflicts": []}

        monkeypatch.setattr(mod, "forecast", fake_forecast)
        remote = "refs/remotes/origin/master"
        monkeypatch.setattr(mod.seq, "_qualify_ref", lambda ref: remote)
        monkeypatch.setattr(mod.seq, "_refresh_base", lambda ref: events.append(("refresh", ref)))

        rc = mod.main(["--base", "origin/master", "--json", "1"])
        capsys.readouterr()

        assert rc == 0, rc
        assert ("refresh", "origin/master") not in events, (
            "the short spelling is ambiguous; the refresh must be given the name the "
            "caller's ref was resolved to, which is what the refusal above guarantees"
        )
        assert events == [
            ("refresh", remote),
            ("read", remote),
            ("forecast", remote),
        ], events

    def test_a_stale_remote_tracking_base_follows_the_remote_with_real_git(
        self, mod, monkeypatch, capsys, tmp_path
    ) -> None:
        """The defect's effect, with real git: the printed base is the remote's current tip.

        A hermetic clone whose `refs/remotes/origin/master` sits one commit behind the
        remote it was cloned from - the normal state of a checkout that has not
        fetched. The arm states the discriminating reading first (the commit the ref
        holds *is* the stale one), then runs `main` with real git for the refresh, the
        naming and the read, faking only the network-shaped parts (the PR heads), so
        the test neither reaches GitHub nor runs a pipeline. The commit dates are
        pinned, so both shas reproduce on every run rather than being a timestamp.
        """
        import os

        pinned = {
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        env = {**os.environ, **pinned}

        def git(cwd, *args: str) -> str:
            out = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
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
        # The PR heads are the network-shaped part: `forecast` fetches them through this
        # module's own `_fetch_head`, so that is the call that is faked.
        monkeypatch.setattr(mod, "_fetch_head", lambda repo_name, n: advanced)

        assert git(clone, "rev-parse", track) == stale
        # The discriminating reading before the fix: naming the ref is not refreshing
        # it, so at this point the tool would print - and measure everything against -
        # the stale commit.
        assert mod._rev_parse(mod.seq._qualify_ref("origin/master")) == stale

        rc = mod.main(["--base", "origin/master", "--json", "1"])
        out = capsys.readouterr().out

        assert rc == 0, out
        assert advanced in out, out
        assert stale not in out, "the stale base is what the refresh exists to prevent"
        assert git(clone, "rev-parse", track) == advanced, "the tracking ref was refreshed"
        assert git(clone, "rev-parse", "refs/heads/master") == stale, (
            "the refresh moves the remote-tracking ref only, never the local branch"
        )

    def test_a_base_that_cannot_be_refreshed_is_not_answered(
        self, mod, monkeypatch, capsys
    ) -> None:
        """A base that could not be verified is exit 2 - never measured from anyway.

        A fetch error (offline, no such branch) is this tool's own loud failure:
        answering below it would report an order for a question that was not asked
        about the tree the caller named.
        """
        forecasts: list[tuple] = []

        def boom(ref: str) -> None:
            raise mod.seq.MeasurementError(f"could not refresh {ref}: fetch failed")

        monkeypatch.setattr(mod.seq, "_refresh_base", boom)
        monkeypatch.setattr(
            mod, "forecast", lambda *a, **k: forecasts.append(a) or {"base": ""}
        )
        rc = mod.main(["--base", "origin/master", "1"])
        err = capsys.readouterr().err

        assert rc == 2, "an unverifiable base is not a pass"
        assert "could not measure" in err, err
        assert forecasts == [], "nothing may be forecast from a base that was not verified"
