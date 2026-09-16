"""What a failed daemon start reports (issue #1276).

⚠️ Nothing in this file starts, stops or restarts a daemon. MANIFESTO 第四条附则二
forbids any test that terminates or restarts the server, so the pieces these tests
cover are the PURE ones: the log-delta reader, the diagnostic text, and the startup
wait loop driven by a stub child and a stub probe. No process is spawned, no port
is probed, and `start_daemon()` is never called — it calls `cleanup_server()`.

The two host-visible defects: a start failure printed the *previous* run's normal
SIGTERM shutdown as the crash that explained it, and a child that died in 50 ms
cost the whole 4.5 s window while reporting no exit code.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.client import daemon_manager as dm  # noqa: E402
from emrg.server import daemon as srv  # noqa: E402


# ── the log delta, not the file ─────────────────────────────────────────────


def test_the_tail_is_empty_when_this_attempt_appended_nothing(tmp_path):
    """The defect: history presented as this attempt's cause."""
    log = tmp_path / "emrgd.log"
    log.write_text("old run: SystemExit: SIGTERM (15) received\n", encoding="utf-8")
    mark = dm._log_mark(log)
    assert dm._read_log_tail(log, lines=15, since=mark) == ""
    # and the same reader still answers when there IS something new
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("this attempt: config.toml is not valid TOML\n")
    got = dm._read_log_tail(log, lines=15, since=mark)
    assert "this attempt" in got and "old run" not in got


def test_the_delta_reader_counts_lines_within_the_delta(tmp_path):
    log = tmp_path / "emrgd.log"
    log.write_text("old\n", encoding="utf-8")
    mark = dm._log_mark(log)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("\n".join(f"new{i}" for i in range(20)) + "\n")
    got = dm._read_log_tail(log, lines=3, since=mark).splitlines()
    assert got == ["new17", "new18", "new19"]


def test_a_missing_or_unreadable_log_is_not_an_exception(tmp_path):
    assert dm._read_log_tail(tmp_path / "nope.log", since=(0, None)) == ""
    assert dm._log_mark(tmp_path / "nope.log") == (0, None)


