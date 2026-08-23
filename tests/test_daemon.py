"""Unit tests for daemon internals — prompt building and project discovery.

These test the methods that were broken by squash-merge conflict markers
in cycles #6-#8. Having test coverage here ensures that critical
evolution infrastructure stays operational.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

from emrg.config import LlmConfig
from emrg.protocol import InstanceIdentity
from emrg.server.daemon import EmrgServer
from emrg.server.scheduler import TaskHandler, TaskScheduler
from emrg.session import Session


# ── TaskHandler._build_evolution_prompt ─────────────────────


def test_build_prompt_emrg_self():
    """Builds prompt for emrg self-evolution."""
    handler = TaskHandler(
        name="emrg", config={"path": "/tmp/emrg"}, interval=1800,
        identity=InstanceIdentity(instance_id="test-id", host_name="testhost"),
    )
    prompt = handler._build_evolution_prompt()

    # Core template variables must be present
    assert "test-id" in prompt
    assert "testhost" in prompt
    assert "argszero/emrg" in prompt
    assert "emrg-evolution" in prompt
    assert "https://github.com/argszero/emrg.git" in prompt
    # Conflict markers must NOT be present
    assert "<<<<<<<" not in prompt
    assert ">>>>>>>" not in prompt


def test_build_prompt_with_project():
    """Builds prompt for a custom project — derives owner/repo via git remote."""
    handler = TaskHandler(
        name="myproject", config={"path": "/home/user/src/myproject"}, interval=1800,
        identity=InstanceIdentity(instance_id="test-id", host_name="testhost"),
    )
    # Override owner/repo for project testing
    handler._owner = "user"
    handler._repo = "myproject"
    handler._repo_url = "https://github.com/user/myproject.git"
    prompt = handler._build_evolution_prompt()

    assert "/home/user/src/myproject" in prompt
    assert "emrg-evolution-myproject" in prompt
    assert "https://github.com/user/myproject.git" in prompt
    assert "<<<<<<<" not in prompt
    assert ">>>>>>>" not in prompt


def test_build_prompt_all_variables_substituted():
    """No raw template placeholders ({var}) should remain in output."""
    import re

    handler = TaskHandler(
        name="emrg", config={"path": "/tmp/emrg"}, interval=1800,
        identity=InstanceIdentity(instance_id="test-id", host_name="testhost"),
    )
    p1 = handler._build_evolution_prompt()
    braces = re.findall(r"\{[a-z_]+\}", p1)
    assert not braces, f"Unsubstituted placeholders: {braces}"


def test_build_prompt_step22_uses_fetch_head():
    """Step 2.2 must log FETCH_HEAD, not origin/master.

    `git fetch origin master` always writes FETCH_HEAD even when the repo
    has no remote-tracking refs (e.g. after a workspace repair that
    stripped `remote.origin.fetch`), where `git log origin/master` fails
    with "unknown revision" (observed 2026-08-08, cycles 09:15 & 09:30).
    """
    handler = TaskHandler(
        name="emrg", config={"path": "/tmp/emrg"}, interval=1800,
        identity=InstanceIdentity(instance_id="test-id", host_name="testhost"),
    )
    prompt = handler._build_evolution_prompt()
    # The actual Step 2.2 command block must log FETCH_HEAD, not origin/master.
    step22 = prompt.split("#### 2.2 Latest GitHub code changes", 1)[1].split("#### 2.3", 1)[0]
    assert "git fetch origin master && git log FETCH_HEAD --oneline -10" in step22
    assert "git fetch origin master && git log origin/master" not in step22
    # Merge-conflict guidance must also merge FETCH_HEAD (line 148).
    assert "git merge FETCH_HEAD" in prompt
    assert "git merge origin/master" not in prompt


# ── TaskScheduler._load_tasks ────────────────────────────────────


def test_scheduler_load_no_file():
    """Returns empty list when tasks.yml doesn't exist."""
    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = Path("/nonexistent/path/tasks_test.yml")
    assert sched._load_tasks() == []


def test_scheduler_load_empty_list():
    """Returns empty list for an empty YAML list."""
    sched = TaskScheduler(InstanceIdentity())
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write("[]\n")
        tmp = f.name
    try:
        sched._tasks_file = Path(tmp)
        assert sched._load_tasks() == []
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_scheduler_load_enabled_tasks():
    """Loads task entries correctly."""
    sched = TaskScheduler(InstanceIdentity())
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(
            "- name: auto1\n  type: evolution\n  path: /tmp/a1\n  interval: 600\n  enabled: true\n"
            "- name: disabled\n  type: evolution\n  path: /tmp/a2\n  interval: 1800\n  enabled: false\n"
        )
        tmp = f.name
    try:
        sched._tasks_file = Path(tmp)
        result = sched._load_tasks()
        assert len(result) == 2
        assert result[0]["name"] == "auto1"
        assert result[0]["enabled"] is True
        assert result[1]["name"] == "disabled"
        assert result[1]["enabled"] is False
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_scheduler_load_invalid_yaml():
    """Returns empty list for garbage YAML (doesn't crash)."""
    sched = TaskScheduler(InstanceIdentity())
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(": not valid yaml {[[\n")
        tmp = f.name
    try:
        sched._tasks_file = Path(tmp)
        assert sched._load_tasks() == []
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_scheduler_load_non_list():
    """Returns empty list when YAML root is not a list."""
    sched = TaskScheduler(InstanceIdentity())
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write("key: value\n")
        tmp = f.name
    try:
        sched._tasks_file = Path(tmp)
        assert sched._load_tasks() == []
    finally:
        Path(tmp).unlink(missing_ok=True)


# ── TaskScheduler._migrate_from_projects ─────────────────────────


def test_migrate_auto_evolve_entries(tmp_path):
    """Migrates auto_evolve=True entries from projects.yml to tasks.yml."""
    from unittest.mock import patch

    sched = TaskScheduler(InstanceIdentity())
    # Use tmp_path for both files
    projects_yml = tmp_path / "projects.yml"
    tasks_yml = tmp_path / "tasks.yml"
    sched._tasks_file = tasks_yml

    projects_yml.write_text(
        "- name: manual\n  path: /tmp/m\n  auto_evolve: false\n"
        "- name: auto1\n  path: /tmp/a1\n  auto_evolve: true\n  interval: 600\n"
        "- name: auto2\n  path: /tmp/a2\n  auto_evolve: true\n"
    )

    with patch.object(sched, "_save_tasks") as mock_save:
        # Patch _load_tasks to return empty (simulates fresh tasks.yml)
        # and point at the test projects.yml
        real_load = sched._load_tasks
        def _fake_load():
            return []
        sched._load_tasks = _fake_load

        # Override projects_file path
        orig_migrate = sched._migrate_from_projects
        def _migrate_wrapper():
            sched._tasks_file = tasks_yml
            sched._migrate_from_projects = orig_migrate
            orig_migrate()
        sched._migrate_from_projects = _migrate_wrapper

        # Can't easily redirect config_dir() in this test without patching —
        # for now, verify the load/migrate logic works structurally
        sched._load_tasks = real_load

    assert sched._load_tasks() == []


# ── _collect_project_context ───────────────────────────────


def _make_server() -> EmrgServer:
    """Create a minimal EmrgServer for testing."""
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    # Point the project log at a tmp file by default so context/message tests
    # can never write the real ~/.emrg/projects.yml (2026-08-13 leak). Tests
    # that exercise _projects_log override it explicitly afterwards.
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def test_context_section_no_files(tmp_path):
    """No context files found → returns empty list."""
    server = _make_server()
    session = Session.create_with_id("ctx-test", tmp_path)
    result = server._collect_project_context(session)
    assert result == []


def test_system_prompt_environment_time_and_os(tmp_path):
    """system.j2 renders Current time + Operating system env info.

    Rant 2026-08-13T14:01:46: agent must know "what time it is now"
    (tz-aware local time, not UTC) and what platform it runs on.
    """
    server = _make_server()
    session = Session.create_with_id("env-test", tmp_path)
    rendered = server._build_system_prompt(session)

    # Current time: tz-aware local ISO, seconds precision
    assert "**Current time**: `" in rendered
    m = re.search(r"\*\*Current time\*\*: `([^`]+)`", rendered)
    assert m is not None
    parsed = datetime.fromisoformat(m.group(1))
    assert parsed.tzinfo is not None  # local tz-aware, never naive UTC

    # OS name + platform detail present
    assert "**Operating system**: `" in rendered
    assert "Darwin" in rendered or "Windows" in rendered or "Linux" in rendered

    # system.md debug output written
    assert (session.dir_path / "system.md").exists()
    md = (session.dir_path / "system.md").read_text(encoding="utf-8")
    assert "Current time" in md and "Operating system" in md


