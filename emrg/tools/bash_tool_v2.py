"""Bash tool v2 — the command runs confined at the process boundary.

Blueprint: ``.emrg/designs/bash-tool-v2-design.md`` (P1, macOS).  This module is
the consumer half of deepseek-harness's ``packages/shell/bash-sandbox`` (which
short-circuits, wraps and classifies) plus ``packages/shell/tool-bash`` (which
renders), translated one for one:

* the policy is resolved once, at this boundary (``policy.resolve_policy``);
* the exact argv about to be spawned is handed to the confinement seam
  (``["bash", "-c", command]`` — the dialect contract, not a shell string);
* the run is classified from a **structured** result (``result.sandbox``) with
  runner failure outranking denial, because a runner that failed means the
  command never ran;
* the model-facing text is rendered from that result, never by appending prose
  to stderr.

The old ``emrg/tools/bash_tool.py`` is frozen while this is built beside it
(delivery rules R1/R2, design §1.6): nothing here imports it, and it keeps
serving until the switch is flipped (D10) and the file is deleted (P7).  That is
why the output framing and decoding below are this module's own copy rather than
calls into the old one — the copy is what survives P7.

Two behaviours are deliberately NOT the blueprint's, and both are registered in
the design rather than quietly assumed: Linux runs unconfined until P3
(``providers.unconfined_mode``, deviation D4) and the tier vocabulary keeps
EMRG's task-level default (``policy.DEFAULT_MODE``).
"""

from __future__ import annotations

import asyncio
import locale
import logging
import os
import re
import signal
import tempfile
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
from emrg.sandbox.roots import canonical_path, writable_roots
from emrg.sandbox.providers import unconfined_mode
from emrg.server.git_utils import no_prompt_env
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools.base import ToolExecutor

logger = logging.getLogger(__name__)

# Package caches under a confined run: the rule itself now lives in
# ``emrg/tools/shell_env.py``, because the pwsh dialect needs the same rule and
# may not import this module (design §14.5 item 1).  The two names stay
# importable from here — this module's tests and the switch's call site read
# them by this path, and re-exporting is what keeps one definition while
# leaving that surface where it was.
from emrg.tools.shell_env import CACHE_ENV as _CACHE_ENV  # noqa: F401  (re-export)
from emrg.tools.shell_env import confined_env, runner_import_env  # noqa: F401  (re-export)


#: Output budget and framing, unchanged from the old tool: keep stderr intact
#: (errors are critical) and truncate stdout head+tail, so build/test failures
#: at the tail survive.  ``framing supports up to 16MB`` is the API ceiling, not
#: this budget.
MAX_OUTPUT_CHARS = 200_000
_ERR_MAX = 30_000
_HEAD_TAIL_RATIO = 0.6
_STDERR_SEPARATOR = "\n[stderr]\n"

#: How long a killed run's pipes get to close before the reader is abandoned.
_STREAM_GRACE_SECONDS = 1.0

#: The dialect.  The model-facing contract is the dialect itself, so the inner
#: program is named here once and always: the old tool advertised bash while
#: ``create_subprocess_shell`` ran ``/bin/sh`` (dash on Linux), which is a
#: measured contract violation, not a detail (blueprint §3.6).
_INNER_SHELL = "bash"


