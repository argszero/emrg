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
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from emrg.config import config_dir
from emrg.skills.registry import (
    find_catalog_skill,
    read_state,
    write_state,
)

logger = logging.getLogger(__name__)

CLI_NAME = "browser-harness"
CLI_PYPI_PACKAGE = "browser-harness"
CLI_INSTALL_CMD = ["uv", "tool", "install", "--python", "3.12", CLI_PYPI_PACKAGE]
GITHUB_API = "https://api.github.com"
_UPDATE_TTL_SECONDS = 24 * 3600

# Catalog "install" values we know how to drive. Anything else is refused
# with a clear error (capability passport style: catalog is a decision aid).
_KNOWN_INSTALL_KINDS = ("self-publishing",)


@dataclass
class CmdResult:
    """Result of a subprocess run (returncode + merged stdout/stderr)."""

    returncode: int
    stdout: str


Runner = Callable[..., Awaitable[CmdResult]]
HttpGet = Callable[[str], Awaitable[Optional[dict]]]


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


#: Exit codes a POSIX shell uses when it **found the file and could not run it**
#: (126) or could not find the command at all (127). Both mean the CLI never
#: started, which is a different fact from "it started and refused", and the
#: remedy is different too — see `_could_not_start`.
_STARTUP_EXIT_CODES = (126, 127)


def install_hint() -> str:
    """The command that puts this CLI on PATH — the one home for the remedy.

    It is quoted in `install_skill`'s `confirm_required` answer and in the
    startup failure below, and a remedy written twice is a remedy that drifts.
    """
    return " ".join(CLI_INSTALL_CMD)


def cli_available() -> bool:
    """True when the CLI **name resolves on PATH** — not when the CLI runs.

    Measured 2026-10-03 (`cyc20261003-155102`): a CLI whose shim points at a
    removed interpreter (this host's asdf shape for `npm`/`node`) resolves under
    `shutil.which` and exits **126** when invoked. Read as "the CLI is
    installed", that answer sends the caller past the `confirm_required` branch
    — so the host is never told to install it, and the failure arrives as
    `... skill failed (exit 126)`, which names no remedy.

    Running the CLI here to tell the two apart is **not** available: this CLI
    starts a browser daemon as a side effect (`ensure_daemon()` runs before the
    script), so a preflight probe would have effects of its own. The two states
    are therefore separated where they actually differ — at the run, by
    `_could_not_start` — and this function keeps the cheap presence answer,
    named for exactly what it answers.
    """
    return shutil.which(CLI_NAME) is not None


def _could_not_start(returncode: int | None = None, detail: str = "") -> str:
    """The message for a CLI that never started, carrying the remedy.

    Split out from `_publish_skill` so the two entrance points — the raw
    `OSError` (no such file) and a startup exit code — produce one wording, and
    so both are testable without a subprocess.

    The CLI's own output is carried when there is any: for a broken shim that
    line names the interpreter that is missing, which is the evidence the old
    message dropped on the floor.
    """
    why = "is not runnable" if returncode is None else f"could not be started (exit {returncode})"
    message = (
        f"{CLI_NAME} {why} — it is on PATH but the command did not run. Reinstall it: "
        f"`{install_hint()}`"
    )
    return f"{message}. The CLI said: {detail.strip()[:300]}" if detail.strip() else message


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
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        # The command could not be started at all — no such file, a component of
        # the path is not a directory, or it is not executable. All three are
        # "install/repair it", never "the CLI ran and said no".
        return {"error": _could_not_start(None, str(exc))}
    if result.returncode in _STARTUP_EXIT_CODES:
        return {"error": _could_not_start(result.returncode, result.stdout)}
    if result.returncode != 0:
        return {"error": f"{CLI_NAME} skill failed (exit {result.returncode}): {result.stdout.strip()[:300]}"}

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
) -> dict:
    """Install a catalog skill by name (host-confirmed CLI install).

    Returns one of:
      {"error": ...}                     — unknown skill / failed
      {"confirm_required": True, ...}    — CLI missing, host must confirm
      {"ok": True, "name", "version", "installed_at"} — done
    """
    entry = find_catalog_skill(name)
    if entry is None:
        return {"error": f"unknown catalog skill: {name!r}"}

    if not cli_available():
        if not confirmed:
            return {
                "confirm_required": True,
                "name": name,
                "install_command": install_hint(),
                "message": (
                    f"Skill {name!r} needs its CLI installed first: `{install_hint()}`"
                ),
            }
        run = runner or _default_runner
        try:
            result = await run(CLI_INSTALL_CMD)
        except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            return {"error": f"`{CLI_INSTALL_CMD[0]}` could not be started: {exc}"}
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
) -> dict:
    """Refresh managed skills whose latest GitHub release differs.

    Background-deterministic: never installs a missing CLI silently, never
    touches non-managed (host-modified) skill files. Returns a summary:
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
