"""The `when` field: its validation, its two conditions, and the loop that obeys it.

Three subjects, and each has its own section below.

* the field itself — what is accepted, and what is refused with which message;
* the conditions — `rant` reads a store, `feishu` reads a CLI, and both answer
  *holds* / *absent* / *cannot tell*, where the third is never the second;
* the wait — a poll may shorten a task's own cadence but must never restart it, and a
  poll that finds nothing must not run the task (issue #2592).

The `feishu` half is driven through an injected runner rather than the real `lark-cli`:
a test that authenticated against the host's own account would read his messages, and
one that did not would prove nothing. The argv it is handed is asserted, so the shape
the real CLI receives is still pinned.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import yaml

from emrg.protocol import InstanceIdentity
from emrg.server import wake as wake_mod
from emrg.server.scheduler import MIN_INTERVAL, TaskScheduler, validate_task_record
from emrg.server.wake import (
    MIN_WHEN_SECONDS,
    shortest_when_seconds,
    validate_when,
    wakes,
)
from tests.task_handler_factory import make_handler

HOST = "ou_host"


# ── harness ──────────────────────────────────────────────────────────────


class _Done:
    """What `subprocess.run` answers, for a runner that never runs anything."""

    def __init__(self, payload=None, returncode: int = 0, stderr: str = "", raw=None):
        self.returncode = returncode
        self.stdout = raw if raw is not None else json.dumps(payload or {})
        self.stderr = stderr


def _feishu_runner(*, chats, messages, identity=None, fail=()):
    """A `lark-cli` stand-in: route each argv to a payload, and record what it saw.

    `messages` maps a chat id to the list `+chat-messages-list` should answer. `fail`
    holds the argv fragments whose call should exit non-zero, so an unreadable answer is
    a scenario rather than an accident.
    """
    seen: list[list[str]] = []

    def runner(argv):
        seen.append(list(argv))
        joined = " ".join(argv)
        for fragment in fail:
            if fragment in joined:
                return _Done(returncode=1, stderr=f"refused: {fragment}")
        if "auth" in argv:
            return _Done(identity if identity is not None else {
                "identities": {"user": {"openId": HOST}},
            })
        if "+chat-list" in joined:
            return _Done({"data": {"chats": chats}})
        if "+chat-messages-list" in joined:
            chat_id = argv[argv.index("--chat-id") + 1]
            return _Done({"data": {"messages": messages.get(chat_id, [])}})
        return _Done(returncode=1, stderr=f"unexpected call: {joined}")

    runner.seen = seen
    return runner


def _message(message_id, *, sender="ou_stranger", sender_type="user", mentions=(), replies=()):
    """A message in the shape `+chat-messages-list` answers.

    The sender defaults to a stranger: the host's own messages are excluded by design, so
    a host-sender default would make every case below read as noise for the wrong reason.

    `replies` are the messages hanging under this one in a thread. The listing answers them
    nested, so they are attached rather than listed beside their parent.
    """
    message = {
        "message_id": message_id,
        "sender": {"id": sender, "sender_type": sender_type},
        "mentions": list(mentions),
    }
    if replies:
        message["thread_replies"] = list(replies)
    return message


def _mention(open_id=None, *, name=None, key=None):
    entry: dict = {}
    if open_id is not None:
        entry["id"] = {"open_id": open_id}
    if name is not None:
        entry["name"] = name
    if key is not None:
        entry["key"] = key
    return entry


def _record(**when) -> dict:
    return {"name": "t", "type": "evolution", "interval": 14400, "when": dict(when)}


# ── the field ────────────────────────────────────────────────────────────


def test_a_record_without_when_is_accepted_unchanged():
    """The field is optional: no `when` means no refusal and no behaviour change."""
    assert validate_when(None) is None
    assert validate_task_record({"name": "t", "type": "evolution"}) is None
    assert shortest_when_seconds({"name": "t"}) is None
    assert shortest_when_seconds({"name": "t", "when": None}) is None


def test_a_usable_when_is_accepted_on_the_one_validation_path():
    """The loader and the CRUD path share `validate_task_record`, so one test covers both."""
    assert validate_when({"rant": 600}) is None
    assert validate_when({"rant": 600, "feishu": 300}) is None
    assert validate_when({"rant": MIN_WHEN_SECONDS}) is None
    assert validate_task_record(_record(rant=600)) is None


def test_every_bad_when_value_is_refused_with_its_own_reason():
    """Four classes of bad input, each refused — never silently ignored.

    The messages are asserted, not just "some error": a refusal a host cannot act on is
    the defect `validate_schedule`'s docstring names, and a value that is dropped
    without a word turns a mis-set field into a task that never wakes early.
    """
    cases = [
        ({"nope": 300}, "unknown when condition 'nope'"),
        ({"rant": 0}, "has 0 seconds"),
        ({"rant": -60}, "has -60 seconds"),
        ({"rant": MIN_WHEN_SECONDS - 1}, f"must be an integer >= {MIN_WHEN_SECONDS}"),
    ]
    for value, expected in cases:
        error = validate_when(value)
        assert error is not None, value
        assert expected in error, f"{value!r} answered {error!r}"

    # And the record-level path reports it under the task's own name.
    error = validate_task_record(_record(nope=300))
    assert error is not None and "unknown when condition 'nope'" in error, error


def test_a_when_that_is_not_a_mapping_or_is_empty_is_refused():
    """`when: 'rant'` and `when: {}` are both refused, each with what it expects."""
    not_a_mapping = validate_when("rant")
    assert not_a_mapping is not None and "not a mapping" in not_a_mapping

    empty = validate_when({})
    assert empty is not None and "empty when mapping" in empty

    boolean = validate_when({"rant": True})
    assert boolean is not None and "has True seconds" in boolean, (
        "a bool is an int in Python; True must not pass as one second"
    )


def test_the_when_floor_is_the_interval_floor():
    """One rule, two carriers: the two floors must not drift apart in silence.

    `MIN_INTERVAL` bounds `interval` and `MIN_WHEN_SECONDS` bounds a `when` value, and
    they are the same bound for the same reason — a condition may not poll faster than a
    task is allowed to run. Two literals in two modules is exactly the shape that goes
    stale, so the agreement is asserted rather than assumed.
    """
    assert MIN_WHEN_SECONDS == MIN_INTERVAL


# ── the conditions ───────────────────────────────────────────────────────


def test_the_rant_condition_reads_the_store_for_this_tasks_agent(tmp_path, monkeypatch):
    """`rant` holds while the task's own agent carries a rant that is not completed."""
    log = tmp_path / "rants.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(row) for row in [
                {"timestamp": "2026-10-26T10:00:00+08:00", "agent": "other",
                 "status": "pending", "progress": "", "completed": None, "message": "x"},
            ]
        ) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(wake_mod, "default_rants_log", lambda: log)

    state_dir = tmp_path / "wake-state"
    held, notes = wakes(_record(rant=600), task_name="t", agent="t", state_dir=state_dir)
    assert held is False, notes
    assert any("rant: absent" in note for note in notes), notes

    # The same store, now carrying this agent's rant: it holds.
    log.write_text(
        log.read_text(encoding="utf-8")
        + json.dumps({"timestamp": "2026-10-26T11:00:00+08:00", "agent": "t",
                      "status": "in_progress", "progress": "", "completed": None,
                      "message": "y"}) + "\n",
        encoding="utf-8",
    )
    # The condition's own clock has not come round yet — that is the next test's subject,
    # so this one starts the watermark over rather than waiting ten minutes for it.
    (state_dir / "t.json").unlink()
    held, notes = wakes(_record(rant=600), task_name="t", agent="t", state_dir=state_dir)
    assert held is True, notes
    assert any("rant: holds" in note for note in notes), notes


