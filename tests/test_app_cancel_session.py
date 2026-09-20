"""The TUI's half of "a cancel is a session's turn, not a connection's" (rant
2026-09-20T12:50:13, whose server half is PR #1474).

What is testable without a terminal, and why that is enough: the keystroke loop
in `app.py` needs a TTY (raw mode, SIGWINCH), so no test can press Esc and watch
the display. What it *can* pin is every decision that branch makes — the frame a
cancel sends, whether the client narrates before the daemon answers, and which
receipts earn a line. The untestable remainder is the wiring, which is why the
wiring is one call and the decisions are not.

The last test reads the source, because the point of that half is an *absence*:
the optimistic branch (clear busy, stop the timer, print "⏸ Interrupted" at the
keystroke) must be gone, and no runtime test can assert that a keystroke does
nothing. Its instrument is given a control — the same extraction must find the
narration where it now belongs — so a blind or mis-split scan cannot pass.

No daemon is started, stopped or restarted by this file: `request_cancel` is
handed a recording stub, and nothing here opens a socket.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from types import CodeType

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="TUI widget rendering depends on POSIX terminal behaviour (raw mode/SIGWINCH)",
)

from emrg.client.app import (
    CANCELLED_LINE,
    cancelled_line,
    receipt_is_about_a_turn_this_client_shows,
    request_cancel,
    the_ending_completes_a_receipt_this_client_holds,
)

APP_PY = Path(__file__).resolve().parents[1] / "emrg" / "client" / "app.py"


class RecordingConn:
    """A connection stub: `send_command` remembers the frame it was handed."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_command(self, type_: str, **params) -> None:
        self.frames.append({"type": type_, **params})


# ── the request names the session ────────────────────────────


def test_the_cancel_names_the_session_and_nothing_else():
    """The whole frame, not a substring: the defect was its *shape*.

    A bare `{"type": "cancel"}` is what the TUI sent before this — the daemon
    resolves it to the connection's last session, i.e. a client that cannot say
    which session it is stopping gets whichever it touched last.
    """
    conn = RecordingConn()
    asyncio.run(request_cancel(conn, "s_260920_1200_abcd"))

    assert conn.frames == [{"type": "cancel", "session_id": "s_260920_1200_abcd"}]


def test_a_cancel_with_no_session_is_not_sent():
    """The other direction of the same decision: no session to name, no request.

    Sending the bare frame "to be safe" would be the connection-scoped cancel
    again — and the daemon would answer it by guessing, which is precisely what
    this half removes.
    """
    conn = RecordingConn()
    asyncio.run(request_cancel(conn, ""))

    assert conn.frames == []


# ── the receipt is what ends the turn ────────────────────────


def test_the_receipt_for_the_session_on_screen_is_narrated():
    line = cancelled_line({"type": "cancelled", "session_id": "s1"}, "s1")

    assert line == CANCELLED_LINE


def test_a_receipt_for_another_session_is_this_client_s_business():
    """A peer's cancel of a session this client has moved away from.

    The daemon broadcasts to every client watching the cancelled session, so
    this arrives while the client shows something else; printing an interruption
    there would report a stop about a turn that is not on screen.
    """
    assert cancelled_line({"type": "cancelled", "session_id": "s2"}, "s1") is None


def test_a_receipt_without_a_stamp_is_still_a_receipt():
    """An older daemon's broadcast carries no `session_id`; it went to the
    session's subscribers only, so there is nothing to disambiguate."""
    assert cancelled_line({"type": "cancelled"}, "s1") == CANCELLED_LINE


def test_no_other_frame_earns_the_line():
    """The control: the instrument must not fire on everything that passes.

    `done` — the frame that ends a *completed* turn — is the one that matters
    here: narrating it as an interruption is the false statement the receipt
    handling exists to avoid.
    """
    for frame in (
        {"type": "done", "session_id": "s1"},
        {"type": "queued_cancelled", "session_id": "s1"},
        {"type": "turn_start", "session_id": "s1"},
        {"session_id": "s1"},
    ):
        assert cancelled_line(frame, "s1") is None, frame


