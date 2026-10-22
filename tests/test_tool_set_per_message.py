"""A message says which tools its turn may use, and the daemon enforces it (issue #2465).

Before this, the tool table was fixed at daemon start: `build_tool_registry`
registered eight tools and nothing afterwards narrowed that set for any session or
any message, so a host conversation and a scheduled task's round were served
exactly the same list with no way to make them differ.

The subjects here are the sites a filter has to reach for the set to be real, and
none of them proves another:

* **the effective set** — four cases, because the intersection has four outcomes
  and a test of the happy one says nothing about the silent drop;
* **the table offered to the model**, read off the wire: the `tools` argument the
  loop actually hands the LLM, not a helper's return value;
* **the execution side**, driven for real: a registered tool that the turn's set
  omits must not run — with a **positive control** that runs it when the set
  admits it, so a red arm cannot be a harness that never executes anything;
* **the memory-reflection loop**, which builds its own table and therefore needs
  its own assertion: a filter in `_run_tool_loop` alone leaves this copy whole;
* **the two ends of the message** — what a producer states and what the receiver
  reads back.

No daemon is started, stopped or restarted: `_run_tool_loop`, `_process_message`
and `_maybe_reflect_memory` are called directly, with the LLM, the broadcast and
the websocket stubbed.
"""

from __future__ import annotations

import asyncio
import json

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import (
    EmrgServer,
    build_tool_registry,
    default_tool_names,
    effective_tool_names,
    scheduled_task_tool_names,
)
from emrg.server.tool_types import ToolResult
from emrg.session import Session
from emrg.tools.shell_dialects import SHELL_TOOL_NAMES


def _registry(platform_name: str = "darwin"):
    return build_tool_registry(None, None, platform_name=platform_name)


def _server(tmp_path) -> EmrgServer:
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = tmp_path / "projects.yml"
    return server


