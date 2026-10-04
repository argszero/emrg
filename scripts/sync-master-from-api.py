#!/usr/bin/env python3
"""Advance local git refs via the GitHub REST API when git-over-https is down.

EMRG has repeatedly hit github.com:443 being unreachable while api.github.com
stays up (10+ documented cycles, e.g. 08-22..08-26). The usual fallback flow:
the local repo already contains the content (a branch pushed via the Git Data
API that later got squash-merged upstream), so advancing local refs only needs
the missing *commit* objects - reconstructed byte-exact from the API's
verification payload + signature, including web-flow GPG-signed squash merges.

An **unsigned** commit has no such payload (GitHub answers `"payload": null`,
whatever the shape of the walk), so its bytes come from the fields the API does
keep - tree, parents, both identities, message - with the two losses restored by
*verification* rather than by assumption: both dates come back normalised to UTC
and the message comes back trimmed.  See `reconstruct_unsigned_commit`, whose
every candidate is accepted only if its object name equals the remote sha.  A
commit that no candidate reproduces is reported unmeasurable and no ref moves.


When the head commit's *content* objects (blobs/trees) are also missing locally
(e.g. a parallel PR introduced files this repo never had - first hit in cycle
cyc20260826-154904 with #994's GUI assets), the script now auto-fetches them via
the Git Data API: blobs via `git/blobs/{sha}` + `git hash-object -w`, trees via
`git/trees/{sha}` + `git mktree` (canonical ordering), recursing bottom-up. The
previous behavior failed loud with "run git fetch when https returns" and forced
a manual gh-api + mktree recovery dance.

Usage:
    python scripts/sync-master-from-api.py [--repo owner/name] [--ref master] [--repo owner/name] [--ref master]

Behavior:
  * resolves repo from --repo or `git remote get-url origin`
  * walks the remote commit chain from <ref> head down to the first commit
    already present locally, writing each missing commit object via
    `git hash-object -t commit -w` (byte-exact, GPG signature preserved; an
    unsigned commit is rebuilt from the API's fields and accepted only if its
    object name equals the remote sha, else the run stops as unmeasurable)
    A merge commit ends the walk, and its remaining parents are then assumed to
    be local. That assumption is checked and printed rather than left implicit:
    when it is wrong the graph is incomplete, and a caller measuring a tree is
    not left unanswered but **misled** - `git merge` prints "Already up to date"
    off a parent it cannot read, and `--is-ancestor` exits 128 where it should
    exit 1 (measured 2026-09-28, `--ref <a PR head>`). A commit with no parents
    ends the walk as the repository's **root** and is printed as one - it is not
    reported as a merge whose "0 parent(s)" are trusted. Following those parents
    instead is deliberately not done here: it would re-fetch history this repo
    usually has, and the walk is bounded by exactly that assumption.
  * verifies the root tree sha matches the remote; if content objects are
    missing, fetches missing blobs/trees via the Git Data API (disable with
    --no-fetch-objects) and re-verifies - fail-loud only if still mismatched
  * updates refs/heads/<ref> and refs/remotes/origin/<ref>; a `--ref` that is a
    full 40-hex object name creates no ref at all - a ref named after a sha
    shadows it, so `git rev-parse <sha>` answers ambiguously and the tools that
    ask it break. Such a caller gets the commit materialized, which is what it
    asked for.

Requirements: git on PATH; api.github.com reachable. Auth: optional for public
repos (GH_TOKEN or gh CLI used if available, higher rate limit).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime

API = "https://api.github.com"

_TOKEN: str | None = None  # resolved once by _auth_token(), held in memory only


def _auth_token() -> str | None:
    """Resolve a GitHub token once: env var, else `gh auth token` (read into
    memory only — never printed). Falls back to anonymous when unavailable.

    Anonymous requests are limited to 60/hr, which a commit-chain walk can
    exhaust mid-run (observed cycle 2026-08-26 01:46 on the push counterpart);
    authenticating upfront keeps long walks under the limit.
    """
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


def api_get(url: str) -> dict:
    """GET a GitHub API URL, authenticated when a token is available."""
    headers = {"User-Agent": "emrg-sync-master-from-api", "Accept": "application/vnd.github+json"}
    token = _auth_token()
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 403 and not token:
            # anonymous rate-limited; try gh CLI which uses keyring auth
            # encoding="utf-8": the API returns UTF-8 JSON (issue/pull titles,
            # author logins), and the host locale is not always UTF-8 - a cp936
            # host raised UnicodeDecodeError on the CJK bytes rather than
            # falling back to the urllib path this branch is a fallback for.
            # tests/test_script_decode_is_locale_independent.py guards the class.
            out = subprocess.run(["gh", "api", url.replace(API, ""), "--jq", "."],
                                 capture_output=True, text=True, timeout=30,
                                 encoding="utf-8", errors="replace")
            if out.returncode == 0 and out.stdout.strip():
                return json.loads(out.stdout)
        raise


def reconstruct_commit(payload: str, signature: str | None, message: str) -> bytes:
    """Rebuild the raw commit object bytes from the API's signed payload.

    The verification payload is exactly the content that was GPG-signed:
    header block + blank line + message. The raw object additionally embeds
    the `gpgsig` header between the committer line and the blank line, with
    every continuation line prefixed by a single space. With no signature the
    payload *is* the raw object - but only a caller that has one may assume
    that: GitHub returns a null payload for an unsigned commit, so a signature
    of `None` here does not mean "unsigned, bytes available", it means the
    caller's payload came from somewhere that had them. The unsigned path from
    live API fields is `reconstruct_unsigned_commit`.
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


