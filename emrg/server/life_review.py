"""The roster one life-review round is handed, and the gate that skips an empty one.

A half-hour round exists so the judge writes a score without a person running it by
hand. The rule that shapes this module is the one a scheduled task does not have by
itself: **a round that has nobody to judge must not start**. `TaskHandler.run()` knows
only *when* a round is due — the wait ends and the cycle is dispatched — so the
question "is there anybody to hand over?" has to be asked between the two, and asked
here rather than inside the cycle, because a cycle that starts and then discovers it
has no subject has already spent the request.

Two facts decide the roster, and they are different questions:

* **who is on it** — the agents whose register row shows activity inside the window.
* **how much each one presents** — the whole absence, from the instant it was last
  judged. An agent that has been away a fortnight and comes back presents those two
  weeks, not the half hour that happens to be the schedule. That span is why the
  window's own start is not the answer: the window decides *membership*, and the last
  judgement decides *reach*.

The watermark is written **before** the round runs, not after it. A round that dies
mid-way has then consumed its window, which sounds like a loss and is not: the agents
it was about still carry their old `life_at`, so the next roster presents their spans
again. Writing it after the round would be the worse arrangement in the other
direction — a crash would leave the same window to be presented for ever, and a
window already judged twice is two scores for one stretch of work.

The judge is not on its own roster. It is an agent like any other and the register can
hold its value, but a round whose trigger is the judge's own activity would be
presenting the judge's presence as the evidence for the judge's score — a closed loop
rather than a judgement. A row nobody has judged reads as unjudged, which is the
honest state for that one, and it is the state the register already renders.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import tempfile
from pathlib import Path

from emrg.config import config_dir
from emrg.server.life import JUDGE_AGENT_NAME, parse_instant, tier_of
from emrg.tools.base import non_regular_kind

#: The built-in task type that runs a review round. One constant, because three
#: readers ask for it: the template table, the gate, and the README row a guard
#: compares against.
LIFE_REVIEW_TASK_TYPE = "life-review"


def watermark_dir() -> Path:
    """Where a review task's last-check instant lives. A pure path."""
    return config_dir() / "life-review"


def watermark_path(task_name: str) -> Path:
    """The last-check instant's file for one task. A pure path."""
    return watermark_dir() / f"{task_name}.json"


def read_watermark(path: Path) -> datetime.datetime | None:
    """The last-check instant, or ``None`` when there is none to read.

    ``None`` is also what an unreadable or absent file answers, and the caller treats
    it as "no window has been settled yet" — the first round then presents every agent
    that has ever been active, which is what an unvisited register deserves. A file
    that exists but cannot be parsed is the same absence: refusing the whole round over
    a damaged watermark would stop the review for ever and say nothing about it.

    The kind is asked before the open, and a subject that is not a regular file answers
    ``None`` for the same reason a damaged one does: a FIFO at this path would block the
    open until a peer appeared, and this reader runs on the scheduler's tick — the whole
    daemon, not one round. Refusing the round over it would stop the review for ever,
    which is the outcome this function's ``None`` exists to avoid.
    """
    try:
        kind = non_regular_kind(path)
    except OSError:  # absent, unreadable, or not statable: all one absence here
        return None
    if kind:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return parse_instant(raw.get("since"))


