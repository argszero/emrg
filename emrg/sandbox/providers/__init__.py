"""Platform runner chains — selection is by platform first, probes second.

Mirrors ``packages/sandbox/sandbox-local/src/index.ts:159-166, 495-513``
(blueprint ``.emrg/designs/bash-tool-v2-design.md`` §5.6): a chain with one
candidate is selected **without** a probe — "probing arbitrates; it does not
re-validate a choice that has no alternative", and that candidate's own refusal
at execution time is the fail-closed end — while a chain with several candidates
is arbitrated by functional probes in chain order.  A platform whose chain is
empty, or has no candidate left, fails closed at ``confine()``: the command
never runs.
"""

from __future__ import annotations

from emrg.sandbox.contract import Runner, SandboxUnavailableError, host_platform
from emrg.sandbox.policy import DANGER_FULL_ACCESS
from emrg.sandbox.providers import darwin, linux, win32

#: The runner chain per platform, in preference order.
#:
#: ``darwin`` and ``win32`` are the blueprint's rows for those hosts (Seatbelt,
#: and the ACL restricted-token backend); ``linux`` carries its **first** rung
#: only (``bwrap``), not its second — the ``landlock-run`` launcher, a native
#: artifact this pure-Python package does not have yet (blueprint §1.5 B1).
#: A missing product is a pending artifact, not a licence to pretend (design
#: §1.4) — so ``select_runner`` fails closed where a chain is absent or empty
#: rather than silently running the command bare.
#:
#: A chain of one is selected **without** a probe, which is why the linux row
#: needs none: the blueprint probes only to arbitrate between candidates, and
#: when landlock arrives it becomes the second rung and the probe comes with it.
PLATFORM_CHAINS: dict[str, tuple[Runner, ...]] = {
    "darwin": (darwin.SEATBELT,),
    "linux": (linux.BWRAP,),
    "win32": (win32.WINDOWS_ACL,),
}

# A backend's confinement covers whatever the wrapped argv goes on to execute.
# That is the point of moving the boundary to the process spawn: the old scan's
# "what it does not reach is an interpreter" (``bash_tool.py:2880``) stops being
# a category at all.


def select_runner(mode: str, *, platform_name: str | None = None) -> Runner:
    """Resolve which runner confines commands on ``platform_name``.

    :param mode: the confined mode being requested (carried into the refusal).
    :param platform_name: the platform to select for; defaults to the host's.
    :returns: the selected backend.
    :raises SandboxUnavailableError: when this platform has no usable candidate.
    """
    platform = platform_name or host_platform()
    chain = PLATFORM_CHAINS.get(platform, ())
    if not chain:
        raise SandboxUnavailableError(mode)
    first, *rest = chain
    if not rest:
        # A sole candidate needs no arbitration (see the module docstring).
        return first
    raise NotImplementedError(
        f"the {platform} chain has {len(chain)} candidates, so it must be arbitrated by "
        "functional probes in chain order (blueprint sandbox-local/src/index.ts:502-513). "
        "No platform has two candidates yet; implementing it means also asserting a POSITIVE "
        "probe timeout, because Python's timeout=0 means 'no wait at all' while Node's means "
        "'unbounded' (design M8)."
    )


def unconfined_mode(mode: str, *, platform_name: str | None = None) -> str | None:
    """The mode this call runs WITHOUT confinement under, or ``None`` when it is confined.

    One reason a run is unconfined, and it is not silent: ``danger-full-access``
    — the mode's own meaning.  The blueprint short-circuits it in the consumer,
    before any provider is consulted (``shell/bash-sandbox/src/index.ts:92-95``),
    and the result surface then carries no ``enforcement`` field at all.

    Linux's host-authorised deviation D4 (2026-09-21 18:29, "run unconfined and
    report ``danger-full-access`` until P3 lands") lived here and is **deleted by
    P3**, which is that deviation's own stated exit condition: the chain now has
    a real rung, so a Linux host without ``bwrap`` fails closed exactly like the
    blueprint instead of running bare.

    :param mode: the requested tier.
    :param platform_name: the platform to decide for; defaults to the host's.
    :returns: the mode to run under unconfined, or ``None`` to confine.
    """
    if mode == DANGER_FULL_ACCESS:
        return DANGER_FULL_ACCESS
    return None
