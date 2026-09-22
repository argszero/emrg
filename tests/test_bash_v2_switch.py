"""The bash-tool-v2 switch (design D10) and the argument injection the boundary needs.

Rant ``2026-09-21T18:50:03`` ("bash tool v2").

Two subjects, both of which are about *who decides*:

* which executor the daemon builds — one switch, read once at startup, defaulting
  to v2 and keeping the frozen tool reachable as the rollback;
* which parts of a tool call the model may choose — the D1 root fix.  Before it,
  ``workdir`` was injected only when the model had not supplied one, so the model
  could name the very root it was trusted in (``workdir=/Users/<host>``), and the
  sandbox took its authorization root from the agent it was confining.
"""

import inspect
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from emrg.config import ENV_BASH_TOOL_V2, EmrgConfig, SandboxConfig, load_config, load_sandbox_config
from emrg.protocol import TaskRequest
from emrg.server.daemon import EmrgServer
from emrg.tools import ToolRegistry
from emrg.tools.bash_tool import BashTool
from emrg.tools.bash_tool_v2 import BashToolV2


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


@pytest.fixture(autouse=True)
def _no_ambient_switch(monkeypatch):
    """The shell must not decide what a test here measures.

    ``EMRG_BASH_TOOL_V2`` is the documented one-launch rollback, so a host
    starting pytest with it set is doing the normal thing and would otherwise see
    every default-reading test below fail while the product is correct.  The tests
    that are *about* the variable set it themselves, after this fixture runs.
    """
    monkeypatch.delenv(ENV_BASH_TOOL_V2, raising=False)


# ── the config seam ───────────────────────────────────────────────────────


def test_the_switch_defaults_to_v2():
    """The boundary is the default now: a boundary switched off is not the one measured.

    P6 of the programme (design §D7).  The frozen tool stays reachable — that is
    the rollback asserted at the bottom of this file — but an instance that has
    said nothing gets the OS boundary, on every platform whose chain has a rung.
    """
    assert SandboxConfig().bash_tool_v2 is True
    assert EmrgConfig().sandbox.bash_tool_v2 is True


def test_a_missing_config_file_keeps_the_default(tmp_path):
    assert load_sandbox_config().bash_tool_v2 is True


def test_the_section_is_read_from_the_file(tmp_path):
    _write_config(tmp_path, "[sandbox]\nbash_tool_v2 = false\n")
    assert load_sandbox_config().bash_tool_v2 is False


def test_a_malformed_sandbox_section_falls_back_to_the_default(tmp_path):
    """A config file the daemon cannot parse must not decide which tool runs."""
    _write_config(tmp_path, 'sandbox = "not a table"\n')
    assert load_sandbox_config().bash_tool_v2 is True


def test_the_environment_overrides_the_file_in_both_directions(tmp_path, monkeypatch):
    """The host can overrule ``config.toml`` in a session without editing it."""
    monkeypatch.setenv(ENV_BASH_TOOL_V2, "0")
    assert load_sandbox_config().bash_tool_v2 is False
    _write_config(tmp_path, "[sandbox]\nbash_tool_v2 = false\n")
    monkeypatch.setenv(ENV_BASH_TOOL_V2, "1")
    assert load_sandbox_config().bash_tool_v2 is True


def test_an_unparseable_environment_value_keeps_the_files_answer(tmp_path, monkeypatch):
    _write_config(tmp_path, "[sandbox]\nbash_tool_v2 = false\n")
    monkeypatch.setenv(ENV_BASH_TOOL_V2, "maybe")
    assert load_sandbox_config().bash_tool_v2 is False


def test_the_rest_of_the_config_is_unaffected_by_the_new_section(tmp_path):
    """The section is additive: ``load_config`` still resolves the old ones."""
    _write_config(
        tmp_path,
        "[llm]\nbase_url = \"http://x\"\napi_key = \"k\"\nmodel = \"m\"\n[sandbox]\nbash_tool_v2 = true\n",
    )
    cfg = load_config()
    assert cfg.llm.model == "m"
    assert cfg.sandbox.bash_tool_v2 is True


# ── the daemon's choice ───────────────────────────────────────────────────


def test_both_executors_answer_to_the_same_tool_name():
    """The model-visible contract is the dialect, so the registry can hold only one.

    Two names would be two behaviours for one tool call — the drift the parallel
    period exists to prevent — so this pins the collision deliberately.
    """
    assert BashTool().definition().name == "bash"
    assert BashToolV2().definition().name == "bash"
    registry = ToolRegistry()
    registry.register(BashToolV2())
    assert isinstance(registry.get("bash"), BashToolV2)


def test_the_daemon_builds_v2_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_BASH_TOOL_V2, raising=False)
    server = _instantiate()
    assert isinstance(server.tools.get("bash"), BashToolV2)
    assert not isinstance(server.tools.get("bash"), BashTool)


def test_the_file_switch_rolls_back_to_the_frozen_tool(tmp_path, monkeypatch):
    """The rollback is a supported path, not an accident: one line, no code change.

    A boundary that cannot be turned off in the field is not deployable, so this
    pins the way back as firmly as the way forward.
    """
    monkeypatch.delenv(ENV_BASH_TOOL_V2, raising=False)
    _write_config(tmp_path, "[sandbox]\nbash_tool_v2 = false\n")
    server = _instantiate()
    assert isinstance(server.tools.get("bash"), BashTool)
    assert not isinstance(server.tools.get("bash"), BashToolV2)


def test_the_environment_rolls_back_without_a_config_edit(monkeypatch):
    monkeypatch.setenv(ENV_BASH_TOOL_V2, "0")
    server = _instantiate()
    assert isinstance(server.tools.get("bash"), BashTool)


def test_the_environment_switch_builds_v2_without_a_config_edit(monkeypatch):
    monkeypatch.setenv(ENV_BASH_TOOL_V2, "1")
    server = _instantiate()
    assert isinstance(server.tools.get("bash"), BashToolV2)


def test_a_populated_registry_still_answers_every_other_tool(monkeypatch):
    """The switch chooses one executor; it must not disturb the rest."""
    monkeypatch.setenv(ENV_BASH_TOOL_V2, "1")
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


def test_the_old_executor_ignores_the_new_key(injected, tmp_path):
    """D10: the injection reaches v2 without moving the frozen tool's behaviour.

    ``BashTool`` reads ``command``/``timeout``/``workdir``/``sandbox`` only, so an
    extra ``workspace`` key is invisible to it — which is what lets one injection
    site serve both executors during the parallel period.
    """
    assert 'arguments.get("workspace")' not in inspect.getsource(BashTool.execute)
    args = injected("bash", {"command": "ls"}, sandbox="workspace-write")
    assert args["workspace"] == str(Path(args["workdir"]))


def test_an_unknown_tool_receives_nothing(injected):
    assert injected("read", {"path": "x"}) == {"path": "x"}


def test_the_injection_runs_before_every_execution(injected):
    """One call site, not two: the loop must not keep a parallel copy of the rule."""
    from emrg.server import daemon

    source = inspect.getsource(daemon.EmrgServer._run_tool_loop)
    assert source.count("_inject_tool_arguments(") == 1
    assert 'args["workdir"] = ' not in source
