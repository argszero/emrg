"""PowerShell tool (bash tool v2, P8) — the Windows dialect at the same boundary.

Blueprint: ``.emrg/designs/bash-tool-v2-design.md`` §14.  This module is the twin
of ``emrg/tools/bash_tool_v2.py``, translated from deepseek-harness's
``packages/shell/pwsh-local`` (the executor: dialect, argv, encoding preamble,
environment overrides) plus ``packages/shell/tool-pwsh`` (the model-visible tool
and its Windows contracts).  The blueprint mounts the two as **peer tool
families** and gates them at composition time — ``bash`` on POSIX, ``pwsh`` on
Windows — with the rule stated in its own Agent Note: *the model-visible
contract is the dialect itself*.  So there is no "called bash, runs pwsh" and no
"called bash, but Windows has no bash": the mounted tool's name, description and
argv are all the dialect.

Why this file exists (the accident it repairs, measured): the old tool's
subprocess shell on Windows is ``cmd.exe`` via ``COMSPEC`` (``bash_tool.py:33``),
so it never needed ``bash`` to exist; v2 spawns ``bash -c`` and P6 made it the
default, so a Windows host ran ``['bash', '-c', 'echo ok']`` and got
``[WinError 2] 系统找不到指定的文件`` — a boundary with no usable dialect.  The
blueprint cannot reach that state, for three structural reasons, and this module
is the third of them: the roster is composition data (``daemon.build_shell_tool``),
the side that owns an executable-resolution chain is the side that ships on
Windows (``resolve_pwsh_path`` below), and the tool's identity is its dialect.

Deliberate duplication: this file restates the framing, truncation, decoding and
render helpers of ``bash_tool_v2.py`` instead of importing them, exactly as the
blueprint's ``pwsh-local`` mirrors ``bash-local`` call for call (its sources
carry ``jscpd:ignore`` markers saying so).  The bargain is explicit: the two
files may never disagree about the model-facing contract, and a test —
``tests/test_pwsh_tool_v2.py::test_the_two_twins_agree_on_the_framing_contract``
— pins the shared constants rather than trusting the copy to stay faithful.

Ported, NOT verified on Windows hardware (design §14.6): the restricted-token
contracts below (ConstrainedLanguage under ``read-only``; named-pipe ``EPERM``
when capturing a program's output under either confined mode) are copied from
the blueprint's ``tool-pwsh`` description, and the backend that produces them
(``emrg/sandbox/win32/token.py``, P4) is itself a port.  They are stated in the
tool description because the model needs them to interpret an ``EPERM``
correctly, and they are marked here so that no reader mistakes a port for a
measurement.  Nothing in this module spawns ``pwsh`` in a test.
"""

from __future__ import annotations

import asyncio
import logging
import ntpath
import os
import re
import signal
from dataclasses import dataclass, field

from emrg._win import win32_no_window_kwargs
from emrg.sandbox.contract import (
    ConfinedArgv,
    RunnerFailureRule,
    SandboxUnavailableError,
    confine,
    sandbox_denial_marker,
)
from emrg.sandbox.policy import SandboxPolicy, resolve_policy
from emrg.sandbox.providers import unconfined_mode
from emrg.server.git_utils import no_prompt_env
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor
from emrg.tools.shell_env import confined_env

logger = logging.getLogger(__name__)

#: The tool's name — the dialect, which is the whole contract (blueprint §14.5).
TOOL_NAME = "pwsh"

#: UTF-8 output pinning prepended to every command (``pwsh-local/src/index.ts:48``).
#: The collector decodes bytes as UTF-8, but Windows PowerShell 5.1 — the
#: last-resort fallback in the resolution chain below — writes the console/OEM
#: code page by default, which garbles non-ASCII output; pwsh 7 is unaffected.
#: The statements ride on line 1 after ``; `` separators so PowerShell error line
#: numbers stay accurate.
ENCODING_PREAMBLE = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
)

#: The argv flags between the executable and the command, in the blueprint's
#: order (``pwsh-local/src/index.ts:194``): no banner, no profile, no
#: interactive prompts, then the command string as ONE element — PowerShell
#: itself parses it, and no intermediate shell exists to escape for.
PWSH_FLAGS: tuple[str, ...] = ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command")

