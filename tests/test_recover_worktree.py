"""The reconstructible-dirt criterion and `scripts/recover-worktree.py` (#1237).

The guard's contract is "a cycle must not destroy work that exists nowhere else".
These pin the two halves: dirt that IS unique is refused (nothing touched), and
dirt that is NOT unique is converged **reversibly** — the scenario the measured
33-cycle deadlock was made of, built here as a real repository.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "recover-worktree.py"
DOC = Path(__file__).resolve().parent.parent / "DEVELOPMENT.md"
SCHEDULER = Path(__file__).resolve().parent.parent / "emrg" / "server" / "scheduler.py"

#: The one owner of the undo spelling (issue #1284), imported rather than re-spelled:
#: a test that copies the string it is checking cannot see the copy drift. Both
#: printers in the tree — the daemon's receipt and the manual tool's fallback — are
#: asserted against this function, so the only way to change the spelling is to
#: change it in one place, and the only thing left to assert *about* it is which
#: stash it names and that `--index` is in it.
from emrg.server.scheduler import recovery_recipe  # noqa: E402  (repo root is on sys.path)

#: The recovery bullet whose prose is the reader-facing undo recipe. Its opening
#: words are the anchor, not a line number: the section is re-wrapped and
#: re-numbered by edits that have nothing to do with the recipe, and the anchor is
#: asserted to appear exactly once so a renamed bullet fails instead of silently
#: measuring a different paragraph.
_RECIPE_BULLET = "- **stashes it when it is reconstructible**"


def _recovery_bullet() -> str:
    """That bullet as one paragraph, with its markdown line breaks collapsed."""
    text = DOC.read_text(encoding="utf-8")
    found = text.count(_RECIPE_BULLET)
    assert found == 1, (
        f"expected exactly one `{_RECIPE_BULLET}` bullet in {DOC.name}, found {found}"
        " — the anchor moved, so this test would be measuring a different paragraph"
    )
    rest = text.split(_RECIPE_BULLET, 1)[1]
    end = rest.find("\n- ")  # the bullet ends at the next top-level item
    assert end != -1, f"`{_RECIPE_BULLET}` is the last bullet in {DOC.name}"
    return " ".join(rest[:end].split())


#: The document's *second* statement of the undo, in the `-f` discussion rather than
#: in the recovery bullet: it tells a reader who has untracked-only dirt to reach for
#: `git stash -u`, and how to come back (`git stash apply --index`). Named by its
#: opening words, not a line number, and asserted to appear exactly once so a moved
#: paragraph fails instead of silently measuring a different one.
_RECIPE_CAVEAT = "`-f` is the right tool for tracked modifications only."


def _recovery_caveat() -> str:
    """That paragraph as one string, with its markdown line breaks collapsed."""
    text = DOC.read_text(encoding="utf-8")
    found = text.count(_RECIPE_CAVEAT)
    assert found == 1, (
        f"expected exactly one `{_RECIPE_CAVEAT}` paragraph in {DOC.name}, found "
        f"{found} — the anchor moved, so this test would be measuring a different "
        "paragraph"
    )
    rest = text.split(_RECIPE_CAVEAT, 1)[1]
    end = rest.find("\n\n")  # the paragraph ends at the blank line
    assert end != -1, f"`{_RECIPE_CAVEAT}` is the last paragraph in {DOC.name}"
    return " ".join((_RECIPE_CAVEAT + rest[:end]).split())


def _load():
    spec = importlib.util.spec_from_file_location("recover_worktree", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30,
        # The guard's rule, and the reason for it: a text-mode subprocess without
        # an explicit encoding decodes with the *host* locale codec, so it raises or
        # mojibakes on cp936/cp1252 for data that is valid UTF-8 (issue #1132).
        encoding="utf-8", errors="replace",
    )


def _status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain").stdout


def _new_repo(path: Path, content: str = "v1", name: str = "f.txt") -> None:
    """A one-commit repository. `-b master` because the criterion's upstream ref is
    the default branch, and a test whose branch name drifts would measure nothing.

    ``name`` exists because the criterion's defects are about *paths*: `git status`
    quotes some names and not others, so a geometry has to be buildable at the name
    that triggers the quoting (`add -A` rather than a literal name keeps that possible
    for a name beginning with a space or a dash).
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    (path / name).write_text(content, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")


def _with_upstream(tmp_path: Path, name: str = "f.txt"):
    """The deadlock's exact shape: HEAD behind, worktree carrying upstream's bytes.

    Measures `(repo, head_sha)`: the tree is dirty (one modified tracked file) and
    holds **no unique work at all** — the file's bytes are the upstream tip's blob.
    Of the 33 lost cycles, this was the state of the working tree.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "master", str(origin)],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    work = tmp_path / "work"
    _new_repo(work, "v1", name)
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "master")
    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    (work / name).write_text("v2", encoding="utf-8")
    _git(work, "commit", "-q", "-am", "v2")
    _git(work, "push", "-q", "origin", "master")
    _git(work, "reset", "-q", "--hard", head)      # HEAD back to v1 …
    (work / name).write_text("v2", encoding="utf-8")  # … worktree at upstream's v2
    assert _status(work).startswith(" M"), _status(work)
    return work, head


# ── the criterion itself, against real repositories ──────────────────────────


def test_dirt_carrying_upstreams_bytes_is_reconstructible(tmp_path):
    """False = nothing would be lost. This is the case that cost 33 cycles."""
    work, _ = _with_upstream(tmp_path)
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(work))
    assert loses is False, why
    assert "upstream" in why


def test_a_modification_found_nowhere_is_unique(tmp_path):
    """The real protection, in the other direction: nothing upstream holds these bytes."""
    _new_repo(tmp_path / "repo", "v1")
    repo = tmp_path / "repo"
    (repo / "f.txt").write_text("host's unreleased work", encoding="utf-8")
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True
    assert "f.txt" in why


def test_an_untracked_file_is_unique(tmp_path):
    """Untracked content exists in exactly one place, so it is never reconstructible."""
    _new_repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    (repo / "notes.md").write_text("only here", encoding="utf-8")
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True
    assert "notes.md" in why


def test_an_untracked_file_is_unique_even_when_upstream_has_those_bytes(tmp_path):
    """The clause that decides this case: "untracked == 0" is a precondition.

    Found by mutation (removing the `??` fast path changed no existing answer —
    the content comparison reaches the same verdict for a file git never tracked).
    That is not true here: when the path is *absent from HEAD* but present in the
    upstream tip, the content comparison finds a matching blob and would release
    the file, while the criterion says otherwise. An untracked file is content git
    has never tracked, so it is the host's by construction; releasing it because a
    copy was published once would discard a file the host created. Conservative by
    design, and the safe side: naming it unique only refuses.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "master", str(origin)],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    repo = tmp_path / "repo"
    _new_repo(repo, "v1")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "master")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "b.txt").write_text("published elsewhere", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "b")
    _git(repo, "push", "-q", "origin", "master")
    _git(repo, "reset", "-q", "--hard", head)   # b.txt now exists only upstream
    (repo / "b.txt").write_text("published elsewhere", encoding="utf-8")  # untracked again
    assert _status(repo).strip() == "?? b.txt", _status(repo)

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True, "untracked content is never reconstructible"
    assert "b.txt" in why


