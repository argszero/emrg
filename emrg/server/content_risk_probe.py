"""Find the record a content filter refuses, and take it out of the history.

Rant `2026-09-29T15:55:44.643795+08:00` (requirement L2 of the content-risk
series). The sibling requirement L1 — replace offending codepoints, fall back to
spacing — is a cure for one **class** of trigger, and the rant that measured it
says so in its own words: *the trigger this was measured against is an astral
codepoint pair; if the trigger is a word or a phrase, replacing codepoints cannot
help — that is where L2 comes in.* When the trigger is a word, the only cure is to
take the offending record out of the session history, and that is what this module
does.

Two properties shape every line below.

**Out of band, and logarithmic.** Asking the provider "is it this one?" once per
record is not a cure, it is a second poison: the measured session was 131 messages
and ~738k prompt tokens, so a linear scan is hundreds of requests. The provider's
refusal is monotone in the records it is shown — a payload is refused iff the
triggering record is in it — so a bisection locates the record in
`ceil(log2(n)) + 1` probes. The search does not assume monotonicity blindly: when
neither half is refused on its own while the whole is, it stops and says the
trigger is a **combination**, rather than deleting something to look decisive.

**It never echoes the trigger.** The upstream plugin's own verification probe
printed the triggering text into the session history, and every session that read
it back was killed by the same filter again — *reading it back poisons the
reader*. So nothing here writes, logs or returns record text: a record is named by
position, length, digest and kind, and a search result carries those and nothing
else. `tests/test_content_risk_probe.py` fails if the trigger appears anywhere in
what the search produces.

The provider is reached through a `RefusalProbe` the caller supplies — a callable
taking a candidate record list and answering "does the provider still refuse
this?". Keeping it a parameter is what makes all of this testable offline: the
tests hand in a stub, and no test in this file talks to a provider.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "ProbeBudgetExceeded",
    "ProbeTrace",
    "Record",
    "RefusalProbe",
    "ScrubResult",
    "describe_record",
    "find_offending_index",
    "records_to_remove",
    "scrub_session",
]

Record = dict
"""One line of `history.jsonl`, as `Session._read_history` hands it back."""

RefusalProbe = Callable[[Sequence[Record]], bool]
"""Answers whether the provider still refuses a payload built from these records.