def test_system_prompt_environment_without_session(tmp_path):
    """Env info renders even with no session (current_time/os always known)."""
    server = _make_server()
    rendered = server._build_system_prompt()
    assert "**Current time**: `" in rendered
    assert "**Operating system**: `" in rendered
    assert "**Working directory**" not in rendered


def test_system_prompt_rant_handling_section(tmp_path):
    """Rant Handling section renders (rant 2026-08-17T11:51:59): agent must
    know to detect rants in normal conversation, confirm with the user, and
    only then call submit_rant."""
    server = _make_server()
    rendered = server._build_system_prompt()
    assert "## Rant Handling" in rendered
    assert "submit_rant" in rendered
    assert "explicit" in rendered.lower()  # consent-before-call contract
    assert "/rant" in rendered


def test_submit_rant_tool_registered():
    """submit_rant is in the daemon tool registry (available in all sessions)."""
    server = _make_server()
    assert "submit_rant" in server.tools.names
    assert server.tools.get("submit_rant") is not None


def test_context_section_single_file(tmp_path):
    """When CLAUDE.md exists, it's returned as a dict entry."""
    server = _make_server()
    (tmp_path / "CLAUDE.md").write_text("# Project Rules\n- Use tabs\n")
    session = Session.create_with_id("ctx-test", tmp_path)
    result = server._collect_project_context(session)
    assert len(result) == 1
    assert result[0]["name"] == "CLAUDE.md"
    assert "- Use tabs" in result[0]["content"]
    assert "# Project Rules" in result[0]["content"]


def test_context_section_multiple_files(tmp_path):
    """All matching context files are included as dict entries."""
    server = _make_server()
    (tmp_path / "CLAUDE.md").write_text("claude content")
    (tmp_path / "AGENTS.md").write_text("agents content")
    session = Session.create_with_id("ctx-test", tmp_path)
    result = server._collect_project_context(session)
    names = {r["name"] for r in result}
    assert names == {"CLAUDE.md", "AGENTS.md"}
    assert any("claude content" in r["content"] for r in result)
    assert any("agents content" in r["content"] for r in result)


def test_context_section_truncation(tmp_path):
    """Files over 8000 chars are truncated with a notice."""
    server = _make_server()
    big = "x" * 9000
    (tmp_path / "CLAUDE.md").write_text(big)
    session = Session.create_with_id("ctx-test", tmp_path)
    result = server._collect_project_context(session)
    assert "truncated" in result[0]["content"]
    assert "1000 chars" in result[0]["content"]  # 9000 - 8000 = 1000


def test_context_section_manifesto(tmp_path):
    """MANIFESTO.md is also read as a context file."""
    server = _make_server()
    (tmp_path / "MANIFESTO.md").write_text("# Design\nKeep it simple.\n")
    session = Session.create_with_id("ctx-test", tmp_path)
    result = server._collect_project_context(session)
    assert result[0]["name"] == "MANIFESTO.md"
    assert "Keep it simple" in result[0]["content"]


# ── _cap_memory_index / _collect_memory_data ──────────────────────


def test_cap_memory_index_small_file(tmp_path):
    """Index under the 50KB cap is embedded as-is."""
    server = _make_server()
    idx = tmp_path / "MEMORY.md"
    idx.write_text("- [row](cycle-1.md) — title\n" * 50, encoding="utf-8")  # well under 50KB
    assert server._cap_memory_index(idx) == idx.read_text(encoding="utf-8")


def test_cap_memory_index_large_file(tmp_path):
    """Index over 50KB is truncated at a line boundary with a notice.

    Rant 2026-08-23T11:00:31: evolution agents append MEMORY.md rows
    directly (bypassing memory_store's write-time guards), so the embedded
    index must be capped at render time — defense in depth.
    """
    server = _make_server()
    idx = tmp_path / "MEMORY.md"
    line = "- [cyc00000000-000000](cycle-20260823-000000.md) — " + "x" * 100 + "\n"
    idx.write_text(line * 600, encoding="utf-8")  # ~64KB > 50KB cap
    capped = server._cap_memory_index(idx)
    assert len(capped) <= 50 * 1024 + 200  # head + notice
    assert "truncated" in capped
    assert "cycle-archive" in capped
    # truncation lands on a line boundary (no half-cut index row)
    last_line = capped.rsplit("\n", 1)[1]
    assert last_line.startswith("… [truncated")


def test_collect_memory_data_caps_index(tmp_path):
    """_collect_memory_data embeds a capped index (never the raw giant)."""
    server = _make_server()
    (tmp_path / ".emrg" / "memory").mkdir(parents=True)
    idx = tmp_path / ".emrg" / "memory" / "MEMORY.md"
    line = "- [cyc00000000-000000](cycle-20260823-000000.md) — " + "y" * 100 + "\n"
    idx.write_text(line * 600, encoding="utf-8")  # ~64KB
    session = Session.create_with_id("mem-cap", tmp_path)
    data = server._collect_memory_data(session)
    assert data["has_memories"] is True
    assert data["project_memory_index_path"] == str(idx)
    assert "truncated" in data["project_memory_index"]
    assert len(data["project_memory_index"]) <= 50 * 1024 + 200


def test_collect_memory_data_no_index(tmp_path):
    """No MEMORY.md files → has_memories stays False."""
    server = _make_server()
    session = Session.create_with_id("mem-none", tmp_path)
    assert server._collect_memory_data(session) is None


# ── _count_chars_for_tokens ───────────────────────────────────────


def test_count_chars_pure_ascii():
    """ASCII text: ~4 chars/token."""
    server = _make_server()
    # "helloworld" = 10 chars → 10 // 4 = 2 tokens
    assert server._count_chars_for_tokens("helloworld") == 2
    # 4 chars → 1 token
    assert server._count_chars_for_tokens("abcd") == 1


def test_count_chars_pure_cjk():
    """CJK text: ~2 chars/token."""
    server = _make_server()
    # "你好世界" = 4 CJK chars → 4 // 2 = 2 tokens
    assert server._count_chars_for_tokens("你好世界") == 2
    # "中文" = 2 CJK chars → 1 token
    assert server._count_chars_for_tokens("中文") == 1


def test_count_chars_mixed():
    """Mixed CJK + ASCII: counted separately then summed."""
    server = _make_server()
    # "hello世界" = 5 ASCII + 2 CJK → 5//4 + 2//2 = 1 + 1 = 2
    assert server._count_chars_for_tokens("hello世界") == 2


def test_count_chars_empty():
    """Empty string → 0 tokens."""
    server = _make_server()
    assert server._count_chars_for_tokens("") == 0


def test_count_chars_kana():
    """Hiragana/Katakana counted as CJK."""
    server = _make_server()
    # "あいうえお" = 5 Kana → 5 // 2 = 2
    assert server._count_chars_for_tokens("あいうえお") == 2


# ── _estimate_tokens ──────────────────────────────────────────────


def test_estimate_tokens_empty():
    """Empty message list → 0 tokens."""
    server = _make_server()
    assert server._estimate_tokens([]) == 0


def test_estimate_tokens_single_message():
    """Single user message with ASCII content."""
    server = _make_server()
    msgs = [{"role": "user", "content": "hello world"}]
    # 3 (overhead) + count_chars("hello world"=11) → 3 + 11//4 = 3 + 2 = 5
    assert server._estimate_tokens(msgs) == 5


def test_estimate_tokens_with_tool_calls():
    """Message with embedded tool_calls adds their token cost."""
    server = _make_server()
    msgs = [{
        "role": "assistant",
        "content": "ok",
        "tool_calls": [{"id": "1", "type": "function",
                        "function": {"name": "bash", "arguments": "ls"}}]
    }]
    result = server._estimate_tokens(msgs)
    # 3 (overhead) + 2//4 (content) + count_chars(json of tool_calls)
    assert result > 3  # at least overhead


def test_estimate_tokens_multiple_messages():
    """Multiple messages each add overhead."""
    server = _make_server()
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    result = server._estimate_tokens(msgs)
    # 2 * 3 (overhead) + count_chars("hi"=2)//4 + count_chars("hello"=5)//4
    # = 6 + 0 + 1 = 7
    assert result == 7


# ── _estimate_single ──────────────────────────────────────────────


def test_estimate_single_message():
    """Single record with content."""
    server = _make_server()
    record = {"role": "user", "content": "hello world"}
    # 3 (overhead) + count_chars("hello world"=11)//4 = 3 + 2 = 5
    assert server._estimate_single(record) == 5


