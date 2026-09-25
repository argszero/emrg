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
import shutil
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
# The version the install replaced last time (written by the upgrade prompt
# before it overwrites version.txt — PR #912). It names the one backup that a
# failed upgrade can still be restored from, so the retention below keeps it.
PREVIOUS_VERSION_FILE = INSTALL_DIR / "previous-version.txt"
BACKUP_DIR = Path.home() / ".emrg" / "upgrade-backup"
GUI_SRC = Path(__file__).parent.parent / "gui"  # evolution repo's emrg/gui
# Hard-coded 5-minute check interval (host: not configurable).
TICK_INTERVAL = 300
# What one *attempt* costs, and therefore how long an attempt that did not install
# is held back (issue #1598). `_trigger` starts a full LLM session, so a target
# whose install can never succeed costs one session per tick — 288 a day — and
# before this the code had no way to tell that attempt from the first one. The
# base wait is 30 minutes, doubling per consecutive ineffective attempt up to the
# ceiling; a *different* target is never held back (see `_is_waiting`).
INEFFECTIVE_ATTEMPT_BACKOFF_SECONDS = 30 * 60
MAX_INEFFECTIVE_ATTEMPT_BACKOFF_SECONDS = 6 * 60 * 60
CHECK_TIMEOUT_SECONDS = 10.0
# Fixed upgrade session id — traceable, one session per upgrade.
SESSION_ID = "emrg-upgrade"
# How many pre-upgrade snapshots survive a prune, counted newest-first **by
# version, never by mtime** (issue #1389). One snapshot is a full copy of the
# install — 599 MB measured on the host that filed the issue — and the upgrade
# prompt writes one per upgraded version, so the directory grew without bound:
# 14 snapshots / 8.2 GB, of which exactly one was still restorable.
#
# One is enough because the count is not the only protection: the versions the
# install can still name (`version.txt`, `previous-version.txt`) are kept
# whatever their rank, and `previous-version.txt` *is* the rollback target — the
# prompt only ever restores from `backup_dir/<current_version>` (step 4). So the
# surviving snapshot is the one a failed upgrade can actually be rolled back to;
# a larger bound would only keep generations that nothing can name. The host
# reached the same rule by hand before this policy existed (the cleanup of
# 2026-09-18 kept `previous-version.txt`'s snapshot and nothing else).
BACKUP_KEEP = 1


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


# ── Retention of the pre-upgrade snapshots (issue #1389) ───────────────────


def _snapshot_version(name: str) -> tuple:
    """The version a snapshot directory's name denotes, or () if it denotes none.

    Stricter than `parse_version` on purpose. `parse_version` is a *comparison*
    helper that deliberately stops at a suffix ("0.2.18-rc.2" → (0, 2, 18)), and
    that is right for ordering prereleases. It is wrong for *recognition*, which
    here decides a deletion: reading it that way, a directory named
    `0.2.18-rc.2` would pass for the snapshot of `0.2.18` and could be removed
    in that snapshot's place. The prompt writes `backup_dir/<current_version>`
    and `version.txt` holds a plain version (a `v` prefix at most), so the whole
    name must be the version — nothing else is this feature's to delete.
    """
    stripped = name.lstrip("v")
    version = parse_version(stripped)
    if not version or ".".join(str(piece) for piece in version) != stripped:
        return ()
    return version


def backup_snapshot_versions(backup_dir: Path) -> list[tuple[tuple, str]]:
    """The snapshot directories in `backup_dir`, ascending by parsed version.

    A snapshot is a **directory whose name is a version**, because that is the
    only shape this feature writes: the upgrade prompt backs the current install
    up to `backup_dir/<current_version>/` (step 4 of
    `prompts/upgrade_prompt.j2`). Everything else is left alone rather than
    guessed at — a name this module cannot read as a version is not a file it
    may delete, and neither is a symlink (which `rmtree` would follow) or a
    plain file. A missing or unreadable directory is `[]`, not an error: a fresh
    install has no backups at all, and that must stay a non-event.
    """
    try:
        entries = list(backup_dir.iterdir())
    except OSError:
        return []
    snapshots = [
        (_snapshot_version(entry.name), entry.name)
        for entry in entries
        if not entry.is_symlink() and entry.is_dir() and _snapshot_version(entry.name)
    ]
    snapshots.sort()
    return snapshots


