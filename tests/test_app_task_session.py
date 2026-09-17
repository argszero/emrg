"""Tests for the TUI `/task-session` command — the picker's intent, and the
"which task can supply a session" predicate (rant 2026-09-17T18:36:08).

What is testable without a terminal, and why that is enough: the keystroke loop
in `app.py` needs a TTY (raw mode, SIGWINCH), so a test cannot drive Enter and
watch the cwd move. What it *can* pin is every decision that branch makes — the
widget it reads its intent from, the row it reads `project_path`/`session_id`
from, and the predicate that decides whether a task has a session at all. The
untestable remainder is the wiring between them, which is why the wiring is
three straight-line statements and the decisions are not.

No daemon is started, stopped or restarted by this file: nothing here touches a
socket, and `_task_session_target` is pure.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="TUI widget rendering depends on POSIX terminal behaviour (raw mode/SIGWINCH)",
)

from emrg.client.app import _task_open_switch, _task_session_target
from emrg.client.python_tui.widgets.base import RenderContext, Span
from emrg.client.widgets import TASK_SELECTOR_TITLES, TaskSelector, _COMMAND_HELP


def make_task(name: str = "t", **extra) -> dict:
    row = {
        "name": name,
        "project": "emrg",
        "project_path": "/Users/x/projects/emrg",
        "session_id": "emrg-evolution-emrg",
        "running": False,
        "interval": 300,
    }
    row.update(extra)
    return row


def title_of(sel: TaskSelector) -> str:
    """The rendered header line's text, as the terminal would show it."""
    lines = sel.render(RenderContext(width=120))
    return "".join(s.text for s in lines[0].spans)


# ── the title names the purpose, and cannot be set independently of it ──


def test_the_default_intent_is_trigger_and_says_so():
    """`/trigger`'s picker keeps its wording — the new command must not move it."""
    assert TaskSelector([make_task()]).intent == "trigger"
    assert "trigger" in title_of(TaskSelector([make_task()]))


def test_a_session_picker_does_not_offer_to_trigger():
    """The other direction, and the point of the rant: the same list serves two
    commands, so a host with both open must be able to tell which one Enter is
    about to run."""
    sel = TaskSelector([make_task()], intent="session")
    title = title_of(sel)
    assert "session" in title
    assert "trigger" not in title


def test_the_title_is_derived_from_the_intent_rather_than_passed_beside_it():
    """The two cannot disagree: `TASK_SELECTOR_TITLES` is keyed by the intent the
    Enter branch reads, so there is no call shape that builds a session picker
    wearing the trigger wording."""
    assert set(TASK_SELECTOR_TITLES) == {"trigger", "session"}
    for intent, title in TASK_SELECTOR_TITLES.items():
        # The header line is the bullet span plus the title span; compare on the
        # title's own text so the assertion is about the mapping, not the glyph.
        assert title_of(TaskSelector([make_task()], intent=intent)).endswith(title)


def test_an_unknown_intent_is_refused_rather_than_defaulted():
    """A typo must not silently produce a picker whose Enter triggers a task the
    host meant to open."""
    with pytest.raises(ValueError):
        TaskSelector([make_task()], intent="open")


# ── the row the Enter branch reads ──


def test_selected_task_is_the_whole_row_and_follows_the_cursor():
    rows = [make_task("a"), make_task("b")]
    sel = TaskSelector(rows)
    assert sel.selected_task is rows[0]
    sel.move_down()
    assert sel.selected_task is rows[1]


def test_selected_task_is_none_when_nothing_is_selected():
    """Empty list, and an index past the end (a list that shrank under the
    cursor) — both must answer None rather than raise or wrap."""
    assert TaskSelector([]).selected_task is None
    sel = TaskSelector([make_task()])
    sel.selected_index = 5
    assert sel.selected_task is None


# ── the predicate that decides whether a session can be opened ──


def test_a_real_row_yields_the_tasks_project_and_session():
    assert _task_session_target(make_task()) == (
        "/Users/x/projects/emrg",
        "emrg-evolution-emrg",
    )


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        # A task that has never run: the daemon's status carries no session.
        make_task(session_id=""),
        make_task(project_path=""),
        make_task(session_id=None),
        make_task(project_path=None),
        # Whitespace is not a path either — a cwd of " " would put the history
        # replay somewhere that does not exist.
        make_task(project_path="   "),
        # Not a dict at all: the row comes off the wire.
        "emrg-evolution-emrg",
    ],
)
def test_a_row_that_cannot_name_a_session_yields_nothing(row):
    """`None` is the answer that keeps the client where it is: the caller reports
    "has no session yet" instead of asking the daemon to resume a session that is
    not there (the ghost-session shape the resume path already guards against)."""
    assert _task_session_target(row) is None


# ── when the confirmed session is the one the open asked for ──


def test_the_cwd_moves_only_for_the_session_the_open_requested():
    pending = ("/Users/x/projects/emrg", "emrg-evolution-emrg")
    assert _task_open_switch(pending, "emrg-evolution-emrg") == "/Users/x/projects/emrg"


def test_no_open_means_no_switch():
    """A plain `/resume <id>` must leave the cwd alone."""
    assert _task_open_switch(None, "s_whatever") is None


def test_a_stale_open_does_not_ride_along_on_an_unrelated_resume():
    """The case a truthiness check gets wrong: an open whose verdict never arrived
    (connection dropped) is still pending when a different session resumes, and
    moving the host into that stale project would put every later message and the
    history replay in the wrong directory."""
    stale = ("/Users/x/projects/other", "emrg-evolution-other")
    assert _task_open_switch(stale, "s_260917_1200_abcd") is None


# ── the command is reachable, and says so in both places a host looks ──


def test_the_command_is_registered_for_autocomplete_and_help():
    """Two thirds of the rant's registration requirement, mechanically: the
    autocomplete table is the authority for `/task-session` being a command at
    all (a partial prefix would otherwise open the dropdown and never submit)."""
    assert "/task-session" in _COMMAND_HELP


def test_the_help_screen_lists_the_command():
    """The third: the static `/help` block in app.py is not derived from
    `_COMMAND_HELP`, so a command added to one and not the other is invisible to
    a host reading the help — the defect this asserts against."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "emrg" / "client" / "app.py").read_text(
        encoding="utf-8"
    )
    listed = [
        line for line in source.splitlines()
        if line.strip().startswith("/task-session")
    ]
    assert listed, "the /help block does not list /task-session"
    help_line = listed[0]
    assert "session" in help_line.lower()
