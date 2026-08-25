#!/usr/bin/env python3
"""Advance local git refs via the GitHub REST API when git-over-https is down.

EMRG has repeatedly hit github.com:443 being unreachable while api.github.com
stays up (10+ documented cycles, e.g. 08-22..08-26). The usual fallback flow:
the local repo already contains the content (a branch pushed via the Git Data
API that later got squash-merged upstream), so advancing local refs only needs
the missing *commit* objects — reconstructed byte-exact from the API's
verification payload + signature, including web-flow GPG-signed squash merges.

Usage:
    python scripts/sync-master-from-api.py [--repo owner/name] [--ref master]

Behavior:
  * resolves repo from --repo or `git remote get-url origin`
  * walks the remote commit chain from <ref> head down to the first commit
    already present locally, writing each missing commit object via
    `git hash-object -t commit -w` (byte-exact, GPG signature preserved)
  * verifies the root tree sha matches the remote (fail-loud if content
    objects are missing locally — prefer `git fetch` when https returns)
  * updates refs/heads/<ref> and refs/remotes/origin/<ref>

Requirements: git on PATH; api.github.com reachable. Auth: optional for public
repos (GH_TOKEN or gh CLI used if available, higher rate limit).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.request

API = "https://api.github.com"


def api_get(url: str) -> dict:
    """GET a GitHub API URL, using GH_TOKEN or gh CLI auth when available."""
    headers = {"User-Agent": "emrg-sync-master-from-api", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 403 and not token:
            # anonymous rate-limited; try gh CLI which uses keyring auth
            out = subprocess.run(["gh", "api", url.replace(API, ""), "--jq", "."],
                                 capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and out.stdout.strip():
                return json.loads(out.stdout)
        raise


def reconstruct_commit(payload: str, signature: str | None, message: str) -> bytes:
    """Rebuild the raw commit object bytes from the API's signed payload.

    The verification payload is exactly the content that was GPG-signed:
    header block + blank line + message. The raw object additionally embeds
    the `gpgsig` header between the committer line and the blank line, with
    every continuation line prefixed by a single space. Unsigned commits have
    no signature — the raw object equals the payload as-is.
    """
    if signature:
        idx = payload.index("\n\n")
        header = payload[:idx]
        msg = payload[idx + 2 :]
        sig_lines = signature.split("\n")
        gpgsig = ["gpgsig " + sig_lines[0]] + [" " + l for l in sig_lines[1:]]
        raw = header + "\n" + "\n".join(gpgsig) + "\n\n" + msg
        if message and message not in raw:
            raise ValueError("payload/message mismatch: reconstructed object does not contain the API message")
        return raw.encode("utf-8")
    raw = payload.encode("utf-8")
    if message and message.encode("utf-8") not in raw:
        raise ValueError("payload/message mismatch: unsigned payload does not contain the API message")
    return raw


def write_commit_object(raw: bytes) -> str:
    """Write a raw commit object into the local store, returning its sha."""
    r = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                       input=raw, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("git hash-object failed: " + r.stderr.decode(errors="replace"))
    return r.stdout.decode().strip()


def has_object(sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", sha + "^{commit}"],
                          capture_output=True).returncode == 0


def rev_parse(ref: str) -> str:
    r = subprocess.run(["git", "rev-parse", "-q", "--verify", ref], capture_output=True)
    return r.stdout.decode().strip() if r.returncode == 0 else ""


def repo_from_origin() -> str:
    r = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True)
    url = r.stdout.strip()
    m = re.search(r"(?:github\.com[:/])([^/]+)/([^/.]+)", url)
    if not m:
        raise SystemExit("cannot infer owner/repo from origin URL: " + url)
    return m.group(1) + "/" + m.group(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", help="owner/name (default: inferred from origin URL)")
    ap.add_argument("--ref", default="master", help="branch name to sync (default: master)")
    args = ap.parse_args()

    repo = args.repo or repo_from_origin()
    head = api_get(f"{API}/repos/{repo}/commits/{args.ref}")["sha"]
    print(f"remote {repo} {args.ref} head: {head[:7]}")

    # Walk the commit chain, reconstructing missing commits until a known one.
    sha = head
    created = 0
    while sha and not has_object(sha):
        c = api_get(f"{API}/repos/{repo}/commits/{sha}")
        body = c["commit"]
        payload = body["verification"]["payload"]
        signature = body["verification"]["signature"] or None
        raw = reconstruct_commit(payload, signature, body["message"])
        got = write_commit_object(raw)
        if got != sha:
            raise RuntimeError(f"reconstruction mismatch: want {sha}, got {got} — aborting (no refs touched)")
        created += 1
        print(f"  + {sha[:7]} ({body['author']['name']}, {body['message'].splitlines()[0][:60]})")
        parents = [p["sha"] for p in c["parents"]]
        sha = parents[0] if len(parents) == 1 else None  # merge commits: stop, local must have them
    if created == 0:
        print(f"  (head already present locally: {sha[:7]})")

    # Verify the root tree matches (fail-loud if content objects are missing).
    tree = api_get(f"{API}/repos/{repo}/git/commits/{head}")["tree"]["sha"]
    local_tree = subprocess.run(["git", "rev-parse", head + "^{tree}"],
                                capture_output=True, text=True)
    if local_tree.returncode != 0 or local_tree.stdout.strip() != tree:
        raise SystemExit(f"tree mismatch or missing objects for {head[:7]} "
                         f"(want {tree}, got {local_tree.stdout.strip() or 'NONE'}) — "
                         "run `git fetch` when https returns")

    for ref in (f"refs/heads/{args.ref}", f"refs/remotes/origin/{args.ref}"):
        subprocess.run(["git", "update-ref", ref, head], check=True)
    print(f"updated refs/heads/{args.ref} and refs/remotes/origin/{args.ref} -> {head[:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
