"""What the CLI says when a daemon is there and the command still failed.

The defect this exists for: the daemon-rejection path of `emrg server stop` was
caught in the same `except` as "nothing is listening" and printed one line for
all of them — "daemon not running." — so a host with a stale token (or with a
CLI and a daemon from different versions, which is how the v0.2.7-era
"install ≠ running" gap presents) was told a daemon they still had was gone,
while it kept running. `emrg rant` had the mirror-image version of the same
mistake: `AuthError` was caught by no clause at all and escaped as a traceback.

**Neither test runs the stop path.** `_stop_daemon` SIGTERMs a pid; exercising
it would kill the daemon hosting the evolution (MANIFESTO 第四条附则二). That is
now enforced rather than intended — conftest::_guard_live_daemon_signals makes
the SIGTERM raise (issue #1337). What is
asserted is the pure message function, plus the wiring read from source, plus
`_send_rant` driven with `connect_to_server` replaced in-process — so no socket
is opened and no rant can reach `rants.jsonl`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from json import JSONDecodeError
from websockets.exceptions import ConnectionClosed

import pytest

from emrg import __main__ as cli
from emrg.connect import AuthError

NOT_RUNNING = "daemon not running."
AUTH = AuthError("authentication failed — check token / daemon version")


# ── the derived message ────────────────────────────────────────

def test_auth_rejection_is_not_reported_as_an_absent_daemon() -> None:
    """The regression this change is about, in one assertion pair."""
    msg = cli._stop_failure_message(AUTH)
    assert NOT_RUNNING not in msg
    assert "running" in msg and "NOT" in msg


def test_the_auth_message_says_which_repair_to_try() -> None:
    """A truthful message that names no repair is only half the fix."""
    msg = cli._stop_failure_message(AUTH)
    assert "token" in msg and "version" in msg


@pytest.mark.parametrize("exc", [
    ConnectionRefusedError(61, "Connection refused"),
    FileNotFoundError(2, "No such file or directory"),
    asyncio.TimeoutError(),
    JSONDecodeError("Expecting value", "", 0),
])
def test_a_genuinely_absent_or_silent_daemon_still_says_not_running(
        exc: BaseException) -> None:
    """The widening must not swallow the honest case: nothing answered."""
    assert cli._stop_failure_message(exc) == NOT_RUNNING


def test_a_closed_connection_is_still_an_absent_daemon() -> None:
    """`ConnectionClosed` is the other member of that tuple; keep it covered."""
    assert cli._stop_failure_message(ConnectionClosed(None, None)) == NOT_RUNNING


def test_the_two_messages_are_different() -> None:
    """A collapse into one message is the defect; assert the discrimination."""
    assert (cli._stop_failure_message(AUTH)
            != cli._stop_failure_message(ConnectionRefusedError(61, "nope")))


def test_the_stop_path_prints_the_derived_message() -> None:
    """Wiring, not behaviour: a helper nothing calls is a silent no-op.

    Read from source on purpose — calling `_stop_daemon` to observe the print
    would execute the stop path, which the docstring above rules out.
    """
    src = inspect.getsource(cli._stop_daemon)
    assert "_stop_failure_message(" in src
    assert 'print("daemon not running.")' not in src


# ── emrg rant: same mistake, one call site over ────────────────

def _connect_raises(exc: BaseException, calls: list) -> object:
    """A stand-in for `connect_to_server` that fails before any socket exists."""
    async def _stub():
        calls.append(exc)
        raise exc
    return _stub


def test_rant_does_not_call_an_auth_rejection_an_absent_daemon(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    calls: list = []
    monkeypatch.setattr(cli, "connect_to_server", _connect_raises(AUTH, calls))
    # If the patch had not taken, this would really connect — and really submit a
    # rant. Assert the stub is installed before calling anything.
    assert cli.connect_to_server.__name__ == "_stub"

    with pytest.raises(SystemExit) as excinfo:
        cli._send_rant("probe", project="emrg")

    out = capsys.readouterr().out
    assert len(calls) == 1, "the stub never ran: the CLI opened a real connection"
    assert NOT_RUNNING not in out
    assert "not sent" in out and "auth failed" in out and "token" in out
    assert excinfo.value.code == 1


def test_rant_keeps_the_honest_message_when_nothing_answered(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    calls: list = []
    monkeypatch.setattr(
        cli, "connect_to_server",
        _connect_raises(FileNotFoundError(2, "No such file or directory"), calls))

    cli._send_rant("probe", project="emrg")  # no SystemExit

    out = capsys.readouterr().out
    assert len(calls) == 1
    assert "daemon not running. Start it first with: emrg" in out