def backup_snapshots_to_prune(
    backup_dir: Path, keep: int = BACKUP_KEEP, keep_versions: tuple = ()
) -> list[str]:
    """Which snapshot names a prune would remove — the decision, doing nothing.

    Separated from the removal so the policy can be read and tested without a
    filesystem side effect, which is the property the issue asks for: "the
    policy must be executable and testable with a scratch `backup_dir`".

    Two protections, and they answer different questions:

    * the newest `keep` snapshots by **version** — the floor. At `BACKUP_KEEP=1`
      this is the safety net for a snapshot directory whose version files are
      missing or unreadable, where the protection below has nothing to name;
    * every snapshot whose parsed version is named by `keep_versions` — the
      versions `version.txt` and `previous-version.txt` record, i.e. what the
      install can actually restore from. That is what keeps the snapshots that
      matter alive even when they are not the newest (a downgrade, or a
      hand-copied install), and it is why `keep=1` cannot drop the rollback
      target.

    Names, not paths, so the caller cannot be handed a path outside the
    directory it scanned. Ascending, so a caller that logs the removals logs
    them oldest-first.
    """
    snapshots = backup_snapshot_versions(backup_dir)
    protected = {parse_version(name) for name in keep_versions if parse_version(name)}
    survivors = {name for _ver, name in snapshots[len(snapshots) - keep :]} if keep > 0 else set()
    survivors |= {name for ver, name in snapshots if ver in protected}
    return [name for _ver, name in snapshots if name not in survivors]


def _installed_versions() -> tuple:
    """The versions the install can still name: what runs, and what it replaced.

    Both files are read defensively — the policy must not depend on them being
    there. `version.txt` and `previous-version.txt` hold the *tag* (the prompt
    writes `{{ target_tag }}`), so the leading `v` is stripped before parsing;
    `parse_version` would drop it anyway, but saying so keeps the two readers in
    step with `_read_local_version`.
    """
    versions = []
    for path in (VERSION_FILE, PREVIOUS_VERSION_FILE):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            versions.append(text.lstrip("v"))
    return tuple(versions)


def prune_upgrade_backups(backup_dir: Optional[Path] = None, keep: int = BACKUP_KEEP) -> list[str]:
    """Remove the pre-upgrade snapshots the retention no longer keeps.

    Returns the names it removed, oldest-first, so a caller (or a test) reads
    back what happened rather than inferring it. Removal is one `shutil.rmtree`
    per snapshot, each in its own try: a snapshot that cannot be removed is
    logged and skipped, never allowed to abort the sweep or to hide the ones
    that were.

    `backup_dir` is resolved **at call time** from the module constant, which is
    what makes the daemon's own call site patchable: a test that boots a server
    must never point this at the host's real snapshots (see `tests/conftest.py`'s
    upgrade guard), and a default argument would bind the real path at import.
    """
    target = Path(backup_dir) if backup_dir is not None else BACKUP_DIR
    removed: list[str] = []
    for name in backup_snapshots_to_prune(target, keep=keep, keep_versions=_installed_versions()):
        try:
            shutil.rmtree(target / name)
        except OSError:
            logger.warning(
                "upgrade: could not remove the superseded backup %s", target / name,
                exc_info=True,
            )
            continue
        removed.append(name)
    if removed:
        logger.info(
            "upgrade: removed %d superseded upgrade backup(s), kept the newest %d: %s",
            len(removed), keep, ", ".join(removed),
        )
    return removed


def _published_epoch(published_at: str) -> Optional[float]:
    """ISO published_at → epoch seconds; None on unparseable input."""
    try:
        iso = published_at.replace("Z", "+00:00")
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


def _now() -> float:
    """The clock the attempt wait is measured against.

    A function rather than an inline `time.monotonic()` for the reason
    `prune_upgrade_backups` resolves `BACKUP_DIR` at call time: a module global a
    test can move. `monotonic`, not `time.time` — the quantity is an interval, and a
    host clock step (NTP, sleep/resume) must not skip or extend the wait.
    """
    return time.monotonic()