def test_an_untracked_copy_of_head_is_recoverable(tmp_path):
    """`git rm --cached f` on unchanged content: `D  f` *and* `?? f` for one path (#1277).

    The `??` line used to name the path unique before anything was hashed, so a tree
    that loses nothing stayed read-only and the git verbs that could converge it stayed
    refused. The bytes are `HEAD`'s own, under that same name — which is the whole
    distinction from `test_an_untracked_file_is_unique_even_when_upstream_has_those_bytes`
    above: content published once at this path but absent from `HEAD` is the host's file.

    This geometry also measures something about the reversal that the receipt's own
    wording does not cover, pinned below rather than left as a surprise: the named
    inverse *works* and *exits 1* on it. That is the undo/audit half, filed in #1284.
    """
    repo = tmp_path / "repo"
    _new_repo(repo, "unchanged")
    _git(repo, "rm", "-q", "--cached", "f.txt")
    assert _status(repo) == "D  f.txt\n?? f.txt\n", _status(repo)
    # The precondition that makes releasing the tier lossless, asserted rather than
    # assumed: the untracked bytes are byte-identical to `HEAD`'s blob for this path,
    # so they are reachable from a commit — the measurement #1277 reports, and the one
    # that separates this case from a file the host wrote.
    digest = _git(repo, "hash-object", "--", "f.txt").stdout.strip()
    assert digest == _git(repo, "rev-parse", "HEAD:f.txt").stdout.strip()
    assert _git(repo, "log", "--all", "--oneline", f"--find-object={digest}").stdout.strip()

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is False, why

    # And the action obeys that verdict, reversibly, with `HEAD` where it was.
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    status, detail = _load().TaskHandler._recover_dirty_tree_sync(str(repo))
    assert status == "recovered", f"{status}: {detail}"
    assert _status(repo) == ""
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head

    # The inverse the receipt names, run as named: the receipt names the entry
    # `git stash list` prints, and this repository has exactly one stash, so the list
    # prints `stash@{0}`. What this geometry does to it is *measured*, not assumed
    # (the residual is a finding, filed with the undo/audit half in #1284, not
    # something to wish away here): the state comes back byte for byte — deletion
    # still staged, file still untracked — while git exits **1** and warns `f.txt
    # already exists, no checkout`, having restored the untracked copy already by the
    # time it tries again. The stash is consequently *kept*, so the one-shot
    # spelling's evidence is not consumed either (measured: `pop --index` reports the
    # same failure and keeps the entry here).
    receipt = json.loads(
        (Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
         / "emrg-recovery-receipt.json").read_text(encoding="utf-8")
    )
    listing = _git(repo, "stash", "list").stdout
    index = next(
        ln.split(":")[0].split("{")[1].rstrip("}")
        for ln in listing.splitlines()
        if ln.endswith(receipt["stash_message"])
    )
    applied = _git(repo, "stash", "apply", "--index", f"stash@{{{index}}}")
    assert "already exists, no checkout" in applied.stderr, applied.stderr
    assert _status(repo) == "D  f.txt\n?? f.txt\n", _status(repo)
    assert _git(repo, "stash", "list").stdout.strip() != "", \
        "the reversal must not consume the stash it is named by"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head


def test_an_untracked_copy_of_head_with_other_bytes_is_unique(tmp_path):
    """The control for the clause above: the measurement is of the bytes, not the gesture.

    The same `git rm --cached` shape with edited content — the host's newer draft — exists
    in no commit, so the tier must stay refused and the draft must survive the attempt.
    """
    repo = tmp_path / "repo"
    _new_repo(repo, "unchanged")
    _git(repo, "rm", "-q", "--cached", "f.txt")
    (repo / "f.txt").write_text("the host's newer draft", encoding="utf-8")
    assert _status(repo) == "D  f.txt\n?? f.txt\n", _status(repo)

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True, why
    assert "f.txt" in why

    status, _detail = _load().TaskHandler._recover_dirty_tree_sync(str(repo))
    assert status == "refused", status
    assert (repo / "f.txt").read_text(encoding="utf-8") == "the host's newer draft"


def test_an_untracked_file_duplicating_another_path_is_unique(tmp_path):
    """Path-exactness: a blob with these bytes at *another* path is not evidence about this file.

    The tempting looser test — "some blob with these bytes is in a commit" — releases the
    tier here, over a file the host wrote; only `HEAD:<this path>` counts. Pinned because
    the temptation is concrete: the duplication is measurable with one `hash-object` and
    the shortcut would have passed every other test in this file.
    """
    repo = tmp_path / "repo"
    _new_repo(repo)
    (repo / "other.txt").write_text("v1", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-q", "-m", "other")
    assert (
        _git(repo, "rev-parse", "HEAD:other.txt").stdout.strip()
        == _git(repo, "hash-object", "--", "other.txt").stdout.strip()
    )
    (repo / "fresh.txt").write_text("v1", encoding="utf-8")   # same bytes, a new path
    assert _status(repo).strip() == "?? fresh.txt", _status(repo)

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True, why
    assert "fresh.txt" in why


def test_a_deletion_loses_nothing(tmp_path):
    """A deleted tracked file is restored by discarding, so it is not unique work."""
    _new_repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    (repo / "f.txt").unlink()
    assert _status(repo).startswith(" D"), _status(repo)
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is False, why


def test_a_staged_deletion_loses_nothing(tmp_path):
    """The same verdict on the *index* side of a deletion (`D `, not ` D`).

    Pinned because the clause that skips deletions was narrowed from `"D" in code`
    to the two sides that actually lose nothing. `D ` is the case that a careless
    narrowing drops: the worktree copy is gone as well, so a check that looked for a
    worktree blob would answer "could not be read to compare" and refuse a tree that
    loses nothing. Both spellings are deletions; both must stay recoverable.
    """
    _new_repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    _git(repo, "rm", "-q", "--cached", "f.txt")
    (repo / "f.txt").unlink()
    assert _status(repo) == "D  f.txt\n", _status(repo)
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is False, why


def test_staged_content_the_worktree_cannot_evidence_is_unique(tmp_path):
    """`MM`: staged, then the worktree copy reverted to HEAD (review of #1274).

    The worktree blob *is* HEAD's, so a measurement that reads only the worktree
    answers "recoverable" — while the staged blob is in no commit at all, and a plain
    `git stash pop` then drops it as an unreferenced object (measured: the index went
    back to HEAD's blob and the unique bytes became unreachable). The criterion must
    measure the index side too.
    """
    repo = tmp_path / "repo"
    _new_repo(repo)
    (repo / "f.txt").write_text("STAGED-ONLY-UNIQUE", encoding="utf-8")
    _git(repo, "add", "f.txt")
    (repo / "f.txt").write_text("v1", encoding="utf-8")   # back to HEAD's own bytes
    assert _status(repo) == "MM f.txt\n", _status(repo)

    staged = _git(repo, "rev-parse", ":f.txt").stdout.strip()
    reachable = _git(repo, "log", "--all", "--oneline", f"--find-object={staged}")
    assert reachable.stdout.strip() == "", "precondition: the staged blob is in no commit"

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True, why
    assert "staged" in why, why

    # And the action obeys that verdict: nothing may be moved aside.
    status, detail = _load().TaskHandler._recover_dirty_tree_sync(str(repo))
    assert status == "refused", f"{status}: {detail}"
    assert _git(repo, "stash", "list").stdout.strip() == ""
    assert _git(repo, "rev-parse", ":f.txt").stdout.strip() == staged, \
        "the staged blob must still be in the index"


def test_staged_content_with_no_worktree_copy_is_unique(tmp_path):
    """`MD`: staged, then the worktree copy removed — the same hole, other spelling.

    The worktree cannot evidence the index here for a different reason (there is no
    worktree blob at all), which is why the deletion clause had to be narrowed rather
    than left to skip anything containing a `D`.
    """
    repo = tmp_path / "repo"
    _new_repo(repo)
    (repo / "f.txt").write_text("STAGED-ONLY-UNIQUE", encoding="utf-8")
    _git(repo, "add", "f.txt")
    (repo / "f.txt").unlink()
    assert _status(repo) == "MD f.txt\n", _status(repo)

    staged = _git(repo, "rev-parse", ":f.txt").stdout.strip()
    assert _git(repo, "log", "--all", "--oneline",
                f"--find-object={staged}").stdout.strip() == ""

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True, why
    assert "staged" in why, why


def test_staged_content_upstream_already_holds_is_recoverable(tmp_path):
    """The other direction: the index clause must not refuse published content.

    Both states carry an index blob that equals the **upstream tip's** blob for that
    path, so discarding loses nothing (the bytes are in a commit anyone can reach).
    A clause that refused every staged change would trade the #1274 hole for the
    over-block class #1273 tracks — the two are not interchangeable.
    """
    for kind in ("mm", "md"):
        work, _head = _with_upstream(tmp_path / kind)
        published = _git(work, "rev-parse", "origin/master:f.txt").stdout.strip()
        # Stage upstream's own blob for that path (HEAD still holds v1's).
        staged = _git(work, "update-index", "--cacheinfo", f"100644,{published},f.txt")
        assert staged.returncode == 0, staged.stderr
        if kind == "md":
            (work / "f.txt").unlink()
        else:
            # Back to HEAD's bytes, so the worktree cannot evidence the index either —
            # the same `MM` geometry as the unique case above, differing only in
            # whether a commit already holds the staged blob.
            (work / "f.txt").write_text(
                _git(work, "show", "HEAD:f.txt").stdout, encoding="utf-8"
            )
        assert _status(work) == f"{'MM' if kind == 'mm' else 'MD'} f.txt\n", _status(work)

        loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(work))
        assert loses is False, f"{kind}: {why}"


