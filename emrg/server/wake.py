"""The `when` conditions — what may wake a scheduled task ahead of its own cadence.

A task carries one frequency: `interval`, or the windows `schedule` states. That is how
often it runs **on its own initiative**, and nothing here changes it. `when` adds an
optional mapping from a condition name to a number of seconds, and it only ever makes a
task run *sooner*: while a condition holds, the task is woken again at most that many
seconds later. A task without `when` is unchanged, and a `when` never suppresses a run
the task would have taken anyway (issue #2592).

Conditions are **built in**. A host writes a condition name and a number of seconds —
never a command, a script or a function name — so `CONDITIONS` is the whole vocabulary,
and an unknown name is refused with the list of the known ones rather than ignored.

Every evaluator answers one of three things, and the third one is not the second:

* `HOLDS`  — the condition is true; wake the task.
* `ABSENT` — the condition was read, and it is not true.
* `UNKNOWN` — it could not be read at all. This is **not** "nothing to do": an
  unauthenticated CLI says nothing about whether a message is waiting. A wake policy
  still has to act, so `UNKNOWN` leaves the task asleep and logs the reason. It never
  raises, never blocks another condition, and never stops the task's own rhythm.

The watch is a poll: each condition is consulted once per its own configured seconds,
inside the wait the scheduler already performs. There is no second, event-push path, and
no writer anywhere is modified to announce anything — that is what keeps the pop-up of a
rant out of the scheduler's business (issue #2592, defaults 3, 7).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from emrg.server.rants import default_rants_log, list_rants

logger = logging.getLogger("emrg.server.wake")

#: The floor on a `when` value, and the same bound `scheduler.MIN_INTERVAL` puts on
#: `interval`: a condition may not be polled faster than a task is allowed to run, or
#: the field would become a way around that bound. `tests/test_scheduler.py` asserts the
#: two numbers agree, so the second carrier cannot drift from the first in silence.
MIN_WHEN_SECONDS = 60

HOLDS = "holds"
ABSENT = "absent"
UNKNOWN = "unknown"

#: The CLI that speaks the Feishu IM API, and its own patience. Resolved from PATH at
#: call time, never stored: a deployment that has never installed it is a condition that
#: answers `UNKNOWN`, not an error at import.
LARK_CLI = "lark-cli"
_CLI_TIMEOUT = 120

#: How many of the host's most recently active conversations one poll reads. Each chat
#: is one CLI call in the foreground, so this is a budget on wall-clock time, not on
#: correctness: a chat outside the window is read on a poll where it is more recent.
FEISHU_MAX_CHATS = 5

#: The window each Feishu poll re-reads. Feishu reports message times to the minute, so
#: a time can never be a cursor: consecutive polls overlap deliberately and the message
#: id is what decides, which is why `seen` exists at all (issue #2592, default 4).
FEISHU_LOOKBACK_SECONDS = 900

#: How many message ids the watermark remembers. Bounded because the file is read and
#: rewritten on every poll: older ids fall out, and the lookback window is what makes
#: that safe — an id older than the window is never re-read in the first place.
FEISHU_SEEN_CAP = 500


class ConditionUnavailable(Exception):
    """A condition could not be read. Never fatal, never a wake, never silent."""


@dataclass(frozen=True)
class Verdict:
    """What one condition answered, and why — the reason is what the log carries."""

    state: str
    reason: str = ""

    @property
    def holds(self) -> bool:
        return self.state == HOLDS


# ── the field's own rules ────────────────────────────────────────────────


def _is_seconds(value) -> bool:
    """A `when` value: a positive whole number of seconds at or above the floor."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= MIN_WHEN_SECONDS
    )


def validate_when(when) -> str | None:
    """Return why a `when` field cannot be run, or None when it is usable.

    One function for both the loader and the CRUD path, like `validate_schedule`: a
    record refused by one and accepted by the other is how a task ends up in the file
    that the daemon silently drops. Every refusal names the value it refused and the
    shape it expects, because "invalid when" leaves a host with a file and no next step.
    """
    if when is None:
        return None
    if not isinstance(when, dict):
        return (
            f"when of type {type(when).__name__}, not a mapping of condition to seconds"
        )
    if not when:
        return "an empty when mapping - drop the field for one cadence all day"
    known = ", ".join(sorted(CONDITIONS))
    for name, seconds in when.items():
        if not isinstance(name, str) or name not in CONDITIONS:
            return f"unknown when condition {name!r} (known: {known})"
        if not _is_seconds(seconds):
            return (
                f"when condition {name!r} has {seconds!r} seconds "
                f"(must be an integer >= {MIN_WHEN_SECONDS})"
            )
    return None


