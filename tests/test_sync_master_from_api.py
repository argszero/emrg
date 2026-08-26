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
    payload = raw.decode("utf-8")  # API payload == raw for unsigned commits
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