# ── the path, not git's rendering of it ──────────────────────────────────────


@pytest.mark.parametrize("name", ["a b.txt", "文档.txt", " leading.txt"])
def test_a_modification_at_a_quoted_path_is_recoverable(tmp_path, name):
    """`git status --porcelain` *quotes* these names, and the quotes are not the path.

    The criterion took its path from that rendering, so every comparison it makes
    (`hash-object -- <path>`, `HEAD:<path>`, `<upstream>:<path>`) was asked about a
    name containing literal quote characters, could not resolve, and the walk answered
    *unique*. Measured on the merged tree before this test: the ` M` geometry whose
    bytes are the upstream tip's — the 33-cycle deadlock shape — was **refused** for
    `a b.txt` and for `文档.txt`, i.e. the guard re-imposed the deadlock on any host
    whose filenames are not all ASCII.

    The three names cover the three ways to need quoting: a space, a byte > 0x7f
    (octal-escaped under the default `core.quotePath=true`), and a *leading* space,
    which additionally pins that the path is taken verbatim rather than stripped.
    """
    work, head = _with_upstream(tmp_path, name)
    assert '"' in _status(work), f"precondition: git quotes this name: {_status(work)!r}"

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(work))
    assert loses is False, f"{name!r}: {why}"

    status, detail = _load().TaskHandler._recover_dirty_tree_sync(str(work))
    assert status == "recovered", f"{status}: {detail}"
    assert _status(work) == ""
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head, "HEAD must not move"
    # Converging means the tree is at HEAD again, so the file *is* v1 now: the v2 bytes
    # live in the upstream tip's commit (that is why releasing this tree loses nothing)
    # and in the stash. Both spellings are asserted rather than assumed.
    assert (work / name).read_text(encoding="utf-8") == "v1"
    _git(work, "stash", "pop")
    assert (work / name).read_text(encoding="utf-8") == "v2"


def test_a_modification_at_a_quoted_path_holding_unique_work_is_unique(tmp_path):
    """The other direction: reading the real path must not become a way to release work.

    With the path finally resolvable, the content comparison *can* answer, and for
    content that is in no commit it must still refuse — the clause this test guards is
    the same one the fix touches.
    """
    name = "a b.txt"
    repo = tmp_path / "repo"
    _new_repo(repo, "v1", name)
    (repo / name).write_text("the host's unreleased work", encoding="utf-8")

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True, why
    assert name in why

    status, _detail = _load().TaskHandler._recover_dirty_tree_sync(str(repo))
    assert status == "refused", status
    assert (repo / name).read_text(encoding="utf-8") == "the host's unreleased work"


def test_an_untracked_file_at_a_quoted_path_names_its_own_path(tmp_path):
    """The reported reason has to name the path, not the rendering.

    On the pre-fix tree this reason was `"host notes.md" exists only in this checkout`
    — the message a human reads when the guard refuses their cycle named a filename
    with quotes in it, which is not a file they can go and look at.
    """
    repo = tmp_path / "repo"
    _new_repo(repo)
    (repo / "host notes.md").write_text("only here", encoding="utf-8")

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True, why
    # Exact, not a substring: on the pre-fix tree the reason was
    # `"host notes.md" exists only in this checkout` — a filename with quotes in it,
    # which is not a file the host can go and look at.
    assert why == "host notes.md exists only in this checkout", why


def test_a_rename_is_unique_and_its_origin_is_not_walked(tmp_path):
    """`R` spends **two** NUL fields in `-z` mode; only the first is an entry.

    Walking the origin field as an entry reads its first two characters as a status,
    which lands it in the staged clause and reports a filename nobody has — measured by
    mutation: without the consumption this reason becomes `ig name.txt is staged with
    content that is in neither HEAD nor the upstream tip`. The assertion on the origin
    path's absence is therefore the discriminator, not the verdict (which is *unique*
    either way).
    """
    repo = tmp_path / "repo"
    _new_repo(repo, "v1", "orig name.txt")
    _git(repo, "mv", "orig name.txt", "moved name.txt")
    assert _status(repo).startswith("R"), _status(repo)

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True
    # Exact, and that is what carries the evidence: on the pre-fix tree this reason is
    # the v1 rendering (`"orig name.txt" -> "moved name.txt" was renamed`), and with the
    # origin field walked as an entry it gains a **second** clause naming `ig name.txt`
    # (the first two characters eaten as a status). One equality pins both.
    assert why == "moved name.txt was renamed", why


def test_every_entry_is_walked_when_one_of_them_is_quoted(tmp_path):
    """Both entries must be measured, which is what a NUL-field parse can get wrong.

    A parse that consumes one field too many per entry drops the second one — and a
    dropped entry is not a *refusal*, it is content the walk never looked at, so the
    verdict here would flip from unique to recoverable. (This is the failure the
    origin-field rule above is narrow against; the geometry keeps one entry that is
    already upstream's and one that is unique, so the unique one has to govern.)
    """
    root = tmp_path / "both"
    work, _head = _with_upstream(root, "plain.txt")
    (work / "host notes.md").write_text("only here", encoding="utf-8")
    assert _status(work).count("\n") == 2, _status(work)

    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(work))
    assert loses is True, why
    assert "host notes.md" in why, why
    assert _load().TaskHandler._recover_dirty_tree_sync(str(work))[0] == "refused"