#: Model-friendly environment overrides (``pwsh-local/src/index.ts:34-38``).
#: ``TERM=dumb`` is deliberately **absent**: it is a POSIX concept, and the
#: blueprint gives it to the bash executor and withholds it here.
ENV_OVERRIDES: dict[str, str] = {
    "NO_COLOR": "1",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
}


# ── the executable resolution chain (pwsh-local/src/resolve.ts) ────────────


def candidate_pwsh_paths(env: dict | None = None) -> list[str]:
    """Well-known PowerShell locations plus PATH entries, newest first.

    Verbatim from ``resolve.ts:21-37``, including the two details that are easy
    to lose in translation: PATH entries may carry surrounding quotes (``setx``
    writes them), and Windows PowerShell **5.1 is the last-resort fallback** —
    every Windows host has it, so "the executable is missing" is not a state the
    Windows roster can reach.

    ``ntpath`` rather than ``os.path``, and the reason is not decoration: this
    chain describes *Windows* paths, so it must not inherit the semantics of the
    host it happens to be running on.  Composed with ``os.path`` it produced
    ``C:\\a/pwsh.exe`` off Windows and ``C:/a;b`` separators, i.e. a correct
    answer only because it is only ever *called* on Windows — an implicit coupling
    that also made it unassertable from a POSIX host, which is where the test that
    measured this runs.  Naming the semantics makes the function's output a
    property of its arguments, which is what its docstring claims.

    :param env: the environment to probe; defaults to ``os.environ``.
    :returns: candidate paths in resolution order.
    """
    env = os.environ if env is None else env
    program_files = env.get("ProgramFiles") or "C:\\Program Files"
    system_root = env.get("SystemRoot") or "C:\\Windows"
    candidates = [ntpath.join(program_files, "PowerShell", "7", "pwsh.exe")]
    # Microsoft Store installs (and any user-added location) live on PATH.
    for entry in (env.get("PATH") or "").split(ntpath.pathsep):
        trimmed = entry.strip().strip('"')
        if not trimmed:
            continue
        candidates.append(ntpath.join(trimmed, "pwsh.exe"))
    candidates.append(
        ntpath.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    )
    return candidates


def _candidate_exists(candidate: str) -> bool:
    """Whether a candidate can be spawned, without following reparse points.

    ``os.path.lexists`` is the ``lstat`` of the blueprint's ``candidateExists``
    (``resolve.ts:46-56``) and the distinction is load-bearing: the Microsoft
    Store's app execution alias is a reparse point, and following it hits the
    target's ACL (``EACCES``) where ``lstat`` sees the entry itself.  A directory
    never matches, and every probe failure answers ``False`` — an unprobeable
    path is not a spawnable one.

    :param candidate: the path to probe.
    :returns: whether it exists as a file or link.
    """
    try:
        if not os.path.lexists(candidate):
            return False
        # lexists says "the entry is there"; a directory is not an executable.
        return not os.path.isdir(candidate) or os.path.islink(candidate)
    except OSError:
        return False


def resolve_pwsh_path(
    configured: str | None = None,
    env: dict | None = None,
    platform_name: str | None = None,
) -> str:
    """The ``pwsh`` executable this tool spawns — a pure function of its inputs.

    Order (``resolve.ts:67-79``): an explicit configuration value is trusted
    as-is; on Windows the well-known candidates are probed in order; otherwise
    the bare name ``pwsh``, which leaves resolution to ``PATH``.  Parameterised
    by env and platform so every candidate and every fallback is testable
    without spawning anything.

    :param configured: the ``[sandbox] pwsh_path`` value, if the deployer set one.
    :param env: the environment to probe; defaults to ``os.environ``.
    :param platform_name: the platform to resolve for; defaults to the host's.
    :returns: the executable to spawn.
    """
    if configured:
        return configured
    platform_name = platform_name or ("win32" if os.name == "nt" else "posix")
    if platform_name == "win32":
        for candidate in candidate_pwsh_paths(env):
            if _candidate_exists(candidate):
                return candidate
    return "pwsh"


