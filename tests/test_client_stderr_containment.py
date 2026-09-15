"""The TUI's terminal belongs to the TUI (rant 2026-09-14T21:50:54).

The host pasted what had appeared on their screen:

    tui显示了： File ".../emrg/client/app.py", line 308, in interactive
                 await conn.send_command("ping")
                 ...
                 File ".../websockets/protocol.py", line 755, in send_frame
                   self.logger.debug("> %s", frame)
               Message: '> %s'
               Arguments: (Frame(opcode=<Opcode.TEXT: 1>, ...),)
               --- Logging error ---
               Traceback (most recent call last):
                 File ".../logging/handlers.py", line 79, in emit
                   if self.shouldRollover(record):
                 File ".../logging/handlers.py", line 199, in shouldRollover
                   pos = self.stream.tell()

and stated the requirement:

    「任何时候，tui都不应该显示内部错误。也就是内部异常只应该在日志里，
      不应该出现在tui里」

That dump is ``logging.Handler.handleError``, whose report goes to
``sys.stderr`` — and for a TUI, the screen. These tests pin the *policy*, not
the one cause: whatever writes to the client's stderr mid-session must end up
in ``<cwd>/.emrg/emrg-client-crash.log`` instead of on the screen.

⚠️ Harness note, measured 2026-09-14: the screen must be installed **in the
test body**, never from a fixture. pytest's capture manager re-installs its own
``sys.stderr`` between the setup phase and the call phase
(``CaptureManager.item_capture`` → ``resume_global_capture`` →
``FDCapture.resume`` → ``setattr(sys, "stderr", tmpfile)``,
``_pytest/capture.py``), so a ``monkeypatch`` applied during fixture setup is
gone before the test body runs. With the patch lost, the guard saw pytest's
capture object, decided "not a TTY", did nothing, and these assertions passed
for the wrong reason — a green suite proving nothing.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import sys
from logging.handlers import RotatingFileHandler

import pytest

from emrg.client import app as client_app


class _FakeTerminal(io.StringIO):
    """Stands in for the screen: a text stream that claims to be a TTY."""

    def isatty(self) -> bool:
        return True


class _FakeTtyStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


def _crash_log(cwd):
    return cwd / ".emrg" / "emrg-client-crash.log"


@contextlib.contextmanager
def _screen(screen):
    """Make ``screen`` the process's ``sys.stderr`` for the block.

    Deliberately a body-level context manager and not a fixture — see the
    harness note in this module's docstring.
    """
    original = sys.stderr
    sys.stderr = screen
    try:
        yield screen
    finally:
        sys.stderr = original


@pytest.fixture
def screen(tmp_path, monkeypatch):
    """A fake screen plus a live-logging cwd. The screen is *not* installed."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    return _FakeTerminal(), tmp_path


@pytest.fixture
def broken_rotating_handler(tmp_path, monkeypatch):
    """The host's shape: a RotatingFileHandler whose stream has gone bad."""
    handler = RotatingFileHandler(str(tmp_path / "emrg-client.log"),
                                  maxBytes=1024, backupCount=1)
    handler.stream.close()  # emit() → shouldRollover() → stream.tell() now raises
    monkeypatch.setattr(logging.getLogger(), "handlers", [handler])
    return handler


# ── the reported mechanism: a failing handler in mid-session ────────────────


def test_failing_rotating_handler_does_not_print_to_the_terminal(
        screen, broken_rotating_handler):
    """``handleError``'s report must go to the crash log, not the screen."""
    screen, cwd = screen

    with _screen(screen):
        guard = client_app._contain_stderr_for_tui()
        try:
            logging.getLogger().warning("> %s", "frame")
        finally:
            guard.restore()

    assert screen.getvalue() == "", "the screen must stay free of internal output"
    crash = _crash_log(cwd).read_text(encoding="utf-8")
    assert "--- Logging error ---" in crash
    assert "> %s" in crash
    assert "Traceback" in crash


def test_a_handler_that_captured_the_terminal_is_repointed(screen, monkeypatch):
    """``StreamHandler()`` binds ``sys.stderr`` at construction.

    Swapping ``sys.stderr`` alone would leave such a handler writing to the
    screen, so containment repoints the handlers that still target it.
    """
    screen, cwd = screen

    with _screen(screen):
        handler = logging.StreamHandler()  # binds the *object* sys.stderr (= screen)
        monkeypatch.setattr(logging.getLogger(), "handlers", [handler])

        guard = client_app._contain_stderr_for_tui()
        try:
            logging.getLogger().warning("internal detail")
        finally:
            guard.restore()

        assert handler.stream is screen, "the terminal must be handed back"

    assert screen.getvalue() == ""
    assert "internal detail" in _crash_log(cwd).read_text(encoding="utf-8")


def test_a_handler_on_a_named_logger_is_repointed(screen, monkeypatch):
    """A ``StreamHandler`` binds ``sys.stderr`` at construction — on *any* logger.

    The repoint loop walked ``logging.getLogger().handlers``, root only, while
    the record in the host's traceback comes from a **named** logger
    (``websockets/protocol.py``: ``self.logger.debug("> %s", frame)``). A
    handler on a named logger is the same defect one logger over, and the policy
    is "whatever writes to the client's stderr".
    """
    screen, cwd = screen
    logger = logging.getLogger("websockets.client")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logging.DEBUG)

    with _screen(screen):
        handler = logging.StreamHandler()      # binds the *object* sys.stderr (= screen)
        logger.addHandler(handler)             # a named logger, not root

        guard = client_app._contain_stderr_for_tui()
        try:
            logger.debug("> %s", "frame")
        finally:
            guard.restore()

        assert handler.stream is screen, "the terminal must be handed back"

    assert screen.getvalue() == "", "a named logger's record reached the screen"
    # the handler *formats* the record; only ``handleError`` echoes the raw message
    assert "> frame" in _crash_log(cwd).read_text(encoding="utf-8")


