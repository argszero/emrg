"""Global cross-project session index (rant 2026-08-13T16:42:22).

Maintains a single JSON map ``session_id -> absolute session directory`` at
``~/.emrg/sessions_index.json`` so that any session (or the agent in any
session) can locate and read another project's conversation records.

Design (host-finalized, minimal index):
- The index stores ONLY the session_id → directory mapping. Everything else
  (title, message_count, updated_at, history, memory) is read on demand from
  the target session's meta.json / history.jsonl / memory/MEMORY.md.
- Write hooks live in ``Session._save_meta_with_title`` (create/append/compact/
  rename/clear all funnel through it) and ``Session.delete``.
- A startup scan in the daemon (``rebuild_sessions_index``) backfills sessions
  that predate this feature, including unregistered projects under ~/.emrg.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from emrg.config import config_dir

# The "is this path a regular file" rule is the tool layer's, reached at a path. Imported
# rather than restated, for the reason `emrg/memory.py` gives at the same import: the
# boundary is one fact, and a second spelling of it is a second boundary that can drift.
from emrg.tools.base import non_regular_kind

logger = logging.getLogger(__name__)

_INDEX_FILENAME = "sessions_index.json"

# Subtrees that never contain session dirs but are large/irrelevant — pruning
# them keeps the recursive ~/.emrg scan fast (a full Python dist under
# install/, git history, node_modules, etc. would dominate the walk).
_PRUNE_DIRS = {
    "install", "updates", "logs", ".git", "node_modules", ".venv",
    "__pycache__", "dist", "build", ".cache", "Cache",
}


def sessions_index_path() -> Path:
    """Return the global index file path (~/.emrg/sessions_index.json)."""
    return config_dir() / _INDEX_FILENAME


def _load(index_path: Path) -> dict[str, str]:
    """Read the index; a corrupt, missing or non-regular file yields an empty dict.

    Never raises, and that promise is the caller's too: ``_save_meta_with_title`` records it
    for the write half ("a failed index write must not break session creation or message
    persistence") and calls this through ``upsert_session_index`` with no ``try`` of its own.
    Every session create, append, rename, clear and delete comes through here, so a raise
    would take message persistence down with it.

    The subject's **kind** is asked before it is opened, because opening it is what decides
    whether this call returns at all: ``read_text`` on a FIFO blocks at the *open* until a
    writer appears, raises nothing, and so the ``except`` below cannot bound the consequence.
    Measured 2026-10-11 (`cyc20261011-123924`) on master `99477516`, one child process per
    arm under an 8 s cap: a ``mkfifo`` at the index path made ``_load``, ``_upsert`` and
    ``rebuild_sessions_index`` **not return in 8 s**, where the same fixture with a regular
    file returned in 0.00 s.

    A non-regular subject takes **the answer this function already gives a corrupt file** —
    logged, and ``{}``. Raising would be a worse answer than the hang it replaces: every
    message would fail to persist instead of one call blocking. The index is a derived cache
    rather than a source of truth, which is what makes the empty answer safe:
    ``rebuild_sessions_index`` rebuilds it from the session directories on disk, and each
    caller re-establishes the entry it cares about. ``_write`` needs no guard of its own — it
    is ``mkstemp`` plus ``os.replace``, which renames over the destination without opening it
    (measured: a FIFO destination returns in 0.00 s and is left a regular file).

    ``exists()`` was the guard this used to lean on, and it is *true* for a FIFO — the reading
    that let the block through — so it is removed rather than kept as a second opinion, for
    the reason ``emrg/skills/registry.py`` gives at its own two sites. ``non_regular_kind``
    answers both halves of the question the old line got wrong: it names the kind when there
    is one, and raises ``FileNotFoundError`` for a missing path, which is the ``OSError``
    handled below.

    ``UnicodeDecodeError`` is in that ``except`` for the same reason: bytes that are not
    UTF-8 are one more form of *corrupt*, and the promise above says corrupt is answered, not
    raised. Measured the same day on the same master: a ``b"\\xff\\xfe..."`` at the index path
    raised ``UnicodeDecodeError`` out of this function, through ``upsert_session_index``, into
    the session-save path that documents it as never raising.
    """
    try:
        kind = non_regular_kind(index_path)
    except OSError:
        # Missing or unstattable — the empty answer a missing index has always got.
        return {}
    if kind:
        logger.warning(
            "sessions index %s is %s, not a regular file — treating it as corrupt",
            index_path, kind,
        )
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        logger.warning("corrupt sessions index %s — resetting", index_path)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _write(data: dict[str, str], index_path: Path) -> None:
    """Atomically write the index (tmp file + os.replace); never raises."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(index_path.parent), prefix=".sessions_index_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, index_path)
    except OSError:
        logger.warning("failed to write sessions index %s", index_path, exc_info=True)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _upsert(session_id: str, session_dir: str, index_path: Path) -> None:
    """Idempotently set index[session_id] = session_dir (skip if unchanged)."""
    data = _load(index_path)
    if data.get(session_id) == session_dir:
        return  # already correct — avoid a redundant rewrite on every meta save
    data[session_id] = session_dir
    _write(data, index_path)