# ── output framing: the same contract values as the bash twin ──────────────


#: Same budget and framing as ``bash_tool_v2``: keep stderr intact (errors are
#: critical) and truncate stdout head+tail, so build/test failures at the tail
#: survive.  ``_HEAD_TAIL_RATIO`` of the *end* is kept.  A test pins the equality
#: rather than leaving two copies to drift.
MAX_OUTPUT_CHARS = 200_000
_ERR_MAX = 30_000
_HEAD_TAIL_RATIO = 0.6
_STDERR_SEPARATOR = "\n[stderr]\n"

#: How long a killed run's pipes get to close before the reader is abandoned.
_STREAM_GRACE_SECONDS = 1.0


@dataclass
class ShellRunResult:
    """One finished confined run — the structured result surface, as in bash v2.

    ``sandbox`` is ``{mode, denied}`` when the run was unconfined (no
    ``enforcement`` field exists to be read as a promise never made) and
    ``{mode, denied, enforcement}`` when a backend confined it.
    """

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    signal: int | None = None
    timed_out: bool = False
    timeout_ms: int = 0
    sandbox: dict = field(default_factory=dict)


def classify_runner_failure(
    exit_code: int | None,
    stderr: str,
    rules: tuple[RunnerFailureRule, ...],
) -> str | None:
    """The fatal stderr line proving the runner failed, or ``None``.

    Each rule requires a nonzero exit, its optional exit-code gate, and a fatal
    signature on one stderr line after exact informational lines are excluded.
    Exit status alone never proves runner failure.

    :param exit_code: the process's exit code; ``None`` means signal death.
    :param stderr: collected stderr text, left unchanged.
    :param rules: structured runner-failure rules from the active wrap.
    :returns: the first matching fatal line, or ``None``.
    """
    if exit_code is None or exit_code == 0:
        return None
    lines = re.split(r"\r?\n", stderr)
    for rule in rules:
        if rule.allowed_exit_codes is not None and exit_code not in rule.allowed_exit_codes:
            continue
        informational = {line.lower() for line in rule.informational_lines}
        signatures = [sig.lower() for sig in rule.fatal_signatures if sig.strip()]
        for line in lines:
            lowered = line.lower()
            if lowered in informational:
                continue
            if any(signature in lowered for signature in signatures):
                return line
    return None


def matches_signature(
    exit_code: int | None, stderr: str, signatures: tuple[str, ...]
) -> bool:
    """Whether a non-zero exit's stderr carries a denial in this backend's dialect.

    :param exit_code: the process's exit code; ``None`` means signal death.
    :param stderr: collected stderr text.
    :param signatures: the selected backend's denial dialect.
    :returns: whether this exit is a denial by signature.
    """
    if exit_code is None or exit_code == 0:
        return False
    lowered = stderr.lower()
    return any(signature.lower() in lowered for signature in signatures)


def is_runner_spawn_failure(error: BaseException, runner_program: str | None, workdir: str) -> bool:
    """Whether a spawn rejection identifies the *runner* as the missing program.

    Only ``ENOENT``/``EACCES`` count, only when the error names exactly
    ``argv[0]``, and only with the caller-owned cwd independently ruled usable —
    so a bad workdir can never be reported as a sandbox problem.

    :param error: the original spawn rejection.
    :param runner_program: the provider's ``argv[0]``, the executable that
        establishes confinement.
    :param workdir: the caller-owned spawn cwd.
    :returns: whether the rejection carries executable-specific runner evidence.
    """
    if not runner_program or not _is_usable_workdir(workdir):
        return False
    if not isinstance(error, OSError):
        return False
    if error.errno not in (2, 13):  # ENOENT, EACCES
        return False
    filename = error.filename
    if filename is None:
        return False
    return os.fspath(filename) == runner_program


def _is_usable_workdir(path: str) -> bool:
    """Whether the caller-owned spawn cwd can be entered.

    :param path: the cwd the caller asked for.
    :returns: whether it is an existing, traversable directory.
    """
    return os.path.isdir(path) and os.access(path, os.X_OK)