def test_a_commit_only_on_this_branch_is_unique(tmp_path):
    """A branch reset would orphan it: dirty *and* ahead is never reconstructible."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "master", str(origin)],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    repo = tmp_path / "repo"
    _new_repo(repo)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "master")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "local only")
    (repo / "f.txt").write_text("v2", encoding="utf-8")
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(repo))
    assert loses is True
    assert "only on this branch" in why


def test_a_clean_tree_is_not_dirt(tmp_path):
    _new_repo(tmp_path / "repo")
    loses, why = _load().TaskHandler._dirty_tree_would_lose_work_sync(str(tmp_path / "repo"))
    assert loses is False
    assert "clean" in why


# ── the tool: refuse, converge, stay reversible ──────────────────────────────


def test_the_tool_refuses_unique_work_and_touches_nothing(tmp_path, capsys):
    """The refusal is the guard working: the file must survive the attempt."""
    repo = tmp_path / "repo"
    _new_repo(repo)
    (repo / "notes.md").write_text("only here", encoding="utf-8")

    assert _load().recover(repo, apply=True) == 1
    out = capsys.readouterr().out
    assert "refused" in out
    assert (repo / "notes.md").read_text(encoding="utf-8") == "only here"
    assert _git(repo, "stash", "list").stdout.strip() == ""
    assert not (Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
                / "emrg-recovery-receipt.json").exists()


def test_the_tool_converges_reconstructible_dirt_reversibly(tmp_path, capsys):
    """The deadlock's state, repaired: clean tree, HEAD unmoved, work recoverable."""
    work, head = _with_upstream(tmp_path)

    assert _load().recover(work, apply=False) == 0
    assert "recoverable" in capsys.readouterr().out
    assert _status(work).startswith(" M"), "a dry run must not change the tree"

    assert _load().recover(work, apply=True) == 0
    out = capsys.readouterr().out
    assert "recovered" in out and "reversible" in out
    assert _status(work).strip() == "", "the tool's whole job is a clean tree"
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head, "HEAD must not move"

    # Reversibility is the reason this action is allowed at all: every byte is
    # still there, one command away.
    _git(work, "stash", "pop")
    assert (work / "f.txt").read_text(encoding="utf-8") == "v2"


def test_the_tool_writes_a_receipt_of_what_it_moved(tmp_path):
    """The structural guard's contract: every release of a safety rule is a receipt."""
    work, head = _with_upstream(tmp_path)

    assert _load().recover(work, apply=True) == 0
    git_dir = Path(_git(work, "rev-parse", "--absolute-git-dir").stdout.strip())
    receipt = json.loads(
        (git_dir / "emrg-recovery-receipt.json").read_text(encoding="utf-8")
    )
    # The receipt must not re-dirty the tree it just cleaned — measured while
    # writing this tool, when the receipt lived beside it.
    assert _status(work).strip() == "", "the receipt itself must not be dirt"

    assert receipt["repo"] == str(work)
    assert receipt["head_before"] == head
    assert receipt["head_after"] == head, "the receipt must show HEAD did not move"
    assert any(line.startswith(" M") for line in receipt["status_before"])
    assert receipt["status_after"] == []
    assert "stash" in receipt["action"]
    # The spelled route is the one owner's, not a copy of it: this asserts identity
    # with the shared recipe, so a receipt that drifted from the function (or a
    # function that drifted from the receipt) fails here rather than in a reader's
    # shell. The substrings below only say *what* the owner has to contain.
    assert receipt["reversible_with"] == recovery_recipe(receipt["stash_message"]), (
        "the receipt must carry the one owner's recipe for *this* stash"
    )
    # The route must name *this* stash: a host may already have stashes, so a bare
    # `git stash pop` is only correct until the next one is made (measured on the
    # authoring workspace, which held an unrelated `stash@{0}` when this was written).
    assert receipt["stash_message"] in receipt["reversible_with"]
    # A stash carries the index side as well, and a plain pop does not restore it:
    # measured on an index-only change, `stash pop` printed "Already up to date.",
    # dropped the stash and left the index at HEAD's blob (#1274 review). The
    # documented inverse must therefore be the `--index` spelling.
    assert "--index" in receipt["reversible_with"]
    assert "upstream" in receipt["reason"]


def test_the_tool_says_when_the_receipt_could_not_be_written(tmp_path, capsys):
    """Issue #1284: the tool's `receipt:` line is a claim about a *file*, so read the file.

    `_receipt_path` computes where a receipt *would* be written, and the branch for a
    receipt that could not be written was therefore unreachable: with the path
    pre-created as a directory (`open(..., "w")` raises `OSError`) the tool printed
    `receipt: <path>` for a file that does not exist. Same input, twice, so the line is
    a discriminator rather than a constant.
    """
    work, _head = _with_upstream(tmp_path)
    git_dir = Path(_git(work, "rev-parse", "--absolute-git-dir").stdout.strip())
    target = git_dir / "emrg-recovery-receipt.json"
    target.mkdir()

    assert _load().recover(work, apply=True) == 0
    out = capsys.readouterr().out
    assert "recovered" in out, out
    assert "receipt: could not be written" in out, out
    assert str(target) not in out, "a path is not a receipt"
    assert not target.is_file()
    # The convergence itself still happened, and the stash is still the record.
    assert _status(work).strip() == ""
    assert _git(work, "stash", "list").stdout.strip() != ""

    control_root = tmp_path / "control"
    control_root.mkdir()
    control, _head2 = _with_upstream(control_root)
    assert _load().recover(control, apply=True) == 0
    out = capsys.readouterr().out
    assert "receipt: could not be written" not in out, out
    assert f"receipt: {Path(_git(control, 'rev-parse', '--absolute-git-dir').stdout.strip()) / 'emrg-recovery-receipt.json'}" in out, out


def test_the_fallback_recipe_is_the_owners_recipe_not_a_paraphrase(tmp_path, capsys):
    """Issue #1284, the branch a receipt-less recovery prints: one owner, not three.

    The tool prints its `reversible:` line from the receipt when it has this run's
    receipt, and it used to write the spelling **by hand** when it did not — the exact
    branch a reader lands on when the receipt could not be read. Two copies of a
    spelling is how the wrong one gets printed, and this pair did disagree once (the
    receipt said `apply --index`, this line said a bare `git stash pop`), so the
    fallback now calls the same function the receipt is built from. Measured gap this
    closes: the branch was *reached* by the test above but its text was named in no
    test at all — grepping the suite for the fallback's wording found nothing, so a
    re-worded fallback was a silent edit.

    The assertion is identity with the owner rather than a substring of it: a
    paraphrase that still contained `--index` would pass a substring test, and the
    paraphrase is the defect. The placeholder is asserted too, because the fallback's
    one honest difference from the receipt is that this run's message is unknown here.
    """
    work, _head = _with_upstream(tmp_path)
    git_dir = Path(_git(work, "rev-parse", "--absolute-git-dir").stdout.strip())
    # Pre-create the receipt path as a *directory*: `open(..., "w")` raises `OSError`,
    # so no receipt exists and `_receipt_recipe` answers None — the fallback branch.
    (git_dir / "emrg-recovery-receipt.json").mkdir()

    assert _load().recover(work, apply=True) == 0
    out = capsys.readouterr().out
    assert "receipt: could not be written" in out, (
        f"precondition: this run must take the no-receipt branch; got:\n{out}"
    )

    line = next((ln for ln in out.splitlines() if ln.startswith("reversible: ")), None)
    assert line is not None, f"the recovery must print its route; got:\n{out}"
    assert line == f"reversible: {recovery_recipe('<message>')}", (
        "the fallback must be the one owner's recipe with the message left as the "
        "placeholder the reader substitutes from `git stash list` — a hand-written "
        f"spelling here is the second copy that drifted once; got:\n{line}"
    )
    # And the other half of the same claim: the placeholder is a *placeholder*, not a
    # concrete name invented for a stash this recovery cannot know.
    assert "stash@{N}" in line, line


