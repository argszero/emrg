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
import pytest
import subprocess
import time as _real_time
import urllib as _real_urllib
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


def _raw_tag_from_payload(payload):
    """Reconstruct the raw annotated tag object GitHub stores for a POST /git/tags
    payload: the tagger's epoch+offset, and a message with no trailing newline -
    the same normalization create-commit is observed to apply."""
    def person(p):
        import datetime as dt
        d = dt.datetime.fromisoformat(p["date"])
        return f"{p['name']} <{p['email']}> {int(d.timestamp())} {d.strftime('%z')}"

    msg = payload["message"]
    if msg.endswith("\n"):
        msg = msg[:-1]
    header = (f"object {payload['object']}\ntype {payload['type']}\n"
              f"tag {payload['tag']}\ntagger {person(payload['tagger'])}")
    return (header + "\n\n" + msg).encode("utf-8")


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
        self.tag_targets = {}  # tag object sha -> the commit it names
        self.tag_payloads = [] # every POST /git/tags body, for the offset reading

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
            # like real GitHub: parents must already exist remotely
            for p in body.get("parents", []):
                assert p in self.objects, f"parent {p} does not exist (not created)"
            assert body["tree"] in self.objects, "tree not uploaded before commit"
            raw = _raw_commit_from_payload(body)
            sha = _hash_object(self.repo, "commit", raw, write=True)
            self.objects[sha] = ("commit", raw)
            return {"sha": sha}
        if path.startswith("/repos/x/commits/"):
            sha = path.rsplit("/", 1)[1]
            if sha in self.objects:
                return {"sha": sha}
            raise _http_error(404)
        if path.startswith("/repos/x/git/ref/tags/"):
            ref_key = "refs/tags/" + path[len("/repos/x/git/ref/tags/"):]
            if method != "GET":
                raise AssertionError(f"unexpected singular-ref call {method} {path}")
            if ref_key in self.refs:
                return {"ref": ref_key, "object": {"sha": self.refs[ref_key]}}
            raise _http_error(404)
        if path == "/repos/x/git/tags":
            self.tag_payloads.append(body)
            raw = _raw_tag_from_payload(body)
            sha = _hash_object(self.repo, "tag", raw, write=True)
            self.objects[sha] = ("tag", raw)
            self.tag_targets[sha] = body["object"]
            return {"sha": sha, "object": {"sha": body["object"], "type": "commit"}}
        if path.startswith("/repos/x/git/tags/"):
            sha = path.rsplit("/", 1)[1]
            if sha in self.tag_targets:
                return {"sha": sha, "object": {"sha": self.tag_targets[sha], "type": "commit"}}
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
    # full local materialization: git log must traverse the API-normalized
    # chain from the new branch head down to the base (all parents local)
    r = _git("log", "--oneline", "refs/heads/feature/test", cwd=repo)
    assert r.returncode == 0, r.stderr
    log = r.stdout.decode()
    assert log.count("\n") == 2, f"expected 2 commits in log, got: {log}"

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


# ------------------------------------------------- what a failed call costs
#
# `api()` retries what is worth retrying. The two defects these tests pin were
# measured on 2026-10-16 (`cyc20261016-183420`), during the outage this tool
# exists for: `HTTPError` is a `URLError`, the network handler stood ahead of the
# HTTP one, and so the HTTP branch - which reads 401/403 and falls back to `gh
# api` - was unreachable while every 4xx was retried with 5 s + 10 s of backoff.


def _stub_time(calls):
    """The module's `time`, with `sleep` recorded and everything else unchanged.

    `mod.time` is replaced, never `mod.time.sleep`. The script does `import time`,
    so `mod.time` IS the process's `time` module: patching its `sleep` made the
    recorder collect the sleeps of every other sleeper alive in the worker. On the
    Linux CI leg a leftover poller put 1131 entries (0.001, 0.002, 0.004 … 0.05)
    into `calls["sleep"]` during these four tests and turned three of them red -
    the assertion had been a statement about the process, not about `api()`.
    """
    class _Time:
        sleep = staticmethod(lambda seconds: calls["sleep"].append(seconds))

        def __getattr__(self, name):
            return getattr(_real_time, name)

    return _Time()