def render_result(result: ShellRunResult, escalation_modes: tuple[str, ...] = ()) -> str:
    """Shape one finished run into the text the model sees.

    Output body, then the markers, in the blueprint's order: the denial marker
    only when the run was denied, then timeout or signal, and the exit marker
    **last** because it is the anchor a reader greps for.  Non-zero exits are
    reported, not errored — the model decides how to react.

    One Windows asymmetry is stated in the tool description rather than handled
    here: a force-killed process there settles as a positive exit code, so it is
    reported as ``[exit code: 1]`` with no signal marker — a fact about the
    platform, not a second rendering rule (blueprint §14.2 layer 5).

    :param result: the completed run.
    :param escalation_modes: the escalation targets this composition advertises;
        empty here, because EMRG does not advertise escalation yet (design §1.5
        A5, phase P5).
    :returns: the model-facing text.
    """
    stdout, stderr = _fit_streams(result.stdout, result.stderr)

    body = stdout
    if stderr:
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"[stderr]\n{stderr}"

    markers: list[str] = []
    if result.sandbox.get("denied"):
        mode = result.sandbox.get("mode", "")
        markers.append(f"[sandbox: file access denied under {mode} mode]")
        markers.append(sandbox_denial_marker(mode))
    if result.timed_out:
        markers.append(f"[timed out after {result.timeout_ms}ms]")
    elif result.signal is not None:
        markers.append(f"[killed by signal: {result.signal}]")
    if result.exit_code is not None:
        markers.append(f"[exit code: {result.exit_code}]")

    if not markers:
        return body
    return body + "\n" + "\n".join(markers) if body else "\n".join(markers)


def _fit_streams(stdout: str, stderr: str) -> tuple[str, str]:
    """Fit both streams into the output budget, stderr first.

    :param stdout: the raw stdout text.
    :param stderr: the raw stderr text.
    :returns: the two texts, each fitted.
    """
    stderr = _truncate_stderr(stderr)
    remaining = MAX_OUTPUT_CHARS - len(stderr) - len(_STDERR_SEPARATOR)
    return _truncate_stdout(stdout, max(remaining, 0)), stderr


def _truncate_stderr(stderr: str) -> str:
    """Cut stderr to its own cap, keeping the head.

    :param stderr: the raw stderr text.
    :returns: the text, unchanged when it fits.
    """
    if len(stderr) <= _ERR_MAX:
        return stderr
    return stderr[:_ERR_MAX] + f"\n... [stderr truncated, {len(stderr)} chars total]"


def _truncate_stdout(stdout: str, remaining: int) -> str:
    """Cut stdout head+tail so a build's failing tail survives.

    :param stdout: the raw stdout text.
    :param remaining: the characters left in the budget.
    :returns: the text, unchanged when it fits.
    """
    if len(stdout) <= remaining:
        return stdout
    if remaining <= 0:
        return f"... [stdout truncated, {len(stdout)} chars total]"
    head = int(remaining * _HEAD_TAIL_RATIO)
    tail = remaining - head
    omitted = len(stdout) - head - tail
    return (
        stdout[:head]
        + f"\n... [{omitted} chars omitted] ...\n"
        + stdout[len(stdout) - tail:]
    )


def _decode_output(data: bytes, os_name: str | None = None) -> str:
    """Decode a stream while keeping one unreadable byte from losing the run.

    :param data: the raw bytes.
    :param os_name: the platform whose console code page to fall back on.
    :returns: the decoded text.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate a spawned run's whole group, then the process itself.

    :param proc: the spawned process.
    """
    if proc.returncode is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        logger.debug("killing the run's process group failed", exc_info=True)


