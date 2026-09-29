"""The TUI's replay rows come from the daemon's records, not from a file on disk.

Rant 2026-09-29T15:52:49, requirement 4. The defect this pins: the TUI rendered a
resumed session by reading `history.jsonl` off disk while the GUI asked the daemon,
so the two clients could show different material for one session — and the daemon's
answer is the one built to carry what the live stream showed (record order, tool
pairs never cut apart, an absolute `record_index` on each).

What is testable here is the mapping, not the keystroke: the branch that sends the
request needs a connection and a terminal, so what a test can pin is the function
turning one record list into the other. Same split, and the same reason, as
`test_app_resume_cwd.py` and `test_app_task_session.py`.

No daemon is started, stopped or restarted by this file: `_replay_rows` is pure and
touches no socket.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="TUI widget rendering depends on POSIX terminal behaviour (raw mode/SIGWINCH)",
)

from emrg.client.app import _replay_rows


def test_a_message_record_becomes_its_own_role_row():
    """The roles the records mode projects map through with their content intact.

    The old disk path truncated nothing for a message either, and the reason it matters
    is the same defect that made `preview` unacceptable in the daemon's records mode: a
    replay that shows 80 characters of a 5064-character message is showing different
    material from the live stream while claiming to replay it.
    """
    long_body = "x" * 5064
    rows = _replay_rows([
        {"record_index": 0, "kind": "message", "role": "user", "content": "hello"},
        {"record_index": 1, "kind": "message", "role": "assistant", "content": long_body},
    ])

    assert rows == [("user", "hello"), ("assistant", long_body)]


def test_a_role_the_records_mode_does_not_project_is_skipped():
    """A `system` record is not material this client may invent.

    The daemon's records mode answers `role: "user" | "assistant"` explicitly. The file
    on disk holds more than that (system rows, compaction summaries), so a client that
    rendered them would again be reading a different session than the GUI — the drift
    requirement 4 removes. If the mode is widened, this client learns it there.
    """
    rows = _replay_rows([
        {"record_index": 0, "kind": "message", "role": "system", "content": "Interrupted"},
        {"record_index": 1, "kind": "message", "role": "tool", "content": "legacy role"},
        {"record_index": 2, "kind": "summary", "content": "[Session summary from compact #3]"},
        {"record_index": 3, "kind": "message", "role": "user", "content": "kept"},
    ])

    assert rows == [("user", "kept")]


def test_a_tool_result_names_the_tool_and_whether_it_failed():
    """The two facts the live stream shows about a tool record, and nothing invented.

    `error` is read rather than inferred from the text: a successful command whose output
    contains the word "error" must not be rendered as a failure, and the reverse.
    """
    rows = _replay_rows([
        {"record_index": 3, "kind": "tool_result", "tool_name": "bash",
         "tool_call_id": "c1", "content": "boom in the log", "error": True},
        {"record_index": 4, "kind": "tool_result", "tool_name": "read",
         "tool_call_id": "c2", "content": "file body", "error": False},
    ])

    assert rows[0] == ("tool", "  bash error: boom in the log")
    assert rows[1] == ("tool", "  read result: file body")


def test_a_tool_result_without_a_name_is_still_a_row():
    """A missing `tool_name` must not drop the record — the content is the point."""
    rows = _replay_rows([
        {"record_index": 5, "kind": "tool_result", "content": "body", "error": False},
    ])

    assert rows == [("tool", "  tool result: body")]


def test_an_unknown_kind_is_skipped_rather_than_rendered_blank():
    """The daemon's record set is the contract; an unknown kind is a newer daemon.

    A blank row would be a rendering of nothing, which reads to the host as a message
    they sent that came back empty — the failure mode this mapping exists to avoid.
    """
    rows = _replay_rows([
        {"record_index": 0, "kind": "message", "role": "user", "content": "kept"},
        {"record_index": 1, "kind": "something_new", "content": "not mine"},
    ])

    assert rows == [("user", "kept")]


def test_a_malformed_record_does_not_take_the_replay_down():
    """One bad element must not cost the whole session its history.

    The daemon builds these dicts from a file that a crash can truncate, so "the record
    is not a dict" is a shape that reaches here; a `TypeError` mid-replay would leave the
    transcript cleared and empty, which is worse than the older disk path's behaviour.
    """
    rows = _replay_rows(["not a record", None, 42, {"kind": "message", "role": "user", "content": "ok"}])

    assert rows == [("user", "ok")]


def test_an_empty_or_absent_history_is_no_rows():
    """A session with no records replays to nothing, which the caller then reports."""
    assert _replay_rows([]) == []
    assert _replay_rows(None) == []