# ── the frame order decides whether the line can be printed at all ──
#
# Found by running the daemon's own in-process harness rather than by reading the
# client (cycles cyc20260921-010110 and -031413), and there are **two** orders to
# survive, not one. The asker's own Esc produces `done{cancelled: true}` and *then*
# the receipt, because the daemon awaits the turn it owns before broadcasting the
# receipt and the unwinding tool loop broadcasts that `done` on the way out. A
# peer's Esc — the connection does not own the turn, so nothing is awaited — gets
# the receipt *first*, with the `done` after it. Asking only `busy` printed nothing
# in the first order; asking only the flag prints nothing in the second.


def test_a_turn_that_ended_cancelled_still_earns_the_line():
    """The asker's order: `busy` is already false when the receipt arrives."""
    assert receipt_is_about_a_turn_this_client_shows(
        busy=False, turn_ended_cancelled=True
    )


def test_a_receipt_arriving_mid_turn_earns_the_line():
    """The receipt while this client is still busy, whoever asked for the cancel."""
    assert receipt_is_about_a_turn_this_client_shows(
        busy=True, turn_ended_cancelled=False
    )


def test_a_receipt_after_a_completed_turn_is_silent():
    """The control, and the reason the flag is read rather than assumed false.

    A cancel that reached the daemon too late to stop a turn still gets a
    receipt — the task is already done, so the branch does not cancel it but
    broadcasts anyway. `busy` is false and no turn of this client's ended
    cancelled, so "response stopped" would be false under a response that
    finished. Delete the flag (the version this replaces asked `busy` alone) and
    only the tests above go red — this one would pass for the wrong reason, which
    is why it is stated separately.
    """
    assert not receipt_is_about_a_turn_this_client_shows(
        busy=False, turn_ended_cancelled=False
    )


def test_the_ending_completes_a_receipt_this_client_holds():
    """The peer's order, which no single-frame question can answer.

    Neither fact is known when the receipt lands (`busy` false, no ending yet), so
    the receipt is held and the question is asked again at the `done`. Both halves
    are asked here in one place: a held receipt without a cancelled ending is
    silent, and a cancelled ending with no receipt of this client's is silent too.
    """
    assert the_ending_completes_a_receipt_this_client_holds(
        receipt_held=True, turn_ended_cancelled=True
    )
    # the late-cancel control: the receipt was held, the turn had already finished
    assert not the_ending_completes_a_receipt_this_client_holds(
        receipt_held=True, turn_ended_cancelled=False
    )
    # and a cancelled ending this client never asked about (a peer asked, this
    # client was not even subscribed to that session's frames) names nothing
    assert not the_ending_completes_a_receipt_this_client_holds(
        receipt_held=False, turn_ended_cancelled=True
    )


def test_the_receipt_is_held_when_nothing_says_a_turn_stopped_yet():
    """The wiring of the held half, at the branch that decides it.

    Read out of the source for the same reason as the assignments below (the
    branch runs inside a TTY loop), and with the same control: the block must also
    hold the narration call, so a marker that matched nothing cannot pass by
    producing "". The arm this row kills is the branch calling `_narrate_the_stop`
    unconditionally (then nothing is ever held and the peer's order loses the
    line); a source row cannot see a short-circuit neutralizer such as
    `if False and …`, which is a limit of the instrument and is why the *decision*
    it wires is a predicate with its own rows above.
    """
    block = _block_after('if data.get("type") == "cancelled":', "continue")

    assert "if receipt_is_about_a_turn_this_client_shows(" in block, (
        "the receipt must ask the two-fact question before it decides"
    )
    assert "_narrate_the_stop()" in block, "the extraction is looking at the right branch"
    assert "cancel_receipt_held = True" in block, "a receipt that cannot narrate yet is kept"


def test_the_done_frame_is_where_the_flag_comes_from():
    """The wiring the three tests above cannot reach: the flag's only writer.

    The frame loop needs a TTY, so the assignment is read out of the source the
    way the narration tests are. The control is the `busy = False` line beside
    it: if the extraction collapsed, both assertions would fail together.
    """
    block = _block_after("if resp.done:", "if stream_buffer:")

    assert 'turn_ended_cancelled = data.get("cancelled") is True' in block, (
        "the frame that ends the turn is the only place that says why"
    )
    assert "busy = False" in block, "the extraction is looking at the right branch"


