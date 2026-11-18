"""`delete_rant`: one row leaves the ledger, through the ledger's one writer.

Issue #2959 rebuilt the GUI into an agent cockpit. Its right-hand panel lists agents,
scheduled tasks and rants, and every one of those rows carries the same in-row delete
with the same confirmation. For agents and tasks the operation already existed; for
rants it did not, so the row's delete had nothing behind it — the row came back on the
next refresh, which reads as a button that silently failed.

Two things are deliberately *not* what this file tests:

* **Not a status transition.** The lifecycle (`pending → in_progress → completed`) has
  no "gone" state and nothing in it deletes a row; a delete is its own operation and
  the function says so in its docstring. So no test here asserts that a deleted row is
  reachable, restorable or counted anywhere — it is not.
* **Not a second writer.** `rants.jsonl` has exactly one writer (`_write_rants`), and
  this file holds `delete_rant` to being a *caller* of it rather than a rewrite of its
  own: format, ordering and the kind guard all belong to that function, and a delete
  that re-implemented them would drift from the ledger it edits.

The subjects, in order: the store's decision (a row removed / a timestamp that matched
nothing), the writer it goes through, and the daemon handler that turns the decision
into the frame the GUI acts on. A refusal is asserted as a *reading*, not as an error:
"nothing carried that timestamp" and "it is gone" are different claims and only one of
them means the row is no longer there.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from emrg.config import LlmConfig
from emrg.server.daemon import EmrgServer
from emrg.server.rants import _write_rants, delete_rant


def _rows() -> list[dict]:
    """Three rows in the six-field order `_write_rants` writes, with distinct times."""
    return [
        {
            "timestamp": "2026-11-18T09:00:00+08:00",
            "agent": "emrg",
            "status": "pending",
            "progress": None,
            "completed": None,
            "message": "oldest",
        },
        {
            "timestamp": "2026-11-18T10:00:00+08:00",
            "agent": "emrg",
            "status": "in_progress",
            "progress": "issue #2959",
            "completed": None,
            "message": "the one that goes",
        },
        {
            "timestamp": "2026-11-18T11:00:00+08:00",
            "agent": "emrg",
            "status": "completed",
            "progress": None,
            "completed": "2026-11-18T11:30:00+08:00",
            "message": "newest",
        },
    ]


def _ledger(tmp_path: Path) -> Path:
    log = tmp_path / "rants.jsonl"
    _write_rants(log, _rows())
    return log


def _read(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_delete_rant_removes_exactly_that_row_and_keeps_the_others_intact(tmp_path):
    """The named row leaves; the survivors keep every field they had."""
    log = _ledger(tmp_path)
    target = "2026-11-18T10:00:00+08:00"

    ok, notice = delete_rant(log, target)

    assert ok is True
    assert notice == f"deleted rant {target}"
    remaining = _read(log)
    assert [r["timestamp"] for r in remaining] == [
        "2026-11-18T09:00:00+08:00",
        "2026-11-18T11:00:00+08:00",
    ]
    # 不只是时间戳走了：留下来的行是它们原来那几行，字段一个不少
    assert remaining[0] == _rows()[0]
    assert remaining[1] == _rows()[2]
    # 账本保持单写者写出来的形状：每行一个 JSON 对象、末行换行
    raw = log.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert len(raw.splitlines()) == 2


def test_delete_rant_reports_a_timestamp_no_row_carries_and_leaves_the_file_alone(tmp_path):
    """A delete that matched nothing is a refusal, and it does not touch the ledger.

    This is the half that makes the GUI honest: `ok: false` becomes a line in the
    transcript instead of a row that disappears from the panel and reappears on the
    next refresh.
    """
    log = _ledger(tmp_path)
    before = log.read_bytes()

    ok, notice = delete_rant(log, "2026-11-18T23:59:59+08:00")

    assert ok is False
    assert notice == "rant not found: 2026-11-18T23:59:59+08:00"
    # 字节级不变：被拒绝的删除没有重写（重写会重排、重编码，也就改变了它没动的东西）
    assert log.read_bytes() == before


def test_delete_rant_goes_through_the_ledgers_only_writer(tmp_path, monkeypatch):
    """The rewrite is `_write_rants`'s, not one of its own.

    Arming the owner is the point: a delete that opened the file itself would still
    pass the two tests above on a well-formed ledger, and would keep doing so until the
    day the writer's format changed and only one of the two paths followed.
    """
    log = _ledger(tmp_path)
    calls: list[list[dict]] = []
    real_write = _write_rants

    def spy(path, rants):
        calls.append([dict(r) for r in rants])
        real_write(path, rants)

    monkeypatch.setattr("emrg.server.rants._write_rants", spy)

    ok, _ = delete_rant(log, "2026-11-18T09:00:00+08:00")

    assert ok is True
    assert len(calls) == 1, "delete_rant 自己写了文件，而不是走 _write_rants"
    assert [r["timestamp"] for r in calls[0]] == [
        "2026-11-18T10:00:00+08:00",
        "2026-11-18T11:00:00+08:00",
    ]


# ── The daemon handler: the decision turned into the frame the GUI reads ────


class _FakeWriter:
    """Minimal WebSocket stand-in capturing `_send` payloads (as `test_daemon.py` does)."""

    def __init__(self) -> None:
        self._frames: list[bytes] = []

    async def send(self, data) -> None:
        if isinstance(data, (bytes, bytearray)):
            self._frames.append(bytes(data))
        else:
            self._frames.append(data.encode() if isinstance(data, str) else data)

    async def close(self) -> None:
        pass

    def last(self) -> dict:
        raw = self._frames[-1]
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


def _make_server(tmp_path: Path) -> EmrgServer:
    """A server whose rant ledger is the test's, not the host's."""
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._agents_log = tmp_path / "agents.yml"
    server._rants_log = tmp_path / "rants.jsonl"
    return server


def test_daemon_delete_rant_frame_carries_the_store_verdict(tmp_path):
    """`ok` is the store's answer, and the frame says which timestamp it was about."""
    server = _make_server(tmp_path)
    _ledger(tmp_path)
    writer = _FakeWriter()
    target = "2026-11-18T10:00:00+08:00"

    asyncio.run(server._process_message({"type": "delete_rant", "timestamp": target}, writer))
    frame = writer.last()

    assert frame["type"] == "rant_deleted"
    assert frame["ok"] is True
    assert frame["timestamp"] == target
    assert frame["notice"] == f"deleted rant {target}"
    assert target not in [r["timestamp"] for r in _read(tmp_path / "rants.jsonl")]


def test_daemon_delete_rant_without_a_timestamp_is_refused_not_treated_as_all(tmp_path):
    """An empty timestamp refuses; it must never be read as "the row with no time"."""
    server = _make_server(tmp_path)
    log = _ledger(tmp_path)
    before = log.read_bytes()
    writer = _FakeWriter()

    asyncio.run(server._process_message({"type": "delete_rant", "timestamp": "   "}, writer))
    frame = writer.last()

    assert frame["type"] == "rant_deleted"
    assert frame["ok"] is False
    assert frame["notice"] == "missing timestamp"
    assert log.read_bytes() == before


def test_daemon_delete_rant_unknown_timestamp_reports_the_stores_notice(tmp_path):
    """A miss reaches the client with the store's own words, not a generic failure."""
    server = _make_server(tmp_path)
    _ledger(tmp_path)
    writer = _FakeWriter()

    asyncio.run(server._process_message(
        {"type": "delete_rant", "timestamp": "2026-11-18T23:59:59+08:00"}, writer))
    frame = writer.last()

    assert frame["ok"] is False
    assert frame["notice"] == "rant not found: 2026-11-18T23:59:59+08:00"
