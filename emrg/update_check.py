"""Automatic update-check + notify + auto-download (rants 2026-08-10T07:12:12,
2026-08-12T12:10:12).

Design (host-specified boundaries):
- CHECK: look for new versions on api.github.com (never prereleases).
- PROMPT: one prompt per version, idempotent via state file, silent on
  network failure (retry at next TTL).
- DOWNLOAD (rant 2026-08-12T12:10:12): when a newer version is found and
  [update] auto_download is enabled, the daemon downloads the current
  platform's installer asset in the background — stream + Range resume,
  SHA256 verify against the release asset digest, landed in
  ~/.emrg/updates/. NEVER auto-installs: the GUI prompts the user and the
  user clicks to install.
- [update] check=false disables everything (including download).

Check source: api.github.com (github.com:443 / raw.githubusercontent.com are
blocked on the host network — see git_utils / skills installer patterns).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import time
from pathlib import Path
from typing import Optional

import httpx

from emrg.config import config_dir

# Default TTL between checks (seconds). Host-configurable via [update] ttl_hours
# (rant 2026-08-12T12:10:12: default 24h → 1h).
DEFAULT_TTL_SECONDS = 3600
# API endpoint — releases/latest never includes prereleases (semver-satisfying).
RELEASES_LATEST_URL = "https://api.github.com/repos/argszero/emrg/releases/latest"
# Direct asset download base (no API rate limits on release downloads).
DOWNLOAD_BASE_URL = "https://github.com/argszero/emrg/releases/download"
CHECK_TIMEOUT_SECONDS = 10.0
DOWNLOAD_TIMEOUT_SECONDS = 600.0

STATE_FILE_NAME = ".last_update_check.json"
UPDATES_DIR_NAME = "updates"


def parse_version(tag: str) -> tuple:
    """Parse a version tag like 'v0.2.18' into a numeric tuple for comparison.

    Only dot-separated pieces that are entirely digits are kept; the first
    piece with any non-digit (prerelease/build suffix like '-beta1' or
    '18-rc.2') terminates parsing — prerelease tags can never compare as
    newer than a released version. Unparseable input → () (never newer).
    """
    if not tag:
        return ()
    s = tag.strip()
    if s.startswith("v"):
        s = s[1:]
    parts = []
    for piece in s.split("."):
        if piece.isdigit():
            parts.append(int(piece))
        else:
            break  # prerelease/build suffix — stop, drop the rest
    return tuple(parts)


def is_newer(latest: tuple, current: tuple) -> bool:
    """True iff latest > current (pure tuple comparison)."""
    return bool(latest) and latest > current


# ── State file (~/.emrg/.last_update_check.json) ──────────────────────────
# {checked_at: float epoch, latest_version: "0.2.18"|None,
#  prompted_version: "0.2.18"|None,
#  downloaded_version/path/sha: last successful auto-download (rant 12:10:12)}
# - checked_at: last successful check timestamp (TTL gate)
# - latest_version: last known latest from GitHub
# - prompted_version: the version for which a prompt was already shown
#   (idempotency: same version is only prompted once)
# - downloaded_*: populated by the background auto-download when a new
#   installer was fetched + SHA256-verified into ~/.emrg/updates/


def state_path() -> Path:
    return config_dir() / STATE_FILE_NAME


def updates_dir() -> Path:
    """Landing directory for auto-downloaded installers (~/.emrg/updates/)."""
    return config_dir() / UPDATES_DIR_NAME


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_state(state: dict) -> None:
    try:
        state_path().write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # state file is best-effort — never crash on write failure


def should_check(state: dict, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    """True iff a check is due (no record, or last check older than TTL)."""
    checked_at = state.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return True
    return (time.time() - checked_at) >= ttl_seconds


def should_prompt(state: dict, latest_version: str, current_version: str) -> bool:
    """True iff a prompt should be shown for this version (idempotent).

    Conditions: latest is parseable and newer than current, AND this exact
    version was not already prompted before.
    """
    if not latest_version:
        return False
    if not is_newer(parse_version(latest_version), parse_version(current_version)):
        return False
    return state.get("prompted_version") != latest_version


def mark_prompted(state: dict, version: str) -> dict:
    """Record that a prompt was shown for `version` (mutates + persists)."""
    state["prompted_version"] = version
    save_state(state)
    return state


async def check_latest_version(timeout: float = CHECK_TIMEOUT_SECONDS) -> Optional[str]:
    """Fetch the latest release tag from api.github.com.

    Returns the tag_name (e.g. '0.2.18') or None on ANY failure — silent,
    never raises, never logs noise. The caller retries at the next TTL.
    """
    release = await fetch_latest_release(timeout)
    if release is None:
        return None
    tag = release.get("tag_name") or ""
    return tag.lstrip("v") if tag else None


async def fetch_latest_release(timeout: float = CHECK_TIMEOUT_SECONDS) -> Optional[dict]:
    """Fetch the full releases/latest JSON (tag + assets + digests).

    Returns None on ANY failure (silent, never raises). The asset digest
    field is used by download_release_asset for SHA256 verification.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(RELEASES_LATEST_URL)
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception:
        return None