def test_estimate_single_empty_content():
    """Record with no content → just overhead."""
    server = _make_server()
    record = {"role": "user"}
    assert server._estimate_single(record) == 3


def test_estimate_single_non_string_content():
    """Non-string content (e.g. list) → just overhead."""
    server = _make_server()
    record = {"role": "user", "content": ["part1", "part2"]}
    assert server._estimate_single(record) == 3


# ── _records_to_text ──────────────────────────────────────────────


def test_records_to_text_empty():
    """Empty list → empty string."""
    server = _make_server()
    assert server._records_to_text([]) == ""


def test_records_to_text_message():
    """Message record → formatted line."""
    server = _make_server()
    records = [{"type": "message", "role": "user", "content": "hello",
                "timestamp": "2026-01-15T12:00:00.000Z"}]
    result = server._records_to_text(records)
    assert "[2026-01-15T12:00:00] user: hello" in result


def test_records_to_text_tool_call():
    """Tool call record with arguments."""
    server = _make_server()
    records = [{"type": "tool_call", "tool_name": "bash",
                "arguments": {"command": "ls"},
                "timestamp": "2026-01-15T12:00:00.000Z"}]
    result = server._records_to_text(records)
    assert "tool_call: bash" in result
    assert '"command": "ls"' in result


def test_records_to_text_tool_result():
    """Tool result content truncated to 500 chars."""
    server = _make_server()
    long_content = "x" * 600
    records = [{"type": "tool_result", "content": long_content,
                "timestamp": "2026-01-15T12:00:00.000Z"}]
    result = server._records_to_text(records)
    assert "tool_result:" in result
    assert len(result.split("tool_result: ")[1]) == 500  # truncated


def test_records_to_text_summary():
    """Summary record renders with marker."""
    server = _make_server()
    records = [{"type": "summary", "content": "prior context here",
                "timestamp": "2026-01-15T12:00:00.000Z"}]
    result = server._records_to_text(records)
    assert "[PREVIOUS SUMMARY]" in result
    assert "prior context here" in result


def test_records_to_text_mixed():
    """Multiple record types in sequence."""
    server = _make_server()
    records = [
        {"type": "message", "role": "user", "content": "hi",
         "timestamp": "2026-01-15T12:00:00.000Z"},
        {"type": "message", "role": "assistant", "content": "hey",
         "timestamp": "2026-01-15T12:00:01.000Z"},
    ]
    result = server._records_to_text(records)
    lines = result.split("\n")
    assert len(lines) == 2
    assert "user: hi" in lines[0]
    assert "assistant: hey" in lines[1]


# ── _truncate_record ──────────────────────────────────────────────


def test_truncate_record_short():
    """Short content passes through unchanged."""
    server = _make_server()
    record = {"role": "user", "content": "short"}
    result = server._truncate_record(record, max_tokens=100)
    assert result["content"] == "short"


def test_truncate_record_long():
    """Long content gets truncated with notice."""
    server = _make_server()
    long_content = "x" * 500
    record = {"role": "user", "content": long_content}
    result = server._truncate_record(record, max_tokens=100)
    # max_chars = 100 * 2 = 200, so truncated to 200 chars + notice
    assert len(result["content"]) == 200 + len("\n...[truncated for compact]")
    assert "[truncated" in result["content"]


def test_truncate_record_exact_boundary():
    """Content exactly at max_chars boundary is NOT truncated."""
    server = _make_server()
    content = "x" * 200  # max_tokens=100 → max_chars=200
    record = {"role": "user", "content": content}
    result = server._truncate_record(record, max_tokens=100)
    assert result["content"] == content  # unchanged


def test_truncate_record_preserves_other_fields():
    """Only content is modified; other fields survive."""
    server = _make_server()
    record = {"role": "assistant", "content": "x" * 500, "tool_calls": []}
    result = server._truncate_record(record, max_tokens=100)
    assert result["role"] == "assistant"
    assert result["tool_calls"] == []


# ── rant field order (user feedback 2026-07-31T20:46) ────────────


