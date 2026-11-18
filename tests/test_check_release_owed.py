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


def _repo(
    root: Path,
    declared: str | None,
    tags: list[str],
    *,
    commits_after_tag: int = 0,
    lightweight: list[str] | None = None,
) -> Path:
    """A repository whose tree declares `declared` and carries `tags`.

    `tags` are created **annotated**, because that is what a release tag is: the workflow's
    `verify-tag` job rejects a lightweight tag, so a fixture building lightweight ones would
    be testing a shape that publishes nothing. `commits_after_tag` adds that many further
    commits on top, so the "how many commits are not in the release" reading has something
    to count. `lightweight` names tags to create the other way, for the test that asks what
    the guard does with them.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init")
    (root / "emrg").mkdir()
    body = '# nothing\n' if declared is None else f'__version__ = "{declared}"\n'
    (root / "emrg" / "__init__.py").write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "first")
    for tag in tags:
        _git(root, "tag", "-a", tag, "-m", f"emrg {tag}")
    for tag in lightweight or []:
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


def test_a_lightweight_tag_publishes_nothing_so_a_release_is_still_owed(tmp_path, capsys):
    """`verify-tag` rejects a lightweight tag, so it cannot carry a version.

    Measured 2026-11-18 (`cyc20261118-105553`): on a tree declaring `0.3.59` whose tag was a
    **lightweight** `v0.3.59`, the guard answered `rc 0, nothing is owed` - while
    `git cat-file -t` says `commit`, and `build-release.yml`'s `verify-tag` job exits 1
    unless the object type is `tag`, so the run stops before any platform builds. This
    repository has shipped one such tag (`v0.2.97`), produced by the bare
    `git tag v<x.y.z>` that `tests/test_release_tag_form.py` records.
    """
    mod = _load()
    root = _repo(tmp_path / "light", "0.3.59", [], lightweight=["v0.3.59"])

    rc, out = _run(mod, root, capsys)

    assert rc == 1, f"a lightweight tag publishes nothing, so a release is still owed:\n{out}"
    assert "A RELEASE IS OWED" in out, out
    assert "lightweight" in out, (
        f"the near miss has to be named, and by the word that says why it failed:\n{out}")


def test_an_annotated_tag_of_the_same_version_does_satisfy_it(tmp_path, capsys):
    """The other direction, so the fix above cannot be "always report owed"."""
    mod = _load()
    root = _repo(tmp_path / "annotated", "0.3.59", ["v0.3.59"])

    rc, out = _run(mod, root, capsys)

    assert rc == 0, out
    assert "nothing is owed" in out, out


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
    _git(root, "tag", "-a", "v0.3.60", "-m", "emrg v0.3.60")
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


# --- a stale local tag list is not a release owed ---------------------------------------
#
# A fixture under `tmp_path` *is* the whole repository, so its tag list cannot be stale and
# the remote is the seam. Each of the four answers is pinned, and the two that decide the
# verdict -- `present` and `absent` -- are pinned against each other: removing the
# distinction reddens one of them whichever way it is removed.


def _remote(monkeypatch, mod, state: str, detail: str = "the remote answered") -> None:
    """Pin what the remote says, without touching the network."""
    monkeypatch.setattr(mod, "remote_tag_state", lambda root, version: (state, detail))


def test_a_stale_tag_list_is_not_a_release_owed(tmp_path, capsys, monkeypatch):
    """The checkout's list stops one release short; the repository carries the tag.

    Measured on this host 2026-11-18 (`cyc20261118-113027`) on master `4111b6be`: the
    checkout's newest tag was `v0.3.58` while `v0.3.59` was published as Latest with 11
    assets, and the guard answered `A RELEASE IS OWED ... the newest tag is v0.3.58` with
    the remedy `git tag -a v0.3.59` and a push. The first command of that remedy fails,
    because the tag exists -- so the reader learns nothing about the state they are in,
    which is that their checkout is stale.
    """
    mod = _load()
    root = _repo(tmp_path / "stale", "0.3.59", ["v0.3.58"])
    _remote(monkeypatch, mod, "present", "the remote carries refs/tags/v0.3.59")

    rc, out = _run(mod, root, capsys)

    assert rc == 2, f"a list that cannot see the tag has not answered the question:\n{out}"
    assert "NOT MEASURABLE" in out, out
    assert "stale" in out, f"the state has to be named, not implied:\n{out}"
    assert "git fetch --tags" in out, f"the remedy is the fetch, not a tag:\n{out}"
    assert "A RELEASE IS OWED" not in out, (
        f"this exact sentence on a published release is the fault being fixed:\n{out}")


def test_a_version_the_remote_does_not_carry_is_still_owed(tmp_path, capsys, monkeypatch):
    """The case the tool exists for, and the fix must not soften it.

    Same local shape as the stale test above -- declared `0.3.59`, newest local tag
    `v0.3.58` -- with the one fact reversed: the remote answers that it carries no such
    tag. The two tests are the pair that makes the distinction do work.
    """
    mod = _load()
    root = _repo(tmp_path / "absent", "0.3.59", ["v0.3.58"], commits_after_tag=1)
    _remote(monkeypatch, mod, "absent", "the remote carries no refs/tags/v0.3.59")

    rc, out = _run(mod, root, capsys)

    assert rc == 1, out
    assert "A RELEASE IS OWED" in out, out
    assert "git tag -a v0.3.59" in out, f"the genuine case keeps the tag remedy:\n{out}"
    assert "1 commit(s)" in out, f"the count is what makes the gap real:\n{out}"


def test_a_remote_that_cannot_be_asked_leaves_the_local_verdict(tmp_path, capsys, monkeypatch):
    """Offline is not stale: the checkout's own reading stands, and says so.

    A screen that stopped working without a network would be replaced by its reader's
    memory of it, which is how the release chain lost nine commits in the first place.
    """
    mod = _load()
    root = _repo(tmp_path / "offline", "0.3.59", ["v0.3.58"])
    _remote(monkeypatch, mod, "unknown", "the tags of example/example could not be read")

    rc, out = _run(mod, root, capsys)

    assert rc == 1, out
    assert "A RELEASE IS OWED" in out, out
    assert "tag list alone" in out, (
        f"the reader must know the remote added nothing to this verdict:\n{out}")


def test_a_remote_lightweight_tag_still_owes_the_release(tmp_path, capsys, monkeypatch):
    """A tag on the remote is not enough -- it has to be one that publishes."""
    mod = _load()
    root = _repo(tmp_path / "light-remote", "0.3.59", ["v0.3.58"])
    _remote(
        monkeypatch,
        mod,
        "lightweight",
        "the remote carries refs/tags/v0.3.59 as a commit object, which `verify-tag` rejects",
    )

    rc, out = _run(mod, root, capsys)

    assert rc == 1, out
    assert "A RELEASE IS OWED" in out, out
    assert "verify-tag" in out, f"why it does not count is the near miss:\n{out}"


def test_an_empty_local_list_with_the_tag_on_the_remote_is_stale(
    tmp_path, capsys, monkeypatch
):
    """A clone made without tags is the same defect one step further out."""
    mod = _load()
    root = _repo(tmp_path / "no-tags-remote", "0.3.59", [])
    _remote(monkeypatch, mod, "present", "the remote carries refs/tags/v0.3.59")

    rc, out = _run(mod, root, capsys)

    assert rc == 2, out
    assert "NOT MEASURABLE" in out, out
    assert "A RELEASE IS OWED" not in out, out
    # Nothing of that name is held here, so "missing" is the true word and the fetch fills
    # the gap -- and the sentence that says otherwise has no business appearing.
    assert "missing v0.3.59" in out, f"no tag of that name is held, so it is missing:\n{out}"
    assert "git tag -d" not in out, f"there is no local tag here to delete:\n{out}"


def test_a_local_lightweight_tag_of_the_same_name_is_held_not_missing(
    tmp_path, capsys, monkeypatch
):
    """The checkout holds the name; it holds the wrong object under it.

    Measured 2026-11-18 in a checkout whose `v0.3.60` was a lightweight tag while the
    repository's was annotated (`git cat-file -t` answered `commit`; the API answered
    `object.type=tag`). The guard said the list "is missing v0.3.60" -- about a tag the
    reader could see -- and printed `git fetch --tags origin`, which git answers
    `! [rejected] ... (would clobber existing tag)` with rc 1, the local object left exactly
    where it was. So the printed cure was not soft, it was a no-op on the state it named.
    """
    mod = _load()
    root = _repo(tmp_path / "light-local", "0.3.59", [], lightweight=["v0.3.59"])
    _remote(
        monkeypatch,
        mod,
        "present",
        "the remote carries refs/tags/v0.3.59 as an annotated tag",
    )

    rc, out = _run(mod, root, capsys)

    assert rc == 2, f"two different objects of one name have answered nothing:\n{out}"
    assert "NOT MEASURABLE" in out, out
    assert "as a lightweight tag" in out, (
        f"the kind of the local object is the discriminator here:\n{out}")
    assert "missing v0.3.59" not in out, (
        f"that tag is in the list the reader is looking at:\n{out}")
    assert "git tag -d v0.3.59" in out, (
        f"the remedy must be able to move the local object, and a fetch cannot:\n{out}")


def test_the_held_name_is_reported_on_the_owed_path_too(tmp_path, capsys, monkeypatch):
    """The same rule at the other call site: an older annotated tag beside the held name.

    A checkout can stop short *and* hold the declared name as the wrong object -- the two
    shapes are independent, and a fix that reached only the empty-list branch would leave
    this one printing a fetch for a tag that is already there.
    """
    mod = _load()
    root = _repo(
        tmp_path / "light-local-beside-older",
        "0.3.59",
        ["v0.3.58"],
        commits_after_tag=1,
        lightweight=["v0.3.59"],
    )
    _remote(
        monkeypatch,
        mod,
        "present",
        "the remote carries refs/tags/v0.3.59 as an annotated tag",
    )

    rc, out = _run(mod, root, capsys)

    assert rc == 2, out
    assert "NOT MEASURABLE" in out, out
    assert "as a lightweight tag" in out, out
    assert "missing v0.3.59" not in out, out
    assert "git tag -d v0.3.59" in out, out
    assert "A RELEASE IS OWED" not in out, (
        f"both lists carry the name, so neither answer follows:\n{out}")


def test_a_settled_tree_never_asks_the_remote(tmp_path, capsys, monkeypatch):
    """The network is paid for only where the answer can change.

    A stale list can only ever **understate** the repository: it is missing tags, never
    holding ones the remote lacks. So it cannot turn a declared version into a tag that is
    there, and `OK` -- like "behind" and the near-miss buckets -- stays a reading of the
    checkout alone.
    """
    mod = _load()
    root = _repo(tmp_path / "settled-offline", "0.3.58", ["v0.3.58"])

    def _refuse(root_, version):
        raise AssertionError(
            "the remote must not be asked when the checkout's own list settles the question"
        )

    monkeypatch.setattr(mod, "remote_tag_state", _refuse)

    rc, out = _run(mod, root, capsys)

    assert rc == 0, out
    assert "nothing is owed" in out, out


def _release(monkeypatch, mod, state: str, detail: str = "the repository answered") -> None:
    """Pin what the repository says about a published release, without the network."""
    monkeypatch.setattr(mod, "published_release", lambda root, version: (state, detail))


class _Answer:
    """What `subprocess.run` returns, in the three fields `published_release` reads."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_a_lightweight_tag_that_already_published_owes_nothing(tmp_path, capsys, monkeypatch):
    """The state this repository is actually in, 79 times over.

    Measured 2026-11-18 (`cyc20261118-151657`) on `8a4b40d2`: every one of this checkout's
    79 lightweight version-shaped tags has a published release, because `verify-tag` is
    younger than they are. A guard that reads "lightweight" as "reached nobody" therefore
    told a reader to re-cut a release that hosts already have, which publishes the same
    version twice. The tag is still not one to push again -- the answer says so.
    """
    mod = _load()
    root = _repo(tmp_path / "light-published", "0.3.59", [], lightweight=["v0.3.59"])
    _remote(monkeypatch, mod, "lightweight", "the remote carries refs/tags/v0.3.59 as a commit")
    _release(monkeypatch, mod, "published", "v0.3.59 is a published release with 11 asset(s)")

    rc, out = _run(mod, root, capsys)

    assert rc == 0, f"a version that already published is not owed again:\n{out}"
    assert "nothing is owed" in out, out
    assert "11 asset(s)" in out, f"the reading it rests on has to be visible:\n{out}"
    assert "must not be pushed again" in out, f"the remedy is what not to do:\n{out}"


