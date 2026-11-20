"""The life value an agent carries — the bands, the words, and the write.

An agent's life value answers "is this agent still alive?" in one integer that a
judge agent writes and nothing else does. It belongs to the agent, not to a
session: a session ends, the agent goes on, and the value is about the agent.

**The arithmetic here is a boundary table and nothing more.** Everything about
*how* a score is decided belongs to the judge's own prompt as prose, because the
moment a rule for "what is alive" is spelled as a formula, the judge stops
judging and starts computing. What code owns is what a reader needs: which of the
four bands a number falls in, and what the per-round context says about it.

The bands, and the reason the edges read the way they do — *below* ten is dying,
so a ten survives; *below* a hundred is healthy, so a hundred is healthy and the
first value above it thrives. None of them is a cap: the value has no upper
bound, and a number far above the top band is simply thriving.

**The judge is an agent like any other, and the one thing it does not share is
the write.** It reads and writes any agent's value; every other agent reads its
own and has no channel to write one. That is not enforced here — it is enforced
where a turn's tool list is decided, so a tool the judge alone carries is a tool
no other agent is ever offered, and a forged call names a tool that turn does not
hold.
"""

from __future__ import annotations

import datetime

from emrg.config import config_dir

#: The judge agent's registered name. One constant, because three readers ask for
#: it: the register's installer, the turn that decides whether to offer the write
#: tool, and the prompt that tells the judge what it is.
JUDGE_AGENT_NAME = "命"

#: The directory the judge agent runs in, as a child of the installation's config
#: directory. It is a directory rather than a name in a table because an agent
#: here *is* a directory — that is what a session's working directory resolves to,
#: and what the register's rows point at.
JUDGE_DIR_NAME = "life-judge"

#: The band table: the smallest value that reaches each band, highest first. A
#: value is in the first band whose floor it reaches.
_BANDS: tuple[tuple[int, str], ...] = (
    (101, "thriving"),
    (50, "healthy"),
    (10, "surviving"),
)

#: What a value below every floor is. It is the one band with no floor, which is
#: why it is not a row in the table above.
_DYING = "dying"


def judge_dir():
    """Where the judge agent's directory lives. A pure path; nothing is created."""
    return config_dir() / JUDGE_DIR_NAME


def tier_of(value: int) -> str:
    """The band a life value falls in — one word, the same word the context shows.

    Exact at both edges and deliberately so: the boundaries read "below ten",
    "below fifty", "below a hundred" and "above a hundred". A hundred is in neither
    of the last two statements taken literally, so the table settles it the way the
    sentence's own grammar points — a hundred is *not* above a hundred, so it is
    healthy, and the first value that thrives is a hundred and one.
    """
    for floor, name in _BANDS:
        if value >= floor:
            return name
    return _DYING


def parse_instant(moment: object) -> datetime.datetime | None:
    """A stored instant as a timezone-aware ``datetime``, or ``None``.

    The register writes ``datetime.now().isoformat()`` and nothing else, so this is
    a narrow reader rather than a general parser: a value it cannot read answers
    ``None``, and every caller treats that as "no instant recorded" rather than as
    an error. A life value whose time cannot be read is still a value.
    """
    if not isinstance(moment, str) or not moment.strip():
        return None
    try:
        parsed = datetime.datetime.fromisoformat(moment.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def life_section(row: dict) -> str:
    """The life section of the per-round context for one agent's register row.

    Two things a reader needs, and a third that makes the second useful: the value
    and its band, and the last change's instant, size and reason. A row with no
    value reads as unjudged rather than as a number, because a zero and "nobody has
    looked yet" are different states and only one of them is a judgement.

    The section is built here rather than in the context builder for one reason: it
    has one producer, so the context, the tests that hold its placement and any
    later reader cannot drift apart.
    """
    value = row.get("life")
    if not isinstance(value, int) or isinstance(value, bool):
        return (
            "[context] Agent life (this session's agent): unjudged — no life value "
            "has been written for it yet."
        )
    lines = [f"[context] Agent life (this session's agent): {value} ({tier_of(value)})"]
    when = parse_instant(row.get("life_at"))
    delta = row.get("life_delta")
    reason = row.get("life_reason")
    if when is not None:
        change = f"last change {when.isoformat(timespec='seconds')}"
        if isinstance(delta, int) and not isinstance(delta, bool):
            change += f", {delta:+d}"
        if isinstance(reason, str) and reason.strip():
            change += f" — {reason.strip()}"
        lines.append(change)
    return "\n".join(lines)