def test_the_owners_recipe_names_the_measured_route():
    """The owner's content is a claim too — identity alone cannot see it change.

    The two printer tests assert that each printer equals `recovery_recipe(...)`, and
    that is exactly one assertion short: **a mutation that changes the owner's own
    text keeps every identity assertion green**, because both sides moved together.
    Measured while writing this change: dropping `--index` from the owner left all 36
    tests in this file passing. The identity assertions cannot close that — only an
    assertion about the *content* can, and it has to be about the route rather than
    about the whole string, because the recipe deliberately spells the wrong spelling
    out in its warning ("a bare `git stash pop` takes the newest…") so a reader knows
    why the route is the one it is.

    So the route is extracted — the command in the first backtick pair after "then " —
    and asserted to be the spelling `test_the_advertised_reversal_is_the_measured_one`
    and `test_the_advertised_selector_survives_a_later_stash` measured with git:
    `apply --index`, selecting the stash by the **ordinal `git stash list` prints** —
    not by the message (no documented `@{…}` form names a stash by message, and the
    ancestry form stops resolving as soon as a later stash exists) and not by
    `stash@{0}` (the newest, which is this one only until the next is made).
    """
    recipe = recovery_recipe("emrg-recovery-20260917T000000Z")
    assert "then " in recipe, f"the recipe must name the route after 'then': {recipe}"
    assert "emrg-recovery-20260917T000000Z" in recipe, (
        f"the recipe must carry the message it was given: {recipe}"
    )
    after = recipe.split("then ", 1)[1]
    assert after.startswith("`"), f"the route must be a quoted command: {after}"
    route = after.split("`")[1]

    assert "--index" in route, (
        "the advertised route must restore the staged side — a bare `apply` or `pop` "
        f"brings a staged change back unstaged; route was: {route!r}"
    )
    assert route == "git stash apply --index stash@{N}", (
        "the route must name the stash the way `git stash list` does and leave the "
        "ordinal to that list — a message selector either fails (`^{/…}`) or silently "
        f"applies the newest stash (`@{{/…}}`); route was: {route!r}"
    )
    # The ordinal is the reader's substitution, and the test above proves the recipe's
    # own first half is where they get it: the list is named in the same recipe.
    assert "`git stash list`" in recipe, (
        f"the route's `N` has to come from somewhere, and the recipe must say where: {recipe}"
    )
    # The message is substituted, not left as a word: two runs must not print the same
    # recipe, or the receipt would name no stash in particular.
    other = recovery_recipe("emrg-recovery-19990101T000000Z")
    assert recipe != other, "the recipe must carry the message it was given"


def test_the_git_state_dir_is_answered_normalised(monkeypatch):
    """The invariant behind #1292's red Windows leg, pinned on every platform.

    `git rev-parse --absolute-git-dir` prints a Windows git dir with **forward
    slashes** (`C:/.../.git`). The branch that returned it verbatim made the receipt
    path mixed-separator (`C:/.../.git\\emrg-recovery-receipt.json`) — a spelling that
    `str(Path(state) / name)`, which is what the test above builds, does not produce,
    so that test failed on windows-2025 while passing here. The platform semantics
    cannot be replayed on POSIX (`os.path` is `posixpath`), so what is pinned is the
    property the fix establishes: whatever git prints — here an absolute git dir
    wearing a redundant separator, the same *shape* — the answer is normalised, and
    appending a name to it agrees with `Path(state) / name` on that platform.

    Without the normalisation this fails on every platform, which is the point: the
    discriminating case used to live only in CI, and a local green said nothing.
    """
    class _Fake:
        returncode = 0
        stdout = f"{Path(__file__).resolve().parent}/.git/./"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Fake())
    state = _load().TaskHandler._git_state_dir("/does/not/matter")
    assert state == os.path.normpath(state), state
    assert os.path.join(state, "emrg-recovery-receipt.json") == str(
        Path(state) / "emrg-recovery-receipt.json"
    ), state


def test_the_action_asks_the_criterion_itself_and_cannot_be_told_the_answer(tmp_path):
    """Defence in depth: no caller's verdict can disarm the net (found in review, #1274).

    An independent review measured the earlier shape — the action accepted the
    caller's already-measured verdict as an optional argument — stashing a tree that
    held an untracked file, i.e. work that exists nowhere else, with the receipt
    calling it a recovery. A guarantee that holds only while every caller passes the
    truth is not a guarantee, so the parameter is gone: a caller that has not measured
    now gets a ``TypeError`` instead of a silently wrong answer.
    """
    repo = tmp_path / "repo"
    _new_repo(repo)
    (repo / "notes.md").write_text("only here", encoding="utf-8")

    status, detail = _load().TaskHandler._recover_dirty_tree_sync(str(repo))
    assert status == "refused" and "notes.md" in detail, detail
    assert (repo / "notes.md").read_text(encoding="utf-8") == "only here"
    assert _git(repo, "stash", "list").stdout.strip() == "", "nothing may be moved aside"

    # The reviewed exploit, verbatim: `_recover_dirty_tree_sync(repo, <verdict>)`.
    # It must not be expressible rather than merely discouraged.
    with pytest.raises(TypeError):
        _load().TaskHandler._recover_dirty_tree_sync(str(repo), "reported by the caller")

    # The other direction, same entry point: reconstructible dirt does converge.
    work, head = _with_upstream(tmp_path)
    status, detail = _load().TaskHandler._recover_dirty_tree_sync(str(work))
    assert status == "recovered" and "stash" in detail, detail
    assert _status(work).strip() == ""
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head, "HEAD must not move"


def test_the_four_outcomes_are_not_two(tmp_path):
    """`(bool, detail)` collapsed three different answers into `False`; #1274 split them.

    A caller reading any non-recovered answer as "nothing happened" would report a
    tree it just *refused* to touch as though the question had been settled, and one
    reading `clean` as `recovered` would claim a convergence that never ran. So the
    states are pinned apart, including the one that means "I could not answer".
    """
    seen = {}

    clean = tmp_path / "clean"
    _new_repo(clean)
    seen["clean"], _ = _load().TaskHandler._recover_dirty_tree_sync(str(clean))
    assert _git(clean, "stash", "list").stdout.strip() == "", "a no-op makes no stash"

    unique = tmp_path / "unique"
    _new_repo(unique)
    (unique / "notes.md").write_text("only here", encoding="utf-8")
    seen["refused"], _ = _load().TaskHandler._recover_dirty_tree_sync(str(unique))

    plain = tmp_path / "plain"
    plain.mkdir()
    seen["error"], detail = _load().TaskHandler._recover_dirty_tree_sync(str(plain))
    assert "could not run" in detail, detail

    assert sorted(seen.values()) == ["clean", "error", "refused"], seen


