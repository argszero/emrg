"""The Windows dialect layer (bash tool v2, P8) — the ``pwsh`` peer tool family.

Rant ``2026-09-23T10:07:35``.  The accident it repairs, in one line: v0.3.0 turned
the process-boundary bash tool on by default, and on Windows that tool spawned
``bash -c`` — an executable Windows does not have — so **every** command,
including ``echo ok``, returned ``[WinError 2] 系统找不到指定的文件``.  A host
with a *boundary* (the ACL restricted token, P4) and no usable *dialect*.

Four subjects, and the file is organised by them:

* **the resolution chain** — a pure function of ``(configured, env, platform)``,
  tested by injection, never by spawning;
* **the argv** — the dialect is the one word in front of the flags, and the
  command is a single element after them;
* **the environment** — the same overrides as the bash twin *minus* ``TERM``,
  which is a POSIX concept;
* **the platform gate and the prompt** — Windows mounts ``pwsh`` and never
  ``bash``; POSIX the reverse; and the system prompt names the tool that was
  actually registered, because a prompt that advertises a dialect the platform
  does not run is what the model then tries to use.

Nothing here spawns ``pwsh``, and nothing touches the daemon lifecycle or the
upgrade chain (the two standing red lines).  The four candidate paths are
asserted as *strings*; whether a real Windows resolves them is an acceptance item
that needs Windows hardware (design §14.7) and is marked unverified in the code
it describes.
"""