def test_a_lightweight_tag_with_no_release_still_owes_it(tmp_path, capsys, monkeypatch):
    """The other direction, and the one `verify-tag` was added for: no release, still owed."""
    mod = _load()
    root = _repo(tmp_path / "light-unpublished", "0.3.59", [], lightweight=["v0.3.59"])
    _remote(monkeypatch, mod, "lightweight", "the remote carries refs/tags/v0.3.59 as a commit")
    _release(monkeypatch, mod, "absent", "argszero/emrg carries no release for v0.3.59")

    rc, out = _run(mod, root, capsys)

    assert rc == 1, f"a lightweight tag with no release carries the version to nobody:\n{out}"
    assert "A RELEASE IS OWED" in out, out
    assert "no release for v0.3.59" in out, f"the absent release is the measurement:\n{out}"
    assert "annotated" in out or "`git tag -a" in out, f"the cure is an annotated tag:\n{out}"


def test_a_draft_release_has_not_reached_hosts(tmp_path, capsys, monkeypatch):
    """A draft is a release object that no host has downloaded, so the work is still owed."""
    mod = _load()
    root = _repo(tmp_path / "light-draft", "0.3.59", [], lightweight=["v0.3.59"])
    _remote(monkeypatch, mod, "lightweight", "the remote carries refs/tags/v0.3.59 as a commit")
    _release(
        monkeypatch, mod, "draft", "v0.3.59 exists as a DRAFT release with 3 asset(s)"
    )

    rc, out = _run(mod, root, capsys)

    assert rc == 1, out
    assert "A RELEASE IS OWED" in out, out
    assert "DRAFT" in out, f"a draft is named as why it does not count:\n{out}"


