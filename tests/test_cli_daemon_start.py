"""What `emrg server restart` is allowed to announce (issue #1321).

The defect: the CLI had its own background spawn — `subprocess.Popen(…,
stderr=DEVNULL)` — and printed `daemon started (pid=N).` straight after it, with
no readiness probe in between. A child that died at import or config-parse stage
was therefore announced as started, its own stderr was discarded so the cause was
nowhere, and the host learned the truth later from somewhere else.

**Nothing in this file stops, starts or restarts a daemon.** MANIFESTO 第四条附则二
forbids any test that terminates or restarts the server, so the restart path is
never executed: what is asserted is the half that decides what to print, driven
with `daemon_manager.start_daemon` replaced in-process (so no process is spawned
and `cleanup_server` — the stop path — never runs), plus the pure failure
message, plus the wiring read from source.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from emrg import __main__ as cli
from emrg.client import daemon_manager as dm


@pytest.fixture(autouse=True)
def _reset_spawn_attempts():
    """The spawn gate is module state; one test must not throttle the next."""
    dm._spawn_attempts = 0
    yield
    dm._spawn_attempts = 0


class _StubProc:
    """A started daemon stand-in: the one fact the announcement reads."""

    pid = 4242


def _fake_start(result=None, error: BaseException | None = None):
    """A stand-in for the client's start path — no process, no port, no cleanup."""
    calls: list[int] = []

    async def _start():
        calls.append(1)
        if error is not None:
            raise error
        return result

    return _start, calls


# ── the announcement is a measurement, not a hope ───────────────────────────


