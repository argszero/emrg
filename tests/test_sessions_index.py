"""Tests for the global cross-project session index (rant 2026-08-13T16:42:22)."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

import pytest

from emrg.session import Session
from emrg.sessions_index import (
    _load,
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


# ── the write keeps the promise its caller relies on ──────────────────

_posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the arm denies a directory by mode, which Windows does not enforce — without "
        "POSIX permissions the run measures nothing rather than the guard."
    ),
)


class TestTheWriteKeepsItsPromise:
    """`_write` says "never raises", and that claim is why its caller ignores it.

    `Session._save_meta_with_title` runs on every meta save (create, append, compact,
    rename, clear, delete) and its comment says the index write "must not break
    session creation or message persistence". Measured 2026-10-04: the two statements
    before the guard — `mkdir` and `mkstemp` — raised out of this function, so a
    host whose index directory cannot be written could not save a session at all.
    """

    def test_a_parent_that_is_a_file_does_not_raise(self, tmp_path: Path, caplog):
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="emrg.sessions_index"):
            _write({"a": "b"}, blocker / "sessions_index.json")  # must not raise
        assert blocker.read_text(encoding="utf-8") == "x", "the blocker is untouched"
        assert "failed to write sessions index" in caplog.text, "and it is reported"

    @_posix_only
    def test_a_directory_that_refuses_new_files_does_not_raise(self, tmp_path: Path, caplog):
        target_dir = tmp_path / "ro"
        target_dir.mkdir()
        os.chmod(target_dir, 0o500)
        try:
            with caplog.at_level(logging.WARNING, logger="emrg.sessions_index"):
                _write({"a": "b"}, target_dir / "sessions_index.json")  # must not raise
        finally:
            os.chmod(target_dir, 0o700)
        assert "failed to write sessions index" in caplog.text

    def test_a_target_that_can_be_written_is_still_written(self, tmp_path: Path):
        """Control leg: the guard must not turn a success into a silent nothing."""
        target = tmp_path / "nested" / "sessions_index.json"

        _write({"a": "b"}, target)

        assert json.loads(target.read_text(encoding="utf-8")) == {"a": "b"}
        leftovers = [p.name for p in target.parent.iterdir() if p.name != target.name]
        assert leftovers == [], f"no temp file may survive a successful write; got {leftovers!r}"

    def test_a_failed_write_leaves_no_temp_file_behind(self, tmp_path: Path):
        """The cleanup has to survive the guard moving: a temp file is only unlinked
        when one was really made."""
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")

        _write({"a": "b"}, blocker / "sessions_index.json")

        assert not list(tmp_path.glob(".sessions_index_*")), (
            "nothing may be left in the directory the write failed in"
        )
