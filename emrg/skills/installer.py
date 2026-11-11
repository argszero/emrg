"""Install/update skills from the catalog — deterministic, no LLM.

Revised design (rant 2026-08-08T10:14:29, supersedes the 10:11:35
registry design): the installable list lives in the normal skill file
``~/.emrg/skills/skill-catalog.md``; the installer reads its frontmatter
``skills:`` list for the 5 metadata fields (name/description/repo/
install/dest/check).

Install flow (``/skills install <name>``, host-confirmed):

1. look up the catalog entry
2. ensure the CLI exists: ``uv tool install --python 3.12 <pkg>`` —
   first-time CLI install requires explicit host confirmation
   (MANIFESTO host-rights §10: the TUI surfaces a yes/no prompt; the
   background update path never installs a CLI silently)
3. run ``<cli> skill`` to self-publish the skill file(s) — EMRG does not
   need to know the file list in advance
4. write the published output into dest (``~/.emrg/skills/``)
5. validate the frontmatter (name + description) — roll back on failure
6. record ``{name: {version, installed_at, managed: true}}`` in
   ``~/.emrg/skills/.state.json``

Update check (daemon startup + every 24h, background deterministic):

- for each ``managed: true`` entry, compare the latest GitHub release tag
  (via api.github.com — raw.githubusercontent.com / github.com:443 may be
  blocked on the host network) with the recorded version
- tag differs → re-run the publish step (CLI already present, so no host
  confirmation); never touch host-modified copies (not managed)

The PyPI package name is hardcoded in the installer logic per design
("uv tool install --python 3.12 browser-harness" — it does not go into
the catalog).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

from emrg.config import config_dir
from emrg.memory import NotARegularFile
from emrg.skills.registry import (
    find_catalog_skill,
    read_state,
    write_state,
)

# The "is this path a regular file" rule is the tool layer's, reached at a path. Imported
# rather than restated, for the reason `emrg/skills/registry.py` gives at the same import:
# the boundary is one fact, and a second spelling of it is a second boundary that can drift.
from emrg.tools.base import non_regular_kind

logger = logging.getLogger(__name__)

CLI_NAME = "browser-harness"
CLI_PYPI_PACKAGE = "browser-harness"
CLI_INSTALL_CMD = ["uv", "tool", "install", "--python", "3.12", CLI_PYPI_PACKAGE]
GITHUB_API = "https://api.github.com"
GITHUB_CODELOAD = "https://codeload.github.com"
_UPDATE_TTL_SECONDS = 24 * 3600

# Catalog "install" values we know how to drive. Anything else is refused
# with a clear error (capability passport style: catalog is a decision aid).
#
# ``self-publishing``  the skill's own CLI emits its files (``<cli> skill``)
# ``vendor-copy``      the catalog names a directory in a repository, copied whole
_KNOWN_INSTALL_KINDS = ("self-publishing", "vendor-copy")


@dataclass
class CmdResult:
    """Result of a subprocess run (returncode + merged stdout/stderr)."""

    returncode: int
    stdout: str


Runner = Callable[..., Awaitable[CmdResult]]
HttpGet = Callable[[str], Awaitable[Optional[dict]]]
FetchBytes = Callable[[str], Awaitable[Optional[bytes]]]


async def _default_runner(cmd: list[str], **kwargs) -> CmdResult:
    """Run a command via asyncio subprocess (captures merged output)."""
    from emrg._win import win32_no_window_kwargs

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Windows: background daemon children must not pop a console window
        # (rant 2026-08-09T13:16:36 — cmd-window storm).
        **win32_no_window_kwargs(),
        **kwargs,
    )
    out, _ = await proc.communicate()
    return CmdResult(proc.returncode or 0, out.decode("utf-8", "replace"))


async def _default_http_get(url: str) -> Optional[dict]:
    """GET a JSON endpoint via httpx (used for GitHub release checks)."""
    import httpx

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": "emrg-skill-catalog", "Accept": "application/vnd.github+json"},
    ) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        return resp.json()


async def _default_fetch_bytes(url: str) -> Optional[bytes]:
    """Download a URL's bytes via httpx (used for a repository tarball)."""
    import httpx

    async with httpx.AsyncClient(
        timeout=120.0,
        headers={"User-Agent": "emrg-skill-catalog"},
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        return resp.content


def cli_available() -> bool:
    """True when the CLI executable is on PATH."""
    return shutil.which(CLI_NAME) is not None


async def _fetch_latest_tag(repo: str, http_get: Optional[HttpGet]) -> Optional[str]:
    """Latest release tag for a repo, via api.github.com (None on failure).

    Strips a leading "v" so "v0.1.8" compares against a state version
    recorded as "0.1.8".
    """
    get = http_get or _default_http_get
    try:
        data = await get(f"{GITHUB_API}/repos/{repo}/releases/latest")
    except Exception:
        logger.debug("release check failed for %s", repo, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag:
        return None
    return tag[1:] if tag.startswith("v") else tag


async def _fetch_path_commit(
    repo: str, source: str, http_get: Optional[HttpGet]
) -> Optional[str]:
    """The last commit that touched ``source`` in ``repo`` — a vendor copy's version.

    The commits endpoint, not ``releases/latest``: ``anthropics/skills`` publishes a
    repository with no releases, so reading a release tag there would answer ``None`` for
    ever and leave the 24h check permanently silent about it.
    """
    if not repo or not source:
        return None
    get = http_get or _default_http_get
    url = f"{GITHUB_API}/repos/{repo}/commits?path={quote(source, safe='/')}&per_page=1"
    try:
        data = await get(url)
    except Exception:
        logger.debug("commit check failed for %s %s", repo, source, exc_info=True)
        return None
    if not isinstance(data, list) or not data:
        return None
    head = data[0]
    sha = head.get("sha") if isinstance(head, dict) else None
    return sha if isinstance(sha, str) and sha else None


def _extract_subtree(payload: bytes, source: str, staging: Path) -> Optional[str]:
    """Write ``<top>/<source>/`` out of a GitHub tarball into ``staging``.

    Returns an error string, or ``None`` when the subtree was written. A member that
    escapes the archive or the named directory is **refused**, not skipped: a ``..``
    component is an attempt to write outside the destination, and a link's target is not
    something the archive has proved. GitHub's tarballs carry one top-level
    ``<repo>-<sha>/`` directory, which is stripped before ``source`` is matched.
    """
    source = source.strip("/")
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            for member in tar:
                name = member.name
                if name.startswith("/") or ":" in name.split("/")[0]:
                    return f"archive member is an absolute path: {name}"
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if any(p == ".." for p in parts):
                    return f"archive member escapes the archive: {name}"
                inner = "/".join(parts[1:])
                if not inner:
                    continue
                if inner != source and not inner.startswith(source + "/"):
                    continue
                rel = inner[len(source):].strip("/")
                if not rel:
                    continue
                if member.isdir():
                    (staging / rel).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    return f"archive member is not a regular file: {name}"
                target = staging / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = tar.extractfile(member)
                if handle is None:
                    return f"cannot read archive member: {name}"
                target.write_bytes(handle.read())
    except (tarfile.TarError, OSError, EOFError) as e:
        return f"cannot unpack the archive: {e}"
    return None


async def _vendor_copy(
    entry: dict,
    *,
    name: str,
    http_get: Optional[HttpGet] = None,
    fetch_bytes: Optional[FetchBytes] = None,
    sha: Optional[str] = None,
) -> dict:
    """Copy a repository directory into ``dest`` as a bundle (``vendor-copy``).

    No CLI and no subprocess: the catalog names ``repo`` + ``source``, the source
    directory's last commit is the version, and the tarball at that commit is unpacked.
    The copy is staged inside ``dest`` and only renamed into place once its ``SKILL.md``
    carries the name+description frontmatter every loader in this tree requires, so a
    refused copy leaves no half-written directory behind.

    Returns ``{"ok": True, "path", "version"}`` or ``{"error": ...}``.
    """
    repo = entry.get("repo", "")
    source = (entry.get("source") or "").strip("/")
    if not repo or not source:
        return {"error": "vendor-copy entry needs a repo and a source directory"}

    commit = sha or await _fetch_path_commit(repo, source, http_get)
    if not commit:
        return {"error": f"cannot resolve the commit of {repo} {source}"}

    url = f"{GITHUB_CODELOAD}/{repo}/tar.gz/{commit}"
    fetch = fetch_bytes or _default_fetch_bytes
    try:
        payload = await fetch(url)
    except Exception:
        logger.debug("tarball fetch failed for %s", url, exc_info=True)
        return {"error": f"cannot download {url}"}
    if not payload:
        return {"error": f"cannot download {url}"}

    dest = _resolve_dest(entry.get("dest", "~/.emrg/skills/"))
    try:
        dest.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=str(dest), prefix=f".vendor-{name}-"))
    except OSError as e:
        return {"error": f"cannot prepare {dest}: {e}"}

    try:
        problem = _extract_subtree(payload, source, staging)
        if problem:
            return {"error": problem}
        if not any(p.is_file() for p in staging.rglob("*")):
            return {"error": f"{source} is not in the archive of {repo}@{commit[:7]}"}

        from emrg.skills.loader import _parse_frontmatter

        skill_file = staging / "SKILL.md"
        text = (
            skill_file.read_text(encoding="utf-8", errors="replace")
            if skill_file.is_file()
            else ""
        )
        fm = _parse_frontmatter(text) if text.startswith("---") else {}
        if not fm.get("name") or not fm.get("description"):
            return {"error": f"{source} carries no usable SKILL.md (name and description)"}

        target = dest / name
        try:
            if os.path.islink(target) or (target.exists() and not target.is_dir()):
                return {"error": f"cannot install into {target}: it is not a directory"}
            if target.is_dir():
                shutil.rmtree(target)
            os.replace(staging, target)
        except OSError as e:
            return {"error": f"cannot move the copy into {target}: {e}"}
        return {"ok": True, "name": name, "path": str(target), "version": commit}
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