class _StubTool:
    """Records that it ran, so "did not run" is observable rather than inferred."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, args):
        self.calls.append(dict(args))
        return ToolResult(content="the stub tool ran")


def _stub_two_tools(server: EmrgServer) -> _StubTool:
    """Make `read` and `write` answer from one stub, and leave the rest real."""
    stub = _StubTool()
    real_get = server.tools.get
    server.tools.get = lambda name: stub if name in {"read", "write"} else real_get(name)
    return stub


# ── the protocol field ────────────────────────────────────────────────


def test_a_task_request_carries_its_tool_list():
    assert TaskRequest(tools=["read"]).to_dict()["tools"] == ["read"]


def test_an_empty_tool_list_is_a_statement_and_survives_the_wire():
    # `sandbox` is sent only when truthy; `tools` is sent when it is not None,
    # because the empty list means "no tools at all" and dropping it would ask
    # for the default set instead — the opposite of what it says.
    assert TaskRequest(tools=[]).to_dict()["tools"] == []


def test_an_absent_tool_list_stays_absent():
    assert "tools" not in TaskRequest().to_dict()


# ── the effective set: four outcomes ──────────────────────────────────


def test_a_list_is_honoured_by_intersection():
    assert effective_tool_names(_registry(), ["read", "grep"]) == ["read", "grep"]


def test_no_list_falls_back_to_the_default_set():
    registry = _registry()
    assert effective_tool_names(registry, None) == [
        name for name in default_tool_names() if registry.get(name) is not None
    ]


def test_an_unregistered_name_is_dropped_and_the_rest_are_kept():
    assert effective_tool_names(_registry(), ["read", "no-such-tool", "grep"]) == [
        "read", "grep",
    ]


def test_a_list_of_nothing_but_unknown_names_yields_no_tools():
    assert effective_tool_names(_registry(), ["no-such-tool", "nor-this"]) == []


def test_the_default_set_names_the_shell_this_platform_registers():
    # The whitelist is written once and runs everywhere, so the shell entry must
    # come from the platform rather than from the word "bash": on Windows the
    # registered tool is `pwsh`, and a list spelling the other dialect would be
    # intersected away — the session would lose its shell without a word.
    registry = _registry()
    shells = {name for name in registry.names if name in SHELL_TOOL_NAMES}
    assert len(shells) == 1, f"exactly one dialect is mounted, got {shells}"
    assert shells <= set(effective_tool_names(registry, None))


def test_the_two_whitelists_name_tools_that_exist():
    registry = _registry()
    for name in set(default_tool_names()) | set(scheduled_task_tool_names()):
        if name in SHELL_TOOL_NAMES:
            continue
        assert registry.get(name) is not None, (
            f"{name!r} is in a whitelist but not registered — the whitelist and the "
            "registration have drifted apart"
        )


# ── the table offered to the model, and the execution side ────────────


def _drive_tool_loop(tmp_path, monkeypatch, name, requested, call_name):
    """Run one turn whose first round calls `call_name`; report what it was offered."""
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH", tmp_path / "fire")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH", tmp_path / "rc")
    server = _server(tmp_path)
    session = Session.create_with_id(f"tool-set-{name}", tmp_path)
    stub = _stub_two_tools(server)
    # The reflection loop is a subject of its own below; here it would only add a
    # background turn whose LLM is this test's stub.
    server._maybe_reflect_memory = lambda *a, **k: None

    offered: dict = {"tools": None}
    state = {"round": 0}

    async def fake_stream(messages, tools=None):
        state["round"] += 1
        if state["round"] == 1:
            offered["tools"] = [
                entry["function"]["name"] for entry in (tools or [])
            ]
            yield {
                "content": None,
                "tool_calls": [{
                    "index": 0,
                    "id": "call_0",
                    "function": {"name": call_name, "arguments": '{"intent": "x"}'},
                }],
                "finish_reason": "tool_calls",
                "usage": None,
            }
        else:
            yield {
                "content": "done", "tool_calls": None, "finish_reason": "stop",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    frames: list[dict] = []

    async def fake_broadcast(session_id, payload):
        frames.append(payload)

    server.llm.chat_stream = fake_stream
    server._broadcast = fake_broadcast
    req = TaskRequest(
        id=f"req-{name}", session_id=session.session_id, prompt="go",
        tools=requested,
    )
    asyncio.run(server._run_tool_loop(req, None, session))
    return offered["tools"], stub.calls, frames


def test_the_model_is_offered_only_the_tools_the_message_named(tmp_path, monkeypatch):
    offered, _calls, _frames = _drive_tool_loop(
        tmp_path, monkeypatch, "named", ["read"], "read",
    )
    assert offered == ["read"], (
        "the turn's set is the message's list; offering the whole registry here is "
        "the defect the set exists to remove"
    )


def test_the_model_is_offered_the_default_set_when_the_message_names_none(
    tmp_path, monkeypatch,
):
    offered, _calls, _frames = _drive_tool_loop(
        tmp_path, monkeypatch, "default", None, "read",
    )
    assert offered == effective_tool_names(_registry(), None)


def test_a_registered_tool_outside_the_set_does_not_run(tmp_path, monkeypatch):
    # `write` is registered, and this turn's set names only `read`.
    offered, calls, frames = _drive_tool_loop(
        tmp_path, monkeypatch, "refused", ["read"], "write",
    )
    assert offered == ["read"]
    assert calls == [], (
        "a tool the turn was never offered ran anyway — the serialised table is then "
        "advice, not a gate"
    )
    said = json.dumps([frame for frame in frames], ensure_ascii=False)
    assert "unknown" in said.lower(), (
        "the call has to be answered, not dropped in silence: the model has to learn "
        f"that the name is not available; frames said {said[:400]!r}"
    )


def test_a_tool_inside_the_set_does_run(tmp_path, monkeypatch):
    # The positive control. Without it, the arm above would also pass on a harness
    # that never executes any tool at all — "not caught" and "never ran" print the
    # same green.
    _offered, calls, _frames = _drive_tool_loop(
        tmp_path, monkeypatch, "allowed", ["read", "write"], "write",
    )
    assert calls, "the harness cannot execute a tool at all, so the refusal above proves nothing"


# ── the memory-reflection loop, which builds its own table ────────────


def test_the_memory_reflection_loop_honours_the_same_set(tmp_path):
    async def _test():
        server = _server(tmp_path)
        session = Session.create_with_id("tool-set-reflect", tmp_path)
        stub = _stub_two_tools(server)
        tables: list[list[str]] = []
        rounds = {"n": 0}

        async def fake_chat(messages, tools=None):
            rounds["n"] += 1
            if rounds["n"] == 1:
                tables.append([entry["function"]["name"] for entry in (tools or [])])
                return {
                    "content": "",
                    "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "write", "arguments": "{}"},
                    }],
                }
            return {"content": "no new memories", "tool_calls": None}

        server.llm.chat = fake_chat
        server._maybe_reflect_memory(
            session,
            "a prompt long enough to be worth reflecting on " * 2,
            "an answer long enough to be worth reflecting on " * 2,
            ["read"],
        )
        await asyncio.sleep(0.2)
        assert tables, "the reflection loop never ran, so it asserted nothing"
        assert tables[0] == ["read"], (
            "the reflection loop offers the whole registry while the turn offered one "
            "tool — the second table is a second gate, and it is the one that is open"
        )
        assert stub.calls == [], (
            "the reflection loop executed a tool the turn's set does not contain"
        )

    asyncio.run(_test())


# ── the message's two ends ────────────────────────────────────────────


class _FakeSocket:
    """A websocket stand-in the read loop can drive: `recv` yields frames, then ends."""

    def __init__(self, frames: list[dict]) -> None:
        self._frames = [json.dumps(frame) for frame in frames]
        self.sent: list[dict] = []

    async def recv(self) -> str:
        if not self._frames:
            from websockets.exceptions import ConnectionClosed

            raise ConnectionClosed(None, None)
        return self._frames.pop(0)

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))
        # Yield to the loop. The turn is started with `asyncio.create_task`, and a
        # disconnect cancels it in `_handle_client`'s `finally` — so a stand-in
        # whose `send` never yields lets the connection close before the spawned
        # turn has run at all, and the assertion below would read "not started" as
        # "field dropped".
        await asyncio.sleep(0)

    async def close(self) -> None:
        pass


def test_the_task_message_carries_its_tool_list_into_the_turn(tmp_path, monkeypatch):
    """Driven through `_handle_client`, because that is where a `task` is dispatched.

    `_process_message` has no `task` branch at all — it answers `unknown message
    type` — so a test that hands a `task` frame to it certifies a door production
    does not use.

    `HOME` and `TMPDIR` are pinned **for this test alone**: a `task` frame creates
    a session, and creating one writes the machine-wide session index under the
    home directory. Pinning them for the whole suite would turn every unrelated
    test red (a home under a temporary root is itself a writable area); pinning
    them here keeps this test's writes inside the test's own directory.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))
    (tmp_path / "home").mkdir()
    (tmp_path / "tmpdir").mkdir()
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH", tmp_path / "fire")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH", tmp_path / "rc")
    server = _server(tmp_path)
    server._auth_token = "test-token"
    seen: dict = {}

    async def handle(req, ws=None, session=None, cancel_event=None, allow_tools=True):
        # The wrapper, not `_run_tool_loop`: a disconnect cancels the turn, and the
        # cancel lands before the loop body runs — so the assertion is about what
        # the turn was **handed**, which is exactly what this test is about.
        seen["tools"] = req.tools

    server._run_tool_loop_locked = handle
    socket = _FakeSocket([
        {"type": "auth", "token": "test-token"},
        {
            "type": "task", "id": "req-receive", "session_id": "tool-set-receive",
            "cwd": str(tmp_path), "prompt": "go", "tools": ["read", "grep"],
        },
        # The turn is started as a task, so the read loop has to be given one more
        # frame to await before the spawned turn gets to run.
        {"type": "list_history", "session_id": "tool-set-receive", "cwd": str(tmp_path)},
    ])

    async def _drive():
        await server._handle_client(socket)
        # The turn was started as a task; it needs the loop to keep turning after
        # the connection has closed, or it is cancelled before its body runs.
        await asyncio.sleep(0.2)

    asyncio.run(_drive())

    assert seen.get("tools") == ["read", "grep"], (
        "the receiver dropped the field, so a producer can state a set and the turn "
        "runs on the default one"
    )


def test_the_scheduler_states_the_scheduled_task_set(tmp_path, monkeypatch):
    """The producer side, driven through the real cycle runner in test_scheduler.py."""
    # Kept as a pointer to the assertion's home: `_run_cycle_bounded` needs a
    # websocket, a handler and a template, and test_scheduler.py already builds
    # all three for the neighbouring field (`input_history`).
    assert scheduled_task_tool_names() == default_tool_names(), (
        "today the two sets are equal; when they stop being equal, the assertion in "
        "test_scheduler.py is what has to change with them"
    )