def test_a_condition_that_holds_is_not_marked_read_by_a_wake(tmp_path, monkeypatch):
    """A wake does not consume the condition: it still holds at the next consultation.

    Requirement 4 of the issue, at the level the condition can see it. A condition that
    marked itself read on a wake would swallow the work the wake was for — the task runs
    once for a rant and the rant is then invisible to it for ever.
    """
    log = tmp_path / "rants.jsonl"
    log.write_text(json.dumps({"timestamp": "2026-10-26T11:00:00+08:00", "agent": "t",
                               "status": "pending", "progress": "", "completed": None,
                               "message": "y"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(wake_mod, "default_rants_log", lambda: log)
    state_dir = tmp_path / "wake-state"

    start = datetime(2026, 10, 26, 12, 0, 0)
    first, _ = wakes(_record(rant=60), task_name="t", agent="t",
                     state_dir=state_dir, now=start)
    # An hour later the condition's clock has come round again, and the rant is still open.
    second, notes = wakes(_record(rant=60), task_name="t", agent="t",
                          state_dir=state_dir, now=start + timedelta(hours=1))
    assert first is True
    assert second is True, notes


def test_each_condition_keeps_its_own_clock(tmp_path, monkeypatch):
    """`rant: 600` and `feishu: 300` are consulted on their own cadences.

    Requirement 2's "各自按自己的秒数计时互不影响": the rant store is asked once per ten
    minutes even though the wait is sized for the five-minute condition.
    """
    log = tmp_path / "rants.jsonl"
    log.write_text(json.dumps({"timestamp": "2026-10-26T11:00:00+08:00", "agent": "t",
                               "status": "pending", "progress": "", "completed": None,
                               "message": "y"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(wake_mod, "default_rants_log", lambda: log)
    # A readable answer, so this condition is stamped and its clock can be compared with
    # `rant`'s: an unreadable one is deliberately retried and never stamped (next test).
    runner = _feishu_runner(chats=[{"chat_id": "oc_g", "chat_mode": "group"}],
                            messages={"oc_g": []})
    state_dir = tmp_path / "wake-state"
    record = _record(rant=600, feishu=300)

    start = datetime(2026, 10, 26, 12, 0, 0)
    wakes(record, task_name="t", agent="t", state_dir=state_dir, runner=runner, now=start)
    first_calls = len(runner.seen)
    assert first_calls > 0, "the first consultation must ask both conditions"

    # Four minutes later: neither condition's clock has come round, so nothing is asked.
    wakes(record, task_name="t", agent="t", state_dir=state_dir, runner=runner,
          now=start + timedelta(minutes=4))
    assert len(runner.seen) == first_calls, "no condition is due four minutes in"

    # Six minutes in, only `feishu` (300 s) is due — `rant` (600 s) is not.
    wakes(record, task_name="t", agent="t", state_dir=state_dir, runner=runner,
          now=start + timedelta(minutes=6))
    assert len(runner.seen) > first_calls, "the 300-second condition must be asked again"

    state = json.loads((state_dir / "t.json").read_text(encoding="utf-8"))
    assert set(state["checked"]) == {"rant", "feishu"}, state["checked"]


def test_a_condition_that_cannot_be_read_is_not_a_quiet_one(tmp_path, monkeypatch):
    """`UNKNOWN` never wakes the task, and never counts as `ABSENT` either.

    An unauthenticated CLI says nothing about whether a message is waiting, so the task
    stays asleep — but the reason is logged and the condition's stamp is *not* advanced,
    so the next poll asks again instead of waiting out a whole interval on a failed read.
    """
    monkeypatch.setattr(wake_mod, "default_rants_log", lambda: tmp_path / "missing.jsonl")
    runner = _feishu_runner(chats=[], messages={}, identity={"identities": {}})
    state_dir = tmp_path / "wake-state"
    start = datetime(2026, 10, 26, 12, 0, 0)

    held, notes = wakes(_record(feishu=300), task_name="t", agent="t",
                        state_dir=state_dir, runner=runner, now=start)
    assert held is False, notes
    assert any("feishu: unknown" in note for note in notes), notes

    state = json.loads((state_dir / "t.json").read_text(encoding="utf-8"))
    assert "feishu" not in state["checked"], (
        "an unreadable condition must be retried on the next poll, not stamped as read"
    )


def test_the_feishu_condition_answers_the_three_cases_that_need_the_host(tmp_path):
    """A real @, an @all, and a direct message all hold; none of them is a mention of the host."""
    cases = {
        "a real mention of the host": ([_message("m1", mentions=[_mention(HOST)])], []),
        "an @all": ([_message("m2", mentions=[_mention(key="@_all")])], []),
        "a direct message": ([], [_message("m3", sender="ou_stranger")]),
    }
    for label, (group_messages, direct_messages) in cases.items():
        state_dir = tmp_path / label.replace(" ", "-")
        runner = _feishu_runner(
            chats=[{"chat_id": "oc_group", "chat_mode": "group"},
                   {"chat_id": "oc_direct", "chat_mode": "p2p"}],
            messages={"oc_group": group_messages, "oc_direct": direct_messages},
        )
        held, notes = wakes(_record(feishu=300), task_name="t", agent="t",
                            state_dir=state_dir, runner=runner)
        assert held is True, f"{label} should wake the task: {notes}"


def test_the_feishu_condition_reads_a_thread_reply_that_mentions_the_host(tmp_path):
    """An @ inside a thread holds the condition, exactly as a top-level one does.

    `+chat-messages-list` answers a thread as its parent carrying the replies, so the @
    arrives under `thread_replies` and in no other list. A condition that walks the top
    level alone answers `absent` — the same answer a quiet inbox gives, which is why the
    miss is silent and the task then sleeps for its whole interval.
    """
    runner = _feishu_runner(
        chats=[{"chat_id": "oc_group", "chat_mode": "group"}],
        messages={"oc_group": [
            _message("om_parent", replies=[_message("om_reply", mentions=[_mention(HOST)])]),
        ]},
    )
    held, notes = wakes(_record(feishu=300), task_name="t", agent="t",
                        state_dir=tmp_path / "wake-state", runner=runner)
    assert held is True, notes


def test_the_feishu_condition_does_not_wake_on_a_thread_that_asks_nothing(tmp_path):
    """The other direction: a reply that mentions nobody, and one the task itself sent.

    A wake that fires on every threaded conversation costs a round every interval, which
    is worse than the miss it buys — so a reply passes the same sender test the top level
    does, and the answer stays `absent`.
    """
    runner = _feishu_runner(
        chats=[{"chat_id": "oc_group", "chat_mode": "group"}],
        messages={"oc_group": [
            _message("om_parent", sender="ou_stranger", replies=[
                _message("om_reply_noise", sender="ou_other"),
                _message("om_reply_own", sender="ou_bot", sender_type="app"),
            ]),
        ]},
    )
    held, notes = wakes(_record(feishu=300), task_name="t", agent="t",
                        state_dir=tmp_path / "wake-state", runner=runner)
    assert held is False, notes
    assert any("feishu: absent" in note for note in notes), notes


def test_a_thread_reply_is_de_duplicated_by_its_own_id(tmp_path):
    """A reply read twice in one lookback window is one message, not a second wake.

    The reply's id is not its parent's: a de-duplication that keyed a thread by its parent
    would re-wake the task for as long as the parent stayed in the window.
    """
    runner = _feishu_runner(
        chats=[{"chat_id": "oc_group", "chat_mode": "group"}],
        messages={"oc_group": [
            _message("om_parent", replies=[_message("om_reply", mentions=[_mention(HOST)])]),
        ]},
    )
    state_dir = tmp_path / "wake-state"
    start = datetime(2026, 10, 26, 12, 0, 0)
    first, _ = wakes(_record(feishu=300), task_name="t", agent="t",
                     state_dir=state_dir, runner=runner, now=start)
    second, notes = wakes(_record(feishu=300), task_name="t", agent="t",
                          state_dir=state_dir, runner=runner,
                          now=start + timedelta(minutes=6))
    assert first is True
    assert second is False, f"the same reply must not wake the task twice: {notes}"


def test_the_feishu_condition_ignores_noise_and_the_tasks_own_messages(tmp_path):
    """A group message that asks for nothing, and the bot's own reply, both leave it asleep.

    The bot writes into the host's own conversation, so a direct chat with no sender
    filter would read every answer this very task sent as a fresh request — the task
    waking itself in a loop. `sender_type: app` is what excludes it.
    """
    runner = _feishu_runner(
        chats=[{"chat_id": "oc_group", "chat_mode": "group"},
               {"chat_id": "oc_direct", "chat_mode": "p2p"}],
        messages={
            "oc_group": [_message("m1", sender="ou_stranger")],
            "oc_direct": [
                _message("m2", sender="ou_bot", sender_type="app"),
                _message("m3", sender=HOST),
            ],
        },
    )
    held, notes = wakes(_record(feishu=300), task_name="t", agent="t",
                        state_dir=tmp_path / "wake-state", runner=runner)
    assert held is False, notes
    assert any("feishu: absent" in note for note in notes), notes


def test_the_feishu_condition_de_duplicates_by_message_id(tmp_path):
    """One message wakes the task once, however many polls re-read it.

    Feishu reports message times to the minute, so consecutive polls overlap on purpose
    and a time can never be the cursor. The id is.
    """
    message = _message("om_same", mentions=[_mention(HOST)])
    runner = _feishu_runner(
        chats=[{"chat_id": "oc_group", "chat_mode": "group"}],
        messages={"oc_group": [message]},
    )
    state_dir = tmp_path / "wake-state"
    start = datetime(2026, 10, 26, 12, 0, 0)

    first, _ = wakes(_record(feishu=300), task_name="t", agent="t",
                     state_dir=state_dir, runner=runner, now=start)
    second, notes = wakes(_record(feishu=300), task_name="t", agent="t",
                          state_dir=state_dir, runner=runner,
                          now=start + timedelta(minutes=6))
    assert first is True
    assert second is False, f"the same message id must not wake the task twice: {notes}"

    # A genuinely new message still does.
    runner = _feishu_runner(
        chats=[{"chat_id": "oc_group", "chat_mode": "group"}],
        messages={"oc_group": [message, _message("om_new", mentions=[_mention(HOST)])]},
    )
    third, notes = wakes(_record(feishu=300), task_name="t", agent="t",
                         state_dir=state_dir, runner=runner,
                         now=start + timedelta(minutes=12))
    assert third is True, notes


def test_the_feishu_condition_asks_the_cli_the_documented_shape(tmp_path):
    """The argv is pinned: a condition that called a different endpoint would be silent.

    The listing must name `user` (a bot cannot see a direct conversation) and must ask
    for p2p as well as groups — without `--types`, groups are all that comes back.
    """
    runner = _feishu_runner(chats=[{"chat_id": "oc_g", "chat_mode": "group"}],
                            messages={"oc_g": []})
    wakes(_record(feishu=300), task_name="t", agent="t",
          state_dir=tmp_path / "wake-state", runner=runner)

    calls = [" ".join(argv) for argv in runner.seen]
    listing = next(call for call in calls if "+chat-list" in call)
    assert "--types=p2p,group" in listing, listing
    assert "--as user" in listing, listing
    reading = next(call for call in calls if "+chat-messages-list" in call)
    assert "--as user" in reading, reading
    assert "--start" in reading, reading


def test_the_stamp_sent_to_the_cli_names_its_zone(tmp_path):
    """A boundary without an offset leaves its own meaning to the reader.

    `lark-cli --start` answers this repository's Feishu task in two places: the round,
    which the template hands `scheduler.py`'s `datetime.now(timezone.utc).isoformat()`,
    and this poll. A bare local instant here would be the second carrier disagreeing
    with the first about what a time is, and the CLI's own help offers the offset form
    (`2026-01-01T15:04:05+08:00`), so the unaware spelling is not even the documented
    one. Measured: the CLI accepts a naive stamp silently, which is exactly why nothing
    downstream would notice.
    """
    runner = _feishu_runner(chats=[{"chat_id": "oc_g", "chat_mode": "group"}],
                            messages={"oc_g": []})
    local_clock = datetime(2026, 10, 26, 12, 0, 0)  # naive, as a caller may pass it
    wakes(_record(feishu=300), task_name="t", agent="t",
          state_dir=tmp_path / "wake-state", runner=runner, now=local_clock)

    reading = next(" ".join(argv) for argv in runner.seen if "+chat-messages-list" in " ".join(argv))
    stamp = reading.split("--start ", 1)[1].split(" ", 1)[0]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None, (
        f"the stamp sent to the CLI is {stamp!r}, which names no zone — the round is told "
        "an aware UTC instant for the same query"
    )
    # …and it is the same instant the caller named, less this module's own lookback —
    # read from the module rather than restated, so the window has one producer.
    assert parsed == (local_clock - timedelta(seconds=wake_mod.FEISHU_LOOKBACK_SECONDS)).astimezone(), (
        stamp, local_clock
    )


def test_a_watermark_written_in_another_zone_does_not_end_the_wait(tmp_path):
    """The watermark outlives the code that wrote it, so a mixed pair must not raise.

    `aware - naive` raises `TypeError`, and the subtraction that compares a stamp with
    the clock sits outside the `try` that guards a condition. So before the instants
    were normalised in one place, a state file carrying the older naive stamp met a
    zoned clock and raised out of `wakes()` — and the task's own `run()` catches only
    `asyncio.TimeoutError`, so that error ended the task's loop rather than one wait.
    Both spellings are one instant here, and the condition is still consulted.
    """
    state_dir = tmp_path / "wake-state"
    state_dir.mkdir()
    (state_dir / "t.json").write_text(
        # Naive, because that is what the code before this fix wrote: the watermark
        # outlives its writer, so the pairing that has to survive is old-file/now-clock.
        json.dumps({"checked": {"feishu": "2026-10-26T10:00:00"}}), encoding="utf-8"
    )
    runner = _feishu_runner(chats=[{"chat_id": "oc_g", "chat_mode": "group"}],
                            messages={"oc_g": []})

    # 12:00 local is 04:00 UTC, two hours past the stamp — so the condition is due.
    held, notes = wakes(_record(feishu=300), task_name="t", agent="t",
                        state_dir=state_dir, runner=runner,
                        now=datetime(2026, 10, 26, 12, 0, 0))
    assert held is False, notes
    assert runner.seen, "the condition was skipped instead of consulted — its clock never elapsed"

    written = json.loads((state_dir / "t.json").read_text(encoding="utf-8"))
    assert datetime.fromisoformat(written["checked"]["feishu"]).tzinfo is not None, written


# ── the wait ─────────────────────────────────────────────────────────────


def test_the_shortest_when_sizes_the_wait():
    """The scheduler waits for the tightest condition and consults each on its own clock."""
    assert shortest_when_seconds(_record(rant=600, feishu=300)) == 300
    assert shortest_when_seconds(_record(rant=600)) == 600
    assert shortest_when_seconds({"interval": 60}) is None


def test_a_poll_that_finds_nothing_does_not_run_the_task_and_does_not_move_the_run(
    tmp_path, monkeypatch,
):
    """A poll shortens a wait; it must not restart the cadence behind it.

    Two defects are held down here, and both were reachable in the obvious shape:

    * a poll that ran the task on timeout would turn a long interval into the poll's
      own cadence — the opposite of what the field is for;
    * a poll that recomputed the natural wait from *now* at every iteration would push
      the task's own run out for ever, so the task would never run on its own initiative
      again. `next_run_at` is read at each poll and must not move.
    """
    from emrg.server import scheduler as mod

    # Every runtime file this handler writes — logs, the next-run slot, the saturation
    # flag, the wake watermark — derives from `config_dir`, so redirecting that one name
    # keeps the loop under a root the test owns.
    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    handler = make_handler(name="t", config={}, interval=14400)
    handler._task["when"] = {"rant": 60}

    async def _ok():
        return mod.TaskHandler.RECORD_OK

    handler._refresh_record = _ok
    handler._natural_wait = lambda: 10.0
    handler._wake_poll_seconds = lambda: 0.02

    runs: list[float] = []
    reported: list[float | None] = []
    polls: list[int] = []

    async def _cycle():
        runs.append(1.0)
        handler._running = False  # the run is the end of this test's story

    handler._run_cycle_bounded = _cycle

    async def _condition_wake():
        polls.append(1)
        reported.append(handler._next_run_at)
        return (len(polls) >= 3, [f"rant: {'holds' if len(polls) >= 3 else 'absent'}"])

    handler._condition_wake = _condition_wake

    asyncio.run(handler.run())

    assert len(polls) >= 3, f"the poll never came round: {polls}"
    assert len(runs) == 1, "exactly one cycle: the two quiet polls must not have run it"
    first_reported = reported[0]
    assert first_reported is not None
    assert all(value == first_reported for value in reported), (
        "the reported next run moved between polls — the cadence was restarted, "
        f"so the task's own run would be pushed out for ever: {reported}"
    )
    assert first_reported > __import__("time").time(), (
        "the reported next run must be in the future (the task's own cadence), "
        "not the poll's instant"
    )


def test_a_task_without_when_never_polls(tmp_path):
    """No `when`, no poll: the loop's wait and its run are exactly what they were."""
    handler = make_handler(name="t", config={}, interval=14400)
    assert handler._wake_poll_seconds() is None


# ── the field survives a client edit ─────────────────────────────────────


def _scheduler_fixture(tmp_path, monkeypatch) -> TaskScheduler:
    """A scheduler with its own table, and one registered agent to validate against."""
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    (tmp_path / "agents.yml").write_text(
        yaml.safe_dump([{"name": "emrg", "path": str(tmp_path / "emrg")}])
    )
    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tmp_path / "tasks.yml"
    sched._tasks_file.write_text(yaml.safe_dump([{
        "name": "emrg-task", "type": "evolution", "config": {"agent": "emrg"},
        "interval": 14400, "enabled": True, "when": {"rant": 600},
    }]))
    return sched


def test_editing_another_field_keeps_when(tmp_path, monkeypatch):
    """Requirement 9: a client that does not know the field must not drop it.

    `task_update` writes only the keys a payload carries, so a form editing `interval`
    leaves `when` where it was. This is the guarantee the GUI relies on, and it is why
    no GUI change is needed for the field to survive an edit there.
    """
    sched = _scheduler_fixture(tmp_path, monkeypatch)

    ok, result = sched.task_update("emrg-task", interval=900)
    assert ok, result

    written = yaml.safe_load(sched._tasks_file.read_text(encoding="utf-8"))
    record = next(row for row in written if row["name"] == "emrg-task")
    assert record["interval"] == 900
    assert record["when"] == {"rant": 600}, record


def test_when_can_be_set_and_removed_through_the_update_path(tmp_path, monkeypatch):
    """A client that *does* send the field is obeyed, and an explicit null removes it."""
    sched = _scheduler_fixture(tmp_path, monkeypatch)

    ok, result = sched.task_update("emrg-task", when={"feishu": 300})
    assert ok, result
    written = yaml.safe_load(sched._tasks_file.read_text(encoding="utf-8"))
    assert written[0]["when"] == {"feishu": 300}

    ok, result = sched.task_update("emrg-task", when=None)
    assert ok, result
    written = yaml.safe_load(sched._tasks_file.read_text(encoding="utf-8"))
    assert "when" not in written[0], written[0]


def test_the_update_path_refuses_a_bad_when(tmp_path, monkeypatch):
    """The CRUD path refuses what the loader refuses, with the same message."""
    sched = _scheduler_fixture(tmp_path, monkeypatch)

    ok, error = sched.task_update("emrg-task", when={"rant": 1})
    assert ok is False and "must be an integer >= " in error, error

    ok, error = sched.task_update("emrg-task", when={"nope": 300})
    assert ok is False and "unknown when condition 'nope'" in error, error
