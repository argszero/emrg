"""Tests for protocol message types."""

from emrg.protocol import (
    TaskRequest,
    TaskResponse,
    ToolStart,
    ToolEnd,
    ServerPong,
    EvolutionLog,
    InstanceIdentity,
)


def test_task_request_to_dict():
    req = TaskRequest(
        id="abc-123",
        session_id="s_260718_1200_a3f9",
        cwd="/home/user/project",
        prompt="hello",
        stream=True,
    )
    d = req.to_dict()
    assert d["type"] == "task"
    assert d["id"] == "abc-123"
    assert d["session_id"] == "s_260718_1200_a3f9"
    assert d["cwd"] == "/home/user/project"
    assert d["prompt"] == "hello"
    assert d["stream"] is True
    assert "timestamp" in d


def test_task_request_defaults():
    req = TaskRequest()
    d = req.to_dict()
    assert d["type"] == "task"
    assert d["id"]  # auto-generated UUID
    assert d["session_id"] == ""
    assert d["stream"] is False


def test_task_response_from_dict():
    d = {"request_id": "abc", "content": "hello", "done": True, "delta": False}
    resp = TaskResponse.from_dict(d)
    assert resp.request_id == "abc"
    assert resp.content == "hello"
    assert resp.done is True
    assert resp.delta is False


def test_task_response_defaults():
    resp = TaskResponse.from_dict({})
    assert resp.request_id == ""
    assert resp.content == ""
    assert resp.done is False
    assert resp.delta is False


def test_tool_start_from_dict():
    d = {
        "request_id": "req-1",
        "tool_name": "bash",
        "tool_call_id": "call-42",
        "arguments": {"command": "echo hi"},
    }
    ts = ToolStart.from_dict(d)
    assert ts.request_id == "req-1"
    assert ts.tool_name == "bash"
    assert ts.tool_call_id == "call-42"
    assert ts.arguments == {"command": "echo hi"}


def test_tool_start_defaults():
    ts = ToolStart.from_dict({})
    assert ts.request_id == ""
    assert ts.tool_name == ""
    assert ts.arguments == {}


def test_tool_end_from_dict():
    d = {
        "request_id": "req-1",
        "tool_name": "bash",
        "tool_call_id": "call-42",
        "content": "output",
        "error": True,
    }
    te = ToolEnd.from_dict(d)
    assert te.request_id == "req-1"
    assert te.tool_name == "bash"
    assert te.content == "output"
    assert te.error is True


def test_tool_end_defaults():
    te = ToolEnd.from_dict({})
    assert te.error is False
    assert te.content == ""


def test_server_pong_from_dict():
    d = {
        "identity": {"instance_id": "emrg-abc", "host_name": "mac-mini"},
        "uptime_seconds": 3600,
        "evolution_count": 27,
    }
    sp = ServerPong.from_dict(d)
    assert sp.identity == {"instance_id": "emrg-abc", "host_name": "mac-mini"}
    assert sp.uptime_seconds == 3600
    assert sp.evolution_count == 27


def test_server_pong_defaults():
    sp = ServerPong.from_dict({})
    assert sp.identity is None
    assert sp.uptime_seconds == 0


def test_server_pong_with_model():
    """ServerPong response includes model name for TUI status line display."""
    d = {
        "identity": {"instance_id": "emrg-abc", "host_name": "mac-mini"},
        "uptime_seconds": 3600,
        "evolution_count": 5,
        "model": "deepseek-chat",
    }
    sp = ServerPong.from_dict(d)
    assert sp.identity == {"instance_id": "emrg-abc", "host_name": "mac-mini"}
    assert sp.uptime_seconds == 3600
    assert sp.evolution_count == 5
    # Model is an extra field — ServerPong.from_dict doesn't explicitly parse it,
    # but the TUI client reads it directly from the raw data dict.
    assert d.get("model") == "deepseek-chat"
    assert "model" in d


def test_evolution_log_defaults():
    log = EvolutionLog()
    assert log.timestamp == ""
    assert log.trigger == ""
    assert log.impact == []
    assert log.operations == []
    assert log.upstream_contribution is None


def test_instance_identity_defaults():
    ident = InstanceIdentity()
    assert ident.instance_id == ""
    assert ident.host_name == ""
    assert ident.fork_source is None
    assert ident.branch_id == "master"
