"""A run of aborts by one cause is counted, not merely repeated.

Rant 2026-09-28T15:57:31, requirement L3 (「同一原因连续 abort 要能一眼看出」).
The measurement that made it a requirement: 53 aborted cycles over two days, one
cause, and no line anywhere said "this is the 53rd" — each abort was reported on
its own, so a run looked exactly like one unlucky turn.

These tests cover the counter itself; `tests/test_daemon.py` covers the daemon
using it (an abort increments, a round the filter let through resets).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import emrg.server.abort_runs as mod
from emrg.server.abort_runs import RUN_TTL_DAYS, AbortRuns, default_path


def test_the_first_abort_opens_a_run(tmp_path):
    runs = AbortRuns(tmp_path / "abort-runs.json")
    record = runs.note("content_filter", "s_1")

    assert record["count"] == 1
    assert record["first_at"] == record["last_at"]
    assert record["cause"] == "content_filter"
    assert record["session_id"] == "s_1"


def test_consecutive_aborts_extend_one_run_and_keep_its_start(tmp_path):
    """The three facts the reading is made of: how many, since when, last when."""
    runs = AbortRuns(tmp_path / "abort-runs.json")
    first = runs.note("content_filter", "s_1", now=datetime(2026, 9, 27, 10, 0))
    second = runs.note("content_filter", "s_1", now=datetime(2026, 9, 28, 10, 0))

    assert second["count"] == 2
    assert second["first_at"] == first["first_at"], "the run's start is the first abort"
    assert second["last_at"] != first["last_at"]
    assert runs.run("content_filter", "s_1")["count"] == 2


def test_the_clock_note_was_given_governs_the_prune(tmp_path):
    """`now` is the clock the operation is about, not merely the stamp's source.

    The prune measures against the same instant, so a back-dated abort is not
    dropped as stale by the wall clock. Without that the injected clock is only
    half-honoured, and a test whose fixed stamp is older than ``RUN_TTL_DAYS``
    fails on the day the calendar catches up rather than on a change — measured
    2026-10-04: the sibling test's ``2026-09-27`` stamp left the 7-day window at
    10:00 local and turned
    ``test_consecutive_aborts_extend_one_run_and_keep_its_start`` red on master.

    This pins the mechanism **without a fixed date**, so it cannot expire: the
    run is older than the TTL in wall-clock terms the whole time, and it is only
    the injected clock that keeps it alive.
    """
    runs = AbortRuns(tmp_path / "abort-runs.json")
    old = datetime.now().astimezone() - timedelta(days=RUN_TTL_DAYS + 5)
    runs.note("content_filter", "s_1", now=old)
    second = runs.note("content_filter", "s_1", now=old + timedelta(days=1))

    assert second["count"] == 2, (
        "the prune must read the injected clock, so this stale run was not dropped"
    )


def test_a_round_the_cause_did_not_block_ends_the_run(tmp_path):
    """The count answers "how many in a row", so one success makes a new run."""
    runs = AbortRuns(tmp_path / "abort-runs.json")
    runs.note("content_filter", "s_1")
    runs.note("content_filter", "s_1")
    runs.clear("content_filter", "s_1")

    assert runs.run("content_filter", "s_1") is None
    assert runs.note("content_filter", "s_1")["count"] == 1


def test_runs_are_counted_per_session_not_together(tmp_path):
    """53 aborts belong to the task that produced them.

    A single global counter reports one task's collapse as every task's, which is
    the wrong answer to "is one thing broken or many" — the question the host was
    answering by hand.
    """
    runs = AbortRuns(tmp_path / "abort-runs.json")
    runs.note("content_filter", "s_1")
    runs.note("content_filter", "s_1")
    runs.note("content_filter", "s_2")

    assert runs.run("content_filter", "s_1")["count"] == 2
    assert runs.run("content_filter", "s_2")["count"] == 1

    runs.clear("content_filter", "s_1")
    assert runs.run("content_filter", "s_2")["count"] == 1, (
        "clearing one session's run must not clear another's"
    )


def test_two_causes_do_not_share_a_run(tmp_path):
    runs = AbortRuns(tmp_path / "abort-runs.json")
    runs.note("content_filter", "s_1")
    runs.note("other_cause", "s_1")

    assert runs.run("content_filter", "s_1")["count"] == 1
    assert runs.run("other_cause", "s_1")["count"] == 1


def test_the_run_survives_a_restart(tmp_path):
    """State is a file, not a process variable: restarting must not reset it.

    The run that prompted the requirement spanned days; a counter that only lived
    in the daemon's memory would have made the host start counting again.
    """
    path = tmp_path / "abort-runs.json"
    AbortRuns(path).note("content_filter", "s_1")
    AbortRuns(path).note("content_filter", "s_1")

    assert AbortRuns(path).run("content_filter", "s_1")["count"] == 2


def test_the_state_file_says_what_it_counts(tmp_path):
    """Inspectable by design: the host can open the file rather than grep a log."""
    path = tmp_path / "abort-runs.json"
    runs = AbortRuns(path)
    runs.note("content_filter", "s_1", now=datetime(2026, 9, 28, 10, 0))

    written = json.loads(path.read_text(encoding="utf-8"))
    entry = written["content_filter:s_1"]
    assert entry["count"] == 1
    assert entry["cause"] == "content_filter"
    assert entry["session_id"] == "s_1"
    assert entry["first_at"].startswith("2026-09-28T10:00")


def test_an_unreadable_state_file_counts_from_scratch_and_never_raises(tmp_path):
    """A turn that lost its answer must not also lose the explanation of why."""
    path = tmp_path / "abort-runs.json"
    path.write_text("{not json", encoding="utf-8")

    runs = AbortRuns(path)
    assert runs.snapshot() == {}
    assert runs.note("content_filter", "s_1")["count"] == 1


def test_a_state_file_that_is_not_an_object_counts_from_scratch(tmp_path):
    path = tmp_path / "abort-runs.json"
    path.write_text('["a list"]', encoding="utf-8")

    assert AbortRuns(path).snapshot() == {}
    assert AbortRuns(path).note("content_filter", "s_1")["count"] == 1


def test_an_unwritable_state_file_does_not_raise(tmp_path, monkeypatch):
    """Best-effort by construction: counting is worth less than the turn."""
    def refuse(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(mod, "atomic_write_bytes", refuse)
    runs = AbortRuns(tmp_path / "abort-runs.json")

    assert runs.note("content_filter", "s_1")["count"] == 1


def test_no_file_is_written_until_something_aborts(tmp_path):
    """A daemon that never aborts leaves nothing behind — including in tests."""
    path = tmp_path / "abort-runs.json"
    runs = AbortRuns(path)

    assert runs.snapshot() == {}
    runs.clear("content_filter", "s_1")
    assert not path.exists(), "clearing a run that was never open wrote a file"


def test_a_run_that_has_not_aborted_for_a_week_is_dropped(tmp_path):
    """Otherwise a session that aborted once and was never used again lingers."""
    path = tmp_path / "abort-runs.json"
    runs = AbortRuns(path)
    nice = datetime.now().astimezone() - timedelta(days=RUN_TTL_DAYS + 1)
    runs.note("content_filter", "old", now=nice)
    runs.note("content_filter", "new")

    snapshot = runs.snapshot()
    assert "content_filter:old" not in snapshot
    assert "content_filter:new" in snapshot


def test_a_hand_edited_stamp_is_dropped_rather_than_crashing(tmp_path):
    """The file is meant to be readable, which means it can be edited."""
    path = tmp_path / "abort-runs.json"
    path.write_text(json.dumps({
        "content_filter:s_1": {"count": 9, "last_at": "not a timestamp"},
    }), encoding="utf-8")

    runs = AbortRuns(path)
    runs.note("content_filter", "s_2")  # forces the write that prunes

    assert runs.snapshot().keys() == {"content_filter:s_2"}


def test_the_default_path_is_under_the_daemons_log_directory(tmp_path, monkeypatch):
    """Beside the task-run records the host already reads.

    Read through **this file's own by-value import**, not through `mod.default_path`:
    the autouse redirect in `tests/conftest.py` replaces the *module attribute*, so
    in every test of the suite `mod.default_path` is the redirect's lambda. An
    assertion on it measures the redirect, never the resolution.

    This test did worse than that — it installed a lambda of its own and asserted
    its own arithmetic back out — and it was hollow for it. Measured 2026-10-04 with
    `scripts/run-mutation-arm.py`, moving the state off the daemon's log directory
    (`config_dir() / "state" / STATE_FILENAME`) left it **SURVIVED**: the line the
    test exists to pin was unguarded, and the file could be relocated without one
    red row.

    The by-value name is the real function, and it resolves `config_dir` at call
    time, so patching that input is what points it at this test's tree. The sibling
    below reads the other way round **on purpose** — it asserts the redirect through
    the module attribute — which is the same pair
    `tests/test_config.py::test_config_path` (by value) and
    `test_the_default_config_path_is_redirected_into_the_scratch_tree` (through the
    module) already records for the config path.
    """
    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path / "home")

    # Spelled as literals rather than as `mod.STATE_FILENAME` under a directory the
    # module also names: the assertion is where the state lives, so a rename of
    # either half has to reach it.
    assert default_path() == tmp_path / "home" / "logs" / "abort-runs.json"


def test_the_test_suite_is_not_pointed_at_the_hosts_state(tmp_path):
    """The autouse redirect in `tests/conftest.py` — asserted, not assumed.

    A test that drives an abort is exactly the test that would write the host's
    `~/.emrg/logs/abort-runs.json`, and the guard is autouse precisely because
    such a test cannot be relied on to remember it.

    `tmp_path` IS the assertion, not a proxy for it. An earlier version asked
    the neighbouring question — "is the redirect outside the home directory" —
    and that is false on a Windows runner, where pytest's temp base lives under
    `%USERPROFILE%\\AppData\\Local\\Temp`: a red row about the platform rather
    than about the guard (measured on CI run 36538564479, `test-windows`). The
    property the fixture actually establishes is that this test's state file is
    in this test's own temp directory, so that is what is checked.
    """
    # Read through the module, not a name imported at collection time: the
    # fixture patches the module attribute, so an early-bound name would keep
    # pointing at the real function and quietly assert nothing — which is the
    # reading the sibling above wants, and the only one that can see the
    # resolution rather than the redirect.
    assert mod.default_path() == tmp_path / "abort-runs.json"
