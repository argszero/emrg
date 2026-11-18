"""`scripts/check-release-owed.py` — a tree declaring an untagged version owes a release.

Every case builds its own git repository under `tmp_path`: the guard runs `git` in the tree
it is given, so a fixture that reused this checkout would measure this checkout's tags
instead of the layout it means to test.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-release-owed.py"

#: Pinned so a host's own git identity never decides whether a fixture can commit.
_GIT_ID = (
    "-c", "user.email=test@example.invalid",
    "-c", "user.name=check-release-owed test",
    "-c", "commit.gpgsign=false",
)


def _load():
    spec = importlib.util.spec_from_file_location("check_release_owed", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(root: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", *_GIT_ID, *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"


def _repo(root: Path, declared: str | None, tags: list[str], *, commits_after_tag: int = 0) -> Path:
    """A repository whose tree declares `declared` and carries `tags`.

    `commits_after_tag` adds that many further commits on top, so the "how many commits
    are not in the release" reading has something to count.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init")
    (root / "emrg").mkdir()
    body = '# nothing\n' if declared is None else f'__version__ = "{declared}"\n'
    (root / "emrg" / "__init__.py").write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "first")
    for tag in tags:
        _git(root, "tag", tag)
    for index in range(commits_after_tag):
        (root / "note.txt").write_text(f"{index}\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", f"after {index}")
    return root


def _run(mod, root: Path, capsys) -> tuple[int, str]:
    """Call `check` the way `main` does, returning its code and everything it printed."""
    rc = mod.check(root)
    captured = capsys.readouterr()
    return rc, captured.out


def test_a_tag_without_the_v_publishes_nothing_so_a_release_is_still_owed(tmp_path, capsys):
    """`build-release.yml` triggers on `tags: ["v*"]`, so a `v`-less tag is not a release.

    Measured 2026-11-18 (`cyc20261118-104130`) on exactly this fixture: the guard answered
    **rc 0, "nothing is owed"** for a tree declaring `0.3.59` whose only tag was `0.3.59`.
    That tag builds nothing and publishes nothing, so the work had shipped to nobody while
    the guard - whose whole purpose is catching that - reported it as shipped. The tag passes
    the version regex but must not count as a release.
    """
    mod = _load()
    root = _repo(tmp_path / "vless", "0.3.59", ["0.3.59"])

    rc, out = _run(mod, root, capsys)

    assert rc == 1, f"a tag that triggers no build cannot satisfy the declaration:\n{out}"
    assert "A RELEASE IS OWED" in out, out
    assert "0.3.59" in out, out
    assert "no `v`" in out, (
        f"the near miss has to be named, or the reader cannot tell it from no tag at all:\n{out}")


def test_a_v_prefixed_tag_of_the_same_version_does_satisfy_it(tmp_path, capsys):
    """The other direction, so the fix above cannot be "always report owed"."""
    mod = _load()
    root = _repo(tmp_path / "vfull", "0.3.59", ["v0.3.59"])

    rc, out = _run(mod, root, capsys)

    assert rc == 0, out
    assert "nothing is owed" in out, out


def test_a_tree_older_than_the_newest_tag_is_behind_not_owed(tmp_path, capsys):
    """A checkout behind the release must not be told to cut one.

    Measured 2026-11-18 (`cyc20261118-102744`) on a fixture built exactly like this: a tree
    declaring `0.3.59` whose newest tag is `v0.3.60` was reported as `A RELEASE IS OWED ...
    1 commit(s) on HEAD are not in v0.3.60, so that work has merged` with the remedy
    `git tag -a v0.3.59` - three false statements in one verdict, because nothing had merged
    unreleased and that tag would publish an **older** version ahead of `v0.3.60`.

    The direction of the difference is the whole answer, so it is asserted both ways: the
    message must say the checkout is behind, must not offer the tag remedy, and the newer
    case must still say a release is owed.
    """
    mod = _load()
    root = tmp_path / "behind"
    root.mkdir()
    _git(root, "init")
    (root / "emrg").mkdir()
    decl = root / "emrg" / "__init__.py"
    decl.write_text('__version__ = "0.3.60"\n', encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "release 0.3.60")
    _git(root, "tag", "v0.3.60")
    # The declaration moves backwards on top of that release: a stale checkout's shape.
    decl.write_text('__version__ = "0.3.59"\n', encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "an older declaration on top")

    rc, out = _run(mod, root, capsys)

    assert rc == 1, out
    assert "BEHIND" in out, f"the verdict has to name the state it found: {out}"
    assert "A RELEASE IS OWED" not in out, (
        f"nothing merged unreleased here, so 'owed' is the wrong verdict: {out}")
    assert f"tag -a v0.3.59" not in out, (
        f"the remedy must not invite tagging an older version over v0.3.60: {out}")
    assert "v0.3.60" in out and "0.3.59" in out, (
        f"both versions are the evidence for the direction: {out}")