def test_a_start_that_answered_is_announced_with_its_pid(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    start, calls = _fake_start(_StubProc())
    monkeypatch.setattr(dm, "start_daemon", start)

    assert cli._start_daemon_and_report() == 0
    assert calls == [1]
    assert "daemon started (pid=4242)." in capsys.readouterr().out


def test_a_start_that_never_answered_is_not_announced(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The regression itself: the success sentence was printed unconditionally."""
    start, _ = _fake_start(error=RuntimeError("emrgd failed to start within 4.5s"))
    monkeypatch.setattr(dm, "start_daemon", start)

    assert cli._start_daemon_and_report() == 1
    out = capsys.readouterr().out
    assert "daemon started" not in out, "no daemon answered, so nothing started"
    assert "NOT started" in out


@pytest.mark.parametrize("detail", [
    "emrgd exited during startup (exit=1)\n  emrgd 自身 stderr（本次启动新增）: ImportError: boom",
    "emrgd failed to start within 4.5s\n  this start attempt wrote nothing to emrgd.log",
    "daemon failed to start after 3 attempts — please run 'emrg server' manually",
])
def test_the_cause_the_start_left_behind_reaches_the_host(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
        detail: str) -> None:
    """A failure line without the evidence is the defect in a quieter form.

    The three shapes are the three ways `daemon_manager` reports a start that did
    not come up: the child exited (with a code), the window expired with a live
    child, and the spawn gate. Each carries what to read next; summarising them
    into one sentence would drop exactly that.
    """
    start, _ = _fake_start(error=RuntimeError(detail))
    monkeypatch.setattr(dm, "start_daemon", start)

    assert cli._start_daemon_and_report() == 1
    out = capsys.readouterr().out
    assert detail.splitlines()[-1] in out


def test_the_exit_code_follows_the_outcome(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Two restarts, two states, two exit codes — measured in one test so the
    pair cannot drift into both meaning 'fine'."""
    start, _ = _fake_start(_StubProc())
    monkeypatch.setattr(dm, "start_daemon", start)
    up = cli._start_daemon_and_report()
    capsys.readouterr()

    start, _ = _fake_start(error=RuntimeError("emrgd exited during startup (exit=1)"))
    monkeypatch.setattr(dm, "start_daemon", start)
    down = cli._start_daemon_and_report()
    capsys.readouterr()

    assert (up, down) == (0, 1)


def test_the_announcement_comes_from_the_real_client_start_path(
        monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture) -> None:
    """The CLI's call site against the *real* start path, with a stand-in child.

    Everything above replaces `daemon_manager.start_daemon`; this one does not, so
    the delegation itself is exercised: the log mark, the captured stderr and the
    readiness wait all run for real. Only the three things that leave the process
    are replaced — the spawn, the token cleanup (a stop path) and the port probe —
    which is the pattern `test_daemon_manager.py` already uses. Nothing is
    started: the "child" is a `MagicMock`, no port is opened, and `HOME` is pinned
    to a directory this test creates, so the log mark and `emrgd-start.err` are
    the test's files rather than the host's `~/.emrg`.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(dm, "cleanup_server", lambda: None)
    monkeypatch.setattr(dm, "is_running", lambda: True)
    proc = MagicMock(pid=4321)
    monkeypatch.setattr(dm.asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=proc))

    assert cli._start_daemon_and_report() == 0
    assert "daemon started (pid=4321)." in capsys.readouterr().out

    # The child's stderr is a real, opened file — not DEVNULL, and not the
    # host's: that channel is what names a cause when the child dies early.
    _args, kwargs = dm.asyncio.create_subprocess_exec.await_args
    assert kwargs["stderr"] is not subprocess.DEVNULL
    err = home / ".emrg" / "emrgd-start.err"
    assert err.exists(), "the captured stderr lives where the diagnostic reads it"


def test_the_announcement_is_withheld_until_the_probe_answers(
        monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture) -> None:
    """The same call site with a probe that never answers.

    The child is alive but nothing is listening — the exact state the old code
    announced as `daemon started`. Asserted on the real path: the CLI must exit
    non-zero and print no success sentence.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(dm, "cleanup_server", lambda: None)
    monkeypatch.setattr(dm, "is_running", lambda: False)
    monkeypatch.setattr(dm.asyncio, "sleep", AsyncMock())  # no real wait, same path
    monkeypatch.setattr(dm.asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=MagicMock(pid=4321)))

    assert cli._start_daemon_and_report() == 1
    out = capsys.readouterr().out
    assert "daemon started" not in out
    assert "failed to start within" in out, "the window that expired is named"


# ── the message, derived from the exception ────────────────────────────────


def test_the_start_message_is_not_the_success_line() -> None:
    """A collapse into the success sentence is the defect; assert the discrimination."""
    msg = cli._start_failure_message(RuntimeError("emrgd exited during startup (exit=1)"))
    assert "daemon started" not in msg
    assert "daemon was NOT started" in msg


def test_the_start_message_says_the_daemon_is_gone() -> None:
    """The host's state changed: the restart stopped one and started none."""
    msg = cli._start_failure_message(RuntimeError("emrgd failed to start within 4.5s"))
    assert "stopped" in msg and "never answered" in msg


def test_the_start_message_carries_the_evidence_verbatim() -> None:
    detail = ("emrgd exited during startup (exit=1)\n"
              "  emrgd 自身 stderr（本次启动新增）:\nImportError: no such patch")
    msg = cli._start_failure_message(RuntimeError(detail))
    assert "exit=1" in msg
    assert "ImportError: no such patch" in msg


# ── the wiring, read from source ───────────────────────────────────────────


def _source(obj) -> str:
    """Source with line endings normalised: a CRLF checkout would otherwise
    satisfy an absence assertion with a newline alone."""
    text = Path(cli.__file__).read_text(encoding="utf-8") if obj is None \
        else inspect.getsource(obj)
    return text.replace("\r\n", "\n")


def test_the_restart_announces_nothing_of_its_own() -> None:
    """The success sentence may only come from the path that measured it.

    `_restart_daemon` stops the daemon and delegates; if it printed the
    announcement itself, `_start_daemon_and_report`'s verified branch could be
    bypassed and the defect would be back with the tests still green.
    """
    src = _source(cli._restart_daemon)
    assert "daemon started" not in src
    assert "_start_daemon_and_report()" in src


def test_the_cli_start_is_the_client_start_path() -> None:
    """One background start in the product, asserted on the source.

    A text assertion because calling the real start path spawns a daemon; what it
    pins is that the CLI delegates rather than composing `daemon_manager`'s
    helpers itself — the drift between the two spawn sites is how one got the
    stderr diagnostic and the other did not (issue #1321's own note).
    """
    src = _source(None)
    assert "asyncio.run(dm.start_daemon())" in src


def test_the_cli_does_not_spawn_its_own_daemon() -> None:
    """The third spawn site must not come back.

    It was the defect: a `subprocess.Popen` of its own meant a start the product's
    other paths knew nothing about. Removing it is what makes the two halves —
    captured stderr and the readiness probe — impossible to lose again on this
    path, so the absence is the property, not a style preference.
    """
    src = _source(None)
    assert '"emrg.server"' not in src, "the CLI must not build its own daemon command line"
    assert "stderr=subprocess.DEVNULL" not in src, "a discarded child stderr has no cause"