def test_containment_leaves_the_last_resort_handler_alone(screen, monkeypatch):
    """``logging.lastResort`` cannot be repointed, and need not be.

    It is a ``_StderrHandler`` whose ``stream`` is a read-only property returning
    the *current* ``sys.stderr``: it follows the swap by itself, and
    ``handler.stream = sink`` raises ``AttributeError: property 'stream' of
    '_StderrHandler' object has no setter``. It also sits in
    ``logging._handlerList``, so a containment that widened by walking *that*
    list would raise from inside itself — before ``sys.stderr`` is swapped —
    and print the traceback on the screen, producing the symptom it removes.
    """
    screen, cwd = screen
    monkeypatch.setattr(logging.getLogger(), "handlers", [])   # nothing but lastResort

    with _screen(screen):
        guard = client_app._contain_stderr_for_tui()           # must not raise
        try:
            logging.getLogger("emrg.client.probe").warning("last resort")
        finally:
            guard.restore()

    assert screen.getvalue() == ""
    assert "last resort" in _crash_log(cwd).read_text(encoding="utf-8")


# ── the two states of the guard itself ────────────────────────────────────


def test_a_user_redirected_stderr_is_left_alone(tmp_path, monkeypatch):
    """``emrg 2>errors.txt`` asked for stderr there; no screen to corrupt."""
    sink = io.StringIO()  # isatty() is False
    monkeypatch.chdir(tmp_path)

    with _screen(sink):
        guard = client_app._contain_stderr_for_tui()
        try:
            assert sys.stderr is sink
        finally:
            guard.restore()

        assert sys.stderr is sink
        assert not _crash_log(tmp_path).exists()


def test_restore_puts_the_terminal_back(screen):
    screen, _cwd = screen

    with _screen(screen):
        guard = client_app._contain_stderr_for_tui()
        assert sys.stderr is not screen
        guard.restore()
        assert sys.stderr is screen
        guard.restore()  # idempotent: a second call must not raise
        assert sys.stderr is screen


def test_two_containments_hand_the_terminal_back_in_order(screen, monkeypatch):
    """``restore()`` must undo exactly what it did, newest first."""
    screen, _cwd = screen

    with _screen(screen):
        handler = logging.StreamHandler()
        monkeypatch.setattr(logging.getLogger(), "handlers", [handler])

        outer = client_app._contain_stderr_for_tui()
        inner = client_app._contain_stderr_for_tui()
        inner.restore()
        assert sys.stderr is not screen, "the outer containment is still in force"
        outer.restore()
        assert sys.stderr is screen
        assert handler.stream is screen, "the terminal must be handed back"


# ── end to end, at the call site the host's traceback named ───────────────


def test_run_client_contains_the_whole_session(screen, monkeypatch):
    """The containment covers everything, including the session body.

    ``run_client`` installs it before ``asyncio.run`` builds the event loop, so
    a record emitted while the loop is being created is already contained —
    nothing internal is left to a "before the TUI is up" window.
    """
    screen, cwd = screen
    seen: dict = {}

    async def _stub(init_auto_evolve=False, console=None):
        seen["contained_at_entry"] = sys.stderr is not screen
        seen["console_is_the_terminal"] = console is screen
        # At session entry stderr is already the sink: loop creation, the
        # session body and everything after it share the same containment.
        seen["sink"] = getattr(sys.stderr, "name", None)

    monkeypatch.setattr(client_app, "interactive", _stub)

    with _screen(screen):
        client_app.run_client()

    assert seen["contained_at_entry"] is True
    assert seen["console_is_the_terminal"] is True
    assert seen["sink"] == str(_crash_log(cwd))
    assert screen.getvalue() == ""


def test_a_connect_failure_still_reaches_the_terminal(screen, monkeypatch):
    """The policy's other direction: a message the *user* needs is not contained.

    "Failed to connect to emrgd" is how the host learns the daemon is down; it
    goes to the stream captured before the containment was installed.
    """
    screen, _cwd = screen
    monkeypatch.setattr(sys, "stdin", _FakeTtyStdin())

    async def _refused():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(client_app.daemon_manager, "ensure_connected", _refused)

    with _screen(screen):
        client_app.run_client()

    assert "Failed to connect to emrgd: connection refused" in screen.getvalue()


def test_a_crash_never_reaches_the_screen(
        tmp_path, monkeypatch, broken_rotating_handler):
    """``app.py interactive → conn.send_command("ping")``, reproduced.

    A log record emitted from inside ``send_command`` goes through a broken
    handler; the failure report must not reach the terminal — including when
    the exception that follows escapes the session, which is the window in
    which the host's screen got written to.
    """
    screen = _FakeTerminal()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    monkeypatch.setattr(sys, "stdin", _FakeTtyStdin())

    class _Conn:
        async def send_command(self, type_, **kw):
            logging.getLogger("websockets.client").debug("> %s", "frame")
            raise RuntimeError("daemon went away")

    async def _connected():
        return _Conn()

    monkeypatch.setattr(client_app.daemon_manager, "ensure_connected", _connected)

    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.DEBUG)  # websockets' frame log is DEBUG
    with _screen(screen):
        try:
            with pytest.raises(RuntimeError):
                client_app.run_client()
            # The session died with an exception, and the containment is still
            # in force on purpose: that traceback is printed by the process
            # entry point, after ``run_client`` returns — it belongs in the
            # crash log, not on the screen.
            assert sys.stderr is not screen
        finally:
            root.setLevel(previous_level)

    assert screen.getvalue() == ""
    assert "--- Logging error ---" in _crash_log(tmp_path).read_text(encoding="utf-8")
