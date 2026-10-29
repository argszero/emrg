"""Tests for scripts/check-staged-inverse.py - an index holding an earlier commit's tree.

The class the check exists for: `git status` reports a staged path as a staged
*modification* and never its direction, so an index left holding the content of an
older commit reads as a tidy list of staged files while a commit made from it
undoes everything that landed since. Measured on this host 2026-10-30: an index
whose `git write-tree` was `783fba29` - byte for byte the tree of `b0a6f7f`, a
commit already on master - against a HEAD of `79eb021` (`5fc083ac`), 11 paths and
115 deletions staged.

Both directions are pinned here, because a guard that answers `0` for the reason
it was written to catch is worse than no guard:

* an index holding an ancestor's tree is an **inverse** (`1`);
* an index holding content of its own - a staged file no commit has ever had - is
  **not** (`0`), and this is the arm that fails if the check ever degrades into
  "the index differs from HEAD", which would fire on every honest staging;
* an index that only *looks* clean because a ref could not be walked is
  **could not measure** (`2`), never `0`.

The last of those is the arm the whole design turns on. `git log --all` walks
every ref and exits `128` **after** printing whatever it reached, so a version
that read its stdout without reading its status would take a truncated history
for the whole one and report no inverse. Two refs of this checkout's 1028 were
in exactly that state, and `git log --all` answered 128 over 152 printed lines.
`test_a_failed_walk_does_not_contribute_what_it_printed` pins the discard, and
`test_a_broken_ref_with_no_match_refuses_to_answer` pins the refusal - the words
"no inverse" must not appear in it.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-staged-inverse.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_staged_inverse", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")


def _commit(repo: Path, relpath: str, body: str, message: str) -> str:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _two_commits(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo at its second commit, plus the first commit's sha and tree.

    The first commit's tree is the shape an index falls back to, so a test that
    needs an inverse stages it with `git read-tree <first>` - which writes the
    index only and leaves the working tree alone.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    first = _commit(repo, "a.txt", "one\n", "the first landing")
    _commit(repo, "a.txt", "two\n", "the second landing")
    return repo, first, _git(repo, "rev-parse", f"{first}^{{tree}}")


def _break_a_ref(repo: Path, name: str = "broken") -> str:
    """A ref whose object is not present, the state that makes `git log --all` exit 128.

    Written as a ref file directly: `git update-ref` refuses an object it cannot
    find, so the only way to reproduce this shape is to place the file, which is
    also how the two broken refs in this checkout came to exist.
    """
    ref = repo / ".git" / "refs" / "heads" / name
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text("0123456789012345678901234567890123456789\n", encoding="ascii")
    return f"refs/heads/{name}"


class TestTheTwoDirections:
    def test_a_clean_index_is_clean(self, mod, tmp_path, capsys) -> None:
        repo, _first, _tree = _two_commits(tmp_path)
        code = mod.main(["--repo", str(repo)])
        out = capsys.readouterr().out
        assert code == 0, out
        assert "nothing is staged" in out

    def test_an_index_holding_an_ancestors_tree_is_an_inverse(
        self, mod, tmp_path, capsys
    ) -> None:
        repo, first, tree = _two_commits(tmp_path)
        _git(repo, "read-tree", first)
        assert _git(repo, "write-tree") == tree, "the fixture did not stage the ancestor"

        code = mod.main(["--repo", str(repo)])
        out = capsys.readouterr().out

        assert code == 1, out
        assert "INVERSE" in out
        # The finding has to name a commit a reader recognises: the subject, not
        # only the sha, is what makes the line actionable.
        assert "the first landing" in out
        assert tree in out
        # And the paths it would undo, so the reader can see the size of it.
        assert "a.txt" in out

    def test_an_index_holding_its_own_content_is_not_an_inverse(
        self, mod, tmp_path, capsys
    ) -> None:
        """The arm that keeps the check from firing on every honest staging."""
        repo, _first, _tree = _two_commits(tmp_path)
        (repo / "new.txt").write_text("a file no commit has had\n", encoding="utf-8")
        _git(repo, "add", "new.txt")
        assert _git(repo, "write-tree") != _git(repo, "rev-parse", "HEAD^{tree}")

        code = mod.main(["--repo", str(repo)])
        out = capsys.readouterr().out

        assert code == 0, out
        assert "matches no commit's tree" in out
        assert "INVERSE" not in out


class TestTheRefusal:
    """An unmeasured silence is the one output the check may not produce."""

    def test_a_broken_ref_with_no_match_refuses_to_answer(
        self, mod, tmp_path, capsys
    ) -> None:
        repo, _first, _tree = _two_commits(tmp_path)
        broken = _break_a_ref(repo)
        (repo / "new.txt").write_text("of its own\n", encoding="utf-8")
        _git(repo, "add", "new.txt")

        code = mod.main(["--repo", str(repo)])
        captured = capsys.readouterr()
        out, err = captured.out, captured.err

        assert code == 2, out + err
        assert "could not measure" in err
        assert broken in err
        # The exact words that would turn a partial walk into a verdict.
        combined = out + err
        assert "matches no commit's tree" not in combined
        assert "INVERSE" not in combined

    def test_a_match_found_through_the_refs_that_answer_is_still_reported(
        self, mod, tmp_path, capsys
    ) -> None:
        """A broken ref is a caveat on a finding, not a reason to withhold it."""
        repo, first, _tree = _two_commits(tmp_path)
        broken = _break_a_ref(repo)
        _git(repo, "read-tree", first)

        code = mod.main(["--repo", str(repo)])
        captured = capsys.readouterr()

        assert code == 1, captured.out + captured.err
        assert "INVERSE" in captured.out
        assert broken in captured.err

    def test_a_failed_walk_does_not_contribute_what_it_printed(
        self, mod, tmp_path, monkeypatch, capsys
    ) -> None:
        """A failed `git log --all` has already printed history, and it looks whole.

        The fake walk fails *and* prints a pair whose tree is the index's own, naming
        a commit that does not exist. Trusting that stdout would report an inverse
        naming a phantom commit; discarding it leaves the per-ref walk, which finds
        no match and answers clean.
        """
        repo, _first, _tree = _two_commits(tmp_path)
        (repo / "new.txt").write_text("of its own\n", encoding="utf-8")
        _git(repo, "add", "new.txt")
        index_tree = _git(repo, "write-tree")
        phantom = "dead" * 10
        real_git = mod._git

        def fake_git(where, *args):
            if args[:2] == ("log", "--all"):
                return subprocess.CompletedProcess(
                    args, 128, stdout=f"{phantom} {index_tree}\n", stderr="fatal: bad object"
                )
            return real_git(where, *args)

        monkeypatch.setattr(mod, "_git", fake_git)
        code = mod.main(["--repo", str(repo)])
        captured = capsys.readouterr()

        assert code == 0, captured.out + captured.err
        assert phantom not in captured.out
        assert "INVERSE" not in captured.out

    def test_a_directory_that_is_not_a_working_tree_refuses(self, mod, tmp_path, capsys) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        code = mod.main(["--repo", str(plain)])
        err = capsys.readouterr().err
        assert code == 2, err
        assert "not a git working tree" in err


class TestWhatItNamesBeforeTheVerdict:
    def test_the_working_tree_is_named_before_anything_else(self, mod, tmp_path, capsys) -> None:
        """Every guard here names its subject; a verdict without one is unreadable."""
        repo, _first, _tree = _two_commits(tmp_path)
        mod.main(["--repo", str(repo)])
        out = capsys.readouterr().out
        assert out.splitlines()[0].startswith("tree: ")
        assert "repo" in out.splitlines()[0]

    def test_it_reads_the_named_tree_not_the_directory_the_caller_stands_in(
        self, mod, tmp_path, capsys, monkeypatch
    ) -> None:
        """`--repo` from outside the tree must still measure that tree."""
        repo, first, _tree = _two_commits(tmp_path)
        _git(repo, "read-tree", first)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        code = mod.main(["--repo", str(repo)])
        out = capsys.readouterr().out
        assert code == 1, out
        assert str(repo.resolve()) in out.splitlines()[0]