def commit_sha(raw: bytes) -> str:
    """The object name git gives these raw commit bytes. Nothing is written and
    no process is started: the name is the hash of `commit <len>\\0<bytes>`, which
    is what makes it usable as an *acceptance test* for a reconstruction - a
    candidate that hashes to the remote sha is the remote object, because the
    alternative is a sha1 collision."""
    header = b"commit " + str(len(raw)).encode("ascii") + b"\0"
    return hashlib.sha1(header + raw).hexdigest()


def _iso_epoch(date: str) -> int:
    """The unix timestamp of an API date (`2026-09-28T07:30:46Z`)."""
    return int(datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp())


def _plausible_offsets() -> list[str]:
    """Every UTC offset a modern machine can have, nearest UTC first.

    Git writes `<unix ts> <+HHMM>` on the author and committer lines; the API
    normalises both dates to UTC, so the offset is the one quantity a rebuild
    cannot read anywhere and must not invent. The range is the tz database's
    (-12:00..+14:00) at the granularity every offset in use today has - whole,
    half and quarter hours. An object written with a second-granularity offset
    (a pre-1972 LMT) is outside the search; that is a stated limit, and such a
    commit is reported unmeasurable rather than written with a guessed name."""
    out = []
    for minutes in range(0, 14 * 60 + 1, 15):
        out.append(f"+{minutes // 60:02d}{minutes % 60:02d}")
        if minutes:
            out.append(f"-{minutes // 60:02d}{minutes % 60:02d}")
    return out