async def run_command(
    command: str,
    *,
    policy: SandboxPolicy,
    workdir: str,
    timeout: float,
    platform_name: str | None = None,
    pwsh_path: str | None = None,
    env: dict | None = None,
) -> ShellRunResult:
    """Run one PowerShell command under ``policy`` and return the result.

    :param command: the command source, passed as a single argv element after the
        flags (never re-parsed, never re-quoted — PowerShell parses it).
    :param policy: the file-effect policy this execution runs under.
    :param workdir: the session's working directory — also the writable
        boundary, one identity, not two.
    :param timeout: seconds to wait before killing the process group.
    :param platform_name: the platform to confine for; defaults to the host's.
    :param pwsh_path: the configured executable, if the deployer set one.
    :param env: the environment the resolution chain probes; defaults to
        ``os.environ``.
    :returns: the finished run, with ``sandbox`` describing what confined it.
    :raises SandboxUnavailableError: when confinement was requested and could not
        be established or could not run — the command did not run, and no
        unconfined fallback is attempted.
    """
    executable = resolve_pwsh_path(pwsh_path, env=env, platform_name=platform_name)
    inner_argv = [executable, *PWSH_FLAGS, ENCODING_PREAMBLE + command]
    effective_mode = unconfined_mode(policy.mode, platform_name=platform_name)
    confined: ConfinedArgv | None = None
    if effective_mode is None:
        confined = confine(inner_argv, policy, platform_name=platform_name)
        argv = confined.argv
    else:
        argv = inner_argv

    timeout_ms = int(float(timeout) * 1000)
    logger.debug(
        "pwsh v2: mode=%s effective=%s workdir=%s argv=%r",
        policy.mode,
        effective_mode or policy.mode,
        workdir,
        argv[:3],
    )
    # The child's environment: the caller's with interactive git prompts off,
    # the dialect's own colour/pager overrides (no `TERM` — see ENV_OVERRIDES),
    # and — when this run really is confined and the mode grants somewhere to
    # write — the package caches relocated into the granted temp area.  The
    # relocation is EMRG's own measure (the blueprint's deployer sets caches
    # from their own shell); it is shared with the bash twin through
    # `confined_env`, so the two dialects cannot disagree about where a cache
    # may live.
    child_env = no_prompt_env()
    child_env.update(ENV_OVERRIDES)
    if confined is not None:
        child_env.update(confined_env(policy))
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=child_env,
            preexec_fn=os.setsid if os.name != "nt" else None,
            **win32_no_window_kwargs(),
        )
    except OSError as exc:
        if confined is not None and is_runner_spawn_failure(exc, confined.argv[0], workdir):
            raise SandboxUnavailableError(policy.mode, str(exc)) from exc
        raise

    result = await _collect(proc, timeout, timeout_ms)
    if confined is not None:
        failure = classify_runner_failure(result.exit_code, result.stderr, confined.runner_failure_rules)
        if failure is not None:
            raise SandboxUnavailableError(policy.mode, failure)
        result.sandbox = {
            "mode": policy.mode,
            "denied": matches_signature(result.exit_code, result.stderr, confined.denial_signatures),
            "enforcement": confined.enforcement,
        }
    else:
        result.sandbox = {"mode": effective_mode, "denied": False}
    return result


async def _collect(
    proc: asyncio.subprocess.Process, timeout: float, timeout_ms: int
) -> ShellRunResult:
    """Wait for a process, kill it on timeout, and collect what it produced.

    :param proc: the spawned process.
    :param timeout: seconds to wait before killing the group.
    :param timeout_ms: the same bound in milliseconds, for the marker.
    :returns: the collected run.
    """
    stdout_task = asyncio.ensure_future(proc.stdout.read())
    stderr_task = asyncio.ensure_future(proc.stderr.read())
    wait_task = asyncio.ensure_future(proc.wait())
    _, pending = await asyncio.wait({stdout_task, stderr_task, wait_task}, timeout=timeout)
    timed_out = wait_task in pending
    if pending:
        _kill_process_group(proc)
        _, still_pending = await asyncio.wait(pending, timeout=_STREAM_GRACE_SECONDS)
        if still_pending:
            timed_out = True
        for task in still_pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    exit_code: int | None = None
    signal_number: int | None = None
    returncode = proc.returncode
    if returncode is not None:
        if returncode < 0:
            signal_number = -returncode
        else:
            exit_code = returncode

    return ShellRunResult(
        stdout=_decode_output(stdout_task.result() if stdout_task.done() else b"").rstrip(),
        stderr=_decode_output(stderr_task.result() if stderr_task.done() else b"").rstrip(),
        exit_code=exit_code,
        signal=signal_number,
        timed_out=timed_out,
        timeout_ms=timeout_ms,
    )


