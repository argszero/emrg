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


#: The three answers a liveness check can give. Named rather than expressed as
#: a boolean because the third one is not the second — see `liveness` below.
PRESENT = "present"
GONE = "gone"
UNMEASURABLE = "unmeasurable"


def liveness(path: Path) -> str:
    """Is ``path`` still on disk? **Three** answers, and the third is not "no".

    ``rebuild_sessions_index`` prunes index entries whose session directory has
    vanished, and pruning **rewrites the whole index** — so this reading decides
    whether a row is destroyed. It used to be ``Path(sdir).exists()`` inside an
    ``except (OSError, ValueError): alive = False``, which reads "I could not
    look" as "it is not there":

    * on Python 3.13 ``Path.exists()`` no longer swallows errors — it raises for
      anything that is not "not found". *Measured 2026-10-03
      (`cyc20261003-110524`):* ``PermissionError`` for a directory whose parent
      refuses traversal;
    * the ``except`` then turned that into ``False``, the entry became "stale",
      and it was deleted. With the whole ``sessions`` tree unreadable, the
      rebuild answered **0** and replaced the index with ``{}`` — every
      project's session rows gone, no exception, and the daemon's log line
      discarded (server logs go to DEVNULL). The directory was still on disk.

    So the three states are distinguished here, once: only ``GONE`` may be acted
    on. ``UNMEASURABLE`` is returned for any other ``OSError`` (permission, a
    stale network handle, a name the filesystem rejects) and for ``ValueError``
    (an embedded NUL), and its callers keep what they have.
    """
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return GONE
    except (OSError, ValueError):
        return UNMEASURABLE
    return PRESENT


def _load(index_path: Path) -> dict[str, str]:
    """Read the index; corrupt/missing file yields an empty dict (never raises)."""
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
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
    """Return the session_id from a meta.json, or None if missing/corrupt."""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    sid = meta.get("session_id")
    return str(sid) if sid else None


def _sessions_in(entry: Path, unreadable: list[str] | None = None):
    """Yield (session_id, session_dir) for one session directory, or nothing.

    The **single home** for "this directory could not be read". It is here and
    not at each call site because a second producer of the same report is a
    second thing to keep true: the caller used to have its own
    ``except OSError: continue``, which meant a mutation that removed this
    guard changed nothing a reader could see.

    Every filesystem call is inside the try, because on Python 3.13
    ``Path.is_dir()``/``exists()`` raise for anything that is not "not found"
    rather than answering ``False``.
    """
    try:
        if not entry.is_dir():
            return
        meta_path = entry / "meta.json"
        if not meta_path.exists():
            return
        sid = _read_meta_session_id(meta_path)
    except OSError:
        if unreadable is not None:
            unreadable.append(str(entry))
        return
    if sid:
        yield sid, str(entry)


def _iter_project_sessions(project_path: Path, unreadable: list[str] | None = None):
    """Yield (session_id, session_dir) from <project>/.emrg/sessions/*/meta.json.

    A project whose ``sessions`` directory exists but cannot be read used to
    raise into the caller's blanket ``except OSError: continue`` and vanish:
    one unreadable project contributed *nothing* and the rebuild said so
    nowhere. A skipped subject is not a read one.
    """
    sessions_dir = project_path / ".emrg" / "sessions"
    try:
        if not sessions_dir.is_dir():
            return
        entries = sorted(sessions_dir.iterdir())
    except OSError:
        if unreadable is not None:
            unreadable.append(str(sessions_dir))
        return
    for entry in entries:
        yield from _sessions_in(entry, unreadable)


