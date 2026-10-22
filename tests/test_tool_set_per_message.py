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
* **the order against the approval channel**: a name the set omits must not put a
  question to the host first, since escalation asks permission for a command —
  with its own control, the same hop on a set that admits the shell;
* **the memory-reflection loop**, which builds its own table and therefore needs
  its own assertion: a filter in `_run_tool_loop` alone leaves this copy whole,
  and "no set stated" has to mean the default set here as it does everywhere;
* **the two ends of the message** — what a producer states and what the receiver
  reads back.

No daemon is started, stopped or restarted: `_run_tool_loop`, `_process_message`
and `_maybe_reflect_memory` are called directly, with the LLM, the broadcast and
the websocket stubbed.
"""

from __future__ import annotations

import asyncio
import json
import sys

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
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.session import Session
from emrg.tools.shell_dialects import SHELL_TOOL_NAMES, shell_tool_name


def _registry(platform_name: str | None = None):
    """The real registry, for a named platform or (by default) for this host.

    The platform defaults to the host rather than to a literal. A helper spelling
    `"darwin"` mounts `bash` on a Windows runner while `default_tool_names()`
    answers `pwsh`, so the shell is intersected away and every assertion about the
    set ends up describing a platform the runner is not — which is how the first
    version of this file read green on one leg and red on the other.
    """
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


def test_the_default_set_names_the_shell_the_registry_mounts():
    # The whitelist is written once and runs everywhere, so its shell entry must
    # come from the platform rather than from the word "bash" — asked here in both
    # dialects on whichever host runs this, plus the host's own. A reading taken
    # only where the two dialects agree (every POSIX box) cannot see the failure at
    # all, and one taken only on the Windows leg cannot be checked before pushing.
    for platform_name in ("darwin", "win32", None):
        subject = platform_name or sys.platform
        registry = _registry(platform_name)
        shells = {name for name in registry.names if name in SHELL_TOOL_NAMES}
        assert shells == {shell_tool_name(platform_name)}, (
            f"{subject}: the registry mounted {shells}, not this platform's dialect"
        )
        assert shells <= set(default_tool_names(platform_name)), (
            f"{subject}: the default set {default_tool_names(platform_name)} does not "
            f"name {shells}, the only shell this registry carries"
        )
        assert shells <= set(effective_tool_names(registry, None, platform_name)), (
            f"{subject}: the intersection "
            f"{effective_tool_names(registry, None, platform_name)} dropped the shell, "
            "so the session loses it without a word"
        )


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


def _drive_tool_loop(tmp_path, monkeypatch, name, requested, call_name,
                     arguments='{"intent": "x"}', sandbox=None, prepare=None):
    """Run one turn whose first round calls `call_name`; report what it was offered."""
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH", tmp_path / "fire")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH", tmp_path / "rc")
    server = _server(tmp_path)
    session = Session.create_with_id(f"tool-set-{name}", tmp_path)
    stub = _stub_two_tools(server)
    # The reflection loop is a subject of its own below; here it would only add a
    # background turn whose LLM is this test's stub.
    server._maybe_reflect_memory = lambda *a, **k: None
    if prepare is not None:
        # Lets a test stub what the escalation path would ask (`_approval_channel`)
        # before the turn runs, without widening this harness's return value.
        prepare(server)

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
                    "function": {"name": call_name, "arguments": arguments},
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
        tools=requested, sandbox=sandbox,
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
    # The refusal names what this turn *does* have. Listing the whole registry
    # would advertise, in the one message the model reads about availability, the
    # tools the turn cannot use — the same leak as an unfiltered table, spoken.
    assert f"Available: {['read']}" in said, (
        "the refusal has to name the turn's own set; frames said "
        f"{said[:400]!r}"
    )


def test_a_tool_inside_the_set_does_run(tmp_path, monkeypatch):
    # The positive control. Without it, the arm above would also pass on a harness
    # that never executes any tool at all — "not caught" and "never ran" print the
    # same green.
    _offered, calls, _frames = _drive_tool_loop(
        tmp_path, monkeypatch, "allowed", ["read", "write"], "write",
    )
    assert calls, "the harness cannot execute a tool at all, so the refusal above proves nothing"


# ── the set decides before the host is asked anything ─────────────────


def _record_approval_channel(asked):
    """A `_approval_channel` stand-in that records each question, then refuses.

    Refusing keeps the harness from running a command: the subject here is that
    the host is asked at all, and a green assertion must not depend on what the
    answer would have been.
    """
    async def channel(session_id):
        asked.append(session_id)

        async def ask(question):
            return None
        return ask
    return channel


def _hop_arguments():
    """A shell call that asks for one hop — legal from `read-only` in one step."""
    return json.dumps({
        "intent": "x",
        "sandbox_permissions": "workspace-write",
        "justification": "the command needs to write inside the workspace",
    })


def test_a_name_outside_the_set_asks_the_host_nothing(tmp_path, monkeypatch):
    # `read-only` is a tier the shell tool may widen from, so this call would
    # reach the approval channel if the set were consulted afterwards. It is the
    # order that is under test: an escalation for a tool the turn was never
    # offered is a question about a command that cannot run either way.
    asked: list[str] = []
    shell = shell_tool_name()
    _offered, calls, frames = _drive_tool_loop(
        tmp_path, monkeypatch, "no-approval", ["read"], shell,
        arguments=_hop_arguments(), sandbox="read-only",
        prepare=lambda server: setattr(
            server, "_approval_channel", _record_approval_channel(asked),
        ),
    )
    assert asked == [], (
        "the host was asked to approve an escalation for a tool this turn was "
        "never offered — the question goes out before the answer that the call "
        "cannot run, and a yes would be reported as the call's outcome"
    )
    assert calls == []
    said = json.dumps(frames, ensure_ascii=False)
    assert "unknown" in said.lower(), (
        "the call still has to be answered as unknown, not dropped in silence; "
        f"frames said {said[:400]!r}"
    )


def test_a_name_inside_the_set_does_ask_the_host(tmp_path, monkeypatch):
    # The positive control for the arm above: same hop, same arguments, and the
    # shell named in the set. Without it an `asked == []` assertion would also
    # hold on a harness that can never reach the approval channel.
    asked: list[str] = []
    shell = shell_tool_name()
    _drive_tool_loop(
        tmp_path, monkeypatch, "approval", ["read", shell], shell,
        arguments=_hop_arguments(), sandbox="read-only",
        prepare=lambda server: setattr(
            server, "_approval_channel", _record_approval_channel(asked),
        ),
    )
    assert asked, (
        "the stub cannot observe an approval question at all, so the arm above "
        "proves nothing about whether one is skipped"
    )


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


class _UnlistedTool:
    """A registered tool no whitelist names — the shape a `boss`-style role has."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls: list[dict] = []

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._name,
            description="registered but named in neither whitelist",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args):
        self.calls.append(dict(args))
        return ToolResult(content="the unlisted tool ran")