def reconstruct_unsigned_commit(
    *,
    tree: str,
    parents: list[str],
    author: tuple[str, str, str],
    committer: tuple[str, str, str],
    message: str,
    want: str,
) -> bytes:
    """Rebuild an unsigned commit's exact bytes from the fields the API keeps.

    GitHub answers `"payload": null` for an unsigned commit's verification block,
    so there are no signed bytes to copy and the fields are lossy in exactly two
    ways: both dates come back normalised to UTC (the local offset git wrote into
    the object is gone) and the message comes back without the trailing newline
    the raw object carries. Neither is assumed: candidates are built over the
    offsets a machine can have and three message spellings, and each is accepted
    only when its object name equals `want`. A wrong offset or a wrong trailing
    newline therefore cannot be accepted - it would take a sha1 collision.

    The offsets normally agree (one machine wrote both lines), so those pairs are
    tried first and the mixed ones after, because a commit can be re-committed by
    another machine (a rebase) without its author line changing.

    Raises ValueError when no candidate reproduces `want`, i.e. the object is not
    reconstructible from the API - the caller reports that instead of writing a
    commit whose name would not be the remote's.
    """
    offsets = _plausible_offsets()
    agreeing = [(o, o) for o in offsets]
    mixed = [(a, c) for a in offsets for c in offsets if a != c]
    a_ts, c_ts = _iso_epoch(author[2]), _iso_epoch(committer[2])
    header = "\n".join([f"tree {tree}"] + [f"parent {p}" for p in parents])
    for pairs in (agreeing, mixed):
        for a_off, c_off in pairs:
            block = header + "\n" + "\n".join([
                f"author {author[0]} <{author[1]}> {a_ts} {a_off}",
                f"committer {committer[0]} <{committer[1]}> {c_ts} {c_off}",
            ]) + "\n\n"
            for tail in (message, message + "\n", message + "\n\n"):
                raw = (block + tail).encode("utf-8")
                if commit_sha(raw) == want:
                    return raw
    raise ValueError(
        "no author/committer offset (15-minute steps, -12:00..+14:00) and message "
        "spelling reproduces this object; it is unsigned *and* not reconstructible "
        "from the API's fields")


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



def _absent_parents(parents: list[str], present=has_object) -> list[str]:
    """The listed commits this repo does NOT have.

    The walk stops at a merge commit and trusts the rest of its parents to be
    present already. When that assumption is wrong the gap is **silent**, and the
    caller is misled rather than unanswered: `git merge-base --is-ancestor` exits
    128 where it should exit 1, and `git merge` prints "Already up to date" for a
    parent it cannot read. Measured 2026-09-28 (`--ref <a PR head>`,
    `d7452a1b`): the walk stopped at its merge commit and left `2456e72d`
    missing, so the head looked like it contained master while it did not.

    `present` is a parameter so the answer about a layout can be asked without a
    repository: the caller passes the predicate, this decides nothing else.
    """
    return [p for p in parents if not present(p)]


def _stop_lines(sha: str, parents: list[str], absent: list[str]) -> list[str]:
    """The lines the walk prints when it stops at `sha`.

    Two different stops, so two different statements. A commit with no parents is
    the repository's **root** - reachable on a checkout that has none of the
    remote's objects, the situation this script repairs - and history simply ends
    there. A commit with parents left over is a merge, whose remaining parents are
    *assumed* local. Labelling the root a merge would print a trust statement
    about an empty list ("trusting its 0 parent(s) to be local"), asserting
    something that is not a reading.

    An absent parent gets two statements rather than one, because the gap has two
    halves and the reader meets the second first: `merge-base --is-ancestor` and
    `merge` swallow it *silently* (the measurement in `_absent_parents`), while a
    command that walks history from the commit *fails* on it and leaves a checkout
    standing there unable to leave with a bare `git checkout <branch>`.
    """
    if not parents:
        return [f"  (root commit {sha[:7]}: history ends here)"]
    lines = [f"  (merge commit {sha[:7]}: walk ends here, trusting its "
             f"{len(parents)} parent(s) to be local)"]
    if absent:
        lines.append("  ! parent(s) " + ", ".join(p[:7] for p in absent)
                     + " are NOT present locally - the commit graph is incomplete, and"
                     " git reads it as if it were whole")
        # The half the line above does not carry, and the one a reader meets next:
        # the *silent* gap is what `merge-base --is-ancestor` and `merge` do with it
        # (`_absent_parents` records that measurement), but the loud half is what a
        # command that walks history from this commit does - it fails, and a checkout
        # standing on the commit cannot be left with `git checkout <branch>`. Both
        # halves were measured 2026-10-04 on this host after `--ref <PR head>`: `git
        # log --oneline -1` printed nothing and `fatal: Failed to traverse parents of
        # commit <sha>`, and `git checkout master` answered `fatal: internal error in
        # revision walk` and left HEAD detached at the materialized commit.
        lines.append("  ! not everywhere, though: a walk from this commit dies on the"
                     " missing parent (`git log` printed `fatal: Failed to traverse"
                     " parents`, `git checkout <branch>` answered `fatal: internal error"
                     " in revision walk` and left HEAD where it was) - so a checkout"
                     " standing on it is left with `git symbolic-ref HEAD"
                     " refs/heads/<branch> && git reset --hard`; a bare `git checkout"
                     " <branch>` does not get you off it")
    return lines


