"""The TUI's half of issue #1757 — the frame closes the question, the deadline backs it up.

Rant 2026-09-29T15:52:38.987951+08:00 follow-up (issue #1757), the TUI's half of
the defect the `approval_resolved` frame closes. The daemon refuses a confined
call at `APPROVAL_TIMEOUT_SECONDS` and says so; the frame is the primary path,
and this file is about the client's own backstop for a frame that never arrives
(a dropped connection, an older daemon): `_approval_pending` carries a deadline
stamped when the question arrived.

Both halves are named here because requirement 2 asks for both — the client
clears the question on the frame *and* bounds it independently — and the
clearing half was guarded by nothing: the branch sits inside the frame loop,
which needs a connection and a terminal, and by this repo's rule a requirement no
test names does not exist. Deleting those two lines put the original defect back
in silence, which the arms below measure.

The defect this pins: the expiry arm cleared the input and returned, so a line
the host typed after the daemon had given up was consumed as an answer that
never reached anyone — the host's prompt vanished with the question, which is
the same swallow this requirement exists to remove, one branch over.

What is testable without a terminal: the decision (inside the deadline the line
is the answer, outside it the line is the host typing) is a pure function, and
the *absence* of the swallow is read from the source — no keystroke test can
assert that a branch does not consume a line, and the branch sits behind a
120-second deadline. The source reading carries a control in both directions, so
a blind or mis-split scan cannot pass: the live arm must still consume the line.

No daemon is started, stopped or restarted by this file: nothing here opens a
socket, and the source is read as text.
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
    approval_question_is_still_live,
    approval_resolution_closes,
)

APP_PY = Path(__file__).resolve().parents[1] / "emrg" / "client" / "app.py"

#: Where the pending-question arm starts and the next handler begins. The start
#: marker matches only the keystroke handler's arm: the frame loop's resolution
#: branch asks `approval_resolution_closes(...)` instead of reading the tuple
#: itself, so this exact string occurs once and the scan cannot drift onto it.
ARM_START = "if _approval_pending is not None:"
ARM_END = "# Pending /skills install confirmation"

#: The frame loop's resolution branch, and the handler after it. Bounded by the
#: next `# Sessions list` comment rather than by an indentation inference, for
#: the same reason the keystroke scan is bounded by a comment.
RESOLUTION_START = 'if data.get("type") == "approval_resolved":'
RESOLUTION_END = "# Sessions list"


def _pending_approval_arm() -> str:
    """The source of the keystroke handler's pending-question arm."""
    src = APP_PY.read_text(encoding="utf-8")
    start = src.index(ARM_START)
    end = src.index(ARM_END, start)
    return src[start:end]


def _resolution_arm() -> str:
    """The source of the frame loop's resolution branch."""
    src = APP_PY.read_text(encoding="utf-8")
    start = src.index(RESOLUTION_START)
    end = src.index(RESOLUTION_END, start)
    return src[start:end]


# ── the decision ─────────────────────────────────────────────


def test_a_line_inside_the_deadline_is_the_answer():
    assert approval_question_is_still_live(now=100.0, deadline=220.0) is True


def test_a_line_after_the_deadline_is_the_host_typing():
    assert approval_question_is_still_live(now=220.5, deadline=220.0) is False


def test_the_deadline_instant_itself_is_already_expired():
    """The boundary, in the one direction that is safe to be wrong in.

    The client's stamp is taken when the frame arrives, so its deadline is the
    daemon's plus the frame's flight time: at the client's deadline the daemon
    has certainly stopped waiting. Treating the instant itself as expired keeps
    that ordering (`>=`, as the branch read it before this predicate existed).
    """
    assert approval_question_is_still_live(now=220.0, deadline=220.0) is False


# ── the absence: the expired arm does not consume the line ───


def test_the_arm_still_reads_the_deadline_through_the_predicate():
    """The control that the predicate is wired in, not merely defined."""
    arm = _pending_approval_arm()
    assert "approval_question_is_still_live(" in arm


def test_an_expired_question_leaves_the_line_to_the_prompt_path():
    arm = _pending_approval_arm()
    expired, split, answered = arm.partition("else:")
    assert split, "the arm's two outcomes are no longer split by an `else:`"
    # The split found the right region: the expiry message is in the first half.
    assert "expired unanswered" in expired
    # Nothing in the expired half consumes the line: no send, no clearing of the
    # input, no early return that would keep the ordinary prompt path from
    # running.
    assert "return True" not in expired
    assert 'inp.text = ""' not in expired
    assert "approval_response" not in expired
    # And the control in the other direction — a live question is still answered
    # by consuming the line, so this cannot pass by the arm having gone inert.
    assert "approval_response" in answered
    assert "return True" in answered


# ── the decision: which frame closes which question ──────────


def test_a_resolution_naming_this_question_closes_it():
    assert approval_resolution_closes(("appr-1", "widen?"), "appr-1") is True


def test_a_resolution_naming_another_question_leaves_this_one_alone():
    """Requirement 3's clause, on the client the requirement does not name.

    The daemon keeps questions per request id, so it can be waiting on one for
    another session while this client has one on screen. A client that closed on
    the *arrival* of any resolution would drop the question the host is looking
    at, and the answer they then typed would reach nobody — the swallow this
    issue exists to remove, entered from the other side.
    """
    assert approval_resolution_closes(("appr-1", "widen?"), "appr-2") is False


def test_there_is_nothing_to_close_with_no_pending_question():
    assert approval_resolution_closes(None, "appr-1") is False


# ── the wiring: the frame arm asks the predicate, and clears ──


def test_the_resolution_region_is_the_frame_handlers():
    """The control that the two scans below read the right slice.

    Without it a marker that had moved, or a slice that came back empty, would
    make every assertion about the region pass vacuously. The two endings the
    branch reports are the anchor: they are written inside it and nowhere else in
    the frame loop.
    """
    arm = _resolution_arm()
    assert "timed_out" in arm
    assert "cancelled" in arm


def test_the_frame_arm_reads_the_decision_through_the_predicate():
    """The control that the predicate is wired in, not merely defined."""
    assert "approval_resolution_closes(" in _resolution_arm()


def test_the_frame_arm_actually_clears_the_question():
    """Asking is not closing: the state write must live in the same branch.

    A scan that found only the call would pass with the predicate asked and its
    answer discarded — the question would stay live, which is the defect itself.
    """
    assert "_approval_pending = None" in _resolution_arm()