async def run_update_check_once(state: Optional[dict] = None) -> dict:
    """One deterministic check cycle: fetch latest, persist state, return result.

    Never raises. Used by the daemon background loop and directly testable.
    """
    state = state if state is not None else load_state()
    latest = await check_latest_version()
    if latest is None:
        return {"checked": False, "latest_version": None, "state": state}
    state["checked_at"] = time.time()
    state["latest_version"] = latest
    save_state(state)
    return {"checked": True, "latest_version": latest, "state": state}


# ── Auto-download (rant 2026-08-12T12:10:12) ──────────────────────────────


def platform_asset_name(version: str) -> Optional[str]:
    """Map the current platform+arch to the make-installer asset name.

    Artifact naming produced by scripts/make-installer (see build-release.yml):
      Windows: EMRG-<ver>-windows-x64.exe
      macOS:   EMRG-<ver>-macos-arm64.pkg / -x64.pkg (by machine arch)
      Linux:   EMRG-<ver>-linux-x86_64.AppImage / -aarch64.AppImage
    Returns None on unsupported platforms — the download is then skipped.
    """
    ver = (version or "").lstrip("v")
    if not ver:
        return None
    sysname = platform.system()
    machine = (platform.machine() or "").lower()
    if sysname == "Windows":
        return f"EMRG-{ver}-windows-x64.exe"
    if sysname == "Darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        return f"EMRG-{ver}-macos-{arch}.pkg"
    if sysname == "Linux":
        arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
        return f"EMRG-{ver}-linux-{arch}.AppImage"
    return None


def release_asset_url(version: str, asset_name: str) -> str:
    """Direct download URL for a release asset (no API rate limits)."""
    return f"{DOWNLOAD_BASE_URL}/v{(version or '').lstrip('v')}/{asset_name}"


def asset_sha256(release_data: dict, asset_name: str) -> Optional[str]:
    """Extract the asset digest from the GitHub release JSON, if present.

    GitHub exposes `digest` (e.g. "sha256:ab12…") on release assets. Older
    API responses may lack it → return None (caller skips verification and
    logs — never blocks the download on a missing digest).
    """
    if not isinstance(release_data, dict):
        return None
    for asset in release_data.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        if asset.get("name") != asset_name:
            continue
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            return digest[len("sha256:"):].strip().lower()
        return None  # asset found but no usable digest → skip verification
    return None  # asset not in release metadata (should not happen)


def sha256_file(path: Path) -> str:
    """Hex SHA256 of a file (streamed, memory-safe)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def download_release_asset(version: str, timeout: float = DOWNLOAD_TIMEOUT_SECONDS) -> dict:
    """Download the current platform's installer asset into ~/.emrg/updates/.

    Behavior (rant 2026-08-12T12:10:12):
    - only the current platform's asset is fetched (platform_asset_name)
    - stream + Range header → interrupted downloads resume from the last byte
    - SHA256 verified against the release asset digest when available
      (digest missing → skip + log, do NOT block); mismatch → delete, retried
      at the next TTL
    - silent on any failure (never raises)

    Returns a state-update dict on success ({downloaded_version,
    downloaded_path, downloaded_sha}) or {} on failure.
    """
    asset_name = platform_asset_name(version)
    if not asset_name:
        return {}
    release = await fetch_latest_release()
    if release is None:
        return {}
    digest = asset_sha256(release, asset_name)

    dest_dir = updates_dir()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {}
    dest = dest_dir / asset_name
    part = dest_dir / f"{asset_name}.part"
    normalized = (version or "").lstrip("v")

    # Already downloaded + verified → nothing to do.
    if dest.exists():
        if digest:
            if sha256_file(dest) == digest:
                return {
                    "downloaded_version": normalized,
                    "downloaded_path": str(dest),
                    "downloaded_sha": digest,
                }
            try:
                dest.unlink()  # tampered → start over
            except OSError:
                pass
        else:
            # No digest available — accept the existing file (nothing to
            # verify against) and record it.
            return {
                "downloaded_version": normalized,
                "downloaded_path": str(dest),
                "downloaded_sha": "",
            }

    url = release_asset_url(version, asset_name)
    try:
        resume_from = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={resume_from}-"} if resume_from > 0 else {}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 206:
                    mode = "ab"  # partial content → resume appending
                elif resp.status_code == 200:
                    mode = "wb"  # server ignored Range → full rewrite
                else:
                    return {}
                with open(part, mode) as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
    except Exception:
        return {}  # interrupted — .part kept so the next TTL resumes

    sha = sha256_file(part)
    if digest and sha != digest:
        # verification failure → delete the partial/tampered file, retry next TTL
        try:
            part.unlink()
        except OSError:
            pass
        return {}
    try:
        os.replace(part, dest)
    except OSError:
        return {}
    return {
        "downloaded_version": normalized,
        "downloaded_path": str(dest),
        "downloaded_sha": sha,
    }