def _stub_urllib(fake_urlopen):
    """The module's `urllib`, with `request.urlopen` faked and the rest forwarded."""
    class _Request:
        urlopen = staticmethod(fake_urlopen)

        def __getattr__(self, name):
            return getattr(_real_urllib.request, name)

    class _Urllib:
        request = _Request()

        def __getattr__(self, name):
            return getattr(_real_urllib, name)

    return _Urllib()


def _stub_one_call(monkeypatch, mod, error):
    """Make `api()` see `error` from urlopen, recording requests and sleeps.

    Both stubs replace an attribute *of the script module*, never an attribute of
    the shared module it points at: the script does `import time` / `import urllib`,
    so `mod.time.sleep` and `mod.urllib.request.urlopen` are the process's own, and
    patching those collects (or intercepts) the calls of every other caller alive in
    the worker. Measured on the Linux leg: a leftover poller put 1131 entries into
    `calls["sleep"]` and turned three of these four tests red.
    """
    calls = {"urlopen": 0, "sleep": []}

    def fake_urlopen(req, timeout=None):
        calls["urlopen"] += 1
        raise error

    monkeypatch.setattr(mod, "urllib", _stub_urllib(fake_urlopen))
    monkeypatch.setattr(mod, "time", _stub_time(calls))
    return calls


def test_a_four_xx_is_an_answer_and_is_not_retried(monkeypatch):
    """404 and 422 are this tool's own control flow: they must cost one request.

    A 422 is how `push_branch` learns "this commit is not on the remote yet", and it
    asks that question once per ancestor. Spending 15 s on each answer is the cost
    the fix removed (measured: a 422 answered in 0.5 s took 17.5 s).
    """
    mod = _load_module()
    calls = _stub_one_call(monkeypatch, mod, _http_error(422, b'{"message":"no"}'))

    with pytest.raises(urllib.error.HTTPError):
        mod.api("GET", "/repos/x/commits/deadbeef")

    assert calls["urlopen"] == 1, "a 4xx was requested more than once"
    assert calls["sleep"] == [], "a 4xx was slept on"


def test_a_not_found_is_an_answer_and_is_not_retried(monkeypatch):
    """The other half of the control flow: the branch/commit existence probes."""
    mod = _load_module()
    calls = _stub_one_call(monkeypatch, mod, _http_error(404))

    with pytest.raises(urllib.error.HTTPError):
        mod.api("GET", "/repos/x/git/refs/heads/feature/x")

    assert calls["urlopen"] == 1
    assert calls["sleep"] == []


def test_a_transient_status_is_still_retried(monkeypatch):
    """The control: the fix does not turn a flaky server into an immediate failure."""
    mod = _load_module()
    calls = _stub_one_call(monkeypatch, mod, _http_error(503))

    with pytest.raises(urllib.error.HTTPError):
        mod.api("GET", "/repos/x/git/refs/heads/y")

    assert calls["urlopen"] == 3
    assert calls["sleep"] == [5, 10]


def test_a_network_failure_is_still_retried(monkeypatch):
    """And the handler that has to stay: a reset/timeout is not an answer at all."""
    mod = _load_module()
    calls = _stub_one_call(monkeypatch, mod, urllib.error.URLError("reset by peer"))

    with pytest.raises(urllib.error.URLError):
        mod.api("GET", "/repos/x/git/refs/heads/y")

    assert calls["urlopen"] == 3
    assert calls["sleep"] == [5, 10]


def test_without_a_token_a_401_falls_back_to_gh(monkeypatch):
    """The branch the handler order had made unreachable."""
    mod = _load_module()
    monkeypatch.setattr(mod, "_auth_token", lambda: None)
    calls = _stub_one_call(monkeypatch, mod, _http_error(401))
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, '{"sha": "abc"}', "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod.api("GET", "/repos/x/git/refs/heads/y") == {"sha": "abc"}
    assert calls["urlopen"] == 1
    assert seen["argv"][:3] == ["gh", "api", "-X"]


# ------------------------------------------------- what a push uploads


