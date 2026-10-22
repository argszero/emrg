"""File-effect policy for one confined execution.

Mirrors ``packages/sandbox/sandbox-policy`` in deepseek-harness (blueprint
``.emrg/designs/bash-tool-v2-design.md`` §5.2): one call, one policy.  The mode
and the workspace root are resolved once at the consumer's boundary, and the
provider treats the policy as fully specified.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from emrg.sandbox.grants import GRANT_DIRECTORY, GRANT_FILE, GRANT_REGISTRY, RootGrant

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
    #: Grants the **host** named for this session, alongside the ones the mode
    #: derives (rant 2026-10-09T09:43:39).  The workspace is not the only place a
    #: session may legitimately need to write, and the alternative the mode table
    #: offered was ``danger-full-access`` — the whole filesystem, to reach one
    #: file outside it.  A host-named grant is the middle: this session, these
    #: objects, named by a person and withdrawable by one.
    #:
    #: Typed since issue #2492: a grant carries its **kind** (``directory``,
    #: ``file``, ``registry``) so no dialect re-decides it from the disk at
    #: profile-build time, and so an object that is not a path can be named at
    #: all.  ``emrg/sandbox/grants.py`` holds the model.
    #:
    #: Two things this field is *not*.  It is not a widening the agent can ask
    #: for: the daemon overwrites the argument it travels in on every tool call
    #: (``_inject_tool_arguments``), exactly as it does the tier, so a model that
    #: invents a grant in a call replaces nothing.  And it is not a
    #: per-deployment constant — the root host decision D5 deleted was exactly
    #: that, unnamed by anyone and unremovable; the test that keeps that name
    #: dead (``tests/test_file_policy_has_one_home.py``) is the boundary between
    #: the two, and it stays.  The **default manifest** is the third mechanism,
    #: and it is neither: its entries are visible in ``/sandbox list`` with their
    #: provenance and each is withdrawn by one command.
    extra_grants: tuple[RootGrant, ...] = ()
    #: Default-manifest targets this session's host has **withdrawn**.
    #:
    #: A default applies without anyone typing anything, so "stop applying it"
    #: has to be a piece of state somewhere.  It is here rather than in a config
    #: file for the reason the tier is: the daemon is the only writer, the value
    #: is per session, and a client is a display.  Requirements 3 and 4 of issue
    #: #2492 — a default is visible, withdrawable, and only ever applies to a
    #: tier that grants something — are decided by the derivation reading this.
    withheld_roots: tuple[str, ...] = ()

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
        # Same assertion, same reason, for the host-named grants: a relative one
        # would resolve against whatever cwd the provider happened to run in, so
        # the path granted and the path judged could differ.  A caller that
        # cannot spell it absolutely has not decided what it is granting.
        #
        # Only the two filesystem kinds are asked this question, and that is
        # requirement 1 of issue #2492 rather than a shortcut: a registry key is
        # not a path, `os.path.isabs` has no meaning for it, and a model that
        # ran the path assertion over every kind would refuse the one kind this
        # change exists to make namable.
        for grant in self.extra_grants:
            if grant.kind == GRANT_REGISTRY:
                if not grant.target.strip():
                    raise ValueError("sandbox policy: a registry grant must name a key")
                continue
            if grant.kind not in (GRANT_DIRECTORY, GRANT_FILE):
                raise ValueError(f"sandbox policy: unknown grant kind {grant.kind!r}")
            if not os.path.isabs(grant.target):
                raise ValueError(
                    "sandbox policy: every extra grant must name an absolute execution-world "
                    f"path (got {grant.target!r})"
                )


def grants_from_arguments(arguments: dict) -> list[RootGrant]:
    """The session's writable grants as one tool call carries them.

    The daemon writes the session's grants into a call under **two** keys
    (``emrg/server/daemon.py::_inject_tool_arguments``): ``writable_grants``, the
    typed list, and ``writable_roots``, its path projection. This is the one
    reader of that pair, so four tools cannot disagree about which they prefer —
    and the answer is the typed one, because a tool that read only the paths
    would have to re-decide each object's kind from the disk, which is the defect
    issue #2492 exists to remove.

    ``writable_roots``'s fallback is not a licence to infer: it is for a call that
    carries the older key alone (an in-process caller, a test), and its cost is
    the one :func:`resolve_policy` documents for ``extra_roots``.

    :param arguments: the call's arguments, as the daemon handed them over.
    :returns: the grants; empty when the call carries neither key.
    """
    typed = arguments.get("writable_grants")
    if typed is not None:
        read = (RootGrant.from_wire(value) for value in typed)
        return [grant for grant in read if grant is not None]
    roots = arguments.get("writable_roots")
    if roots is not None:
        return [RootGrant.from_path(str(root)) for root in roots]
    return []


def resolve_policy(
    *,
    mode: str | None = None,
    workspace_root: str,
    session_id: str | None = None,
    extra_grants: "tuple[RootGrant, ...] | list[RootGrant] | list[dict] | None" = None,
    extra_roots: "tuple[str, ...] | list[str] | None" = None,
    withheld_roots: "tuple[str, ...] | list[str] | None" = None,
) -> SandboxPolicy:
    """Resolve the policy one call runs under.

    Two ways to name the host's grants and **one store**, which is why passing
    both is an error rather than a merge: ``extra_grants`` is the typed form the
    daemon injects, ``extra_roots`` the path spelling an in-process caller
    writes.  The second is a convenience with a cost the caller should know:
    :meth:`~emrg.sandbox.grants.RootGrant.from_path` reads each kind off the disk
    at construction, so a path-form grant is decided once here instead of once
    per dialect — narrower than the defect issue #2492 fixes, but not the same
    thing as naming the kind.

    :param mode: the tier the daemon injected for this task, or ``None`` when
        the call carries none (see :data:`DEFAULT_MODE`).
    :param workspace_root: the session's cwd — the same identity the command
        runs in and the boundary it may write under, one value, not two.
    :param session_id: the calling session, when the caller has one.
    :param extra_grants: the session's host-named grants, typed, as the daemon
        injected them.  Entries it cannot read are dropped
        (:meth:`~emrg.sandbox.grants.RootGrant.from_wire`), because the only
        safe direction for an unreadable grant is a smaller surface.
    :param extra_roots: the same thing spelled as paths; see above.
    :param withheld_roots: default-manifest targets this session withdrew.
    :returns: the fully resolved policy.
    :raises ValueError: when both grant forms are given, so a caller cannot
        believe two lists were combined when one of them was ignored.
    """
    if extra_grants is not None and extra_roots is not None:
        raise ValueError(
            "sandbox policy: name the host's grants once — pass extra_grants (typed) or "
            "extra_roots (paths), not both, or one list is silently ignored"
        )
    grants: tuple[RootGrant, ...] = ()
    if extra_grants is not None:
        read = (RootGrant.from_wire(value) for value in extra_grants)
        grants = tuple(grant for grant in read if grant is not None)
    elif extra_roots is not None:
        grants = tuple(RootGrant.from_path(str(root)) for root in extra_roots)
    return SandboxPolicy(
        mode=DEFAULT_MODE if mode is None else mode,
        workspace_root=workspace_root,
        session_id=session_id,
        extra_grants=grants,
        withheld_roots=tuple(withheld_roots or ()),
    )