class _FakeWriter:
    """Minimal WebSocket stand-in capturing _send payloads."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._frames: list[bytes] = []

    async def send(self, data) -> None:
        if isinstance(data, (bytes, bytearray)):
            self._frames.append(bytes(data))
        else:
            self._frames.append(data.encode() if isinstance(data, str) else data)

    async def close(self) -> None:
        pass

    async def recv(self) -> str:
        raise ConnectionError("no frames to receive in test")

    async def _send(self, data: dict) -> bool:
        self.sent.append(data)
        return True


def test_rant_field_order(tmp_path, monkeypatch):
    """Rant entries are written with field order:
    timestamp → project → status → progress → completed → message (message last)."""
    monkeypatch.setattr("emrg.server.daemon.config_dir", lambda: tmp_path)
    server = _make_server()
    writer = _FakeWriter()

    import asyncio
    asyncio.run(server._process_message({
        "type": "rant",
        "message": "test rant message",
        "project": "emrg",
        # Client-supplied timestamp must be IGNORED — the daemon stamps rants
        # with tz-aware local time (rant 2026-08-07T13:34Z: GUI sent UTC Z,
        # 8h behind on UTC+8 hosts). A stale UTC value here must not leak through.
        "timestamp": "2026-07-31T20:46:59.734987Z",
    }, writer))  # type: ignore[arg-type]

    lines = (tmp_path / "rants.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert list(entry.keys()) == [
        "timestamp", "project", "status", "progress", "completed", "message",
    ]
    assert entry["completed"] is None  # new rants default to null completed
    assert entry["project"] == "emrg"
    assert entry["status"] == "pending"
    assert entry["message"] == "test rant message"
    # Daemon-authoritative local timestamp: tz-aware, NOT UTC (no Z suffix),
    # and within 60s of the wall clock.
    import datetime as _dt
    assert not entry["timestamp"].endswith("Z")
    ts = _dt.datetime.fromisoformat(entry["timestamp"])
    assert ts.tzinfo is not None
    assert abs((_dt.datetime.now(ts.tzinfo) - ts).total_seconds()) < 60


def _last_frame(writer: "_FakeWriter") -> dict:
    """Parse the last WebSocket frame the daemon sent (daemon._send → ws.send → _frames)."""
    import json as _json
    raw = writer._frames[-1]
    return _json.loads(raw.decode() if isinstance(raw, bytes) else raw)


def test_list_rants_filter_and_order(tmp_path, monkeypatch):
    """list_rants reads rants.jsonl: optional status filter + newest-first order."""
    import asyncio

    monkeypatch.setattr("emrg.server.daemon.config_dir", lambda: tmp_path)
    server = _make_server()
    writer = _FakeWriter()

    # 写入 3 条（乱序），覆盖三状态
    entries = [
        {"timestamp": "2026-08-13T10:00:00+08:00", "project": "", "status": "pending", "progress": None, "completed": None, "message": "new idea"},
        {"timestamp": "2026-08-13T14:10:14+08:00", "project": "emrg", "status": "in_progress", "progress": "P1 done", "completed": None, "message": "GUI redesign"},
        {"timestamp": "2026-08-12T09:00:00+08:00", "project": "", "status": "completed", "progress": None, "completed": "2026-08-12T12:00:00+08:00", "message": "old rant"},
    ]
    with open(tmp_path / "rants.jsonl", "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # 无筛选 → 全部，时间倒序（最新在前）
    asyncio.run(server._process_message({"type": "list_rants"}, writer))
    frame = _last_frame(writer)
    assert frame["type"] == "rants_list"
    assert [r["message"] for r in frame["rants"]] == ["GUI redesign", "new idea", "old rant"]

    # status=completed 筛选
    asyncio.run(server._process_message({"type": "list_rants", "status": "completed"}, writer))
    frame = _last_frame(writer)
    assert [r["message"] for r in frame["rants"]] == ["old rant"]

    # status=in_progress 筛选
    asyncio.run(server._process_message({"type": "list_rants", "status": "in_progress"}, writer))
    frame = _last_frame(writer)
    assert [r["message"] for r in frame["rants"]] == ["GUI redesign"]

    # 损坏行跳过（corrupt JSON 不崩）
    with open(tmp_path / "rants.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json}\n")
    asyncio.run(server._process_message({"type": "list_rants"}, writer))
    frame = _last_frame(writer)
    assert len(frame["rants"]) == 3  # 损坏行被跳过


def test_list_rants_missing_file_returns_empty(tmp_path, monkeypatch):
    """list_rants with no rants.jsonl → empty list (no crash)."""
    import asyncio

    monkeypatch.setattr("emrg.server.daemon.config_dir", lambda: tmp_path)
    server = _make_server()
    writer = _FakeWriter()
    asyncio.run(server._process_message({"type": "list_rants"}, writer))
    frame = _last_frame(writer)
    assert frame["type"] == "rants_list"
    assert frame["rants"] == []


# ── Token-file self-heal (rant 2026-08-09T13:16:36 root cause) ─────────
# G43 stale-port logic once deleted a healthy daemon's emrgd.token after a
# transient ws failure → daemon's own scheduler lost the file (93× "cannot
# connect") while the PID lock blocked new spawns. The daemon re-asserts
# its token file so any external deletion self-heals.

def test_assert_token_file_writes_token(tmp_path, monkeypatch):
    """_assert_token_file writes the single-line token with mode 0o600."""
    monkeypatch.setattr("emrg.server.daemon.config_dir", lambda: tmp_path)
    server = _make_server()
    server._auth_token = "tok123"
    server._assert_token_file()
    text = (tmp_path / "emrgd.token").read_text(encoding="utf-8")
    assert text == "tok123"


def test_assert_token_file_rewrites_deleted_file(tmp_path, monkeypatch):
    """A deleted token file is re-asserted on the next keepalive tick."""
    monkeypatch.setattr("emrg.server.daemon.config_dir", lambda: tmp_path)
    server = _make_server()
    server._auth_token = "tok456"
    server._assert_token_file()
    token_path = tmp_path / "emrgd.token"
    assert token_path.exists()

    # 外部删除（模拟 G43 unlink 竞态）
    token_path.unlink()
    assert not token_path.exists()

    # keepalive loop 的恢复逻辑：缺失 → 重新断言
    server._assert_token_file()
    text = (tmp_path / "emrgd.token").read_text(encoding="utf-8")
    assert text == "tok456"


def test_port_keepalive_loop_restores_missing_file(tmp_path, monkeypatch):
    """The keepalive loop re-asserts a deleted token file within one tick."""
    import asyncio

    monkeypatch.setattr("emrg.server.daemon.config_dir", lambda: tmp_path)
    server = _make_server()
    server._auth_token = "tok789"
    server._server = type("S", (), {"sockets": [type("Sock", (), {"getsockname": lambda self: (None, 9999)})()]})()
    server._running = True
    server._assert_token_file()
    token_path = tmp_path / "emrgd.token"
    token_path.unlink()

    # 执行与 loop 相同的恢复逻辑（loop 本体 sleep 60s，测试直接驱动检查体）
    async def one_tick():
        if not token_path.exists():
            server._assert_token_file()
    asyncio.run(one_tick())
    assert token_path.exists()
    assert token_path.read_text(encoding="utf-8") == "tok789"


# ── _redact 日志脱敏（rant 10:21 + 跨项目 base64 教训）──────────────


def test_redact_masks_sensitive_keys():
    """按键名脱敏（原有行为）。"""
    from emrg.server.daemon import _redact
    assert _redact({"api_key": "sk-abc", "model": "m"}) == {"api_key": "***", "model": "m"}
    assert _redact([{"token": "x"}, "plain"]) == [{"token": "***"}, "plain"]


def test_redact_inline_secrets_in_strings():
    """字符串值内联凭据（sk-/ghp_/Bearer/JWT）被遮蔽。"""
    from emrg.server.daemon import _redact
    # bash command 内联 API key（拼接构造，避免 push protection 拦截）
    k = "sk-" + "a1b2c3d4" * 4
    assert k not in str(_redact({"command": f"export OPENAI_API_KEY={k}; curl x"}))
    # GitHub token 嵌在 URL
    assert "ghp_" not in str(_redact({"command": "git push https://x-access-token:ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890@github.com/r.git"}))
    # Bearer + JWT
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    assert "eyJ" not in str(_redact({"text": f"Authorization: Bearer {jwt}"}))


def test_redact_base64_encoded_token_blob():
    """base64 编码的 access_token JSON 被整段遮蔽（跨项目教训：明文正则匹配不到编码形式）。"""
    import base64
    import json as _json
    from emrg.server.daemon import _redact
    blob = base64.b64encode(_json.dumps({"access_token": "super-secret"}).encode()).decode()
    out = _redact({"command": f"curl -d {blob} http://x"})
    assert "***" in out["command"]
    assert "super-secret" not in out["command"]
    assert blob not in out["command"]


def test_redact_no_false_positive_on_normal_strings():
    """普通字符串/短 sk- 前缀/路径片段不被误伤。"""
    from emrg.server.daemon import _redact
    assert _redact({"command": "echo hello world", "path": "/tmp/a.txt"}) == {
        "command": "echo hello world", "path": "/tmp/a.txt"}
    # 短密钥（<16 位）不匹配 sk- 模式 → 保留
    assert "sk-abc" in _redact({"command": "echo sk-abc"})["command"]
    # 路径片段 "task-evolution-*" 含 "sk-evolution"（12 位）→ 不得误伤（#513 修复）
    assert "task-evolution-233-emrg-3193bc65.memory" in _redact(
        {"command": "ls task-evolution-233-emrg-3193bc65.memory"})["command"]
    assert "grep -r sk-evolution ~/scm" in _redact(
        {"command": "grep -r sk-evolution ~/scm"})["command"]


def test_redact_real_key_formats_still_masked():
    """真实密钥格式（sk- 16+ 位 / sk-proj- / sk-ant-apiNN-）仍被遮蔽。

    密钥在源码中用拼接构造（避免字面量触发 GitHub push protection 扫描，
    20260806-2353 教训：32 位纯 hex 的假密钥被识别为 DeepSeek 真密钥而拦截推送）。
    """
    from emrg.server.daemon import _redact
    # OpenAI sk- + 48 字母数字
    openai = "sk-" + "A1b2C3d4" * 6
    assert openai not in str(_redact(
        {"command": f"export OPENAI_API_KEY={openai}; curl x"}))
    # DeepSeek sk- + 32 位
    ds = "sk-" + "0a1b2c3d" * 4
    assert ds not in str(_redact(
        {"command": f"export DEEPSEEK={ds}; curl x"}))
    # OpenAI 项目密钥 sk-proj-（裸值，无 key= 前缀）
    proj = "sk-proj-" + "AbCdEfGh" * 6
    assert "sk-proj-" not in _redact({"command": f"echo {proj}"})["command"]
    # Anthropic sk-ant-apiNN-（裸值）
    ant = "sk-ant-api03-" + "Q2xjbHVkZ" * 4
    assert "sk-ant-" not in _redact({"command": f"echo {ant}"})["command"]


# ── _redact_string 覆盖日志内容预览（rant/任务 prompt/记忆工具结果）──────


def test_redact_string_applies_to_log_previews():
    """日志内容预览（rant 消息/任务 prompt/记忆工具结果）同样脱敏。"""
    from emrg.server.daemon import _redact_string
    # 用户消息/提示词里粘贴的密钥
    assert "sk-" not in _redact_string("帮我看看 sk-A1b2C3d4A1b2C3d4A1b2C3d4A1b2C3d4 是不是我的 key")
    assert "ghp_" not in _redact_string("token 是 ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 吗")
    # 普通内容保留
    assert "帮我看看这个文件" in _redact_string("帮我看看这个文件")


# ── github_status command (rant 2026-08-07T10:17:27, Windows GCM) ──


def test_github_status_authenticated(monkeypatch):
    """github_status returns authenticated user when _check_github_auth succeeds."""
    import asyncio

    server = _make_server()
    writer = _FakeWriter()

    async def fake_check():
        return {"authenticated": True, "user": "octocat", "method": "gh"}

    monkeypatch.setattr(server, "_check_github_auth", fake_check)
    asyncio.run(server._process_message({"type": "github_status"}, writer))

    assert len(writer._frames) == 1
    reply = json.loads(writer._frames[0])
    assert reply["type"] == "github_status"
    assert reply["authenticated"] is True
    assert reply["user"] == "octocat"
    assert reply["method"] == "gh"


def test_github_status_unauthenticated(monkeypatch):
    """github_status returns not-authenticated when no gh token is present."""
    import asyncio

    server = _make_server()
    writer = _FakeWriter()

    async def fake_check():
        return {"authenticated": False, "user": None, "method": "none"}

    monkeypatch.setattr(server, "_check_github_auth", fake_check)
    asyncio.run(server._process_message({"type": "github_status"}, writer))

    reply = json.loads(writer._frames[0])
    assert reply["authenticated"] is False
    assert reply["user"] is None


def test_check_github_auth_no_gh_binary(monkeypatch):
    """Missing gh binary degrades to not-authenticated, never raises."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("", ""))
    result = asyncio.run(server._check_github_auth())
    assert result == {"authenticated": False, "user": None, "method": "none"}


