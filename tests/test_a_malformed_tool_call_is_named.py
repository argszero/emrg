"""A tool call the client cannot read says so — it is not a raw exception.

Measured on this host 2026-10-03 (`cyc20261003-043254`) over a **real** turn —
an httpx stub in place of the transport, everything downstream of it untouched —
with the provider's `tool_calls` array carrying one malformed entry:

==========================  ==========================================================
entry sent                  what the host saw
==========================  ==========================================================
``None``                    ``LLM error: 'NoneType' object has no attribute 'get'.
``"x"``                     Check config at ~/.emrg/config.toml``
``[1]``                     (same sentence, shape substituted)
``{"function": None}``      (same)
``{"function": "f"}``       (same)
``{"function": {"name":    ``TypeError: unhashable type: 'list'`` — unhandled, out of
``["read"]}}``              the round, with no frame at all
==========================  ==========================================================

Five of the six are one failure with two halves, and both halves are wrong: a raw
Python sentence that names neither the field nor the shape, and a remedy —
"Check config at ~/.emrg/config.toml" — that sends the host to a file that has
nothing to do with a provider's malformed payload. The sixth ends the round with
no frame at all. In the memory reflection loop (the third reader) every one of
them stopped the loop *silently*: one LLM call, nothing written above DEBUG, and
the round looked exactly like "nothing worth remembering".

What is pinned here:

* the shape rule itself, over every measured shape and over the readable
  controls, with the part it could not read named;
* the two streaming readers (``llm.chat_stream`` and the round's own
  accumulator), behaviourally, through the frames a client would receive;
* the third reader, behaviourally: the reflection loop names what it dropped and
  still runs the readable call beside it;
* the frame's remedy, in both directions — an error that explains itself is not
  told to check the config, and one that does not still is;
* the population: every loop in the product code that binds a tool call to a
  name must sit in a function that asks the rule, with a registry for the
  readers that read *this client's own records* rather than the provider's
  payload.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.protocol import TaskRequest
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.server.llm import (
    CONTENT_FILTER_ERROR,
    OTHER_ERROR,
    ContentFilterAbort,
    SelfExplainingLlmError,
    classify_llm_error,
)
from emrg.server.tool_types import tool_call_shape_problem
from emrg.session import Session

PRODUCT_ROOT = Path(daemon_mod.__file__).resolve().parent.parent

#: The measured-malformed entries, with the part of the call each one breaks and
#: the shape word the sentence must use.
MALFORMED = [
    (None, "the tool call is not an object", "null"),
    ("x", "the tool call is not an object", "str"),
    ([1], "the tool call is not an object", "list"),
    ({"function": None}, "the tool call's `function` is not an object", "null"),
    ({"function": "f"}, "the tool call's `function` is not an object", "str"),
    ({"function": {"name": ["read"]}}, "the tool call's `function.name` is not a string", "list"),
    ({"function": {"name": None}}, "the tool call's `function.name` is not a string", "null"),
]

#: Readable entries — including two that are odd but were **not** measured to
#: break anything, so refusing them would be a behaviour change dressed as a
#: defect fix.
READABLE = [
    {},
    {"id": "c1"},
    {"id": "c1", "function": {}},
    {"function": {"name": ""}},
    {"function": {"name": "read"}},
    {"function": {"name": "read", "arguments": None}},
    {"function": {"name": "read", "arguments": {"file_path": "a"}}},
    {"index": 0, "id": "c1", "function": {"name": "read", "arguments": "{}"}},
    {"unexpected": ["field"]},
]


# ── the rule ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("entry,part,shape", MALFORMED)
def test_every_measured_shape_is_refused_by_name(entry, part, shape):
    problem = tool_call_shape_problem(entry)
    assert problem is not None, f"{entry!r} was accepted"
    assert part in problem, problem
    assert problem.endswith(f"got {shape}"), problem


@pytest.mark.parametrize("entry", READABLE)
def test_a_readable_entry_is_not_refused(entry):
    assert tool_call_shape_problem(entry) is None, entry


def test_the_sentence_names_the_part_not_just_the_verdict():
    """The discriminating signal is *which* part could not be read.

    A sentence that said only "the tool call is malformed" would satisfy every
    leg above and tell the host nothing about what the provider did.
    """
    entry = {"function": {"name": ["read"], "arguments": "{}"}}
    problem = tool_call_shape_problem(entry)
    assert "`function.name`" in problem
    assert "`function`" != problem.split("`")[1]
    assert "arguments" not in problem, (
        "the arguments rule is a different rule with its own home; this one "
        f"must not answer for it: {problem}"
    )


# ── the streaming readers, through the frames a client receives ─────────────


class _Stream:
    status_code = 200
    headers = {}

    def __init__(self, deltas):
        self.deltas = deltas

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for obj in self.deltas:
            yield "data: " + json.dumps(obj)


class _Transport:
    """A fake transport whose first stream carries the payload under test.

    Later streams answer with plain text: the round re-asks after running a
    tool, and a fake that answered the same way forever would loop to the round
    budget instead of finishing the turn.
    """

    def __init__(self, deltas):
        self.deltas = deltas
        self.calls = 0

    def stream(self, method, url, headers=None, json=None):
        self.calls += 1
        if self.calls == 1:
            return _Stream(self.deltas)
        return _Stream([
            {"choices": [{"delta": {"content": "done"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ])


def _frames_for(entries, tmp_path, *, well_formed=None, abort_runs=None):
    """Run one real round over a fake transport and return the frames it sent.

    Nothing but the HTTP layer is stubbed: `llm.chat_stream` accumulates the
    deltas itself and `_run_tool_loop` reads what it yields, so this is the
    production path rather than a stand-in for it.
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tmp_path) / "projects.yml"
    if abort_runs is not None:
        server._abort_runs = abort_runs
    session = Session.create_with_id("shape-round", Path(tmp_path))
    frames: list[dict] = []

    async def fake_broadcast(session_id, frame):
        frames.append(frame)

    server._broadcast = fake_broadcast

    if well_formed is None:
        delta = {"tool_calls": entries}
    else:
        delta = {"tool_calls": [well_formed]}
    server.llm._client = _Transport([
        {"choices": [{"delta": delta}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ])
    req = TaskRequest(id="r", session_id=session.session_id, prompt="go")
    asyncio.run(asyncio.wait_for(server._run_tool_loop(req, None, session), 20))
    return session, frames


def _errors(frames):
    return [f.get("error") for f in frames if f.get("error")]


@pytest.mark.parametrize("entry,part,shape", MALFORMED)
def test_a_malformed_entry_reaches_the_client_as_its_own_sentence(entry, part, shape, tmp_path):
    _session, frames = _frames_for([entry], tmp_path)
    errors = _errors(frames)
    assert len(errors) == 1, (errors, len(frames))
    assert part in errors[0], errors[0]
    assert f"got {shape} —" in errors[0], errors[0]
    assert "tool_calls entry 0" in errors[0], errors[0]


@pytest.mark.parametrize("entry,part,shape", MALFORMED)
def test_the_raw_python_sentence_never_reaches_the_client(entry, part, shape, tmp_path):
    """The whole defect, as one assertion: the client is not shown Python."""
    _session, frames = _frames_for([entry], tmp_path)
    errors = _errors(frames)
    assert errors, "no error frame at all — the round ended without saying why"
    for raw in ("has no attribute", "unhashable", "Traceback", "NoneType"):
        assert raw not in errors[0], f"{raw!r} is the old raw sentence: {errors[0]}"


def test_the_unhashable_name_does_not_escape_the_round(tmp_path):
    """The one shape that used to leave `_run_tool_loop` by exception.

    A test that only asserted the message would have passed on a run that still
    propagated; this asserts the round *returned*.
    """
    _session, frames = _frames_for([{"function": {"name": ["read"], "arguments": "{}"}}], tmp_path)
    assert _errors(frames)
    assert any(f.get("done") for f in frames), "the round ended without a done frame"


def test_the_refusal_is_not_mistaken_for_an_overlong_request(tmp_path):
    """A wrong classification would be a wrong *action*.

    `classify_llm_error` drives two real behaviours in this loop: a content
    refusal is handed to the refusal ladder, an overlong one to the chunked
    compactor. A sentence that matched either vocabulary would have the round
    re-send a request whose tool calls could never be read.
    """
    _session, frames = _frames_for([None], tmp_path)
    errors = _errors(frames)
    assert errors
    assert classify_llm_error(SelfExplainingLlmError(errors[0])) == OTHER_ERROR, errors[0]


def test_a_readable_stream_is_untouched(tmp_path):
    """The control direction: a well-formed call still runs."""
    target = Path(tmp_path) / "note.md"
    target.write_text("hello\n", encoding="utf-8")
    _session, frames = _frames_for(None, tmp_path, well_formed={
        "index": 0, "id": "c1", "type": "function",
        "function": {"name": "read", "arguments": json.dumps({"file_path": str(target)})},
    })
    assert not _errors(frames), _errors(frames)
    kinds = [f.get("type") for f in frames if f.get("type")]
    assert kinds[:2] == ["tool_start", "tool_end"], frames


def test_the_round_s_own_accumulator_refuses_a_malformed_entry(tmp_path):
    """Reader two, reached on its own.

    `llm.chat_stream` normalizes what it yields, so the round's accumulator sees
    objects in production — but that is a fact about *one implementation* of
    `chat_stream`, not about the loop, and the read is the loop's own.
    `cyc20261003-041426`'s lesson is the reason this leg exists at all: the
    argument rule was real at one of two sites and the suite was green. The stub
    below yields the provider's entries unchanged, which is the shape the
    measurement of 2026-10-03 used.
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tmp_path) / "projects.yml"
    session = Session.create_with_id("shape-accumulator", Path(tmp_path))
    frames: list[dict] = []

    async def fake_broadcast(session_id, frame):
        frames.append(frame)

    server._broadcast = fake_broadcast

    async def raw_stream(messages, tools=None):
        yield {"content": "", "tool_calls": [None], "finish_reason": "tool_calls",
               "usage": None, "reasoning": None}

    server.llm.chat_stream = raw_stream
    req = TaskRequest(id="r", session_id=session.session_id, prompt="go")
    asyncio.run(asyncio.wait_for(server._run_tool_loop(req, None, session), 20))
    errors = _errors(frames)
    assert errors, "the round's own accumulator let a null entry through"
    assert "the tool call is not an object; got null" in errors[0], errors[0]
    assert "has no attribute" not in errors[0], errors[0]


def test_an_error_that_explains_itself_is_not_told_to_check_the_config(tmp_path):
    """The remedy follows the cause, and the content refusal is the control.

    Before this change the frame test was a substring of the content-filter
    sentence, so only that one error escaped the generic remedy; measured
    2026-10-03, the malformed-payload sentence carried it too.
    """
    _run_session, malformed_frames = _frames_for([None], tmp_path)
    malformed = _errors(malformed_frames)
    assert malformed and "Check config" not in malformed[0], malformed

    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tmp_path) / "projects.yml"
    session = Session.create_with_id("shape-content-filter", Path(tmp_path))
    frames: list[dict] = []

    async def fake_broadcast(session_id, frame):
        frames.append(frame)

    server._broadcast = fake_broadcast

    async def refusing_stream(messages, tools=None):
        raise SelfExplainingLlmError(CONTENT_FILTER_ERROR)
        yield  # pragma: no cover — the generator contract

    server.llm.chat_stream = refusing_stream
    req = TaskRequest(id="r", session_id=session.session_id, prompt="go")
    asyncio.run(asyncio.wait_for(server._run_tool_loop(req, None, session), 20))
    assert CONTENT_FILTER_ERROR in _errors(frames)


def test_a_malformed_tool_call_is_not_counted_as_a_content_filter_abort(tmp_path):
    """A diagnostic must report the cause that fired, not the error's shape.

    The daemon counts *runs* of content-filter aborts (rant 2026-09-28T15:57:31)
    and used to recognise one by searching the exception's text for the refusal
    sentence — one substring answering both "did this failure explain itself?"
    and "was the answer refused by the filter?". Widening the first to "any
    failure that explains itself" (which the malformed tool call needs) without
    a second signal would count this failure too, writing a provider's malformed
    payload into the run as a refused answer: a host told a trigger has been
    firing that never fired.
    """
    from emrg.server.abort_runs import AbortRuns

    runs = AbortRuns(Path(tmp_path) / "abort-runs.json")
    session, _frames = _frames_for([None], tmp_path, abort_runs=runs)
    assert runs.run("content_filter", session.session_id) is None, (
        "a malformed tool call was counted as a content-filter abort"
    )


def test_a_content_filter_abort_is_still_counted_as_one(tmp_path):
    """The control direction at the same site: the counter must still count."""
    from emrg.server.abort_runs import AbortRuns

    runs = AbortRuns(Path(tmp_path) / "abort-runs.json")
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tmp_path) / "projects.yml"
    server._abort_runs = runs
    session = Session.create_with_id("shape-abort-run", Path(tmp_path))
    frames: list[dict] = []

    async def fake_broadcast(session_id, frame):
        frames.append(frame)

    server._broadcast = fake_broadcast

    async def refusing_stream(messages, tools=None):
        raise ContentFilterAbort(CONTENT_FILTER_ERROR)
        yield  # pragma: no cover — the generator contract

    server.llm.chat_stream = refusing_stream
    req = TaskRequest(id="r", session_id=session.session_id, prompt="go")
    asyncio.run(asyncio.wait_for(server._run_tool_loop(req, None, session), 20))
    assert runs.run("content_filter", session.session_id)["count"] == 1, frames


def test_an_ordinary_failure_still_gets_the_config_remedy(tmp_path):
    """The other control direction: the remedy is right for what it was for."""
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tmp_path) / "projects.yml"
    session = Session.create_with_id("shape-transport", Path(tmp_path))
    frames: list[dict] = []

    async def fake_broadcast(session_id, frame):
        frames.append(frame)

    server._broadcast = fake_broadcast

    async def broken_stream(messages, tools=None):
        raise RuntimeError("All connection attempts failed")
        yield  # pragma: no cover — the generator contract

    server.llm.chat_stream = broken_stream
    req = TaskRequest(id="r", session_id=session.session_id, prompt="go")
    asyncio.run(asyncio.wait_for(server._run_tool_loop(req, None, session), 20))
    errors = _errors(frames)
    assert errors and "Check config at ~/.emrg/config.toml" in errors[0], errors


# ── the third reader: the memory reflection mini loop ───────────────────────


def _drive_reflection(entries, tmp_path, *, readable=None):
    """Run one reflection mini loop whose first reply carries `entries`.

    `tmp_path` is the session's workspace and must be a directory the test
    creates: the reflection loop reads the session's memory before it runs a
    single tool, and pointed at the host's own home it bails first (the sibling
    lesson in `test_a_tool_call_is_checked_wherever_it_is_dispatched.py`).
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    session = Session.create_with_id("reflect-shape", Path(tmp_path))
    calls: list[list[dict]] = []

    async def fake_chat(messages, tools=None):
        calls.append([dict(m) for m in messages])
        if len(calls) == 1:
            declared = list(entries)
            if readable is not None:
                declared.append(readable)
            return {"content": "", "tool_calls": declared}
        return {"content": "done", "tool_calls": None}

    server.llm.chat = fake_chat

    async def scenario():
        server._maybe_reflect_memory(session, "p" * 40, "a" * 40)
        for _ in range(400):
            await asyncio.sleep(0.01)
            if len(calls) >= 2:
                return
        return

    asyncio.run(scenario())
    return calls


@pytest.mark.parametrize("entry,part,shape", MALFORMED)
def test_a_malformed_entry_does_not_abort_the_reflection_silently(entry, part, shape, tmp_path, caplog):
    """Measured before the change: one LLM call, then nothing — no round two,
    no record, nothing above DEBUG. The loop must say what it dropped."""
    import logging
    caplog.set_level(logging.WARNING, logger="emrg.server.daemon")
    calls = _drive_reflection([entry], tmp_path)
    assert part in caplog.text, (
        f"the reflection loop said nothing about {entry!r}; "
        f"caplog={caplog.text!r} calls={len(calls)}"
    )
    assert "tool_calls[0]" in caplog.text, caplog.text


def test_the_reflection_loop_still_runs_the_readable_call_beside_it(tmp_path):
    """The control direction: dropping one entry must not drop the round."""
    target = Path(tmp_path) / "note.md"
    target.write_text("hello\n", encoding="utf-8")
    calls = _drive_reflection(
        [None],
        tmp_path,
        readable={"id": "c1", "type": "function", "function": {
            "name": "read", "arguments": json.dumps({"file_path": str(target)}),
        }},
    )
    assert len(calls) >= 2, "the reflection loop never reached a second round"
    tool_messages = [m for m in calls[-1] if m.get("role") == "tool"]
    assert tool_messages, f"the readable call never ran: {calls[-1]}"
    assert "hello" in tool_messages[0]["content"], tool_messages[0]["content"]


# ── the population of readers ───────────────────────────────────────────────

#: Loops that bind a tool call to a name but read **this client's own records**
#: rather than the provider's payload, each with the reason. Keys name the
#: *function*, not the line: what makes a loop a reader of the wire is where its
#: entries come from, and that is a fact about the function (two loops in
#: `records_to_messages` read the same source). A line-numbered key would go
#: stale every time a comment above it grew, which is a red that tells a later
#: cycle nothing.
EXEMPT: dict[str, str] = {
    "server/daemon.py:_estimate_tokens": (
        "measures the messages this client is about to send, and every "
        "`tool_calls` in them was written here; a wrong shape there is this "
        "client's own record, not a provider payload."
    ),
    "session.py:records_to_messages": (
        "converts this client's persisted session records back into messages — "
        "the writer is the daemon above."
    ),
    "session.py:_validate_tool_messages": (
        "pairs persisted tool results with the calls they answer, in records "
        "this client wrote."
    ),
    "client/app.py:_cards_from_tool_calls": (
        "the TUI renders transcript records; the provider's payload never "
        "reaches a client (a client is an interface, not the body)."
    ),
    "client/app.py:_replay_rows": (
        "same subject: rendering a transcript the daemon wrote."
    ),
    "server/content_risk_probe.py:records_to_remove": (
        "reads llm.jsonl records written by this client, and already answers a "
        "non-object entry itself (`isinstance(tc, dict)` in the comprehension)."
    ),
    "server/content_risk_probe.py:_tool_round_positions": (
        "same function family: llm.jsonl records, with the same isinstance guard."
    ),
}


def _entry_loops():
    """Every loop in the product code that binds a tool call to a name.

    The subject is the *binding*, not the iterable's spelling: the reader that
    matters most here iterates `tcs`, a local assigned from `delta.get(
    "tool_calls")` one line earlier, so a scan keyed on the text `tool_calls`
    would have skipped the very site this change guards. Both signals are used —
    a `tool_calls` in the iterable, or a target named `tc…` — and the registry
    above answers the false positives.

    Returns `(relpath, lineno, enclosing function node or None)`.
    """
    loops = []
    for path in sorted(PRODUCT_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            generators = []
            if isinstance(node, ast.For):
                generators = [node]
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                generators = list(node.generators)
            for loop in generators:
                target = loop.target
                names = [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]
                if "tool_calls" not in ast.unparse(loop.iter) and not any(
                    n.startswith("tc") for n in names
                ):
                    continue
                cur = parents.get(node)
                while cur is not None and not isinstance(
                    cur, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    cur = parents.get(cur)
                loops.append(
                    (path.relative_to(PRODUCT_ROOT).as_posix(), node.lineno, cur)
                )
    return loops


def _asks_the_rule(func) -> bool:
    return any(
        isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "tool_call_shape_problem")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "tool_call_shape_problem")
        )
        for n in ast.walk(func)
    )


