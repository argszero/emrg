"""Tests for the global cross-project session index (rant 2026-08-13T16:42:22)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from emrg.session import Session
from emrg.sessions_index import (
    _load,
    _read_meta_session_id,
    _write,
    read_meta_object,
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


# ── the meta.json reader family ───────────────────────────────────────────
#
# One rule, five readers: a meta.json that is not a mapping must answer exactly
# as a missing one does. Four on-disk states yield no mapping — missing,
# undecodable/unreadable, unparseable, and parses-to-a-non-object — and the
# readers each guarded a different subset of them before this: three crashed
# with `AttributeError: 'list' object has no attribute 'get'` on the last
# state (while `_read_meta_session_id`'s own docstring promised "None if
# missing/corrupt"), and `Session.load` had no guard at all, so a truncated
# meta.json raised JSONDecodeError out of `resume_session` — a state
# `_save_meta` really can leave behind, because it writes in place.

_NON_MAPPINGS = {
    "truncated": '{"session_id": "s_1"',
    "empty": "",
    "list": "[]",
    "null": "null",
    "string": '"s_1"',
    "int": "5",
    "undecodable": b"\xff\xfe\x00\x01",
}


def _meta_path(root: Path, sid: str = "s_1") -> Path:
    d = root / ".emrg" / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    return d / "meta.json"


def _family_answers(root: Path, meta: Path) -> dict:
    """What every reader of this meta.json answers — the family, side by side."""
    return {
        "sessions_index._read_meta_session_id": _read_meta_session_id(meta),
        # `_created_at` rides along: `Session.load` may skip the file entirely
        # rather than apply field defaults, and only a second field shows it.
        "Session.load": (Session.load("s_1", root).message_count,
                         Session.load("s_1", root)._created_at),
        "Session.title": Session("s_1", root).title,
        "Session.list_sessions": [
            (m.get("session_id"), m.get("message_count"))
            for m in Session.list_sessions(root)
        ],
    }


class TestMetaReaderFamily:
    def test_read_meta_object_returns_the_mapping_it_read(self, tmp_path):
        meta = _meta_path(tmp_path)
        meta.write_text('{"session_id": "s_1", "message_count": 3}', encoding="utf-8")
        assert read_meta_object(meta) == {"session_id": "s_1", "message_count": 3}

    def test_read_meta_object_refuses_every_non_mapping(self, tmp_path):
        meta = _meta_path(tmp_path)
        assert read_meta_object(meta) is None, "missing file is not a mapping"
        for label, payload in _NON_MAPPINGS.items():
            meta.write_bytes(payload if isinstance(payload, bytes) else payload.encode("utf-8"))
            assert read_meta_object(meta) is None, label

    def test_an_empty_object_is_still_a_mapping(self, tmp_path):
        """`{}` is a mapping with nothing in it — a different answer from None."""
        meta = _meta_path(tmp_path)
        meta.write_text("{}", encoding="utf-8")
        assert read_meta_object(meta) == {}

    def test_every_reader_answers_a_non_mapping_as_it_answers_a_missing_file(self, tmp_path):
        meta = _meta_path(tmp_path)
        missing = _family_answers(tmp_path, meta)
        for label, payload in _NON_MAPPINGS.items():
            meta.write_bytes(payload if isinstance(payload, bytes) else payload.encode("utf-8"))
            assert _family_answers(tmp_path, meta) == missing, (
                f"{label}: a meta.json that is not a mapping must answer as a "
                f"missing one does, reader by reader"
            )

    def test_list_sessions_skips_a_non_mapping_entry_and_keeps_the_others(self, tmp_path):
        """Skipping is what a corrupt entry already got; crashing was the bug."""
        good = Session.create_with_id("s_good", tmp_path)
        good.rename("Kept")
        bad = _meta_path(tmp_path, "s_bad")
        bad.write_text("[]", encoding="utf-8")
        listed = Session.list_sessions(tmp_path)
        assert [m["session_id"] for m in listed] == ["s_good"], listed
        assert listed[0]["title"] == "Kept"

    def test_writing_a_meta_over_a_non_mapping_keeps_the_title(self, tmp_path):
        """The writer reads the old meta to preserve the title it did not get.

        `"title" in 5` is a TypeError, not a False — so this path crashed for a
        numeric meta.json while silently surviving a list one.
        """
        session = Session.create_with_id("s_writer", tmp_path)
        session.rename("Kept Title")
        session._meta_path.write_text("5", encoding="utf-8")
        session.append_message({"type": "message", "role": "user", "content": "hi"})
        assert session.title == "s_writer", "a non-mapping carries no title to keep"
        assert json.loads(session._meta_path.read_text(encoding="utf-8"))["message_count"] == 1

    def test_a_preserved_title_survives_a_rewrite(self, tmp_path):
        """The same path, on a healthy meta: the title it reads is written back."""
        session = Session.create_with_id("s_kept", tmp_path)
        session.rename("Kept Title")
        session.append_message({"type": "message", "role": "user", "content": "hi"})
        assert json.loads(session._meta_path.read_text(encoding="utf-8"))["title"] == "Kept Title"