@dataclass
class ShellRunResult:
    """One finished confined run — the structured half of the result surface.

    ``sandbox`` is the blueprint's ``result.sandbox``: ``{mode, denied}`` alone
    when the run was unconfined (no ``enforcement`` field exists to be read as a
    promise that was never made), and ``{mode, denied, enforcement}`` when a
    backend confined it.
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
    :returns: the first matching fatal line, or ``None`` when the evidence is
        insufficient.
    """
    if exit_code is None or exit_code == 0:
        return None
    lines = re.split(r"\r?\n", stderr)
    for rule in rules:
        if rule.allowed_exit_codes is not None and exit_code not in rule.allowed_exit_codes:
            continue
        informational = {line.lower() for line in rule.informational_lines}
        # An empty or whitespace-only substring is not evidence; ignore it while
        # keeping any valid signature beside it active.
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
    so a bad workdir can never be reported as a sandbox problem.  The workdir is
    checked at classification time, not atomically with the spawn.

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
        # Python sets ``filename`` for every exec failure, so its absence means
        # the error is not attributed to a program at all — not a runner failure.
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

    :param result: the completed run.
    :param escalation_modes: the escalation targets this composition advertises;
        non-empty would add the same-turn hint after a denial marker.  Empty
        here: EMRG does not advertise escalation yet (design §1.5 A5, phase P5),
        and the blueprint adds that line only when a composition advertises it.
    :returns: the model-facing text.
    """
    stdout, stderr = _fit_streams(result.stdout, result.stderr)

    body = stdout
    if stderr:
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"[stderr]\n{stderr}"
    if not body:
        body = "(no output)"

    markers: list[str] = []
    if result.sandbox.get("denied"):
        markers.append(sandbox_denial_marker(str(result.sandbox.get("mode", ""))))
        for _mode in escalation_modes:  # pragma: no cover - no composition advertises yet
            raise NotImplementedError("escalation is phase P5")
    if result.timed_out:
        markers.append(f"[timed out after {result.timeout_ms}ms]")
    if result.signal is not None:
        markers.append(f"[killed by signal: {result.signal}]")
    elif result.exit_code != 0:
        markers.append(f"[exit code: {result.exit_code}]")

    if not markers:
        return body
    if not body.endswith("\n"):
        body += "\n"
    return body + "\n".join(markers)


def _fit_streams(stdout: str, stderr: str) -> tuple[str, str]:
    """Bound both streams to one budget, keeping stderr whole when it can.

    Same policy as the old tool this copy survives: stderr is critical, so it
    keeps its own cap and stdout absorbs what is left; if stderr leaves stdout
    almost nothing, stderr is cut back rather than starving it.  Every truncation
    says so in the text, because a silent one reads as a short output.

    :param stdout: the collected stdout.
    :param stderr: the collected stderr.
    :returns: the two streams, bounded.
    """
    stderr = _truncate_stderr(stderr)
    separator = len(_STDERR_SEPARATOR) if (stdout and stderr) else 0
    remaining = MAX_OUTPUT_CHARS - len(stderr) - separator
    if remaining < 2000 and stderr:
        remaining = MAX_OUTPUT_CHARS // 2
        stderr = stderr[:remaining] + "\n\n... [stderr truncated to make room for stdout]"
    if len(stdout) > remaining:
        stdout = _truncate_stdout(stdout, remaining)
    return stdout, stderr


def _truncate_stderr(stderr: str) -> str:
    """Bound stderr, keeping both ends.

    :param stderr: the collected stderr.
    :returns: stderr at most ``_ERR_MAX`` characters.
    """
    if len(stderr) <= _ERR_MAX:
        return stderr
    half = _ERR_MAX // 2
    return (
        f"{stderr[:half]}\n\n"
        f"... [stderr truncated: {len(stderr)} → {_ERR_MAX} chars, head+tail kept]"
        f"\n\n{stderr[-half:]}"
    )


def _truncate_stdout(stdout: str, remaining: int) -> str:
    """Bound stdout to ``remaining``, keeping head and tail.

    :param stdout: the collected stdout.
    :param remaining: the characters left in the budget.
    :returns: stdout within that budget.
    """
    head_chars = int(remaining * _HEAD_TAIL_RATIO)
    tail_chars = remaining - head_chars - 200
    if tail_chars < 500:
        return stdout[: remaining - 50] + f"\n\n... [stdout truncated: {len(stdout)} → {remaining} chars]"
    return (
        f"{stdout[:head_chars]}\n\n"
        f"... [{len(stdout) - remaining} chars omitted] ..."
        f"\n\n{stdout[-tail_chars:]}"
    )


def _decode_output(data: bytes, os_name: str | None = None) -> str:
    """Decode subprocess output without corrupting non-UTF-8 text.

    POSIX output is UTF-8.  Windows console output uses the console code page
    while git/gh emit UTF-8, so the locale codec is tried strictly first there
    and UTF-8 second, with replacement as the last resort (rant
    2026-08-08T09:35:30: U+FFFD garbage from decoding GBK bytes as UTF-8).

    :param data: the bytes read from the pipe.
    :param os_name: injectable for tests; defaults to ``os.name``.
    :returns: the decoded text.
    """
    if not data:
        return ""
    if (os_name or os.name) == "nt":
        codec = locale.getpreferredencoding(False) or "utf-8"
        for candidate in (codec, "utf-8"):
            try:
                return data.decode(candidate)  # strict
            except (LookupError, UnicodeDecodeError):
                continue
    return data.decode("utf-8", errors="replace")


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the whole process group so no descendant outlives the run.

    :param proc: the running process.
    """
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def run_command(
    command: str,
    *,
    policy: SandboxPolicy,
    workdir: str,
    timeout: float,
    platform_name: str | None = None,
) -> ShellRunResult:
    """Run one command under ``policy`` and return the structured result.

    :param command: the command source, passed as a single argv element to the
        inner shell (never re-parsed, never re-quoted).
    :param policy: the file-effect policy this execution runs under.
    :param workdir: the session's working directory — also the writable
        boundary, one identity, not two.
    :param timeout: seconds to wait before killing the process group.
    :param platform_name: the platform to confine for; defaults to the host's.
    :returns: the finished run, with ``sandbox`` describing what confined it.
    :raises SandboxUnavailableError: when confinement was requested and could not
        be established or could not run — the command did not run, and no
        unconfined fallback is attempted.
    """
    inner_argv = [_INNER_SHELL, "-c", command]
    effective_mode = unconfined_mode(policy.mode, platform_name=platform_name)
    confined: ConfinedArgv | None = None
    if effective_mode is None:
        confined = confine(inner_argv, policy, platform_name=platform_name)
        argv = confined.argv
    else:
        argv = inner_argv

    timeout_ms = int(float(timeout) * 1000)
    logger.debug(
        "bash v2: mode=%s effective=%s workdir=%s argv=%r",
        policy.mode,
        effective_mode or policy.mode,
        workdir,
        argv[:4],
    )
    # The child's environment: the caller's, with interactive git prompts off
    # (that is the old tool's behaviour, kept) and — when this run really is
    # confined — the two things the boundary itself needs from it: the package
    # caches relocated into the run's granted temp area, and the runner's own
    # import root (see ``confined_env`` / ``runner_import_env``).
    child_env = no_prompt_env()
    if confined is not None:
        child_env.update(confined_env(policy))
        child_env.update(runner_import_env())
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
        # Runner failure outranks denial: the command did not run, so calling
        # this a refusal by the policy would name the wrong event.
        failure = classify_runner_failure(result.exit_code, result.stderr, confined.runner_failure_rules)
        if failure is not None:
            raise SandboxUnavailableError(policy.mode, failure)
        result.sandbox = {
            "mode": policy.mode,
            "denied": matches_signature(result.exit_code, result.stderr, confined.denial_signatures),
            "enforcement": confined.enforcement,
        }
    else:
        # No `enforcement` field: the run was not confined, and a field here
        # could only claim a boundary that does not exist.
        result.sandbox = {"mode": effective_mode, "denied": False}
    return result


