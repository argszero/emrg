"""Which shell tool the daemon mounts, and which arguments the model may choose.

Rant ``2026-09-21T18:50:03`` ("bash tool v2").  The file was ``test_bash_v2_switch.py``
and its first subject was the switch; P7 retired the switch with the executor it
selected, so what is left is the decision itself and the file is named for it.

Two subjects, both of which are about *who decides*:

* which executor the daemon builds — now the **dialect's**, with no key, no
  environment override and no rollback to answer it (``shell_dialects.py`` says
  which dialect a platform gets).  Three tests still pin the negative half,
  because a host's ``config.toml`` or environment may carry the retired name:
  neither may change what is mounted, and neither may break the daemon;
* which parts of a tool call the model may choose — the D1 root fix.  Before it,
  ``workdir`` was injected only when the model had not supplied one, so the model
  could name the very root it was trusted in (``workdir=/Users/<host>``), and the
  sandbox took its authorization root from the agent it was confining.

A third subject joined them: **the text that names the mounted shell** (§14.5
item 6).  The prompt was repaired to name the registered tool, and the repair was
guarded at the template — but the prompt is not the only thing the model reads.
Three other carriers still sent it to ``bash``: the read tool's description and
its two size-limit hints, the grep tool's "instead of 'bash grep'", and the
daemon's placeholder for an image a non-vision model cannot see.  On Windows each
named a tool that is not mounted — the accident itself, one carrier over.
"""

import asyncio
import json
import re
import importlib
import importlib.util
import inspect
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from emrg.config import load_sandbox_config
from emrg.protocol import TaskRequest
from emrg.server.daemon import EmrgServer
from emrg.tools import ToolRegistry
from emrg.tools.bash_tool_v2 import BashToolV2
from emrg.tools.pwsh_tool_v2 import PwshToolV2
from emrg.tools.read_tool import MAX_IMAGE_SIZE, MAX_READ_SIZE, ReadTool
from emrg.tools.shell_dialects import (
    SHELL_TOOL_NAME_POSIX,
    SHELL_TOOL_NAME_WINDOWS,
    SHELL_TOOL_NAMES,
)


def _run(coro):
    return asyncio.run(coro)


def _write_config(tmp_path: Path, body: str) -> None:
    """Write the config file the autouse fixture points ``config_path()`` at."""
    path = tmp_path / ".emrg" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _instantiate() -> EmrgServer:
    """Build a server the way the daemon does, then keep its logs out of the host."""
    from emrg.config import LlmConfig

    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


# ── the daemon's choice ───────────────────────────────────────────────────


def _mounted_shell(server):
    """The shell tool the daemon registered, by the name the daemon itself reports.

    Read rather than assumed to be ``bash``: since P8 the roster is a platform
    gate (``emrg/tools/shell_dialects.py``), so on Windows the mounted tool is
    ``pwsh`` and ``get("bash")`` is ``None`` — a test that hardcoded the name
    would pass on the dev host and fail on Windows CI while the product was
    correct.  ``_mounted_shell_tool_name`` is the same function the prompt builder
    reads, so this asserts about the tool the model is actually offered.
    """
    name = server._mounted_shell_tool_name()
    return name, server.tools.get(name)


def test_both_executors_answer_to_the_same_tool_name():
    """The model-visible contract is the dialect, so the registry can hold only one.

    Two names would be two behaviours for one tool call — the drift the parallel
    period exists to prevent — so this pins the collision deliberately. The two
    frozen/parallel executors are what share the name ``bash``; ``pwsh`` is a peer
    dialect, not a second name for the same one (``tests/test_pwsh_tool_v2.py``).
    """
    assert BashToolV2().definition().name == "bash"
    registry = ToolRegistry()
    registry.register(BashToolV2())
    assert isinstance(registry.get("bash"), BashToolV2)


