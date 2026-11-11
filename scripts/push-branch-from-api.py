#!/usr/bin/env python3
"""Push a local branch to GitHub via the Git Data API when git-over-https is down.

Counterpart of scripts/sync-master-from-api.py (fetch direction). EMRG repeatedly
hits github.com:443 unreachable while api.github.com stays up (10+ documented
cycles, 08-22..08-26). When a fix must be pushed during an outage, the old flow
was a hand-written ephemeral script re-derived each time from memory notes -
cycle 2026-08-26 00:59 recorded four gotchas learned the hard way:

  1. blobs must be uploaded from the *committed* object bytes (`git cat-file
     blob <sha>`), not working-tree bytes (CRLF normalization differs)
  2. `git ls-tree` needs `-r` to enumerate nested paths
  3. the API displays dates as UTC ('Z') but stores the raw offset (+0800) -
     recreate commits with the original epoch+offset or the sha will not match
  4. subprocess text I/O must use encoding='utf-8', errors='replace' (GBK
     console crashes on non-ASCII commit messages)

This script automates that recipe:

  * walks the local chain from <ref> (default HEAD) down to the remote base -
    the existing branch head, or the first ancestor the remote already has
    (GET /repos/{repo}/commits/{sha})
  * uploads blobs (raw bytes, byte-exact) and trees (structured entries,
    children referenced by their *remote* sha) bottom-up - **only the ones the
    remote base does not already carry**: everything reachable from the base
    commit's tree is there by definition, so re-uploading it is a request whose
    answer is known (an 8-file bump re-uploaded 745 objects before this, and did
    not finish inside a 10-minute window)
  * recreates commits via the structured endpoint, deriving author/committer
    name, email and epoch+offset from the local raw object (gotcha 3), with the
    message passed without a trailing newline
  * updates the remote ref (create, or fast-forward; force only with --force)
  * rewrites the local branch ref to the remote sha (content identical) and
    verifies: GET refs == local rev-parse, and `git diff` of the remote sha
    against the original local tip is empty

The tag direction is the release's stage 2 and has the same problem: a tag push is
the only trigger of build-release.yml, so an outage with no maintained tag path
means re-deriving one from notes every release, untested. `--tag` publishes an
annotated tag through the same two endpoints a release needs:

  * `git cat-file -t refs/tags/<name>` must answer `tag` - a lightweight tag is
    refused before a single request, because build-release.yml's verify-tag job
    requires an annotated tag and would refuse it only after the build was spent
  * the tag object is created with POST /git/tags, carrying the tagger's own
    instant **and offset** (a tag stamped "now" is a different object)
  * refs/tags/<name> is created with POST /git/refs - creating that ref is what
    fires the tag workflow, so this is the whole of stage 2
  * an existing tag at a different commit is refused rather than moved: a
    published version number is not a branch to rewrite

Usage:
    python scripts/push-branch-from-api.py [--repo owner/name] [--branch feature/x]
                                           [--ref HEAD] [--force]
    python scripts/push-branch-from-api.py [--repo owner/name] --tag v0.3.48

Requirements: git on PATH; api.github.com reachable; gh CLI or GH_TOKEN/GITHUB_TOKEN
auth (private repos need it; public repos work anonymously but rate-limit).
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

#: The HTTP statuses worth a retry: GitHub's own transient answers. Every other
#: status is the reply the caller asked for - `404` and `422` are how this tool
#: asks "is this object on the remote yet?", so retrying them retries the answer.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

_TOKEN: str | None = None  # resolved once by _auth_token(), held in memory only

_AUTHOR_RE = re.compile(r"^(.*) <(.*)> (\d+) ([+-]\d{4})$")


def git(*args: str, cwd: str | None = None) -> bytes:
    """Run git with UTF-8 text I/O (gotcha 4: GBK console)."""
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    r = subprocess.run(["git"] + list(args), capture_output=True, cwd=cwd,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.encode("utf-8")


def _auth_token() -> str | None:
    """Resolve a GitHub token once: env var, else `gh auth token` (read into
    memory only — never printed). Falls back to anonymous when unavailable."""
    global _TOKEN
    if _TOKEN is None:
        t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not t:
            try:
                out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                     text=True, timeout=15, encoding="utf-8",
                                     errors="replace")
                t = out.stdout.strip() if out.returncode == 0 else None
            except Exception:
                t = None
        _TOKEN = t or ""
    return _TOKEN or None


def api(method: str, path: str, body: dict | None = None) -> dict:
    """Call the GitHub REST API, authenticated when a token is available.

    A transient failure - a timeout, a reset, or one of GitHub's own transient
    statuses - is retried up to 3 times with backoff. Every other HTTP status is
    the answer the caller asked for and is raised at once: the caller reads 404
    and 422 as control flow ("this commit is not on the remote yet"), so spending
    5 s + 10 s before handing them over is time bought for nobody.
    """
    url = API + path
    headers = {"User-Agent": "emrg-push-branch-from-api",
               "Accept": "application/vnd.github+json"}
    token = _auth_token()
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            # `HTTPError` IS a `URLError`, so this handler must come first. With the
            # network handler ahead of it this branch was unreachable and every 4xx was
            # retried three times with 5 s + 10 s of backoff (measured 2026-10-16,
            # `cyc20261016-183420`: a 422 answered in 0.5 s took 17.5 s), which the
            # caller pays on exactly the probes it makes by design.
            if e.code in (401, 403) and not token:
                out = subprocess.run(["gh", "api", "-X", method, path,
                                      "--input", "-"], input=json.dumps(body or {}),
                                     capture_output=True, text=True, timeout=60,
                                     encoding="utf-8", errors="replace")
                if out.returncode == 0 and out.stdout.strip():
                    return json.loads(out.stdout)
            if e.code in RETRYABLE_STATUSES and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError as e:
            # Not an HTTP answer at all: a timeout, a reset, a DNS failure - the
            # failures of a flaky link rather than the server's reply.
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")  # pragma: no cover


def parse_commit(raw: bytes) -> dict:
    """Parse a raw git commit object into structured fields for the API.

    Raw object layout: header lines (tree/parent/author/committer[/gpgsig]),
    blank line, message (no trailing newline in the object). The epoch+offset
    from the author/committer lines is preserved as an ISO-8601 datetime with
    the original offset — the API stores the offset even though it displays
    the date as UTC (gotcha 3).
    """
    header, sep, message = raw.partition(b"\n\n")
    if not sep:
        raise ValueError("malformed commit object: no blank line before message")
    fields = {"parents": [], "message": message.decode("utf-8")}
    for line in header.decode("utf-8").splitlines():
        if line.startswith("tree "):
            fields["tree"] = line.split()[1]
        elif line.startswith("parent "):
            fields["parents"].append(line.split()[1])
        elif line.startswith("author "):
            fields["author"] = _parse_person(line[len("author "):])
        elif line.startswith("committer "):
            fields["committer"] = _parse_person(line[len("committer "):])
    if "tree" not in fields or "author" not in fields or "committer" not in fields:
        raise ValueError("malformed commit object: missing tree/author/committer")
    return fields


def _parse_person(line: str) -> dict:
    m = _AUTHOR_RE.match(line)
    if not m:
        raise ValueError(f"cannot parse identity line: {line!r}")
    name, email, epoch, offset = m.groups()
    tz = _dt.timezone(_dt.timedelta(hours=int(offset[:3]), minutes=int(offset[3:])))
    dt = _dt.datetime.fromtimestamp(int(epoch), tz)
    return {"name": name, "email": email, "date": dt.isoformat()}


def _raw_commit(payload: dict) -> bytes:
    """Rebuild the raw commit object GitHub stored for a create-commit payload.

    GitHub stores author/committer with the original epoch+offset (gotcha 3)
    and the message without a trailing newline; rebuilding from the payload
    reproduces the exact raw bytes, so `git hash-object -w` yields the same
    sha the API returned — which lets `git update-ref` point at it locally.
    """
    def person(p):
        d = _dt.datetime.fromisoformat(p["date"])
        return f"{p['name']} <{p['email']}> {int(d.timestamp())} {d.strftime('%z')}"

    lines = [f"tree {payload['tree']}"]
    lines += [f"parent {p}" for p in payload.get("parents", [])]
    lines.append(f"author {person(payload['author'])}")
    lines.append(f"committer {person(payload['committer'])}")
    return ("\n".join(lines) + "\n\n" + payload["message"]).encode("utf-8")


def collect_objects(commit: str, cwd: str | None = None) -> dict:
    """Collect every object the commit needs: blobs and all trees (any depth).

    `git ls-tree -r -t <commit>` lists tree entries at all depths plus blob
    entries; we return them as {sha: path} so upload order can be bottom-up.
    """
    out = git("ls-tree", "-r", "-t", commit, cwd=cwd).decode("utf-8")
    blobs, trees = {}, {}
    trees[commit] = ""  # ls-tree -r -t lists subtrees but not the root itself
    for line in out.splitlines():
        mode, typ, sha, path = line.split(None, 3)
        if typ == "blob":
            blobs[sha] = path
        elif typ == "tree":
            trees[sha] = path
    return {"blobs": blobs, "trees": trees}


def tree_entries(sha: str, cwd: str | None = None) -> list[dict]:
    """Immediate children of a tree, as GitHub create-tree API entries."""
    out = git("ls-tree", sha, cwd=cwd).decode("utf-8")
    entries = []
    for line in out.splitlines():
        mode, typ, child_sha, path = line.split(None, 3)
        entries.append({"path": path, "mode": mode, "type": typ, "sha": child_sha})
    return entries


class PushError(RuntimeError):
    pass


#: A commit touching this directory needs the token's `workflow` scope to be published.
WORKFLOWS_DIR = ".github/workflows/"


def ref_write_404_hint(paths: list[str] | None, response_body: str = "") -> str:
    """Say what a `404` from a ref write leaves out — and no more than the evidence says.

    The endpoint's documented refusals all answer `422` (an existing ref, an
    unresolvable sha, an invalid ref name), so a `404` on this request is a refusal
    GitHub does not describe. One of them is measurable from here: a commit that
    touches `.github/workflows/` cannot be published by an OAuth app whose token lacks
    the `workflow` scope, and that refusal is reported as `404` rather than `403` —
    measured 2026-11-11, where `git push` of the same commit answered `refusing to
    allow an OAuth App to create or update workflow .github/workflows/test.yml without
    'workflow' scope` while this tool printed `Not Found` and sent the reader after a
    branch name that was spelled correctly.

    The hint is therefore conditional on the chain, not on the status: claiming that
    cause for a chain that publishes no workflow file would point the reader at a scope
    that was never the problem, which is worse than the bare status.

    `paths` is `None` when the caller could not measure what the publication changes -
    and then this function must not answer as if it had, because "I compared nothing"
    and "no workflow path" are the same empty list to a reader.
    """
    if paths is None:
        return (
            "this endpoint answers 404 for refusals it does not describe, while its "
            "documented ones answer 422. What this publication would change could not be "
            "measured from this checkout, so a missing `workflow` scope cannot be ruled "
            f"out: a commit touching {WORKFLOWS_DIR} needs that scope and is refused as "
            "404 rather than 403. Settle it with `git diff --name-only <the commit the "
            "ref names> <tip>` before renewing a scope. "
            f"Response body: {response_body or '(empty)'}"
        )
    if any(str(p).startswith(WORKFLOWS_DIR) for p in paths):
        return (
            f"a commit in this chain changes a path under {WORKFLOWS_DIR}, and an OAuth "
            "app whose token lacks the `workflow` scope is refused exactly at this "
            "request — `git push` names that scope outright, while this endpoint answers "
            "404. The remedy is `gh auth refresh -s workflow`, which needs a browser and "
            "so cannot be run from an automated session. Nothing was published"
        )
    return (
        "this endpoint answers 404 for refusals it does not describe, while its "
        "documented ones answer 422 — the ref name and the objects were accepted, so "
        "read this as a permission or ruleset refusal rather than a missing branch. "
        f"Response body: {response_body or '(empty)'}"
    )


def push_branch(repo: str, branch: str, ref: str, force: bool, cwd: str | None = None) -> dict:
    """Upload objects and advance the remote branch; returns result summary."""
    local_tip = git("rev-parse", ref, cwd=cwd).decode("utf-8").strip()
    if git("cat-file", "-t", local_tip, cwd=cwd).decode("utf-8").strip() != "commit":
        raise PushError(f"ref {ref} does not resolve to a commit")

    # ---- find the remote base: existing branch head, or first known ancestor
    ref_path = f"/repos/{repo}/git/refs/heads/{branch}"
    ref_sha = None  # the commit the ref itself names, kept apart from the walk's cursor
    try:
        existing = api("GET", ref_path)
        ref_sha = base = existing["object"]["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        existing = None
        base = None
        cur = local_tip
        while True:
            try:
                api("GET", f"/repos/{repo}/commits/{cur}")
                base = cur
                break
            except urllib.error.HTTPError as e2:
                if e2.code not in (404, 422):
                    raise
            raw = git("cat-file", "commit", cur, cwd=cwd)
            parents = parse_commit(raw)["parents"]
            if not parents:
                break
            cur = parents[0]
    # A ref that already names the tip is a genuine no-op. The *absence* of a ref is not
    # one: when the walk found the tip on the remote with no ref naming it, exactly one
    # request is outstanding - the ref write - and answering "no-op" would report a
    # publication as already done while the branch does not exist. That is the shape a
    # refused `POST /git/refs` leaves behind, since the commit is created before the ref
    # is written; measured 2026-11-11 on the retry of such a push.
    if ref_sha is not None and ref_sha == local_tip:
        return {"result": "no-op", "branch": branch, "sha": local_tip}

    # ---- collect the missing chain, oldest first. Probe each ancestor against
    # the remote: a commit already present remotely is the real base, which
    # keeps fast-forward pushes after an amend cheap (upload only new commits)
    # even when the branch head is an API-normalized commit not in local history
    chain: list[tuple[str, dict]] = []  # (local_sha, parsed_commit)
    cur = local_tip
    while cur and cur != base:
        try:
            api("GET", f"/repos/{repo}/commits/{cur}")
            base = cur  # already on the remote -> walk stops here
            break
        except urllib.error.HTTPError as e:
            if e.code not in (404, 422):
                raise
        raw = git("cat-file", "commit", cur, cwd=cwd)
        chain.append((cur, parse_commit(raw)))
        parents = chain[-1][1]["parents"]
        if not parents:
            break
        cur = parents[0] if parents[0] != base else None
    chain.reverse()
    # An empty chain after a walk that found the *tip itself* on the remote is not a
    # defect: the objects and commits are already published and only a ref is missing.
    # That is precisely the state a refusal at the ref step leaves behind - a
    # non-fast-forward, a ruleset, a token missing a scope - because the commit is created
    # before the ref is written. Refusing it here makes the publication unrecoverable:
    # measured 2026-11-11, where re-running a push whose ref write had been refused
    # answered `internal: empty commit chain` while exactly one request remained. Proceed
    # with nothing to upload and let the ref write finish the publication, or report why
    # it cannot.
    if not chain and base != local_tip:
        raise PushError("internal: empty commit chain")

    print(f"  base: {base or '(new branch - nothing on remote yet)'}")
    print(f"  uploading {len(chain)} commit(s)...", flush=True)

    # ---- upload objects bottom-up, mapping local -> remote shas
    obj_map: dict[str, str] = {}
    if base:
        obj_map[base] = base
        # Everything reachable from the base commit's tree is already on the remote:
        # git shas are content-addressed, and `base` is only ever set from an existing
        # branch ref or a `GET /commits/{sha}` the remote answered - so re-uploading
        # that half is a POST whose answer is known before it is sent. Measured
        # 2026-10-16 (`cyc20261016-183420`): an 8-file version bump uploaded 745
        # objects (707 blobs + 38 trees) and did not finish inside a 10-minute window,
        # where the objects the base does not already carry are **11**. This walk costs
        # 0.02 s on that tree.
        #
        # Named limit: this holds while the remote is a complete repository, which is
        # what makes "the base's objects are on the remote" true. Against a remote
        # missing part of its own history the skip could omit an object it lacks, and
        # nothing would say so - GitHub's create-tree accepts entries it cannot resolve.
        for line in git(
            "rev-list", "--objects", f"{base}^{{tree}}", cwd=cwd
        ).decode("utf-8").splitlines():
            present = line.split(" ", 1)[0]
            if present:
                obj_map[present] = present

    def remote_sha(sha: str, kind: str) -> str:
        if sha in obj_map:
            return obj_map[sha]
        if kind == "blob":
            content = git("cat-file", "blob", sha, cwd=cwd)
            try:
                resp = api("POST", f"/repos/{repo}/git/blobs",
                           {"content": base64.b64encode(content).decode("ascii"),
                            "encoding": "base64"})
            except urllib.error.HTTPError as e:
                if e.code == 422:  # already exists
                    resp = api("GET", f"/repos/{repo}/git/blobs/{sha}")
                else:
                    raise
        else:  # tree
            entries = tree_entries(sha, cwd=cwd)
            for e in entries:
                if e["type"] != "blob":
                    e["sha"] = remote_sha(e["sha"], "tree")
                else:
                    e["sha"] = remote_sha(e["sha"], "blob")
            try:
                resp = api("POST", f"/repos/{repo}/git/trees", {"tree": entries})
            except urllib.error.HTTPError as e:
                if e.code == 422:
                    resp = api("GET", f"/repos/{repo}/git/trees/{sha}")
                else:
                    raise
        rsha = resp["sha"]
        obj_map[sha] = rsha
        return rsha

    for commit in chain:
        objs = collect_objects(commit[1]["tree"], cwd=cwd)
        for bsha in sorted(objs["blobs"], key=lambda s: objs["blobs"][s]):
            remote_sha(bsha, "blob")
        # deepest paths first so children exist before parents
        # (ls-tree -r -t includes the root tree at path "" — covered here)
        for tsha in sorted(objs["trees"], key=lambda s: (objs["trees"][s].count("/"), objs["trees"][s]), reverse=True):
            remote_sha(tsha, "tree")

    # ---- create commits oldest -> newest; a commit's parents must reference
    # the *remote* shas of already-created commits (they differ from the local
    # shas when GitHub normalizes the message), so track local->remote mapping
    commit_map: dict[str, str] = {}
    payloads: list[dict] = []  # parallel to chain — needed for local materialization
    for local_sha, commit in chain:
        payload = {
            # GitHub's create-commit stores the message without a trailing
            # newline (observed cycle 2026-08-26 00:59) — normalize here so the
            # payload reflects the object that will actually be stored
            "message": commit["message"].rstrip("\n"),
            "tree": obj_map.get(commit["tree"], commit["tree"]),
            "parents": [commit_map.get(p) or obj_map.get(p, p) for p in commit["parents"]],
            "author": commit["author"],
            "committer": commit["committer"],
        }
        try:
            resp = api("POST", f"/repos/{repo}/git/commits", payload)
        except urllib.error.HTTPError as e:
            if e.code == 422:
                raise PushError("commit creation rejected by API (no refs touched "
                                "remotely) — check author/committer dates (must "
                                "carry the original +0800 offset, gotcha 3): "
                                + e.read().decode("utf-8", "replace")[:300])
            raise
        commit_map[local_sha] = resp["sha"]
        payloads.append(payload)

    # ---- update the remote ref (fast-forward semantics; force only if asked)
    final_sha = commit_map[chain[-1][0]] if chain else local_tip

    def changed_paths() -> list[str] | None:
        """Paths this publication changes, so a refusal can be explained from evidence.

        The subject of this diff is not the ancestor walk's cursor. `base` is where that
        walk stopped, and a walk that finds the tip already on the remote stops *at* the
        tip - so diffing `base..local_tip` compares the tip with itself and answers
        "nothing". That is a true comparison and a false finding: measured 2026-11-11 on
        the retry of a refused push whose commit edits `.github/workflows/`, where the
        empty diff made the refusal diagnose itself as "not a missing branch" and exclude
        the one cause the hint exists to name.

        What the publication actually changes is, in order of preference: the commit the
        ref names (what a new ref value changes from), or the ancestor the chain was built
        from. Both are exact. When nothing was uploaded there is no such anchor, and the
        tip's own parent is only a **lower bound**: a refused attempt uploaded every commit
        of its publication, so a retried push of three commits whose workflow edit is the
        middle one is bounded by the last commit alone. A lower bound that names a workflow
        path is still a cause; one that names nothing proves nothing, so it answers `None`
        — *unmeasured* — rather than the negative a reader would act on.
        """
        subjects = []
        if ref_sha:
            subjects.append(ref_sha)
        if base and base != local_tip:
            subjects.append(base)
        for subject in subjects:
            try:
                out = git("diff", "--name-only", subject, local_tip, cwd=cwd)
            except RuntimeError:
                continue  # not an object in this checkout - try the next reading
            return out.decode("utf-8").splitlines()
        if chain:
            # no base to diff against: every path in the uploaded chain is new by definition
            return [p for commit in chain
                    for p in collect_objects(commit[1]["tree"], cwd=cwd)["blobs"].values()]
        if not subjects:
            # nothing was uploaded and no anchor was measured: the tip's parent bounds the
            # publication from below, and only a workflow path inside that bound is a cause
            parents = parse_commit(git("cat-file", "commit", local_tip, cwd=cwd))["parents"]
            if parents:
                out = git("diff", "--name-only", parents[0], local_tip, cwd=cwd)
                bound = out.decode("utf-8").splitlines()
                if any(str(p).startswith(WORKFLOWS_DIR) for p in bound):
                    return bound
        return None

    body = {"sha": final_sha, "force": force}
    try:
        if existing:
            api("PATCH", ref_path, body)
        else:
            try:
                api("POST", f"/repos/{repo}/git/refs",
                    {"ref": f"refs/heads/{branch}", "sha": final_sha})
            except urllib.error.HTTPError as e:
                if e.code == 422 and "Reference already exists" in e.read().decode("utf-8", "replace"):
                    api("PATCH", ref_path, body)  # raced with another pusher
                else:
                    raise
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise PushError(
                "remote ref write answered 404 (no refs touched) — "
                + ref_write_404_hint(changed_paths(),
                                     e.read().decode("utf-8", "replace")[:300])
            ) from e
        if e.code == 422 and not force:
            raise PushError("remote ref update rejected (non-fast-forward?) — "
                            "use --force only if you know the remote is stale (no refs touched)") from e
        raise

    # ---- materialize EVERY created remote commit locally (not just the head),
    # so `git update-ref` works and `git log` can traverse the new branch head
    # through the API-normalized intermediate commits; then rewrite the local
    # branch ref to the remote sha and verify
    for (local_sha, _commit), payload in zip(chain, payloads):
        raw = _raw_commit(payload)
        r = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                           input=raw, capture_output=True, cwd=cwd)
        computed = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else "?"
        expected = commit_map[local_sha]
        if r.returncode != 0 or computed != expected:
            raise PushError(f"remote ref already updated to {final_sha} but local "
                            f"materialization of {local_sha} produced {computed} "
                            f"(expected {expected}) — run "
                            f"'git fetch origin {branch}' to sync locally")
    git("update-ref", f"refs/heads/{branch}", final_sha, cwd=cwd)
    confirmed = api("GET", ref_path)["object"]["sha"]
    if confirmed != final_sha:
        raise PushError(f"verification failed: remote ref {confirmed} != {final_sha}")
    diff = git("diff", "--stat", final_sha, local_tip, cwd=cwd)
    return {"result": "pushed", "branch": branch, "sha": final_sha,
            "original_local_tip": local_tip,
            "content_identical": not diff.strip()}


def parse_tag(raw: bytes) -> dict:
    """Parse a raw annotated tag object into the payload POST /git/tags takes.

    Layout: object/type/tag/tagger header lines, a blank line, then the message.
    The tagger's epoch+offset is preserved as ISO-8601 carrying that offset, for
    the same reason `parse_commit` preserves the author's: the API stores the
    offset it is given, and a tag stamped "now" is not the object that was cut.
    """
    header, sep, message = raw.partition(b"\n\n")
    if not sep:
        raise ValueError("malformed tag object: no blank line before message")
    fields = {}
    for line in header.decode("utf-8").splitlines():
        key, _, value = line.partition(" ")
        fields[key] = value
    for required in ("object", "type", "tag", "tagger"):
        if required not in fields:
            raise ValueError(f"malformed tag object: missing {required}")
    return {"object": fields["object"], "type": fields["type"],
            "tag": fields["tag"], "tagger": _parse_person(fields["tagger"]),
            "message": message.decode("utf-8").rstrip("\n")}


def _remote_tag_target(repo: str, tag: str) -> str | None:
    """The commit refs/tags/<tag> names on the remote, or None when it is absent."""
    try:
        ref = api("GET", f"/repos/{repo}/git/ref/tags/{tag}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    sha = ref["object"]["sha"]
    try:
        return api("GET", f"/repos/{repo}/git/tags/{sha}")["object"]["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return sha     # a lightweight remote tag holds the commit itself
        raise


def push_tag(repo: str, tag: str, cwd: str | None = None) -> dict:
    """Publish an annotated tag through the Git Data API.

    Creating refs/tags/<name> is what fires the tag workflow, so this is the
    release's stage 2 when git-over-https is down. Everything that can be refused
    is refused before the first request: a lightweight tag (the release pipeline
    requires an annotated one), a missing local tag, and an already-published tag
    naming a different commit - a version number is not a branch to rewrite.
    """
    ref = f"refs/tags/{tag}"
    try:
        kind = git("cat-file", "-t", ref, cwd=cwd).decode("utf-8").strip()
    except RuntimeError as e:
        raise PushError(f"{ref} is not present locally: {e}") from None
    if kind != "tag":
        raise PushError(f"{ref} is a {kind}, not an annotated tag object; the release "
                        "pipeline requires an annotated tag, so nothing was sent")
    payload = parse_tag(git("cat-file", "tag", ref, cwd=cwd))
    commit = payload["object"]

    existing = _remote_tag_target(repo, tag)
    if existing == commit:
        return {"result": "no-op", "tag": tag, "commit": commit}
    if existing is not None:
        raise PushError(f"{ref} on the remote already names {existing}; refusing to move "
                        "a published tag - cut a new version instead")

    created = api("POST", f"/repos/{repo}/git/tags", payload)["sha"]
    api("POST", f"/repos/{repo}/git/refs", {"ref": ref, "sha": created})

    confirmed = api("GET", f"/repos/{repo}/git/ref/tags/{tag}")["object"]["sha"]
    if confirmed != created:
        raise PushError(f"verification failed: {ref} reads {confirmed}, not {created}")
    return {"result": "pushed", "tag": tag, "tag_object": created, "commit": commit}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=None, help="owner/name (default: from origin)")
    ap.add_argument("--branch", default=None, help="remote branch name, e.g. feature/x")
    ap.add_argument("--tag", default=None,
                    help="annotated tag to publish, e.g. v0.3.48 (release stage 2)")
    ap.add_argument("--ref", default="HEAD", help="local ref to push (default: HEAD)")
    ap.add_argument("--force", action="store_true",
                    help="force-update the remote ref (default: fail on conflict)")
    args = ap.parse_args()

    if bool(args.branch) == bool(args.tag):
        print("pass exactly one of --branch <name> or --tag <name>")
        return 2

    repo = args.repo
    if not repo:
        out = git("remote", "get-url", "origin").decode("utf-8").strip()
        m = re.search(r"[:/]([^:/]+/[^/]+?)(?:\.git)?$", out)
        if not m:
            print(f"cannot infer repo from origin {out!r}; pass --repo owner/name")
            return 2
        repo = m.group(1)

    try:
        if args.tag:
            result = push_tag(repo, args.tag)
        else:
            result = push_branch(repo, args.branch, args.ref, args.force)
    except PushError as e:
        print(f"push aborted: {e}")
        return 1
    except urllib.error.HTTPError as e:
        print(f"push aborted (HTTP {e.code}): {e.read().decode('utf-8', 'replace')[:400]}")
        return 1
    except urllib.error.URLError as e:
        print(f"push aborted (network error after retries): {e}")
        return 1

    if args.tag:
        print(f"repo {repo} tag {args.tag}")
        print(f"  release commit: {result['commit']}")
        if result["result"] == "no-op":
            print("  nothing to do - refs/tags/ already names that commit")
        else:
            print(f"  tag object: {result['tag_object']}")
            print(f"  created refs/tags/{args.tag} - creating it fires the tag workflow")
        return 0

    print(f"repo {repo} branch {args.branch}")
    print(f"  remote sha: {result['sha']}")
    if result["result"] == "no-op":
        print("  nothing to do - ref already at that commit")
    else:
        print(f"  original local tip: {result['original_local_tip']}")
        print(f"  content identical to local tip: {result['content_identical']}")
        print(f"  local branch ref updated -> {result['sha']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