def write_watermark(path: Path, moment: datetime.datetime) -> None:
    """Record that everything before ``moment`` has been presented to the judge.

    Written through a temporary file in the same directory and then renamed, so a
    reader never sees half a file: this is the one value whose partial form would be
    read as "no window yet" and re-present a window already judged.

    The temporary file is *created* rather than named: ``mkstemp`` makes it with
    ``O_CREAT | O_EXCL`` under a name nobody can predict, so this writer opens a subject
    it has just brought into being. Naming the temporary file ``<name>.tmp`` instead
    would open a path this process does not own, and a FIFO planted there would block
    the open on the scheduler's tick — the daemon, not the round.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"since": moment.isoformat()}, ensure_ascii=False)
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
        os.replace(tmp, path)
    except BaseException:
        # The rename did not happen, so the temporary file is ours to remove. A failure
        # here must not mask the one that brought us in.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def roster(
    rows: list[dict],
    since: datetime.datetime | None,
    now: datetime.datetime | None = None,
) -> list[dict]:
    """The agents one round is about, each carrying the whole absence it presents.

    ``since`` is the last settled window's start; ``None`` means nothing has been
    settled, so every row that has ever recorded activity is on the roster.

    A row with no readable ``last_active`` is not on it: activity is the only thing
    that decides membership, and an instant that cannot be read is not evidence of
    any. It stays in the register with whatever value it has — being absent from one
    round is not a judgement, and the roster is not the register.
    """
    moment = now or datetime.datetime.now().astimezone()
    out: list[dict] = []
    for row in rows:
        name = str(row.get("name") or "")
        if not name or name == JUDGE_AGENT_NAME:
            continue
        active = parse_instant(row.get("last_active"))
        if active is None:
            continue
        if since is not None and active < since:
            continue
        judged = parse_instant(row.get("life_at"))
        # The reach is the last judgement, or the window's own start for a row nobody
        # has judged — the only instant this module can honestly name for it.
        start = judged or since or active
        value = row.get("life")
        out.append({
            "name": name,
            "path": str(row.get("path") or ""),
            "last_active": active,
            "judged_at": judged,
            "since": start,
            "span": moment - start,
            "life": value if isinstance(value, int) and not isinstance(value, bool) else None,
        })
    return out


def skip_reason(
    rows: list[dict],
    since: datetime.datetime | None,
    now: datetime.datetime | None = None,
) -> str | None:
    """Why this round must not start, or ``None`` when it may.

    One subject and one answer, so the gate is a pure function a test can ask without
    building a handler: an empty roster is the whole rule.
    """
    if roster(rows, since, now):
        return None
    if since is None:
        return "no agent has recorded any activity yet"
    return f"no agent has been active since {since.isoformat(timespec='seconds')}"


def _counted(count: int, unit: str) -> str:
    """A count with its unit, in the number the count calls for.

    Two windows reach a count of one — a minute at 90 seconds and an hour at 90
    minutes — so the plural alone would tell the judge "1 hours". Both windows are
    asserted in `tests/test_life_review.py::test_a_span_of_one_unit_says_the_singular`,
    which is what keeps a unit added later from shipping with the plural only.
    """
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def describe_span(delta: datetime.timedelta) -> str:
    """A span in the words a reader uses, coarse on purpose.

    The judge is asked what a stretch of work looks like, not to compare durations
    against a table, so the span is stated at the coarseness a person would use and
    never as a number of seconds. A negative or zero span reads as "just now" rather
    than as a duration that has not happened.

    Every unit word follows its count, singular included: this string is read as a
    sentence ("the stretch to settle is …"), where the wrong number is a wrong word
    rather than a rounding.
    """
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 90:
        return _counted(minutes, "minute")
    hours = minutes // 60
    if hours < 48:
        return _counted(hours, "hour")
    days = hours // 24
    if days < 14:
        return _counted(days, "day")
    return _counted(days // 7, "week")


def render_roster(entries: list[dict]) -> str:
    """The roster as the lines the judge's prompt carries.

    Both readings a judgement needs are here and no third: when the agent was last
    seen working, and what its value was before this round. The span is stated beside
    the window it spans so a returning agent is not read as a busy one.

    A row with no value says so in words. ``0`` and "nobody has looked yet" are
    different states and only one of them is a judgement, so the absence is spelled
    rather than rendered as a number that would be read as a verdict.
    """
    if not entries:
        return "(nobody was active in this window)"
    lines: list[str] = []
    for entry in entries:
        when = entry["last_active"].isoformat(timespec="seconds")
        if entry["life"] is None:
            value = "never judged"
        else:
            value = f"{entry['life']} ({tier_of(entry['life'])})"
        lines.append(
            f"- `{entry['name']}` — last active {when}; previous value {value}; "
            f"the stretch to settle is {describe_span(entry['span'])}"
        )
    return "\n".join(lines)