class UpgradeManager:
    """Program-side upgrade trigger (all logic lives here — daemon only
    references it; the daemon must NOT grow upgrade logic, host decision).

    tick() is called by the daemon every TICK_INTERVAL seconds.
    """

    def __init__(self, config: UpdateConfig, run_session_cb: Callable):
        self._config = config
        self._run_session_cb = run_session_cb  # daemon-provided session runner
        self._inflight = False  # upgrade session in progress (re-entry guard)
        # Attempts that did not move version.txt (issue #1598). In-memory by
        # design: a daemon restart forgets the wait and tries again, which is
        # the same shape `_trigger`'s docstring gives an interrupted session.
        self._attempted_tag: Optional[str] = None
        self._attempted_at: float = 0.0
        self._ineffective_attempts = 0

    # ── Public: called by the daemon every 5 minutes ──────────────────────
    async def tick(self) -> None:
        if not self._config.enabled:
            return
        if self._inflight:
            return  # re-entry guard: skip while an upgrade session runs
        # Retention of the pre-upgrade snapshots (issue #1389). Here, and after
        # the re-entry guard, because this is the only process that outlives the
        # upgrades that fill the directory — and never while a session may be
        # writing the next snapshot. A failed sweep is not a failed tick.
        try:
            prune_upgrade_backups()
        except Exception:
            logger.warning("upgrade: backup retention failed", exc_info=True)
        target = await self._find_target_tag()
        if not target:
            return  # nothing eligible this round
        local = self._read_local_version()
        if local == target.lstrip("v"):
            return  # already at target — nothing to do
        if self._is_waiting(target):
            return  # an attempt at THIS tag did not move version.txt; still paying it off
        await self._trigger(target)
        self._record_attempt(target, before=local)

    # ── What an attempt that did not install costs the next one ───────────
    def _ineffective_backoff(self) -> float:
        """Seconds to wait before re-attempting a tag that did not install.

        The shape the scheduler's `_connect_backoff` uses — exponential in the
        consecutive-ineffective count, with a ceiling — because the defect is the
        same one a level down: a per-tick retry whose cost is not the tick but the
        work a tick starts. Measured live while writing this (2026-09-25): the
        upgrade session's own record was rewritten at 06:44:30 local, i.e. a full
        LLM session every five minutes for a target whose writes are refused, so
        `version.txt` never moves (issue #1598 holds the probe table and `Tick 221`).
        """
        if self._ineffective_attempts <= 0:
            return 0.0
        n = min(self._ineffective_attempts, 10)  # cap the exponent growth
        return min(
            INEFFECTIVE_ATTEMPT_BACKOFF_SECONDS * (2 ** (n - 1)),
            MAX_INEFFECTIVE_ATTEMPT_BACKOFF_SECONDS,
        )

    def _is_waiting(self, target: str) -> bool:
        """Whether `target` was attempted too recently to be attempted again.

        A different tag is *never* held back, and that clause is the one that keeps
        this from delaying the repair it is meant to make affordable: a new release
        may be the one that installs, and for a host whose chain is blocked by the
        release it keeps re-installing, the next release **is** the fix arriving.
        """
        if self._ineffective_attempts <= 0 or self._attempted_tag != target:
            return False
        return (_now() - self._attempted_at) < self._ineffective_backoff()

    def _record_attempt(self, target: str, before: str) -> None:
        """Charge the next attempt for how this one ended.

        `version.txt` moving is the only evidence an upgrade session did its job,
        and it is read here rather than inside `_trigger`, which keeps its single
        responsibility. A *whole* upgrade is not waited for: `_trigger` returns when
        the session ends, so this reads what the session left behind.

        Counted whether the session was refused or crashed — the same slot of the
        host's budget is spent either way, and the alternative (distinguishing them
        here) would need `_trigger` to report *why* it ended, which is knowledge the
        caller cannot use: the wait is bounded, and a new target resets it.

        A third outcome is neither: an attempt that leaves **no** readable version at
        all. `_read_local_version` answers `""` for a file that is missing or
        unreadable, so it is not compared — an empty answer is the absence of
        evidence, and reading it as a new version would clear the wait for a session
        that destroyed `version.txt`, which is the moment the chain is most broken
        rather than the one where it worked.
        """
        after = self._read_local_version()
        if after and after != before:
            self._ineffective_attempts = 0  # the install moved: this path works
            self._attempted_tag = None
            return
        self._ineffective_attempts += 1
        self._attempted_tag = target
        self._attempted_at = _now()
        logger.info(
            "upgrade: %s left version.txt at %r (%d ineffective attempt(s)); "
            "next attempt of that tag in %.0f s",
            target,
            after,
            self._ineffective_attempts,
            self._ineffective_backoff(),
        )

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