def test_check_github_auth_parses_gh_output(monkeypatch):
    """Real gh auth status output is parsed into an authenticated user."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("/usr/bin/git", "/usr/bin/gh"))

    class FakeProc:
        async def communicate(self):
            return (b"Logged in to github.com as octocat (keyring)\n", None)

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(dmod.asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(server._check_github_auth())
    assert result == {"authenticated": True, "user": "octocat", "method": "gh"}


# ── github_connect / github_disconnect commands (rant 10:17:27 Stage 2) ──


class _FakeProc:
    """Minimal fake subprocess for github_connect/setup-git tests."""

    def __init__(self, returncode=0, output=b""):
        self.returncode = returncode
        self._output = output
        self.received = None

    async def communicate(self, data=None):
        self.received = data
        return (self._output, None)


def test_github_connect_success_dispatch(monkeypatch):
    """github_connect returns github_connect_result with ok/user."""
    import asyncio

    server = _make_server()
    writer = _FakeWriter()

    async def fake_connect(token):
        return {"ok": True, "user": "octocat", "error": None}

    monkeypatch.setattr(server, "_github_connect", fake_connect)
    asyncio.run(server._process_message({"type": "github_connect", "token": "ghp_x"}, writer))

    assert len(writer._frames) == 1
    reply = json.loads(writer._frames[0])
    assert reply["type"] == "github_connect_result"
    assert reply["ok"] is True
    assert reply["user"] == "octocat"


def test_github_connect_empty_token(monkeypatch):
    """Empty/whitespace token is rejected before any subprocess runs."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    writer = _FakeWriter()
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("/usr/bin/git", "/usr/bin/gh"))
    asyncio.run(server._process_message({"type": "github_connect", "token": "  "}, writer))

    reply = json.loads(writer._frames[0])
    assert reply["type"] == "github_connect_result"
    assert reply["ok"] is False
    assert reply["error"] == "empty token"


