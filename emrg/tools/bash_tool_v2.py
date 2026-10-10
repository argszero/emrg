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

The old ``emrg/tools/bash_tool.py`` was frozen while this was built beside it
(delivery rules R1/R2, design §1.6), and P7 (issue #1675) has now deleted it:
this module and ``pwsh_tool_v2.py`` are the whole shell-tool layer, and nothing
anywhere imports the old file.  The output framing and decoding below are this
module's own copy rather than calls into it — which is why the copy is what
survived.

Two behaviours are deliberately NOT the blueprint's, and both are registered in
the design rather than quietly assumed: Linux runs unconfined until P3
(``providers.unconfined_mode``, deviation D4) and the tier vocabulary keeps
EMRG's task-level default (``policy.DEFAULT_MODE``).
"""

from __future__ import annotations

import asyncio
import locale
import math
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
    never_announced_detail,
    never_started_detail,
    sandbox_denial_marker,
    without_start_announcements,
)
from emrg.sandbox.policy import (
    DANGER_FULL_ACCESS,
    SandboxPolicy,
    resolve_policy,
)
from emrg.sandbox.roots import canonical_path, writable_roots
from emrg.sandbox.providers import unconfined_mode
from emrg.server.git_utils import no_prompt_env
from emrg.server.tool_types import ToolDefinition, ToolResult
from emrg.tools import command_scan
from emrg.sandbox.escalation import ESCALATION_TARGETS
from emrg.sandbox.escalation import hops_from as escalation_hops
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
    """The fatal evidence line proving the runner failed, or ``None``.

    Each rule requires a nonzero exit, its optional exit-code gate, and a fatal
    signature on one stderr line after exact informational lines are excluded.
    Exit status alone never proves runner failure — except for the one family of
    statuses that cannot be a command's own, the loader's
    (:attr:`RunnerFailureRule.never_started_exit_codes`), which is checked first
    and independently of the gate because those runs have no stderr to read: the
    line returned for them is synthesized by
    :func:`emrg.sandbox.contract.never_started_detail`.

    The walk over the fatal signatures is the other reading, and it is taken
    *before* the start line.  It is the specific answer: our runner's ``fail()``
    prints a recognised line for every refusal it raises **before** the spawn
    (argv parsing, directory validation, ``SetConsoleCtrlHandler``), and not one
    of those is preceded by an announcement — so reading the silence first would
    report a deliberate refusal as a death and drop the single line naming the
    argument or the directory.  Only a run whose stderr the walk recognises
    *nothing* in is read through the runner's own channel,
    :attr:`RunnerFailureRule.start_line`: its **absence** beside a nonzero exit
    means the runner died before it could mirror anything.  That silence reading
    decodes no status at all and is reported through
    :func:`emrg.sandbox.contract.never_announced_detail`.

    :param exit_code: the process's exit code; ``None`` means signal death.
    :param stderr: collected stderr text, left unchanged.
    :param rules: structured runner-failure rules from the active wrap.
    :returns: the first matching fatal line, or ``None`` when the evidence is
        insufficient.
    """
    if exit_code is None or exit_code == 0:
        return None
    # The same bits, whatever spelling the platform hands over: a parent sees the
    # 32-bit status, while the value can arrive as its two's complement (that is
    # the form ``sys.exit`` takes on Windows).
    unsigned = exit_code & 0xFFFFFFFF if exit_code < 0 else exit_code
    lines = re.split(r"\r?\n", stderr)
    lowered_lines = [line.lower() for line in lines]
    for rule in rules:
        if unsigned in rule.never_started_exit_codes:
            return never_started_detail(unsigned)
    # The walk comes next, because a recognised line is the *specific* answer: our
    # runner's ``fail()`` prints one for every refusal it raises **before** the
    # spawn — argv parsing, directory validation, ``SetConsoleCtrlHandler`` — and
    # not one of those is preceded by an announcement.  Answering "the environment
    # died" to those would discard the one line naming the argument or the
    # directory. Nothing on this path needs a signature the walk has not seen.
    for rule in rules:
        if rule.allowed_exit_codes is not None and exit_code not in rule.allowed_exit_codes:
            continue
        informational = {line.lower() for line in rule.informational_lines}
        if rule.start_line is not None:
            informational.add(rule.start_line.lower())
        # An empty or whitespace-only substring is not evidence; ignore it while
        # keeping any valid signature beside it active.
        signatures = [sig.lower() for sig in rule.fatal_signatures if sig.strip()]
        for line in lines:
            lowered = line.lower()
            if lowered in informational:
                continue
            if any(signature in lowered for signature in signatures):
                return line
    # Nothing was recognised, so — and only now — the silence is read. The runner
    # writes its announcement on the way to the spawn, so a run carrying neither a
    # signature nor an announcement is one that never got there: the measured
    # import death, which left no signature because no code of ours ran. Outside
    # the exit-code gate on purpose — the status a loader dies with is not a
    # dialect this can decode.
    for rule in rules:
        if rule.start_line is not None and rule.start_line.lower() not in lowered_lines:
            return never_announced_detail(unsigned)
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

    The exit marker carries a status, so it appears only when the run has one:
    a signal death is reported as the signal, and a run that never settled
    (``exit_code is None``) reports no exit line — ``[exit code: None]`` is not
    a status the model can act on, and the tool's own description promises the
    line only for *non-zero exits*.  The twin in ``pwsh_tool_v2`` states the
    same contract; ``tests/test_pwsh_tool_v2.py`` drives one run through both
    so the two cannot drift apart again (issue #2039).

    :param result: the completed run.
    :param escalation_modes: the escalation targets the composition advertises
        for the tier this run used (design §1.5 A5, phase P5 —
        ``emrg/sandbox/escalation.py``).  The blueprint adds the hint line only
        when a composition advertises the field (``tool-bash/src/render.ts:43-51``),
        and that condition is carried here as the tier's own hop set: a run at
        ``danger-full-access`` has nowhere wider to go, so it passes none and the
        refusal is reported exactly as it always was.
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
        from emrg.sandbox.escalation import with_retry_hint

        marker = sandbox_denial_marker(str(result.sandbox.get("mode", "")))
        markers.append(
            with_retry_hint(
                marker,
                mode=str(result.sandbox.get("mode", "")),
                advertised=escalation_modes,
            )
        )
    if result.timed_out:
        markers.append(f"[timed out after {result.timeout_ms}ms]")
    if result.signal is not None:
        markers.append(f"[killed by signal: {result.signal}]")
    elif result.exit_code is not None and result.exit_code != 0:
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
        stderr = (
            stderr[:remaining]
            + "\n\n... [stderr truncated to make room for stdout: "
            "only the head is kept, the tail is dropped]"
        )
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

    The group id is the **pid recorded at spawn**: ``preexec_fn=os.setsid`` made
    the child a session leader, so it leads a group of its own and its number is
    the group's. Asking the OS instead — ``os.getpgid(proc.pid)``, which is what
    this did — fails in exactly the case the kill exists for: when a descendant
    holds the pipe open, the direct child has already been reaped, and
    ``os.getpgid`` on a reaped pid raises ``ProcessLookupError``. The fallback
    ``proc.kill()`` raises too (same reason), and the whole cleanup became a
    silent no-op while the survivor kept stdout open (measured 2026-09-27, host
    P0 rant: a turn died with no log line and the task wedged for 45 minutes).

    A group that is already gone is the end state we wanted, so ``ESRCH`` is
    recorded at debug; anything else is a real failure and says so, because a
    cleanup that cannot clean up must not be invisible.

    :param proc: the running process.
    """
    if os.name == "nt":
        # No session/group of our own on Windows: the child is the only handle.
        try:
            proc.kill()
        except (ProcessLookupError, OSError) as exc:
            logger.warning("bash v2: cannot kill process %s: %s", proc.pid, exc)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        logger.debug("bash v2: process group %s is already gone", proc.pid)
    except OSError as exc:
        logger.warning("bash v2: cannot signal process group %s: %s", proc.pid, exc)


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

    # A cancellation — the host's ESC, a turn replaced by a new message, a
    # shutdown — lands on an await *inside* `_collect`, so control leaves this
    # function with the child and its descendants still running. Measured
    # 2026-09-28 (host P0 rant): the command survived as an orphan in its own
    # session, which even a daemon restart could not reach. The kill is the same
    # one the timeout path uses, on the same group id, and the cancellation is
    # re-raised unchanged: a cancelled run is not a result, and turning it into
    # one would be a second defect in the same line.
    try:
        result = await _collect(proc, timeout, timeout_ms)
    except BaseException:
        _kill_process_group(proc)
        raise
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
        # Last, after every reading: the runner's start announcement is evidence
        # for this seam, not output the caller asked for, and it is written on
        # every confined run — so the text the model reads loses it.  Placed last
        # on purpose: a strip that ran before the readings could only ever be
        # one's business by accident, and this line is presentational alone.
        result.stderr = without_start_announcements(result.stderr, confined.runner_failure_rules)
    else:
        # No `enforcement` field: the run was not confined, and a field here
        # could only claim a boundary that does not exist.
        result.sandbox = {"mode": effective_mode, "denied": False}
    return result


def _stream_bytes(task: "asyncio.Task[bytes]") -> bytes:
    """The bytes a stream read produced, or ``b""`` when it produced none.

    ``.result()`` is not safe to call on a task that was cancelled: it
    re-raises, and ``CancelledError`` is a ``BaseException``, so it slips past
    the ``except Exception`` guarding the tool loop and ends the turn with no
    error frame and no log (measured 2026-09-27, host P0 rant — the exact
    expression below this function killed a scheduler-driven turn silently and
    left the task wedged for 45 minutes). A read that raised is the same shape.

    Absence of output is therefore reported as absence. What the streams did
    produce is kept — the drain before this call exists so a timed-out command
    still reports what it wrote — but a read nobody can ask is not a reason to
    raise out of a collector.
    """
    if not task.done() or task.cancelled():
        return b""
    if task.exception() is not None:
        return b""
    value = task.result()
    return value if isinstance(value, bytes) else b""


async def _collect(
    proc: asyncio.subprocess.Process, timeout: float, timeout_ms: int
) -> ShellRunResult:
    """Wait for a process, kill it on timeout, and collect what it produced.

    Reading is not left to a cancelled ``communicate()``: measured on this host,
    a cancelled read loses the bytes already buffered, so the shape here waits on
    the three tasks together and drains them **after** the kill instead — a
    timed-out command still reports the output it managed to write.

    A command that leaves a descendant holding the pipe open is cut the same way
    the old tool cut it — the group is signalled, which closes the pipe with the
    survivor. What the caller sees then depends on whether that worked, and the
    two readings are kept distinct: the output closes within the grace, so the
    command is reported as the run it was; or it does not, and the run is
    reported as a timeout because its output never closed, with whatever the
    streams gave up before their reads were cancelled. (Before 2026-09-27 the
    first outcome was unreachable — the kill signalled nobody and the second
    outcome raised ``CancelledError`` out of this function, ending the turn.)

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
        stdout=_decode_output(_stream_bytes(stdout_task)).rstrip(),
        stderr=_decode_output(_stream_bytes(stderr_task)).rstrip(),
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
                "runs under `bash -c`. Each call is a fresh process — no "
                "state (working directory, variables, functions) persists "
                "between calls, so pass `workdir` instead of using `cd`. "
                "Non-zero exits are reported as "
                "`[exit code: N]`. The kernel confines it to the session's "
                "working directory: writes outside it (and outside the OS temp "
                "area) are refused whatever language or subprocess attempts "
                "them, and the run is reported as a sandbox denial "
                "(`[sandbox: file access denied under <mode> mode]`) — the "
                "policy, not a bug in the command, so do not retry another way. "
                "Long output is truncated, and each cut says so: stderr to its "
                "own cap and stdout to what the budget leaves, both keeping head "
                "and tail."
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
                        "description": "Timeout in seconds — a positive number "
                        "(default: 30). Zero, a negative value or a non-number is "
                        "refused rather than run under a different bound.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Working directory for the command. The daemon "
                        "supplies the session's working directory, which is also the "
                        "writable boundary; passing another value does not widen it.",
                    },
                    "sandbox_permissions": {
                        "type": "string",
                        "enum": list(ESCALATION_TARGETS),
                        "description": "Ask for a one-hop wider sandbox tier for THIS "
                        "call only. Must be sent together with `justification`, and it "
                        "is refused unless the host approves: it never changes the "
                        "session's default tier, and it reaches only the tiers the "
                        "call's current one lists (from `read-only`: `workspace-write` "
                        "or `danger-full-access`).",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Why this call needs the wider tier — a non-empty "
                        "sentence the host reads before approving. Required with "
                        "`sandbox_permissions`.",
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
        timeout, refusal = _as_timeout(arguments.get("timeout"))
        if refusal is not None:
            return ToolResult(name="bash", content=refusal, error=True)
        workdir = str(
            arguments.get("workspace") or arguments.get("workdir") or os.getcwd()
        )
        policy = resolve_policy(
            mode=arguments.get("sandbox"),
            workspace_root=workdir,
            session_id=arguments.get("session_id"),
            extra_roots=arguments.get("writable_roots"),
        )
        # The command-text rules a checked tier makes (containment escape, and the
        # host's daemon-lifecycle red line) are read from the text by
        # `emrg/tools/command_scan.py`, at the boundary, for both dialects — the
        # kernel fence below is a *write* fence, so signal- and IPC-shaped acts
        # reach it as ordinary commands and `emrg server stop` would otherwise
        # reach the daemon's shutdown frame unchecked (P7, issue #1675).
        if policy.mode != DANGER_FULL_ACCESS:
            refusal = command_scan.command_refusal(command)
            if refusal:
                return ToolResult(name="bash", content=f"⛔ {refusal}", error=True)
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
        return ToolResult(
            name="bash",
            content=render_result(
                result, escalation_modes=escalation_hops(policy.mode),
            ),
            error=False,
        )


def _as_timeout(value: object) -> tuple[float | None, str | None]:
    """Read the timeout argument, or hand back the refusal it earns.

    A non-positive timeout was not merely unusable — it was **obeyed**. Measured
    2026-10-08 (`cyc20261008-175325`), `echo HELLO` under each value:

    ==================  ==========================================================
    absent              ``HELLO``
    ``5``               ``HELLO``
    ``0``               ``(no output)`` / ``[timed out after 0ms]`` / ``[killed by
                        signal: 9]`` — the command never ran, and the reading calls
                        it a timeout
    ``-5``              the same, with ``[timed out after -5000ms]``
    ``"abc"``           ``HELLO``, silently under the 30s default
    ==================  ==========================================================

    The daemon's own watchdog already reads this field the other way — ``scheduler.
    _tool_silence_seconds`` treats "a non-positive timeout" as *unusable* and
    bounds the call by the default, with a test stating why ("clamping it to the
    grace alone would fire the watchdog instantly on every such call"). Two readers
    of one datum with two answers is the defect; this is the tool's half, and it
    refuses rather than substitutes a number the caller did not send.

    :param value: whatever the model sent.
    :returns: ``(seconds, None)``, or ``(None, refusal)`` for the caller to report
        as ``ToolResult(..., error=True)``. An absent value is the documented
        default of 30 seconds, which the schema states.
    """
    if value is None:
        return 30.0, None
    refusal = (
        f"timeout must be a positive number of seconds (got {value!r}); this call "
        "is refused rather than run under a different bound. Omit it to use the "
        "default of 30 seconds."
    )
    if isinstance(value, bool):
        return None, refusal
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None, refusal
    if not math.isfinite(seconds) or seconds <= 0:
        return None, refusal
    return seconds, None
