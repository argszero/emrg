"""Tests for the submit_rant tool + shared append_rant (rant 2026-08-17T11:51:59).

Rants are not a special mode: the agent detects rant intent in normal
conversation, confirms with the user, then calls submit_rant. The daemon's
``rant`` command and the tool share emrg.server.rants.append_rant, so the
file format / sort / daemon-authoritative timestamp stay identical.
"""

import json

import pytest

from emrg.server.rants import append_rant
from emrg.tools.submit_rant_tool import SubmitRantTool


def test_append_rant_writes_sorted_entry(tmp_path):
    """New rant appended, sorted by timestamp, field order message last."""
    # pre-existing rant (older timestamp) must stay before the new one
    rants_file = tmp_path / "rants.jsonl"
    rants_file.write_text(
        json.dumps({
            "timestamp": "2026-08-17T08:00:00+08:00",
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
    import datetime as _dt
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


def test_submit_rant_definition_exposes_consent_contract():
    """The tool description must require explicit user consent before calling."""
    tool = SubmitRantTool()
    d = tool.definition()
    assert d.name == "submit_rant"
    assert "consent" in d.description.lower() or "confirm" in d.description.lower()
    assert "message" in d.parameters["required"]