async def _publish_skill(entry: dict, runner: Optional[Runner]) -> dict:
    """Run ``<cli> skill`` and write its output into dest.

    Returns {"ok": True, "path": ..., "name": ...} or {"error": ...}.
    Writes are rollback-safe: on validation failure the freshly written
    file is removed.
    """
    install_kind = entry.get("install", "")
    if install_kind not in _KNOWN_INSTALL_KINDS:
        return {"error": f"unsupported install kind: {install_kind!r}"}

    dest = _resolve_dest(entry.get("dest", "~/.emrg/skills/"))
    run = runner or _default_runner
    try:
        result = await run([CLI_NAME, "skill"])
    except FileNotFoundError:
        return {"error": f"{CLI_NAME} CLI not found on PATH"}
    if result.returncode != 0:
        return {"error": f"{CLI_NAME} skill failed (exit {result.returncode})"}

    skill_text = result.stdout.strip()
    if not skill_text:
        return {"error": f"{CLI_NAME} skill produced empty output"}

    # Validate the published file has a name+description frontmatter
    # (reuse the loader's parser — no new YAML dependency).
    from emrg.skills.loader import _parse_frontmatter

    fm = _parse_frontmatter(skill_text) if skill_text.startswith("---") else {}
    name = fm.get("name", "")
    description = fm.get("description", "")
    if not name or not description:
        return {"error": "published skill missing name/description frontmatter"}

    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"{name}.md"
    try:
        # The subject's kind is asked before it is opened, because the open is what decides
        # whether this call returns at all: `Path.write_text` is `open(path, "w")`, and opening
        # a FIFO for **writing** waits for a *reader* to appear — raises nothing, so the
        # `except OSError` below would never be reached and the install would never finish.
        # Measured 2026-10-11 (`cyc20261011-130932`) on master `3f18f6a6`, one child process per
        # arm under an 8 s cap: a `mkfifo` at `<dest>/probe.md` left `_publish_skill` **not
        # returned in 8 s**, where the same fixture with a regular file returned in 0.00 s with
        # `{"ok": True, ...}`.
        #
        # Refused rather than degraded, and named: `NotARegularFile` **is** an `OSError`, so it
        # lands in the handler already written for a failed write and produces the reading this
        # function already gives one — `{"error": "cannot write skill file: <the kind>"}` —
        # which `install_skill` returns to the tool and the client. The `FileNotFoundError`
        # branch is not optional: a writer's normal case is a destination that does not exist
        # yet, and this function exists to create it.
        try:
            kind = non_regular_kind(target)
        except FileNotFoundError:
            kind = None
        if kind:
            raise NotARegularFile(f"{target} is {kind}, not a regular file")
        target.write_text(skill_text + "\n", encoding="utf-8")
    except OSError as e:
        return {"error": f"cannot write skill file: {e}"}
    return {"ok": True, "path": str(target), "name": name}


