"""Tests for the submit_rant tool + shared append_rant (rant 2026-08-17T11:51:59).

Rants are not a special mode: the agent detects rant intent in normal
conversation, confirms with the user, then calls submit_rant. The daemon's
``rant`` command and the tool share emrg.server.rants.append_rant, so the
file format / sort / daemon-authoritative timestamp stay identical.

Rant 2026-08-18T16:42:52: the tool also exposes list/update/cleanup actions
(and rants.py tolerantly parses legacy array rows) so the evolution loop
curates rants.jsonl through the tool instead of hand-written rewrites.
"""

import json

import pytest

from emrg.server.rants import (
    append_rant,
    cleanup_rants,
    list_rants,
    update_rant,
)
from emrg.tools.submit_rant_tool import SubmitRantTool


def test_append_rant_writes_sorted_entry(tmp_path):
    """New rant appended, sorted by timestamp, field order message last."""
    import datetime as _dt
    # pre-existing rant (older timestamp) must stay before the new one —
    # use the same tz-aware local offset as the daemon so the lexicographic
    # string sort matches chronological order (all rants share the host
    # timezone in production; mixing offsets would mis-sort on UTC hosts)
    older_ts = (_dt.datetime.now().astimezone() - _dt.timedelta(hours=1)).isoformat()
    rants_file = tmp_path / "rants.jsonl"
    rants_file.write_text(
        json.dumps({
            "timestamp": older_ts,
            "project": "emrg",
            "status": "completed",
            "progress": None,
            "completed": "2026-08-17T08:30:00+08:00",
            "message": "older rant",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    count = append_rant(rants_file, "new feedback", project="argszero/aitokenpool")
    assert count == 2

    lines = rants_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    entries = [json.loads(l) for l in lines]
    # sorted by timestamp: older first, new last
    assert entries[0]["message"] == "older rant"
    assert entries[1]["message"] == "new feedback"
    # field order: timestamp → project → status → progress → completed → message
    assert list(entries[1].keys()) == [
        "timestamp", "project", "status", "progress", "completed", "message",
    ]
    assert entries[1]["project"] == "argszero/aitokenpool"
    assert entries[1]["status"] == "pending"
    assert entries[1]["completed"] is None
    # daemon-authoritative tz-aware timestamp
    ts = _dt.datetime.fromisoformat(entries[1]["timestamp"])
    assert ts.tzinfo is not None
    assert abs((_dt.datetime.now(ts.tzinfo) - ts).total_seconds()) < 60


def test_append_rant_missing_file_and_corrupt_lines(tmp_path):
    """Missing file → created; corrupt lines skipped without crashing."""
    rants_file = tmp_path / "rants.jsonl"
    rants_file.write_text("not-json\n", encoding="utf-8")
    count = append_rant(rants_file, "hello")
    assert count == 1
    entries = [json.loads(l) for l in
               rants_file.read_text(encoding="utf-8").strip().splitlines()]
    assert entries[0]["message"] == "hello"
    assert entries[0]["project"] == ""


def test_submit_rant_tool_writes_and_reports_count(tmp_path, monkeypatch):
    """Tool execute writes via append_rant and returns the new count."""
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    tool = SubmitRantTool()

    result = __import__("asyncio").run(tool.execute(
        {"message": "this feature is broken", "project": "argszero/aitokenpool"}
    ))
    assert result.error is False
    assert "Total rants: 1" in result.content
    lines = (tmp_path / "rants.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "this feature is broken"


def test_submit_rant_tool_requires_message():
    """Empty message → error result, nothing written."""
    tool = SubmitRantTool()
    result = __import__("asyncio").run(tool.execute({"message": "   "}))
    assert result.error is True
    assert "requires a message" in result.content


def test_submit_rant_tool_requires_project():
    """Missing project → error telling the agent to ask the user (rant 12:03:13)."""
    tool = SubmitRantTool()
    result = __import__("asyncio").run(tool.execute({"message": "some complaint"}))
    assert result.error is True
    assert "project is required" in result.content
    assert "ask the user" in result.content


def test_submit_rant_definition_exposes_consent_contract():
    """The tool description must require explicit user consent before calling."""
    tool = SubmitRantTool()
    d = tool.definition()
    assert d.name == "submit_rant"
    assert "consent" in d.description.lower() or "confirm" in d.description.lower()
    # action is the only statically-required param; submit's project/message
    # are enforced in execute() (see test_submit_rant_tool_requires_*)
    assert "action" in d.parameters["required"]
    assert "message" in d.parameters["properties"]  # documented
    assert "project" in d.parameters["properties"]  # documented since rant 12:03:13
    # rant 2026-08-19T10:35:24: static purpose removed → per-call intent required
    assert not hasattr(d, "purpose")
    assert "intent" in d.parameters["required"]


def test_all_tools_require_intent():
    """Rant 2026-08-19T10:35:24: every registered tool requires the per-call
    `intent` parameter (agent writes why it is calling); static purpose gone."""
    from emrg.tools.bash_tool import BashTool
    from emrg.tools.read_tool import ReadTool
    from emrg.tools.write_tool import WriteTool
    from emrg.tools.edit_tool import EditTool
    from emrg.tools.glob_tool import GlobTool
    from emrg.tools.grep_tool import GrepTool

    for tool in (BashTool(), ReadTool(), WriteTool(), EditTool(),
                 GlobTool(), GrepTool(), SubmitRantTool()):
        d = tool.definition()
        assert d.name, "tool name missing"
        assert "intent" in d.parameters["properties"], f"{d.name} missing intent property"
        assert "intent" in d.parameters["required"], f"{d.name} intent not required"
        assert not hasattr(d, "purpose"), f"{d.name} still has static purpose"


# --- rant 2026-08-18T16:42:52: unified rant tool (list/update/cleanup) --------


def _write_rant_lines(tmp_path, entries: list[dict]):
    """Write dict entries in canonical order + sorted by timestamp."""
    entries = sorted(entries, key=lambda r: r["timestamp"])
    lines = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
    f = tmp_path / "rants.jsonl"
    f.write_text(lines, encoding="utf-8")
    return f


def test_read_tolerates_legacy_array_rows(tmp_path):
    """Legacy array rows ([ts, project, status, progress, completed, message])
    are converted back to dicts; corrupt rows skipped (rant 16:42:52)."""
    # Relative timestamps — CI runs on UTC, so hardcoded +08:00 strings would
    # mis-sort against the now()-generated append timestamp (existing lesson
    # in test_append_rant_writes_sorted_entry: mixing offsets mis-sorts).
    import datetime as _dt
    now = _dt.datetime.now().astimezone()
    older = (now - _dt.timedelta(hours=2)).isoformat()
    newer = (now - _dt.timedelta(hours=1)).isoformat()
    f = tmp_path / "rants.jsonl"
    f.write_text(
        f'["{older}", "emrg", "pending", null, null, "array rant"]\n'
        "not-json\n"
        f'{{"timestamp": "{newer}", "project": "emrg", '
        '"status": "completed", "progress": null, '
        f'"completed": "{newer}", "message": "dict rant", '
        '"unknown": "ignored"}\n',
        encoding="utf-8",
    )
    # append_rant must not crash on the mixed file and must normalize rows
    count = append_rant(f, "new rant", project="emrg")
    assert count == 3
    entries = [json.loads(l) for l in
               f.read_text(encoding="utf-8").strip().splitlines()]
    assert entries[0]["message"] == "array rant"
    assert entries[0]["project"] == "emrg"
    # dict rows keep the 6 canonical fields, unknown fields dropped
    assert list(entries[1].keys()) == [
        "timestamp", "project", "status", "progress", "completed", "message",
    ]
    assert entries[1]["message"] == "dict rant"


def test_list_rants_filters_by_status_and_project(tmp_path):
    f = _write_rant_lines(tmp_path, [
        {"timestamp": "2026-08-18T10:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": "emrg pending"},
        {"timestamp": "2026-08-18T11:00:00+08:00", "project": "emrg",
         "status": "completed", "progress": None,
         "completed": "2026-08-18T10:30:00+08:00", "message": "emrg done"},
        {"timestamp": "2026-08-18T12:00:00+08:00", "project": "aitokenpool",
         "status": "pending", "progress": None, "completed": None,
         "message": "atp pending"},
    ])
    assert len(list_rants(f)) == 3
    assert len(list_rants(f, status="pending")) == 2
    assert len(list_rants(f, status="completed")) == 1
    assert len(list_rants(f, project="emrg")) == 2
    assert len(list_rants(f, status="pending", project="emrg")) == 1
    assert len(list_rants(f, status="completed", project="aitokenpool")) == 0


def test_update_rant_state_machine(tmp_path):
    f = _write_rant_lines(tmp_path, [
        {"timestamp": "2026-08-18T10:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": "test rant"},
    ])
    # pending → in_progress valid
    ok, msg = update_rant(f, "2026-08-18T10:00:00+08:00", status="in_progress",
                          progress="PR #1 submitted")
    assert ok
    assert "in_progress" in msg
    # in_progress → completed valid, timestamp auto-written
    ok, msg = update_rant(f, "2026-08-18T10:00:00+08:00", status="completed")
    assert ok
    entries = [json.loads(l) for l in
               f.read_text(encoding="utf-8").strip().splitlines()]
    assert entries[0]["status"] == "completed"
    assert entries[0]["completed"]  # auto-written ISO timestamp
    assert entries[0]["progress"] == "PR #1 submitted"  # retained
    # completed → pending is a skip-back: invalid
    ok, msg = update_rant(f, "2026-08-18T10:00:00+08:00", status="pending")
    assert not ok
    assert "invalid transition" in msg
    # pending → completed directly: invalid (no skipping)
    f2 = _write_rant_lines(tmp_path, [
        {"timestamp": "2026-08-18T10:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": "test rant 2"},
    ])
    ok, msg = update_rant(f2, "2026-08-18T10:00:00+08:00", status="completed")
    assert not ok
    assert "no skipping" in msg


def test_update_rant_unknown_timestamp_and_bad_status(tmp_path):
    f = _write_rant_lines(tmp_path, [
        {"timestamp": "2026-08-18T10:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": "test rant"},
    ])
    ok, msg = update_rant(f, "2026-08-18T99:00:00+08:00", status="in_progress")
    assert not ok
    assert "not found" in msg
    ok, msg = update_rant(f, "2026-08-18T10:00:00+08:00", status="bogus")
    assert not ok
    assert "invalid status" in msg


def test_cleanup_rants_keeps_pending_plus_10_completed(tmp_path):
    entries = []
    # 3 active rants (2 pending + 1 in_progress)
    for i, (ts, status) in enumerate([
        ("2026-08-18T01:00:00+08:00", "pending"),
        ("2026-08-18T02:00:00+08:00", "in_progress"),
        ("2026-08-18T03:00:00+08:00", "pending"),
    ]):
        entries.append({"timestamp": ts, "project": "emrg", "status": status,
                        "progress": None, "completed": None,
                        "message": f"active {i}"})
    # 12 completed rants
    for i in range(12):
        entries.append({
            "timestamp": f"2026-08-18T{i + 4:02d}:00:00+08:00",
            "project": "emrg", "status": "completed", "progress": None,
            "completed": f"2026-08-18T{i + 4:02d}:30:00+08:00",
            "message": f"done {i}",
        })
    f = _write_rant_lines(tmp_path, entries)
    count = cleanup_rants(f)
    assert count == 13  # 3 active + 10 most recent completed
    kept = [json.loads(l) for l in
            f.read_text(encoding="utf-8").strip().splitlines()]
    assert len(kept) == 13
    done = [e for e in kept if e["status"] == "completed"]
    assert len(done) == 10
    # oldest completed (completed 04:30) pruned, newest (15:30) kept
    completed_ts = [e["completed"] for e in done]
    assert "2026-08-18T04:30:00+08:00" not in completed_ts
    assert "2026-08-18T15:30:00+08:00" in completed_ts
    # file still sorted ascending by timestamp
    timestamps = [e["timestamp"] for e in kept]
    assert timestamps == sorted(timestamps)


def test_tool_list_action(tmp_path, monkeypatch):
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    tool = SubmitRantTool()
    f = tmp_path / "rants.jsonl"
    _write_rant_lines(tmp_path, [
        {"timestamp": "2026-08-18T10:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": "some pending rant"},
    ])
    result = __import__("asyncio").run(tool.execute({"action": "list"}))
    assert result.error is False
    assert "2026-08-18T10:00:00+08:00" in result.content
    assert "some pending rant" in result.content
    result = __import__("asyncio").run(
        tool.execute({"action": "list", "status": "completed"}))
    assert result.error is False
    assert "No rants match" in result.content


def test_tool_update_action(tmp_path, monkeypatch):
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    tool = SubmitRantTool()
    _write_rant_lines(tmp_path, [
        {"timestamp": "2026-08-18T10:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": "test rant"},
    ])
    result = __import__("asyncio").run(tool.execute({
        "action": "update",
        "timestamp": "2026-08-18T10:00:00+08:00",
        "status": "in_progress",
        "progress": "PR #1 submitted",
    }))
    assert result.error is False
    entry = json.loads((tmp_path / "rants.jsonl")
                       .read_text(encoding="utf-8").strip().splitlines()[0])
    assert entry["status"] == "in_progress"
    assert entry["progress"] == "PR #1 submitted"
    # missing timestamp → error
    result = __import__("asyncio").run(
        tool.execute({"action": "update", "status": "in_progress"}))
    assert result.error is True
    assert "requires timestamp" in result.content
    # in_progress → completed valid: tool auto-writes the completed timestamp
    result = __import__("asyncio").run(tool.execute({
        "action": "update", "timestamp": "2026-08-18T10:00:00+08:00",
        "status": "completed"}))
    assert result.error is False
    entry = json.loads((tmp_path / "rants.jsonl")
                       .read_text(encoding="utf-8").strip().splitlines()[0])
    assert entry["status"] == "completed"
    assert entry["completed"]
    # pending → completed (no skipping) → error, no crash
    _write_rant_lines(tmp_path, [
        {"timestamp": "2026-08-18T09:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": "fresh pending rant"},
    ])
    result = __import__("asyncio").run(tool.execute({
        "action": "update", "timestamp": "2026-08-18T09:00:00+08:00",
        "status": "completed"}))
    assert result.error is True
    assert "no skipping" in result.content


def test_tool_cleanup_action(tmp_path, monkeypatch):
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    tool = SubmitRantTool()
    _write_rant_lines(tmp_path, [
        {"timestamp": "2026-08-18T10:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": "active rant"},
    ])
    result = __import__("asyncio").run(tool.execute({"action": "cleanup"}))
    assert result.error is False
    assert "1 entries kept" in result.content


def test_tool_unknown_action():
    tool = SubmitRantTool()
    result = __import__("asyncio").run(tool.execute({"action": "explode"}))
    assert result.error is True
    assert "unknown action" in result.content


def test_tool_definition_exposes_actions():
    """Definition documents the 4 actions and keeps submit required params."""
    tool = SubmitRantTool()
    d = tool.definition()
    assert d.name == "submit_rant"
    assert "action" in d.parameters["required"]
    assert set(d.parameters["properties"]["action"]["enum"]) == {
        "submit", "list", "update", "cleanup",
    }
    # submit contract (project/message) still documented
    assert "message" in d.parameters["properties"]
    assert "project" in d.parameters["properties"]
    assert "consent" in d.description.lower() or "confirm" in d.description.lower()

