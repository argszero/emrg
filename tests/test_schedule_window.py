"""Scheduling windows on a task record — issue #2486.

A task carries one frequency; `schedule` adds windows that answer with their own
frequency while they are open. Acceptance is asserted, not demonstrated:

  * both edges of a window answer with their own frequency (17:59 → 15 min,
    18:00 → 3 h);
  * a wait is cut to the next edge on its own — a weekend band entered at 06:59
    wakes at 09:00, not at 09:59 and not three hours later;
  * a record without `schedule` waits exactly as long as it does today, measured
    through the same loop;
  * four rejected inputs, each with a readable error;
  * the day rule on its own, and the cut wired into `run()` rather than only into
    the helper that computes it — so deleting either rule reddens a test.

The edge answer, the day rule and the cut are separate rules on purpose: a mutation
of any one of them has to redden something here.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest
import yaml

from emrg.protocol import InstanceIdentity
from emrg.server import scheduler as mod
from emrg.server.scheduler import (
    DEFAULT_INTERVAL,
    MIN_INTERVAL,
    TaskHandler,
    TaskScheduler,
    effective_interval,
    seconds_to_next_boundary,
    validate_schedule,
    validate_task_record,
)

# A day whose weekday the test itself states, so no assertion here rests on a calendar
# the reader has to take on trust.
MONDAY = datetime(2026, 10, 19)
SATURDAY = MONDAY + timedelta(days=5)


def _record(**overrides) -> dict:
    record = {
        "name": "windowed",
        "type": "evolution",
        "config": {"project": "emrg"},
        "interval": 10800,
        "enabled": True,
        "schedule": [
            {"days": "mon-fri", "from": "09:00", "to": "18:00", "interval": 900},
        ],
    }
    record.update(overrides)
    return record


# ── the day rule (deleting it reddens these) ─────────────────────────────────


def test_the_calendar_of_this_test_is_stated_not_assumed():
    assert MONDAY.weekday() == 0, "MONDAY is a Monday"
    assert SATURDAY.weekday() == 5, "SATURDAY is a Saturday"


def test_days_parse_case_whitespace_and_wrap_around_the_ring():
    assert mod._parse_window_days("MON") == {0}
    assert mod._parse_window_days("  mon , fri  ") == {0, 4}
    assert mod._parse_window_days("mon-fri") == {0, 1, 2, 3, 4}
    # A range wraps: fri-mon is Fri, Sat, Sun, Mon — not the empty set, and not fri..sun.
    assert mod._parse_window_days("fri-mon") == {4, 5, 6, 0}
    assert mod._parse_window_days("sat-sun") == {5, 6}
    assert mod._parse_window_days(None) == set(range(7)), "absent = every day"
    assert mod._parse_window_days("funday") is None


def test_a_window_applies_only_on_its_own_weekdays():
    """The day rule, read through the answer it selects — a weekday and a weekend."""
    record = _record()
    inside_a_weekday = effective_interval(record, MONDAY.replace(hour=10, minute=0))
    inside_a_saturday = effective_interval(record, SATURDAY.replace(hour=10, minute=0))
    assert inside_a_weekday == 900
    assert inside_a_saturday == 10800, "mon-fri says nothing about Saturday"


def test_a_wrapping_range_does_open_on_the_weekend():
    record = _record(schedule=[
        {"days": "fri-mon", "from": "09:00", "to": "18:00", "interval": 900},
    ])
    assert effective_interval(record, SATURDAY.replace(hour=10, minute=0)) == 900


# ── both edges answer with their own frequency ───────────────────────────────


@pytest.mark.parametrize("hour, minute, expected", [
    (8, 59, 10800),   # one minute before the window opens
    (9, 0, 900),      # the opening edge selects the window's frequency
    (17, 59, 900),    # one minute before the window closes
    (18, 0, 10800),   # the closing edge gives the frequency back
])
def test_both_window_edges_answer_with_their_own_frequency(hour, minute, expected):
    record = _record()
    assert effective_interval(record, MONDAY.replace(hour=hour, minute=minute)) == expected


def test_between_two_windows_the_top_level_frequency_is_answered():
    record = _record(schedule=[
        {"days": "mon", "from": "09:00", "to": "12:00", "interval": 900},
        {"days": "mon", "from": "14:00", "to": "18:00", "interval": 600},
    ])
    assert effective_interval(record, MONDAY.replace(hour=11, minute=0)) == 900
    assert effective_interval(record, MONDAY.replace(hour=13, minute=0)) == 10800
    assert effective_interval(record, MONDAY.replace(hour=15, minute=0)) == 600


# ── the cut is a rule of its own ─────────────────────────────────────────────


def test_a_weekend_band_wakes_at_the_window_edge_not_after_the_whole_interval():
    """06:59 on Saturday, 3-hour weekend frequency, a band opening at 09:00.

    The wait must be 121 minutes: at 09:59 it would be 180 minutes (121 to the edge
    plus a full interval inside it) and at 12:00 a whole 3 hours, sleeping through the
    window it was about to enter.
    """
    record = _record(schedule=[
        {"days": "sat-sun", "from": "09:00", "to": "18:00", "interval": 900},
    ])
    at_0659 = SATURDAY.replace(hour=6, minute=59)
    assert effective_interval(record, at_0659) == 10800, "outside the band"
    assert seconds_to_next_boundary(record, at_0659) == 7260.0, "121 minutes to 09:00"
    assert 7260.0 < 10800, "the plain interval would have slept through the band"


def test_the_next_boundary_is_always_in_the_future():
    record = _record()
    for hour in (8, 9, 17, 18, 23):
        at = MONDAY.replace(hour=hour, minute=0)
        boundary = seconds_to_next_boundary(record, at)
        assert boundary is not None and boundary > 0, (
            f"at {hour}:00 the boundary must be ahead, never zero seconds away"
        )


def test_waking_at_the_edge_recomputes_instead_of_finding_the_same_edge():
    record = _record()
    at_edge = MONDAY.replace(hour=9, minute=0)
    assert seconds_to_next_boundary(record, at_edge) == 9 * 3600, "to 18:00"
    assert effective_interval(record, at_edge) == 900, "and the edge changed the answer"


def test_no_schedule_means_no_boundary_and_todays_frequency():
    plain = _record()
    del plain["schedule"]
    for hour in (0, 9, 12, 17, 23):
        at = MONDAY.replace(hour=hour, minute=30)
        assert effective_interval(plain, at) == 10800
    assert seconds_to_next_boundary(plain, MONDAY) is None


def test_a_default_record_without_a_schedule_answers_the_documented_default():
    assert effective_interval({}) == DEFAULT_INTERVAL


# ── four rejected inputs, each with a readable error ─────────────────────────


def test_an_unparsable_days_value_is_refused_by_name():
    error = validate_schedule([
        {"days": "mon-funday", "from": "09:00", "to": "18:00", "interval": 900},
    ])
    assert error and "`mon`" in error and "mon-funday" in error


def test_a_window_that_does_not_move_forward_is_refused():
    error = validate_schedule([
        {"days": "mon", "from": "18:00", "to": "09:00", "interval": 900},
    ])
    assert error and "does not move forward" in error and "two windows" in error


def test_a_window_interval_below_the_minimum_is_refused():
    error = validate_schedule([
        {"days": "mon", "from": "09:00", "to": "18:00", "interval": MIN_INTERVAL - 1},
    ])
    assert error and str(MIN_INTERVAL) in error


def test_two_overlapping_windows_of_one_day_are_refused():
    error = validate_schedule([
        {"days": "mon-fri", "from": "09:00", "to": "12:00", "interval": 900},
        {"days": "mon", "from": "11:00", "to": "14:00", "interval": 600},
    ])
    assert error and "overlap" in error and "mon" in error, error


def test_windows_on_disjoint_days_do_not_overlap():
    """The overlap rule reads the day intersection, not the clock alone."""
    assert validate_schedule([
        {"days": "mon-fri", "from": "09:00", "to": "12:00", "interval": 900},
        {"days": "sat-sun", "from": "10:00", "to": "14:00", "interval": 600},
    ]) is None


def test_touching_windows_are_not_an_overlap():
    assert validate_schedule([
        {"days": "mon", "from": "09:00", "to": "12:00", "interval": 900},
        {"days": "mon", "from": "12:00", "to": "14:00", "interval": 600},
    ]) is None


@pytest.mark.parametrize("schedule, needle", [
    ("mon-fri", "not a list"),
    ([], "empty schedule list"),
    (["09:00-18:00"], "not a mapping"),
    ([{"days": "mon", "from": 540, "to": "18:00", "interval": 900}], "quoted string"),
    ([{"days": "mon", "from": "09:00", "to": "18:00"}], "must be an integer"),
])
def test_every_rejected_shape_names_the_value_and_the_expectation(schedule, needle):
    error = validate_schedule(schedule)
    assert error and needle in error, error


def test_an_absent_schedule_is_not_an_error():
    assert validate_schedule(None) is None


def test_the_loader_refuses_a_bad_window_before_the_task_starts():
    """`validate_task_record` is the reader the loader and the CRUD path share."""
    record = _record(schedule=[
        {"days": "mon", "from": "18:00", "to": "09:00", "interval": 900},
    ])
    error = validate_task_record(record)
    assert error and "does not move forward" in error, error
    assert validate_task_record(_record()) is None


# ── the record is the one source of truth for the field ──────────────────────


@pytest.fixture
def sched(tmp_path, monkeypatch):
    """A TaskScheduler over a tasks.yml in `tmp_path`, with config_dir redirected."""
    original = mod.config_dir
    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    # A registered project, so `task_create` has something to be valid against.
    (tmp_path / "agents.yml").write_text(
        yaml.safe_dump([{"name": "emrg", "path": str(tmp_path)}])
    )
    try:
        scheduler = TaskScheduler(InstanceIdentity())
        scheduler._tasks_file = tmp_path / "tasks.yml"
        yield scheduler
    finally:
        mod.config_dir = original


WINDOW = [{"days": "mon-fri", "from": "09:00", "to": "18:00", "interval": 900}]


def _row(tasks_yml, name: str) -> dict:
    """One record out of a written tasks.yml by name (the file is seeded, so not [0])."""
    return next(r for r in yaml.safe_load(tasks_yml.read_text()) if r.get("name") == name)


def test_create_and_update_carry_the_windows_through_the_file(sched):
    ok, created = sched.task_create(
        name="windowed", task_type="evolution", project="emrg",
        interval=10800, schedule=WINDOW,
    )
    assert ok, created
    written = _row(sched._tasks_file, "windowed")
    assert written["schedule"] == WINDOW

    ok, updated = sched.task_update("windowed", interval=7200)
    assert ok, updated
    # A payload that omits the field leaves the windows alone — which is what lets a
    # client that predates the field keep editing the task (issue #2486 ⑩).
    assert updated["schedule"] == WINDOW

    ok, cleared = sched.task_update("windowed", schedule=None)
    assert ok, cleared
    assert "schedule" not in _row(sched._tasks_file, "windowed")


def test_a_bad_window_is_refused_on_both_write_paths(sched):
    bad = [{"days": "mon", "from": "18:00", "to": "09:00", "interval": 900}]
    ok, error = sched.task_create(
        name="bad", task_type="evolution", project="emrg", interval=10800, schedule=bad,
    )
    assert not ok and "does not move forward" in error, error

    assert sched.task_create(
        name="good", task_type="evolution", project="emrg", interval=10800,
    )[0]
    ok, error = sched.task_update("good", schedule=bad)
    assert not ok and "does not move forward" in error, error


# ── the reported rows carry the frequency in force ───────────────────────────


def test_list_tasks_reports_the_effective_interval_and_the_next_edge(sched):
    """A record with no handler reports the same cadence keys a live one does."""
    all_day = [{"days": None, "from": "00:00", "to": "24:00", "interval": 900}]
    sched._tasks_file.write_text(yaml.safe_dump([
        {"name": "always", "type": "evolution", "config": {"project": "emrg"},
         "interval": 10800, "enabled": True, "schedule": all_day},
    ]))
    row = sched.list_tasks()[0]
    assert row["interval"] == 10800, "the declared frequency is still reported"
    assert row["effective_interval"] == 900, "and the one in force at this instant"
    assert row["schedule"] == all_day
    boundary = row["next_window_boundary_in_seconds"]
    assert isinstance(boundary, int) and 0 < boundary <= 86400


def test_a_live_handler_reports_the_same_three_keys(monkeypatch):
    # The clock is frozen inside the band: `status()` reads `now()` itself, and a test
    # that compared it with a second `now()` would describe the instant the suite ran at.
    monkeypatch.setattr(mod, "datetime", _frozen_at(datetime(2026, 10, 19, 10, 0)))
    handler = _handler_with(_record())
    status = handler.status()
    assert status["interval"] == 10800
    assert status["schedule"] == _record()["schedule"]
    assert status["effective_interval"] == 900, "the window answers, not the record"
    assert status["next_window_boundary_in_seconds"] is not None


def test_the_heartbeat_and_the_backoff_take_the_frequency_in_force(monkeypatch):
    """⑧: neither layer re-derives the rule, and neither bypasses the other."""
    monkeypatch.setattr(mod, "datetime", _frozen_at(datetime(2026, 10, 19, 10, 0)))
    handler = _handler_with(_record())
    in_force = handler._effective_interval()
    assert in_force == 900, "inside the band the frequency in force is the window's"
    assert handler._heartbeat_interval() == max(in_force, min(in_force * 8, 8 * 3600))
    handler._connect_failures = 0
    assert handler._connect_backoff() == float(in_force)


# ── the cut is wired into the loop, not only into the helper ────────────────


def _handler_with(record: dict) -> TaskHandler:
    handler = TaskHandler(name=record["name"], identity=InstanceIdentity())
    handler._apply_record(record)
    return handler


async def _first_wait(handler: TaskHandler) -> float:
    """Run the loop until it arms its first wait, then stop it and return that wait."""
    task = asyncio.ensure_future(handler.run())
    try:
        for _ in range(300):
            if handler._next_run_at is not None:
                break
            await asyncio.sleep(0.01)
        if handler._next_run_at is None:
            return -1.0
        return handler._next_run_at - time.time()
    finally:
        handler._running = False
        handler._trigger_event.set()
        try:
            await asyncio.wait_for(task, 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()


def _frozen_at(instant: datetime):
    """A `datetime` whose `now()` answers `instant`, so a run does not read the clock.

    A test of the wait reads whatever frequency `now()` selects. Left to the real clock
    a window is open or shut depending on the hour the suite happens to run at — which
    is how this file first reddened: measured on CI at 10:54 **UTC** on a Thursday the
    `mon-fri 09:00-18:00` band was open, so a record that still carried it waited its
    window's 900s, while the same suite was green locally at 18:54 +08:00 where the band
    was shut. The instant is stated here instead.
    """
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant
    return _Frozen


def _plain_record(interval: int = 3600) -> dict:
    """A record with **no** `schedule` key at all — absent, not a stored null."""
    record = _record(interval=interval)
    del record["schedule"]
    return record


def _loop_wait(tmp_path, monkeypatch, boundary, record=None, freeze_at=None):
    """The wait one real `run()` arms, over a real tasks.yml, with `boundary` injected."""
    original = mod.config_dir
    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(mod, "seconds_to_next_boundary", lambda record, now=None: boundary)
    if freeze_at is not None:
        monkeypatch.setattr(mod, "datetime", _frozen_at(freeze_at))
    try:
        tasks_yml = tmp_path / "tasks.yml"
        tasks_yml.write_text(yaml.safe_dump([record if record is not None else _plain_record()]))
        handler = TaskHandler(name="windowed", identity=InstanceIdentity())
        handler._tasks_file = tasks_yml
        return asyncio.run(_first_wait(handler))
    finally:
        mod.config_dir = original


def test_run_cuts_the_wait_to_the_next_boundary(tmp_path, monkeypatch):
    """Delete the cut in `run()` and this reads ~3600 instead of ~1: it reddens."""
    wait = _loop_wait(tmp_path, monkeypatch, boundary=1.0)
    assert 0 < wait <= 2, f"the wait must be cut to the 1s boundary, not the interval: {wait}"


def test_run_without_a_boundary_waits_the_whole_interval(tmp_path, monkeypatch):
    """The same loop over a record with **no** `schedule`: today's wait, unchanged."""
    wait = _loop_wait(tmp_path, monkeypatch, boundary=None)
    assert 3500 < wait <= 3600, f"a task with no windows waits its interval: {wait}"


def test_run_waits_at_the_window_the_record_states(tmp_path, monkeypatch):
    """The dual of the test above: the record's window is read, not merely carried.

    Same helper, same `interval: 3600`, same injected `boundary=None` — only the record
    differs (it keeps the `mon-fri 09:00-18:00 / 900` band) and the clock is frozen
    inside that band. The wait must therefore be the window's frequency. Make
    `_effective_interval` answer the declared interval again and this reads ~3600.
    """
    inside = datetime(2026, 10, 19, 10, 0)  # a Monday, inside the band
    wait = _loop_wait(
        tmp_path, monkeypatch, boundary=None,
        record=_record(interval=3600), freeze_at=inside,
    )
    assert 800 < wait <= 900, f"inside the window the wait is the window's interval: {wait}"
