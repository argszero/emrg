"""The reconstructible-dirt criterion and `scripts/recover-worktree.py` (#1237).

The guard's contract is "a cycle must not destroy work that exists nowhere else".
These pin the two halves: dirt that IS unique is refused (nothing touched), and
dirt that is NOT unique is converged **reversibly** — the scenario the measured
33-cycle deadlock was made of, built here as a real repository.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "recover-worktree.py"


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

    # The inverse the receipt names, run as named. What this geometry does to it is
    # *measured*, not assumed (the residual is a finding, filed with the undo/audit
    # half in #1284, not something to wish away here): the state comes back byte for
    # byte — deletion still staged, file still untracked — while git exits **1** and
    # warns `f.txt already exists, no checkout`, having restored the untracked copy
    # already by the time it tries again. The stash is consequently *kept*, so the
    # one-shot spelling's evidence is not consumed either (measured: `pop --index`
    # reports the same failure and keeps the entry here).
    message = json.loads(
        (Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
         / "emrg-recovery-receipt.json").read_text(encoding="utf-8")
    )["stash_message"]
    applied = _git(repo, "stash", "apply", "--index", f"stash^{{/{message}}}")
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