def test_a_clean_tree_is_a_no_op(tmp_path, capsys):
    repo = tmp_path / "repo"
    _new_repo(repo)

    assert _load().recover(repo, apply=True) == 0
    assert "nothing to recover" in capsys.readouterr().out
    assert _git(repo, "stash", "list").stdout.strip() == ""


def test_a_directory_that_is_not_a_repo_cannot_be_measured(tmp_path, capsys):
    """Exit 2 — a question that could not be answered is never reported as a pass."""
    plain = tmp_path / "plain"
    plain.mkdir()

    assert _load().recover(plain, apply=True) == 2
    assert "could not measure" in capsys.readouterr().out


def test_main_parses_the_repo_and_apply_flags(tmp_path, capsys):
    """The CLI wiring, so the documented invocation is the tested one."""
    work, _ = _with_upstream(tmp_path)

    assert _load().main(["--repo", str(work)]) == 0
    assert "recoverable" in capsys.readouterr().out
    assert _status(work).startswith(" M"), "without --apply nothing is written"


def _staged_deletion_repo(tmp_path: Path, name: str, message: str | None = None) -> Path:
    """A repository whose only dirt is a staged deletion (`D  f.txt`), now stashed.

    The geometry issue #1284's table is about, and one the criterion releases:
    `git rm` on content unchanged from `HEAD`, so every byte of the removed file is
    already in `HEAD` and moving it aside cannot lose anything. Stashed with the
    action's own spelling (`git stash push -u`), which is what the reversal is the
    inverse *of*.

    `message` is the action's own `emrg-recovery-<ts>` name when a test needs to
    address the stash *by* it — the selector is a claim about names, so the tests
    that measure it cannot use git's default `WIP on …` message.
    """
    repo = tmp_path / name
    _new_repo(repo)
    _git(repo, "rm", "-q", "f.txt")
    assert _status(repo) == "D  f.txt\n", _status(repo)
    _git(repo, "stash", "push", "-u", *(["-m", message] if message else []))
    assert _status(repo) == "", _status(repo)
    return repo


def test_the_advertised_reversal_is_the_measured_one(tmp_path):
    """Issue #1284 item 2: the undo half is a claim about a git command, so measure it.

    The prose used to say the stashed work is "one `git stash pop` away". On this
    geometry — a staged deletion, which the criterion answers *recoverable* — that
    spelling is **not** the inverse, and the difference is invisible until someone
    needs the undo: it returns the change unstaged and it consumes the stash, so the
    exact spelling is no longer available to try again. Measured with git 2.50.1,
    three fresh repositories stashed the same way, one reversal each:

        git stash pop                     -> ` D f.txt`, stash dropped
        git stash pop --index             -> `D  f.txt`, stash dropped
        git stash apply --index stash@{0} -> `D  f.txt`, stash kept   <- advertised

    So this is not a stylistic preference between spellings: only the advertised one
    restores the state *and* leaves the evidence. The claim is a claim about `git`,
    which means a future git can invalidate it — if this test ever fails, the prose
    below is what has to be re-read and re-worded, not the test:
    `DEVELOPMENT.md` (the recovery section), `scripts/recover-worktree.py`'s
    docstring and its `reversible:` output, and `_recover_dirty_tree_sync`'s
    docstring in `emrg/server/scheduler.py`.
    """
    bare = _staged_deletion_repo(tmp_path, "bare-pop")
    _git(bare, "stash", "pop")
    assert _status(bare).startswith(" D"), (
        "the bare `git stash pop` was expected to bring the staged deletion back "
        f"*unstaged* — that is what makes it not the inverse; got {_status(bare)!r}"
    )
    assert _git(bare, "stash", "list").stdout.strip() == "", (
        "the bare `git stash pop` was expected to consume the stash"
    )

    one_shot = _staged_deletion_repo(tmp_path, "pop-index")
    _git(one_shot, "stash", "pop", "--index")
    assert _status(one_shot).startswith("D  "), _status(one_shot)
    assert _git(one_shot, "stash", "list").stdout.strip() == ""

    advertised = _staged_deletion_repo(tmp_path, "apply-index")
    _git(advertised, "stash", "apply", "--index", "stash@{0}")
    assert _status(advertised).startswith("D  "), (
        "the advertised spelling `git stash apply --index` must restore the staged "
        f"side, byte for byte; got {_status(advertised)!r}"
    )
    assert _git(advertised, "stash", "list").stdout.strip() != "", (
        "the advertised spelling must KEEP the stash — it is the one a reader can "
        "run again, and the one the receipt hands them"
    )


