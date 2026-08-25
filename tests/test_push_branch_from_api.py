"""scripts/push-branch-from-api.py 回归测试。

背景（#988 的 push 方向补全，周期 2026-08-26 00:59 四坑实证）：git-over-https
(github.com:443) 反复不可达而 api.github.com 可达。fetch 方向已有
sync-master-from-api.py；本脚本把 push 方向固化为可维护工具。核心风险 =
Git Data API 结构化创建 commit/tree 时产生与本地不同的 sha（日期偏移丢失、
消息尾随换行等），导致远端分支与本地分叉：
  1. 文本断言：脚本必须包含 blob/tree/commit/refs 四个端点接线 + 失败即止
     （不触碰 refs）标记
  2. 行为断言（hermetic，无网络）：用临时 git 仓库 + 忠实假 API（用真实
     git hash-object 复算 sha）验证 parse_commit 日期偏移保留（+0800 → ISO
     带 +08:00）、对象上传自底向上、commit 链顺序、ref 更新最后、本地 ref
     重写为远端 sha、内容字节一致
"""
import base64
import importlib.util
import io
import json
import os
import subprocess
import urllib.error

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "push-branch-from-api.py"

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
    spec = importlib.util.spec_from_file_location("push_branch_from_api", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(*args, cwd=None, env=None, input_bytes=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["git"] + list(args), capture_output=True, cwd=cwd, env=e,
                          input=input_bytes)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.name", "Test Author", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    return repo


def _write_file(repo: Path, rel: str, content: bytes = b"hello\n"):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return rel


def _commit_all(repo: Path, message: str = "test commit", new_file: str | None = None) -> str:
    if new_file:
        _write_file(repo, new_file, f"content {message}\n".encode())
    _git("add", "-A", cwd=repo)
    r = _git("commit", "-q", "-m", message, cwd=repo, env=GIT_ENV)
    assert r.returncode == 0, r.stderr
    return _git("rev-parse", "HEAD", cwd=repo).stdout.decode().strip()


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com", code, "err", {},
                                  io.BytesIO(body))


def _raw_commit_from_payload(payload):
    """Reconstruct raw commit bytes from a create-commit payload; converts the
    ISO-8601 date (with the original offset) back to '<epoch> <offset>', exactly
    the format git stores (and what GitHub's API stores under the hood).

    Message normalization mirrors observed GitHub create-commit behavior
    (cycle 2026-08-26 00:59): the API-stored object has no trailing newline in
    the message, so a local git-commit-made object (which has one) gets a
    different sha — the script then rewrites the local ref to the remote sha."""
    def person(p):
        import datetime as dt
        d = dt.datetime.fromisoformat(p["date"])
        return f"{p['name']} <{p['email']}> {int(d.timestamp())} {d.strftime('%z')}"

    lines = [f"tree {payload['tree']}"]
    lines += [f"parent {p}" for p in payload.get("parents", [])]
    lines.append(f"author {person(payload['author'])}")
    lines.append(f"committer {person(payload['committer'])}")
    msg = payload["message"]
    if msg.endswith("\n"):
        msg = msg[:-1]
    return ("\n".join(lines) + "\n\n" + msg).encode("utf-8")


def _hash_object(repo: Path, obj_type: str, raw: bytes, write: bool = False) -> str:
    args = ["hash-object", "-t", obj_type]
    if write:
        args.append("-w")
    args.append("--stdin")
    r = _git(*args, cwd=repo, input_bytes=raw)
    assert r.returncode == 0, r.stderr
    return r.stdout.decode().strip()


