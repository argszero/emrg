"""File-effect policy for one confined execution.

Mirrors ``packages/sandbox/sandbox-policy`` in deepseek-harness (blueprint
``.emrg/designs/bash-tool-v2-design.md`` §5.2): one call, one policy.  The mode
and the workspace root are resolved once at the consumer's boundary, and the
provider treats the policy as fully specified.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: The mode vocabulary of the file-effect policy.  Unchanged from the old
#: tool's, because it is also the blueprint's ``SandboxMode`` — one word list,
#: two implementations for the length of the parallel period, pinned equal by
#: ``tests/test_bash_v2_policy.py`` so a drift cannot hide (the old definition
#: disappears with its file at P7).
SANDBOX_MODES: tuple[str, ...] = (
    "danger-full-access",
    "read-only",
    "workspace-write",
)

#: The mode that bypasses confinement entirely.
DANGER_FULL_ACCESS = "danger-full-access"

#: The mode a call that carries no tier at all runs under.
#:
#: NOT the blueprint's deployment default.  dsh defaults its deployment to
#: ``read-only`` (``sandbox-policy/src/index.ts:113``, "the fail-safe default"
#: for a deployment whose consumers all declare a mode); EMRG's tier is chosen
#: by the *task* configuration (``scheduler._resolve_sandbox``), and a call that
#: carries none is a host session — which the old tool ran unconfined
#: (``protocol.py:34``).  Mapping that silence to ``read-only`` would confiscate
#: a session nobody asked to confine, so the value it already had is kept.  The
#: blueprint's fail-safe default stays a recorded difference (design §1.3), not
#: something smuggled in here.
DEFAULT_MODE = DANGER_FULL_ACCESS


@dataclass(frozen=True)
class SandboxPolicy:
    """The complete file-effect policy resolved for one capability call.

    The workspace root is carried even under modes that do not consume it, so a
    caller can resolve the policy once before choosing the enforcement path —
    the blueprint keeps ``workspaceRoot`` in ``SandboxExecutionPolicy`` for the
    same reason.

    ``session_id`` is not decoration: a backend keys per-session state off it
    (the Windows ACL runner gives each live session/workspace pair a random
    private temp directory and SID while the workspace grant stays
    per-workspace).  Agentless calls carry ``None`` and fall back to per-call
    state, exactly as the blueprint's provider documents.
    """

    mode: str
    workspace_root: str
    session_id: str | None = None
    #: Roots the **host** named for this session, alongside the ones the mode
    #: derives (rant 2026-10-09T09:43:39).  The workspace is not the only place a
    #: session may legitimately need to write, and the alternative the mode table
    #: offered was ``danger-full-access`` — the whole filesystem, to reach one
    #: file outside it.  A host-named root is the middle: this session, these
    #: paths, named by a person and withdrawable by one.
    #:
    #: Two things this field is *not*.  It is not a widening the agent can ask
    #: for: the daemon overwrites the argument it travels in on every tool call
    #: (``_inject_tool_arguments``), exactly as it does the tier, so a model that
    #: invents ``writable_roots`` in a call replaces nothing.  And it is not a
    #: per-deployment constant — the root host decision D5 deleted was exactly
    #: that, unnamed by anyone and unremovable; the test that keeps that name
    #: dead (``tests/test_file_policy_has_one_home.py``) is the boundary between
    #: the two, and it stays.
    extra_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Absolute-path assertion at the POLICY layer, canonicalization at the
        # provider entry (blueprint ``sandbox-policy/src/index.ts:36-39`` vs
        # ``sandbox-local/src/index.ts:319``): two jobs, deliberately apart.
        if self.mode not in SANDBOX_MODES:
            raise ValueError(f"sandbox policy: unknown mode {self.mode!r}")
        if not os.path.isabs(self.workspace_root):
            raise ValueError(
                "sandbox policy: workspace_root must be an absolute execution-world path "
                f"(got {self.workspace_root!r})"
            )
        # Same assertion, same reason, for the host-named roots: a relative one
        # would resolve against whatever cwd the provider happened to run in, so
        # the path granted and the path judged could differ.  A caller that
        # cannot spell it absolutely has not decided what it is granting.
        for root in self.extra_roots:
            if not os.path.isabs(root):
                raise ValueError(
                    "sandbox policy: every extra root must be an absolute execution-world "
                    f"path (got {root!r})"
                )


def resolve_policy(
    *,
    mode: str | None = None,
    workspace_root: str,
    session_id: str | None = None,
    extra_roots: "tuple[str, ...] | list[str] | None" = None,
) -> SandboxPolicy:
    """Resolve the policy one call runs under.

    :param mode: the tier the daemon injected for this task, or ``None`` when
        the call carries none (see :data:`DEFAULT_MODE`).
    :param workspace_root: the session's cwd — the same identity the command
        runs in and the boundary it may write under, one value, not two.
    :param session_id: the calling session, when the caller has one.
    :param extra_roots: the session's host-named writable roots, as the daemon
        injected them.  ``None`` means "this call was told of none", which is
        the same list as an empty one: a caller that supplies nothing grants
        nothing, and silence never widens a boundary.
    :returns: the fully resolved policy.
    """
    return SandboxPolicy(
        mode=DEFAULT_MODE if mode is None else mode,
        workspace_root=workspace_root,
        session_id=session_id,
        extra_roots=tuple(extra_roots or ()),
    )