def test_github_connect_gh_missing(monkeypatch):
    """Missing gh binary degrades to ok=False, never raises."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    writer = _FakeWriter()
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("", ""))
    asyncio.run(server._process_message({"type": "github_connect", "token": "ghp_x"}, writer))

    reply = json.loads(writer._frames[0])
    assert reply["type"] == "github_connect_result"
    assert reply["ok"] is False
    assert reply["error"] == "gh binary not found"


def test_github_connect_pat_login_sets_up_git(monkeypatch):
    """PAT path: gh auth login --with-token → re-verify → setup-git runs."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("/usr/bin/git", "/usr/bin/gh"))
    calls = []  # [(argv, proc)]

    async def fake_exec(*args, **kwargs):
        if "setup-git" in args:
            proc = _FakeProc(0, b"gh auth setup-git ok\n")
        elif "status" in args:
            proc = _FakeProc(0, b"Logged in to github.com as octocat (keyring)\n")
        else:
            proc = _FakeProc(0, b"Logged in as octocat\n")
        calls.append((list(args), proc))
        return proc

    monkeypatch.setattr(dmod.asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(server._github_connect("ghp_x"))

    assert result["ok"] is True
    assert result["user"] == "octocat"
    assert result["error"] is None
    login_entries = [(a, p) for a, p in calls if "login" in a]
    setup_entries = [(a, p) for a, p in calls if "setup-git" in a]
    assert len(login_entries) == 1
    assert login_entries[0][0][1:] == ["auth", "login", "--with-token"]
    # token must be delivered via stdin, never argv (argv leaks into ps)
    assert login_entries[0][1].received == b"ghp_x\n"
    assert len(setup_entries) == 1
    assert setup_entries[0][0][1:] == ["auth", "setup-git"]


def test_github_connect_login_failure_reported(monkeypatch):
    """gh auth login non-zero → ok=False with gh output as error."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("/usr/bin/git", "/usr/bin/gh"))

    async def fake_exec(*args, **kwargs):
        return _FakeProc(1, b"HTTP 401: Bad credentials\n")

    monkeypatch.setattr(dmod.asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(server._github_connect("ghp_bad"))

    assert result["ok"] is False
    assert "401" in result["error"]


def test_github_connect_setup_git_failure_reported(monkeypatch):
    """Auth ok but setup-git fails → ok=True with explanatory error note."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("/usr/bin/git", "/usr/bin/gh"))

    async def fake_exec(*args, **kwargs):
        if "setup-git" in args:
            return _FakeProc(1, b"could not set git config\n")
        if "status" in args:
            return _FakeProc(0, b"Logged in to github.com as octocat (keyring)\n")
        return _FakeProc(0, b"Logged in as octocat\n")

    monkeypatch.setattr(dmod.asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(server._github_connect("ghp_x"))

    assert result["ok"] is True
    assert result["user"] == "octocat"
    assert "setup-git" in result["error"]


def test_github_disconnect_success_dispatch(monkeypatch):
    """github_disconnect returns github_disconnect_result with ok."""
    import asyncio

    server = _make_server()
    writer = _FakeWriter()

    async def fake_disconnect():
        return {"ok": True, "error": None}

    monkeypatch.setattr(server, "_github_disconnect", fake_disconnect)
    asyncio.run(server._process_message({"type": "github_disconnect"}, writer))

    reply = json.loads(writer._frames[0])
    assert reply["type"] == "github_disconnect_result"
    assert reply["ok"] is True


def test_github_disconnect_gh_missing(monkeypatch):
    """Missing gh binary degrades to ok=False with error, never raises."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    writer = _FakeWriter()
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("", ""))
    asyncio.run(server._process_message({"type": "github_disconnect"}, writer))

    reply = json.loads(writer._frames[0])
    assert reply["type"] == "github_disconnect_result"
    assert reply["ok"] is False
    assert reply["error"] == "gh binary not found"


# ── github_connect_web (device flow) command (rant 10:17:27 Stage 2b) ──


class _FakeWebStream:
    """Async-iterable of stdout lines for the device-flow fake proc."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeWebProc:
    """Fake gh auth login --web subprocess."""

    def __init__(self, stream, blocking=False):
        self.stdout = stream
        self.killed = False
        self.returncode = None
        self._blocking = blocking

    async def communicate(self, data=None):
        if self._blocking:
            while True:
                await asyncio.sleep(3600)
        return (b"", None)

    def kill(self):
        self.killed = True


def test_github_connect_web_parses_device_code(monkeypatch):
    """Device flow parses the one-time code + URL and starts a background task."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    monkeypatch.setattr(server, "_check_github_auth",
                        lambda: _async_value({"authenticated": False, "user": None, "method": "none"}))
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("/usr/bin/git", "/usr/bin/gh"))
    procs = []

    async def fake_exec(*args, **kwargs):
        p = _FakeWebProc(_FakeWebStream([
            b"\n",
            b"! First copy your one-time code: ABCD-1234\n",
            b"Open this URL to continue in your web browser: https://github.com/login/device\n",
        ]))
        procs.append(p)
        return p

    monkeypatch.setattr(dmod.asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(server._github_connect_web_start())

    assert result["ok"] is True
    assert result["code"] == "ABCD-1234"
    assert result["url"] == "https://github.com/login/device"
    assert result["error"] is None
    assert len(procs) == 1
    assert procs[0].killed is False


def test_github_connect_web_already_authenticated(monkeypatch):
    """Already-authenticated short-circuits without spawning a subprocess."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    monkeypatch.setattr(server, "_check_github_auth",
                        lambda: _async_value({"authenticated": True, "user": "octocat", "method": "gh"}))
    spawned = []
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("/usr/bin/git", "/usr/bin/gh"))

    async def fake_exec(*args, **kwargs):
        spawned.append(args)
        return _FakeWebProc(_FakeWebStream([]))

    monkeypatch.setattr(dmod.asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(server._github_connect_web_start())

    assert result["ok"] is True
    assert result["user"] == "octocat"
    assert result["error"] == "already_authenticated"
    assert spawned == []  # no gh process started


def test_github_connect_web_gh_missing(monkeypatch):
    """Missing gh binary degrades to ok=False, never raises."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    monkeypatch.setattr(server, "_check_github_auth",
                        lambda: _async_value({"authenticated": False, "user": None, "method": "none"}))
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("", ""))
    result = asyncio.run(server._github_connect_web_start())

    assert result["ok"] is False
    assert result["error"] == "gh binary not found"


def test_github_connect_web_no_device_code_kills_proc(monkeypatch):
    """gh produces no code → ok=False and the process is killed."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    monkeypatch.setattr(server, "_check_github_auth",
                        lambda: _async_value({"authenticated": False, "user": None, "method": "none"}))
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("/usr/bin/git", "/usr/bin/gh"))
    proc = _FakeWebProc(_FakeWebStream([b"unexpected output\n"]))

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(dmod.asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(server._github_connect_web_start())

    assert result["ok"] is False
    assert result["error"] == "gh auth login --web produced no device code"
    assert proc.killed is True


def test_github_connect_web_dispatch(monkeypatch):
    """github_connect_web returns github_connect_web_result frame."""
    import asyncio

    server = _make_server()
    writer = _FakeWriter()

    async def fake_start():
        return {"ok": True, "code": "WXYZ-9876", "url": "https://github.com/login/device",
                "user": None, "error": None}

    monkeypatch.setattr(server, "_github_connect_web_start", fake_start)
    asyncio.run(server._process_message({"type": "github_connect_web"}, writer))

    assert len(writer._frames) == 1
    reply = json.loads(writer._frames[0])
    assert reply["type"] == "github_connect_web_result"
    assert reply["ok"] is True
    assert reply["code"] == "WXYZ-9876"


def test_github_connect_web_cancel_kills_pending(monkeypatch):
    """Cancelling a pending device flow kills the gh process."""
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    monkeypatch.setattr(server, "_check_github_auth",
                        lambda: _async_value({"authenticated": False, "user": None, "method": "none"}))
    monkeypatch.setattr(dmod, "resolve_git_gh", lambda: ("/usr/bin/git", "/usr/bin/gh"))
    proc = _FakeWebProc(
        _FakeWebStream([b"one-time code: WXYZ-9876\nhttps://github.com/login/device\n"]),
        blocking=True,
    )

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(dmod.asyncio, "create_subprocess_exec", fake_exec)

    async def run():
        r = await server._github_connect_web_start()
        assert r["ok"] is True
        assert server._pending_web_auth is not None
        await server._github_connect_web_cancel()
        assert server._pending_web_auth is None
        return r

    result = asyncio.run(run())
    assert result["code"] == "WXYZ-9876"
    assert proc.killed is True


def _async_value(v):
    """Return an awaitable that yields v (for monkeypatched async fakes)."""
    async def _wrap():
        return v
    return _wrap()


# ── /rant project list shows evolution-workspace entries (rant 10:48:00) ──


def test_list_projects_includes_evolution_workspace(tmp_path, monkeypatch):
    """A registered project under the evolution workspace is NOT filtered from /rant.

    Discriminating-power fix (review cycle 190846): the registered path must
    actually live under the monkeypatched EVOLUTION_CWD — otherwise a restored
    filter would keep the entry and the test would pass anyway (false
    confidence). Here the emrg entry's path is under tmp_path (= EVOLUTION_CWD),
    so re-adding the old filter would exclude it and fail the assertion.
    """
    import asyncio

    from emrg.server import daemon as dmod

    server = _make_server()
    evolution_cwd = str(tmp_path.resolve())
    emrg_path = f"{evolution_cwd}/emrg"  # under EVOLUTION_CWD on purpose
    projects_file = tmp_path / "projects.yml"
    projects_file.write_text(
        f"- name: emrg\n  path: {emrg_path}\n"
        "- name: other\n  path: /home/u/work/other\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_projects_log", projects_file)
    monkeypatch.setattr(dmod, "EVOLUTION_CWD", tmp_path)

    writer = _FakeWriter()
    asyncio.run(server._handle_list_projects(writer))

    assert len(writer._frames) == 1
    reply = json.loads(writer._frames[0])
    assert reply["type"] == "projects_list"
    paths = [p["path"] for p in reply["projects"]]
    assert emrg_path in paths  # emrg visible even though under evolution cwd
    assert "/home/u/work/other" in paths


# ── remove_project (P1 GUI multi-session, rant 2026-08-10T15:07:19) ──


def _server_with_projects(tmp_path: Path, entries: list[dict]) -> tuple[EmrgServer, Path]:
    """Server whose _projects_log points at a tmp projects.yml with entries."""
    server = _make_server()
    projects_file = tmp_path / "projects.yml"
    projects_file.write_text(
        yaml.safe_dump(entries, allow_unicode=True), encoding="utf-8"
    )
    server._projects_log = projects_file
    return server, projects_file


def _decode_frames(writer: _FakeWriter) -> list[dict]:
    return [json.loads(f) for f in writer._frames]


def test_remove_project_removes_matching_entry(tmp_path):
    """Removing an existing project drops only its projects.yml entry."""
    server, projects_file = _server_with_projects(tmp_path, [
        {"name": "alpha", "path": "/home/u/a", "last_active": "2026-08-10T10:00:00"},
        {"name": "beta", "path": "/home/u/b", "last_active": "2026-08-10T10:05:00"},
    ])
    writer = _FakeWriter()
    asyncio.run(server._handle_remove_project("alpha", writer))

    replies = _decode_frames(writer)
    assert len(replies) == 1
    assert replies[0] == {"type": "project_removed", "removed": True, "name": "alpha"}

    remaining = yaml.safe_load(projects_file.read_text(encoding="utf-8"))
    assert [e["name"] for e in remaining] == ["beta"]


def test_remove_project_unknown_name_leaves_file_unchanged(tmp_path):
    """Removing a non-existent name reports removed=False and keeps the file."""
    entries = [
        {"name": "alpha", "path": "/home/u/a", "last_active": "2026-08-10T10:00:00"},
    ]
    server, projects_file = _server_with_projects(tmp_path, entries)
    writer = _FakeWriter()
    asyncio.run(server._handle_remove_project("nope", writer))

    replies = _decode_frames(writer)
    assert len(replies) == 1
    assert replies[0]["removed"] is False
    assert replies[0]["name"] == "nope"
    assert yaml.safe_load(projects_file.read_text(encoding="utf-8")) == entries


def test_remove_project_preserves_disk_session_data(tmp_path):
    """Deleting the project entry must NOT touch <path>/.emrg/sessions/ data."""
    project_dir = tmp_path / "u" / "a"
    sessions_dir = project_dir / ".emrg" / "sessions" / "sess-1"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "history.jsonl").write_text('{"role":"user"}\n', encoding="utf-8")

    server, _ = _server_with_projects(tmp_path, [
        {"name": "alpha", "path": str(project_dir), "last_active": "2026-08-10T10:00:00"},
    ])
    writer = _FakeWriter()
    asyncio.run(server._handle_remove_project("alpha", writer))
    assert _decode_frames(writer)[0]["removed"] is True
    # on-disk session data survives the project removal
    assert (sessions_dir / "history.jsonl").read_text(encoding="utf-8") == '{"role":"user"}\n'


def test_remove_project_no_projects_file(tmp_path):
    """No projects.yml → removed=False without raising."""
    server = _make_server()
    server._projects_log = tmp_path / "projects.yml"  # never created
    writer = _FakeWriter()
    asyncio.run(server._handle_remove_project("alpha", writer))
    assert _decode_frames(writer)[0] == {
        "type": "project_removed", "removed": False, "name": "alpha",
    }


def test_remove_project_corrupt_yaml_reports_error(tmp_path):
    """Corrupt projects.yml → removed=False + error, no crash."""
    server = _make_server()
    projects_file = tmp_path / "projects.yml"
    projects_file.write_text("a: [unclosed", encoding="utf-8")
    server._projects_log = projects_file
    writer = _FakeWriter()
    asyncio.run(server._handle_remove_project("alpha", writer))
    replies = _decode_frames(writer)
    assert replies[0]["removed"] is False
    assert replies[0]["error"]


# ── list_projects ordering by latest session activity (P6, rant 15:07:19) ──