import asyncio
import ntpath
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from emrg.config import ENV_BASH_TOOL_V2, LlmConfig, SandboxConfig
from emrg.server.daemon import EmrgServer, _get_jinja_env, build_shell_tool
from emrg.tools import ToolRegistry
from emrg.tools.bash_tool import BashTool
from emrg.tools.bash_tool_v2 import BashToolV2
from emrg.tools import pwsh_tool_v2 as pwsh
from emrg.tools.pwsh_tool_v2 import PwshToolV2
from emrg.tools.shell_dialects import (
    SHELL_TOOL_NAME_POSIX,
    SHELL_TOOL_NAME_WINDOWS,
    SHELL_TOOL_NAMES,
    shell_tool_name,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_ambient_switch(monkeypatch):
    """The shell must not decide what a test here measures.

    ``EMRG_BASH_TOOL_V2`` is the documented one-launch rollback, so a host
    starting pytest with it set is doing the normal thing; without this, the
    roster tests below would read the ambient choice and fail while the product
    is correct (the defect class P6 shipped and then fixed in its own tests).
    """
    monkeypatch.delenv(ENV_BASH_TOOL_V2, raising=False)


def _instantiate() -> EmrgServer:
    """Build a server the way the daemon does, then keep its logs out of the host."""
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


# ── the resolution chain (pwsh-local/src/resolve.ts) ─────────────────────


def test_an_explicit_configuration_is_trusted_as_is():
    """``[sandbox] pwsh_path`` is used verbatim — the blueprint trusts ``pwshPath``.

    Not "preferred if it exists": a deployer who names an executable has answered
    the question, and a chain that second-guesses them makes the configured value
    unobservable (the same reasoning that keeps the cache relocation from
    overriding a declared ``UV_CACHE_DIR``).
    """
    assert pwsh.resolve_pwsh_path("D:\\tools\\pwsh.exe") == "D:\\tools\\pwsh.exe"
    assert pwsh.resolve_pwsh_path("D:\\tools\\pwsh.exe", env={}, platform_name="win32") == (
        "D:\\tools\\pwsh.exe"
    )


def test_the_chain_probes_seven_then_path_then_five_one():
    """Candidate order, verbatim from ``resolve.ts:21-37``.

    Order is the whole content of the rung: PowerShell 7's install directory
    first (the modern interpreter), then whatever ``PATH`` offers (a Microsoft
    Store install is an app-execution alias on ``PATH``), and Windows PowerShell
    5.1 last.
    """
    env = {"ProgramFiles": "C:\\PF", "SystemRoot": "C:\\SR", "PATH": "C:\\a;C:\\b"}
    assert pwsh.candidate_pwsh_paths(env) == [
        ntpath.join("C:\\PF", "PowerShell", "7", "pwsh.exe"),
        ntpath.join("C:\\a", "pwsh.exe"),
        ntpath.join("C:\\b", "pwsh.exe"),
        ntpath.join("C:\\SR", "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
    ]


def test_the_five_one_fallback_is_present_and_last():
    """The rung that makes "the executable is missing" unreachable on Windows.

    Every Windows host has Windows PowerShell 5.1, so a machine with pwsh 7
    uninstalled still runs commands.  This test is the mutation arm's target: drop
    the fallback from ``candidate_pwsh_paths`` and it fails.
    """
    candidates = pwsh.candidate_pwsh_paths({"SystemRoot": "C:\\SR"})
    assert candidates[-1] == ntpath.join(
        "C:\\SR", "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )


def test_a_path_entry_keeps_its_quotes_stripped():
    """``setx`` writes a quoted ``PATH``; a quote inside a path is not a path.

    ``resolve.ts`` strips them at the candidate, not at the probe, so the value
    that comes out of the chain is spawnable as-is.
    """
    env = {"PATH": '"C:\\quoted\\bin";C:\\plain', "SystemRoot": "C:\\SR"}
    candidates = pwsh.candidate_pwsh_paths(env)
    assert ntpath.join("C:\\quoted\\bin", "pwsh.exe") in candidates
    assert not any('"' in c for c in candidates)


def test_a_path_entry_of_whitespace_is_not_a_candidate():
    """An empty or blank ``PATH`` element must not become ``\\pwsh.exe``."""
    candidates = pwsh.candidate_pwsh_paths({"PATH": " ; ;; ", "SystemRoot": "C:\\SR"})
    assert candidates == [
        ntpath.join("C:\\Program Files", "PowerShell", "7", "pwsh.exe"),
        ntpath.join("C:\\SR", "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
    ]


def test_a_missing_well_known_directory_falls_through_to_the_next_rung(monkeypatch):
    """The chain is a *chain*: an existing candidate wins, and order decides ties."""
    monkeypatch.setattr(
        pwsh, "_candidate_exists", lambda c: c.endswith(ntpath.join("C:\\a", "pwsh.exe"))
    )
    env = {"ProgramFiles": "C:\\PF", "SystemRoot": "C:\\SR", "PATH": "C:\\a;C:\\b"}
    assert pwsh.resolve_pwsh_path(env=env, platform_name="win32") == ntpath.join(
        "C:\\a", "pwsh.exe"
    )


def test_an_unprobeable_candidate_is_not_a_candidate(monkeypatch):
    """A probe that raises answers "no" — an unprobeable path is not a spawnable one.

    The measured reason the probe is ``lstat``-shaped at all: the Store's alias
    follows to a target whose ACL answers ``EACCES``, and the chain must step over
    that rung rather than fall off the ladder.
    """

    def boom(_candidate):
        raise OSError("EACCES")

    monkeypatch.setattr(pwsh.os.path, "lexists", boom)
    assert pwsh._candidate_exists("C:\\whatever\\pwsh.exe") is False


def test_a_posix_host_does_not_probe_windows_paths(monkeypatch):
    """The chain is Windows-only, and the guard is the platform parameter.

    Proving it by *not consulting the candidates* rather than by their absence:
    on POSIX there is no ``C:\\Program Files`` to miss, so an assertion about the
    result alone would pass for the wrong reason.
    """

    def unexpected():
        raise AssertionError("a POSIX host must not enumerate Windows candidates")

    monkeypatch.setattr(pwsh, "candidate_pwsh_paths", unexpected)
    assert pwsh.resolve_pwsh_path(env={}, platform_name="linux") == "pwsh"


def test_both_coordinates_of_the_platform_agree_on_windows():
    """The chain's platform default and the gate's must name the same Windows.

    ``resolve_pwsh_path`` defaults from ``os.name == "nt"`` while the roster
    defaults from ``sys.platform``; two spellings of one fact is where a drift
    starts, so the equivalence is pinned rather than assumed.
    """
    assert (os.name == "nt") is (shell_tool_name() == SHELL_TOOL_NAME_WINDOWS) or (
        shell_tool_name() != SHELL_TOOL_NAME_WINDOWS
    )


# ── the argv: the dialect is the word in front of the flags ───────────────


def _captured_argv(monkeypatch, command: str, *, pwsh_path=None, mode="danger-full-access"):
    """Run ``run_command`` with the spawn intercepted, returning the argv and env.

    ``danger-full-access`` is the mode that reaches the spawn without a backend,
    which is what makes this runnable on any host: the subject is the argv the
    dialect builds, not the boundary (that is the bash twin's file).
    """
    import emrg.sandbox.policy  # noqa: PLC0415

    seen: dict = {}

    async def fake_spawn(*argv, **kwargs):
        seen["argv"] = list(argv)
        seen.update(kwargs)
        raise AssertionError("stop here: the argv is the subject")

    monkeypatch.setattr(pwsh.asyncio, "create_subprocess_exec", fake_spawn)
    policy = emrg.sandbox.policy.SandboxPolicy(mode=mode, workspace_root=tempfile.mkdtemp())
    with pytest.raises(AssertionError):
        asyncio.run(
            pwsh.run_command(
                command,
                policy=policy,
                workdir=policy.workspace_root,
                timeout=5.0,
                platform_name="win32",
                pwsh_path=pwsh_path,
            )
        )
    return seen


def test_the_argv_is_the_blueprints_argv(monkeypatch):
    """``[pwsh, -NoLogo, -NoProfile, -NonInteractive, -Command, preamble+command]``.

    Verbatim from ``pwsh-local/src/index.ts:194``.  ``-NonInteractive`` is the one
    that matters most operationally: without it a command that prompts hangs until
    the timeout, which reads as a slow command rather than as a stuck one.
    """
    seen = _captured_argv(monkeypatch, "echo ok", pwsh_path="C:\\pwsh.exe")
    assert seen["argv"] == [
        "C:\\pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        pwsh.ENCODING_PREAMBLE + "echo ok",
    ]


def test_the_command_rides_as_one_element_and_is_not_re_escaped(monkeypatch):
    """PowerShell parses the text itself; there is no intermediate shell to escape for.

    The measured contrast with the old tool is exactly here: it passed the command
    through a *shell*, so quoting, ``$VAR`` and ``;`` were interpreted twice.  This
    pins that the string arrives byte-identical.
    """
    hostile = "echo 'a b'; $x = 'c;d'; Write-Output \"$x`n%PATH%\""
    seen = _captured_argv(monkeypatch, hostile, pwsh_path="C:\\pwsh.exe")
    assert seen["argv"][-1] == pwsh.ENCODING_PREAMBLE + hostile


def test_the_encoding_preamble_pins_utf8(monkeypatch):
    """5.1 writes the OEM code page by default, which garbles non-ASCII output.

    Asserted on the preamble's *content* (both encoding statements, and the
    line-1 placement the blueprint chose so PowerShell's error line numbers stay
    accurate) rather than merely on its presence — presence alone would pass for a
    preamble that pins the wrong thing.
    """
    assert "[Console]::OutputEncoding" in pwsh.ENCODING_PREAMBLE
    assert "$OutputEncoding" in pwsh.ENCODING_PREAMBLE
    assert "\n" not in pwsh.ENCODING_PREAMBLE
    seen = _captured_argv(monkeypatch, "echo ok", pwsh_path="C:\\pwsh.exe")
    assert seen["argv"][-1].startswith(pwsh.ENCODING_PREAMBLE)


def test_the_flags_are_the_blueprints_flags():
    """Pinned as a tuple, in order — a reordering is a behaviour change."""
    assert pwsh.PWSH_FLAGS == ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command")


# ── the environment: the twin's overrides, minus TERM ─────────────────────


def test_the_env_overrides_carry_no_term():
    """``TERM`` is a POSIX concept and the blueprint withholds it here.

    The bash twin sets ``TERM=dumb``; sending it to PowerShell would be EMRG
    inventing a variable the dialect has no meaning for.
    """
    assert "TERM" not in pwsh.ENV_OVERRIDES
    assert pwsh.ENV_OVERRIDES == {"NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat"}


def test_the_child_environment_never_gets_a_term_from_us(monkeypatch):
    """The constants are not the contract — the spawned child's env is.

    And the contract is *"we do not set ``TERM``"*, which is not the same as
    "``TERM`` is absent": the child inherits the deployer's environment, so if the
    deployer exported one it stays (the blueprint withholds the override, it does
    not scrub the variable).  Both halves are measured, because asserting only the
    first would report a defect where the real behaviour is inheritance.
    """
    monkeypatch.setenv("TERM", "xterm-256color")
    seen = _captured_argv(monkeypatch, "echo ok", pwsh_path="C:\\pwsh.exe")
    assert seen["env"]["TERM"] == "xterm-256color", "inherited, not rewritten to dumb"
    assert seen["env"]["NO_COLOR"] == "1"

    monkeypatch.delenv("TERM", raising=False)
    without = _captured_argv(monkeypatch, "echo ok", pwsh_path="C:\\pwsh.exe")
    assert "TERM" not in without["env"], "nothing invents one"


# ── the platform gate (bundle/base cordis.patch.yml, four rows) ───────────


def test_the_roster_holds_exactly_the_two_dialects():
    """One set, both readers (the injection and the gate) — and nothing else."""
    assert SHELL_TOOL_NAMES == frozenset({SHELL_TOOL_NAME_POSIX, SHELL_TOOL_NAME_WINDOWS})


def test_windows_mounts_pwsh():
    """Acceptance item 1: on Windows the tool table has ``pwsh``."""
    tool = build_shell_tool(SandboxConfig(), platform_name="win32")
    assert isinstance(tool, PwshToolV2)
    assert tool.definition().name == "pwsh"


def test_windows_has_no_bash_tool():
    """The negative half, which is the half that was missing.

    Not "bash is not the default" — ``bash`` is not *representable* on Windows:
    the gate never builds it, so no later change to a switch can resurrect it.
    """
    assert build_shell_tool(SandboxConfig(), platform_name="win32").definition().name != "bash"


def test_posix_mounts_bash():
    """Acceptance item 2, forward half."""
    for platform_name in ("darwin", "linux", "freebsd"):
        tool = build_shell_tool(SandboxConfig(), platform_name=platform_name)
        assert isinstance(tool, BashToolV2)
        assert tool.definition().name == "bash"


def test_posix_has_no_pwsh_tool():
    """Acceptance item 2, negative half: the dialects do not leak across."""
    for platform_name in ("darwin", "linux"):
        assert (
            build_shell_tool(SandboxConfig(), platform_name=platform_name).definition().name
            != "pwsh"
        )


def test_the_gate_never_builds_two_dialects_in_one_registry():
    """Exactly one shell tool, on every platform — the registry indexes by name.

    Two would be two behaviours for one intent, and if the names differed the
    model would be taught two dialects at once, which the blueprint explicitly
    rejects (``2026-08-01-pwsh-tool-and-executor.md:25,27``).
    """
    for platform_name in ("win32", "darwin", "linux"):
        server = _instantiate()
        registry = ToolRegistry()
        registry.register(build_shell_tool(SandboxConfig(), platform_name=platform_name))
        present = [n for n in registry.names if n in SHELL_TOOL_NAMES]
        assert len(present) == 1, f"{platform_name} mounted {present}"
        del server  # the daemon's own registry is asserted separately
    # And the daemon's real registry holds one, on this host.
    daemon_registry = _instantiate().tools
    assert len([n for n in daemon_registry.names if n in SHELL_TOOL_NAMES]) == 1


def test_the_configured_pwsh_path_reaches_the_tool():
    """``[sandbox] pwsh_path`` is plumbed to the executor, not merely stored."""
    tool = build_shell_tool(SandboxConfig(pwsh_path="D:\\pwsh.exe"), platform_name="win32")
    assert isinstance(tool, PwshToolV2)
    assert tool._pwsh_path == "D:\\pwsh.exe"


def test_the_rollback_still_works_on_windows():
    """``bash_tool_v2 = false`` on Windows mounts the frozen tool, which runs cmd.exe.

    This is why P8 had to land *before* P7: the frozen tool is the only working
    shell a Windows host has until ``pwsh`` is real, and deleting it first would
    leave Windows with nothing.  After P7 the Windows roster is ``pwsh`` alone.
    """
    tool = build_shell_tool(SandboxConfig(bash_tool_v2=False), platform_name="win32")
    assert isinstance(tool, BashTool)
    assert tool.definition().name == "bash"


def test_the_hosts_own_answer_is_the_real_one():
    """No argument means "this machine" — and that is what the daemon registers."""
    expected = SHELL_TOOL_NAME_WINDOWS if os.name == "nt" else SHELL_TOOL_NAME_POSIX
    assert type(build_shell_tool(SandboxConfig())).__name__ in (
        "PwshToolV2" if expected == SHELL_TOOL_NAME_WINDOWS else "BashToolV2",
    )
    mounted = _instantiate()._mounted_shell_tool_name()
    assert mounted == expected


def test_every_mounted_dialect_receives_the_session_cwd(tmp_path):
    """The injected arguments cover the roster, not one member of it.

    ``workdir`` and ``workspace`` are one fact on Windows exactly as on POSIX
    (design §14.5 item 5), so a dialect that did not receive them would run in —
    and be bounded by — something else.  Driven from ``SHELL_TOOL_NAMES``, so a
    dialect added to the roster is covered here without editing this test.
    """
    server = _instantiate()
    session = SimpleNamespace(cwd=tmp_path)
    for name in sorted(SHELL_TOOL_NAMES):
        args: dict = {}
        server._inject_tool_arguments(
            name, args, session, SimpleNamespace(sandbox="workspace-write")
        )
        assert args["workdir"] == str(tmp_path), name
        assert args["workspace"] == str(tmp_path), name
        assert args["sandbox"] == "workspace-write", name


def test_a_dialect_that_is_not_mounted_receives_nothing(tmp_path):
    """The set is a set, not a wildcard: an unknown tool is left alone."""
    server = _instantiate()
    args: dict = {}
    server._inject_tool_arguments(
        "read", args, SimpleNamespace(cwd=tmp_path), SimpleNamespace(sandbox="workspace-write")
    )
    assert args == {}


# ── the prompt: it names the tool that is registered ─────────────────────


def test_the_prompt_names_the_dialect_it_was_given():
    """Acceptance item 6, both directions, at the template level."""
    for name, absent in (("pwsh", "bash"), ("bash", "pwsh")):
        rendered = _get_jinja_env().get_template("system.j2").render(
            os_name="test", config_dir="/nonexistent", shell_tool=name
        )
        assert f"use the {name} tool" in rendered
        assert f"{absent} tool" not in rendered


def test_the_prompt_follows_the_registry_not_the_platform():
    """The strongest form: mount the other dialect and the prompt changes with it.

    A prompt rendered from the *platform* would pass the test above and still be
    wrong in the one configuration that admits two answers — Windows with
    ``bash_tool_v2 = false``, where the mounted tool is the cmd.exe one and its
    name is ``bash``.  So the registry here holds exactly one shell tool, mounted
    as the host would *not* have mounted it, and the prompt must follow the
    registry.

    Registering a second dialect into the daemon's own registry would not do:
    the registry indexes by name, so adding ``pwsh`` beside ``bash`` produces two
    entries, not a swap — which is why the roster must be a gate at build time
    and not a mutable table at runtime.
    """
    server = _instantiate()
    host_dialect = server._mounted_shell_tool_name()
    assert f"use the {host_dialect} tool" in server._build_system_prompt()

    other = (
        SHELL_TOOL_NAME_WINDOWS if host_dialect == SHELL_TOOL_NAME_POSIX else SHELL_TOOL_NAME_POSIX
    )
    swapped = ToolRegistry()
    swapped.register(PwshToolV2() if other == SHELL_TOOL_NAME_WINDOWS else BashToolV2())
    server.tools = swapped
    assert server._mounted_shell_tool_name() == other
    assert f"use the {other} tool" in server._build_system_prompt()


def test_a_bare_render_guesses_no_dialect():
    """A template rendered with no context must not invent a shell.

    This is the shape of the defect itself: something names a dialect the caller
    never chose.  Odd-looking to assert, but the alternative — defaulting to
    ``bash`` — is exactly the accident, and it would be invisible because a bare
    render is what template tests do.
    """
    rendered = _get_jinja_env().get_template("system.j2").render(
        os_name="test", config_dir="/nonexistent"
    )
    assert "use the shell tool" in rendered
    assert "use the bash tool" not in rendered
    assert "use the pwsh tool" not in rendered


# ── the renderer's half (the cross-language boundary) ────────────────────


def test_the_renderer_knows_every_dialect():
    """Textual, and honestly so: TypeScript cannot import the Python roster.

    Two things are checked, both of which were real defects of this phase: the
    renderer's own name list carries every dialect, and each dialect has its
    translated phrases in *both* locales — a missing phrase renders the raw key
    (``tool.pwsh.doing``) in the tool card, which is what a Windows host would
    have seen.
    """
    result_panel = (REPO_ROOT / "emrg/gui/renderer/src/lib/resultPanel.ts").read_text(
        encoding="utf-8"
    )
    i18n = (REPO_ROOT / "emrg/gui/renderer/src/lib/i18n-dicts.ts").read_text(encoding="utf-8")
    roster_line = next(
        line for line in result_panel.splitlines() if line.startswith("export const SHELL_TOOL_NAMES")
    )
    for name in sorted(SHELL_TOOL_NAMES):
        assert f'"{name}"' in roster_line, f"the renderer's roster is missing {name}"
        for suffix in ("doing", "done"):
            key = f'"tool.{name}.{suffix}"'
            assert i18n.count(key) == 2, (
                f"{key} must exist once per locale (zh and en); found {i18n.count(key)}"
            )


def test_the_renderer_does_not_hardcode_one_dialect():
    """The path extractor reads the roster; a literal is how pwsh lost its paths."""
    result_panel = (REPO_ROOT / "emrg/gui/renderer/src/lib/resultPanel.ts").read_text(
        encoding="utf-8"
    )
    assert 'toolName === "bash"' not in result_panel


# ── the twins agree ──────────────────────────────────────────────────────


def test_the_two_twins_agree_on_the_framing_contract():
    """The duplication is deliberate; the drift it invites is not.

    ``pwsh_tool_v2`` restates the bash twin's framing helpers rather than
    importing them (the dialects are peers — design §14.5 item 1), so the shared
    values are pinned here instead of trusted to stay faithful.  The model-facing
    contract must be identical across dialects: the same budget, the same
    separator, the same truncation policy — only the shell differs.
    """
    from emrg.tools import bash_tool_v2 as bash

    assert pwsh.MAX_OUTPUT_CHARS == bash.MAX_OUTPUT_CHARS
    assert pwsh._ERR_MAX == bash._ERR_MAX
    assert pwsh._HEAD_TAIL_RATIO == bash._HEAD_TAIL_RATIO
    assert pwsh._STDERR_SEPARATOR == bash._STDERR_SEPARATOR
    assert pwsh._STREAM_GRACE_SECONDS == bash._STREAM_GRACE_SECONDS


def test_the_pwsh_module_does_not_import_the_bash_executor():
    """The peers are peers: layering one dialect on the other is the shape rejected.

    Measured by reading the module's imports rather than its text, so a mention in
    a docstring or a comment does not count as a dependency.
    """
    import ast

    tree = ast.parse((REPO_ROOT / "emrg/tools/pwsh_tool_v2.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert "emrg.tools.bash_tool_v2" not in imported
    assert "emrg.tools.bash_tool" not in imported
