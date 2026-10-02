"""Every dispatch of a tool call checks the arguments it hands the tool.

`cyc20261003-035245` closed the leak at **one** dispatch site — the main tool
loop — and wrote "one home, at the dispatch site" in its own record. There were
**two**. Measured 2026-10-03 (`cyc20261003-041426`) by driving the memory
reflection loop with `read(file_path=["a"])`:

    Error: argument should be a str or an os.PathLike object where __fspath__
    returns a str, not 'list'

— the same Python-internals sentence, naming neither the tool nor the argument,
from the same tool, through the same registry, with the same schema. A fix that
is real at one of two sites is a fix that reads green and leaves the defect
live, which is why the rule below is asserted over the **population of dispatch
sites** and not over the site that happened to be noticed.

What is pinned here:

* the second site, behaviourally, through the real registry — the model is
  handed the refusal, and a call that fits its schema still runs;
* the rule over the population: an AST scan of every `… .execute(` call site in
  the product code, each of which must sit in a function that asks
  `argument_shape_problem`, with a registry for a justified exemption;
* both control legs, so the scan cannot pass vacuously by finding nothing, and
  an exemption cannot be added without a reason.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.session import Session
from emrg.tools.argument_shape import argument_shape_problem

PRODUCT_ROOT = Path(daemon_mod.__file__).resolve().parent.parent

#: `.execute(` call sites whose enclosing function does not call
#: `argument_shape_problem`, each with the reason it is not a tool dispatch.
#: Empty today: every site found is a tool dispatch. A future non-dispatch call
#: (a database cursor, a subprocess helper) belongs here **with its reason**,
#: and the leg below refuses an entry without one.
EXEMPT: dict[str, str] = {}


# ── the population of dispatch sites ────────────────────────────────────────


def _execute_call_sites():
    """Every `… .execute(` call site in the product code, as (relpath, line, node).

    The receiver is deliberately not filtered: a rule that only looked for the
    name `tool` would miss the very next site that calls it something else, and
    the false positives a wider net catches are answered by the registry above.
    """
    sites = []
    for path in sorted(PRODUCT_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "execute":
                sites.append((path.relative_to(PRODUCT_ROOT).as_posix(), node.lineno, node, parents))
    return sites


def _enclosing_function(node, parents):
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    return None


def _calls_the_rule(func) -> bool:
    """Whether this function body asks `argument_shape_problem` at all."""
    return any(
        isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "argument_shape_problem")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "argument_shape_problem")
        )
        for n in ast.walk(func)
    )


def test_every_tool_dispatch_asks_the_argument_shape_rule():
    """The rule, over the population — which is what would have caught site two."""
    sites = _execute_call_sites()
    assert len(sites) >= 2, (
        f"only {len(sites)} `… .execute(` call site(s) found under "
        f"{PRODUCT_ROOT}; a scan that finds nothing cannot say a site is guarded"
    )
    unguarded = []
    for rel, lineno, node, parents in sites:
        key = f"{rel}:{lineno}"
        if key in EXEMPT:
            continue
        func = _enclosing_function(node, parents)
        if func is None or not _calls_the_rule(func):
            unguarded.append(key)
    assert not unguarded, (
        "these call sites execute a tool without asking whether the arguments "
        "are the types the tool's own schema names, so a wrong-typed argument "
        "reaches the tool as a bare TypeError and the model is answered a "
        f"Python-internals sentence: {unguarded}. Either call "
        "`argument_shape_problem` before the call, or add the site to EXEMPT "
        "with the reason it is not a tool dispatch."
    )


def test_no_exemption_is_written_without_a_reason():
    """The other direction: an exemption is a statement, and it must say why."""
    for key, reason in EXEMPT.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 20, (
            f"exemption {key!r} carries no usable reason: {reason!r}"
        )
    # …and an exemption for a site that does not exist is stale, not harmless.
    keys = {f"{rel}:{lineno}" for rel, lineno, _, _ in _execute_call_sites()}
    for key in EXEMPT:
        assert key in keys, f"exemption {key!r} names no call site that exists"


# ── the second site, behaviourally ──────────────────────────────────────────


def _drive_reflection(tool_name: str, args: dict, cwd: Path):
    """Run one memory-reflection mini loop whose single tool call is `args`.

    The daemon builds the roster itself (`EmrgServer(...)`), so the schema the
    rule reads is the one the model is handed rather than a stand-in's.

    `cwd` is the session's workspace, and it must be a directory the **test**
    creates: the session keeps its memory under `<cwd>/.emrg/sessions/<id>`, and
    the reflection loop reads that memory before it runs a single tool. Measured
    2026-10-03 (`cyc20261003-041426`) — pointed at the host's own `~/.emrg`,
    which this process may not write, the reflection bailed before the tool call
    and these legs failed *in the full suite* while passing on their own. A test
    whose outcome depends on whether `$HOME` happens to be writable is a test
    reporting the host, not the code.
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    session = Session.create_with_id("reflect-shape", cwd)
    calls: list[list[dict]] = []

    async def fake_chat(messages, tools=None):
        calls.append([dict(m) for m in messages])
        if len(calls) == 1:
            return {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(args)},
            }]}
        return {"content": "done", "tool_calls": None}

    server.llm.chat = fake_chat

    async def scenario():
        # Inside the loop: the entry point is fire-and-forget via
        # `asyncio.create_task`, which needs a running loop to schedule on.
        server._maybe_reflect_memory(session, "p" * 40, "a" * 40)
        for _ in range(400):
            await asyncio.sleep(0.01)
            if len(calls) >= 2:
                return
        raise AssertionError("the reflection loop never finished a second round")

    asyncio.run(scenario())
    tool_messages = [m for m in calls[-1] if m.get("role") == "tool"]
    assert tool_messages, f"no tool message reached the model; calls={len(calls)}"
    return tool_messages[0]["content"]