def test_the_tree_it_read_is_named_before_the_verdict(tmp_path, capsys):
    """The family's rule: a tree-reading guard names the tree it read, first.

    This is the assertion the classification in
    `tests/test_a_tree_reading_guard_names_its_tree.py` leans on when it puts this guard in
    the *names-its-tree-but-is-not-run-here* bucket instead of the bare loop - a claim about
    the naming that has to be a measurement rather than a promise.
    """
    mod = _load()
    root = _repo(tmp_path / "named", "0.3.59", ["v0.3.58"])

    rc, out = _run(mod, root, capsys)

    assert rc == 1, out
    assert out.splitlines()[0] == f"tree: {root}", (
        f"the first line is the reading's subject, and it must be the tree that was read: "
        f"{out.splitlines()[:2]}")


def test_a_pointable_guard_refuses_a_tree_it_cannot_read(tmp_path, capsys):
    """An empty directory and an absent path both answer rc 2 with a reason, never 0.

    The same two shapes the family's empty-tree leg puts to every pointable guard: a tree it
    read no file from is not "measured and clean".
    """
    mod = _load()
    empty = tmp_path / "empty"
    empty.mkdir()
    absent = tmp_path / "not-there"

    for target in (empty, absent):
        rc, out = _run(mod, target, capsys)
        assert rc == 2, f"{target}: rc={rc} is 'measured' for a tree no file was read from:\n{out}"
        assert "not measurable" in out, f"{target}: the code has to carry a reason:\n{out}"
        assert "OK:" not in out and "A RELEASE IS OWED" not in out, (
            f"{target}: it printed a verdict as well as refusing:\n{out}")


def test_a_declared_version_with_no_tag_owes_a_release(tmp_path, capsys):
    """The whole point: 0.3.59 declared, v0.3.58 the newest tag, commits in between."""
    mod = _load()
    root = _repo(tmp_path / "owed", "0.3.59", ["v0.3.58"], commits_after_tag=2)

    rc, out = _run(mod, root, capsys)

    assert rc == 1, out
    assert "A RELEASE IS OWED" in out, out
    assert "0.3.59" in out and "v0.3.58" in out, out
    assert "2 commit(s)" in out, (
        f"the count between the tag and HEAD is the fact that makes the gap real: {out}")


def test_a_declared_version_its_tag_carries_owes_nothing(tmp_path, capsys):
    """The other direction, without which a guard that always answers "owed" would pass."""
    mod = _load()
    root = _repo(tmp_path / "settled", "0.3.58", ["v0.3.57", "v0.3.58"])

    rc, out = _run(mod, root, capsys)

    assert rc == 0, out
    assert "nothing is owed" in out, out


def test_versions_compare_as_numbers_not_as_strings(tmp_path, capsys):
    """`0.3.10` is newer than `0.3.9`; string order says the opposite.

    A guard sorting tag names as strings picks `v0.3.9` as the newest, then reports a
    release owed for a version that is already tagged -- a false red on the exact release
    it was meant to confirm.
    """
    mod = _load()
    root = _repo(tmp_path / "numeric", "0.3.10", ["v0.3.9", "v0.3.10"])

    rc, out = _run(mod, root, capsys)

    assert rc == 0, out
    assert "v0.3.10" in out, out


def test_a_tag_that_is_not_version_shaped_is_reported_and_never_chosen(tmp_path, capsys):
    """A tag named `latest` must not become "the newest release"."""
    mod = _load()
    root = _repo(tmp_path / "skipped", "0.3.59", ["v0.3.58", "latest"])

    rc, out = _run(mod, root, capsys)

    assert rc == 1, out
    assert "v0.3.58" in out, out
    assert "not version-shaped" in out, (
        f"a skipped tag is a fact about the reading and has to be named: {out}")


def test_no_version_declaration_is_unmeasurable_never_a_pass(tmp_path, capsys):
    mod = _load()
    root = _repo(tmp_path / "no-declaration", None, ["v0.3.58"])

    rc, out = _run(mod, root, capsys)

    assert rc == 2, out
    assert "not measurable" in out, out


def test_an_unreadable_repository_is_unmeasurable_never_a_pass(tmp_path, capsys):
    """A directory that is not a work tree cannot answer the question at all."""
    mod = _load()
    root = tmp_path / "not-a-repo"
    root.mkdir()
    (root / "emrg").mkdir()
    (root / "emrg" / "__init__.py").write_text('__version__ = "0.3.59"\n', encoding="utf-8")

    rc, out = _run(mod, root, capsys)

    assert rc == 2, out
    assert "not measurable" in out, out


def test_no_tags_at_all_is_a_measurement_and_says_a_release_is_owed(tmp_path, capsys):
    """An empty tag list was read, and it answered: nothing carries the declaration.

    Exit 2 is for a question that could not be put. Here it was put and answered, so
    reporting "not measurable" would tell a fresh fork it has nothing to publish.
    """
    mod = _load()
    root = _repo(tmp_path / "no-tags", "0.3.59", [])

    rc, out = _run(mod, root, capsys)

    assert rc == 1, out
    assert "A RELEASE IS OWED" in out, out