def test_the_advertised_selector_survives_a_later_stash(tmp_path):
    """The selector is a claim about *names*, and the state it is used in has two stashes.

    The recipe's own parenthetical gives the reason it names a stash at all: "a bare
    `git stash pop` takes the newest, which is this one only until the next stash is
    made". The state that sentence describes is not a corner case — it is what the
    second ordinary recovery in a repository looks like, because `apply` keeps the
    first stash forever. Measured with git 2.50.1 in that state (the named stash at
    `stash@{1}`, a later `emrg-recovery-…` at `stash@{0}`):

        stash^{/<message>}  -> rc=128 to resolve; `apply` rc=1, `error: … is not a
                               valid reference`; geometry NOT restored
        stash@{/<message>}  -> resolves to `stash@{0}` (the LATER stash) whatever
                               message is passed; `apply` rc=0 restoring the WRONG one
        stash@{N}           -> rc=0, the named entry; `apply` rc=0, `D  f.txt`
                               restored, both stashes kept          <- advertised

    The middle row is the trap this test exists for: `@{…}` is documented in
    `gitrevisions(7)` for ordinals, dates, upstream and push — not for messages — so
    it does not fail, it silently applies someone else's stash. `^{/<text>}` is
    documented, but searches **commit ancestry**, and an older stash commit is not an
    ancestor of a newer one, so the older name stops resolving the moment a later
    stash exists. Both directions are pinned below, so a future git that changes
    either reddens here instead of in a reader's shell.
    """
    named = "emrg-recovery-20260917T010000Z"
    later = "emrg-recovery-LATER"

    def _two_stash_repo(name: str) -> Path:
        repo = _staged_deletion_repo(tmp_path, name, message=named)
        # The next ordinary recovery in this repository: a second, later stash, so the
        # named one is no longer `stash@{0}`.
        (repo / "g.txt").write_text("base\n", encoding="utf-8")
        _git(repo, "add", "g.txt")
        _git(repo, "commit", "-q", "-m", "add g")
        (repo / "g.txt").write_text("later recovery\n", encoding="utf-8")
        _git(repo, "stash", "push", "-u", "-m", later)
        assert _status(repo) == ""
        listing = _git(repo, "stash", "list").stdout
        assert listing.count("emrg-recovery-") == 2, (
            f"precondition: the named stash and a later one must both exist; got {listing!r}"
        )
        lines = listing.splitlines()
        assert any(ln.endswith(named) for ln in lines), (
            f"precondition: the named stash must be in the list; got {listing!r}"
        )
        assert any(ln.endswith(later) for ln in lines), (
            f"precondition: the later stash must be in the list; got {listing!r}"
        )
        assert not lines[0].endswith(named), (
            f"precondition: the named stash must no longer be the newest; got {listing!r}"
        )
        return repo

    # The route as the receipt hands it over, taken from the one owner rather than
    # re-spelled here — the reader's copy and this one must be the same string. The
    # ordinal the route names is read off the list, which is what the recipe's first
    # half tells the reader to do.
    repo = _two_stash_repo("later-stash")
    index = next(
        ln.split(":")[0].split("{")[1].rstrip("}")
        for ln in _git(repo, "stash", "list").stdout.splitlines()
        if ln.endswith(named)
    )
    route = recovery_recipe(named).split("then ", 1)[1].split("`")[1].replace("N", index)
    argv = route.split()
    assert argv[0] == "git", f"the advertised command must be a literal git command: {route!r}"
    applied = _git(repo, *argv[1:])
    assert "D  f.txt" in _status(repo), (
        f"the advertised route must still find the named stash when a later one "
        f"exists — it restored {_status(repo)!r}; git said: {applied.stderr.strip()!r}"
    )
    assert _git(repo, "stash", "list").stdout.count("emrg-recovery-") == 2, (
        "and it must keep both stashes: the reader may have to run it again"
    )

    # The trap, pinned as measured: `@{/<message>}` resolves to the *newest* entry
    # regardless of the message, so a reader who "repairs" the recipe into that form
    # gets a successful apply of the wrong stash. This is asserted, not described,
    # because a silent wrong-stash apply is the one failure a reader cannot notice.
    trap = _two_stash_repo("later-stash-reflog-form")
    resolved = _git(trap, "rev-parse", f"stash@{{/{named}}}")
    assert resolved.returncode == 0, (
        f"`@{{/<message>}}` was measured to resolve (to the newest, not the named) "
        f"rather than fail; it now fails with {resolved.stderr.strip()!r}"
    )
    newest = _git(trap, "rev-parse", "stash@{0}").stdout.strip()
    assert resolved.stdout.strip() == newest, (
        "`@{/<message>}}` must be recorded as resolving to the newest stash — if it "
        "now names the entry the message mentions, the recipe may use it again"
    )
    _git(trap, "stash", "apply", "--index", f"stash@{{/{named}}}")
    assert "D  f.txt" not in _status(trap), (
        "and applying it must NOT restore the named geometry — that is why the recipe "
        f"does not use this form (status={_status(trap)!r})"
    )

    # The other direction: the ancestry form does not resolve once a later stash exists.
    ancestry = _two_stash_repo("later-stash-ancestry")
    failed = _git(ancestry, "stash", "apply", "--index", f"stash^{{/{named}}}")
    assert failed.returncode != 0, (
        "the `^{/<message>}` form was measured to stop resolving once a later stash "
        f"exists; it now succeeds, so the docstring needs re-measuring: {failed.stdout.strip()!r}"
    )
    assert "D  f.txt" not in _status(ancestry), (
        "the failed form must not have restored the geometry by accident"
    )


def test_the_tool_prints_the_receipts_own_recipe(tmp_path, capsys):
    """Issue #1284 item 2, mechanised: the undo has one owner, and stdout shows it.

    The receipt written beside the move already carried the measured spelling
    (`git stash apply --index` with a message-selecting stash name). The tool's own `reversible:`
    line was a *paraphrase* of it, and the paraphrase was the bare `git stash pop` —
    one recovery, two spellings, and the one a reader sees on stdout is the one that
    costs them the staged side and the stash. A paraphrase is a second copy that can
    drift, so the line is now the receipt's own string, and this asserts that instead
    of assuming it: re-word the daemon's recipe and stdout re-words with it.

    That fix covered the branch where a receipt *exists*. The other branch — no
    receipt, so no message to name — still wrote the spelling by hand, which is how a
    third copy survived the change that was about copies. It now calls the same
    function (`recovery_recipe`) with the message the reader substitutes, asserted by
    `test_the_fallback_recipe_is_the_owners_recipe_not_a_paraphrase`, so both printers
    are the owner and neither can drift from it.

    What the *tool* cannot cover is prose that quotes the recipe in a document, and
    the first attempt at a scanner for it was a false verdict: it required a paragraph
    mentioning `stash pop` next to an "undoable"/"recoverable" word to also name
    `--index`, which refused `emrg/server/scheduler.py`'s own note *recording the harm*
    a plain pop did — prose that has to spell the wrong spelling out. That class is
    specific to an *absence* test, so the document is pinned by the other shape: a
    *presence* assertion that the recovery bullet still names the measured spelling
    (`test_the_document_still_carries_the_measured_spelling`). Together with
    `test_the_advertised_reversal_is_the_measured_one`, which measures the behaviour
    those documents describe and names them so a failure points at the text to re-read,
    the claim is pinned at both ends: what git does, and what the reader is told.
    """
    work, _head = _with_upstream(tmp_path)

    assert _load().recover(work, apply=True) == 0
    out = capsys.readouterr().out
    git_dir = Path(_git(work, "rev-parse", "--absolute-git-dir").stdout.strip())
    receipt = json.loads(
        (git_dir / "emrg-recovery-receipt.json").read_text(encoding="utf-8")
    )

    assert "--index" in receipt["reversible_with"], (
        "precondition: the daemon's recipe is the spelling that restores the state"
    )
    assert f"reversible: {receipt['reversible_with']}" in out, (
        "the tool must print the receipt's own recipe, not a second copy of it that "
        f"can drift; stdout was:\n{out}"
    )


def test_the_document_still_carries_the_measured_spelling():
    """The reader-facing recipe is prose, so it can be *re-worded* back to the harm.

    `DEVELOPMENT.md`'s recovery bullet is where a reader meets the undo, and nothing
    mechanical connected it to the measurement above: re-wording that bullet to "every
    byte one `git stash pop` away" left the whole file green (measured by
    `how2how2how2-arch` on this PR, 34 passed). A *presence* assertion has no
    false-verdict class — it fires on the spelling going missing, not on the wrong
    spelling appearing — so it can hold this one line without repeating the scanner
    that had to be dropped (see `test_the_tool_prints_the_receipts_own_recipe`).

    What it deliberately cannot see is a document that states both spellings, or a
    recipe moved to another sentence: it pins presence, which is weaker than a
    scanner. The bullet is located by its own opening words, and a bullet that has
    been re-wrapped or re-numbered is still the same claim; a bullet that is *gone*
    fails here rather than skipping, because an assertion that cannot find its subject
    has measured nothing.
    """
    bullet = _recovery_bullet()
    assert "git stash apply --index" in bullet, (
        "the recovery section must name the reversal that restores the staged side "
        f"and keeps the stash — the spelling the receipt hands a reader; got:\n{bullet}"
    )
    assert "stash@{N}" in bullet, (
        "the recipe has to select the stash by the message the receipt names, not by "
        f"`stash@{{0}}` (the newest, which is this one only until the next is made); "
        f"got:\n{bullet}"
    )


def _module_docstring(path: Path) -> str:
    """The module docstring of `path`, or "" — located with `ast`, never by line number."""
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""


