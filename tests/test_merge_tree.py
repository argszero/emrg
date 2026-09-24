"""Tests for `scripts/merge_tree.py` — git's merge-tree answer, read once.

Every gate in the merge-precheck family used to read `git merge-tree --write-tree`
itself. Five of them, three with a copy of the conflicted-path reading, each copy blind
to a different arm - which is why the same defect produced five PRs (#1210 … #1216)
instead of one repair. These tests pin the whole arm table in the one place the rule
now lives, in both directions (#455 lesson: never infer a discriminator from one state):

* what "answered" means - the **named tree**, never the exit code - and the three
  verdicts it yields (clean / conflict / unmeasured), including the two shapes that
  look like an answer and are not (`rc 0` printing nothing, `rc 1` printing nothing);
* what the conflicted paths are - the report's **stage block**, decoded out of git's
  C-quoting (the arms where the prose reading and the block reading disagree);
* the arm each *other* reading in this repo was blind to, so a future copy cannot pass
  by being right about the plain case;
* the pinned identity/date of the synthetic fold commits, which three tools used to
  declare separately.

The real-git tests build scratch repositories, because git's quoting is git's behaviour
and not a fixture's; the rest run against a fake runner.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "merge_tree.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("merge_tree_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _proc(argv, rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, rc, out, err)


_TREE = "9" * 40

#: A content conflict's stage block, as git writes it: one line per stage, then the
#: blank line, then prose. Measured 2026-09-14 (`cyc20260914-104220`), git 2.50.1.
_BLOCK = (
    "100644 " + "f" * 40 + " 1\tplain.txt\n"
    "100644 " + "e" * 40 + " 2\tplain.txt\n"
    "100644 " + "d" * 40 + " 3\tplain.txt\n"
)

#: Runners for the three verdicts plus the two unanswered shapes. Module-level, not
#: class attributes: a lambda stored on a class is a function, so `run(argv)` would be
#: handed `self` as the argv.
_clean = lambda argv: _proc(argv, 0, _TREE + "\n")  # noqa: E731
_conflict = lambda argv: _proc(  # noqa: E731
    argv, 1, _TREE + "\n" + _BLOCK + "\nCONFLICT (content): Merge conflict in x\n"
)
_unknown_code = lambda argv: _proc(argv, 3, _TREE + "\n")  # noqa: E731
_silent_zero = lambda argv: _proc(argv, 0, "")  # noqa: E731
_silent_one = lambda argv: _proc(argv, 1, "")  # noqa: E731

#: The refusal git prints for *both* of its two reasons: no common ancestor, and a
#: common ancestor cut out of this clone by a shallow boundary.
_refused = lambda argv: _proc(  # noqa: E731
    argv, 1, "", "fatal: refusing to merge unrelated histories"
)


class TestTheVerdictIsTheNamedTree:
    """`rc` decides nothing; the OID on line 1 decides everything."""

    def test_a_clean_merge_names_its_tree(self, mod) -> None:
        answer = mod.fold("a", "b", run=lambda argv: _proc(argv, 0, _TREE + "\n"))
        assert answer.verdict == "clean"
        assert answer.tree == _TREE
        assert answer.paths == ()

    def test_a_conflict_names_its_tree_and_its_paths(self, mod) -> None:
        report = _TREE + "\n" + _BLOCK + "\nCONFLICT (content): Merge conflict in plain.txt\n"
        answer = mod.fold("a", "b", run=lambda argv: _proc(argv, 1, report))
        assert answer.verdict == "conflict"
        assert answer.paths == ("plain.txt",) * 3

    def test_rc_zero_printing_nothing_is_not_a_clean_merge(self, mod) -> None:
        """`--quiet` on a clean merge, and a git that printed nothing at all.

        Reading `[]` - the clean answer - out of it hands the caller a tree nobody
        built, and `[]` is the one answer no downstream gate re-checks.
        """
        answer = mod.fold("a", "b", run=lambda argv: _proc(argv, 0, ""))
        assert answer.verdict == "unmeasured"
        assert answer.tree is None

    def test_rc_one_printing_nothing_is_not_a_conflict(self, mod) -> None:
        """A failure to merge the two *inputs* - `merge-tree <commit> <blob>`.

        Measured 2026-09-14 (`cyc20260914-050817`): exit 1 with empty stdout and a
        diagnostic on stderr. Reported as a conflict, it sends the caller off to
        resolve a collision that does not exist.
        """
        answer = mod.fold(
            "a", "b", run=lambda argv: _proc(argv, 1, "", "not something we can merge")
        )
        assert answer.verdict == "unmeasured"
        assert "not something we can merge" in answer.diagnosis

    def test_a_code_this_module_does_not_know_is_unmeasured(self, mod) -> None:
        """A tree named under an exit code nobody documented is not a verdict."""
        answer = mod.fold("a", "b", run=lambda argv: _proc(argv, 2, _TREE + "\n"))
        assert answer.tree == _TREE
        assert answer.verdict == "unmeasured"

    def test_both_object_formats_name_a_tree(self, mod) -> None:
        """SHA-256 clones exist; the shape of the answer must not depend on the clone."""
        answer = mod.fold("a", "b", run=lambda argv: _proc(argv, 0, "a" * 64 + "\n"))
        assert answer.verdict == "clean"


class TestThePathsComeFromTheStageBlockDecoded:
    """The reading: shape-matched, then unquoted - the union of what either half alone
    gets wrong."""

    def test_the_escapes_are_gits_own(self, mod) -> None:
        """The table is `quote_c_style`'s: the named escapes, the octal form git uses
        for a byte it will not write raw, and a path that was never quoted."""
        assert mod.unquote_path('"a\\tb\\nc\\\\d\\"e"') == 'a\tb\nc\\d"e'
        assert mod.unquote_path('"\\344\\270\\255\\346\\226\\207.txt"') == "中文.txt"
        assert mod.unquote_path('"\\000"') == "\x00"
        assert mod.unquote_path("plain.txt") == "plain.txt"
        # A trailing lone backslash is not an escape: it is kept as written.
        assert mod.unquote_path('"x\\"') == "x\\"

    def test_a_partial_octal_escape_cannot_crash_the_reading(self, mod) -> None:
        """`"\\3a"` is not a git spelling, and a reading that hits it must still speak.

        The sibling this module replaced did `int(body[i:i + 3], 8)`, which raises
        `ValueError` on `"\\3a"` - and an unhandled exception in a gate exits 1, the
        code that means "the tree this step lands is unhealthy". A crash reported as a
        finding is a finding about a tree nobody measured.
        """
        assert mod.unquote_path('"\\3a"') == "3a"

    def test_the_prose_after_the_block_is_not_a_path(self, mod) -> None:
        """The prose line for a TAB-named file carries a real tab, not a separator.

        Measured 2026-09-14: the "text after the first tab on any line" reading
        answered `"f\\ttab.txt", tab.txt` for this report - one name nobody can open,
        and one file that collides with nothing and does not exist.
        """
        # git writes the name as `"f\\ttab.txt"` - a C escape for the real TAB - while
        # the prose it writes below carries the TAB itself.
        report = (
            _TREE + "\n"
            '100644 ' + "f" * 40 + ' 1\t"f\\ttab.txt"\n'
            '100644 ' + "e" * 40 + ' 2\t"f\\ttab.txt"\n'
            '100644 ' + "d" * 40 + ' 3\t"f\\ttab.txt"\n'
            "\n"
            "Auto-merging f\ttab.txt\n"
            "CONFLICT (content): Merge conflict in f\ttab.txt\n"
        )
        answer = mod.fold("a", "b", run=lambda argv: _proc(argv, 1, report))
        assert set(answer.paths) == {"f\ttab.txt"}

    def test_a_modify_delete_conflict_names_its_path(self, mod) -> None:
        """The arm the prose reading cannot see: its message is not "Merge conflict in".

        Measured 2026-09-14 (arm G of the table in the module docstring): this report's
        prose is `CONFLICT (modify/delete): gone.txt deleted in side and modified in
        main.  Version main of gone.txt left in tree.`, and the prose reading returned
        that **whole sentence** as the path. The block names the path, and it writes two
        stage lines here rather than three - the count is per side that has a blob.
        """
        report = (
            _TREE + "\n"
            "100644 " + "f" * 40 + " 1\tgone.txt\n"
            "100644 " + "e" * 40 + " 2\tgone.txt\n"
            "\n"
            "CONFLICT (modify/delete): gone.txt deleted in side and modified in main."
            "  Version main of gone.txt left in tree.\n"
        )
        answer = mod.fold("a", "b", run=lambda argv: _proc(argv, 1, report))
        assert answer.paths == ("gone.txt", "gone.txt")

    def test_a_conflict_naming_no_path_is_still_a_conflict(self, mod) -> None:
        """The fold reports the facts; `paths == ()` is the *caller's* problem to read
        as "not answered" rather than as "clean" - which is why `verdict` exists."""
        report = _TREE + "\nCONFLICT (content): Merge conflict in plain.txt\n"
        answer = mod.fold("a", "b", run=lambda argv: _proc(argv, 1, report))
        assert answer.verdict == "conflict"
        assert answer.paths == ()


class TestAgainstRealGit:
    """The arms where a fixture cannot be authoritative: git's own quoting."""

    def _git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
        )

    def _conflict_on(self, tmp_path: Path, name: str) -> tuple[Path, str, str]:
        repo = tmp_path / "repo"
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
        self._git(repo, "commit", "-qm", "side")
        self._git(repo, "checkout", "-q", "main")
        (repo / name).write_text("main\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "main")
        return repo, "main", "side"

    #: The names git quotes, and - where Windows cannot hold the name at all - why.
    #: Skipped there rather than weakened or deleted: the property is "the decoded name
    #: is openable", and on Windows the file cannot be created for the reading to be
    #: asked about it (measured, test-windows run 34802892883 on PR #1217: `OSError
    #: [Errno 22]` for the TAB and for the quote, `FileNotFoundError` for the backslash,
    #: which is a *path separator* there and so is never part of a name). The decoding
    #: rule keeps a real-git arm on Windows - the non-ASCII case, which NTFS accepts -
    #: and a string-fixture arm on every platform
    #: (`TestThePathsComeFromTheStageBlockDecoded`).
    NAME_ARMS = (
        ("non-ascii", "\u4e2d\u6587.txt", None),
        (
            "tab",
            "f\ttab.txt",
            "Windows rejects a filename holding a control byte "
            "(CreateFile: OSError [Errno 22])",
        ),
        (
            "quote",
            'q"uote.txt',
            'Windows rejects a filename holding a double quote (OSError [Errno 22])',
        ),
        (
            "backslash",
            "back\\slash.txt",
            "on Windows a backslash separates path components, so it is never part of "
            "a name (the path it would address does not exist)",
        ),
        ("plain", "plain.txt", None),
    )

    NAMES = [
        pytest.param(
            name,
            id=arm,
            marks=[]
            if why is None
            else [pytest.mark.skipif(sys.platform == "win32", reason=why)],
        )
        for arm, name, why in NAME_ARMS
    ]

    @pytest.mark.parametrize("name", NAMES)
    def test_every_real_name_comes_back_openable(self, mod, tmp_path, name) -> None:
        """Every quoted spelling git writes must decode to a file that exists.

        This is the property the gates actually need - `forecast` prints these names
        and attributes a cascade through them, and a refusal that names a file nobody
        can open is a refusal nobody can act on. Measured on the pre-change reading:
        `'"\\344\\270\\255\\346\\226\\207.txt"'` for `中文.txt`, `exists? False`.
        """
        repo, a, b = self._conflict_on(tmp_path, name)

        def run(argv, cwd=None):
            return subprocess.run(
                argv,
                cwd=cwd or str(repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        answer = mod.fold(a, b, run=run)
        assert answer.verdict == "conflict"
        assert set(answer.paths) == {name}, answer.paths
        assert (repo / name).exists()

    def test_a_clean_merge_and_a_failure_to_merge_are_told_apart(self, mod, tmp_path) -> None:
        """Measured on real git: both print nothing, and only one is an answer."""
        repo, _, _ = self._conflict_on(tmp_path, "plain.txt")
        blob = self._git(repo, "rev-parse", "HEAD:plain.txt").stdout.strip()

        def run(argv, cwd=None):
            return subprocess.run(
                argv,
                cwd=cwd or str(repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        assert mod.fold("main", "main", run=run).verdict == "clean"
        assert mod.fold("main", blob, run=run).verdict == "unmeasured"

    def test_a_conflicting_merge_names_a_tree_full_of_markers(
        self, mod, tmp_path
    ) -> None:
        """Why only a *clean* merge is a tree - measured, not argued.

        `git merge-tree --write-tree` names a tree for a conflicting merge as well,
        and the file inside it is the conflict with its markers. So "a tree was
        named" is not "the merge happened": `merged_tree_sha` used to hand this tree
        back to a caller asking for "the clean merge's tree", and
        `check-merge-landing-diff.py` used to read it as the landing - a landing
        whose content nobody can commit.
        """
        repo, a, b = self._conflict_on(tmp_path, "plain.txt")

        def run(argv, cwd=None):
            return subprocess.run(
                argv,
                cwd=cwd or str(repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        answer = mod.fold(a, b, run=run)
        assert answer.verdict == "conflict"
        assert answer.tree is not None, "a conflict names a tree, which is the point"
        content = self._git(repo, "cat-file", "blob", f"{answer.tree}:plain.txt").stdout
        assert "<<<<<<<" in content, content

        with pytest.raises(mod.MeasurementError):
            mod.merged_tree_sha(a, b, run=run)

    def test_a_depth_one_clone_turns_one_merge_into_unrelated_histories(
        self, mod, tmp_path
    ) -> None:
        """The same two commits, two answers, one variable: the clone's boundary.

        Measured 2026-09-22 (git on this host, scratch repo): a depth-1 clone of a
        two-branch repository answers `fatal: refusing to merge unrelated histories`
        (exit 128, no tree) for branches whose common ancestor is one commit behind the
        boundary, and names a tree for the *same* two refs once `git fetch --unshallow`
        restores it. That is why the diagnosis asks: read alone, the refusal looks like a
        fact about the two commits, and on a real PR it cost a cycle of triage about
        PRs that were fine (the measurement is in `shallow_boundary`).

        The clone is a repository this test creates and cuts itself, so nothing outside
        `tmp_path` is read or written.
        """
        repo, a, b = self._conflict_on(tmp_path, "plain.txt")
        clone = tmp_path / "shallow"
        self._git(
            tmp_path, "clone", "-q", "--depth=1", "--branch", a, repo.as_uri(), str(clone)
        )
        self._git(
            clone,
            "fetch",
            "-q",
            "--depth=1",
            "origin",
            f"refs/heads/{b}:refs/remotes/origin/{b}",
        )

        def run(argv, cwd=None):
            return subprocess.run(
                argv,
                cwd=cwd or str(clone),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        before = mod.fold(f"origin/{a}", f"origin/{b}", run=run)
        assert before.verdict == "unmeasured"
        assert "unrelated histories" in before.diagnosis
        assert before.shallow is True
        assert "git fetch --unshallow" in before.diagnosis

        self._git(clone, "fetch", "-q", "--unshallow")
        after = mod.fold(f"origin/{a}", f"origin/{b}", run=run)
        assert after.verdict == "conflict", "the same merge, once the ancestor is back"
        assert after.tree is not None
        assert after.shallow is False, "the probe is never asked of an answered merge"


class TestOnlyACleanMergeIsATree:
    """The one mapping the owner keeps: verdict -> a tree, or `None`, or a raise.

    Two callers used to write it themselves and had drifted: `merged_tree_sha` read
    "a tree was named" as "here is the clean merge", and `check-merge-landing-diff.py`
    its own version of the same. A conflicting merge names a tree (measured above),
    so both were handing back a file full of `<<<<<<<`.
    """

    def test_a_clean_merge_is_its_tree_either_way(self, mod) -> None:
        assert mod.merged_tree("a", "b", run=_clean) == _TREE
        assert mod.merged_tree_sha("a", "b", run=_clean) == _TREE

    def test_a_conflict_is_no_tree(self, mod) -> None:
        """`None` is the conflict answer, and it is the *only* thing it means."""
        assert mod.merged_tree("a", "b", run=_conflict) is None
        with pytest.raises(mod.MeasurementError):
            mod.merged_tree_sha("a", "b", run=_conflict)

    def test_an_answer_nobody_understands_is_not_a_tree(self, mod) -> None:
        """`rc 3` naming a tree: the module's rule, and `merged_tree_sha` used to miss it.

        `merge_commit` refused this answer from the day it was written; the two
        shortcuts beside it did not, and the mutant that returns the tree here
        survives every other arm in this class.
        """
        with pytest.raises(mod.MeasurementError):
            mod.merged_tree("a", "b", run=_unknown_code)
        with pytest.raises(mod.MeasurementError):
            mod.merged_tree_sha("a", "b", run=_unknown_code)

    def test_an_unanswered_merge_is_not_a_tree(self, mod) -> None:
        """Both shapes with no named tree: `--quiet`'s rc 0, and a failure's rc 1."""
        for run in (_silent_zero, _silent_one):
            with pytest.raises(mod.MeasurementError):
                mod.merged_tree("a", "b", run=run)
            with pytest.raises(mod.MeasurementError):
                mod.merged_tree_sha("a", "b", run=run)

    def test_the_shortcut_is_the_same_mapping_as_the_commit(self, mod) -> None:
        """`merge_commit` builds on `merged_tree`, so they cannot diverge again."""
        assert mod.merge_commit("a", "b", run=_conflict) is None
        with pytest.raises(mod.MeasurementError):
            mod.merge_commit("a", "b", run=_unknown_code)


class TestTheSyntheticFold:
    """The commits the family's folds are named by: one identity, one date, in one place."""

    def test_a_conflicting_merge_has_no_commit(self, mod) -> None:
        report = _TREE + "\n" + _BLOCK + "\nCONFLICT (content): Merge conflict in x\n"
        assert mod.merge_commit("a", "b", run=lambda argv: _proc(argv, 1, report)) is None

    def test_an_unanswered_merge_raises_rather_than_conflicting(self, mod) -> None:
        """`None` is the *conflict* answer, so it must never stand for "no answer"."""
        with pytest.raises(mod.MeasurementError):
            mod.merge_commit("a", "b", run=lambda argv: _proc(argv, 1, ""))

    def test_an_unknown_code_naming_a_tree_is_not_a_merge(self, mod) -> None:
        """A named tree from a *code this module does not know* is not an answer.

        The tempting reading is "it named a tree, so commit it" - and that turns "I do
        not understand this answer" into "here is the merge", the reassuring direction
        the named-tree rule exists to refuse. Measured: the mutant that commits the
        tree here survives every other test in this file, which is why this arm is
        pinned in its own right (and it is why `check-merge-sequence.py` could drop its
        own private copy of the check and delegate).
        """
        seen: list[list[str]] = []
        report = _TREE + "\n" + _BLOCK + "\nCONFLICT (content): Merge conflict in x\n"

        def run(argv, env=None):
            seen.append(list(argv))
            return _proc(argv, 3, report)

        with pytest.raises(mod.MeasurementError):
            mod.merge_commit("a", "b", run=run)
        assert [argv[1] for argv in seen] == ["merge-tree"], seen

    def test_the_cwd_reaches_the_runner(self, mod) -> None:
        """`cwd` is which repository answers — the same hazard as the entry point.

        Both fold-based shortcuts carry it, because a gate that measures another
        checkout (`check-merge-tree-health.py`) must not be answered by whatever
        repository the process happens to stand in.
        """
        seen: list[tuple] = []

        def run(argv, cwd=None, env=None):
            seen.append((list(argv), cwd))
            return _proc(argv, 0, _TREE + ("\n" if argv[1:2] == ["merge-tree"] else "\n"))

        mod.merged_tree_sha("a", "b", run=run, cwd="/elsewhere")
        assert seen[-1][1] == "/elsewhere"
        seen.clear()
        mod.merge_commit("a" * 40, "b" * 40, run=run, cwd="/elsewhere")
        assert seen[0][1] == "/elsewhere", seen

    def test_the_commit_identity_and_date_are_pinned(self, mod) -> None:
        """Pinned in the environment, not read from the machine's git config.

        Measured in this family (`cyc20260913-200715`): with no ambient identity and
        `user.useConfigOnly = true`, `git commit-tree` refuses and the gate reports a
        false "could not measure" about a question git config has no bearing on.
        """
        env = mod.commit_env()
        assert env["GIT_AUTHOR_DATE"] == mod.PLAN_COMMIT_DATE
        assert env["GIT_COMMITTER_DATE"] == mod.PLAN_COMMIT_DATE
        assert mod.PLAN_COMMIT_DATE.endswith(" +0000")

    def test_the_message_defaults_to_what_a_merge_would_say(self, mod) -> None:
        seen: list[list[str]] = []

        def run(argv, env=None):
            seen.append(list(argv))
            if argv[:2] == ["git", "merge-tree"]:
                return _proc(argv, 0, _TREE + "\n")
            return _proc(argv, 0, "c" * 40 + "\n")

        sha = mod.merge_commit("a" * 40, "b" * 40, run=run)
        assert sha == "c" * 40
        assert "-m" in seen[-1]
        assert seen[-1][seen[-1].index("-m") + 1] == f"merge {'b' * 8} into {'a' * 8}"
        assert seen[-1][1:3] == ["commit-tree", _TREE]


#: A runner that answers the shallow probe with one of git's two spellings and passes
#: every other question to `runner`. The probe is the *only* call this module makes of
#: its own accord, so a test that wants the diagnosis must say what git would have said.
def _probe_says(shallow: bool, runner=_refused):
    def run(argv, cwd=None):
        if argv[1:2] == ["rev-parse"]:
            return _proc(argv, 0, "true\n" if shallow else "false\n")
        return runner(argv)

    return run


class TestAnUnansweredMergeAsksWhetherTheCloneIsShallow:
    """Git says one sentence for two reasons, and they have different owners.

    `fatal: refusing to merge unrelated histories` is what git answers both for two
    commits that really share no ancestor and for two that do, with the ancestor cut off
    by a shallow boundary (`git fetch --depth=1`, measured on real git above). Reported
    as one, the second sends a reader after the PRs; named, it is one command on the
    checkout. The probe costs a git call, so it is asked only where there is a refusal
    to explain - and an answer the module cannot read is `False`, never a claim.
    """

    def test_a_shallow_clone_is_named_with_its_repair(self, mod) -> None:
        answer = mod.fold("a", "b", run=_probe_says(True))
        assert answer.verdict == "unmeasured"
        assert answer.shallow is True
        assert "unrelated histories" in answer.diagnosis, "git's own words are kept"
        assert "is-shallow-repository" in answer.diagnosis
        assert "git fetch --unshallow" in answer.diagnosis, "the reading must carry its repair"

    def test_a_complete_clone_is_not_accused(self, mod) -> None:
        """The control: the same refusal, a clone that holds the whole history."""
        answer = mod.fold("a", "b", run=_probe_says(False))
        assert answer.shallow is False
        assert "unrelated histories" in answer.diagnosis
        assert "shallow" not in answer.diagnosis

    def test_an_answered_merge_never_asks(self, mod) -> None:
        """One git call for a clean merge, and the question is not silently doubled."""
        seen: list[list[str]] = []

        def run(argv, cwd=None):
            seen.append(list(argv))
            return _proc(argv, 0, _TREE + "\n")

        assert mod.fold("a", "b", run=run).verdict == "clean"
        assert [argv[1] for argv in seen] == ["merge-tree"], seen

    def test_a_conflict_is_answered_and_never_asks_either(self, mod) -> None:
        """A conflict names a tree, so it is an answer - the probe is not asked of it."""
        seen: list[list[str]] = []
        report = _TREE + "\n" + _BLOCK + "\nCONFLICT (content): Merge conflict in x\n"

        def run(argv, cwd=None):
            seen.append(list(argv))
            return _proc(argv, 1, report)

        assert mod.fold("a", "b", run=run).verdict == "conflict"
        assert [argv[1] for argv in seen] == ["merge-tree"], seen

    def test_the_probe_asks_a_repository_question_in_the_callers_repository(self, mod) -> None:
        """The argv and the cwd, because a probe answered by another checkout is a guess.

        The gates measure repositories they do not stand in
        (`check-merge-tree-health.py`), so the probe carries the caller's `cwd` the same
        way the merge question does.
        """
        seen: list[tuple] = []

        def run(argv, cwd=None):
            seen.append((list(argv), cwd))
            if argv[1:2] == ["rev-parse"]:
                return _proc(argv, 0, "true\n")
            return _proc(argv, 1, "")

        mod.fold("a", "b", run=run, cwd="/elsewhere")
        asked, cwd = seen[-1]
        assert asked == ["git", "rev-parse", "--is-shallow-repository"], asked
        assert cwd == "/elsewhere", cwd

    def test_a_probe_that_cannot_run_is_not_a_claim(self, mod) -> None:
        """A runner that cannot answer leaves git's words alone and raises nothing.

        An unreadable probe must not become "not shallow" *asserted* - the flag only ever
        adds a sentence - and it must certainly not turn into an exception, which the
        callers report as a verdict about a tree nobody measured.
        """

        def run(argv, cwd=None):
            if argv[1:2] == ["rev-parse"]:
                raise OSError("no git")
            return _proc(argv, 1, "", "fatal: refusing to merge unrelated histories")

        answer = mod.fold("a", "b", run=run)
        assert answer.shallow is False
        assert "shallow" not in answer.diagnosis
        assert "unrelated histories" in answer.diagnosis

    def test_the_reading_matches_this_hosts_git(self, mod, tmp_path) -> None:
        """`shallow_boundary` reads git's own answer, not a shape it invented.

        Asked of a repository this test creates in `tmp_path`, so the claim is measured
        against the git that will run it rather than against a fixture.
        """

        def run(argv, cwd=None):
            return subprocess.run(
                argv,
                cwd=cwd or str(tmp_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        assert subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=tmp_path, capture_output=True
        ).returncode == 0
        assert mod.shallow_boundary(run, cwd=str(tmp_path)) is False
        assert subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip() == "false", "the reading is git's, spelled the same way"