def _iter_nested_sessions(root: Path, unreadable: list[str] | None = None):
    """Yield (session_id, session_dir) for every <x>/.emrg/sessions under root.

    Recursively walks ``root`` (pruning heavy subtrees) so unregistered
    projects under ~/.emrg (e.g. ~/.emrg itself, ~/.emrg/source) are covered,
    not just paths listed in projects.yml.

    ``unreadable`` collects every directory the walk could not list. It has to
    be passed to ``os.walk`` as ``onerror``: the default ignores a failed
    ``scandir`` outright, so a subtree that could not be read and a subtree with
    nothing in it arrive here as the same empty result.
    """

    def _note(error: OSError) -> None:
        if unreadable is not None:
            unreadable.append(getattr(error, "filename", None) or str(root))

    for dirpath, dirnames, _ in os.walk(root, followlinks=False, onerror=_note):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        if os.path.basename(dirpath) == "sessions" and os.path.basename(
            os.path.dirname(dirpath)
        ) == ".emrg":
            sessions_dir = Path(dirpath)
            try:
                entries = sorted(sessions_dir.iterdir())
            except OSError:
                if unreadable is not None:
                    unreadable.append(str(sessions_dir))
                continue
            for entry in entries:
                yield from _sessions_in(entry, unreadable)


def rebuild_sessions_index(
    config_root: Path, project_paths: list[str] | None = None
) -> int:
    """Backfill the index from on-disk sessions (daemon startup scan).

    Scans ``config_root`` recursively (covers ~/.emrg and anything nested under
    it) plus each explicit project path (covers projects outside ~/.emrg), and
    upserts every discovered session into ``config_root/sessions_index.json``.
    Entries whose session directory **is confirmed gone** are pruned (e.g.
    sessions deleted out-of-band); entries pointing to live directories are
    preserved even when the scan did not rediscover them, and so are entries
    whose liveness check could not be answered at all — this function rewrites
    the whole index, so "I could not look" must never mean "delete it" (see
    ``liveness``).
    Returns the number of distinct sessions indexed — a **lower bound** when any
    directory could not be read, which is reported rather than silently folded
    into the count.
    """
    index_path = config_root / _INDEX_FILENAME
    data = _load(index_path)

    found: dict[str, str] = {}
    unreadable: list[str] = []
    for sid, sdir in _iter_nested_sessions(config_root, unreadable):
        found[sid] = sdir
    for p in project_paths or []:
        if not p:
            continue
        # No guard here: the iterators own "could not be read" (see
        # `_sessions_in`). A second producer at this level made the report
        # unfalsifiable — a mutation that removed the iterator's own guard left
        # every observable answer unchanged.
        for sid, sdir in _iter_project_sessions(Path(p), unreadable):
            found[sid] = sdir

    for sid, sdir in found.items():
        data[sid] = sdir

    # Prune stale entries whose session directory no longer exists on disk
    # (removed out-of-band, outside the Session.delete hook). Rebuild stays a
    # pure backfill for everything still alive — manual entries that point to a
    # live directory are preserved.
    #
    # "No longer exists" is a *measured absence*, not a failed measurement: this
    # loop deletes rows and then rewrites the whole index, so an entry is dropped
    # only when the filesystem said it is not there. An entry whose check raised
    # is kept and counted — see `liveness`, and the measured 2026-10-03 incident
    # in its docstring where reading the two as one emptied the entire index.
    stale: list[str] = []
    unchecked: list[str] = []
    for sid, sdir in data.items():
        state = liveness(Path(sdir))
        if state == GONE:
            stale.append(sid)
        elif state == UNMEASURABLE:
            unchecked.append(sid)
    for sid in stale:
        del data[sid]
    if unchecked:
        logger.warning(
            "sessions index: %d entr(ies) could not be checked and were kept "
            "(their directories are neither confirmed present nor confirmed "
            "gone): %s",
            len(unchecked),
            ", ".join(sorted(unchecked)[:5]),
        )

    if unreadable:
        # A scan that could not look reports fewer sessions, and "fewer" is
        # indistinguishable from "there are fewer" without this line. It is the
        # reason the count returned below is a lower bound in this run.
        logger.warning(
            "sessions index: %d director(ies) could not be read, so this scan is "
            "partial and the count is a lower bound: %s",
            len(unreadable),
            ", ".join(sorted(set(unreadable))[:5]),
        )

    _write(data, index_path)
    return len(data)