def test_the_reflection_loop_reads_silence_as_the_default_set(tmp_path):
    """`None` means "no set stated", and the answer to that is the default set.

    Reading the same silence as the **whole registry** is a second meaning for
    one fact, and it opens exactly the hole the whitelists close: a tool
    registered but named nowhere would be offered by this loop while every other
    turn refuses it.
    """
    async def _test():
        server = _server(tmp_path)
        session = Session.create_with_id("tool-set-reflect-silence", tmp_path)
        unlisted = _UnlistedTool("unlisted-role")
        server.tools.register(unlisted)
        tables: list[list[str]] = []

        async def fake_chat(messages, tools=None):
            tables.append([entry["function"]["name"] for entry in (tools or [])])
            return {"content": "no new memories", "tool_calls": None}

        server.llm.chat = fake_chat
        server._maybe_reflect_memory(
            session,
            "a prompt long enough to be worth reflecting on " * 2,
            "an answer long enough to be worth reflecting on " * 2,
        )
        await asyncio.sleep(0.2)
        assert tables, "the reflection loop never ran, so it asserted nothing"
        assert "unlisted-role" not in tables[0], (
            "a tool registered but named in no whitelist was offered by the "
            "reflection loop — one silence, two meanings"
        )
        assert unlisted.calls == []
        assert tables[0] == effective_tool_names(server.tools, None)

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
    #
    # The two sets were equal until #2467 gave scheduled rounds a tool a
    # conversation must not have. What is asserted here is the **shape** of that
    # difference rather than its contents: an equality against a literal would
    # restate the implementation and go green on any change that kept the restatement
    # in step, while the property below is the one that matters — the difference is
    # exactly the owner role, and no tool is lost from either side.
    scheduled = set(scheduled_task_tool_names())
    default = set(default_tool_names())
    assert scheduled - default == {"boss"}, (
        "the two sets differ by the owner role and nothing else (#2467); any other "
        f"difference is a tool that silently appeared on one side: {scheduled - default}"
    )
    assert not default - scheduled, (
        "a tool offered to a conversation must stay available to a scheduled round; "
        f"these were dropped: {default - scheduled}"
    )