def test_every_reader_of_a_tool_call_asks_the_shape_rule():
    """The rule, over the population — which is what catches reader three.

    `cyc20261003-041426`'s lesson is the reason this leg exists: the argument
    rule was real at one of two dispatch sites and the suite was green.
    """
    loops = _entry_loops()
    assert len(loops) >= 3, (
        f"only {len(loops)} tool-call loop(s) found under {PRODUCT_ROOT}; a scan "
        "that finds nothing cannot say a reader is guarded"
    )
    unguarded = []
    for rel, lineno, func in loops:
        if func is None:
            unguarded.append(f"{rel}:{lineno} (no enclosing function)")
            continue
        if f"{rel}:{func.name}" in EXEMPT:
            continue
        if not _asks_the_rule(func):
            unguarded.append(f"{rel}:{func.name}")
    assert not unguarded, (
        "these functions read a tool call without asking whether it can be read, "
        "so a malformed entry reaches `.get`/indexing as a raw Python exception: "
        f"{unguarded}. Either ask `tool_call_shape_problem` first, or add the "
        "function to EXEMPT with the reason it reads this client's own records."
    )


def test_no_exemption_is_written_without_a_reason():
    """The other direction: an exemption is a statement, and it must say why."""
    for key, reason in EXEMPT.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 20, (
            f"exemption {key!r} carries no usable reason: {reason!r}"
        )
    # …and an exemption for a function that holds no such loop is stale, not
    # harmless: it would keep answering for a site that has moved or gone.
    keys = {
        f"{rel}:{func.name}" for rel, _, func in _entry_loops() if func is not None
    }
    for key in EXEMPT:
        assert key in keys, f"exemption {key!r} names no function that holds one"