def test_a_push_onto_an_existing_remote_base_uploads_only_what_is_missing(tmp_path):
    """A one-file change must not re-upload every object in the tree.

    Measured 2026-10-16 (`cyc20261016-183420`) on the release branch: the tool
    uploaded 745 objects (707 blobs + 38 trees) for an 8-file version bump and did
    not finish inside a 10-minute window, where the objects the remote's own base
    commit does not already carry are 11. Everything reachable from the base is
    already there - git shas are content-addressed and the remote serves that commit
    - so the upload is a POST whose answer is known.
    """
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "a/b.txt", b"nested\n")
    _write_file(repo, "top.txt", b"top\n")
    base = _commit_all(repo, "root commit")
    _commit_all(repo, "child commit", new_file="c.txt")

    fake = FakeGitHub(repo)
    fake.refs["refs/heads/feature/test"] = base   # the remote already serves the base
    fake.objects[base] = ("commit", b"<pre-existing>")

    orig_api = mod.api
    mod.api = fake
    try:
        result = mod.push_branch("x", "feature/test", "HEAD", False, cwd=repo)
    finally:
        mod.api = orig_api

    assert result["result"] == "pushed"
    assert result["content_identical"] is True

    blob_posts = [p for m, p in fake.log if m == "POST" and p == "/repos/x/git/blobs"]
    assert len(blob_posts) == 1, (
        f"a one-file change against a base the remote already has uploaded "
        f"{len(blob_posts)} blob(s) - re-uploading the whole tree is what made this "
        "tool unusable inside a 10-minute window"
    )
    # The control, so the assertion above cannot be satisfied by uploading nothing:
    # the tree the change produced still has to be created, and the new blob with it.
    assert [p for m, p in fake.log if m == "POST" and p == "/repos/x/git/trees"], (
        "the new tree was never created"
    )
    assert result["sha"] in fake.objects, "the new commit was never created"


# ---------------------------------------------------------------- the tag path
#
# Stage 2 of a release is a tag push, and a tag push is the only trigger of
# build-release.yml. The branch path exists so an outage does not mean
# re-deriving a script from notes; the tag path had no home in the repository at
# all, so every outage re-derived one and nothing tested it (#2795). These pin
# the four refusals and the one order that make the tag path safe to run during a
# release: refuse before the network, create the object before the ref, and never
# move a published tag.


def _tag_object(repo: Path, name: str = "v9.9.9") -> str:
    r = _git("tag", "-a", name, "-m", f"emrg {name}", "HEAD", cwd=repo, env=GIT_ENV)
    assert r.returncode == 0, r.stderr
    return _git("rev-parse", f"refs/tags/{name}", cwd=repo).stdout.decode().strip()


def _with_fake(mod, fake):
    orig = mod.api
    mod.api = fake
    return orig


def test_push_tag_creates_the_tag_object_before_the_ref(tmp_path):
    """The ref must be created last, pointing at the object just created, and the
    tagger's own instant and offset must reach the payload - a tag stamped "now"
    is a different object from the one that was cut."""
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "a.txt")
    commit = _commit_all(repo, "root")
    _tag_object(repo)
    fake = FakeGitHub(repo)
    orig = _with_fake(mod, fake)
    try:
        result = mod.push_tag("x", "v9.9.9", cwd=repo)
    finally:
        mod.api = orig

    assert result["result"] == "pushed"
    assert result["commit"] == commit
    assert fake.refs["refs/tags/v9.9.9"] == result["tag_object"], (
        "the ref does not point at the tag object that was created"
    )
    posts = [(m, p) for m, p in fake.log if m == "POST"]
    assert posts == [("POST", "/repos/x/git/tags"), ("POST", "/repos/x/git/refs")], (
        f"unexpected request order/set: {posts} - the ref has to be created after "
        "the object it names, and nothing else is uploaded for a tag"
    )
    assert fake.tag_payloads, "no tag payload was posted"
    payload = fake.tag_payloads[0]
    assert payload["object"] == commit and payload["type"] == "commit"
    assert payload["tag"] == "v9.9.9"
    # GIT_ENV stamps 1700000000 +0800 in every zone: the offset is what is asserted,
    # not the instant's spelling (an offset-carrying date is the whole point).
    assert payload["tagger"]["date"] == "2023-11-15T06:13:20+08:00", (
        f"the tagger offset was not preserved: {payload['tagger']['date']!r}"
    )


