"""Linux bubblewrap backend.

Mirrors ``packages/sandbox/sandbox-local`` in deepseek-harness: the profile
builder is ``profiles.ts`` (``bwrapProfileArgs``) and the three facts around it
are the linux rows of the tables in ``sandbox-local/src/index.ts`` — the static
enforcement table, the denial dialect and the runner-failure rules.

Why this backend needs no probe on this platform
------------------------------------------------
The blueprint's linux chain is ``['bwrap', 'landlock']`` and its rule is that a
chain is arbitrated by functional probes **only when it has more than one
candidate** ("probing arbitrates; it does not re-validate a choice that has no
alternative").  The landlock launcher is a native artifact this package does not
carry yet (blueprint §1.5 B1), so this chain has one rung and is selected
without a probe — exactly as darwin's is.  A host without ``bwrap`` therefore
fails closed at the *spawn* (``FileNotFoundError`` → ``SANDBOX_UNAVAILABLE``),
and a host where ``bwrap`` starts but cannot create a namespace fails closed
through its own stderr signature below.  Neither path runs the command
unconfined.

Measured on this profile (docker ``debian:bookworm-slim`` + ``bubblewrap 0.8.0``,
kernel 6.8, 2026-09-22; the container needs ``--security-opt
seccomp=unconfined --security-opt apparmor=unconfined --cap-add SYS_ADMIN`` —
with only the first of the three, ``bwrap`` dies at ``Failed to make / slave:
Permission denied`` before it reaches the profile):

* ``read-only``: reads pass; **every** write is refused with ``Read-only file
  system`` — outside the workspace, inside it, and in ``/tmp`` alike
  (``--ro-bind / /`` leaves no writable mount).  The refused file is absent
  afterwards.
* ``workspace-write``: a write under the bound workspace root succeeds; a write
  outside it is refused with the same signature; ``/tmp`` is a **fresh tmpfs** —
  writable inside the sandbox and invisible to the host afterwards (measured:
  the file written there does not exist outside).
* a workspace root that does not exist makes ``bwrap`` itself refuse —
  ``bwrap: Can't find source path …`` with exit 1, which the fatal signature
  below turns into a fail-closed refusal.  This is the opposite of Seatbelt,
  where a missing ``subpath`` grants nothing *silently*; both are conservative,
  and the difference is that here the caller is told.
"""

from __future__ import annotations

from emrg.sandbox.contract import Runner, RunnerFailureRule
from emrg.sandbox.policy import SandboxPolicy

#: The program name, resolved through ``PATH``.  Bubblewrap is packaged as
#: ``bubblewrap``/``bwrap`` on every distribution that ships it; a host without
#: it fails closed at the spawn rather than at a version check.
BWRAP_BIN = "bwrap"

#: Bubblewrap governs every promised file effect by construction — the profile
#: *is* the mount table — so the claim is a profile fact, not a probe result.
#: This is the static table's linux row for this rung.
ENFORCEMENT = "full"

#: Bubblewrap's denial dialect: what a refused write produces on stderr (EROFS,
#: raised by the kernel against the read-only mount — measured above, through a
#: shell that reports it as ``cannot create …: Read-only file system``).
DENIAL_SIGNATURES: tuple[str, ...] = ("read-only file system",)

#: ``bwrap`` prints ``bwrap: <detail>`` and exits **1** on its own fatal paths.
#: The rule stays signature-only, as the blueprint has it: 1 is the status an
#: ordinary command can also exit with (the child's status is what ``bwrap``
#: returns), so pinning it would misread a confined command's own failure as
#: "the command never ran".  The signature is what identifies the runner.
RUNNER_FAILURE_RULES: tuple[RunnerFailureRule, ...] = (
    RunnerFailureRule(fatal_signatures=("bwrap: ",)),
)


def bwrap_profile_args(policy: SandboxPolicy) -> list[str]:
    """Build the ``bwrap`` mount profile for one file-effect policy.

    The argument list is the blueprint's, in its order — ``--ro-bind / /``
    makes the whole filesystem readable and immutable, and ``workspace-write``
    adds the two writable mounts:

    * ``--tmpfs /tmp`` — a fresh temporary filesystem, so a confined command
      that writes to ``/tmp`` cannot reach the host's temp area at all.  This is
      the blueprint's choice (``profiles.ts:22``) and it is deliberately *not*
      the Seatbelt grant list: there, ``/tmp`` is a writable host path because
      a profile can only allow or deny, while a mount namespace can give the
      sandbox a private one.
    * ``--bind <workspace> <workspace>`` — the policy's workspace root, bound
      back over the read-only view.  It is applied *after* the ``/tmp`` tmpfs,
      so a workspace that itself lives under ``/tmp`` (pytest's default temp
      root is one) is still writable — measured, because the mount added last
      wins.

    :param policy: the file-effect policy to express as bwrap mounts.
    :returns: the runner arguments before the trailing ``--`` and caller argv.
    """
    args = [
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--unshare-pid",
        "--proc", "/proc",
        "--die-with-parent",
    ]
    if policy.mode == "workspace-write":
        args += ["--tmpfs", "/tmp"]
        args += ["--bind", policy.workspace_root, policy.workspace_root]
    return args


def runner_argv(policy: SandboxPolicy) -> list[str]:
    """The runner invocation (program plus profile arguments) for one policy.

    :param policy: the file-effect policy to confine under.
    :returns: the argv the seam prepends to the caller's own.
    """
    return [BWRAP_BIN, *bwrap_profile_args(policy)]


#: The backend as the chain tables carry it.
BWRAP = Runner(
    name="bwrap",
    enforcement=ENFORCEMENT,
    denial_signatures=DENIAL_SIGNATURES,
    runner_failure_rules=RUNNER_FAILURE_RULES,
    runner_argv=runner_argv,
)