def test_the_daemon_mounts_the_dialects_executor():
    """One executor, chosen by the platform's dialect — there is nothing else to ask.

    ``BashToolV2`` specifically would be an assertion about the platform rather
    than about the rule: Windows mounts ``PwshToolV2`` (P8), so a test naming the
    bash class would pass here and fail there while the product was correct. What
    the daemon decides is "the dialect's own executor, at the process boundary",
    and ``SHELL_TOOL_NAMES`` is that roster read from the module that owns it.
    """
    server = _instantiate()
    name, tool = _mounted_shell(server)
    assert name in SHELL_TOOL_NAMES
    assert isinstance(tool, (BashToolV2, PwshToolV2))


def test_the_retired_key_in_a_config_file_cannot_bring_the_frozen_tool_back(tmp_path):
    """A host whose ``config.toml`` still carries ``bash_tool_v2`` keeps working.

    Both halves are the point. The mount must not change — the key no longer
    selects anything, and the one value it used to carry as a rollback is exactly
    what P7 deleted. And the daemon must not fail on the stale line: a config file
    the loader chokes on would take the whole tool registry with it, so the section
    reads as before with the unknown key ignored.
    """
    _write_config(tmp_path, "[sandbox]\nbash_tool_v2 = false\n")
    cfg = load_sandbox_config()
    assert not hasattr(cfg, "bash_tool_v2"), "the field came back"
    _, tool = _mounted_shell(_instantiate())
    assert isinstance(tool, (BashToolV2, PwshToolV2))


def test_the_retired_environment_variable_is_inert(monkeypatch):
    """The one-launch rollback is gone with the rollback.

    Asserted rather than left implicit because this is the shape a reader is most
    likely to assume still works: an environment override that quietly did nothing
    would send a host looking for a boundary that changed when it did not.
    """
    for spelling in ("0", "false", "1", "true"):
        monkeypatch.setenv("EMRG_BASH_TOOL_V2", spelling)
        _, tool = _mounted_shell(_instantiate())
        assert isinstance(tool, (BashToolV2, PwshToolV2)), spelling


def test_a_populated_registry_still_answers_every_other_tool():
    """The mount chooses one executor; it must not disturb the rest."""
    server = _instantiate()
    for name in ("read", "write", "edit", "glob", "grep"):
        assert server.tools.get(name) is not None


# ── the injected arguments (D1) ───────────────────────────────────────────


@pytest.fixture
def injected(tmp_path):
    """Call ``_inject_tool_arguments`` with a session cwd and a task tier.

    The session is a stub, not a real one: the rule reads exactly one field
    (``cwd``), and a real ``Session`` would create directories on disk to prove
    nothing extra.
    """

    def call(tool: str, args: dict, *, sandbox: str | None = None, cwd: Path | None = None):
        session = SimpleNamespace(cwd=cwd or tmp_path)
        EmrgServer._inject_tool_arguments(tool, args, session, TaskRequest(sandbox=sandbox))
        return args

    return call


def test_the_session_cwd_cannot_be_named_by_the_model(injected):
    """D1: the model could name the root it was trusted in.

    Measured in the design: ``workdir=/Users/<host>`` plus a write to ``.zshrc``
    was allowed, because the whole home directory became "the workspace".  A
    sandbox that takes its authorization root from the agent is not a sandbox,
    which is why the injection is unconditional rather than a default.
    """
    session_cwd = Path("/the/session/cwd")
    args = injected("bash", {"command": "ls", "workdir": "/Users/somebody"}, cwd=session_cwd)
    assert args["workdir"] == str(session_cwd)
    assert args["workdir"] != "/Users/somebody"


def test_the_cwd_is_injected_even_when_the_model_supplied_none(injected, tmp_path):
    args = injected("bash", {"command": "ls"})
    assert args["workdir"] == str(tmp_path)


def test_glob_receives_the_cwd_too(injected, tmp_path):
    assert injected("glob", {"pattern": "*.py"})["workdir"] == str(tmp_path)


