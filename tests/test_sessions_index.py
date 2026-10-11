"""Tests for the global cross-project session index (rant 2026-08-13T16:42:22)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from tests.bounded_read import DEADLINE_SECONDS, within
from emrg.session import Session
from emrg.sessions_index import (
    _iter_project_sessions,
    _load,
    _read_meta_session_id,
    _write,
    rebuild_sessions_index,
    remove_session_index,
    sessions_index_path,
    upsert_session_index,
)


def _index_path(tmp_path: Path) -> Path:
    """The redirected index path used by the autouse conftest fixture."""
    return tmp_path / "sessions_index.json"


class TestUpsertRemove:
    def test_upsert_writes_index(self, tmp_path):
        upsert_session_index("s_abc", tmp_path / "projA" / ".emrg" / "sessions" / "s_abc")
        data = _load(_index_path(tmp_path))
        assert data["s_abc"] == str(tmp_path / "projA" / ".emrg" / "sessions" / "s_abc")

    def test_upsert_multiple_sessions(self, tmp_path):
        upsert_session_index("s_1", tmp_path / "a" / "s_1")
        upsert_session_index("s_2", tmp_path / "b" / "s_2")
        data = _load(_index_path(tmp_path))
        assert data == {
            "s_1": str(tmp_path / "a" / "s_1"),
            "s_2": str(tmp_path / "b" / "s_2"),
        }

    def test_remove_deletes_entry(self, tmp_path):
        upsert_session_index("s_1", tmp_path / "a" / "s_1")
        upsert_session_index("s_2", tmp_path / "b" / "s_2")
        remove_session_index("s_1")
        data = _load(_index_path(tmp_path))
        assert "s_1" not in data
        assert "s_2" in data

    def test_remove_unknown_session_is_noop(self, tmp_path):
        upsert_session_index("s_1", tmp_path / "a" / "s_1")
        remove_session_index("s_unknown")
        data = _load(_index_path(tmp_path))
        assert data == {"s_1": str(tmp_path / "a" / "s_1")}

    def test_upsert_is_idempotent(self, tmp_path):
        """Re-upserting the same value skips the rewrite (no mtime bump)."""
        upsert_session_index("s_1", tmp_path / "a" / "s_1")
        path = _index_path(tmp_path)
        mtime_before = path.stat().st_mtime_ns
        upsert_session_index("s_1", tmp_path / "a" / "s_1")
        assert path.stat().st_mtime_ns == mtime_before

    def test_upsert_updates_changed_path(self, tmp_path):
        upsert_session_index("s_1", tmp_path / "a" / "s_1")
        upsert_session_index("s_1", tmp_path / "moved" / "s_1")
        data = _load(_index_path(tmp_path))
        assert data["s_1"] == str(tmp_path / "moved" / "s_1")


class TestLoad:
    def test_load_missing_returns_empty(self, tmp_path):
        assert _load(tmp_path / "nope.json") == {}

    def test_load_corrupt_returns_empty(self, tmp_path):
        p = tmp_path / "corrupt.json"
        p.write_text("{not valid json", encoding="utf-8")
        assert _load(p) == {}

    def test_load_non_dict_returns_empty(self, tmp_path):
        p = tmp_path / "list.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        assert _load(p) == {}

    def test_write_is_json_object(self, tmp_path):
        p = tmp_path / "idx.json"
        _write({"s_x": "/tmp/x"}, p)
        assert json.loads(p.read_text(encoding="utf-8")) == {"s_x": "/tmp/x"}


class TestRebuild:
    def _make_session(self, root: Path, sid: str) -> Path:
        """Create a session dir with meta.json under <root>/.emrg/sessions/."""
        sessions_dir = root / ".emrg" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        sdir = sessions_dir / sid
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "meta.json").write_text(
            json.dumps({"session_id": sid, "message_count": 0}), encoding="utf-8"
        )
        return sdir

    def test_rebuild_indexes_nested_sessions(self, tmp_path):
        cfg = tmp_path / "cfg"
        sdir = self._make_session(cfg, "s_nested")
        count = rebuild_sessions_index(cfg)
        assert count == 1
        data = _load(cfg / "sessions_index.json")
        assert data["s_nested"] == str(sdir)

    def test_rebuild_scans_project_paths_outside_root(self, tmp_path):
        cfg = tmp_path / "cfg"
        cfg.mkdir(parents=True, exist_ok=True)
        project = tmp_path / "outside_project"
        sdir = self._make_session(project, "s_outside")
        count = rebuild_sessions_index(cfg, project_paths=[str(project)])
        assert count == 1
        data = _load(cfg / "sessions_index.json")
        assert data["s_outside"] == str(sdir)

    def test_rebuild_prunes_heavy_dirs(self, tmp_path):
        """Sessions under install/ (bundled runtime) must not be indexed."""
        cfg = tmp_path / "cfg"
        self._make_session(cfg, "s_real")
        self._make_session(cfg / "install", "s_ignored")
        count = rebuild_sessions_index(cfg)
        assert count == 1
        data = _load(cfg / "sessions_index.json")
        assert "s_real" in data
        assert "s_ignored" not in data

    def test_rebuild_skips_meta_without_sid(self, tmp_path):
        cfg = tmp_path / "cfg"
        sessions_dir = cfg / ".emrg" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / "orphan").mkdir()
        (sessions_dir / "orphan" / "meta.json").write_text(
            json.dumps({"message_count": 0}), encoding="utf-8"
        )
        count = rebuild_sessions_index(cfg)
        assert count == 0

    def test_rebuild_preserves_manual_entries(self, tmp_path):
        """Manual entries pointing to a live directory survive the rebuild."""
        cfg = tmp_path / "cfg"
        cfg.mkdir(parents=True, exist_ok=True)
        manual_dir = tmp_path / "manual_dir"
        manual_dir.mkdir(parents=True, exist_ok=True)
        (cfg / "sessions_index.json").write_text(
            json.dumps({"s_manual": str(manual_dir)}), encoding="utf-8"
        )
        self._make_session(cfg, "s_scanned")
        count = rebuild_sessions_index(cfg)
        assert count == 2
        data = _load(cfg / "sessions_index.json")
        assert data["s_manual"] == str(manual_dir)
        assert "s_scanned" in data

    def test_rebuild_prunes_stale_entries(self, tmp_path):
        """Index entries whose session dir was deleted out-of-band are dropped."""
        cfg = tmp_path / "cfg"
        cfg.mkdir(parents=True, exist_ok=True)
        live_dir = tmp_path / "live"
        live_dir.mkdir(parents=True, exist_ok=True)
        dead_dir = tmp_path / "dead"
        dead_dir.mkdir(parents=True, exist_ok=True)
        (cfg / "sessions_index.json").write_text(
            json.dumps({"s_live": str(live_dir), "s_dead": str(dead_dir)}),
            encoding="utf-8",
        )
        # Delete the dead session's directory out-of-band (no Session.delete hook).
        shutil.rmtree(dead_dir)
        self._make_session(cfg, "s_scanned")
        count = rebuild_sessions_index(cfg)
        assert count == 2
        data = _load(cfg / "sessions_index.json")
        assert "s_dead" not in data
        assert data["s_live"] == str(live_dir)
        assert "s_scanned" in data


class TestSessionHooks:
    def test_session_create_indexes(self, tmp_path):
        session = Session.create(tmp_path)
        data = _load(_index_path(tmp_path))
        assert data[session.session_id] == str(session._dir)

    def test_session_rename_preserves_index_path(self, tmp_path):
        session = Session.create(tmp_path)
        before = _load(_index_path(tmp_path))[session.session_id]
        session.rename("Some Title")
        after = _load(_index_path(tmp_path))[session.session_id]
        assert after == before == str(session._dir)

    def test_session_delete_removes_index(self, tmp_path):
        session = Session.create(tmp_path)
        sid = session.session_id
        assert sid in _load(_index_path(tmp_path))
        Session.delete(sid, tmp_path)
        assert sid not in _load(_index_path(tmp_path))

    def test_session_delete_nonexistent_no_crash(self, tmp_path):
        assert Session.delete("s_never_existed", tmp_path) is False
        assert _load(_index_path(tmp_path)) == {}


def test_sessions_index_path_is_under_config_dir():
    """The default index path lives under ~/.emrg (config_dir)."""
    p = sessions_index_path()
    assert p.name == "sessions_index.json"


class TestAWalkByKindRatherThanByOpening:
    """A session entry that is not a regular file is skipped, not entered (issue #2083).

    `_read_meta_session_id` opened its subject inside `except (json.JSONDecodeError,
    OSError)` — which cannot bound a *block*: `read_text` on a FIFO waits at the open for a
    writer, raises nothing, and never returns. Measured 2026-10-11 (`cyc20261011-070311`) on
    master `3386f239`: a `mkfifo` at `<project>/.emrg/sessions/s1/meta.json` made
    `_iter_project_sessions` **not return in 8 s**.

    The path is under the *workspace* — the agent's own write surface — and both walkers
    (`_iter_project_sessions`, `_iter_nested_sessions`) read through this one function, so
    the kind is asked once where the open happens.
    """

    @staticmethod
    def _session(root: Path, sid: str):
        sdir = root / ".emrg" / "sessions" / sid
        sdir.mkdir(parents=True)
        (sdir / "meta.json").write_text(
            json.dumps({"session_id": sid}), encoding="utf-8"
        )
        return sdir

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
    def test_a_fifo_meta_is_skipped_and_the_other_sessions_still_read(self, tmp_path):
        """Both halves: the walk returns, and the sessions beside it are still indexed.

        A guard that raised out of the walk would pass "returns" while losing every session
        in the directory — the same loss the hang caused one step earlier.
        """
        self._session(tmp_path, "s_real")
        wedged = tmp_path / ".emrg" / "sessions" / "s_wedged"
        wedged.mkdir()
        os.mkfifo(wedged / "meta.json")

        rows = within(DEADLINE_SECONDS, lambda: list(_iter_project_sessions(tmp_path)))
        assert [sid for sid, _ in rows] == ["s_real"], rows

    def test_the_control_without_the_fifo(self, tmp_path):
        """Same fixture, a regular meta.json: both sessions read, so the kind is the axis."""
        self._session(tmp_path, "s_real")
        self._session(tmp_path, "s_also")
        rows = list(_iter_project_sessions(tmp_path))
        assert sorted(sid for sid, _ in rows) == ["s_also", "s_real"], rows

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
    def test_the_reader_answers_none_the_way_it_answers_a_corrupt_meta(self, tmp_path):
        """At the one open site: `None` — the answer this function already gives a bad meta."""
        fifo = tmp_path / "meta.json"
        os.mkfifo(fifo)
        assert within(DEADLINE_SECONDS, lambda: _read_meta_session_id(fifo)) is None

    def test_a_missing_meta_is_still_none(self, tmp_path):
        """The contract the kind check must not narrow: a missing path is `None`, not a raise."""
        assert _read_meta_session_id(tmp_path / "absent.json") is None


class TestTheIndexItselfIsOpenedByKind:
    """The index file, not a session's `meta.json`, is the other open in this module (#2099).

    `_read_meta_session_id` was kind-checked by #2083; `_load` — the reader every session save,
    rename, clear, delete and the daemon's startup rebuild go through — still leaned on
    `index_path.exists()`, and `exists()` is *true* for a FIFO. Measured 2026-10-11
    (`cyc20261011-123924`) on master `99477516`: a `mkfifo` at the index path made `_load`,
    `_upsert` and `rebuild_sessions_index` **not return in 8 s**, where the same fixture with a
    regular file returned in 0.00 s.

    The answer is the one this module already gives a corrupt index — `{}`, logged — because
    the alternative is worse than the hang: `_save_meta_with_title` calls through here with no
    `try` of its own and records the promise that the index must never break message
    persistence.

    The FIFO subjects carry an `os.mkfifo` gate; every control is FIFO-free, so the Windows leg
    still runs it (measured cost of the other shape: `#2096` needed its own follow-up, `a2d...`).
    """

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
    def test_a_fifo_index_answers_empty_instead_of_hanging(self, tmp_path):
        fifo = tmp_path / "sessions_index.json"
        os.mkfifo(fifo)
        assert within(DEADLINE_SECONDS, lambda: _load(fifo)) == {}

    def test_the_control_with_a_regular_index(self, tmp_path):
        """The kind is the axis: the same call on a regular file still reads it."""
        p = tmp_path / "sessions_index.json"
        p.write_text('{"s_1": "/tmp/s_1"}', encoding="utf-8")
        assert _load(p) == {"s_1": "/tmp/s_1"}

    def test_a_non_utf8_index_answers_empty(self, tmp_path):
        """The same promise, the other hole in it: bytes that are not UTF-8 are corrupt too.

        Without `UnicodeDecodeError` in the handler this raised out of `_load`, through
        `upsert_session_index`, into the session-save path that documents it as never raising.

        FIFO-free on purpose — this one runs on every platform.
        """
        p = tmp_path / "sessions_index.json"
        p.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
        assert _load(p) == {}

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
    def test_a_session_save_over_a_fifo_index_returns(self, tmp_path):
        """The caller's path, not just the reader's: `upsert` returns and leaves a real file.

        `_write` renames over the destination rather than opening it, so the FIFO is replaced
        instead of blocking the writer — which is why the guard belongs at the read and not
        also at the write.
        """
        idx = tmp_path / "sessions_index.json"
        os.mkfifo(idx)
        within(DEADLINE_SECONDS, lambda: upsert_session_index("s_1", tmp_path / "s_1"))
        assert not os.path.stat.S_ISFIFO(os.stat(idx).st_mode), "the writer did not replace it"
        assert _load(idx) == {"s_1": str(tmp_path / "s_1")}

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
    def test_the_startup_rebuild_over_a_fifo_index_returns(self, tmp_path):
        """`rebuild_sessions_index` reads the index first (daemon startup scan)."""
        os.mkfifo(tmp_path / "sessions_index.json")
        count = within(DEADLINE_SECONDS, lambda: rebuild_sessions_index(tmp_path))
        assert count == 0