def _resolve_dest(dest: str) -> Path:
    """Resolve a catalog dest value.

    ``~/.emrg/...`` routes through ``config_dir()`` (the single source of
    truth for EMRG's runtime dir — tests redirect it via monkeypatch);
    any other ``~`` path expands against the real home directory.
    """
    if dest.startswith("~"):
        rel = dest[1:].lstrip("/")
        if rel.startswith(".emrg/"):
            return config_dir() / rel[len(".emrg/"):]
        return Path.home() / rel
    return Path(dest)


async def install_skill(
    name: str,
    *,
    confirmed: bool = False,
    runner: Optional[Runner] = None,
    http_get: Optional[HttpGet] = None,
    fetch_bytes: Optional[FetchBytes] = None,
) -> dict:
    """Install a catalog skill by name.

    Returns one of:
      {"error": ...}                     — unknown skill / failed
      {"confirm_required": True, ...}    — CLI missing, host must confirm
      {"ok": True, "name", "version", "installed_at"} — done

    The install kind is dispatched **before** the CLI gate: `vendor-copy` has no CLI, so
    asking `cli_available()` first would report every such entry as needing a
    browser-harness install, and refuse it until the host agreed to one it never uses.
    """
    entry = find_catalog_skill(name)
    if entry is None:
        return {"error": f"unknown catalog skill: {name!r}"}

    kind = entry.get("install", "")
    if kind not in _KNOWN_INSTALL_KINDS:
        return {"error": f"unsupported install kind: {kind!r}"}

    if kind == "vendor-copy":
        copied = await _vendor_copy(
            entry, name=name, http_get=http_get, fetch_bytes=fetch_bytes
        )
        if "error" in copied:
            return {"error": copied["error"]}
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        state = read_state()
        state[name] = {
            "version": copied["version"],
            "installed_at": now,
            "updated_at": now,
            "managed": True,
        }
        write_state(state)
        return {
            "ok": True,
            "name": name,
            "version": copied["version"],
            "installed_at": now,
            "path": copied["path"],
        }

    if not cli_available():
        if not confirmed:
            return {
                "confirm_required": True,
                "name": name,
                "install_command": " ".join(CLI_INSTALL_CMD),
                "message": (
                    f"Skill {name!r} needs its CLI installed first: "
                    f"`{' '.join(CLI_INSTALL_CMD)}`"
                ),
            }
        run = runner or _default_runner
        try:
            result = await run(CLI_INSTALL_CMD)
        except FileNotFoundError:
            return {"error": "uv not found on PATH — cannot install CLI"}
        if result.returncode != 0:
            return {"error": f"CLI install failed (exit {result.returncode}): {result.stdout[-300:]}"}
        if not cli_available():
            return {"error": "CLI install finished but command not found on PATH"}

    published = await _publish_skill(entry, runner)
    if "error" in published:
        return {"error": published["error"]}

    # Record state: version from the latest GitHub release (best effort),
    # installed_at now, managed=True so the 24h check can refresh it.
    latest = await _fetch_latest_tag(entry.get("repo", ""), http_get)
    version = latest or "unknown"
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    state = read_state()
    state[name] = {
        "version": version,
        "installed_at": now,
        "updated_at": now,
        "managed": True,
    }
    write_state(state)

    return {
        "ok": True,
        "name": name,
        "version": version,
        "installed_at": now,
        "path": published.get("path", ""),
    }