def test_grep_receives_the_cwd_only_as_a_default(injected, tmp_path):
    assert injected("grep", {"pattern": "x"})["path"] == str(tmp_path)
    assert injected("grep", {"pattern": "x", "path": "/elsewhere"})["path"] == "/elsewhere"


def test_the_tier_comes_from_the_task_and_carries_the_boundary_with_it(injected, tmp_path):
    """``workspace`` is what v2 reads as its authorization root; the model cannot set it."""
    args = injected("bash", {"command": "ls", "sandbox": "danger-full-access", "workspace": "/"}, sandbox="read-only")
    assert args["sandbox"] == "read-only"
    assert args["workspace"] == str(tmp_path)


def test_write_and_edit_receive_the_tier_and_the_boundary(injected, tmp_path):
    for tool in ("write", "edit"):
        args = injected(tool, {}, sandbox="read-only")
        assert args["sandbox"] == "read-only"
        assert args["workspace"] == str(tmp_path)


def test_a_call_with_no_configured_tier_is_left_unconfined(injected):
    """A host session that asked for nothing keeps the behaviour it already had."""
    assert "sandbox" not in injected("bash", {"command": "ls"})
    assert "workspace" not in injected("write", {"path": "x"})


def test_the_injection_reaches_the_live_executor(injected):
    """D10's surviving half: the injection is what gives the executor its boundary.

    The row this replaces asserted the *frozen* tool ignored the extra key, which
    is why one injection site could serve both executors through the parallel
    period. That period is over — the frozen tool is deleted (issue #1675) — so
    what is left to assert is the half that still has a subject: the injected
    ``workspace`` is the resolved ``workdir``, i.e. the executor is handed the
    same directory the walk uses.
    """
    args = injected("bash", {"command": "ls"}, sandbox="workspace-write")
    assert args["workspace"] == str(Path(args["workdir"]))


def test_the_frozen_executor_is_gone():
    """The other half of issue #1675, stated where the mount is decided.

    The mount tests above used to name the frozen class in a negative assertion
    (``not isinstance(tool, BashTool)``). With the module deleted that assertion
    is unstateable, and the property it protected — that no code path can bring
    the static command scan back — is the one worth keeping.
    """
    assert importlib.util.find_spec("emrg.tools.bash_tool") is None


def test_an_unknown_tool_receives_nothing(injected):
    assert injected("read", {"path": "x"}) == {"path": "x"}


def test_the_injection_runs_before_every_execution(injected):
    """One call site, not two: the loop must not keep a parallel copy of the rule."""
    from emrg.server import daemon

    source = inspect.getsource(daemon.EmrgServer._run_tool_loop)
    assert source.count("_inject_tool_arguments(") == 1
    assert 'args["workdir"] = ' not in source


# ── the text that names it (§14.5 item 6, at the carriers the prompt misses) ──


def _other_dialect(name: str) -> str:
    """The dialect a host that mounted ``name`` did *not* mount."""
    return next(n for n in sorted(SHELL_TOOL_NAMES) if n != name)


