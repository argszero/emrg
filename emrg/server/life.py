"""The life value an agent carries — the bands, the words, and the write.

An agent's life value answers "is this agent still alive?" in one integer that a
judge agent writes and nothing else does. It belongs to the agent, not to a
session: a session ends, the agent goes on, and the value is about the agent.

**The arithmetic here is a boundary table and nothing more.** Everything about
*how* a score is decided belongs to the judge's own prompt as prose, because the
moment a rule for "what is alive" is spelled as a formula, the judge stops
judging and starts computing. What code owns is what a reader needs: which of the
four bands a number falls in, and what the per-round context says about it.

**The bands, and where the edges live.** Four words: dying, surviving, healthy,
thriving — the four the per-round context prints and the read tool answers. The
floors that separate them are `_BANDS` below and nowhere else: that table is the
only place a boundary is written, and
`tests/test_agent_life.py::test_the_bands_fall_where_the_boundaries_read` walks
both sides of every one of them, so an off-by-one reddens a test instead of
printing a band that nothing disagrees with. A sentence restating a floor is a
second statement of a number the guard already holds, and it decays where the
guard does not. None of the bands is a cap: the value has no upper bound, and a
number far above the top band is simply thriving.

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

    Walks `_BANDS` and adds nothing to it: each floor is the smallest value that
    reaches its band, so the edge is exact on both sides — the value at a floor
    takes the higher band, and the value below it does not. No floor is restated
    here. The table is where a boundary is written, and the boundary test holds
    both sides of every one of them.
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