async def update_managed_skills(
    *,
    runner: Optional[Runner] = None,
    http_get: Optional[HttpGet] = None,
    fetch_bytes: Optional[FetchBytes] = None,
) -> dict:
    """Refresh managed skills whose upstream version differs.

    Background-deterministic: never installs a missing CLI silently, never touches
    non-managed (host-modified) skill files. A `self-publishing` entry compares against the
    latest release tag; a `vendor-copy` entry against the source path's last commit, and is
    re-copied only when that commit moved. Returns a summary:
      {"checked": int, "updated": [names], "skipped": [names], "errors": [names]}
    """
    state = read_state()
    managed = {k: v for k, v in state.items() if v.get("managed")}
    updated: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    for name, info in managed.items():
        entry = find_catalog_skill(name)
        if entry is None:
            continue
        if entry.get("install") == "vendor-copy":
            commit = await _fetch_path_commit(
                entry.get("repo", ""), entry.get("source", ""), http_get
            )
            if commit is None:
                continue  # network/API failure — try again next cycle
            if commit == info.get("version"):
                continue  # up to date: nothing is even downloaded
            copied = await _vendor_copy(
                entry, name=name, http_get=http_get, fetch_bytes=fetch_bytes, sha=commit
            )
            if "error" in copied:
                errors.append(name)
                logger.warning("skill update failed for %s: %s", name, copied["error"])
                continue
            info["version"] = copied["version"]
            info["updated_at"] = (
                datetime.now().astimezone().isoformat(timespec="seconds")
            )
            updated.append(name)
            continue
        latest = await _fetch_latest_tag(entry.get("repo", ""), http_get)
        if latest is None:
            continue  # network/API failure — try again next cycle
        if latest == info.get("version"):
            continue  # up to date
        if not cli_available():
            skipped.append(name)  # never install a CLI in the background
            continue
        published = await _publish_skill(entry, runner)
        if "error" in published:
            errors.append(name)
            logger.warning("skill update failed for %s: %s", name, published["error"])
            continue
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        info["version"] = latest
        info["updated_at"] = now
        updated.append(name)

    if updated or errors:
        write_state(state)

    return {
        "checked": len(managed),
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }


async def run_update_check_once() -> dict:
    """One-shot update check (used by the daemon TTL loop)."""
    try:
        return await update_managed_skills()
    except Exception:
        logger.debug("skills update check failed", exc_info=True)
        return {"checked": 0, "updated": [], "skipped": [], "errors": [], "error": "update check failed"}
