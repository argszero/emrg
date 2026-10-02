"""A tool argument that is not the type its schema names is refused by name.

Measured 2026-10-03 (`cyc20261003-035245`) on this host, before this change:
every registered tool, handed the argument its own JSON Schema declares as a
`string` sent as a list, raised out of `execute()` — `read`/`write`/`edit` out of
`Path(...)`, `glob` out of `pathlib`, `grep` with `unhashable type: 'list'`, the
shell with a `TypeError` from the command scan. The loop's only guard is
`except Exception` around `tool.execute(...)`, so the model was answered a
Python-internals sentence that names neither the tool, the argument, nor the
call.

The rule is `emrg/tools/argument_shape.argument_shape_problem` — one home, at
the dispatch site, driven by the tool's **own** schema. What is pinned here:

* the rule over the **population** (every tool the daemon registers, every
  property it declares a string) and in **both directions** — a rule that
  refused everything would satisfy the first half alone;
* the sentence names the property and the shape that arrived;
* the two deliberate boundaries: a required string that is null is a wrong
  value, an optional one is absent; and a property that is not a string is the
  tool's own business;
* what a tool name no tool answers to, and arguments that are not an object, are
  **not** this rule's question;
* the loop's call site — the model is handed the refusal, not a `TypeError`.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.server.tool_types import ToolDefinition
from emrg.session import Session
from emrg.tools.argument_shape import argument_shape_problem


# ── fixtures ────────────────────────────────────────────────────────────────


def _server() -> EmrgServer:
    """A daemon instance with the registry it really builds.

    The population below is read from **this**, not from a list written here: a
    new tool (or a new string property) must be covered the day it is
    registered, and a list maintained by hand would be a second roster free to
    drift from the daemon's.
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def _string_properties(server: EmrgServer):
    """Every `(tool name, property name)` the registry declares as a string."""
    for name in server.tools.names:
        definition = server.tools.get(name).definition()
        properties = definition.parameters.get("properties", {})
        for prop, spec in properties.items():
            if isinstance(spec, dict) and spec.get("type") == "string":
                yield name, prop


# ── the rule over the population ────────────────────────────────────────────


def test_every_string_property_of_every_registered_tool_is_checked():
    """A non-string is refused, and a string is not — over the real registry.

    Both directions in one leg on purpose: the second half is the control that
    says a guard refusing every call could not pass this test, and it also pins
    that the good shape is left alone.
    """
    server = _server()
    population = list(_string_properties(server))
    assert len(population) >= 15, (
        f"the population is {len(population)} properties; if the registry lost "
        f"its schemas this rule would pass vacuously"
    )
    for tool_name, prop in population:
        definition = server.tools.get(tool_name).definition()
        wrong = argument_shape_problem(definition, {prop: ["not", "a", "string"]})
        assert wrong, f"{tool_name}.{prop} accepted a list"
        assert prop in wrong, f"the refusal for {tool_name}.{prop} does not name it: {wrong}"
        assert argument_shape_problem(definition, {prop: "a string"}) is None, (
            f"{tool_name}.{prop} refused a string"
        )