class FakeGitHub:
    """Faithful GitHub Git Data API fake: recomputes every sha with real git so
    byte-exactness of the script's payloads is verifiable end-to-end."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.objects = {}      # sha -> (type, raw_bytes)
        self.refs = {}         # "heads/x" -> sha
        self.log = []          # (method, path) call log
        self.fail_commits = False

    def __call__(self, method, path, body=None):
        self.log.append((method, path))
        if path.startswith("/repos/x/git/refs/"):
            # GET:  /git/refs/heads/feature/x -> "refs/heads/feature/x"
            # PATCH: same
            ref_key = "refs/" + path[len("/repos/x/git/refs/"):]
            if method == "GET":
                if ref_key in self.refs:
                    return {"object": {"sha": self.refs[ref_key]}}
                raise _http_error(404)
            if method == "PATCH":
                if ref_key not in self.refs:
                    raise _http_error(404)
                self.refs[ref_key] = body["sha"]
                return {"object": {"sha": body["sha"]}}
            raise AssertionError(f"unexpected ref call {method} {path}")
        if path == "/repos/x/git/refs":
            ref = body["ref"]
            if ref in self.refs:
                raise _http_error(422, b'{"message":"Reference already exists"}')
            self.refs[ref] = body["sha"]
            return {"ref": ref, "object": {"sha": body["sha"]}}
        if path.startswith("/repos/x/git/blobs"):
            if method == "GET":
                sha = path.rsplit("/", 1)[1]
                if sha in self.objects:
                    return {"sha": sha}
                raise _http_error(404)
            content = base64.b64decode(body["content"])
            sha = _hash_object(self.repo, "blob", content, write=True)
            self.objects[sha] = ("blob", content)
            return {"sha": sha}
        if path.startswith("/repos/x/git/trees"):
            if method == "GET":
                sha = path.rsplit("/", 1)[1]
                if sha in self.objects:
                    return {"sha": sha}
                raise _http_error(404)
            # content-addressed store: rebuild the tree from the entries with
            # real `git mktree` (git's own serialization + sorting), so the
            # stored sha equals the local sha iff the entries are byte-exact
            lines = []
            for e in body["tree"]:
                assert "/" not in e["path"], "create-tree entries must be immediate children"
                typ = _git("cat-file", "-t", e["sha"], cwd=self.repo).stdout.decode().strip()
                assert typ == e["type"], f"entry {e['path']}: local type {typ} != {e['type']}"
                assert e["mode"] in ("100644", "100755", "120000", "040000", "160000")
                lines.append(f"{e['mode']} {e['type']} {e['sha']}\t{e['path']}")
            r = _git("mktree", cwd=self.repo, input_bytes=("\n".join(lines) + "\n").encode())
            assert r.returncode == 0, r.stderr
            sha = r.stdout.decode().strip()
            self.objects[sha] = ("tree", b"<local>")
            return {"sha": sha}
        if path.startswith("/repos/x/git/commits"):
            if self.fail_commits:
                raise _http_error(422, b'{"message":"Validation Failed"}')
            raw = _raw_commit_from_payload(body)
            sha = _hash_object(self.repo, "commit", raw, write=True)
            self.objects[sha] = ("commit", raw)
            return {"sha": sha}
        if path.startswith("/repos/x/commits/"):
            sha = path.rsplit("/", 1)[1]
            if sha in self.objects:
                return {"sha": sha}
            raise _http_error(404)
        raise AssertionError(f"unexpected call {method} {path}")


# ---------------------------------------------------------------- text wiring


def test_script_wires_all_four_git_data_endpoints():
    content = SCRIPT.read_text(encoding="utf-8")
    for marker in ("/git/blobs", "/git/trees", "/git/commits", "/git/refs",
                   "parse_commit", "update-ref"):
        assert marker in content, marker


def test_script_fails_loud_before_touching_refs():
    content = SCRIPT.read_text(encoding="utf-8")
    assert "push aborted:" in content
    assert "no refs touched" in content     # pre-ref failures say so explicitly
    assert "errors=\"replace\"" in content or "errors='replace'" in content  # gotcha 4
    assert "time.sleep" in content          # transient network retry with backoff
    assert "URLError" in content            # clean fail path for network errors
    assert "auth token" in content          # gh keyring token, memory-only auth
    assert "hash-object" in content         # local materialization of remote commit


# ---------------------------------------------------------- byte-exact logic


def test_parse_commit_preserves_original_offset(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "a.txt")
    _commit_all(repo, "root")
    sha = _commit_all(repo, "child msg", new_file="b.txt")
    raw = _git("cat-file", "commit", sha, cwd=repo).stdout
    parsed = mod.parse_commit(raw)
    # parse_commit is faithful: git stores the message with a trailing newline
    assert parsed["message"] == "child msg\n"
    assert parsed["tree"] != EMPTY_TREE
    assert len(parsed["parents"]) == 1
    assert parsed["author"]["name"] == "Test Author"
    assert parsed["author"]["email"] == "test@example.com"
    # gotcha 3: the +0800 offset must survive into the API payload
    # (1700000000 UTC = 2023-11-15T06:13:20+08:00 — same instant, raw offset kept)
    assert parsed["author"]["date"] == "2023-11-15T06:13:20+08:00"
    assert parsed["committer"]["date"] == "2023-11-15T06:13:20+08:00"


def test_collect_objects_finds_nested_blobs_and_all_trees(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "a/b.txt", b"nested\n")
    _write_file(repo, "top.txt", b"top\n")
    sha = _commit_all(repo, "files")
    tree = _git("rev-parse", f"{sha}^{{tree}}", cwd=repo).stdout.decode().strip()
    objs = mod.collect_objects(tree, cwd=repo)
    assert "a/b.txt" in objs["blobs"].values()
    assert "top.txt" in objs["blobs"].values()
    assert tree in objs["trees"]     # root tree included (key = sha, path "")
    assert objs["trees"][tree] == ""
    assert "a" in objs["trees"].values()  # nested subtree included
    entries = mod.tree_entries(tree, cwd=repo)
    types = {e["path"]: e["type"] for e in entries}
    assert types == {"a": "tree", "top.txt": "blob"}


def test_push_end_to_end_byte_exact_with_fake_github(tmp_path):
    """Full push with a faithful fake API: every sha the 'remote' computes must
    equal the local sha (byte-exact payloads), ref updated last, local branch
    ref rewritten to the remote sha, content identical."""
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "a/b.txt", b"nested\n")
    _write_file(repo, "top.txt", b"top\n")
    _commit_all(repo, "root commit")
    tip = _commit_all(repo, "child commit", new_file="c.txt")
    fake = FakeGitHub(repo)

    orig_api = mod.api
    mod.api = fake
    try:
        result = mod.push_branch("x", "feature/test", "HEAD", False, cwd=repo)
    finally:
        mod.api = orig_api

    # remote ref == local tip? NO: GitHub strips the trailing newline from the
    # message, so the remote commit sha differs from the local tip — the script
    # rewrites the local ref to the remote sha and verifies content equality
    assert result["result"] == "pushed"
    assert result["sha"] != tip
    assert result["content_identical"] is True
    assert fake.refs["refs/heads/feature/test"] == result["sha"]
    # local branch ref rewritten to the remote sha
    local = _git("rev-parse", "refs/heads/feature/test", cwd=repo)
    assert local.returncode == 0
    assert local.stdout.decode().strip() == result["sha"]
    # normalized message (no trailing newline) is what the remote object holds
    assert _git("cat-file", "commit", result["sha"], cwd=repo).stdout.endswith(b"\n\nchild commit")

    # call-order: blobs/trees first, then commits oldest-first, ref last
    def kind(p):
        if "/git/refs" in p:
            return "refs"
        if "/git/blobs" in p:
            return "blobs"
        if "/git/trees" in p:
            return "trees"
        if "/git/commits" in p:
            return "commits"
        return "walk"  # /commits/{sha} base-existence probe

    kinds = [kind(p) for _, p in fake.log]
    assert kinds[-1] == "refs"
    commit_idx = [i for i, k in enumerate(kinds) if k == "commits"]
    assert commit_idx, "no commit creation calls"
    assert kinds.index("blobs") < kinds.index("trees") < commit_idx[0]
    assert all(k not in ("blobs", "trees") for k in kinds[commit_idx[0]:-1])
    # two commits -> two create calls, oldest first
    commits = [p for k, p in zip(kinds, fake.log) if k == "commits"]
    assert len(commits) == 2


def test_raw_commit_reconstruction_matches_reference(tmp_path):
    """The script's _raw_commit must produce the same bytes as the test's
    reference implementation (both rebuild the object GitHub stored)."""
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "a.txt")
    _commit_all(repo, "root")
    _commit_all(repo, "child msg", new_file="b.txt")
    raw = _git("cat-file", "commit", "HEAD", cwd=repo).stdout
    parsed = mod.parse_commit(raw)
    payload = {
        "message": parsed["message"].rstrip("\n"),
        "tree": parsed["tree"],
        "parents": parsed["parents"],
        "author": parsed["author"],
        "committer": parsed["committer"],
    }
    assert mod._raw_commit(payload) == _raw_commit_from_payload(payload)


def test_push_no_op_when_ref_already_at_remote_head(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "a.txt")
    tip = _commit_all(repo, "only commit")
    fake = FakeGitHub(repo)
    fake.refs["refs/heads/feature/x"] = tip

    orig_api = mod.api
    mod.api = fake
    try:
        result = mod.push_branch("x", "feature/x", "HEAD", False, cwd=repo)
    finally:
        mod.api = orig_api

    assert result["result"] == "no-op"
    assert result["sha"] == tip


def test_push_fails_loud_and_touches_no_refs_when_commit_rejected(tmp_path):
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "a.txt")
    _commit_all(repo, "root commit")
    fake = FakeGitHub(repo)
    fake.fail_commits = True

    orig_api = mod.api
    mod.api = fake
    try:
        import pytest
        with pytest.raises(mod.PushError):
            mod.push_branch("x", "feature/bad", "HEAD", False, cwd=repo)
    finally:
        mod.api = orig_api

    assert fake.refs == {}           # ref never touched
    r = _git("rev-parse", "refs/heads/feature/bad", cwd=repo)
    assert r.returncode != 0         # local ref never created