def test_the_reflection_loop_answers_a_wrong_typed_argument_by_name(tmp_path):
    """The defect, at the site that was missed — measured before the change:
    `Error: argument should be a str or an os.PathLike object where __fspath__
    returns a str, not 'list'`."""
    answer = _drive_reflection("read", {"file_path": ["a"]}, tmp_path)
    assert answer == "the `file_path` argument must be a string; got list", answer


def test_the_reflection_loop_still_runs_a_call_that_fits(tmp_path):
    """The control direction at the same site: a refusal that fired here would
    satisfy every leg above and break memory reflection."""
    target = tmp_path / "note.md"
    target.write_text("hello\n", encoding="utf-8")
    answer = _drive_reflection("read", {"file_path": str(target)}, tmp_path)
    assert "must be a string" not in answer, answer
    assert "hello" in answer, f"the call did not run: {answer!r}"


@pytest.mark.parametrize(
    "tool_name,args",
    [("write", {"file_path": "z.md", "content": ["a"]}),
     ("glob", {"pattern": ["*.md"]})],
)
def test_the_other_shapes_are_refused_the_same_way_here(tmp_path, tool_name, args):
    answer = _drive_reflection(tool_name, args, tmp_path)
    assert answer.startswith("the `") and "must be a string" in answer, answer
    assert "TypeError" not in answer and "PathLike" not in answer, answer


def test_the_rule_reads_the_tools_own_schema_and_not_a_copy_of_it():
    """The refusal is derived from `definition()`, so it names the property the
    tool declares — not a property this test, or the rule, guessed."""
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    for name in server.tools.names:
        definition = server.tools.get(name).definition()
        parameters = definition.parameters
        forbidden = parameters.get("required", [])
        for prop, spec in parameters.get("properties", {}).items():
            if not isinstance(spec, dict) or spec.get("type") != "string":
                continue
            message = argument_shape_problem(definition, {prop: ["x"]})
            assert message and prop in message, (name, prop, message)
            if prop in forbidden:
                null_message = argument_shape_problem(definition, {prop: None})
                assert null_message and prop in null_message, (name, prop)
