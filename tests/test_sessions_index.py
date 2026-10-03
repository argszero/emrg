"""Tests for the global cross-project session index (rant 2026-08-13T16:42:22)."""

from __future__ import annotations

import json
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


class TestALivenessCheckThatCouldNotAnswer:
    """The prune acts only on a **measured** absence (cycle cyc20261003-110524).

    `rebuild_sessions_index` deletes rows and then rewrites the whole index, so
    the liveness reading decides whether a row is destroyed. It used to be
    `Path(sdir).exists()` inside `except (OSError, ValueError): alive = False`,
    which read "I could not look" as "it is not there". Measured on this host
    before the fix: with the whole `sessions` tree unreadable, the rebuild
    answered **0** and replaced the index with `{}` — every project's session
    rows gone, no exception raised, and the daemon's log line discarded (server
    logs go to DEVNULL). The directories were still on disk.

    The pairs below are the point: the *confirmed* absence must still prune
    (otherwise the fix would silence the feature), and the *unanswerable* one
    must not.
    """

    def _make_session(self, root: Path, sid: str) -> Path:
        return TestRebuild()._make_session(root, sid)

    def test_a_confirmed_gone_directory_is_pruned(self, tmp_path):
        """The control: the feature this guard could silence must stay alive."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        dead = tmp_path / "dead"
        dead.mkdir()
        (cfg / "sessions_index.json").write_text(
            json.dumps({"s_dead": str(dead)}), encoding="utf-8"
        )
        shutil.rmtree(dead)

        rebuild_sessions_index(cfg)

        assert "s_dead" not in _load(cfg / "sessions_index.json")

    def test_the_three_answers_are_not_two(self, tmp_path):
        """`liveness` distinguishes present / gone / could-not-look."""
        from emrg.sessions_index import GONE, PRESENT, UNMEASURABLE, liveness

        there = tmp_path / "there"
        there.mkdir()
        assert liveness(there) == PRESENT
        assert liveness(tmp_path / "never-existed") == GONE

        blocked = tmp_path / "blocked"
        blocked.mkdir()
        _chmod(blocked, 0o000)
        try:
            if not _is_unreadable(blocked / "child"):
                pytest.skip("this filesystem does not enforce the mode")
            assert liveness(blocked / "child") == UNMEASURABLE
        finally:
            _chmod(blocked, 0o755)

    def test_an_unreadable_entry_is_kept_and_the_index_survives(self, tmp_path):
        """The measured disaster, in the smallest reproduction of it.

        A directory whose parent refuses traversal is neither present nor gone
        from here — and the whole index must survive it.
        """
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        sessions = cfg / ".emrg" / "sessions"
        sessions.mkdir(parents=True)
        buried = sessions / "s_buried"
        buried.mkdir()
        (buried / "meta.json").write_text(
            json.dumps({"session_id": "s_buried"}), encoding="utf-8"
        )
        # An entry pointing outside the scanned root, so the scan half of the
        # rebuild cannot re-add it: only the prune loop can decide its fate.
        outside = tmp_path / "outside"
        outside.mkdir()
        (cfg / "sessions_index.json").write_text(
            json.dumps({"s_buried": str(buried), "s_outside": str(outside)}),
            encoding="utf-8",
        )

        _chmod(sessions, 0o000)
        try:
            if not _is_unreadable(sessions / "s_buried"):
                pytest.skip("this filesystem does not enforce the mode")
            count = rebuild_sessions_index(cfg)
        finally:
            _chmod(sessions, 0o755)

        data = _load(cfg / "sessions_index.json")
        assert data, "the index was emptied by a reading that could not be made"
        assert data.get("s_outside") == str(outside)
        assert count >= 2, f"the rebuild counted {count} with both rows still indexed"

    def test_the_kept_entries_are_named_in_a_warning(self, tmp_path, caplog):
        """The state has a producer: a reader of the log can see the partial run."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        _chmod(blocked, 0o000)
        (cfg / "sessions_index.json").write_text(
            json.dumps({"s_kept": str(blocked / "child")}), encoding="utf-8"
        )
        try:
            if not _is_unreadable(blocked / "child"):
                pytest.skip("this filesystem does not enforce the mode")
            with caplog.at_level("WARNING", logger="emrg.sessions_index"):
                rebuild_sessions_index(cfg)
        finally:
            _chmod(blocked, 0o755)

        text = caplog.text
        assert "could not be checked" in text, text
        assert "s_kept" in text, "the warning must name the rows it kept"


    def test_one_unreadable_session_dir_is_reported_and_the_scan_continues(
        self, tmp_path, caplog
    ):
        """`sessions` is listable; one *entry* inside it is not.

        Run through a **registered project path**, not the recursive walk: on the
        walk, `os.walk`'s own `onerror` already names a directory it cannot
        descend into, so the same mutation was invisible there (two producers of
        one report — the second one is what makes the first unfalsifiable). The
        other entries must still be read, and the unreadable one must be named.
        """
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        project = tmp_path / "proj"
        readable = project / ".emrg" / "sessions" / "s_readable"
        readable.mkdir(parents=True)
        (readable / "meta.json").write_text(
            json.dumps({"session_id": "s_readable"}), encoding="utf-8"
        )
        blocked = project / ".emrg" / "sessions" / "s_blocked"
        blocked.mkdir()
        _chmod(blocked, 0o000)

        try:
            if not _is_unreadable(blocked / "meta.json"):
                pytest.skip("this filesystem does not enforce the mode")
            with caplog.at_level("WARNING", logger="emrg.sessions_index"):
                count = rebuild_sessions_index(cfg, project_paths=[str(project)])
        finally:
            _chmod(blocked, 0o755)

        assert count >= 1, "an unreadable entry aborted the scan"
        assert "s_readable" in _load(cfg / "sessions_index.json")
        assert str(blocked) in caplog.text, caplog.text

    def test_a_directory_the_scan_could_not_read_is_reported(self, tmp_path, caplog):
        """`os.walk` ignores a failed listing by default, so a subtree that could
        not be read and an empty subtree arrived as the same result."""
        cfg = tmp_path / "cfg"
        self._make_session(cfg, "s_scanned")
        blocked = cfg / "blocked"
        blocked.mkdir()
        _chmod(blocked, 0o000)

        try:
            # Probe a *child*: mode 000 on a directory leaves `stat` of the
            # directory itself working (that needs search on its parent), so
            # asking about `blocked` would answer PRESENT and skip every run.
            if not _is_unreadable(blocked / "child"):
                pytest.skip("this filesystem does not enforce the mode")
            with caplog.at_level("WARNING", logger="emrg.sessions_index"):
                rebuild_sessions_index(cfg)
        finally:
            _chmod(blocked, 0o755)

        assert "could not be read" in caplog.text, caplog.text
        assert "lower bound" in caplog.text, caplog.text

    def test_a_registered_project_that_cannot_be_listed_is_reported(self, tmp_path, caplog):
        """It used to raise into a blanket `except OSError: continue`: one
        unreadable project contributed nothing and the rebuild said so nowhere."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        project = tmp_path / "proj"
        sessions = project / ".emrg" / "sessions"
        sessions.mkdir(parents=True)
        _chmod(sessions, 0o000)

        try:
            if not _is_unreadable(sessions / "x"):
                pytest.skip("this filesystem does not enforce the mode")
            with caplog.at_level("WARNING", logger="emrg.sessions_index"):
                rebuild_sessions_index(cfg, project_paths=[str(project)])
        finally:
            _chmod(sessions, 0o755)

        assert "could not be read" in caplog.text, caplog.text
        # The exact directory, not merely "a project": naming the caller's
        # placeholder would satisfy a vaguer assertion while the guard that reads
        # the directory was gone.
        assert str(sessions) in caplog.text, caplog.text


def _chmod(path: Path, mode: int) -> None:
    if os.name == "nt":  # Windows has no POSIX mode bits; the tests skip instead.
        return
    os.chmod(path, mode)


def _is_unreadable(path: Path) -> bool:
    """Whether this filesystem really refuses to answer about `path`.

    Asserted rather than assumed (as root, mode 000 does not block), and measured
    with `os.stat` **directly** rather than through `liveness`: a precondition
    probe that calls the function under test turns every mutation of that
    function into a `skip`, and a skipped test is not a failing one. Measured
    2026-10-03 (`cyc20261003-110524`): two arms survived on exactly that.
    """
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return False
