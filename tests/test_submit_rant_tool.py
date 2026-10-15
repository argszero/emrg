"""Tests for the submit_rant tool + shared append_rant (rant 2026-08-17T11:51:59).

Rants are not a special mode: the agent detects rant intent in normal
conversation, confirms with the user, then calls submit_rant. The daemon's
``rant`` command and the tool share emrg.server.rants.append_rant, so the
file format / sort / daemon-authoritative timestamp stay identical.

Rant 2026-08-18T16:42:52: the tool also exposes list/update/cleanup actions
(and rants.py tolerantly parses legacy array rows) so the evolution loop
curates rants.jsonl through the tool instead of hand-written rewrites.
"""

import asyncio
import json
import os
import re

import pytest

from emrg.server.rants import (
    _read_rants,
    _write_rants,
    append_rant,
    cleanup_rants,
    list_rants,
    update_rant,
)
from emrg.tools.submit_rant_tool import SubmitRantTool
from tests.bounded_read import DEADLINE_SECONDS, within


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


def test_submit_rant_warns_on_unregistered_project(tmp_path, monkeypatch):
    """Rant 2026-08-24T10:54:04: project must be a projects.yml short name —
    an owner/repo-style name is recorded but flagged with registered
    candidates so the mistake is visible at submit time."""
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    (tmp_path / "projects.yml").write_text(
        "- name: emrg\n  path: /p/emrg\n"
        "- name: aitokenpool\n  path: /p/aitokenpool\n",
        encoding="utf-8",
    )
    tool = SubmitRantTool()
    result = __import__("asyncio").run(tool.execute(
        {"message": "something is broken", "project": "argszero/aitokenpool"}
    ))
    assert result.error is False
    assert "Total rants: 1" in result.content
    # warning names the problem + lists the registered candidates
    assert "not a registered name" in result.content
    assert "aitokenpool" in result.content
    assert "emrg" in result.content
    # the rant still lands (non-blocking warning)
    lines = (tmp_path / "rants.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["project"] == "argszero/aitokenpool"


def test_submit_rant_clean_on_registered_project(tmp_path, monkeypatch):
    """A registered short name submits without the warning."""
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    (tmp_path / "projects.yml").write_text(
        "- name: emrg\n  path: /p/emrg\n",
        encoding="utf-8",
    )
    tool = SubmitRantTool()
    result = __import__("asyncio").run(tool.execute(
        {"message": "still broken", "project": "emrg"}
    ))
    assert result.error is False
    assert "Total rants: 1" in result.content
    assert "not a registered name" not in result.content


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
    from emrg.tools.bash_tool_v2 import BashToolV2
    from emrg.tools.read_tool import ReadTool
    from emrg.tools.write_tool import WriteTool
    from emrg.tools.edit_tool import EditTool
    from emrg.tools.glob_tool import GlobTool
    from emrg.tools.grep_tool import GrepTool

    for tool in (BashToolV2(), ReadTool(), WriteTool(), EditTool(),
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


def test_update_rant_refuses_a_completion_before_the_submission(tmp_path):
    """A completion stamp cannot precede the instant the rant was submitted.

    The store ranks a completed rant by `completed or timestamp`, and
    `scripts/check-issue-links.py` reads that same key to decide whether pruning can explain a
    record a citation names. Its loudest sentence — *the retention rule cannot explain the
    absence ... this ledger never held it* — assumes a stamp never precedes its own submission
    instant, so a row that breaks the assumption makes the reading report a record the store
    really did prune as one that never existed. Both directions are measured here, because the
    rule is an ordering and an ordering has two: earlier is refused **and nothing is written**,
    later and equal are accepted.
    """
    f = _write_rant_lines(tmp_path, [
        {"timestamp": "2026-08-18T10:00:00+08:00", "project": "emrg",
         "status": "in_progress", "progress": None, "completed": None,
         "message": "test rant"},
    ])
    before = f.read_text(encoding="utf-8")

    ok, msg = update_rant(f, "2026-08-18T10:00:00+08:00", status="completed",
                          completed="2025-01-01T00:00:00+00:00")
    assert not ok, msg
    assert "precedes the rant's submission instant" in msg, msg
    assert f.read_text(encoding="utf-8") == before, "a refused update must write nothing"

    # … and the other direction: at or after the submission instant is accepted. Equal counts
    # ("completed or timestamp" is a key, so a stamp equal to the submission is in order).
    ok, msg = update_rant(f, "2026-08-18T10:00:00+08:00", status="completed",
                          completed="2026-08-18T10:00:00+08:00")
    assert ok, msg
    entry = json.loads(f.read_text(encoding="utf-8").strip())
    assert entry["completed"] == "2026-08-18T10:00:00+08:00"

    # … and a later stamp replaces it.
    ok, msg = update_rant(f, "2026-08-18T10:00:00+08:00",
                          completed="2026-08-19T09:00:00+08:00")
    assert ok, msg
    entry = json.loads(f.read_text(encoding="utf-8").strip())
    assert entry["completed"] == "2026-08-19T09:00:00+08:00"


def test_update_rant_refuses_a_completion_it_cannot_order(tmp_path):
    """A stamp the store cannot *show* to follow the submission is refused, not assumed fine.

    Three routes, each named in the message: the stamp is not an instant at all; the row's own
    timestamp is not one; and the two disagree about carrying a UTC offset, where no order
    exists (a naive `datetime` and an aware one cannot be compared). The third is the one that
    hides: the reader can also be handed a citation without an offset, and then its comparison
    is naive against naive — an unorderable pair whose premise nothing has checked.
    """
    stamp = "2026-08-18T10:00:00+08:00"
    row = {"timestamp": stamp, "project": "emrg", "status": "in_progress",
           "progress": None, "completed": None, "message": "test rant"}

    f = _write_rant_lines(tmp_path, [dict(row)])
    ok, msg = update_rant(f, stamp, status="completed", completed="last Tuesday")
    assert not ok and "is not an ISO instant" in msg, msg

    ok, msg = update_rant(f, stamp, status="completed", completed="2026-08-19T10:00:00")
    assert not ok and "cannot be ordered" in msg, msg
    assert "UTC offset" in msg, msg

    f2 = _write_rant_lines(tmp_path, [{**row, "timestamp": "not an instant"}])
    ok, msg = update_rant(f2, "not an instant", status="completed",
                          completed="2026-08-19T10:00:00+08:00")
    assert not ok and "submission instant" in msg, msg


def test_the_auto_written_stamp_is_checked_too(tmp_path):
    """The transition's own stamp goes through the same rule, not only an explicit one.

    A row dated in the future cannot be completed: the stamp the transition would write is
    *now*, which precedes it. Pinning this is what keeps the write-side rule equal to the
    premise the reading needs — `completed or timestamp` — rather than to the narrower claim
    "an explicit stamp is checked".
    """
    f = _write_rant_lines(tmp_path, [
        {"timestamp": "2099-01-01T00:00:00+08:00", "project": "emrg",
         "status": "in_progress", "progress": None, "completed": None,
         "message": "a row dated in the future"},
    ])

    ok, msg = update_rant(f, "2099-01-01T00:00:00+08:00", status="completed")

    assert not ok, msg
    assert "precedes the rant's submission instant" in msg, msg
    entry = json.loads(f.read_text(encoding="utf-8").strip())
    assert entry["status"] == "in_progress" and entry["completed"] is None


def test_a_completion_stamp_is_written_only_with_the_completed_status(tmp_path):
    """A stamp the state machine calls unfinished is refused, and the legitimate re-stamp is not.

    The two write paths of `update_rant` disagreed: the transition branch cleared the field for
    every target but `completed` (`r["completed"] = stamp if target == "completed" else None`),
    while the explicit-argument branch wrote it unconditionally afterwards. So
    `status="in_progress", completed=<stamp>` — and a bare `completed` on a `pending` row —
    stored a completion stamp on a row every reader ranks by its `status` as unfinished, and
    `action="list"` then printed `status=pending | completed=2026-…` in one row. Refused here,
    nothing written.

    The last block is the other direction, and it is why this is a guard rather than a
    prohibition: re-stamping an **already** `completed` row is the documented use of the
    argument (`_completion_ordering_error`'s own test does it), so a fix that refused every
    explicit stamp would pass the assertions above and break that one.
    """
    stamp = "2026-08-18T10:00:00+08:00"
    row = {"timestamp": stamp, "project": "emrg", "status": "in_progress",
           "progress": None, "completed": None, "message": "test rant"}

    # An allowed transition into a non-completed status, while handing over a completion stamp.
    f = _write_rant_lines(tmp_path, [{**row, "status": "pending"}])
    before = f.read_text(encoding="utf-8")
    ok, msg = update_rant(f, stamp, status="in_progress",
                          completed="2026-08-19T09:00:00+08:00")
    assert not ok, msg
    assert "only together with status='completed'" in msg, msg
    assert f.read_text(encoding="utf-8") == before, "a refused update must write nothing"

    # No status at all: the row stays `pending` and must not take a stamp either.
    f2 = _write_rant_lines(tmp_path, [{**row, "status": "pending"}])
    before2 = f2.read_text(encoding="utf-8")
    ok, msg = update_rant(f2, stamp, completed="2026-08-19T09:00:00+08:00")
    assert not ok, msg
    assert "only together with status='completed'" in msg, msg
    assert f2.read_text(encoding="utf-8") == before2, "a refused update must write nothing"

    # The other direction: an already-completed row may be re-stamped explicitly.
    f3 = _write_rant_lines(tmp_path, [{**row, "status": "completed",
                                       "completed": "2026-08-18T12:00:00+08:00"}])
    ok, msg = update_rant(f3, stamp, completed="2026-08-19T09:00:00+08:00")
    assert ok, msg
    entry = json.loads(f3.read_text(encoding="utf-8").strip())
    assert entry["status"] == "completed"
    assert entry["completed"] == "2026-08-19T09:00:00+08:00"


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


def test_the_list_action_declares_every_argument_it_reads(tmp_path, monkeypatch):
    """A parameter the renderer reads but the schema omits is one no caller can pass.

    The scan-view default (rant 2026-10-15T12:35:03 item (b)) added `full`, and the whole
    point of the change is that a caller *chooses* to pay for the text — so the switch has to
    be visible in the schema the model is shown, not only in the code. A provider that
    validates arguments against `parameters` drops an undeclared key silently, and the caller
    gets the scan view while believing it asked for the text: the silent half of the defect
    the `…` marker and the withheld note exist to prevent, moved up a layer.

    Asserted by reading the renderer's own `arguments.get(...)` keys off the source, so the
    test cannot drift into a list of names someone maintains by hand.
    """
    import inspect

    import emrg.tools.submit_rant_tool as mod

    d = SubmitRantTool().definition()
    declared = set(d.parameters["properties"])
    src = inspect.getsource(mod.SubmitRantTool._execute_list)
    read = set(re.findall(r'arguments\.get\("([a-z_]+)"', src))
    # `full` is read through the shared reader rather than `arguments.get`, and a guard
    # that knew only the first spelling would stop seeing it the moment it moved — the
    # read would be invisible, so an undeclared name handed to the helper would pass.
    # Both spellings, so the guard reads the *call* and not one of its forms.
    read |= set(re.findall(r'boolean_argument\(\s*arguments,\s*"([a-z_]+)"', src))
    undeclared = sorted(read - declared)
    assert not undeclared, (
        f"`_execute_list` reads {undeclared} but the tool schema does not declare "
        f"them — a caller cannot ask for what the code offers: {sorted(declared)}"
    )
    assert "full" in declared, "the scan-view escape hatch is the change's whole point"
    assert d.parameters["properties"]["full"]["type"] == "boolean"


def test_full_is_read_by_the_shared_reader_so_a_bad_spelling_is_refused(tmp_path, monkeypatch):
    """A declared boolean the caller spells out of range is a caller error, not a value.

    `full` was read by a hand-rolled spelling list — `full.strip().lower() in
    ("1","true","yes","y","on")`, else `bool(full)` — so **nothing was refused** and the two
    out-of-domain readings were both wrong in the direction this action exists to fix
    (issue #1935's family, measured on this branch 2026-10-15):

    * `full="maybe"` — a caller who asked for the text — printed the **scan view** and said
      nothing, which is exactly the silent withholding the `…` marker and the withheld note
      were added to prevent, living inside the parameter that adds the escape hatch;
    * `full=2.0` printed the full body from a value the schema does not admit.

    The repository already has this rule and this reader: `emrg.tools.base.boolean_argument`
    reads the two spellings that name a value and refuses everything else, naming the
    parameter and the value. #2294 extends that rule to every parameter a tool declares
    `"type": "boolean"`, so this carrier is read by the shared reader like `edit.replace_all`
    — which costs the lenient spellings (`"yes"`, `1`, `"on"`) that used to work. That is the
    decision this test pins rather than a silent narrowing.

    The discriminator is the *body marker*: a refused call must not have printed the text,
    an accepted one must, and a scan view must show its own name — so the three answers are
    told apart by what they returned, not by an exit code.
    """
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    # The marker sits in the **tail**: the scan view's row carries a ~100-character excerpt
    # of the message's head, so a head marker appears in both readings and could not tell
    # them apart. What only the full read has is the end of the body — and what only the scan
    # has is its own line saying it withheld something.
    _write_rant_lines(tmp_path, [
        {"timestamp": "2026-10-15T09:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": "head-excerpt " + "a" * 120 + " tail-marker-" + "z" * 300},
    ])
    tool = SubmitRantTool()

    def answer(**kwargs):
        return asyncio.run(tool.execute({"action": "list", **kwargs}))

    # The two spellings that name a value do what they say: the whole body, tail included,
    # and no line claiming anything was withheld...
    for spelling in (True, "true"):
        out = answer(full=spelling).content
        assert "tail-marker" in out, (
            f"full={spelling!r} is a spelling the shared reader honours, so it must "
            f"return the text"
        )
        assert "scan view —" not in out, "a read that withheld nothing says no such line"
    # ...the other value's spelling, and an absent parameter, are the scan view: the tail is
    # gone, and the read says so...
    for out in (answer(), answer(full=False), answer(full="false")):
        assert "tail-marker" not in out.content, "a scan view must not print the body"
        assert "scan view" in out.content, "a read that withheld must say so"

    # ...and everything else is refused, with the parameter and the value named. `"maybe"`
    # and `2.0` are the two the old path read *wrong*; the rest are the lenient spellings
    # the shared reader does not admit.
    for value in ("maybe", "yes", "on", "y", "no", "1", 1, 0, 2.0, []):
        result = answer(full=value)
        assert result.error, (
            f"full={value!r} was accepted and read as a value rather than refused — the "
            f"reader's domain is a `bool` or the strings 'true'/'false'"
        )
        assert "full" in result.content and "true or false" in result.content, (
            f"the refusal must name the parameter and the domain: {result.content!r}"
        )
        assert "tail-marker" not in result.content, (
            f"full={value!r} was refused but the read printed the body anyway"
        )

    # The schema tells the caller the domain before it errs — the same sentence the
    # `replace_all` carrier is held to, read out of the schema rather than a literal here.
    desc = SubmitRantTool().definition().parameters["properties"]["full"]["description"]
    assert "true" in desc and "false" in desc and "refused" in desc, (
        f"the `full` description no longer states the domain or the refusal: {desc!r}"
    )


def test_the_list_action_returns_the_whole_message_when_the_caller_asks(tmp_path, monkeypatch):
    """The message IS the rant, so a 100-character excerpt is not a reading of it.

    Measured 2026-09-17: the only rant in flight had a **3504**-character message and
    `action=list` showed its first 100 — 97% dropped by the very path the task templates
    route every read through (`paper_prompt.md` says to check the queue with
    `submit_rant(action="list")` and that there is no reason to open the file at all,
    "not even to read it"; `promote_prompt.md` deduplicates against the same call). A read
    path that cannot deliver the text is not a read path — and the failure is silent: the
    caller sees a plausible sentence and never learns the rest existed.

    Asserted on the *tail* of the message and on an interior line, because those are what
    a truncation at the front removes: a substring taken from the beginning passes under
    `[:100]` and would have made this test green over the defect it exists for.

    The call shape changed on 2026-10-15 (rant 2026-10-15T12:35:03 item (b)): the whole text
    is what a **named slice** buys — `full=True` here, `status=` in the sibling test — not
    what every unfiltered read pays for. What this test pins is unchanged in substance: the
    text the caller asked for arrives **line for line**, tail included.

    The fixture is synthetic and deliberately shaped like the real one (multi-paragraph
    body, long tail) — it quotes no host and names no real rant.
    """
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    message = (
        "line one of the rant, the part a summary keeps\n\n"
        "  an indented detail line\n\n"
        "a third paragraph, which a 100-character cut removes entirely along with"
        " the rest of the message."
        + " tail-marker-" + "z" * 300
    )
    _write_rant_lines(tmp_path, [
        {"timestamp": "2026-09-17T09:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": message},
        {"timestamp": "2026-09-17T09:01:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": "short rant"},
    ])
    tool = SubmitRantTool()
    out = __import__("asyncio").run(
        tool.execute({"action": "list", "full": True})).content

    # Read the message back off the output instead of searching for the raw string: the
    # block is indented under its header line, so the message is present line by line and
    # never as one literal run of characters. Comparing the whole block is also what makes
    # the assertion about *all* of the text — a `in out` check on the head of the message
    # passes under the truncation this test exists for.
    lines = out.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("2026-09-17T09:00:00"))
    block: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("2026-09-17T09:01:00"):  # the next header ends this rant's block
            break
        block.append(line[4:] if line.startswith("    ") else line)
    assert "\n".join(block) == message, (
        "the whole message has to arrive, line for line; a caller that gets a "
        "100-character excerpt cannot decide the rant's relevance"
    )
    assert "tail-marker-" + "z" * 300 in out, "the tail of a long message is the part truncation eats"
    assert "short rant" in out
    # The header line is the scan view: it says it is an excerpt rather than pretending
    # to be the text. Both arms, so the marker cannot be unconditional.
    header = next(line for line in lines if line.startswith("2026-09-17T09:00:00"))
    assert header.endswith("…"), f"a truncated header must say so: {header[-60:]!r}"
    short_header = next(line for line in lines if line.startswith("2026-09-17T09:01:00"))
    assert not short_header.endswith("…"), f"a message that fits needs no marker: {short_header!r}"
    # The read that asked for the text is not told it is being shown a scan view.
    assert "scan view —" not in out, (
        "the closing line belongs to a withheld read; a `full=True` call withheld nothing"
    )


def test_an_unfiltered_list_is_the_scan_view_and_names_the_call_that_returns_the_text(
    tmp_path, monkeypatch,
):
    """The default read costs rows, and every withheld body says how to get it back.

    Rant 2026-10-15T12:35:03 item (b), host decision 「就定 a + b（c 去掉）」 (2026-10-15
    12:34:24, session `s_260727_1103_866d`): the queue is read to decide *which* rant to act
    on, yet the read carried every body. Measured on the live queue the same day — 50 rants,
    2 pending — an unfiltered `list` returned **26,208 characters (~7,280 tokens)** and
    `list(status="pending")` returned **6,431**; the difference is bodies nobody had asked
    for, re-paid in the transcript every round after.

    Four things are asserted together, each failing alone:
    (a) the body is absent from the default read — the cost;
    (b) the row is still there and still bounded — the queue is whole, and `cleanup` decides
        by recency and status, i.e. by the row, so dropping rows would break curation;
    (c) the absence is **stated**, per row and once for the read, and each note names a call
        that actually returns that text — the silent half of the 2026-09-17 defect, whose new
        shape is a block that reads as "the queue holds nothing else";
    (d) `full=True` returns every body line for line, so nothing this action ever printed
        became unreachable.
    """
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    work = ("the rant in flight, whose tail is the part a scan view drops"
            + " work-tail-" + "w" * 400)
    history = ("a completed rant's body, written when it was filed"
               + " history-tail-" + "h" * 400)
    _write_rant_lines(tmp_path, [
        {"timestamp": "2026-10-15T09:00:00+08:00", "project": "emrg",
         "status": "in_progress", "progress": "Stage 1 landed (PR #1)",
         "completed": None, "message": work},
        {"timestamp": "2026-10-15T09:01:00+08:00", "project": "emrg",
         "status": "completed", "progress": "done",
         "completed": "2026-10-15T10:00:00+08:00", "message": history},
    ])
    tool = SubmitRantTool()
    scan = __import__("asyncio").run(tool.execute({"action": "list"})).content
    lines = scan.splitlines()

    # (a) neither body is in the default read, whole or in part past the excerpt.
    assert "work-tail-" + "w" * 400 not in scan, "the scan view must not pay for a body"
    assert "history-tail-" + "h" * 400 not in scan
    assert "    " + work.splitlines()[0] not in scan, (
        "an indented body block is the shape the scan view must not take"
    )

    # (b) both rows are present, bounded, and excerpted — the queue is whole.
    rows = [l for l in lines if l.startswith("2026-10-15T09:0")]
    assert len(rows) == 2, f"one row per rant, completed ones included: {rows!r}"
    assert all(len(r) < 400 for r in rows), [len(r) for r in rows]
    assert any("status=in_progress" in r and "the rant in flight" in r for r in rows), rows
    assert any("status=completed" in r for r in rows), rows

    # (c) stated twice over: per row, naming the call that returns *that* row's text.
    notes = [l for l in lines if l.startswith("    (text withheld")]
    assert len(notes) == 2, f"one note per withheld body: {notes!r}"
    assert any('status="in_progress"' in n for n in notes), (
        f"a live rant's note must name the call that returns it: {notes!r}"
    )
    assert any('status="completed"' in n and "history" in n for n in notes), (
        f"a completed rant's note says why it is history: {notes!r}"
    )
    assert "scan view — 2 row(s)" in scan, (
        "the read itself says it showed rows only, and how many"
    )

    # (d) the way back, line for line.
    full = __import__("asyncio").run(
        tool.execute({"action": "list", "full": True})).content
    flines = full.splitlines()
    start = next(i for i, l in enumerate(flines) if l.startswith("2026-10-15T09:00:00"))
    block = []
    for line in flines[start + 1:]:
        if line.startswith("    progress:") or (line and not line.startswith("    ")):
            break
        block.append(line[4:] if line.startswith("    ") else line)
    assert "\n".join(block) == work, "full=True returns the body the scan view withheld"
    assert "history-tail-" + "h" * 400 in full, "…for every rant, completed included"
    assert "scan view —" not in full, "a read that withheld nothing says no such line"
    assert len(full) > len(scan), "asking for the text costs more than scanning does"


def test_project_alone_does_not_unlock_the_text(tmp_path, monkeypatch):
    """A filter that narrows nothing on the cost's axis is not a request for the body.

    The unlock is named precisely: the text comes when the caller filters by **status** or asks
    explicitly. That sentence is the wording of rant 2026-10-15T12:35:03 item (b)
    (「正文只在按 status 过滤或显式要求时给」), which writes out the host's decision of the same
    day, 2026-10-15T12:34:24: 「就定 a + b（c 去掉）」. `project=` is the odd one out, and
    deliberately so: it is the dedup filter `promote_prompt.md` uses, and on the queue measured
    2026-10-15 it selects **48 of 50** rows — `list(project="emrg")` cost 24,539 characters
    against an unfiltered 26,208, i.e. it narrowed the *subject*, not the cost. Printing bodies
    for it would reintroduce the default-read cost on the one filter a template really passes.
    """
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    body = "a pending rant in the project the caller named" + " tail-" + "t" * 400
    _write_rant_lines(tmp_path, [
        {"timestamp": "2026-10-15T09:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None, "message": body},
        {"timestamp": "2026-10-15T09:01:00+08:00", "project": "other",
         "status": "pending", "progress": None, "completed": None,
         "message": "another project's rant"},
    ])
    tool = SubmitRantTool()
    out = __import__("asyncio").run(
        tool.execute({"action": "list", "project": "emrg"})).content
    assert "2026-10-15T09:00:00" in out, "the filter still selects the row"
    assert "another project" not in out, "…and excludes the other project's"
    assert "tail-" + "t" * 400 not in out, (
        "a project filter selects rows, not bodies: it must stay a scan view"
    )
    assert "scan view — 1 row(s)" in out
    assert 'status="pending"' in out, "the note names the call that returns the text"


def test_the_list_header_is_bounded_and_the_full_progress_follows_it(tmp_path, monkeypatch):
    """The row is a scan view, so it is bounded — and nothing is cut on the way out.

    Measured 2026-09-17 on the live queue: the one rant in flight carried a 1651-character
    `progress` printed **in full** inside its header line, so the row a caller scans down was
    1743 characters — the scan view was itself the bulk of the output, and the field it was
    supposed to frame was a tenth of it. Bounded here, with the whole value following in a
    `progress:` block, on the same rule the message already follows: an excerpt in the scan
    view *and* the full text, never one without the other.

    Both arms of the marker are asserted for `progress` as well as for the message. The
    failure this guards is not only "the text was cut" — the block proves it was not — but
    "the cut was not visible": a row silently showing a third of a field is indistinguishable
    from a row showing the field, which is what made the original truncation survive as long
    as it did.
    """
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    progress = (
        "Stage 1 landed; the guard half is still open\n\n"
        "  a detail line inside the progress\n\n"
        "and a closing line that a header excerpt removes."
        + " progress-tail-" + "p" * 300
    )
    message = ("a short message, then enough filler that the marker below sits past the "
               "excerpt's " + "f" * 80 + " message-tail-" + "m" * 300)
    _write_rant_lines(tmp_path, [
        {"timestamp": "2026-09-17T09:00:00+08:00", "project": "emrg",
         "status": "in_progress", "progress": progress, "completed": None,
         "message": message},
        {"timestamp": "2026-09-17T09:01:00+08:00", "project": "emrg",
         "status": "pending", "progress": "PR #1 submitted", "completed": None,
         "message": "short rant"},
    ])
    out = __import__("asyncio").run(
        SubmitRantTool().execute({"action": "list", "full": True})).content
    lines = out.splitlines()

    header = next(l for l in lines if l.startswith("2026-09-17T09:00:00"))
    # (a) the row is an excerpt of both fields, not either field: neither tail is in it, and
    #     a row that carried one of them whole would be unbounded by construction.
    assert "progress-tail-" not in header, f"the header carries the whole progress: {len(header)} chars"
    assert "message-tail-" not in header, f"the header carries the whole message: {len(header)} chars"
    assert len(header) < 400, (
        f"the header is a scan view, so it is bounded; this one is {len(header)} characters"
    )
    # (b) both arms of the marker, on the progress field as well as on the message.
    assert "progress=Stage 1 landed; the guard half is still open" in header
    assert "progress=Stage 1 landed; the guard half is still open\n" not in header + "\n"
    assert "… | completed=None" in header, f"a cut progress must say so: {header!r}"
    short_header = next(l for l in lines if l.startswith("2026-09-17T09:01:00"))
    assert "progress=PR #1 submitted |" in short_header, short_header
    assert "…" not in short_header.split(" | ")[3], f"a progress that fits needs no marker: {short_header!r}"

    # (c) the full progress arrives, in its own block, line for line — read back off the
    #     output rather than searched for, because the block is indented and the value is
    #     therefore never one literal run of characters in the output.
    start = lines.index(header)
    assert lines[start + 1].startswith("    "), "the message block follows the header"
    label = next(i for i in range(start + 1, len(lines)) if lines[i] == "    progress:")
    progress_block: list[str] = []
    for line in lines[label + 1:]:
        if line and not line.startswith("      "):  # the block's own indent; a header ends it
            break
        progress_block.append(line[6:] if line else "")
    assert "\n".join(progress_block) == progress, (
        "the whole progress has to arrive, line for line; the row above shows an excerpt of it"
    )
    assert "progress-tail-" + "p" * 300 in out, "the tail of the progress is what a header cut removes"
    # (d) the message is not the casualty of the space the progress block takes.
    assert "message-tail-" + "m" * 300 in out, "the message still arrives whole"


def test_a_completed_body_is_withheld_and_one_call_returns_it(tmp_path, monkeypatch):
    """The default read costs what the work costs; history stays reachable.

    Issue #1514, measured on the host that filed it: `list` returned the whole message and the
    whole `progress` of every rant on the queue, **51,482 characters for 13 rants** — eleven of
    them `completed`, five of those carrying 3.5-5 K-character bodies — while the actionable
    part (pending + one `in_progress`) was a small fraction. Every task template routes its
    review through this call, so the same ~52 K (~13 K tokens) was re-paid two or three times a
    cycle for text whose only role was provenance. The comment above the rendering argued that
    a *cap* "would be the same defect with a larger number in it"; this pins the distinction
    the issue drew instead — a **scope** that is still complete, because the withheld text is
    one documented call away (`status="completed"`, the narrowing mechanism the same comment
    already endorsed and no filter on the cost's own axis).

    Three things are asserted together, and each fails alone in a way the others do not catch:
    the body is absent (the cost), the absence is *stated* (the silent half of the 2026-09-17
    defect — an empty block is indistinguishable from an empty message), and the filtered call
    returns it line for line (the guarantee the original refusal was protecting). The rows are
    asserted present for the withheld rant as well: `cleanup` decides by recency and status,
    i.e. by the row, so dropping completed rows would break curation instead of saving tokens.
    """
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    body = (
        "a completed rant's body, written when it was filed\n\n"
        "  an interior line a reader would have seen\n\n"
        "and a tail that says nothing about the work left to do."
        + " history-tail-" + "h" * 300
    )
    progress = ("Stage 1 landed; Stage 2 is the remaining half."
                + " progress-tail-" + "p" * 300)
    _write_rant_lines(tmp_path, [
        {"timestamp": "2026-09-20T09:00:00+08:00", "project": "emrg",
         "status": "completed", "progress": progress,
         "completed": "2026-09-20T18:00:00+08:00", "message": body},
        {"timestamp": "2026-09-21T09:00:00+08:00", "project": "emrg",
         "status": "pending", "progress": None, "completed": None,
         "message": "the work actually in flight"},
    ])
    tool = SubmitRantTool()
    out = __import__("asyncio").run(tool.execute({"action": "list"})).content
    lines = out.splitlines()

    # (a) the cost: the completed rant contributes **two lines** — its row and the note — so
    #     neither its body nor its full `progress` block is paid for again. The row keeps the
    #     100-character excerpts of both fields (the scan view, and the reason the tail run is
    #     the thing to assert absent: a bounded excerpt may well contain the head of it).
    header = next(l for l in lines if l.startswith("2026-09-20T09:00:00"))
    assert header.startswith(
        "2026-09-20T09:00:00+08:00 | emrg | status=completed | progress=")
    assert " | completed=2026-09-20T18:00:00+08:00 | a completed rant's body" in header, header
    assert len(header) < 400, f"the row is a bounded scan view: {len(header)} chars"
    assert "history-tail-" + "h" * 300 not in out, "a completed body is history, not work"
    assert "progress-tail-" + "p" * 300 not in out, "a completed progress is withheld too"
    header_at = lines.index(header)
    next_row = next(i for i in range(header_at + 1, len(lines))
                    if lines[i] and not lines[i].startswith(" "))
    assert next_row - header_at == 2, (
        "a completed row is its row plus one note, never a text block: "
        f"{lines[header_at:next_row]!r}"
    )

    # (b) the disclosure: the withheld row says so and names the call that returns it. This is
    #     the arm that keeps the next reader from concluding "this rant has no message". Since
    #     2026-10-15 (item (b) of rant 2026-10-15T12:35:03) the default read withholds every
    #     body, so the completed row's note is the one that says *why* — history, not work —
    #     and names `status="completed"`, the call that returns history whole.
    withheld = [l for l in lines if "withheld" in l and "history" in l]
    assert len(withheld) == 1, f"exactly the completed row is marked as history: {withheld!r}"
    assert 'status="completed"' in withheld[0], (
        f"the note has to name the one call that returns the text: {withheld[0]!r}"
    )

    # (c) the queue is whole, and the actionable text is untouched: the pending rant's body
    #     arrives even though it sits after the withheld one (the `continue` must not eat it).
    assert any(l.startswith("2026-09-21T09:00:00+08:00") for l in lines)
    assert "the work actually in flight" in out

    # (d) reachability, line for line off the output (the block is indented, so the value is
    #     never one literal run of characters) — the property the scope had to preserve.
    filtered = __import__("asyncio").run(
        tool.execute({"action": "list", "status": "completed"})).content
    flines = filtered.splitlines()
    start = next(i for i, l in enumerate(flines) if l.startswith("2026-09-20T09:00:00"))
    block: list[str] = []
    for line in flines[start + 1:]:
        if line == "    progress:":
            break
        block.append(line[4:] if line.startswith("    ") else line)
    assert "\n".join(block) == body, (
        "a status-filtered call is the documented way back to the withheld text"
    )
    assert "progress-tail-" + "p" * 300 in filtered, "the progress block comes back too"


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



# ── the rant store asks its subject's kind before opening it (issue #2097) ────
#
# `rants.jsonl` is the host's feedback channel and the evolution loop's queue, and it is
# opened by path in three places (`_read_rants`, `_write_rants`, the tool's `projects.yml`
# read). `exists()` was the guard, and it is the *wrong* question: it is true for a FIFO,
# for a socket and for a device, and an `open()` on any of those blocks — the read until a
# writer appears, the write until a reader does, a device never. Nothing is raised, so no
# `except` can bound the consequence, and the tool runs on the daemon's event loop: the
# block is the whole server, the host's connection included.
#
# Measured on `cf9304c7` before the fix (`cyc20261011-114739`), one child process per arm
# under an 8 s cap: `_read_rants` against a FIFO **did not return**, the same call on an
# absent path answered at once. So each test below is written the way this repository
# writes this class: **removing the guard makes it hang, not fail** — hence the deadline
# from `tests.bounded_read`, which turns "never returns" into a readable assertion.
#
# The subjects are pipes, so the family is gated on `mkfifo` (Windows has none). Every
# **control** is pipe-free on purpose, so it runs on both CI legs and pins the half of the
# contract the guard could have broken: absence still means "no rants", and the writer must
# still create a log that does not exist yet.


_needs_mkfifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")


@_needs_mkfifo
def test_read_rants_refuses_a_named_pipe_instead_of_wedging(tmp_path):
    """The store's reader names the kind rather than opening it."""
    fifo = tmp_path / "rants.jsonl"
    os.mkfifo(fifo)

    with pytest.raises(OSError) as excinfo:
        within(DEADLINE_SECONDS, lambda: _read_rants(fifo))

    assert "a FIFO (named pipe)" in str(excinfo.value), str(excinfo.value)
    assert "not a regular file" in str(excinfo.value), str(excinfo.value)


@_needs_mkfifo
def test_write_rants_refuses_a_named_pipe_too(tmp_path):
    """The writer carries its own guard: a read-side guard does not remove this block.

    `append_rant` reads *then* writes, so a guard on the read alone leaves the write's own
    block reachable — and `open(path, "w")` on a FIFO waits for a **reader**, the opposite
    peer to the read's, so it is a second blocking site rather than the same one.
    """
    fifo = tmp_path / "rants.jsonl"
    os.mkfifo(fifo)
    row = {
        "timestamp": "2026-10-11T11:00:00+08:00",
        "project": "emrg",
        "status": "pending",
        "progress": None,
        "completed": None,
        "message": "x",
    }

    with pytest.raises(OSError) as excinfo:
        within(DEADLINE_SECONDS, lambda: _write_rants(fifo, [row]))

    assert "a FIFO (named pipe)" in str(excinfo.value), str(excinfo.value)
    assert "not a regular file" in str(excinfo.value), str(excinfo.value)


@_needs_mkfifo
def test_append_rant_on_a_named_pipe_refuses_rather_than_wedging(tmp_path):
    """The public path the daemon and the tool both call, end to end."""
    fifo = tmp_path / "rants.jsonl"
    os.mkfifo(fifo)

    with pytest.raises(OSError) as excinfo:
        within(DEADLINE_SECONDS, lambda: append_rant(fifo, "host feedback", "emrg"))

    assert "not a regular file" in str(excinfo.value), str(excinfo.value)


@_needs_mkfifo
def test_the_list_action_on_a_named_pipe_is_a_tool_error_not_a_hang(tmp_path, monkeypatch):
    """What a caller actually sees: an error result, on both faces of the family.

    The refusal must not escape as an unhandled exception either — the daemon runs tools
    in a loop that must survive a bad subject, and `_execute_list` already answers a failed
    read that way.
    """
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    os.mkfifo(tmp_path / "rants.jsonl")
    tool = SubmitRantTool()

    for action in ("list", "cleanup"):
        result = within(DEADLINE_SECONDS, lambda a=action: asyncio.run(tool.execute({"action": a})))
        assert result.error is True, result
        assert "not a regular file" in result.content, result.content


@_needs_mkfifo
def test_a_named_pipe_projects_yml_yields_no_candidates(tmp_path, monkeypatch):
    """The advisory read degrades instead of refusing, as it already did for a missing file.

    `projects.yml` only feeds a warning about a mis-spelled project name, so turning its
    unreadability into a hard failure would break `submit` for a reason unrelated to the
    rant. The control in the same test is a real file: the read still works.
    """
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    tool = SubmitRantTool()
    os.mkfifo(tmp_path / "projects.yml")

    assert within(DEADLINE_SECONDS, tool._registered_project_names) == []

    (tmp_path / "projects.yml").unlink()
    (tmp_path / "projects.yml").write_text(
        "- name: emrg\n- name: aitokenpool\n", encoding="utf-8")

    assert sorted(within(DEADLINE_SECONDS, tool._registered_project_names)) == [
        "aitokenpool", "emrg"]


def test_an_absent_rant_log_reads_empty_and_the_writer_creates_it(tmp_path):
    """The control for the whole family — and it runs on Windows, so it is a real control.

    `non_regular_kind` *stats*, so absence arrives as an `OSError` rather than as `None`.
    Both sites branch on that by treating it as the *old* answer: the reader answers `[]`
    (the reading it has always given a missing log) and the writer proceeds to create the
    path. Reading absence as a refusal would wedge the writer on a fresh install — the
    shape `#2095` was fixed for — and answering a FIFO with `[]` would state a falsehood
    about the host's queue, which is why the refusal is the reader's non-absent branch.
    """
    log = tmp_path / "rants.jsonl"
    assert _read_rants(log) == []
    assert not log.exists(), "a read must not create the log"

    write = tmp_path / "nested" / "rants.jsonl"
    _write_rants(write, [])
    assert write.exists(), "the writer creates its subject and its parent"

    assert _read_rants(write) == []
    assert append_rant(write, "host feedback", "emrg") == 1
    assert [r["message"] for r in _read_rants(write)] == ["host feedback"]


def test_projects_yml_absent_or_malformed_yields_no_candidates(tmp_path, monkeypatch):
    """The pipe-free control for the advisory read: absent, malformed, and well-formed."""
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    tool = SubmitRantTool()

    assert tool._registered_project_names() == []       # absent
    (tmp_path / "projects.yml").write_text("just a string\n", encoding="utf-8")
    assert tool._registered_project_names() == []       # not a list
    (tmp_path / "projects.yml").write_text(": :\n", encoding="utf-8")
    assert tool._registered_project_names() == []       # unparseable
    (tmp_path / "projects.yml").write_text(
        "- name: emrg\n- notadict\n- name: ''\n", encoding="utf-8")
    assert tool._registered_project_names() == ["emrg"]