def test_the_done_branch_asks_the_held_question():
    """The second order's wiring: the ending is where a held receipt pays off.

    Same slice trick, run to the end of the `done` branch instead of to the
    stream flush, because the call sits after the flush on purpose — the system row
    it adds must be the last row, or `chat.update_last` would overwrite it with the
    tail of the answer. The arm this row kills is deleting the call (then a peer's
    receipt never narrates and its `done` clears the held flag silently); with the
    call present the *decision* is the predicate's own row above.
    """
    block = _block_after("if resp.done:", 'if "error" in data:')

    assert "if the_ending_completes_a_receipt_this_client_holds(" in block
    assert "receipt_held=cancel_receipt_held" in block, (
        "the held receipt must be the one this client kept, not a constant"
    )
    assert block.index("cancel_receipt_held = False") > block.index("_update_left_extra()"), (
        "a held receipt is spent by the ending, wherever the ending turns out to be"
    )


def test_the_turn_boundary_expires_a_held_receipt():
    """A held receipt lives no longer than the turn it is about.

    The `turn_start` frame is the daemon's authoritative turn boundary for every
    turn of a session, so it is where a hold that was never completed by a `done`
    has to expire — otherwise it would still be held when a *later* turn is cut
    off, and a turn ended by a disconnect broadcasts a cancelled `done` with no
    receipt of its own. The control is the alignment line beside it: if the
    extraction collapsed, both assertions would fail together.
    """
    block = _block_after('if data.get("type") == "turn_start":', "continue")

    assert "cancel_receipt_held = False" in block
    assert "_request_start = float(started)" in block, (
        "the extraction is looking at the right branch"
    )


def test_a_new_turn_clears_both_facts():
    """Every turn *start* resets them, so no receipt can borrow an earlier ending.

    Stated as the invariant rather than as a count of assignments: the claim is
    "a site that makes this client busy also forgets the previous ending and any
    receipt held for it", and a count would turn the next legitimate site into a
    red test. Deriving the sites from the file also means the test fails if there
    are none, which a hand-written list would not.
    """
    lines = APP_PY.read_text(encoding="utf-8").splitlines()
    starts = [n for n, line in enumerate(lines) if "busy = True" in line]

    assert starts, "no turn-start site found — the extraction is looking at the wrong file"
    for n in starts:
        window = "\n".join(lines[max(0, n - 3): n])
        assert "turn_ended_cancelled = False" in window, (
            f"the turn started at line {n + 1} does not clear the flag:\n{window}"
        )
        assert "cancel_receipt_held = False" in window, (
            f"the turn started at line {n + 1} does not drop a held receipt:\n{window}"
        )


# ── the source: the optimistic branch is gone, not bypassed ──


def _block_after(marker: str, stop: str) -> str:
    """The text between `marker` and the next `stop`, or "" when not found."""
    src = APP_PY.read_text(encoding="utf-8")
    assert marker in src, f"marker not found in app.py: {marker!r}"
    return src.split(marker, 1)[1].split(stop, 1)[0]


def _nested_code(outer: CodeType, name: str) -> CodeType | None:
    """The code object of the function `name` defined inside `outer`, if any.

    Scope is the question, so the answer has to come from the compiler: code
    objects report which names a function owns as a local, which it takes from an
    enclosing scope, and which of the enclosing function's names are shared cells.
    """
    for const in outer.co_consts:
        if isinstance(const, CodeType) and const.co_name == name:
            return const
    return None


def test_the_esc_branch_asks_and_shows_nothing():
    """Esc must not clear busy, stop the timer, reset the title or print the line.

    Measured as an absence on purpose: the branch runs on a keystroke inside a
    TTY loop, so no runtime assertion can reach it, and this is the only form in
    which "there is no local optimistic branch" can be checked at all.
    """
    block = _block_after('if data == b"\\x1b" and busy:', "return True")

    assert "request_cancel(" in block, "Esc must ask the daemon"
    assert "chat.add(" not in block, "the keystroke must not narrate"
    assert "busy = False" not in block, "the keystroke must not clear busy (only the receipt does)"
    assert "term.set_title(" not in block, "the keystroke must not reset the title"
    assert "CANCELLED_LINE" not in block


def test_the_narration_is_found_where_it_now_belongs():
    """The control for the test above: the same extraction, on the receipt.

    Without this, a marker that matched nothing (or a split that collapsed to an
    empty string) would satisfy every "not in block" above and read as a pass.
    The branch decides the line and calls the one narration site; the narration
    itself is checked in `test_the_line_is_printed_in_one_place` below, which is
    the extraction this one is the control for.
    """
    block = _block_after('if data.get("type") == "cancelled":', "continue")

    assert "cancelled_line(" in block, "the receipt must decide the line"
    assert "_narrate_the_stop()" in block, "the receipt is where the host reads it"