The caller owns what "a payload built from these records" means — the daemon's
probe walks the same conversion the live request does, so the question asked is
the question the refusal answered. `True` means refused.
"""


class ProbeBudgetExceeded(RuntimeError):
    """The search asked more questions than a logarithmic search can need.

    Raised rather than returning a guess: a search that has lost its bound is a
    search whose answer is not worth acting on, and acting on it means deleting a
    record from a host's conversation.
    """


@dataclass(frozen=True)
class ProbeTrace:
    """What the search is allowed to say about a record: never its text.

    `digest` covers the serialized record, so two candidates can be told apart in
    a report without either being quoted; `kind` is the record's own type and role
    ("message/user", "tool_result"), which is what a host needs to recognise the
    thing that was removed.
    """

    index: int
    length: int
    digest: str
    kind: str


@dataclass
class ScrubResult:
    """The outcome of one search, and of the removal it justified.

    `resolved` is False when the search could **not** name a single culprit — a
    trigger spread across records, or a payload refused for a reason the records
    do not carry (a system prompt of the caller's, for instance). An unresolved
    search removes nothing: a wrong removal costs the host their conversation, and
    an honest "I could not find it" costs them one turn.
    """

    resolved: bool
    index: int | None = None
    removed: int = 0
    probes: int = 0
    reason: str = ""
    traces: list[ProbeTrace] = field(default_factory=list)


def describe_record(index: int, record: Record) -> ProbeTrace:
    """Name a record without quoting it (length, digest, kind — never content)."""
    try:
        blob = json.dumps(record, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # a record that cannot be serialized is still a record
        blob = repr(type(record))
    kind = str(record.get("type", "?"))
    role = record.get("role")
    if role:
        kind = f"{kind}/{role}"
    return ProbeTrace(
        index=index,
        length=len(blob),
        digest=hashlib.blake2b(blob.encode("utf-8"), digest_size=8).hexdigest(),
        kind=kind,
    )


def _probe_budget(n: int) -> int:
    """`ceil(log2(n)) + 2` questions, which is what a bisection can need.

    The `+2` is honest accounting rather than slack: one question decides whether
    the records are implicated at all, and one more is needed whenever an odd
    split has to be re-tested to tell "the first half" from "neither half".
    """
    if n <= 1:
        return 3
    return math.ceil(math.log2(n)) + 2


def find_offending_index(
    records: Sequence[Record],
    is_refused: RefusalProbe,
    *,
    max_probes: int | None = None,
) -> tuple[int | None, int, str, list[ProbeTrace]]:
    """Locate the record whose presence makes `is_refused` answer True.

    Returns `(index | None, probes, reason, traces)`. `index` is None when the
    records are not implicated (nothing was refused to begin with, or the refusal
    survives their removal) or when the trigger is a combination of records rather
    than one of them; `reason` says which, because those are different findings a
    host should be able to tell apart.

    The candidate list is halved by asking about its **first** half: a payload built
    from a subset is refused exactly when the subset holds the culprit, so a refused
    half is the half to descend into and the other one is implied — one question per
    level, not two, which is what keeps the cost at `log2(n)` rather than `2*log2(n)`.
    The single record the descent lands on is then asked about **alone**: that last
    question is what separates "this record is the trigger" from "these records are
    refused together", and it is the difference between removing one and removing none.
    Records are described, never quoted.
    """
    records = list(records)
    budget = _probe_budget(len(records)) if max_probes is None else max_probes
    probes = 0
    traces: list[ProbeTrace] = []

    def ask(candidates: list[int]) -> bool:
        nonlocal probes
        probes += 1
        if probes > budget:
            raise ProbeBudgetExceeded(
                f"the content-risk search asked {probes} questions of a "
                f"{len(records)}-record history, past the {budget} a bisection "
                "can need — stopping rather than guessing"
            )
        return bool(is_refused([records[i] for i in candidates]))

    if not records:
        return None, probes, "no-records", traces

    everything = list(range(len(records)))
    if not ask(everything):
        return None, probes, "records-not-implicated", traces

    candidates = everything
    while len(candidates) > 1:
        middle = len(candidates) // 2
        first = candidates[:middle]
        candidates = first if ask(first) else candidates[middle:]

    index = candidates[0]
    if not ask([index]):
        # The whole is refused and this record alone is not: the trigger is not in
        # any single record. Report it instead of deleting one to look decisive —
        # and report it by name, never by text.
        traces.extend(describe_record(i, records[i]) for i in candidates)
        return None, probes, "combination-trigger", traces

    traces.append(describe_record(index, records[index]))
    return index, probes, "found", traces


def records_to_remove(records: Sequence[Record], index: int) -> list[int]:
    """The positions to remove so the history stays one a provider will accept.

    A tool round is an assistant message carrying `tool_calls` plus the
    `tool_result` records answering it, and providers reject a history that holds
    one without the other — so a removal that takes one side and leaves the other
    does not cure the session, it breaks it. Which side the culprit is on decides
    which way the round is taken out:

    - the culprit is the assistant message → it and every result answering it;
    - the culprit is a result → the whole round, the call and all its results,
      because a call with no results is the same malformed pair from the other
      side;
    - anything else (a user or system message, a summary) → itself alone.
    """
    if not 0 <= index < len(records):
        return []
    culprit = records[index]
    if culprit.get("type") == "message" and culprit.get("tool_calls"):
        ids = {
            str(tc.get("id"))
            for tc in culprit.get("tool_calls") or []
            if isinstance(tc, dict) and tc.get("id")
        }
        out = [index]
        for i, r in enumerate(records):
            if r.get("type") == "tool_result" and str(r.get("tool_call_id")) in ids:
                out.append(i)
        return sorted(out)
    if culprit.get("type") == "tool_result":
        return sorted(_tool_round_positions(records, str(culprit.get("tool_call_id") or "")))
    return [index]


def _tool_round_positions(records: Sequence[Record], tool_call_id: str) -> set[int]:
    """Every position belonging to the round `tool_call_id` is part of."""
    if not tool_call_id:
        return set()
    call_ids: set[str] = set()
    owner: int | None = None
    for i, r in enumerate(records):
        if r.get("type") != "message" or not r.get("tool_calls"):
            continue
        ids = {
            str(tc.get("id"))
            for tc in r.get("tool_calls") or []
            if isinstance(tc, dict) and tc.get("id")
        }
        if tool_call_id in ids:
            owner = i
            call_ids = ids
            break
    out: set[int] = set()
    if owner is not None:
        out.add(owner)
    for i, r in enumerate(records):
        if r.get("type") == "tool_result":
            rid = str(r.get("tool_call_id") or "")
            if rid == tool_call_id or (call_ids and rid in call_ids):
                out.add(i)
    return out


def scrub_session(
    session,
    is_refused: RefusalProbe,
    *,
    max_probes: int | None = None,
) -> ScrubResult:
    """Locate the refused record in a session's history and remove it.

    The write goes through `Session.drop_history_records`, which recomputes the
    session's message count the same way `compact` does — a removal that leaves
    the count stale is the defect that comment already records for compaction, and
    it would show the host a conversation longer than the one on disk.

    `session` is duck-typed on purpose (`_read_history` / `drop_history_records`):
    a test drives a real `Session` on a temporary directory, and no provider is
    reached from here — the caller's `is_refused` owns that.
    """
    records = list(session._read_history())
    index, probes, reason, traces = find_offending_index(
        records, is_refused, max_probes=max_probes
    )
    if index is None:
        result = ScrubResult(resolved=False, probes=probes, reason=reason, traces=traces)
        # Facts only: how big the history was and how the search ended. The
        # trigger is never part of this line, by construction (see module docstring).
        logger.warning(
            "content-risk scrub: no single offending record in %d record(s) "
            "(%d probe(s), %s) — nothing removed",
            len(records), probes, reason,
        )
        return result

    doomed = records_to_remove(records, index)
    removed = session.drop_history_records(doomed)
    logger.warning(
        "content-risk scrub: removed record %d (%s, %d byte(s)) with %d "
        "companion record(s) after %d probe(s)",
        index, traces[-1].kind if traces else "?", traces[-1].length if traces else 0,
        max(0, len(doomed) - 1), probes,
    )
    return ScrubResult(
        resolved=True, index=index, removed=removed, probes=probes,
        reason="found", traces=traces,
    )