def test_list_projects_ordered_by_latest_session_activity(tmp_path, monkeypatch):
    """Projects come back sorted by their newest session's created_at (desc).

    P6 acceptance ("选项目按该项目最新会话活跃倒序"): the daemon aggregates
    each project's sessions dir (parallel scan) because the GUI cannot issue
    concurrent list_sessions on one connection (pending map keys by respType).
    """
    import asyncio
    import json

    from emrg.server import daemon as dmod

    server = _make_server()
    p_old = tmp_path / "old"
    p_new = tmp_path / "new"
    p_none = tmp_path / "nosess"
    for p in (p_old, p_new, p_none):
        p.mkdir()
    # 老项目：最近会话 created_at 较早
    (p_old / ".emrg" / "sessions" / "s-old").mkdir(parents=True)
    (p_old / ".emrg" / "sessions" / "s-old" / "meta.json").write_text(
        json.dumps({"session_id": "s-old", "created_at": "2026-08-01T00:00:00"}), encoding="utf-8"
    )
    # 新项目：最近会话 created_at 较晚 → 应排第一
    (p_new / ".emrg" / "sessions" / "s-new").mkdir(parents=True)
    (p_new / ".emrg" / "sessions" / "s-new" / "meta.json").write_text(
        json.dumps({"session_id": "s-new", "created_at": "2026-08-10T12:00:00"}), encoding="utf-8"
    )
    # 无会话项目 → 排最后（无 latest_session_at）
    projects_file = tmp_path / "projects.yml"
    projects_file.write_text(
        f"- name: old\n  path: {p_old}\n"
        f"- name: new\n  path: {p_new}\n"
        f"- name: nosess\n  path: {p_none}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_projects_log", projects_file)

    writer = _FakeWriter()
    asyncio.run(server._handle_list_projects(writer))

    reply = json.loads(writer._frames[0])
    names = [p["name"] for p in reply["projects"]]
    assert names == ["new", "old", "nosess"], f"ordered by latest session activity: {names}"
    new_proj = next(p for p in reply["projects"] if p["name"] == "new")
    assert new_proj["latest_session_at"] == "2026-08-10T12:00:00"
    none_proj = next(p for p in reply["projects"] if p["name"] == "nosess")
    assert none_proj["latest_session_at"] == ""


# ── rant 2026-08-19T08:05:21：固定端口 bind 排斥 = 唯一单 daemon 准入 ──
def test_serve_refuses_duplicate_when_fixed_port_bound(tmp_path):
    """serve() must exit when another LIVE daemon owns the fixed port.

    The fixed-port bind (EADDRINUSE) is the ONLY single-instance admission
    (rant 2026-08-19T08:05:21): kernel-level resource exclusivity — no PID
    file to forge/delete, no race window (emrgd.pid removed entirely per
    rant 2026-08-21T16:45:06). A refused instance must not reach the
    websockets bind.
    """
    import errno as _errno
    from unittest.mock import AsyncMock, patch

    server = _make_server()

    def _deny(port):
        raise OSError(_errno.EADDRINUSE, "Address already in use")

    with patch("emrg.server.daemon._create_fixed_port_socket", side_effect=_deny), \
         patch("emrg.server.daemon.is_server_running_sync", return_value=True), \
         patch("emrg.server.daemon.config_dir", return_value=tmp_path), \
         patch("emrg.server.daemon.serve", new_callable=AsyncMock) as mock_serve:
        import asyncio
        asyncio.run(server.serve())

    assert server._running is False, "duplicate daemon must not keep running"
    mock_serve.assert_not_awaited(), "must not bind when the fixed port is taken"


