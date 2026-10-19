"""The TUI's `/rewind` keeps the message's words and repaints the screen — issue #2384.

The defect these pin: a pick was reduced to a record index, so the message's text was
thrown away with the picker, and the answer to `rewind_result` cleared the chat area and
printed a count — erasing, on screen, the conversation the daemon still held. A picker is
also forced to draw an 80-character *summary* (`preview`), which is not the message.

What is testable here is the decision, not the keystroke: the branches that send
`rewind_session` and paint its answer need a connection and a terminal. So the file pins
the four pure pieces those branches are built on — the text that goes back in the input
box, the entry the picker hands over, the request that redraws the transcript, and the
line the redraw leaves behind — plus the shared painter both replay paths use. Same
split, and the same reason, as `test_app_replay_rows.py` and `test_app_resume_cwd.py`.

No daemon is started, stopped or restarted by this file: every symbol here is pure or
draws into an in-memory widget.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="TUI widget rendering depends on POSIX terminal behaviour (raw mode/SIGWINCH)",
)

from emrg.client.app import (
    _paint_replay_rows,
    _replay_rows,
    _rewind_fill_text,
    _rewind_note,
    _rewind_replay_request,
)
from emrg.client.widgets import ChatHistory, RewindSelector


APP_PY = Path(__file__).resolve().parent.parent / "emrg" / "client" / "app.py"
MULTILINE = "first line\n\nsecond line at some length: " + "x" * 300


def _history_entry(content: str = MULTILINE, preview: str | None = None) -> dict:
    """One entry shaped like the daemon's `list_history` answer.

    `content` is the message; `preview` is the 80-character summary the picker draws
    (`emrg/server/daemon.py`). Both are sent, and the difference between them is the
    whole point of the first test below.
    """
    return {
        "record_index": 12,
        "role": "user",
        "content": content,
        "preview": preview if preview is not None else content[:80] + "…",
        "timestamp": "2026-10-19T16:31:41",
    }


def test_the_fill_takes_the_whole_message_not_the_display_summary():
    """The input box gets `content`, character for character — never `preview`.

    This is the arm that matters: filling from the summary stays green on every other
    check in this file (the request is still made, the transcript is still repainted)
    while handing the host a prompt truncated at 80 characters. Multiline is preserved
    because a rewind is "say the same thing again", not "say its first line again".
    """
    entry = _history_entry()

    assert _rewind_fill_text(entry) == MULTILINE
    assert _rewind_fill_text(entry) != entry["preview"]
    assert _rewind_fill_text(entry).count("\n") == MULTILINE.count("\n")


def test_the_fill_is_empty_for_an_entry_that_carries_no_message():
    """A missing body fills nothing — the word "None" in the input box is not a fill."""
    assert _rewind_fill_text(None) == ""
    assert _rewind_fill_text({}) == ""
    assert _rewind_fill_text({"preview": "only a summary here"}) == ""


def test_the_picker_hands_over_the_entry_under_the_cursor():
    """The widget must surrender the whole entry, not only its index (#2384 item 1).

    The confirmation branch drops the widget, so an API that answers only with a record
    index loses the text at exactly the moment it is needed.
    """
    older = {"record_index": 4, "content": "older message", "preview": "older message"}
    newer = _history_entry()
    sel = RewindSelector([older, newer])

    assert sel.selected_message is older
    assert sel.selected_record_index == 4

    sel.move_down()

    assert sel.selected_message is newer
    assert sel.selected_message["content"] == MULTILINE
    assert sel.selected_record_index == 12


def test_the_picker_hands_over_nothing_when_it_holds_nothing():
    """An empty picker answers None on both, rather than an IndexError or a phantom row."""
    sel = RewindSelector([])

    assert sel.selected_message is None
    assert sel.selected_record_index is None


def test_the_redraw_asks_for_the_daemons_records():
    """The rewind path redraws from `list_history` in records mode (#2384 item 2).

    Blanking the chat area and printing a count is what this replaces, so the request is
    the thing to pin: `/resume` repaints by the same mode, and a rewound session must be
    painted by that one path rather than by a second, hand-rolled guess.
    """
    request = _rewind_replay_request("s1", "/p/emrg")

    assert request["session_id"] == "s1"
    assert request["cwd"] == "/p/emrg"
    assert request["include_records"] is True


def test_the_note_counts_records_and_names_where_the_cut_landed():
    """The line says records, not messages, and names the rewind point (#2384 item 3).

    One turn with tool calls is several records, so the old wording over-reported the
    cut many times over. The wording also says what is above it, because the screen is
    no longer blank.
    """
    line = _rewind_note(12, 3, 40)

    assert "message #12" in line
    assert "3 record(s) removed" in line
    assert "40 kept" in line
    assert "messages removed" not in line


def test_the_shared_painter_draws_both_replay_paths():
    """`/resume` and `/rewind` paint through one function, and it paints.

    Two loops over `_replay_rows` would be two answers to "how does a record become a
    row", drifting while both stay green. This drives the painter with the rows the
    mapping produces, which is the shape both callers hand it.
    """
    chat = ChatHistory()
    rows = _replay_rows([
        {"record_index": 0, "kind": "message", "role": "user", "content": "hello"},
        {"record_index": 1, "kind": "message", "role": "assistant", "content": "there"},
    ])

    _paint_replay_rows(chat, rows)

    assert len(chat.rows) == 2
    assert chat.rows[0].text == "hello"
    # An assistant row is fed through the streaming widget, which keeps its buffer.
    assert chat.rows[1]._buffer == "there"


def test_the_rewind_branch_asks_for_the_repaint_it_promises():
    """The `rewind_result` branch sends the records request — the branch, not the helper.

    The four tests above pin the pieces; none of them reaches the branch that uses them,
    because it runs inside the frame loop and needs a connection and a terminal. So
    deleting the send left every test in this file green while `chat.rows.clear()` ran
    and nothing was painted back — the blank transcript of issue #2384 item 2, restored
    in full. A surviving arm is a missing row, and this is the row (the technique is
    `tests/test_app_cancel_session.py::_block_after`: read the branch out of the source,
    and assert the *call*, not a helper it might have stopped calling).

    The control is `chat.rows.clear()`: arm D keeps it, so it says the extraction found
    the branch rather than collapsing — while the assertion above it is what goes red.
    """
    block = _block_after('if data.get("type") == "rewind_result":',
                         '# Compact result')

    assert '"list_history", **_rewind_replay_request(session_id, cwd)' in block, (
        "a rewound session must be repainted from the daemon's records, so the branch "
        "has to ask for them"
    )
    assert "_rewind_replay = info" in block, (
        "the answer must be recognisable as a rewind's, or it is painted as a picker fill"
    )
    # The request is sent after the clear — a branch that clears and never asks is
    # exactly the blank screen this issue is about.
    assert block.index('"list_history"') > block.index("chat.rows.clear()"), (
        "the repaint is what follows the blanking, not what precedes it"
    )
    assert block.index("_rewind_replay = info") < block.index('"list_history"'), (
        "the in-flight marker must be set before the question is asked"
    )
    assert "chat.rows.clear()" in block, "the extraction is looking at the right branch"


def _block_after(marker: str, stop: str) -> str:
    """The text between `marker` and the next `stop`, or "" when not found.

    Copied from `tests/test_app_cancel_session.py`, where the same problem — a branch
    that only runs inside the frame loop — was solved the same way: assert the call in
    the branch's own text, with a control that fails if the extraction collapsed.
    """
    src = APP_PY.read_text(encoding="utf-8")
    assert marker in src, f"marker not found in app.py: {marker!r}"
    return src.split(marker, 1)[1].split(stop, 1)[0]