def test_no_registered_tool_advertises_a_shell_this_host_does_not_mount():
    """The repair named the mounted tool in the prompt; the prompt is one carrier.

    Everything a tool's ``definition()`` carries is model-facing — the daemon
    sends it verbatim through ``to_openai_tools()`` — and the read tool used to
    send the model to a tool this platform does not mount ("use the bash tool",
    three times: the description and both size-limit hints).  Swept from the
    registry rather than from a list of files, so a carrier added tomorrow is
    covered here without editing this test.

    What is matched is the *tool* reference — the dialect's name followed by the
    word ``tool``, the same phrase the prompt's own guard asserts on — and the
    name forbidden is the *other* dialect's, so the test says the same thing on
    both platforms (on POSIX ``bash`` is the correct answer and ``pwsh`` the
    load-bearing absence).

    **The shapes measured on master, and which of them this catches.**  Three
    were found.  The read tool's "use the bash tool" is a *tool* reference and the
    pattern catches it.  ``submit_rant``'s "never rewrite the file with
    hand-written bash/python" names a *language* — the idiom for a hand-written
    script — and claims no tool; it is the control arm, and it must keep passing.
    grep's "instead of 'bash grep'" names a *command* the platform lacks and the
    sweep cannot reach it, because the text never said "tool": it is fixed and
    pinned in ``tests/test_grep_tool.py``.  The pattern is the phrase rather than
    the bare word precisely so the honest ``submit_rant`` sentence survives — a
    guard that flags the true sentence is a guard that gets deleted.
    """
    server = _instantiate()
    mounted = server._mounted_shell_tool_name()
    other = _other_dialect(mounted)
    pattern = re.compile(rf"\b{other}\s+tool\b", re.IGNORECASE)
    offenders = []
    for tool in server.tools.to_openai_tools():
        fn = tool["function"]
        text = f"{fn.get('description', '')} {json.dumps(fn.get('parameters', {}))}"
        if pattern.search(text):
            offenders.append(fn["name"])
    assert offenders == [], (
        f"these tools name the `{other}` tool, which this host does not mount "
        f"(it mounts `{mounted}`): {offenders}"
    )
    # The control arm, on synthetic text so it cannot decay with someone else's
    # prose: the pattern must read the tool reference, not the bare dialect name.
    assert pattern.search(f"never rewrite the file with hand-written {other}/python") is None
    assert pattern.search(f"use the {other} tool to finish the job") is not None


def test_the_read_tools_text_names_the_shell_it_sends_you_to(monkeypatch, tmp_path):
    """Both directions, at the three carriers the sweep above cannot enumerate.

    The description is read by the model on every request; the two hints are read
    when the read tool refuses a job it cannot do.  All three send the model to a
    shell, and all three used to send it to ``bash`` whatever the platform.

    ``definition()`` answers for the host, so the platform is asked about
    directly — the module's own ``shell_tool_name`` is the seam that exists for
    exactly this ("a test can ask the question about Windows without being on
    Windows").
    """
    from emrg.tools import read_tool

    for mounted, absent in (
        (SHELL_TOOL_NAME_WINDOWS, SHELL_TOOL_NAME_POSIX),
        (SHELL_TOOL_NAME_POSIX, SHELL_TOOL_NAME_WINDOWS),
    ):
        monkeypatch.setattr(read_tool, "shell_tool_name", lambda p=None, _m=mounted: _m)

        desc = ReadTool().definition().description
        assert f"the {mounted} tool" in desc
        assert f"{absent} tool" not in desc

        big = tmp_path / f"big-{mounted}.txt"
        big.write_text("x" * (MAX_READ_SIZE + 1))
        got = _run(ReadTool().execute({"file_path": str(big), "intent": "read it"}))
        assert got.error, got.content
        assert f"the {mounted} tool" in got.content
        assert f"{absent} tool" not in got.content

        img = tmp_path / f"big-{mounted}.png"
        img.write_bytes(b"\x00" * (MAX_IMAGE_SIZE + 1))
        got = _run(ReadTool().execute({"file_path": str(img), "intent": "read it"}))
        assert got.error, got.content
        assert f"the {mounted} tool" in got.content
        assert f"{absent} tool" not in got.content


def test_the_non_vision_placeholder_names_the_mounted_shell(tmp_path):
    """What the daemon substitutes for an image a non-vision model cannot see.

    It is the same question the prompt asks — ``_mounted_shell_tool_name`` — so
    it must get the same answer, and the answer must be the registry's rather
    than the platform's.  Before this, the placeholder said ``bash`` on Windows,
    where the model has no such tool to call.
    """
    server = _instantiate()
    mounted = server._mounted_shell_tool_name()
    other = _other_dialect(mounted)
    server.llm.config.vision = False
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x00" * 8)
    out = server._tool_content_for_llm(
        json.dumps({"type": "image", "path": str(img), "mime": "image/png"})
    )
    assert isinstance(out, str)
    assert f"the {mounted} tool" in out
    assert f"{other} tool" not in out