def normalised_when(record) -> dict[str, int]:
    """The usable pairs of a record's `when` — what scheduling may act on.

    Unusable is not an exception path here: `validate_when` is what refuses a bad field
    with a readable error, and this is what a record that slipped past every writer
    falls back to. Dropping one bad pair is the choice that keeps the other conditions
    working, and it matches `_normalise_schedule`'s treatment of a broken window.
    """
    when = record.get("when") if isinstance(record, dict) else None
    if not isinstance(when, dict):
        return {}
    return {
        name: seconds
        for name, seconds in when.items()
        if isinstance(name, str) and name in CONDITIONS and _is_seconds(seconds)
    }


def shortest_when_seconds(record) -> int | None:
    """The tightest `when` in this record, or None when it states none.

    The scheduler waits for the shortest one and consults each condition on its own
    clock inside that wait, so a slow condition never holds up a fast one.
    """
    seconds = list(normalised_when(record).values())
    return min(seconds) if seconds else None


# ── the watermark these conditions keep ──────────────────────────────────


def state_path(state_dir: Path, task_name: str) -> Path:
    return Path(state_dir) / f"{task_name}.json"


def load_state(state_dir: Path, task_name: str) -> dict:
    """The persisted watermark, or `{}`.

    An unreadable file is a fresh start with a log line, never a refusal: this state is
    an optimisation that prevents a duplicate wake, and losing it costs one redundant
    poll rather than a wrong answer.
    """
    path = state_path(state_dir, task_name)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("wake state %s is not JSON — starting it over", path)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state_dir: Path, task_name: str, state: dict) -> None:
    """Write the watermark atomically; a failure is logged and nothing more."""
    path = state_path(state_dir, task_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("wake state %s could not be written: %s", path, exc)


def _instant(text) -> datetime | None:
    """`text` as an instant, or None when it is not one."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# ── the conditions ───────────────────────────────────────────────────────


def _condition_rant(*, agent: str, state: dict, now: datetime) -> Verdict:
    """Does this task's agent carry a rant that is not `completed`?

    Read through `rants.list_rants`, the store's own reader, so a legacy array row and
    the ledger's kind check are answered in one place. Nothing here writes: a condition
    that could mark a rant read would race the agent that is working it.
    """
    if not agent:
        return Verdict(UNKNOWN, "the task names no agent to look up")
    try:
        rows = list_rants(default_rants_log(), agent=agent)
    except OSError as exc:
        return Verdict(UNKNOWN, f"the rant log could not be read: {exc}")
    open_rows = [row for row in rows if row.get("status") != "completed"]
    if open_rows:
        return Verdict(
            HOLDS, f"{len(open_rows)} rant(s) not completed for {agent}"
        )
    return Verdict(ABSENT, f"no rant is open for {agent}")


def _condition_feishu(*, state: dict, now: datetime, runner, cli: str) -> Verdict:
    """Does anything on Feishu need the host: a real @, an @all, or a direct message?

    All three count as one question — "is the host being asked for something" — so one
    condition answers it rather than three the host would have to remember to list.
    """
    try:
        identity = _cli_json(["auth", "status", "--json"], runner=runner, cli=cli)
        host = _host_open_id(identity)
        if host is None:
            return Verdict(
                UNKNOWN, "auth status carried no identities.user.openId"
            )
        chats = _chat_items(
            _cli_json(
                [
                    "im", "+chat-list", "--types=p2p,group", "--sort=active_time",
                    "--as", "user", "--page-size", str(FEISHU_MAX_CHATS), "--json",
                ],
                runner=runner, cli=cli,
            )
        )
    except ConditionUnavailable as exc:
        return Verdict(UNKNOWN, str(exc))

    if not chats:
        # An empty list is not the same statement as "nothing was sent": it is what a
        # missing p2p scope, a token that has not refreshed and an account in no chats
        # all answer alike, and only one of those means the host has nothing waiting.
        return Verdict(UNKNOWN, "the conversation list came back empty")

    seen = {value for value in (state.get("seen") or []) if isinstance(value, str)}
    since = _instant(state.get("since")) or (now - timedelta(seconds=FEISHU_LOOKBACK_SECONDS))
    hits = 0
    for chat in chats[:FEISHU_MAX_CHATS]:
        chat_id = _chat_id(chat)
        if not chat_id:
            continue
        try:
            reply = _cli_json(
                [
                    "im", "+chat-messages-list", "--chat-id", chat_id,
                    "--start", since.isoformat(), "--order", "asc", "--as", "user",
                    "--no-reactions", "--json",
                ],
                runner=runner, cli=cli,
            )
        except ConditionUnavailable as exc:
            # One unreadable conversation makes the whole answer unknown: the message
            # that matters may be in exactly the one that could not be read.
            return Verdict(UNKNOWN, f"conversation {chat_id}: {exc}")
        for message in _message_items(reply):
            message_id = _message_id(message)
            if message_id is None or message_id in seen:
                continue
            if _needs_the_host(message, host=host, direct=_is_direct(chat)):
                hits += 1
            seen.add(message_id)

    state["seen"] = sorted(seen)[-FEISHU_SEEN_CAP:]
    state["since"] = (
        now - timedelta(seconds=FEISHU_LOOKBACK_SECONDS)
    ).isoformat()
    if hits:
        return Verdict(HOLDS, f"{hits} message(s) need the host")
    return Verdict(ABSENT, "nothing new needs the host")


#: The whole vocabulary a `when` may name. The key is what a host writes in `tasks.yml`.
CONDITIONS: dict[str, Callable[..., Verdict]] = {
    "rant": _condition_rant,
    "feishu": _condition_feishu,
}

#: What each condition needs handed to it, because they do not share an argument list:
#: `rant` reads a store and `feishu` shells out to a CLI through an injected runner.
_CONDITION_KWARGS = {
    "rant": ("agent", "state", "now"),
    "feishu": ("state", "now", "runner", "cli"),
}


def wakes(
    record,
    *,
    task_name: str,
    agent: str,
    state_dir: Path,
    runner: Callable[[Sequence[str]], object] | None = None,
    cli: str = LARK_CLI,
    now: datetime | None = None,
) -> tuple[bool, list[str]]:
    """Consult this record's `when` conditions; return (does any hold, notes).

    Each condition is consulted only once its own seconds have elapsed since the last
    time it was consulted, so a `when: {rant: 600, feishu: 300}` asks the rant store
    every ten minutes and Feishu every five, and neither waits on the other. The
    watermark is written after the round of checks, so a condition that could not answer
    is retried on the next poll rather than counted as read.

    A condition that raises is caught here: no evaluator may end the task's wait, and a
    bug in one must not take the scheduler's loop down with it.
    """
    when = normalised_when(record)
    if not when:
        return False, []
    moment = now or datetime.now()
    state = load_state(state_dir, task_name)
    checked = state.get("checked")
    if not isinstance(checked, dict):
        checked = {}
    runner = runner or default_runner

    holds = False
    notes: list[str] = []
    consulted = False
    for name in sorted(when):
        seconds = when[name]
        last = _instant(checked.get(name))
        if last is not None and (moment - last).total_seconds() < seconds:
            continue
        kwargs = {}
        for key in _CONDITION_KWARGS[name]:
            kwargs[key] = {
                "agent": agent, "state": state, "now": moment,
                "runner": runner, "cli": cli,
            }[key]
        try:
            verdict = CONDITIONS[name](**kwargs)
        except Exception as exc:  # noqa: BLE001 — a condition must never end the wait
            verdict = Verdict(UNKNOWN, f"the condition raised {type(exc).__name__}: {exc}")
        consulted = True
        notes.append(f"{name}: {verdict.state}" + (f" — {verdict.reason}" if verdict.reason else ""))
        if verdict.holds:
            holds = True
        if verdict.state != UNKNOWN:
            # A condition that could not be read keeps its old stamp, so the next poll
            # asks again instead of waiting out a whole interval on a failed read.
            checked[name] = moment.isoformat()

    state["checked"] = checked
    if consulted:
        save_state(state_dir, task_name, state)
    return holds, notes


# ── the CLI, and the shapes it answers ───────────────────────────────────


def default_runner(argv: Sequence[str]):
    """Run one CLI call in the foreground and return the completed process.

    Foreground, one at a time, and never backgrounded: a poll that detached would leave
    a condition nobody reads the answer of.
    """
    return subprocess.run(
        list(argv), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=_CLI_TIMEOUT, check=False,
    )


def _cli_json(args: Sequence[str], *, runner, cli: str) -> dict:
    """One CLI call, parsed. Every failure is `ConditionUnavailable`."""
    argv = [cli, *args]
    try:
        done = runner(argv)
    except OSError as exc:
        raise ConditionUnavailable(f"{cli} could not be run: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConditionUnavailable(f"{cli} did not answer in time: {exc}") from exc
    if getattr(done, "returncode", 1) != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise ConditionUnavailable(
            f"{cli} exited {done.returncode}: "
            f"{detail[-1] if detail else 'no output'}"
        )
    try:
        data = json.loads(done.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ConditionUnavailable(f"{cli} answered something that is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConditionUnavailable(f"{cli} answered {type(data).__name__}, not an object")
    if data.get("ok") is False:
        error = data.get("error")
        raise ConditionUnavailable(f"{cli} refused: {error!r}")
    return data


def _host_open_id(identity: dict) -> str | None:
    """The host's own open id, read at run time so no account constant is stored."""
    identities = identity.get("identities")
    if not isinstance(identities, dict):
        return None
    user = identities.get("user")
    if not isinstance(user, dict):
        return None
    for key in ("openId", "open_id"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _payload(data: dict) -> dict:
    """The `data` object of an answer, which is where the lists live."""
    inner = data.get("data")
    return inner if isinstance(inner, dict) else data


def _chat_items(data: dict) -> list[dict]:
    """The conversations an answer lists; a missing list is not an empty one."""
    payload = _payload(data)
    for key in ("items", "chats"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    raise ConditionUnavailable(f"the conversation list carried no items: {sorted(payload)}")


def _message_items(data: dict) -> list[dict]:
    """The messages an answer lists. `+chat-messages-list` answers `messages`."""
    payload = _payload(data)
    for key in ("messages", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    raise ConditionUnavailable(f"the message list carried no messages: {sorted(payload)}")


def _chat_id(chat: dict) -> str | None:
    for key in ("chat_id", "chatId", "id"):
        value = chat.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _is_direct(chat: dict) -> bool:
    """Is this a one-to-one conversation?

    Unknown answers **False** on purpose. A group read as direct would wake the task for
    every message in it, while a direct read as a group only misses a message with no
    mention in it — and the mention form is the one the other two cases cover. A
    deployment whose chat list omits the mode therefore loses the quieter half of one
    case rather than gaining noise in every conversation.
    """
    for key in ("chat_mode", "chatMode", "chat_type", "chatType", "mode", "type"):
        value = chat.get(key)
        if isinstance(value, str):
            return value.strip().lower() in ("p2p", "single", "direct")
    return False


def _message_id(message: dict) -> str | None:
    for key in ("message_id", "messageId", "id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _sender_of(message: dict) -> str | None:
    sender = message.get("sender")
    if isinstance(sender, dict):
        for inner in ("id", "open_id", "openId"):
            value = sender.get(inner)
            if isinstance(value, str) and value:
                return value
    for key in ("sender_id", "senderId"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _sender_is_app(message: dict) -> bool:
    """Was this sent by an application — this task's own bot, above all?

    Load-bearing: the bot writes to the host's own conversation, so a direct message
    with no sender filter would read as "the host is being asked something" every time
    the task itself answered him, and the task would wake itself in a loop.
    """
    sender = message.get("sender")
    for holder in (sender, message):
        if not isinstance(holder, dict):
            continue
        for key in ("sender_type", "senderType"):
            value = holder.get(key)
            if isinstance(value, str):
                return value.strip().lower() == "app"
    return False


def _mentions_of(message: dict) -> list[dict]:
    mentions = message.get("mentions")
    if not isinstance(mentions, list):
        return []
    return [item for item in mentions if isinstance(item, dict)]


def _mention_open_id(mention: dict) -> str | None:
    inner = mention.get("id")
    if isinstance(inner, dict):
        for key in ("open_id", "openId", "id"):
            value = inner.get(key)
            if isinstance(value, str) and value:
                return value
    elif isinstance(inner, str) and inner:
        return inner
    for key in ("open_id", "openId"):
        value = mention.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _is_all_mention(mention: dict) -> bool:
    key = mention.get("key")
    if isinstance(key, str) and key.strip().lower() in ("@_all", "all"):
        return True
    if _mention_open_id(mention) in ("all", "@_all"):
        return True
    name = mention.get("name")
    return isinstance(name, str) and name.strip().lower() in ("all", "所有人", "全体成员")


def _needs_the_host(message: dict, *, host: str, direct: bool) -> bool:
    """Is this message a request the host has to look at?"""
    if _sender_is_app(message):
        return False
    if _sender_of(message) == host:
        return False
    for mention in _mentions_of(message):
        if _is_all_mention(mention) or _mention_open_id(mention) == host:
            return True
    return direct