def _is_object_name(ref: str) -> bool:
    """True when `--ref` names a commit rather than a branch.

    A full 40-hex value is an **object name**: `--ref <sha>` is how a PR head is
    materialized during an outage, and such a caller wants the commit, not a
    branch. Writing `refs/heads/<sha>` for it would create a ref nobody reads and,
    worse, shadow the object - `git rev-parse <sha>` then answers ambiguously and
    the tools that ask it break (measured 2026-09-28: `check-merge-plan-suite.py`
    failed with "merge-tree failed" until those refs were deleted). Recorded as a
    predicate so the shape is pinned without a repository.
    """
    return bool(re.fullmatch(r"[0-9a-fA-F]{40}", ref))


def _object_exists(sha: str) -> bool:
    """Any object (blob/tree/commit) present locally by sha."""
    return subprocess.run(["git", "cat-file", "-e", sha],
                          capture_output=True).returncode == 0


def _fetch_blob(repo: str, blob_sha: str) -> None:
    """Fetch one missing blob via the Git Data API, writing it byte-exact."""
    if _object_exists(blob_sha):
        return
    b = api_get(f"{API}/repos/{repo}/git/blobs/{blob_sha}")
    if b.get("encoding") == "base64":
        content = base64.b64decode(b["content"])
    else:  # utf-8 text blobs are returned raw
        content = b["content"].encode("utf-8")
    r = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=content,
                       capture_output=True)
    if r.returncode != 0 or r.stdout.decode().strip() != blob_sha:
        raise RuntimeError(f"blob materialization mismatch for {blob_sha[:7]} "
                           f"(got {r.stdout.decode().strip()[:7] or 'NONE'})")


def _fetch_tree(repo: str, tree_sha: str) -> None:
    """Recursively materialize a missing tree: blobs via hash-object, subtrees
    via git mktree (git canonical ordering), bottom-up. Idempotent."""
    if _object_exists(tree_sha):
        return
    t = api_get(f"{API}/repos/{repo}/git/trees/{tree_sha}")
    if t.get("truncated"):
        raise RuntimeError(f"tree {tree_sha[:7]} truncated by API (>100k entries)")
    entries = t.get("tree", [])
    for e in entries:
        if e["type"] == "blob":
            _fetch_blob(repo, e["sha"])
        elif e["type"] == "tree":
            _fetch_tree(repo, e["sha"])
        # commit entries (submodules): leave to git fetch — rare in this repo
    lines = [f"{e['mode']} {e['type']} {e['sha']}\t{e['path']}" for e in entries]
    r = subprocess.run(["git", "mktree"], input=("\n".join(lines) + "\n").encode("utf-8"),
                       capture_output=True)
    if r.returncode != 0 or r.stdout.decode().strip() != tree_sha:
        raise RuntimeError(f"tree materialization mismatch for {tree_sha[:7]} "
                           f"(got {r.stdout.decode().strip()[:7] or 'NONE'})")


def rev_parse(ref: str) -> str:
    r = subprocess.run(["git", "rev-parse", "-q", "--verify", ref], capture_output=True)
    return r.stdout.decode().strip() if r.returncode == 0 else ""


