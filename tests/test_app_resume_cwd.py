"""The client asks the daemon where a session lives, instead of assuming (rant
2026-09-29T15:52:49, requirement 6 — the client half).

The defect this pins: every `resume_session` the TUI sent carried the client's
**own** cwd. That is only right by accident. A session belonging to another
project — a scheduled task's cycle above all — lives under a different
`.emrg/sessions/`, and the daemon's broadcast filter is keyed by `(session, cwd)`,
so the wrong cwd both fails to open it and, where it does open, leaves the live
frames addressed to nobody. The daemon held the fact all along
(`_canonical_session_cwd`, the global sessions index); the client now asks.

What is testable here is the decision, not the keystroke: `handle_key` needs a TTY
and the answer arrives on the daemon's socket, so what a test can pin is the
function that turns one into the other. The wiring between them is three
straight-line statements by design — the same split `test_app_task_session.py`
uses for the picker, and for the same reason.

No daemon is started, stopped or restarted by this file: `_resume_target_from_resolution`
is pure and touches no socket.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="TUI widget rendering depends on POSIX terminal behaviour (raw mode/SIGWINCH)",
)

from emrg.client.app import _resume_target_from_resolution


def test_the_daemons_answer_wins_over_the_clients_own_cwd():
    """The whole point of the change: the real cwd replaces the assumed one.

    Mutation arm: make the client ignore `result["cwd"]` and keep `fallback_cwd`
    (the pre-fix behaviour) — this test goes red, and nothing else does.
    """
    target = _resume_target_from_resolution(
        {"type": "session_cwd_result", "session_id": "s1", "cwd": "/proj/a",
         "source": "index"},
        "s1",
        "/host/cwd",
    )
    assert target == ("s1", "/proj/a"), (
        "the client resumed with its own cwd instead of the one the daemon "
        f"reported — got {target!r}"
    )


def test_an_unknown_answer_keeps_the_cwd_the_client_has():
    """"Unknown" is not "empty", and it is not a third thing either.

    The daemon does not vouch for a session the global index has never seen. The
    client's own cwd is then the only candidate and is the right one for a session
    in the current project — so it is used, but as a fallback rather than being
    written down as if the daemon had said it.
    """
    target = _resume_target_from_resolution(
        {"type": "session_cwd_result", "session_id": "s1", "cwd": None,
         "source": "unknown", "requested_cwd": "/host/cwd"},
        "s1",
        "/host/cwd",
    )
    assert target == ("s1", "/host/cwd")


@pytest.mark.parametrize("resolved", ["", "   ", None])
def test_an_empty_answer_is_the_unknown_case(resolved):
    """A cwd that is present but empty must not be resumed with.

    `""` is falsy and `"   "` is truthy, so a truthiness test alone would send
    whitespace as a path in the second case. Both are the same defect.
    """
    target = _resume_target_from_resolution(
        {"session_id": "s1", "cwd": resolved}, "s1", "/host/cwd"
    )
    assert target == ("s1", "/host/cwd")


def test_an_answer_about_another_session_moves_nobody():
    """A verdict that arrives after its request was abandoned resumes nothing.

    The answer names its session for exactly this check — the same discipline
    `_task_open_switch` applies at the other end of the round trip. Without it, a
    `/resume A` whose answer never came would let a later `/resume B` drag the
    host into A's project.
    """
    assert _resume_target_from_resolution(
        {"session_id": "other", "cwd": "/proj/a"}, "s1", "/host/cwd"
    ) is None


def test_an_answer_nobody_asked_for_is_ignored():
    """No question outstanding ⇒ no resume. An unsolicited frame is not consent."""
    assert _resume_target_from_resolution(
        {"session_id": "s1", "cwd": "/proj/a"}, None, "/host/cwd"
    ) is None