async def _collect(
    proc: asyncio.subprocess.Process, timeout: float, timeout_ms: int
) -> ShellRunResult:
    """Wait for a process, kill it on timeout, and collect what it produced.

    Reading is not left to a cancelled ``communicate()``: measured on this host,
    a cancelled read loses the bytes already buffered, so the shape here waits on
    the three tasks together and drains them **after** the kill instead — a
    timed-out command still reports the output it managed to write.  A command
    that leaves a descendant holding the pipe open is cut the same way the old
    tool cut it, and reported as a timeout, because its output never closed.

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
            # The streams never closed (a surviving descendant holds them open):
            # the same fate the old tool gave this shape, and the output read so
            # far is the reader's, not ours to keep.
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


class BashToolV2(ToolExecutor):
    """Execute shell commands confined at the process boundary."""

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bash",
            description=(
                "Execute a shell command and return stdout and stderr. "
                "Use for running tests, git commands, listing files, "
                "installing packages, and other shell operations. The command "
                "runs under `bash -c`, and the kernel confines it to the "
                "session's working directory: writes outside it (and outside "
                "the OS temp area) are refused whatever language or subprocess "
                "attempts them."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute (bash syntax, "
                        "heredocs included).",
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
            return ToolResult(name="bash", content="Error: no command provided", error=True)
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
                command, policy=policy, workdir=workdir, timeout=timeout
            )
        except SandboxUnavailableError as exc:
            logger.warning("bash v2: refusing to run unconfined: %s", exc)
            return ToolResult(name="bash", content=f"⛔ {exc}", error=True)
        except OSError as exc:
            logger.warning("bash v2: %s", exc)
            return ToolResult(name="bash", content=f"Error: {exc}", error=True)
        return ToolResult(name="bash", content=render_result(result), error=False)


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