def test_the_line_is_printed_in_one_place():
    """One wording, one site — and the site is reached from both orders.

    The store went into a helper because the second order narrates from the `done`
    branch: a copy of the block there would be a second wording waiting to drift,
    and the two clients of one session are supposed to read the same statement.
    """
    block = _block_after("def _narrate_the_stop() -> None:", "busy = False; server_id")

    assert "CANCELLED_LINE" in block, "the extraction is looking at the right function"
    assert "chat.add(" in block, "the line is what the host reads"
    assert "busy = False" in block, "and the stop is what clears busy"
    assert APP_PY.read_text(encoding="utf-8").count("chat.add(\"system\", CANCELLED_LINE)") == 1, (
        "exactly one site prints the interruption line"
    )


def test_the_receipt_asks_the_two_fact_question():
    """The wiring the predicate's own tests cannot reach, and it needs its own row.

    `receipt_is_about_a_turn_this_client_shows` is exercised above with the flag
    true and false, so those tests pass whether or not the *call site* ever hands
    it the flag: mutating this branch to pass `turn_ended_cancelled=False` — the
    exact defect, asked at the call site instead of in the predicate — left the
    whole file green when it was first written (measured, cycle
    cyc20260921-015450). A surviving arm is a missing row, so this is the row:
    the argument must be this client's own variable, not a constant.
    """
    block = _block_after('if data.get("type") == "cancelled":', "continue")

    assert "receipt_is_about_a_turn_this_client_shows(" in block
    assert "busy=busy, turn_ended_cancelled=turn_ended_cancelled" in block, (
        "the receipt must ask about the turn that ended cancelled, not a constant"
    )


def test_the_two_facts_have_one_owner():
    """The defect that killed the reader task, asked of the interpreter.

    The first version declared these in each function that touched them, so
    `read_server`'s receipt branch read a local that no `done` had assigned on that
    connection yet — `LOAD_FAST_CHECK` raised `UnboundLocalError` out of the frame
    loop, whose only handler catches `json.JSONDecodeError`, and the TUI went deaf
    for the rest of the session (measured, cycle cyc20260921-031413). A source
    search for `nonlocal` would be a spelling test; the compile below asks the
    question the reviewer asked — *which scope owns the name* — and answers it the
    way `co_freevars` does:

    * a name **assigned** anywhere in a function is a local, so it can never also
      appear in that function's `co_freevars` — being a free variable there is
      exactly "nonlocal is declared, the binding is the enclosing one";
    * `interactive` holding it as a **cell** is the other half: a plain local of
      `interactive` would not be shared with the nested functions at all.

    Both halves are asserted per function, so dropping one `nonlocal` line is red
    on that function alone.
    """
    code = compile(APP_PY.read_text(encoding="utf-8"), str(APP_PY), "exec")
    interactive = _nested_code(code, "interactive")
    assert interactive is not None, "the extraction is looking at the wrong file"

    for fact in ("turn_ended_cancelled", "cancel_receipt_held"):
        assert fact in interactive.co_cellvars, (
            f"{fact} is not owned by interactive, so the nested functions cannot share it"
        )
        for fn_name in ("read_server", "handle_key", "_narrate_the_stop"):
            fn = _nested_code(interactive, fn_name)
            assert fn is not None, f"{fn_name} not found under interactive"
            assert fact in fn.co_freevars, (
                f"{fn_name} does not take {fact} from the enclosing scope — it assigns "
                f"a local of its own (the UnboundLocalError defect)"
            )
            assert fact not in fn.co_varnames, f"{fn_name} still declares {fact} locally"


def test_the_bare_connection_scoped_cancel_is_gone():
    """No remnant of the old call shape anywhere in the client.

    `send_command("cancel")` with no session is the frame the daemon answers by
    guessing; leaving one behind would keep the connection-scoped behaviour
    reachable from some other keystroke.
    """
    src = APP_PY.read_text(encoding="utf-8")

    assert not re.search(r'send_command\(\s*"cancel"\s*\)', src), "a bare cancel survives"
    assert "request_cancel(" in src, "the scan is looking at the wrong file"
