"""scripts/sync-master-from-api.py 回归测试。

背景（rant 驱动，10+ 周期实证）：git-over-https (github.com:443) 在受限网络反复
不可达而 api.github.com 可达。本脚本用 Git Data API 的 verification payload +
signature 字节级重建上游 commit（含 web-flow GPG 签名 squash merge），推进本地
refs。核心风险 = 重建逻辑产生错误 sha（→ 本地历史与上游分叉）：
  1. 文本断言：脚本必须包含签名感知重建 + 树校验 + 失败即止（不触碰 refs）的接线
  2. 行为断言（hermetic，无网络）：在临时 git 仓库里用 git commit-tree 合成
     unsigned / signed 两类 commit，验证 reconstruct_commit() 字节级复现同一 sha
"""
import base64
import importlib.util
import os
import subprocess
from datetime import datetime, timezone

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "sync-master-from-api.py"

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0800",
    "GIT_COMMITTER_NAME": "Test Committer",
    "GIT_COMMITTER_EMAIL": "committer@example.com",
    "GIT_COMMITTER_DATE": "1700000000 +0800",
}


def _load_module():
    spec = importlib.util.spec_from_file_location("sync_master_from_api", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(*args, cwd=None, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["git"] + list(args), capture_output=True, cwd=cwd, env=e)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.name", "Test Author", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    return repo


def _make_commit(repo: Path, message: str = "test commit"):
    """Create a commit via commit-tree; return (raw_bytes, sha)."""
    r = _git("commit-tree", EMPTY_TREE, "-m", message, cwd=repo, env=GIT_ENV)
    assert r.returncode == 0, r.stderr
    sha = r.stdout.decode().strip()
    raw = _git("cat-file", "commit", sha, cwd=repo)
    return raw.stdout, sha


# ---------------------------------------------------------------- text wiring


def test_script_reconstructs_gpg_signed_commits():
    content = SCRIPT.read_text(encoding="utf-8")
    assert "gpgsig " in content                      # signature block embedding
    assert "verification" in content                 # payload source
    assert "reconstruct_commit" in content           # core logic named


def test_script_authenticates_upfront_via_gh_token():
    """Anonymous API requests are limited to 60/hr; a commit-chain walk can
    exhaust them mid-run. The script must resolve a token once via env or
    `gh auth token` (memory-only) and use it from the first request."""
    content = SCRIPT.read_text(encoding="utf-8")
    assert "auth token" in content                   # gh keyring token resolution
    assert "_auth_token()" in content                # helper named
    assert "Authorization" in content                # header applied when token present


def test_script_fails_loud_before_touching_refs():
    content = SCRIPT.read_text(encoding="utf-8")
    assert "aborting (no refs touched)" in content   # mismatch → stop, refs safe
    assert "tree mismatch" in content                # content-object check
    assert "update-ref" in content                   # refs updated only at the end


# ---------------------------------------------------------- byte-exact logic


def test_reconstruct_unsigned_commit_is_byte_exact(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    raw, sha = _make_commit(repo, "unsigned msg")
    # The payload here is hand-supplied: GitHub returns none for an unsigned
    # commit (see reconstruct_unsigned_commit for that path). This pins the
    # identity branch only - "no signature, so the payload is the object".
    payload = raw.decode("utf-8")
    rebuilt = mod.reconstruct_commit(payload, None, "unsigned msg")
    assert rebuilt == raw
    r = subprocess.run(["git", "hash-object", "-t", "commit", "--stdin"],
                       input=rebuilt, capture_output=True, cwd=repo)
    assert r.returncode == 0
    assert r.stdout.decode().strip() == sha  # byte-exact sha reproduction


def test_reconstruct_signed_commit_is_byte_exact(tmp_path):
    """Insert a fake gpgsig block into a synthetic commit, then verify the
    signed branch of reconstruct_commit() reproduces the exact raw bytes."""
    mod = _load_module()
    repo = _init_repo(tmp_path)
    raw_u, sha_u = _make_commit(repo, "signed msg")

    idx = raw_u.index(b"\n\n")
    header, msg = raw_u[:idx], raw_u[idx + 2:]
    sig = ("-----BEGIN PGP SIGNATURE-----\n"
           "\n"
           "wsFcBAABCAAQBQJabcdeCRC1aQ7uu5UhlAAARDgQACxKc\n"
           "KDO6GweASekxICOQVyQPEatLzNCjKyEEth8Z6TfQ97s\n"
           "=/aNl\n"
           "-----END PGP SIGNATURE-----")
    # raw signed object: header + newline + gpgsig block (continuation lines
    # space-prefixed) + blank line + message
    sig_lines = sig.split("\n")
    gpgsig = ["gpgsig " + sig_lines[0]] + [" " + l for l in sig_lines[1:]]
    raw_s = header + b"\n" + "\n".join(gpgsig).encode("utf-8") + b"\n\n" + msg

    r = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                       input=raw_s, capture_output=True, cwd=repo)
    assert r.returncode == 0
    sha_s = r.stdout.decode().strip()

    payload_s = (header + b"\n\n" + msg).decode("utf-8")  # what the API stores
    rebuilt = mod.reconstruct_commit(payload_s, sig, "signed msg")
    assert rebuilt == raw_s
    r2 = subprocess.run(["git", "hash-object", "-t", "commit", "--stdin"],
                        input=rebuilt, capture_output=True, cwd=repo)
    assert r2.stdout.decode().strip() == sha_s


def test_reconstruct_commit_mismatch_raises(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    raw, _ = _make_commit(repo, "real msg")
    import pytest

    with pytest.raises(ValueError):
        mod.reconstruct_commit(raw.decode("utf-8"), None, "different msg")


# ------------------------------------------------- unsigned: rebuild + verify


def _api_view_of(raw: bytes, repo: Path) -> dict:
    """The fields the commits API keeps for one commit, i.e. what is *left* of an
    unsigned object: both dates normalised to UTC, the message trimmed."""
    import re as _re

    text = raw.decode("utf-8")
    head, _, message = text.partition("\n\n")
    fields = dict(
        _re.match(r"(\w+) (.*)", line).groups() for line in head.splitlines()
    )
    author = _re.match(r"(.*) <(.*)> (\d+) ([+-]\d{4})", fields["author"]).groups()
    committer = _re.match(r"(.*) <(.*)> (\d+) ([+-]\d{4})", fields["committer"]).groups()
    stamp = datetime.fromtimestamp(int(author[2]), tz=timezone.utc)
    iso = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert stamp.timestamp() == int(committer[2])  # same instant, spelled twice
    return {
        "tree": fields["tree"],
        "parents": [v for k, v in fields.items() if k == "parent"],
        "author": (author[0], author[1], iso),
        "committer": (committer[0], committer[1], iso),
        "message": message.rstrip("\n"),
    }


def test_unsigned_commit_rebuilt_from_the_api_fields(tmp_path):
    """GitHub answers `payload: null` for an unsigned commit (measured on a live
    PR head), so the object has to come from tree/parents/identities/message -
    and the two losses (the date offsets, the message's trailing newline) are
    restored by *verification*: only a candidate whose sha equals the remote one
    is accepted. Byte-exactness is the assertion, not an approximation of it."""
    mod = _load_module()
    repo = _init_repo(tmp_path)
    raw, sha = _make_commit(repo, "unsigned msg")

    rebuilt = mod.reconstruct_unsigned_commit(want=sha, **_api_view_of(raw, repo))

    assert rebuilt == raw
    assert mod.commit_sha(rebuilt) == sha


def test_unsigned_commit_with_two_different_offsets_is_rebuilt(tmp_path):
    """A rebase re-commits with another machine's offset: the author line keeps
    the original one, so the search must cover the *mixed* pairs and not only the
    agreeing ones (the offsets it tries first)."""
    mod = _load_module()
    repo = _init_repo(tmp_path)
    env = dict(GIT_ENV)
    env["GIT_AUTHOR_DATE"] = "1700000000 +0530"
    env["GIT_COMMITTER_DATE"] = "1700000000 -0700"
    r = _git("commit-tree", EMPTY_TREE, "-m", "mixed offsets", cwd=repo, env=env)
    assert r.returncode == 0, r.stderr
    sha = r.stdout.decode().strip()
    raw = _git("cat-file", "commit", sha, cwd=repo).stdout

    rebuilt = mod.reconstruct_unsigned_commit(want=sha, **_api_view_of(raw, repo))

    assert rebuilt == raw
    assert mod.commit_sha(rebuilt) == sha


def test_an_unreconstructible_unsigned_commit_is_refused(tmp_path):
    """The search is bounded (offsets in 15-minute steps, three message
    spellings). When nothing inside it reproduces the remote name, the answer is
    a named refusal - never a commit written with a guessed offset, whose local
    history would then diverge from upstream."""
    import pytest

    mod = _load_module()
    repo = _init_repo(tmp_path)
    raw, _ = _make_commit(repo, "unsigned msg")
    view = _api_view_of(raw, repo)

    with pytest.raises(ValueError) as exc:
        mod.reconstruct_unsigned_commit(want="0" * 40, **view)
    assert "not reconstructible" in str(exc.value)


def test_script_rebuilds_an_unsigned_commit_instead_of_crashing(tmp_path):
    """The walk used to read `verification["payload"]` unconditionally and die
    with `AttributeError: 'NoneType' object has no attribute 'encode'` - a crash
    where the honest answer is a stated "unmeasurable" (and no way at all to
    advance a ref during an outage, which is this script's whole purpose)."""
    content = SCRIPT.read_text(encoding="utf-8")
    assert "if payload:" in content                     # the null payload is expected
    assert "reconstruct_unsigned_commit(" in content    # and has its own path
    assert "unmeasurable:" in content                   # refusal is stated, not raised raw


# ------------------------------------------------- content-object auto-fetch


def test_script_auto_fetches_missing_content_objects():
    """cyc20260826-154904 教训：head commit 存在但其 blobs/trees 本地缺失时，
    脚本应经 Git Data API 自动补全（blob hash-object + tree mktree），而非直接
    fail-loud 要求 git fetch。"""
    content = SCRIPT.read_text(encoding="utf-8")
    assert "git/blobs" in content                    # blob fetch path
    assert "mktree" in content                       # tree rebuild path
    assert "no-fetch-objects" in content             # opt-out flag exists


def _tree_entries(repo: Path, tree_sha: str) -> list[dict]:
    """Parse `git ls-tree` of a tree into GitHub tree-API entry dicts."""
    r = _git("ls-tree", tree_sha, cwd=repo)
    assert r.returncode == 0, r.stderr
    entries = []
    for line in r.stdout.decode().splitlines():
        mode, typ, sha, path = line.split(None, 3)
        entries.append({"path": path, "mode": mode, "type": typ, "sha": sha})
    return entries


def test_fetch_missing_tree_and_blobs_hermetic(tmp_path, monkeypatch):
    """在空仓库中，用假 API 补全 root tree → sub tree → blobs 全链路；
    验证对象落库且 root tree sha 与源仓库一致（递归 + mktree 排序正确）。"""
    mod = _load_module()

    # 源仓库：a.txt + sub/b.txt 两个 blob、一个子树
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", cwd=src)
    _git("config", "user.name", "Test Author", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    (src / "a.txt").write_text("hello alpha\n", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_text("beta bytes\n", encoding="utf-8")
    _git("add", ".", cwd=src)
    r = _git("commit", "-m", "content commit", cwd=src, env=GIT_ENV)
    assert r.returncode == 0, r.stderr

    root = _git("rev-parse", "HEAD^{tree}", cwd=src).stdout.decode().strip()
    entries = _tree_entries(src, root)
    assert len(entries) == 2  # a.txt + sub/
    sub_tree = next(e["sha"] for e in entries if e["type"] == "tree")
    sub_entries = _tree_entries(src, sub_tree)
    assert len(sub_entries) == 1 and sub_entries[0]["path"] == "b.txt"
    blob_a = next(e["sha"] for e in entries if e["type"] == "blob")
    blob_b = sub_entries[0]["sha"]

    # 假 API：按 sha 提供 tree（非递归）与 blob（base64）
    trees = {root: entries, sub_tree: sub_entries}
    blobs = {
        blob_a: _git("cat-file", "blob", blob_a, cwd=src).stdout,
        blob_b: _git("cat-file", "blob", blob_b, cwd=src).stdout,
    }

    def fake_get(url: str) -> dict:
        if "/git/blobs/" in url:
            sha = url.rsplit("/", 1)[1]
            return {"content": base64.b64encode(blobs[sha]).decode("ascii"),
                    "encoding": "base64"}
        if "/git/trees/" in url:
            sha = url.rsplit("/", 1)[1]
            return {"tree": trees[sha], "truncated": False}
        raise AssertionError(f"unexpected API call: {url}")

    monkeypatch.setattr(mod, "api_get", fake_get)

    # 目标：全新空仓库（无任何对象）——模拟本地缺失 blobs/trees 的场景
    target = tmp_path / "target"
    target.mkdir()
    _git("init", "-q", cwd=target)
    monkeypatch.chdir(target)

    mod._fetch_tree("owner/repo", root)

    # 全部对象落库，root tree 可解析且 sha 一致
    assert _git("cat-file", "-e", root, cwd=target).returncode == 0
    assert _git("cat-file", "-e", sub_tree, cwd=target).returncode == 0
    assert _git("cat-file", "-e", blob_a, cwd=target).returncode == 0
    assert _git("cat-file", "-e", blob_b, cwd=target).returncode == 0
    r = _git("rev-parse", root, cwd=target)
    assert r.returncode == 0 and r.stdout.decode().strip() == root


def test_fetch_missing_objects_idempotent(tmp_path, monkeypatch):
    """已存在的对象不再请求 API（幂等），且 blob/tree 均可安全重入。"""
    mod = _load_module()

    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", cwd=src)
    _git("config", "user.name", "Test Author", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    (src / "x.txt").write_text("x\n", encoding="utf-8")
    _git("add", ".", cwd=src)
    r = _git("commit", "-m", "x", cwd=src, env=GIT_ENV)
    assert r.returncode == 0, r.stderr

    root = _git("rev-parse", "HEAD^{tree}", cwd=src).stdout.decode().strip()
    entries = _tree_entries(src, root)
    blob = next(e["sha"] for e in entries if e["type"] == "blob")

    target = tmp_path / "target"
    target.mkdir()
    _git("init", "-q", cwd=target)
    monkeypatch.chdir(target)

    calls = {"n": 0}

    def fake_get(url: str) -> dict:
        calls["n"] += 1
        if "/git/blobs/" in url:
            return {"content": base64.b64encode(b"x\n").decode("ascii"),
                    "encoding": "base64"}
        if "/git/trees/" in url:
            return {"tree": entries, "truncated": False}
        raise AssertionError(f"unexpected API call: {url}")

    monkeypatch.setattr(mod, "api_get", fake_get)
    mod._fetch_tree("owner/repo", root)
    n1 = calls["n"]
    assert n1 >= 1
    # 第二遍：全部已存在 → 零 API 调用
    mod._fetch_tree("owner/repo", root)
    assert calls["n"] == n1
    assert _git("cat-file", "-e", blob, cwd=target).returncode == 0


def test_absent_parents_names_what_the_merge_stop_trusts():
    """The merge-stop assumption is read, not left silent.

    `--ref <a PR head>` on 2026-09-28 stopped the walk at that branch's merge
    commit and left parent `2456e72d` missing. With the gap unmentioned git
    answered *as if the graph were whole*: `git merge` printed "Already up to
    date" off a commit it could not read, and `--is-ancestor` exited 128 where
    it should have exited 1 - a wrong answer, never a missing one, which is the
    failure mode this reading exists to remove. The predicate is a parameter so
    the answer about a layout can be asked without a repository.
    """
    mod = _load_module()

    present = {"a" * 40, "c" * 40}
    assert mod._absent_parents(["a" * 40, "b" * 40, "c" * 40],
                               present=present.__contains__) == ["b" * 40]
    # A walk whose parents are all local stays quiet: nothing is reported.
    assert mod._absent_parents(["a" * 40, "c" * 40],
                               present=present.__contains__) == []


def test_stop_lines_name_a_root_as_a_root_not_a_merge():
    """The stop branch catches every parent count but one - the empty list.

    That list is reachable: on a checkout that has none of the remote's objects
    (the situation this script repairs), `--ref master` walks the whole chain back
    and terminates at the repository's **root commit**, whose parents list is
    empty. A merge statement there would print "trusting its 0 parent(s) to be
    local" - a trust assertion about nothing, the same class of defect this change
    removes: a line asserting something that is not a reading. So the root is named
    a root, and a merge is still named a merge.
    """
    mod = _load_module()

    root = mod._stop_lines("a" * 40, [], [])
    assert len(root) == 1
    assert "root commit" in root[0]
    assert "merge" not in root[0] and "parent(s)" not in root[0]

    # A genuine merge still reads as a merge, stating how many parents it trusts.
    merge = mod._stop_lines("b" * 40, ["c" * 40, "d" * 40], [])
    assert "merge commit" in merge[0] and "2 parent(s)" in merge[0]

    # And an absent parent is still named, as before.
    warned = mod._stop_lines("b" * 40, ["c" * 40, "d" * 40], ["d" * 40])
    assert any("NOT present locally" in ln for ln in warned)


def test_an_object_name_ref_creates_no_ref():
    """`--ref <sha>` materializes a commit; it must not write a ref named after it.

    Measured 2026-09-28: materializing a PR head with `--ref <sha>` left
    `refs/heads/<sha>` and `refs/remotes/origin/<sha>` behind, and `git rev-parse
    <sha>` then answered ambiguously - `check-merge-plan-suite.py` failed with
    "merge-tree failed" until those refs were deleted. So the shape of a `--ref`
    that names an object is pinned here, without a repository.
    """
    mod = _load_module()

    assert mod._is_object_name("a" * 40)
    assert mod._is_object_name("0123456789abcdef0123456789ABCDEF01234567")
    # Branch names - the default included - are not object names.
    assert not mod._is_object_name("master")
    assert not mod._is_object_name("fix/the-walk-names-the-parents-it-trusts")
    assert not mod._is_object_name("a" * 39)
    assert not mod._is_object_name("a" * 41)
    # A short hex string is a branch name, not an object name: it cannot be a full
    # object name, and refusing it would break a real branch called `deadbeef`.
    assert not mod._is_object_name("deadbeef")


# ------------------------------------------------- ref advance + checkout state


def _repo_on_master_with_a_next_commit(tmp_path):
    """A repo standing clean on `master` at c1, plus a c2 the checkout never sees.

    c2 is built with plumbing (`hash-object` / `mktree` / `commit-tree`) so
    refs/heads/master is still c1 while the worktree is clean - the state
    `--ref master` starts from before it moves the ref.
    """
    repo = _init_repo(tmp_path)
    _git("symbolic-ref", "HEAD", "refs/heads/master", cwd=repo)
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "one", cwd=repo, env=GIT_ENV)
    c1 = _git("rev-parse", "HEAD", cwd=repo).stdout.decode().strip()

    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=b"two\n",
                          capture_output=True, cwd=repo).stdout.decode().strip()
    tree = subprocess.run(["git", "mktree"],
                          input=f"100644 blob {blob}\tf.txt\n".encode("utf-8"),
                          capture_output=True, cwd=repo).stdout.decode().strip()
    env = dict(os.environ)
    env.update(GIT_ENV)
    c2 = subprocess.run(["git", "commit-tree", tree, "-p", c1, "-m", "two"],
                        capture_output=True, cwd=repo, env=env).stdout.decode().strip()
    return repo, c1, c2


def test_advancing_the_checked_out_branch_refreshes_the_index(tmp_path, capsys):
    """The trap this closes: `git update-ref` moves the ref, never the index.

    Measured 2026-09-30: `--ref master` while standing on master left HEAD tree
    `24b51c38` against an index tree one commit behind, so `git status` reported
    every commit the sync had just brought in as a *staged reversion of itself* -
    and the next cycle read those rows as local edits and repaired them by hand.
    After the advance the checkout must be clean and hold the new content.
    """
    mod = _load_module()
    repo, c1, c2 = _repo_on_master_with_a_next_commit(tmp_path)
    assert _git("status", "--porcelain", cwd=repo).stdout.strip() == b""

    mod._update_refs("master", c2, repo=str(repo))

    out = capsys.readouterr().out
    assert "index and worktree refreshed" in out
    assert _git("rev-parse", "HEAD", cwd=repo).stdout.decode().strip() == c2
    assert _git("rev-parse", "refs/remotes/origin/master", cwd=repo).stdout.decode().strip() == c2
    assert (repo / "f.txt").read_text(encoding="utf-8") == "two\n"
    # The reading that was broken: no staged reversion is left behind.
    assert _git("status", "--porcelain", cwd=repo).stdout.strip() == b""
    assert c1 != c2


def test_a_dirty_worktree_is_reported_never_clobbered(tmp_path, capsys):
    """Local changes are the caller's, not the script's to discard.

    The refusal must still move the ref (the sync's job) and must *print* the
    condition and its remedy, because a silent half-state is what the previous
    cycle paid a repair for. The local edit survives.
    """
    mod = _load_module()
    repo, _c1, c2 = _repo_on_master_with_a_next_commit(tmp_path)
    (repo / "f.txt").write_text("mine\n", encoding="utf-8")

    mod._update_refs("master", c2, repo=str(repo))

    out = capsys.readouterr().out
    assert "the worktree has local changes" in out
    assert "git checkout HEAD -- ." in out          # the remedy is named, not implied
    assert (repo / "f.txt").read_text(encoding="utf-8") == "mine\n"
    assert _git("rev-parse", "HEAD", cwd=repo).stdout.decode().strip() == c2


def test_a_branch_the_caller_is_not_on_is_not_refreshed(tmp_path, capsys):
    """No index points at a ref the caller is not standing on, so nothing moves.

    The ref advance still happens - it is the sync's job - but the worktree of the
    checked-out branch is left untouched and no refresh is claimed.
    """
    mod = _load_module()
    repo, c1, c2 = _repo_on_master_with_a_next_commit(tmp_path)

    mod._update_refs("other", c2, repo=str(repo))

    out = capsys.readouterr().out
    assert "refreshed" not in out and "local changes" not in out
    assert _git("rev-parse", "refs/heads/other", cwd=repo).stdout.decode().strip() == c2
    assert _git("rev-parse", "HEAD", cwd=repo).stdout.decode().strip() == c1
    assert _git("status", "--porcelain", cwd=repo).stdout.strip() == b""


def test_main_advances_refs_through_the_refresh_helper():
    """Text wiring: the ref-advance path is `_update_refs`, not an inline loop.

    The behaviour tests above exercise the helper directly; this pins that main()
    actually routes through it, so a later edit cannot restore the bare
    `git update-ref` that left the index behind.
    """
    content = SCRIPT.read_text(encoding="utf-8")
    assert "_update_refs(args.ref, head)" in content
    assert 'for ref in (f"refs/heads/{args.ref}"' not in content