def _named_docstring(path: Path, name: str, owner: str | None = None) -> str:
    """The docstring of `name`, optionally as a method of class `owner`; "" if absent."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    candidates = ast.walk(tree) if owner is None else (
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == owner
    )
    for node in candidates:
        for sub in (node.body if isinstance(node, ast.ClassDef) else [node]):
            if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and sub.name == name):
                return ast.get_docstring(sub) or ""
    return ""


#: Every reader-facing copy of the undo recipe, as (what a reader meets, how to get it).
#: An explicit list rather than a scan of every mention (issue #1304): prose that
#: *records the harm* — a plain `git stash pop` dropping staged content — has to spell the
#: wrong spelling out, so a scanner over mentions is a false verdict on this repo's own
#: notes, which is why #1296 dropped the one it tried. Naming the sites also means a site
#: that is **renamed or moved** fails here loudly instead of leaving the class silently,
#: which a scan cannot promise either. Extracted by `ast` rather than by line number, so
#: an unrelated edit above the docstring cannot make this measure a different paragraph.
_RECIPE_DOC_SITES = (
    ("DEVELOPMENT.md's recovery bullet", DOC, lambda: _recovery_bullet()),
    ("DEVELOPMENT.md's `-f` recovery caveat", DOC, lambda: _recovery_caveat()),
    ("scripts/recover-worktree.py's module docstring", SCRIPT,
     lambda: _module_docstring(SCRIPT)),
    ("scheduler.recovery_recipe's docstring", SCHEDULER,
     lambda: _named_docstring(SCHEDULER, "recovery_recipe")),
    ("TaskHandler._recover_dirty_tree_sync's docstring", SCHEDULER,
     lambda: _named_docstring(SCHEDULER, "_recover_dirty_tree_sync", owner="TaskHandler")),
)


def _assert_site_names_the_measured_spelling(label: str, text: str) -> None:
    """The one criterion, applied to one site — so the self-test drives the real check."""
    assert text.strip(), (
        f"{label}: the site was found but is empty — the anchor moved, so this "
        "assertion would be measuring nothing"
    )
    assert "stash" in text, (
        f"{label}: this site has to be about the stash the recovery made; got:\n{text[:400]}"
    )
    assert "--index" in text, (
        f"{label} must name `--index`, the part of the reversal that restores the staged "
        "side. Without it the text sends a reader to the spelling that takes the newest "
        "stash, brings a staged change back unstaged and consumes it — the failure "
        f"#1284 exists to prevent (issue #1304); got:\n{text[:400]}"
    )


def test_every_reader_facing_copy_of_the_recipe_names_the_measured_spelling():
    """Issue #1304: the recipe is stated in several documents, and only one was pinned.

    #1296 corrected the spelling in the manual tool's own usage text and in the docstring
    of the function that performs the move, and pinned the *document* (see
    `test_the_document_still_carries_the_measured_spelling`), concluding the claim was
    "pinned at both ends". Measured on the merged tree, the guard covered one of the
    three reader-facing sites: re-wording either docstring back to "the action is
    undoable with a bare `git stash pop`" left the suite green (`38 passed`, rc=0),
    while the identical edit to the bullet reddens the test above — the positive
    control that shows the instrument is not blind to prose. So the tool could print
    the correct spelling while its own usage text, and the action's own docstring, sent
    a reader to the harmful one.

    The criterion is the same **presence** assertion, applied to every named site rather
    than to one: each must name `--index`. Like the test above, it deliberately cannot
    see a site that states *both* spellings, and cannot see a recipe moved to another
    sentence — that weakness is the price of not re-introducing the scanner, which was a
    false verdict on prose that records the harm (see
    `test_the_tool_prints_the_receipts_own_recipe`).

    Three things keep it from passing vacuously: each extraction is asserted non-empty
    and to be about the stash, so a renamed function or a moved docstring fails here
    instead of yielding `""` and passing; the site list itself is floored, because a
    list that shrank to one entry would be a weaker claim, not a green one; and the
    check is driven once against the harmed spelling in a `pytest.raises` arm, so the
    assertion is shown to be able to fail.

    The list is a *reader-facing* inventory, so it has to be an inventory: this PR
    first named four sites, and review measured a fifth by the list's own criterion —
    the `-f` paragraph in the same document tells a reader "use `git stash -u`,
    recoverable with `git stash apply --index`", which names `--index` exactly as
    `_assert_site_names_the_measured_spelling` requires, while dropping the ordinal.
    Re-wording it back to a bare `git stash pop` left the suite green (`39 passed`,
    rc=0) while the identical edit to the bullet reddened it, which is how the omission
    was found rather than argued. It is in the list now, and the floor moved with it.
    """
    texts = [(label, get()) for label, _path, get in _RECIPE_DOC_SITES]
    assert len(texts) >= 5, (
        "the class is asserted over the sites named in `_RECIPE_DOC_SITES`; a shorter "
        f"list is a weaker claim, not a passing one (got {len(texts)})"
    )
    for label, text in texts:
        _assert_site_names_the_measured_spelling(label, text)

    # The selector half, at class level rather than per site: the action's docstring
    # deliberately delegates the ordinal to the receipt ("the spelling the receipt
    # names"), so requiring it of every site would redden a correct tree. Requiring it
    # of *none* would let the whole class drop the ordinal and still pass.
    naming_the_ordinal = [label for label, text in texts if "stash@{N}" in text]
    assert len(naming_the_ordinal) >= 2, (
        "at least two reader-facing sites must still name the ordinal selector "
        "`stash@{N}` — the newest entry is this one only until the next recovery — "
        f"rather than a bare `stash@{{0}}`; named by: {naming_the_ordinal}"
    )

    # Self-test (the arm that keeps this from being an assertion that cannot fail):
    # this is the spelling #1296 removed from two of these sites, and #1304 measured
    # free to come back.
    with pytest.raises(AssertionError):
        _assert_site_names_the_measured_spelling(
            "planted", "the action is undoable with a bare `git stash pop`."
        )


def test_a_receipt_from_an_earlier_recovery_is_not_printed(tmp_path):
    """The recipe has to be *this* run's, so the receipt is matched to the detail.

    A receipt left by an earlier recovery names a different stash, and printing it
    would send a reader to `apply` someone else's stash — worse than the paraphrase
    the one-owner change replaced. The detail the action just returned names the
    stash it made, so that is the discriminator, and it is tested directly rather
    than through a recovery whose receipt happens to be stale.
    """
    module = _load()
    base = {"stash_message": "emrg-recovery-20260101T000000Z",
            "reversible_with": "`git stash apply --index stash@{/old}`"}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(base), encoding="utf-8")

    assert module._receipt_recipe(
        str(path), "1 change(s) stashed as emrg-recovery-20260916T000000Z; HEAD unmoved"
    ) is None, "a receipt naming a different stash is not this run's"
    assert module._receipt_recipe(
        str(path), "1 change(s) stashed as emrg-recovery-20260101T000000Z; HEAD unmoved"
    ) == "`git stash apply --index stash@{/old}`"
    assert module._receipt_recipe(str(tmp_path / "absent.json"), "anything") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert module._receipt_recipe(str(broken), "anything") is None, (
        "unreadable receipt -> None, so the caller falls back to the stated spelling "
        "rather than crashing a recovery that already succeeded"
    )
