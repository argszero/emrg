"""In-session escalation: one hop strictly wider, approved before it runs.

Blueprint ``.emrg/designs/bash-tool-v2-design.md`` §1.5 A5 / §D3 (phase P5),
copied one for one from deepseek-harness:

* the strict-wider table — ``sandbox/src/escalation.ts:28-31``;
* the advertised target set — ``:41``;
* the pairing check — ``:51-61``;
* the ordered fail-closed approval sequence — `approveEscalation`, ``:153``;
* the retry hint — ``:84-86``.

The shape of the mechanism, in EMRG's own terms: **the tier configured on the
task is this session's default**. Escalation is a one-shot widening *above* that
default — it never writes the default back, never reaches ``tasks.yml`` and never
crosses a session. And it is a hop, not a jump: ``read-only`` may become
``workspace-write`` or ``danger-full-access``, ``workspace-write`` may become
``danger-full-access``, and ``danger-full-access`` has nowhere left to go.

Two properties are the whole point and neither is a detail.

**It fails closed.** No client to ask, a timeout, an answer that cannot be
parsed, a channel that raised: every one of those is a *refusal*. The order is
what makes that true rather than hopeful — the table is checked before the
channel is consulted (a target the current tier's row does not list never
becomes a question a confused host could say yes to), and the answer is mapped
to a boolean before it is acted on.

**The field is validated at execution, not by the schema.** A tool schema is
registry-global and an effective mode is a per-call fact, so the tool advertises
``ESCALATION_TARGETS`` while :func:`validate_hop` is what decides whether *this*
call may go there. A model whose call stands at ``workspace-write`` and asks for
``read-only`` is refused — and the schema would have accepted the same string,
because a schema validates a tier's name and not the call's position. What is
reachable is exactly the current tier's own row, and it is a **strictly wider**
table rather than a ladder: ``read-only`` reaches ``danger-full-access`` in one
hop, since the blueprint's row (``escalation.ts:28-31``) lists it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Sequence

from emrg.sandbox.policy import DANGER_FULL_ACCESS, SANDBOX_MODES

logger = logging.getLogger(__name__)

__all__ = [
    "APPROVAL_TIMEOUT_SECONDS",
    "ESCALATION_TARGETS",
    "ApprovalOutcome",
    "EscalationRefused",
    "Hop",
    "RETRY_HINT",
    "advertises_escalation",
    "approve_escalation",
    "hops_from",
    "validate_hop",
    "validate_pairing",
    "with_retry_hint",
]

#: Every target a composition advertises, in the schema's ``enum``.  The
#: blueprint advertises the whole set whenever the mounted capability can
#: confine, rather than trimming it to the current tier (``escalation.ts:41``):
#: the model is told what is *reachable in principle* and the hop table decides
#: what is reachable from where it actually stands.
ESCALATION_TARGETS: tuple[str, ...] = ("workspace-write", "danger-full-access")

#: The strict-wider table, verbatim from ``escalation.ts:28-31``.  A mode with
#: no entry has nowhere to escalate to, and an entry listing no mode is the same
#: statement — both are read through :func:`hops_from` so there is one table.
_STRICTLY_WIDER: dict[str, tuple[str, ...]] = {
    "read-only": ("workspace-write", "danger-full-access"),
    "workspace-write": ("danger-full-access",),
    DANGER_FULL_ACCESS: (),
}

#: Appended to a refusal while the composition advertises escalation
#: (``escalation.ts:84-86``).  It names the two arguments and no more: the model
#: is being told how to ask, not whether it will be granted.
RETRY_HINT = (
    "retry this exact command once with sandbox_permissions + justification"
)

#: How long a client has to answer an approval question before the answer is
#: taken as a refusal.  A host who walked away has not approved anything.
APPROVAL_TIMEOUT_SECONDS = 120.0


class EscalationRefused(Exception):
    """An escalation that must not run, with the sentence the model is told.

    Raised rather than returned so no caller can forget to check: every path out
    of this module either widens the call or raises, and a caller that swallows
    the raise is left with the *unwidened* call, which is the safe direction.
    """


@dataclass(frozen=True)
class Hop:
    """One permitted widening: where the call stands, and where it may go."""

    from_mode: str
    to_mode: str

    def describe(self) -> str:
        return f"{self.from_mode} → {self.to_mode}"


@dataclass(frozen=True)
class ApprovalOutcome:
    """What the channel answered, and what was done with it.

    ``approved`` is the only thing a caller acts on; ``answered`` and
    ``reason`` exist so a refusal can say *why* (no client, timeout, a "no") —
    a host reading a log should not have to guess which of the three happened.
    """

    approved: bool
    answered: bool
    reason: str


def hops_from(mode: str) -> tuple[str, ...]:
    """Every tier `mode` may escalate to in one hop.

    :param mode: the effective mode of the call about to run.
    :returns: the strictly wider tiers, empty when there are none.
    """
    return _STRICTLY_WIDER.get(mode, ())


def advertises_escalation(mode: str) -> bool:
    """Whether a call standing at `mode` may be told about escalation at all.

    The blueprint adds the retry hint only when the composition advertises the
    field (``tool-bash/src/render.ts:43-51``).  Since a tier with no wider
    neighbour cannot use the field, advertising it there would be an instruction
    that can only ever fail — so the condition is "this mode has a hop", which is
    the honest reading of the blueprint's rule for a deployment whose default
    tier is configurable per task.
    """
    return bool(hops_from(mode))


def validate_pairing(
    sandbox_permissions: object, justification: object,
) -> tuple[str | None, str | None]:
    """Check that the two escalation arguments arrive together and sane.

    The blueprint's rule (``escalation.ts:51-61``): ``sandbox_permissions`` and
    ``justification`` are a pair, and the justification must be a non-empty
    sentence.  A lone ``sandbox_permissions`` is a model asking for something
    without saying why; a lone ``justification`` is a stray field.  Neither is an
    escalation request, and both are refused rather than silently ignored — an
    ignored field is a model that will keep sending it.

    :returns: ``(target, justification)``, each ``None`` when neither argument
        was supplied at all.
    :raises EscalationRefused: when exactly one is present, or when the
        justification is empty.
    """
    target = None if sandbox_permissions is None else str(sandbox_permissions).strip()
    reason = None if justification is None else str(justification).strip()
    if target is None and reason is None:
        return None, None
    if not target:
        raise EscalationRefused(
            "sandbox_permissions and justification must be supplied together, and "
            "sandbox_permissions must name a tier"
        )
    if not reason:
        raise EscalationRefused(
            "sandbox_permissions must be accompanied by a non-empty justification "
            "saying why this command needs it"
        )
    return target, reason


def validate_hop(current_mode: str, target: str) -> Hop:
    """Check that `target` is a tier `current_mode` may widen to in one hop.

    :raises EscalationRefused: when the target is unknown, is the current tier,
        or is not in the current tier's row — the case the schema cannot see,
        because the schema validates the string and not the call's position.
    """
    if target not in SANDBOX_MODES:
        raise EscalationRefused(
            f"sandbox_permissions must name one of {', '.join(SANDBOX_MODES)} "
            f"(got {target!r})"
        )
    allowed = hops_from(current_mode)
    if target not in allowed:
        reachable = ", ".join(allowed) if allowed else "nothing"
        raise EscalationRefused(
            f"this call runs at {current_mode!r} and may escalate to {reachable} in "
            f"one hop; {target!r} is not one of them — a hop reaches only the tiers "
            f"this call's row lists"
        )
    return Hop(from_mode=current_mode, to_mode=target)


async def approve_escalation(
    *,
    current_mode: str,
    target: str,
    justification: str,
    ask: Callable[[str], Awaitable[object]] | None,
    session_id: str | None = None,
    timeout: float = APPROVAL_TIMEOUT_SECONDS,
) -> ApprovalOutcome:
    """Run the ordered fail-closed sequence: table, then channel, then answer.

    The order is the blueprint's (``approveEscalation``, ``escalation.ts:153``)
    and each step exists to keep a later one from being asked a question it
    cannot answer:

    1. **Strictly wider.**  Checked first, so a two-hop request never becomes a
       question a host could say yes to — an approval channel that can authorise
       anything is not a safety mechanism.
    2. **The channel resolves.**  ``ask`` is ``None``, or it raises, or it
       answers nothing in time: all three are *refusals*, because each of them
       means nobody agreed to anything.
    3. **The answer maps.**  Only an explicit affirmative is an approval; a
       missing key, a null, a string nobody can read is a refusal.  This is the
       step that keeps "the host closed the window" from reading as consent.

    :param ask: the channel, taking the question text and returning whatever the
        client said.  ``None`` means there is no channel at all.
    :raises EscalationRefused: when the hop itself is invalid.
    :returns: the outcome; ``approved`` is what the caller acts on.
    """
    hop = validate_hop(current_mode, target)

    if ask is None:
        logger.warning(
            "escalation %s refused: no approval channel for session %s",
            hop.describe(), session_id,
        )
        return ApprovalOutcome(False, False, "no-approval-channel")

    question = (
        f"Approve a one-hop sandbox escalation for this command only?\n"
        f"  {hop.describe()}\n"
        f"  justification: {justification}"
    )
    try:
        answer = await asyncio.wait_for(ask(question), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "escalation %s refused: the approval channel did not answer within %.0fs",
            hop.describe(), timeout,
        )
        return ApprovalOutcome(False, False, "timeout")
    except Exception:
        logger.exception(
            "escalation %s refused: the approval channel failed", hop.describe(),
        )
        return ApprovalOutcome(False, False, "channel-failed")

    approved = _read_answer(answer)
    if approved is None:
        logger.warning(
            "escalation %s refused: unreadable answer (%r)", hop.describe(), answer,
        )
        return ApprovalOutcome(False, False, "unreadable-answer")
    if not approved:
        logger.info("escalation %s refused by the host", hop.describe())
        return ApprovalOutcome(False, True, "denied")
    logger.warning(
        "escalation %s APPROVED for one call (justification: %s, session %s)",
        hop.describe(), justification, session_id,
    )
    return ApprovalOutcome(True, True, "approved")


#: The spellings a client may use for "yes" / "no".  A boolean is the contract;
#: the words are accepted because two clients ship the frame and a wire format
#: that only one of them can produce is a bug waiting for a second implementation.
_AFFIRMATIVE = frozenset({"yes", "y", "true", "approve", "approved", "ok", "allow"})
_NEGATIVE = frozenset({"no", "n", "false", "deny", "denied", "reject", "rejected"})


def _read_answer(answer: object) -> bool | None:
    """Map a client's answer onto a boolean, or ``None`` when it cannot be read.

    ``None`` is deliberately not ``False``: the caller reports *why* it refused,
    and "the host said no" and "nobody could tell what that was" are different
    events that a log should not merge.
    """
    if isinstance(answer, bool):
        return answer
    if isinstance(answer, str):
        word = answer.strip().lower()
        if word in _AFFIRMATIVE:
            return True
        if word in _NEGATIVE:
            return False
        return None
    if isinstance(answer, dict):
        for key in ("approved", "allow", "answer"):
            if key in answer:
                return _read_answer(answer[key])
    return None


def with_retry_hint(text: str, *, mode: str, advertised: Iterable[str] | None = None) -> str:
    """Append the retry hint to a refusal, when escalation is advertised here.

    The blueprint's conditional behaviour (``tool-bash/src/render.ts:43-51``):
    the line appears only when the composition advertises the field.  `mode` is
    the tier the refused call ran at, and a tier with no hop has nothing to
    advertise — so a refusal at ``danger-full-access`` is reported as it always
    was, with no instruction that could only fail.

    Idempotent: a text that already carries the hint is returned unchanged, since
    a refusal can pass through here twice (the tool renders, then the caller
    reports).
    """
    if RETRY_HINT in text:
        return text
    if advertised is None and not advertises_escalation(mode):
        return text
    targets: Sequence[str] = (
        tuple(advertised) if advertised is not None else hops_from(mode)
    )
    if not targets:
        return text
    return f"{text}\n[hint] {RETRY_HINT} (available here: {', '.join(targets)})"