def test_the_lightweight_count_does_not_claim_they_published_nothing(tmp_path, capsys):
    """The sentence this file shipped, and the correction.

    It read "79 lightweight tag(s) that verify-tag rejects and so publish nothing", which was
    false 79 times out of 79. `verify-tag` governs a push made **now**, so that is what the
    line says; the past is not a fact this file measured per tag and must not be asserted.
    """
    mod = _load()
    root = _repo(tmp_path / "count", "0.3.59", ["v0.3.58"], lightweight=["v0.3.57"])

    rc, out = _run(mod, root, capsys)

    assert "publishes nothing today" in out, f"the forward rule is the one stated:\n{out}"
    assert "so publish nothing" not in out, (
        f"the past tense claim was measured false for every lightweight tag this repository "
        f"has, so it must not be printed:\n{out}"
    )


def test_published_release_reads_the_repository_both_ways(tmp_path, monkeypatch):
    """The lookup itself, driven at its own seam, one shape at a time.

    Stubbing `published_release` in the tests above keeps them about the verdict; this one
    keeps the verdict's *evidence* from being a fiction, by exercising the four answers the
    repository can give. A reading that only ever returned one of them would satisfy the
    tests above while meaning nothing.
    """
    mod = _load()
    root = tmp_path / "asks"
    root.mkdir()
    monkeypatch.setattr(mod, "_remote_of", lambda r: "argszero/emrg")

    cases = {
        "published": (
            _Answer(0, '{"tag_name": "v0.3.60", "draft": false, "assets": [{}, {}, {}]}'),
            "published",
        ),
        "draft": (
            _Answer(0, '{"tag_name": "v0.3.60", "draft": true, "assets": [{}]}'),
            "draft",
        ),
        "absent": (_Answer(1, "", "gh: Not Found (HTTP 404)"), "absent"),
        "refused": (_Answer(1, "", "gh: API rate limit exceeded (HTTP 403)"), "unknown"),
        "not json": (_Answer(0, "<html>not a release</html>"), "unknown"),
    }
    for name, (answer, expected) in cases.items():
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: answer)
        state, detail = mod.published_release(root, "0.3.60")
        assert state == expected, f"{name}: expected {expected}, got {state} ({detail})"
        assert detail, f"{name}: a reading with no detail cannot be acted on"


def test_a_repository_that_cannot_be_asked_leaves_the_verdict_alone(
    tmp_path, capsys, monkeypatch
):
    """No remote means no answer, and the local verdict stands rather than becoming a pass."""
    mod = _load()
    root = _repo(tmp_path / "light-offline", "0.3.59", [], lightweight=["v0.3.59"])
    _remote(monkeypatch, mod, "lightweight", "the remote carries refs/tags/v0.3.59 as a commit")
    _release(monkeypatch, mod, "unknown", "this checkout's `origin` names no GitHub repository")

    rc, out = _run(mod, root, capsys)

    assert rc == 1, f"an unaskable repository must not turn into `nothing is owed`:\n{out}"
    assert "A RELEASE IS OWED" in out, out
    assert "could not be asked" in out, out