class PwshToolV2(ToolExecutor):
    """Execute PowerShell commands confined at the process boundary (Windows).

    The tool's identity is the dialect: this is mounted instead of ``bash`` on
    Windows, so the model is never told one language and given another.
    """

    def __init__(self, pwsh_path: str | None = None) -> None:
        """Create the tool.

        :param pwsh_path: the ``[sandbox] pwsh_path`` value; ``None`` means the
            resolution chain decides at call time.
        """
        self._pwsh_path = pwsh_path

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=TOOL_NAME,
            description=(
                "Execute a PowerShell command and return stdout and stderr. "
                "Use for running tests, git commands, listing files, "
                "installing packages, and other shell operations. The command "
                "runs under `pwsh -Command`: PowerShell parses the text itself, "
                "with no intermediate shell. Each call is a fresh process — no "
                "state (working directory, variables, functions) persists "
                "between calls, so pass `workdir` instead of using `cd`. Paths "
                "use native Windows form (`C:\\...`); read environment variables "
                "with `$env:NAME`. Non-zero exits are reported as "
                "`[exit code: N]`. The kernel confines the run to the session's "
                "working directory: a write outside it (and outside the OS temp "
                "area) is refused whatever language or subprocess attempts it, "
                "and is reported as a sandbox denial — that is the policy, not a "
                "bug in the command, so do not retry another way. Long output is "
                "truncated: stderr to its head, stdout head and tail. On Windows "
                "a force-killed command settles as `[exit code: 1]` with no "
                "signal marker — treat it as an interruption, not a command "
                "failure. Under the restricted-token sandbox, a `read-only` run "
                "is in PowerShell ConstrainedLanguage mode, where `.NET` static "
                "calls, `Add-Type`, COM and reflection fail with \"only core "
                "types\" — prefer cmdlets and core types (`[string]`, "
                "`[datetime]`, `[regex]`, `[guid]`); and under either confined "
                "mode programs cannot open named pipes, so capturing another "
                "program's output through piped stdio fails with EPERM, while "
                "PowerShell's own pipelines are unaffected."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The PowerShell command to execute "
                        "(PowerShell syntax; no intermediate shell).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 30).",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Working directory for the command. The daemon "
                        "supplies the session's working directory, which is also the "
                        "writable boundary; passing another value does not widen it.",
                    },
                    "intent": {
                        "type": "string",
                        "description": "The purpose of this call: why you are invoking it and what you want to achieve. "
                        "One human-readable sentence, e.g. 'check how billing is implemented in billing.rs'.",
                    },
                },
                "required": ["command", "intent"],
            },
        )

    async def execute(self, arguments: dict) -> ToolResult:
        command = arguments.get("command", "")
        if not command:
            return ToolResult(name=TOOL_NAME, content="Error: no command provided", error=True)
        timeout = _as_timeout(arguments.get("timeout"))
        workdir = str(
            arguments.get("workspace") or arguments.get("workdir") or os.getcwd()
        )
        policy = resolve_policy(
            mode=arguments.get("sandbox"),
            workspace_root=workdir,
            session_id=arguments.get("session_id"),
        )
        try:
            result = await run_command(
                command,
                policy=policy,
                workdir=workdir,
                timeout=timeout,
                pwsh_path=self._pwsh_path,
            )
        except SandboxUnavailableError as exc:
            logger.warning("pwsh v2: refusing to run unconfined: %s", exc)
            return ToolResult(name=TOOL_NAME, content=f"⛔ {exc}", error=True)
        except OSError as exc:
            logger.warning("pwsh v2: %s", exc)
            return ToolResult(name=TOOL_NAME, content=f"Error: {exc}", error=True)
        return ToolResult(name=TOOL_NAME, content=render_result(result), error=False)


def _as_timeout(value: object) -> float:
    """Read the timeout argument, falling back to the default.

    :param value: whatever the model sent.
    :returns: the timeout in seconds (30 when unusable, as the schema documents).
    """
    if isinstance(value, bool) or value is None:
        return 30.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 30.0
