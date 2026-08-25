#!/usr/bin/env python3
"""Push a local branch to GitHub via the Git Data API when git-over-https is down.

Counterpart of scripts/sync-master-from-api.py (fetch direction). EMRG repeatedly
hits github.com:443 unreachable while api.github.com stays up (10+ documented
cycles, 08-22..08-26). When a fix must be pushed during an outage, the old flow
was a hand-written ephemeral script re-derived each time from memory notes —
cycle 2026-08-26 00:59 recorded four gotchas learned the hard way:

  1. blobs must be uploaded from the *committed* object bytes (`git cat-file
     blob <sha>`), not working-tree bytes (CRLF normalization differs)
  2. `git ls-tree` needs `-r` to enumerate nested paths
  3. the API displays dates as UTC ('Z') but stores the raw offset (+0800) —
     recreate commits with the original epoch+offset or the sha will not match
  4. subprocess text I/O must use encoding='utf-8', errors='replace' (GBK
     console crashes on non-ASCII commit messages)

This script automates that recipe:

  * walks the local chain from <ref> (default HEAD) down to the remote base —
    the existing branch head, or the first ancestor the remote already has
    (GET /repos/{repo}/commits/{sha})
  * uploads blobs (raw bytes, byte-exact) and trees (structured entries,
    children referenced by their *remote* sha) bottom-up
  * recreates commits via the structured endpoint, deriving author/committer
    name, email and epoch+offset from the local raw object (gotcha 3), with the
    message passed without a trailing newline
  * updates the remote ref (create, or fast-forward; force only with --force)
  * rewrites the local branch ref to the remote sha (content identical) and
    verifies: GET refs == local rev-parse, and `git diff` of the remote sha
    against the original local tip is empty

Usage:
    python scripts/push-branch-from-api.py [--repo owner/name] [--branch feature/x]
                                           [--ref HEAD] [--force]

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

    Transient network errors (URLError: timeout/reset — github.com and even
    api.github.com are flaky on this host) are retried up to 3 times with
    backoff; HTTP errors are passed through for the caller to handle.
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
        except urllib.error.URLError as e:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and not token:
                out = subprocess.run(["gh", "api", "-X", method, path,
                                      "--input", "-"], input=json.dumps(body or {}),
                                     capture_output=True, text=True, timeout=60,
                                     encoding="utf-8", errors="replace")
                if out.returncode == 0 and out.stdout.strip():
                    return json.loads(out.stdout)
            raise
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


def push_branch(repo: str, branch: str, ref: str, force: bool, cwd: str | None = None) -> dict:
    """Upload objects and advance the remote branch; returns result summary."""
    local_tip = git("rev-parse", ref, cwd=cwd).decode("utf-8").strip()
    if git("cat-file", "-t", local_tip, cwd=cwd).decode("utf-8").strip() != "commit":
        raise PushError(f"ref {ref} does not resolve to a commit")

    # ---- find the remote base: existing branch head, or first known ancestor
    ref_path = f"/repos/{repo}/git/refs/heads/{branch}"
    try:
        existing = api("GET", ref_path)
        base = existing["object"]["sha"]
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
    if base == local_tip:
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
    if not chain:
        raise PushError("internal: empty commit chain")

    print(f"  base: {base or '(new branch — nothing on remote yet)'}")
    print(f"  uploading {len(chain)} commit(s)...", flush=True)

    # ---- upload objects bottom-up, mapping local -> remote shas
    obj_map: dict[str, str] = {}
    if base:
        obj_map[base] = base

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
    last_payload: dict | None = None
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
        last_payload = payload

    # ---- update the remote ref (fast-forward semantics; force only if asked)
    final_sha = commit_map[chain[-1][0]]
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
        if e.code == 422 and not force:
            raise PushError("remote ref update rejected (non-fast-forward?) — "
                            "use --force only if you know the remote is stale (no refs touched)") from e
        raise

    # ---- materialize the remote commit locally so `git update-ref` can point
    # at it (the API-created object does not exist in the local object store),
    # then rewrite the local branch ref to the remote sha and verify
    raw = _raw_commit(last_payload)
    r = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                       input=raw, capture_output=True, cwd=cwd)
    computed = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else "?"
    if r.returncode != 0 or computed != final_sha:
        raise PushError(f"remote ref already updated to {final_sha} but local "
                        f"materialization produced {computed} — run "
                        f"'git fetch origin {branch}' to sync locally")
    git("update-ref", f"refs/heads/{branch}", final_sha, cwd=cwd)
    confirmed = api("GET", ref_path)["object"]["sha"]
    if confirmed != final_sha:
        raise PushError(f"verification failed: remote ref {confirmed} != {final_sha}")
    diff = git("diff", "--stat", final_sha, local_tip, cwd=cwd)
    return {"result": "pushed", "branch": branch, "sha": final_sha,
            "original_local_tip": local_tip,
            "content_identical": not diff.strip()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=None, help="owner/name (default: from origin)")
    ap.add_argument("--branch", required=True, help="remote branch name, e.g. feature/x")
    ap.add_argument("--ref", default="HEAD", help="local ref to push (default: HEAD)")
    ap.add_argument("--force", action="store_true",
                    help="force-update the remote ref (default: fail on conflict)")
    args = ap.parse_args()

    repo = args.repo
    if not repo:
        out = git("remote", "get-url", "origin").decode("utf-8").strip()
        m = re.search(r"[:/]([^:/]+/[^/]+?)(?:\.git)?$", out)
        if not m:
            print(f"cannot infer repo from origin {out!r}; pass --repo owner/name")
            return 2
        repo = m.group(1)

    try:
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

    print(f"repo {repo} branch {args.branch}")
    print(f"  remote sha: {result['sha']}")
    if result["result"] == "no-op":
        print("  nothing to do — ref already at that commit")
    else:
        print(f"  original local tip: {result['original_local_tip']}")
        print(f"  content identical to local tip: {result['content_identical']}")
        print(f"  local branch ref updated -> {result['sha']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
