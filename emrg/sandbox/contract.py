"""The confinement seam: wrap an exact argv, or fail closed.

Mirrors ``packages/sandbox/sandbox/src/index.ts`` in deepseek-harness
(blueprint ``.emrg/designs/bash-tool-v2-design.md`` §5.3, §5.7): the seam's
whole job is to take **the exact argv the caller is about to spawn** and return
the argv to spawn instead, plus the facts a consumer needs to classify the run
afterwards (enforcement completeness, this backend's denial dialect, its
structured runner-failure rules).

Nothing here is a shell string.  A shell-shaped consumer passes
``["bash", "-c", command]`` and the command survives as a single argv element —
no second parse, no re-quoting (measured: blueprint §3.2, 11/11).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from emrg.sandbox.policy import SandboxPolicy
from emrg.sandbox.roots import canonical_path

#: The error code for a requested confined mode when no backend is usable.  The
#: seam fails closed, and the code travels with the error so a consumer can
#: distinguish *missing confinement* from *the command failed*.
SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"


def sandbox_unavailable_message(mode: str, detail: str | None = None) -> str:
    """The fail-closed refusal, word for word from the blueprint.

    :param mode: the confined mode that was requested.
    :param detail: the runner's own fatal stderr line, when one was captured.
    :returns: the message the model and the logs see.
    """
    message = (
        f'sandbox mode "{mode}" is requested but no sandbox backend is usable on this host; '
        "refusing to run the command unconfined. Install bubblewrap or run a Landlock-enforcing "
        "kernel (Linux), ensure sandbox-exec is usable (macOS), or ensure the ACL "
        "restricted-token runner can start (Windows) — otherwise switch the consumer to "
        "danger-full-access."
    )
    if detail is not None:
        message += f" Runner failure: {detail}"
    return message


class SandboxUnavailableError(RuntimeError):
    """Raised when the seam cannot enforce the requested mode.

    The command is never spawned on this path — the whole point of the
    fail-closed contract: a request for confinement that cannot be honoured must
    not quietly become an unconfined run.
    """

    def __init__(self, mode: str, detail: str | None = None) -> None:
        super().__init__(sandbox_unavailable_message(mode, detail))
        self.mode = mode
        self.detail = detail
        self.code = SANDBOX_UNAVAILABLE


@dataclass(frozen=True)
class RunnerFailureRule:
    """Evidence that identifies a runner failing *before* it ran the command.

    A consumer first applies :attr:`allowed_exit_codes` when present, removes
    :attr:`informational_lines` by case-insensitive exact line equality, then
    matches :attr:`fatal_signatures` case-insensitively within each remaining
    stderr line.  Exit status alone never proves runner failure.

    :attr:`never_started_exit_codes` is the **second** reading and the one that
    needs no stderr at all: the codes the OS loader itself produces when an image
    cannot be loaded or initialized — ``STATUS_DLL_INIT_FAILED`` and its siblings.
    They are evidence because no user code ran to write a line *or* to choose an
    exit status, so a signature rule can never see them: the whole failure is a
    process that never started, on either side of the seam (the runner's own
    interpreter or the child it was about to mirror).  A consumer checks this
    field **before** the signature walk and independently of
    :attr:`allowed_exit_codes`, because the code is the evidence — gating it on
    the runner's *own* failure exit would refuse exactly the case it exists for.
    Empty by default: a backend that cannot name such a code adds nothing.

    :attr:`start_line` is the **third** reading, and the one the runner itself
    supplies rather than the parent observing it: the line a runner that this
    project writes prints on its own stderr immediately before it mirrors the
    command.  Its **absence** from a run's stderr, beside a nonzero exit, is the
    evidence — the runner reached neither its announcement nor its failure
    reporter, so nothing ran.  This is the reading a status cannot give: the
    measured instances are an interpreter that died at *import* (exit 1, empty
    stdout, a ``ModuleNotFoundError`` naming this very runner) and a status no
    loader family contains, and what the exit code was is not decoded but only
    reported.  Only a backend whose runner is our own code can declare one;
    ``bwrap`` and ``sandbox-exec`` are third-party binaries, so their rules leave
    this ``None`` and are unchanged.  The line is deliberately spelled outside
    the fatal prefix's namespace: a consumer also treats it as informational, so
    an announcement sharing the prefix would be the first line every walk
    returns and would hide the detail a real failure printed.
    ``None`` by default: a backend that cannot announce its own start adds
    nothing.
    """

    fatal_signatures: tuple[str, ...]
    allowed_exit_codes: tuple[int, ...] | None = None
    informational_lines: tuple[str, ...] = ()
    never_started_exit_codes: tuple[int, ...] = ()
    start_line: str | None = None


def never_started_detail(exit_code: int) -> str:
    """The line a consumer reports when an exit proves the child never started.

    Synthesized rather than collected, and that is the point: this failure has no
    stderr to quote, so the reading has to be *stated* — with the number, in both
    spellings, because the raw code is the only handle a reader can compare
    against a log or a crash dump.

    :param exit_code: the exit status the loader produced, as the unsigned
        32-bit value a parent observes.
    :returns: the detail line, in the vocabulary the seam already speaks.
    """
    return (
        f"the confined child never started: exit {exit_code} (0x{exit_code:08X}) is one the "
        "OS loader produces when an image cannot be loaded or initialized, so nothing ran "
        "and the failure belongs to the environment, not to the command"
    )


def never_announced_detail(exit_code: int) -> str:
    """The line a consumer reports when a runner never announced its own start.

    The sibling of :func:`never_started_detail`, and the reading that needs
    neither a loader status nor a signature line: the runner writes its start
    line before it mirrors anything, so a nonzero exit with no such line on
    stderr is a runner that died before it could run the command.  The status is
    reported, never decoded — a status is a per-platform dialect, and this
    reading does not rest on which one arrived.

    :param exit_code: the exit status the runner's own process reported, as the
        unsigned 32-bit value a parent observes.
    :returns: the detail line, in the vocabulary the seam already speaks.
    """
    return (
        f"the confined runner never started: exit {exit_code} (0x{exit_code:08X}) came with no "
        "start announcement of its own on stderr, so the runner's process died before it could "
        "mirror the command — nothing ran, and the failure belongs to the environment, not to "
        "the command"
    )


def without_start_announcements(stderr: str, rules: tuple[RunnerFailureRule, ...]) -> str:
    """Drop the runners' own start announcements from a run's collected stderr.

    The announcement is evidence for the seam, not output the caller asked for:
    it is written on **every** confined run, so leaving it in place would put a
    ``[stderr]`` section on every successful command the model reads.  Removed
    after classification and never before it — the line's absence *is* the
    reading (:func:`never_announced_detail`).

    :param stderr: the collected stderr text.
    :param rules: the rules of the active wrap.
    :returns: the same text with the announcement lines removed.
    """
    marks = {rule.start_line.lower() for rule in rules if rule.start_line}
    if not marks or not stderr:
        return stderr
    return "".join(
        line for line in stderr.splitlines(keepends=True) if line.strip().lower() not in marks
    )


@dataclass(frozen=True)
class Runner:
    """One backend, ready to confine for a policy.

    ``enforcement`` is the completeness this host achieves: ``partial`` means the
    backend cannot govern every promised file effect, and a caller needing an
    absolute boundary must not read it as ``full``.
    """

    name: str
    enforcement: str
    denial_signatures: tuple[str, ...]
    runner_failure_rules: tuple[RunnerFailureRule, ...]
    runner_argv: Callable[[SandboxPolicy], list[str]]


@dataclass(frozen=True)
class ConfinedArgv:
    """What the caller spawns in place of its own argv, plus the run's facts."""

    argv: list[str]
    enforcement: str
    denial_signatures: tuple[str, ...]
    runner_failure_rules: tuple[RunnerFailureRule, ...]