def upsert_session_index(session_id: str, session_dir: Path) -> None:
    """Record a session in the global index (write hook for Session meta saves)."""
    _upsert(str(session_id), str(session_dir), sessions_index_path())


def _remove(session_id: str, index_path: Path) -> None:
    data = _load(index_path)
    if session_id in data:
        del data[session_id]
        _write(data, index_path)


def remove_session_index(session_id: str) -> None:
    """Remove a session from the global index (delete hook for Session.delete)."""
    _remove(str(session_id), sessions_index_path())


def _read_meta_session_id(meta_path: Path) -> str | None:
    """Return the session_id from a meta.json, or None if missing/corrupt.

    The subject's **kind** is asked before it is opened, because opening it is what decides
    whether this call returns at all: `read_text` on a FIFO blocks at the *open* until a
    writer appears, so the `except OSError` below cannot bound the consequence — nothing is
    raised and the walk simply never ends. Measured 2026-10-11 (`cyc20261011-070311`): a
    `mkfifo` at `<project>/.emrg/sessions/s1/meta.json` made `_iter_project_sessions` **not
    return in 8 s**. Both walkers call through here (`_iter_project_sessions`,
    `_iter_nested_sessions`), and the path is under the *workspace*, which is the agent's
    own write surface — the argument `#2076` made for `.emrg/memory/`.

    Skipped rather than raised, for this function's own reason: it already answers a
    missing or unparseable `meta.json` with `None` so the walk keeps going, and a session
    directory can legitimately hold anything. A special file is one more entry the index
    cannot describe, and the kind is named in the log so the omission is diagnosable.
    """
    try:
        kind = non_regular_kind(meta_path)
        if kind:
            logger.debug(
                "sessions index: skipping %s — it is %s, not a regular file",
                meta_path, kind,
            )
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    sid = meta.get("session_id")
    return str(sid) if sid else None


def _iter_project_sessions(project_path: Path):
    """Yield (session_id, session_dir) from <project>/.emrg/sessions/*/meta.json."""
    sessions_dir = project_path / ".emrg" / "sessions"
    if not sessions_dir.is_dir():
        return
    for entry in sorted(sessions_dir.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / "meta.json"
        if not meta_path.exists():
            continue
        sid = _read_meta_session_id(meta_path)
        if sid:
            yield sid, str(entry)


def _iter_nested_sessions(root: Path):
    """Yield (session_id, session_dir) for every <x>/.emrg/sessions under root.

    Recursively walks ``root`` (pruning heavy subtrees) so unregistered
    projects under ~/.emrg (e.g. ~/.emrg itself, ~/.emrg/source) are covered,
    not just paths listed in projects.yml.
    """
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        if os.path.basename(dirpath) == "sessions" and os.path.basename(
            os.path.dirname(dirpath)
        ) == ".emrg":
            sessions_dir = Path(dirpath)
            for entry in sorted(sessions_dir.iterdir()):
                if not entry.is_dir():
                    continue
                meta_path = entry / "meta.json"
                if not meta_path.exists():
                    continue
                sid = _read_meta_session_id(meta_path)
                if sid:
                    yield sid, str(entry)


def rebuild_sessions_index(
    config_root: Path, project_paths: list[str] | None = None
) -> int:
    """Backfill the index from on-disk sessions (daemon startup scan).

    Scans ``config_root`` recursively (covers ~/.emrg and anything nested under
    it) plus each explicit project path (covers projects outside ~/.emrg), and
    upserts every discovered session into ``config_root/sessions_index.json``.
    Entries whose session directory no longer exists on disk are pruned (e.g.
    sessions deleted out-of-band); entries pointing to live directories are
    preserved even when the scan did not rediscover them.
    Returns the number of distinct sessions indexed.
    """
    index_path = config_root / _INDEX_FILENAME
    data = _load(index_path)

    found: dict[str, str] = {}
    for sid, sdir in _iter_nested_sessions(config_root):
        found[sid] = sdir
    for p in project_paths or []:
        if not p:
            continue
        try:
            for sid, sdir in _iter_project_sessions(Path(p)):
                found[sid] = sdir
        except OSError:
            continue

    for sid, sdir in found.items():
        data[sid] = sdir

    # Prune stale entries whose session directory no longer exists on disk
    # (removed out-of-band, outside the Session.delete hook). Rebuild stays a
    # pure backfill for everything still alive — manual entries that point to a
    # live directory are preserved.
    stale = []
    for sid, sdir in data.items():
        try:
            alive = Path(sdir).exists()
        except (OSError, ValueError):
            alive = False
        if not alive:
            stale.append(sid)
    for sid in stale:
        del data[sid]

    _write(data, index_path)
    return len(data)
