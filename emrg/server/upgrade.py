"""Automatic upgrade trigger (rant 2026-08-20T12:33:59 — 自动升级重构).

Design (host-specified boundaries, verbatim intent):
- The upgrade does NOT download an installer package, but the effect must be
  fully equivalent to "installing the corresponding release installer".
- The local evolution repo (~/.emrg/evolution/emrg, a git repo with all
  tags/full history) already has the latest code — upgrade takes it from
  there.
- The PROGRAM only triggers: query GitHub releases API → delay-filter →
  compare with local install/version.txt → render upgrade_prompt.j2 → start
  an agent session ("emrg-upgrade"). No success/failure judgment, no state
  files, no retries, no version.txt comparison for success — the agent does
  all of that, template-driven. The program's ONLY state is the in-flight
  re-entry flag.
- The old mechanism (emrg/update_check.py: download installer / state file /
  check-TTL) is fully removed — parse_version / is_newer are migrated here.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx

from emrg.config import UpdateConfig

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
# releases list (per_page=30, includes tag_name + published_at) — never the
# /latest endpoint: delay filtering needs published_at of every recent release.
RELEASES_URL = "https://api.github.com/repos/argszero/emrg/releases?per_page=30"
# Fixed work directory: isolated clone of the evolution repo — the agent can
# freely checkout any tag without touching the evolution task's worktree.
UPGRADE_WORK_DIR = Path.home() / ".emrg" / "upgrade-work" / "emrg"
INSTALL_DIR = Path.home() / ".emrg" / "install"
VERSION_FILE = INSTALL_DIR / "version.txt"
BACKUP_DIR = Path.home() / ".emrg" / "upgrade-backup"
GUI_SRC = Path(__file__).parent.parent / "gui"  # evolution repo's emrg/gui
# Hard-coded 5-minute check interval (host: not configurable).
TICK_INTERVAL = 300
CHECK_TIMEOUT_SECONDS = 10.0
# Fixed upgrade session id — traceable, one session per upgrade.
SESSION_ID = "emrg-upgrade"


def parse_version(tag: str) -> tuple:
    """Parse a version tag like 'v0.2.18' into a numeric tuple for comparison.

    Migrated from the removed emrg/update_check.py (rant 2026-08-20T12:33:59).
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


def _published_epoch(published_at: str) -> Optional[float]:
    """ISO published_at → epoch seconds; None on unparseable input."""
    try:
        iso = published_at.replace("Z", "+00:00")
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


class UpgradeManager:
    """Program-side upgrade trigger (all logic lives here — daemon only
    references it; the daemon must NOT grow upgrade logic, host decision).

    tick() is called by the daemon every TICK_INTERVAL seconds.
    """

    def __init__(self, config: UpdateConfig, run_session_cb: Callable):
        self._config = config
        self._run_session_cb = run_session_cb  # daemon-provided session runner
        self._inflight = False  # upgrade session in progress (re-entry guard)

    # ── Public: called by the daemon every 5 minutes ──────────────────────
    async def tick(self) -> None:
        if not self._config.enabled:
            return
        if self._inflight:
            return  # re-entry guard: skip while an upgrade session runs
        target = await self._find_target_tag()
        if not target:
            return  # nothing eligible this round
        local = self._read_local_version()
        if local == target.lstrip("v"):
            return  # already at target — nothing to do
        await self._trigger(target)

    # ── Target discovery ──────────────────────────────────────────────────
    async def _find_target_tag(self) -> Optional[str]:
        """Delay-filtered newest eligible release tag, or None.

        Network failure / non-200 / 429 → None (silent, retried next tick —
        5-minute cadence is well below the unauthenticated 60/h rate limit,
        no complex backoff needed).
        """
        try:
            async with httpx.AsyncClient(
                timeout=CHECK_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                resp = await client.get(RELEASES_URL)
                if resp.status_code != 200:
                    return None
                data = resp.json()
                if not isinstance(data, list):
                    return None
        except Exception:
            logger.debug("upgrade: releases fetch failed (retry next tick)", exc_info=True)
            return None

        cutoff = time.time() - self._config.delay_minutes * 60
        best_tag: Optional[str] = None
        best_ver: tuple = ()
        for rel in data:
            if not isinstance(rel, dict):
                continue
            tag = rel.get("tag_name") or ""
            published_ts = _published_epoch(rel.get("published_at") or "")
            if published_ts is None or published_ts > cutoff:
                continue  # unparseable or not yet eligible (delay window)
            ver = parse_version(tag)
            if not ver:
                continue
            if ver > best_ver:
                best_tag = tag
                best_ver = ver
        return best_tag

    # ── Local state ───────────────────────────────────────────────────────
    def _read_local_version(self) -> str:
        """Current installed version from install/version.txt ("" on failure).

        Normalized without the leading 'v' — the target tag keeps its 'v'
        prefix when passed to the template/agent (git tag lookup needs it).
        """
        try:
            text = VERSION_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return text.lstrip("v")

    # ── Trigger ───────────────────────────────────────────────────────────
    async def _trigger(self, tag: str) -> None:
        """Render the upgrade prompt and start the agent session.

        in-flight is set BEFORE the session starts and cleared in finally —
        a daemon restart kills the session and the next tick re-triggers
        naturally (version.txt unchanged → trigger again; correct semantics).
        """
        self._inflight = True
        try:
            prompt = self._render_prompt(tag)
            await self._run_session_cb(
                session_id=SESSION_ID,
                cwd=str(UPGRADE_WORK_DIR),
                prompt=prompt,
            )
        except Exception:
            logger.debug("upgrade: session trigger failed (retry next tick)", exc_info=True)
        finally:
            self._inflight = False

    def _render_prompt(self, target_tag: str) -> str:
        """Render upgrade_prompt.j2 (same live-reload FileSystemLoader
        mechanism as system.j2 / vibe_check.j2 — host edits take effect
        without a daemon restart)."""
        import jinja2  # type: ignore[import-untyped]

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(Path(__file__).parent / "prompts"),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template("upgrade_prompt.j2")
        return template.render(
            source_repo=str(Path.home() / ".emrg" / "evolution" / "emrg"),
            upgrade_work=str(UPGRADE_WORK_DIR),
            install_dir=str(INSTALL_DIR),
            target_tag=target_tag,
            current_version=self._read_local_version(),
            delay_minutes=self._config.delay_minutes,
            version_file=str(VERSION_FILE),
            gui_src=str(GUI_SRC),
            backup_dir=str(BACKUP_DIR),
        )