def confine(
    argv: Sequence[str],
    policy: SandboxPolicy,
    *,
    platform_name: str | None = None,
) -> ConfinedArgv:
    """Wrap ``argv`` so it executes confined under ``policy`` on this host.

    :param argv: the exact argv the caller is about to spawn (program plus
        arguments) — NOT a shell string.
    :param policy: the file-effect policy this execution runs under.
    :param platform_name: the platform to select the runner chain for; defaults
        to the host's.  Injectable so a chain's selection is testable anywhere
        (the blueprint replaces ``process.platform`` for the same reason).
    :returns: the argv to spawn instead, with the selected backend's facts.
    :raises SandboxUnavailableError: when no backend on this platform can
        enforce the mode.  Cancellation is the caller's own asyncio task — the
        blueprint's ``signal`` parameter is that responsibility in this
        language, not a keyword dropped here (design §5.3).
    """
    # Local import: providers speak this module's vocabulary, so the dependency
    # runs one way only.  Canonicalize the root here, at the provider entry, and
    # not in the policy layer (blueprint ``sandbox-local/src/index.ts:319``).
    from emrg.sandbox.providers import select_runner

    canonical_policy = replace(policy, workspace_root=canonical_path(policy.workspace_root))
    runner = select_runner(canonical_policy.mode, platform_name=platform_name)
    return ConfinedArgv(
        argv=[*runner.runner_argv(canonical_policy), "--", *argv],
        enforcement=runner.enforcement,
        denial_signatures=runner.denial_signatures,
        runner_failure_rules=runner.runner_failure_rules,
    )


def sandbox_denial_marker(mode: str) -> str:
    """The model-facing denial marker — one vocabulary for both enforcing families.

    The bash family (a refused file effect) and, from P7, the in-process fence
    (a refused mutation) report a denial with the same line, so the model
    recognizes a policy denial identically whichever layer refused it.

    :param mode: the mode the denied call ran under.
    :returns: the marker line, exactly as the model sees it.
    """
    return f"[sandbox: file access denied under {mode} mode]"


def host_platform() -> str:
    """The platform name the chain tables are keyed by.

    ``sys.platform`` already speaks the blueprint's vocabulary for the three
    platforms that matter (``darwin`` / ``linux`` / ``win32``), so no translation
    layer is introduced to be wrong later.

    :returns: the running platform's name.
    """
    return sys.platform