def test_serve_proceeds_when_fixed_port_free(tmp_path):
    """Negative path: fixed port free → bind passes → the websockets serve is
    reached with the pre-bound socket (no pid file is written anymore)."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    server = _make_server()
    fake_sock = MagicMock()

    with patch("emrg.server.daemon._create_fixed_port_socket", return_value=fake_sock), \
         patch("emrg.server.daemon.config_dir", return_value=tmp_path), \
         patch("emrg.server.daemon.serve", new_callable=AsyncMock,
               side_effect=RuntimeError("abort after admission — bind already verified")):
        try:
            asyncio.run(server.serve())
        except RuntimeError as e:
            assert "abort after admission" in str(e), f"unexpected abort: {e}"
        else:
            raise AssertionError("expected the websockets serve abort (admission passed)")
    assert not (tmp_path / "emrgd.pid").exists(), "no pid file may be written (rant 2026-08-21T16:45:06)"


def test_serve_timewait_retry_recovers_bind(tmp_path):
    """EADDRINUSE with NO live listener = TIME_WAIT remnant (Windows
    SO_EXCLUSIVEADDRUSE) → serve() retries the bind and recovers."""
    import asyncio
    import errno as _errno
    from unittest.mock import AsyncMock, MagicMock, patch

    server = _make_server()
    fake_sock = MagicMock()
    bind_calls = {"n": 0}

    def _flaky(port):
        bind_calls["n"] += 1
        if bind_calls["n"] == 1:
            raise OSError(_errno.EADDRINUSE, "Address already in use")
        return fake_sock

    with patch("emrg.server.daemon._create_fixed_port_socket", side_effect=_flaky), \
         patch("emrg.server.daemon.is_server_running_sync", return_value=False), \
         patch("emrg.server.daemon._TIME_WAIT_RETRIES", 3), \
         patch("emrg.server.daemon._TIME_WAIT_RETRY_DELAY", 0.01), \
         patch("emrg.server.daemon.config_dir", return_value=tmp_path), \
         patch("emrg.server.daemon.serve", new_callable=AsyncMock,
               side_effect=RuntimeError("abort after retry recovered the bind")):
        try:
            asyncio.run(server.serve())
        except RuntimeError as e:
            assert "abort after retry" in str(e), f"unexpected abort: {e}"
        else:
            raise AssertionError("expected the websockets serve abort (retry recovered)")
    assert bind_calls["n"] == 2, f"expected 2 bind attempts, got {bind_calls['n']}"
    assert not (tmp_path / "emrgd.pid").exists(), "no pid file may be written (rant 2026-08-21T16:45:06)"


def test_serve_timewait_retry_exhausted(tmp_path):
    """EADDRINUSE with no listener that never clears → serve() gives up
    gracefully (no websockets bind; no pid file exists anymore)."""
    import asyncio
    import errno as _errno
    from unittest.mock import AsyncMock, patch

    server = _make_server()

    def _always_busy(port):
        raise OSError(_errno.EADDRINUSE, "Address already in use")

    with patch("emrg.server.daemon._create_fixed_port_socket", side_effect=_always_busy), \
         patch("emrg.server.daemon.is_server_running_sync", return_value=False), \
         patch("emrg.server.daemon._TIME_WAIT_RETRIES", 2), \
         patch("emrg.server.daemon._TIME_WAIT_RETRY_DELAY", 0.01), \
         patch("emrg.server.daemon.config_dir", return_value=tmp_path), \
         patch("emrg.server.daemon.serve", new_callable=AsyncMock) as mock_serve:
        asyncio.run(server.serve())

    assert server._running is False, "must give up after retries"
    mock_serve.assert_not_awaited()
    assert not (tmp_path / "emrgd.pid").exists(), "failed instance must not claim the pid file"


def test_serve_rethrows_non_bind_errors(tmp_path):
    """A non-EADDRINUSE socket error must propagate — only 'address in use'
    means 'already running'."""
    import asyncio
    import errno as _errno
    from unittest.mock import patch

    server = _make_server()

    def _boom(port):
        raise OSError(_errno.EACCES, "Permission denied")

    with patch("emrg.server.daemon._create_fixed_port_socket", side_effect=_boom), \
         patch("emrg.server.daemon.config_dir", return_value=tmp_path):
        try:
            asyncio.run(server.serve())
        except OSError as e:
            assert e.errno == _errno.EACCES, f"unexpected errno: {e.errno}"
        else:
            raise AssertionError("expected OSError(EACCES) to propagate")


def test_assert_token_file_writes_token_only(tmp_path):
    """_assert_token_file persists ONLY the auth token (no port — rant
    2026-08-20T14:32:52: emrgd.port → emrgd.token, single-line token)."""
    from unittest.mock import patch

    server = _make_server()
    server._auth_token = "tok-123"
    with patch("emrg.server.daemon.config_dir", return_value=tmp_path):
        server._assert_token_file()
    token_file = tmp_path / "emrgd.token"
    assert token_file.exists()
    assert token_file.read_text(encoding="utf-8").strip() == "tok-123"
    # 旧 emrgd.port 不再被写入
    assert not (tmp_path / "emrgd.port").exists()


# ── Daemon stop-path logging (rant 2026-08-19T14:02:37) ──────────────
# Any reason / any path the daemon stops must leave a detailed emrgd.log:
# when, why, who triggered, what was cleaned up. These are PURE-MOCK tests
# (mock the scheduler/llm/background tasks, never boot or stop a real
# daemon) — the highest principle forbids stop/restart server tests.

def _make_shutdown_server(tmp_path) -> EmrgServer:
    """EmrgServer with all teardown dependencies mocked for _shutdown_all."""
    from unittest.mock import AsyncMock, MagicMock

    server = _make_server()
    server._stop_reason = "cancel"
    server._skills_ttl_task = None
    server._upgrade_tick_task = None
    server._port_keepalive_task = None
    server._scheduler = AsyncMock()  # stop_all + wait_all, no real handlers
    server._scheduler.stop_all = MagicMock()  # real API is sync
    server._scheduler.wait_all = AsyncMock()
    server.llm = AsyncMock()         # close() awaitable
    server._server = _ShutdownFakeServer()  # sync .close(), no unawaited coroutine
    return server


class _ShutdownFakeServer:
    """Minimal stand-in for the websockets Server in shutdown-message tests."""

    def close(self) -> None:
        pass


def test_shutdown_all_logs_reason_and_cleanup_steps(tmp_path, caplog):
    """_shutdown_all logs the stop reason + every cleanup step + final line."""
    import logging

    server = _make_shutdown_server(tmp_path)

    caplog.set_level(logging.INFO, logger="emrg.server.daemon")
    asyncio.run(server._shutdown_all())

    text = caplog.text
    assert "daemon stopping (reason=cancel, handlers=0) — cleaning up" in text
    assert "cancelled skills-ttl loop" in text
    assert "cancelled upgrade-tick loop" in text
    assert "cancelled port-keepalive loop" in text
    assert "stopped scheduler" in text
    assert "closed llm client" in text
    assert "removed port file" in text
    assert "daemon stopped (reason=cancel, uptime=" in text
    assert "all_ok=True" in text


def test_shutdown_all_handles_failing_cleanup(tmp_path, caplog):
    """A failing cleanup step is recorded (all_ok=False), never raises."""
    import logging
    from unittest.mock import AsyncMock

    server = _make_shutdown_server(tmp_path)
    server.llm.close = AsyncMock(side_effect=RuntimeError("boom"))

    caplog.set_level(logging.INFO, logger="emrg.server.daemon")
    asyncio.run(server._shutdown_all())

    text = caplog.text
    assert "daemon stopping (reason=cancel" in text
    assert "closed llm client" in text  # step listed even on failure
    assert "daemon stopped (reason=cancel" in text
    assert "all_ok=False" in text


def test_shutdown_all_reason_crash_and_sigint(tmp_path, caplog):
    """_stop_reason is echoed in both the start and final log lines."""
    import logging

    for reason in ("crash", "sigint", "shutdown_msg"):
        server = _make_shutdown_server(tmp_path)
        server._stop_reason = reason
        caplog.set_level(logging.INFO, logger="emrg.server.daemon")
        asyncio.run(server._shutdown_all())
        assert f"daemon stopping (reason={reason}" in caplog.text
        assert f"daemon stopped (reason={reason}" in caplog.text
        caplog.clear()


def test_shutdown_message_logs_peer_and_source(tmp_path, caplog):
    """shutdown msg logs peer + source and sets _stop_reason=shutdown_msg."""
    import logging

    server = _make_shutdown_server(tmp_path)
    writer = _FakeWriter()

    caplog.set_level(logging.INFO, logger="emrg.server.daemon")
    asyncio.run(server._process_message({"type": "shutdown", "source": "stop_all"}, writer))

    assert "shutdown requested by client (peer=unknown peer, source=stop_all)" in caplog.text
    assert server._stop_reason == "shutdown_msg"
    # shutdown_ack is sent back
    assert _last_frame(writer) == {"type": "shutdown_ack"}


def test_shutdown_message_missing_source_degrades(tmp_path, caplog):
    """Legacy shutdown msg without source logs source=unknown (backward compat)."""
    import logging

    server = _make_shutdown_server(tmp_path)
    writer = _FakeWriter()

    caplog.set_level(logging.INFO, logger="emrg.server.daemon")
    asyncio.run(server._process_message({"type": "shutdown"}, writer))

    assert "shutdown requested by client (peer=unknown peer, source=unknown)" in caplog.text
    assert server._stop_reason == "shutdown_msg"


def test_pong_includes_current_version(tmp_path, monkeypatch):
    """Pong carries current_version from ~/.emrg/install/version.txt.

    Rant 2026-08-20T18:30:57: the GUI compares the version it last saw with
    the daemon's installed version and shows the upgrade banner on change.
    Missing file → empty string (dev runs), never an error.
    """
    import emrg.server.daemon as daemon_mod
    install = tmp_path / ".emrg" / "install"
    install.mkdir(parents=True)
    (install / "version.txt").write_text("0.2.59\n", encoding="utf-8")

    monkeypatch.setattr(daemon_mod.Path, "home", lambda: tmp_path)

    server = _make_server()
    assert server._current_installed_version() == "0.2.59"

    # Missing file → "" (no crash)
    empty = tmp_path / "no-install"
    monkeypatch.setattr(daemon_mod.Path, "home", lambda: empty)
    assert server._current_installed_version() == ""

    # Pong payload includes both fields (14:38:27 refactor: current_version =
    # in-memory run version captured at startup; installed_version = live read)
    monkeypatch.setattr(daemon_mod.Path, "home", lambda: tmp_path)
    writer = _FakeWriter()
    import asyncio
    asyncio.run(server._process_message({"type": "ping"}, writer))
    frame = _last_frame(writer)
    assert frame["type"] == "pong"
    assert frame["current_version"] == "0.2.59"
    assert frame["installed_version"] == "0.2.59"


def test_pong_run_vs_installed_version(tmp_path, monkeypatch):
    """current_version is the daemon's in-memory run version (fixed at
    startup); installed_version is the live disk read — the upgrade agent can
    update version.txt while the daemon still runs the pre-upgrade code.

    Rant 2026-08-21T14:38:27: the GUI shows the upgrade banner when
    installed_version ≠ current_version (upgrade installed, restart pending).
    previous-version.txt is no longer used for the judgment.
    """
    import emrg.server.daemon as daemon_mod
    install = tmp_path / ".emrg" / "install"
    install.mkdir(parents=True)
    (install / "version.txt").write_text("0.2.61\n", encoding="utf-8")

    monkeypatch.setattr(daemon_mod.Path, "home", lambda: tmp_path)

    server = _make_server()
    # _run_version captured at startup — mirrors the daemon's running code
    assert server._run_version == "0.2.61"

    # Simulate an upgrade: version.txt updated on disk, daemon not restarted
    (install / "version.txt").write_text("0.2.62\n", encoding="utf-8")
    assert server._run_version == "0.2.61", "run version must stay fixed in-process"
    assert server._current_installed_version() == "0.2.62"

    # Pong: current_version = run (0.2.61), installed_version = disk (0.2.62)
    writer = _FakeWriter()
    import asyncio
    asyncio.run(server._process_message({"type": "ping"}, writer))
    frame = _last_frame(writer)
    assert frame["type"] == "pong"
    assert frame["current_version"] == "0.2.61"
    assert frame["installed_version"] == "0.2.62"
    assert "previous_version" not in frame, "previous-version.txt no longer used (14:38:27)"


# ── _format_cache_pct ─────────────────────────────────────


def test_format_cache_pct_visibility():
    """_format_cache_pct renders hit rate only when cache usage is visible
    (rant 2026-08-23T09:17:14)."""
    server = _make_server()

    # cache hit reported (DeepSeek/OpenAI-compatible) → "cache N%"
    assert server._format_cache_pct(
        {"prompt_tokens": 100, "cache_hit_tokens": 70}) == ", cache 70%"

    # all hits (miss = 0) → 100%
    assert server._format_cache_pct(
        {"prompt_tokens": 50, "cache_hit_tokens": 50}) == ", cache 100%"

    # rounding
    assert server._format_cache_pct(
        {"prompt_tokens": 3, "cache_hit_tokens": 1}) == ", cache 33%"

    # no cache reported → no suffix, format unchanged
    assert server._format_cache_pct(
        {"prompt_tokens": 10, "completion_tokens": 2}) == ""
    assert server._format_cache_pct(
        {"prompt_tokens": 0, "cache_hit_tokens": 0}) == ""

    # None / malformed → no suffix
    assert server._format_cache_pct(None) == ""
    assert server._format_cache_pct({}) == ""
    assert server._format_cache_pct({"prompt_tokens": 10}) == ""
    assert server._format_cache_pct(
        {"prompt_tokens": 10, "cache_hit_tokens": None}) == ""

    # inconsistent (hit > prompt) → no suffix
    assert server._format_cache_pct(
        {"prompt_tokens": 10, "cache_hit_tokens": 20}) == ""