def test_a_lightweight_tag_is_refused_before_any_request(tmp_path):
    """A lightweight tag would pass here and be refused by build-release.yml's
    verify-tag job only after a four-platform build was spent, so the refusal
    belongs in front of the first request."""
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "a.txt")
    _commit_all(repo, "root")
    r = _git("tag", "light", "HEAD", cwd=repo)   # no -a: a lightweight tag
    assert r.returncode == 0, r.stderr
    fake = FakeGitHub(repo)
    orig = _with_fake(mod, fake)
    try:
        with pytest.raises(mod.PushError) as exc:
            mod.push_tag("x", "light", cwd=repo)
    finally:
        mod.api = orig

    assert "annotated" in str(exc.value)
    assert fake.log == [], f"a lightweight tag still made {fake.log} request(s)"


def test_push_tag_refuses_to_move_a_published_tag(tmp_path):
    """A version number is not a branch: an existing tag naming another commit is
    refused, and nothing is posted."""
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "a.txt")
    _commit_all(repo, "root")
    _tag_object(repo)
    other = _commit_all(repo, "later", new_file="b.txt")
    fake = FakeGitHub(repo)
    fake.refs["refs/tags/v9.9.9"] = other      # the published tag names another commit
    orig = _with_fake(mod, fake)
    try:
        with pytest.raises(mod.PushError) as exc:
            mod.push_tag("x", "v9.9.9", cwd=repo)
    finally:
        mod.api = orig

    assert "refusing to move" in str(exc.value)
    assert [p for m, p in fake.log if m == "POST"] == [], "a refused tag still posted"
    assert fake.refs["refs/tags/v9.9.9"] == other, "the published tag was moved anyway"


def test_push_tag_is_a_no_op_when_the_ref_already_names_that_commit(tmp_path):
    """Re-running stage 2 after a dropped connection must not be an error."""
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "a.txt")
    commit = _commit_all(repo, "root")
    _tag_object(repo)
    fake = FakeGitHub(repo)
    fake.refs["refs/tags/v9.9.9"] = commit
    orig = _with_fake(mod, fake)
    try:
        result = mod.push_tag("x", "v9.9.9", cwd=repo)
    finally:
        mod.api = orig

    assert result["result"] == "no-op"
    assert result["commit"] == commit
    assert [p for m, p in fake.log if m == "POST"] == [], "a no-op still posted an object"


def test_the_cli_takes_exactly_one_of_branch_or_tag(tmp_path):
    """Neither or both is a usage error, reported before any git or network call -
    the check has to sit in front of the origin lookup, which needs a repository."""
    import sys

    for argv in ([], ["--branch", "feature/x", "--tag", "v9.9.9"]):
        r = subprocess.run([sys.executable, str(SCRIPT)] + argv, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           cwd=tmp_path, timeout=120)
        assert r.returncode == 2, (argv, r.returncode, r.stdout, r.stderr)
        assert "exactly one of --branch" in r.stdout, (argv, r.stdout)


# -------------------------------- a publication stranded one request short


def test_a_retry_finishes_when_the_remote_already_holds_the_tip(tmp_path):
    """One outstanding request must complete, not report an internal fault.

    Measured 2026-11-11 (issue #2819): a push refused at its ref write leaves exactly this
    state - `POST /git/commits` has already stored the commit, so every retry finds the tip
    on the remote with nothing left to upload and only the ref write outstanding.
    """
    mod = _load_module()
    repo = _init_repo(tmp_path)
    _write_file(repo, "docs/base.md", b"base\n")
    root = _commit_all(repo, "base commit")
    tip = _commit_all(repo, "second commit", new_file="docs/tip.md")
    fake = FakeGitHub(repo)
    fake.refs["refs/heads/feature/stranded"] = root
    # the remote already carries the tip's commit: it was uploaded before the refusal
    fake.objects[tip] = ("commit", b"<already on the remote>")

    orig_api = mod.api
    mod.api = fake
    try:
        result = mod.push_branch("x", "feature/stranded", "HEAD", False, cwd=repo)
    finally:
        mod.api = orig_api

    assert result["result"] == "pushed"
    assert result["sha"] == tip
    assert result["content_identical"] is True
    assert fake.refs["refs/heads/feature/stranded"] == tip
    # the content was already there: re-uploading it is a request whose answer is known
    uploads = [p for method, p in fake.log if method == "POST"]
    assert uploads == [], f"a stranded publication uploaded again: {uploads}"