def repo_from_origin() -> str:
    # encoding="utf-8": the origin URL is echoed back in SystemExit below, and a
    # clone directory may carry a non-ASCII byte; the locale codec would raise
    # on it instead of reporting the infer failure this function exists to give.
    r = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    url = r.stdout.strip()
    m = re.search(r"(?:github\.com[:/])([^/]+)/([^/.]+)", url)
    if not m:
        raise SystemExit("cannot infer owner/repo from origin URL: " + url)
    return m.group(1) + "/" + m.group(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", help="owner/name (default: inferred from origin URL)")
    ap.add_argument("--ref", default="master", help="branch name to sync (default: master)")
    ap.add_argument("--no-fetch-objects", action="store_true",
                    help="fail loud on missing content objects instead of fetching them")
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
        if payload:
            raw = reconstruct_commit(payload, signature, body["message"])
        else:
            # Unsigned: GitHub keeps no payload to copy, so the object is rebuilt
            # from the fields and verified by its own name (measured 2026-09-28:
            # `--ref <an unsigned PR head>` used to die here with an AttributeError
            # on `payload.encode` - a crash where the honest answer was a stated
            # "unmeasurable", and no way at all to advance a ref during an outage).
            try:
                raw = reconstruct_unsigned_commit(
                    tree=body["tree"]["sha"],
                    parents=[p["sha"] for p in c["parents"]],
                    author=(body["author"]["name"], body["author"]["email"],
                            body["author"]["date"]),
                    committer=(body["committer"]["name"], body["committer"]["email"],
                               body["committer"]["date"]),
                    message=body["message"],
                    want=sha,
                )
            except ValueError as exc:
                raise SystemExit(f"unmeasurable: {sha[:7]} is unsigned and {exc} "
                                 "(no refs touched) - retry when https returns")
        got = write_commit_object(raw)
        if got != sha:
            raise RuntimeError(f"reconstruction mismatch: want {sha}, got {got} — aborting (no refs touched)")
        created += 1
        print(f"  + {sha[:7]} ({body['author']['name']}, {body['message'].splitlines()[0][:60]})")
        parents = [p["sha"] for p in c["parents"]]
        if len(parents) == 1:
            sha = parents[0]
            continue
        # A commit with no parents is the repository's root; one with parents left
        # over is a merge whose remaining parents are *assumed* to be local. Each
        # stop is said for what it is - `_stop_lines` records why the root must not
        # be reported as a merge.
        for line in _stop_lines(sha, parents, _absent_parents(parents)):
            print(line)
        sha = None
    if created == 0:
        print(f"  (head already present locally: {sha[:7]})")

    # Verify the root tree matches (fail-loud if content objects are missing).
    tree = api_get(f"{API}/repos/{repo}/git/commits/{head}")["tree"]["sha"]
    local_tree = subprocess.run(["git", "rev-parse", head + "^{tree}"],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    if local_tree.returncode != 0 or local_tree.stdout.strip() != tree:
        if not args.no_fetch_objects:
            print(f"  root tree {tree[:7]} missing locally - fetching blobs/trees via Git Data API")
            _fetch_tree(repo, tree)
            local_tree = subprocess.run(["git", "rev-parse", head + "^{tree}"],
                                        capture_output=True, text=True,
                                        encoding="utf-8", errors="replace")
            if local_tree.returncode == 0 and local_tree.stdout.strip() == tree:
                print(f"  materialized root tree {tree[:7]} (blobs + subtrees) OK")
        if local_tree.returncode != 0 or local_tree.stdout.strip() != tree:
            raise SystemExit(f"tree mismatch or missing objects for {head[:7]} "
                             f"(want {tree}, got {local_tree.stdout.strip() or 'NONE'}) — "
                             "run `git fetch` when https returns")

    if _is_object_name(args.ref):
        # The commit is materialized above, which is what a `--ref <sha>` caller
        # asked for. A ref named after a sha is not a branch anyone reads, and it
        # shadows the object: `git rev-parse <sha>` then answers ambiguously.
        print(f"  ({args.ref[:7]} is an object name, not a branch: the commit is "
              f"materialized and no ref is created - a ref named after a sha would "
              f"shadow it, so `git rev-parse` would answer ambiguously)")
        return 0

    for ref in (f"refs/heads/{args.ref}", f"refs/remotes/origin/{args.ref}"):
        subprocess.run(["git", "update-ref", ref, head], check=True)
    print(f"updated refs/heads/{args.ref} and refs/remotes/origin/{args.ref} -> {head[:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