def _handler(path, cap):
    """The handler emrgd itself installs (`emrg/server/__main__.py`)."""
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(path, maxBytes=cap, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _emit(handler, message: str) -> None:
    handler.emit(logging.LogRecord("emrg", logging.ERROR, __file__, 0, message, None, None))


@pytest.mark.parametrize("pre_spawn_bytes,new_record_bytes", [(1017, 120), (135, 1200)])
def test_a_rotated_log_is_read_from_the_start_not_from_a_stale_offset(
    tmp_path, pre_spawn_bytes, new_record_bytes
):
    """The log is *replaced* by its handler, so a bare offset indexes another file.

    Measured on this handler's own rollover, in both size regimes that produce a
    wrong answer, because the identity is the discriminator and the size is not
    (the inode changes in both):

    * large pre-spawn log (1017 + 120 over a 1024 cap): the mark lands **past** the
      new file's end, the read answers `""`, and the host is told "this start
      attempt wrote nothing" while the new file holds this attempt's error text —
      R124's swallowing, reintroduced by the diagnostic that exists to close it;
    * small pre-spawn log (135 + 1200): the mark lands **inside** a line of the new
      file, and a fragment is reported as this attempt's output.
    """
    cap = 1024
    log = tmp_path / "emrgd.log"
    log.write_text("previous " * (pre_spawn_bytes // 9), encoding="utf-8")
    mark = dm._log_mark(log)

    _emit(_handler(log, cap), "THIS-ATTEMPT " + "y" * new_record_bytes)

    assert (tmp_path / "emrgd.log.1").exists(), "the handler must really have rotated"
    assert log.stat().st_ino != mark[1], "a rotation replaces the file, which is the point"
    # both size regimes, asserted rather than assumed, so this cannot stop testing one
    if new_record_bytes > mark[0]:
        assert log.stat().st_size > mark[0], "regime: the mark is inside the new file"
    else:
        assert log.stat().st_size < mark[0], "regime: the mark is past the new file's end"

    got = dm._read_log_tail(log, lines=15, since=mark)
    assert "THIS-ATTEMPT" in got, "this attempt's own first bytes must survive the rotation"
    assert "previous" not in got, "the file the mark named is gone; none of its text is new"


def test_a_log_truncated_in_place_is_read_from_the_start(tmp_path):
    """The one case the inode cannot see: same file, fewer bytes than the mark.

    Not a rotation (a `RotatingFileHandler` replaces the file), so this is the
    half the size comparison owns — the identity comparison cannot catch it.
    """
    log = tmp_path / "emrgd.log"
    log.write_text("previous run: SystemExit: SIGTERM (15) received\n", encoding="utf-8")
    mark = dm._log_mark(log)
    log.write_text("this attempt: config.toml is not valid TOML\n", encoding="utf-8")
    assert log.stat().st_ino == mark[1], "truncated in place: still the file the mark named"
    assert log.stat().st_size < mark[0], "the mark is past the end of what is there now"

    got = dm._read_log_tail(log, lines=15, since=mark)
    assert "this attempt" in got and "SIGTERM" not in got


def test_a_bare_offset_is_not_a_mark(tmp_path):
    """The shape that caused the defect cannot be written any more.

    An offset alone cannot identify a file the handler replaces, so it is not
    accepted as a mark at all: the failure is a loud `TypeError` at the call, not
    a silent mis-read of a different file — which is how this defect survived a
    suite that had a whole-file and a delta reader.
    """
    log = tmp_path / "emrgd.log"
    log.write_text("previous\n", encoding="utf-8")
    with pytest.raises(TypeError):
        dm._read_log_tail(log, lines=15, since=log.stat().st_size)


def test_the_mark_is_taken_before_the_child_can_write(tmp_path):
    """A size taken after the spawn would already include this attempt's output.

    The expected size is read back from the **file**, never from ``len(text)``:
    a text-mode write turns ``\\n`` into ``\\r\\n`` on Windows, so a byte count
    derived from the string is a POSIX assumption. Measured the hard way — the
    Windows leg of CI rejected exactly that assertion.
    """
    log = tmp_path / "emrgd.log"
    log.write_text("previous run\n", encoding="utf-8")
    mark = dm._log_mark(log)
    assert mark[0] == log.stat().st_size > 0 and mark[1] == log.stat().st_ino
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("this attempt\n")
    assert dm._log_mark(log)[0] > mark[0], "the mark must be a point in the file, not a constant"
    assert dm._read_log_tail(log, since=mark).strip() == "this attempt"


def test_a_silent_child_is_reported_as_silent_not_as_an_older_run(tmp_path):
    log = tmp_path / "emrgd.log"
    log.write_text("previous run: SystemExit: SIGTERM (15) received\n", encoding="utf-8")
    mark = dm._log_mark(log)

    class Dead:
        returncode = 7

    detail = dm._startup_failure_detail(log, mark, Dead())
    assert "wrote nothing" in detail
    assert "exit=7" in detail
    assert "SIGTERM" not in detail, "an older run's shutdown must not be shown as the cause"
    assert "previous run" in detail, "it is still named as old, so it cannot be mistaken"


def test_a_child_that_wrote_something_reports_those_lines(tmp_path):
    log = tmp_path / "emrgd.log"
    log.write_text("previous\n", encoding="utf-8")
    mark = dm._log_mark(log)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("this attempt: Traceback ...\nRuntimeError: bad config\n")

    class Dead:
        returncode = 1

    detail = dm._startup_failure_detail(log, mark, Dead())
    assert "本次启动新增" in detail
    assert "RuntimeError: bad config" in detail


# ── the startup wait: fail fast, and say what is known ──────────────────────


class StubProc:
    """A child stand-in: no process, just the two facts the loop reads."""

    def __init__(self, returncode=None):
        self.returncode = returncode


def test_a_dead_child_fails_fast_instead_of_burning_the_window(tmp_path):
    """issue #1276 item 3: the loop never asked whether the child was alive."""
    log = tmp_path / "emrgd.log"
    log.write_text("previous\n", encoding="utf-8")
    ticks: list[float] = []

    async def fake_sleep(seconds):
        ticks.append(seconds)

    real_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        with pytest.raises(RuntimeError) as err:
            asyncio.run(dm._await_daemon_ready(
                StubProc(returncode=143), log, dm._log_mark(log),
                probe=lambda: False, attempts=15, delay=0.3))
    finally:
        asyncio.sleep = real_sleep
    assert len(ticks) == 1, "one tick, not fifteen"
    assert "exited during startup" in str(err.value)
    assert "exit=143" in str(err.value)


def test_a_live_child_that_never_comes_up_reports_the_window(tmp_path):
    log = tmp_path / "emrgd.log"

    async def fake_sleep(seconds):
        return None

    real_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        with pytest.raises(RuntimeError) as err:
            asyncio.run(dm._await_daemon_ready(
                StubProc(returncode=None), log, (0, None),
                probe=lambda: False, attempts=4, delay=0.25))
    finally:
        asyncio.sleep = real_sleep
    assert "failed to start within 1.0s" in str(err.value)
    assert "still running" in str(err.value), "a live child is a different fact"


def test_a_child_that_comes_up_returns_quietly(tmp_path):
    async def fake_sleep(seconds):
        return None

    real_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        asyncio.run(dm._await_daemon_ready(
            StubProc(returncode=None), tmp_path / "emrgd.log", (0, None),
            probe=lambda: True, attempts=4, delay=0.01))
    finally:
        asyncio.sleep = real_sleep


# ── a stop is not a crash ───────────────────────────────────────────────────


def test_sigterm_is_logged_as_a_stop_without_a_traceback():
    level, message, exc_info = srv._serve_exit_log_record("sigterm", SystemExit("SIGTERM (15) received"))
    assert level == logging.INFO
    assert "stopped" in message and "not a crash" in message
    assert exc_info is False, "a signal handler's frames name no cause"


def test_a_real_crash_keeps_the_word_and_the_traceback():
    level, message, exc_info = srv._serve_exit_log_record("crash", RuntimeError("boom"))
    assert level == logging.CRITICAL
    assert "crashed" in message and "RuntimeError: boom" in message
    assert exc_info is True


def test_the_classification_in_run_server_is_the_one_used():
    """The defect was a computed `reason` that the log line ignored: assert the
    caller routes through the helper rather than re-deciding the wording.

    Line endings are normalised first: on a CRLF checkout the `not in` half would
    otherwise be satisfied by the newline alone and stop guarding anything, which
    is the silent-pass failure mode rather than a red one.
    """
    src = Path(srv.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "level, message, with_traceback = _serve_exit_log_record(reason, exc)" in src
    assert 'logger.critical(\n            "daemon crashed' not in src
