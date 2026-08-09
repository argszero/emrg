"""Automatic update-check + notify (rant 2026-08-10T07:12:12).

Design (host-specified boundary): ONLY check for new versions and PROMPT —
never auto-download, never auto-install, never start an installer flow.
The prompt is lightweight and non-intrusive (TUI status line / GUI settings
about area), one prompt per version (idempotent via state file), silent on
network failure (retry at next TTL).

Check source: api.github.com (github.com:443 / raw.githubusercontent.com are
blocked on the host network — see git_utils / skills installer patterns).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import httpx

from emrg.config import config_dir

# Default TTL between checks (seconds). Host-configurable via [update] ttl_hours.
DEFAULT_TTL_SECONDS = 24 * 3600
# API endpoint — releases/latest never includes prereleases (semver-satisfying).
RELEASES_LATEST_URL = "https://api.github.com/repos/argszero/emrg/releases/latest"
CHECK_TIMEOUT_SECONDS = 10.0

STATE_FILE_NAME = ".last_update_check.json"


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
#  prompted_version: "0.2.18"|None}
# - checked_at: last successful check timestamp (TTL gate)
# - latest_version: last known latest from GitHub
# - prompted_version: the version for which a prompt was already shown
#   (idempotency: same version is only prompted once)


def state_path() -> Path:
    return config_dir() / STATE_FILE_NAME


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
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(RELEASES_LATEST_URL)
            if resp.status_code != 200:
                return None
            data = resp.json()
            tag = data.get("tag_name") or ""
            return tag.lstrip("v") if tag else None
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
