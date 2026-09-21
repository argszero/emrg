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


def test_the_list_action_returns_the_whole_message_not_a_summary(tmp_path, monkeypatch):
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
    out = __import__("asyncio").run(tool.execute({"action": "list"})).content

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
    out = __import__("asyncio").run(SubmitRantTool().execute({"action": "list"})).content
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
    #     the arm that keeps the next reader from concluding "this rant has no message".
    withheld = [l for l in lines if "withheld" in l]
    assert len(withheld) == 1, f"exactly the completed row is marked: {withheld!r}"
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