def test_the_refusal_names_the_property_and_the_shape_that_arrived():
    definition = ToolDefinition(
        name="t", description="", parameters={
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    )
    for value, shape in (
        (["a"], "list"), (5, "int"), ({"a": 1}, "dict"), (True, "bool"),
    ):
        assert argument_shape_problem(definition, {"pattern": value}) == (
            f"the `pattern` argument must be a string; got {shape}"
        )


def test_a_required_string_that_is_null_is_a_wrong_value():
    """`null` on a required property has nothing else it can mean.

    For an **optional** one it is how a model says "I did not set this", and
    every tool's own optional-argument read (an `or` chain) already treats it as
    absent — so refusing those would invent a failure for a call that runs today.
    """
    required = ToolDefinition(
        name="t", description="", parameters={
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
    )
    optional = ToolDefinition(
        name="t", description="", parameters={
            "properties": {"workdir": {"type": "string"}}, "required": [],
        },
    )
    assert argument_shape_problem(required, {"file_path": None}) == (
        "the `file_path` argument must be a string; got null"
    )
    assert argument_shape_problem(optional, {"workdir": None}) is None


def test_a_property_that_is_not_a_string_is_left_alone():
    """The leak this rule closes is the string one, measured; the rest is the
    tool's own business — `start_line: "3"` is accepted today, and turning that
    into a refusal would be a behaviour change dressed as a defect fix."""
    definition = ToolDefinition(
        name="t", description="", parameters={
            "properties": {
                "start_line": {"type": "integer"},
                "replace_all": {"type": "boolean"},
            },
            "required": [],
        },
    )
    assert argument_shape_problem(definition, {"start_line": "3"}) is None
    assert argument_shape_problem(definition, {"start_line": []}) is None
    assert argument_shape_problem(definition, {"replace_all": "yes"}) is None


def test_a_tool_that_does_not_exist_and_arguments_that_are_not_an_object_are_not_my_question():
    """Not "fine" — other rules, with their own readers.

    The loop already names the tools it knows for an unknown name, and the
    shape of the arguments object itself is a separate defect; answering for
    them here would be a second, disagreeing opinion.
    """
    definition = ToolDefinition(
        name="t", description="", parameters={
            "properties": {"pattern": {"type": "string"}}, "required": ["pattern"],
        },
    )
    assert argument_shape_problem(None, {"pattern": ["a"]}) is None
    for arguments in (["a"], "a", 5, None):
        assert argument_shape_problem(definition, arguments) is None


def test_a_definition_with_nothing_to_check_says_nothing():
    for parameters in ({}, {"properties": {}}, {"properties": []}, {"properties": {1: 2}}):
        definition = ToolDefinition(name="t", description="", parameters=parameters)
        assert argument_shape_problem(definition, {"pattern": ["a"]}) is None


def test_a_property_that_is_absent_is_not_a_property_that_is_wrong():
    definition = ToolDefinition(
        name="t", description="", parameters={
            "properties": {"pattern": {"type": "string"}}, "required": ["pattern"],
        },
    )
    assert argument_shape_problem(definition, {"other": ["a"]}) is None


# ── the loop's call site ────────────────────────────────────────────────────


class _FakeWs:
    """Minimal subscriber: records what the daemon broadcasts to it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))


def _drive_call(tmp_path, monkeypatch, *, tool_name: str, args: dict):
    """Run one tool loop whose single call is `tool_name(args)`.

    Nothing is stubbed: the call goes to the tool the daemon really registered,
    so the schema the rule reads is the one the model was handed.

    Returns `(server, frames, messages_of_the_second_llm_call)` — the last is
    what the model is answered with, which is the thing under test.
    """
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_MARKER_PATH",
                        tmp_path / "planted-fire-heartbeat")
    monkeypatch.setattr(daemon_mod, "_PLANTED_FIRE_ROUND_COMPLETE_PATH",
                        tmp_path / "planted-fire-round-complete")
    server = _server()
    session = Session.create_with_id("shape-loop", tmp_path)
    ws = _FakeWs()
    server._session_subscribers[session.session_id] = {ws: str(session.cwd)}

    calls: list[list[dict]] = []

    async def fake_stream(messages, tools=None):
        calls.append(list(messages))
        if len(calls) == 1:
            yield {
                "content": "", "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 10, "completion_tokens": 1},
                "tool_calls": [{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(args)},
                }],
            }
        else:
            yield {"content": "done", "tool_calls": None, "finish_reason": "stop",
                   "usage": {"prompt_tokens": 10, "completion_tokens": 1}}

    server.llm.chat_stream = fake_stream
    req = TaskRequest(id="req-shape", session_id=session.session_id, prompt="go")

    async def scenario():
        await asyncio.wait_for(
            server._run_tool_loop(req, None, session), 10,
        )

    asyncio.run(scenario())
    return server, list(ws.sent), calls[1]


def test_the_model_is_answered_the_refusal_not_a_python_typeerror(tmp_path, monkeypatch):
    """The defect, at the site where the model reads the answer.

    `glob` with `pattern: ["*.py"]` raised `TypeError` out of `pathlib` before
    this change; the model's tool message was `Tool execution error: argument
    should be a str or an os.PathLike object where __fspath__ returns a str,
    not 'list'`.
    """
    _server_obj, frames, messages = _drive_call(
        tmp_path, monkeypatch, tool_name="glob", args={"pattern": ["*.py"]},
    )
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1, tool_messages
    answer = tool_messages[0]["content"]
    assert answer == "the `pattern` argument must be a string; got list", answer
    ends = [f for f in frames if f.get("type") == "tool_end"]
    assert len(ends) == 1
    assert ends[0]["error"] is True
    assert ends[0]["content"] == answer
    # …and the call never reached the tool: nothing was executed for it.
    starts = [f for f in frames if f.get("type") == "tool_start"]
    assert len(starts) == 1, "the call was announced once"


def test_a_call_that_fits_its_schema_still_runs(tmp_path, monkeypatch):
    """The other direction at the same site: the rule is not a blanket refusal.

    A refusal that fired here would satisfy every leg above and break the
    product, so the well-typed call is measured too — and it must have really
    run, not merely avoided the refusal.
    """
    (tmp_path / "only.py").write_text("x = 1\n", encoding="utf-8")
    _server_obj, frames, messages = _drive_call(
        tmp_path, monkeypatch, tool_name="glob", args={"pattern": "*.py"},
    )
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1, tool_messages
    answer = tool_messages[0]["content"]
    assert "must be a string" not in answer
    assert "only.py" in answer, f"the call did not run: {answer!r}"
    ends = [f for f in frames if f.get("type") == "tool_end"]
    assert ends and ends[0]["error"] is False


def test_a_call_that_cannot_be_made_never_asks_the_host_to_widen_it(tmp_path, monkeypatch):
    """The rule is decided **before** the escalation, and that ordering is the
    reason it is not merely cosmetic: a call that will not run must not pop an
    approval dialog for it, or the host is asked to consent to something the
    daemon already knows it will refuse."""
    _server_obj, frames, messages = _drive_call(
        tmp_path, monkeypatch, tool_name="bash",
        args={"command": ["echo", "hi"],
              "sandbox_permissions": "workspace-write",
              "justification": "the model asked for a wider tier"},
    )
    assert [f["type"] for f in frames if f.get("type") == "approval_request"] == [], (
        "the host was asked to widen a call that cannot be made"
    )
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert tool_messages[0]["content"] == (
        "the `command` argument must be a string; got list"
    )


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("bash", {"command": ["echo", "hi"]}),
        ("read", {"file_path": {"a": 1}}),
        ("grep", {"pattern": 5}),
    ],
)
def test_the_other_tools_leak_the_same_way_and_are_refused_the_same(
    tmp_path, monkeypatch, tool_name, args,
):
    """The measured table, one tool per row: each raised before this change."""
    _server_obj, frames, messages = _drive_call(
        tmp_path, monkeypatch, tool_name=tool_name, args=args,
    )
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    answer = tool_messages[0]["content"]
    assert answer.startswith("the `") and "must be a string" in answer, answer
    assert "Traceback" not in answer and "TypeError" not in answer, answer
