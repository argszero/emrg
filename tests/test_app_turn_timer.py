"""The elapsed timer belongs to the **session's** turn, not to this client's send.

Rant 2026-09-27T18:41:52 (host, session state single source of truth): the
daemon is the only state source and a client is only a renderer, so a turn
started by a peer client — or by a scheduled task's cycle — must tick on every
client that has the session open. Measured before this change: the timer's
lifetime was `busy`, set only when *this* client sent the task, and the
`turn_start` alignment was gated on the same flag, so of the subscribers the
daemon broadcast `turn_start` to, exactly one rendered a clock.

What is testable without a terminal, and why that is enough: the frame loop in
`app.py` needs a TTY (raw mode, SIGWINCH), so no test can open a session and
watch the status line. What it *can* pin is every decision that loop makes —
which instant a `turn_start` frame yields, which instant a `resume_result`
snapshot yields, and (by source, since the point is an absence) that the timer's
lifetime is the session's turn. The untestable remainder is the wiring, which is
why the wiring is one assignment and the decisions are not.

Each source assertion is paired with a control that must also hold, so a blind
or mis-split scan cannot pass by finding nothing.

No daemon is started, stopped or restarted by this file: both helpers are pure
and nothing here opens a socket.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="TUI widget rendering depends on POSIX terminal behaviour (raw mode/SIGWINCH)",
)

from emrg.client.app import resume_turn_instant, turn_start_instant

APP_PY = Path(__file__).resolve().parents[1] / "emrg" / "client" / "app.py"


def _function_source(name: str) -> str:
    """The source of the nested function `name`, located by its own definition.

    Cut at the next definition in the enclosing body (four spaces), so the scan
    follows the code rather than a line number that drifts with every edit.
    """
    src = APP_PY.read_text(encoding="utf-8")
    start = src.index(f"    async def {name}(")
    rest = src[start:]
    tail = re.search(r"\n    (?:async )?def \w+\(", rest)
    assert tail, f"the scan could not find the end of {name}"
    return rest[: tail.start()]


# ── the daemon's `turn_start` frame ─────────────────────────


def test_a_turn_start_frame_yields_the_instant_it_names():
    assert turn_start_instant({"type": "turn_start", "started_at": 1759000000.25}) == 1759000000.25


def test_an_integer_instant_is_widened_to_a_float():
    # JSON gives an integer when the epoch happens to be whole; the timer
    # subtracts it from a float clock, so the type is normalised here.
    got = turn_start_instant({"started_at": 1759000000})
    assert isinstance(got, float) and got == 1759000000.0


@pytest.mark.parametrize(
    "frame",
    [
        {},                                                # an older daemon
        {"started_at": None},                              # the field exists, empty
        {"started_at": 0},                                 # epoch: no instant
        {"started_at": -1},                                # nonsense
        {"started_at": "1759000000"},                      # a string is not an instant
        {"started_at": True},                              # bool is not an instant
    ],
)
def test_a_frame_with_no_usable_instant_yields_nothing(frame):
    """`None`, never zero: a stamp that says nothing must leave the reader's
    clock alone rather than park the timer at the epoch."""
    assert turn_start_instant(frame) is None


# ── the daemon's `resume_result` snapshot ───────────────────


def test_a_running_turn_in_the_snapshot_yields_its_instant():
    meta = {"turn": {"running": True, "started_at": 1759000000.5}}
    assert resume_turn_instant(meta) == 1759000000.5


@pytest.mark.parametrize(
    "meta",
    [
        {},                                                            # no snapshot
        {"turn": None},                                                # explicit absence
        {"turn": {"running": False, "started_at": 1759000000.0}},      # idle session
        {"turn": {"running": True, "started_at": None}},               # running, no instant
        {"turn": {"running": True}},                                   # running, field absent
        {"turn": "running"},                                           # shape drift
    ],
)
def test_a_snapshot_that_names_no_running_instant_yields_nothing(meta):
    assert resume_turn_instant(meta) is None


# ── the wiring the decisions feed (source, because the point is an absence) ──


def test_the_elapsed_timer_lives_as_long_as_the_sessions_turn():
    """`while busy` was the defect: it stopped the clock for every turn this
    client did not start, which is every turn a peer or a scheduled task runs."""
    body = _function_source("_run_elapsed_timer")

    assert "while turn_running:" in body, "the timer no longer runs on the session's turn"
    assert "while busy" not in body, "a timer still ends with this client's send"
    assert "status.elapsed = timer" in body, "the scan is looking at the wrong file"


def test_the_turn_start_alignment_no_longer_asks_who_started_the_turn():
    """The frame is honoured whoever started the turn, so the `busy and` gate
    that made a peer's turn invisible must not come back."""
    body = _function_source("read_server")

    assert "turn_start_instant(data)" in body, "the frame's instant is no longer read"
    assert "if busy and isinstance(started" not in body, "the own-turn gate is back"
    assert "turn_running = True" in body, "the scan is looking at the wrong file"


def test_opening_a_session_mid_turn_starts_the_clock_at_the_snapshots_instant():
    """Requirement 2 at the open path: `resume_result`'s snapshot is read the same
    way a `turn_start` frame is, so a session opened mid-turn shows the time it has
    already run instead of starting from zero."""
    body = _function_source("read_server")
    branch = body[body.index('if data.get("type") == "resume_result":') :]

    assert "resume_turn_instant(meta)" in branch, "the snapshot is no longer read"
    assert branch.index("resume_turn_instant(meta)") < branch.index("chat.rows.clear()"), (
        "the snapshot is read after the history replay — the timer would start late"
    )
