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


# ── the child's own stderr: the channel a start that dies before logging has ─


def test_the_captured_stderr_is_truncated_so_it_is_only_this_attempt(tmp_path):
    """The same rule as the log mark, one level down: history is not this attempt.

    ``emrgd-start.err`` is truncated at spawn, so everything a failure report can
    quote from it was written by *this* attempt. Appending instead would put an
    earlier run's traceback back in front of the host — the defect the log-delta
    reader exists for (issue #1276), reintroduced through the new file.
    """
    err = tmp_path / "emrgd-start.err"
    err.write_bytes(b"previous attempt: ImportError: no such patch\n")
    handle = dm._truncate_start_stderr(err)
    assert handle is not None
    try:
        assert err.read_bytes() == b"", "the earlier attempt's bytes must be gone"
        handle.write(b"this attempt: ImportError: real cause\n")
    finally:
        handle.close()
    assert dm._read_start_stderr(err).strip() == "this attempt: ImportError: real cause"


def test_the_child_itself_writes_into_the_captured_stderr(tmp_path):
    """The plumbing, driven by a **stand-in** child: nothing here is the daemon.

    MANIFESTO 第四条附则二 forbids any test that stops or restarts `emrgd`, and
    this one can only ever spawn `python -c "exit(3)"` — a process that is not the
    server, does not read a port file and cannot terminate a daemon. What it
    proves is the mechanism the real spawn now relies on: a handle from
    `_truncate_start_stderr` passed as a child's stderr really does land in that
    file, which is the only channel a child that dies before installing its
    logging handler leaves behind.
    """
    import subprocess

    err = tmp_path / "emrgd-start.err"
    handle = dm._truncate_start_stderr(err)
    assert handle is not None
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.stderr.write('Traceback ...\\nImportError: boom\\n'); sys.exit(3)"],
            stderr=handle,
        )
    finally:
        handle.close()
    assert proc.returncode == 3
    got = dm._read_start_stderr(err)
    assert "Traceback ..." in got and "ImportError: boom" in got


def test_a_child_that_died_before_logging_has_its_own_stderr_reported(tmp_path):
    """issue #1276's second symptom: the report was *nothing at all*.

    A child that fails at import stage writes only to stderr, so before this
    section existed the host was shown an empty emrgd.log and told nothing — the
    first-run symptom was a mislabelled cause, the second was no cause at all.
    """
    log = tmp_path / "emrgd.log"
    log.write_text("previous run: SystemExit: SIGTERM (15) received\n", encoding="utf-8")
    mark = dm._log_mark(log)
    err = tmp_path / "emrgd-start.err"
    err.write_bytes(b"Traceback (most recent call last):\nImportError: boom\n")

    class Dead:
        returncode = 1

    detail = dm._startup_failure_detail(log, mark, Dead(), err)
    assert "ImportError: boom" in detail, "the child's own cause is the one fact this adds"
    assert "SIGTERM" not in detail, "the older run's shutdown still must not be shown"
    assert "wrote nothing to emrgd.log" in detail, "the log half is still reported"


def test_the_child_stderr_is_reported_before_the_log_tail(tmp_path):
    """Both channels can be non-empty, and the order is the argument.

    The child's own stderr is what it said *while dying*; the log tail is what its
    logging was already able to record. The more direct fact comes first, so a
    reader who stops after the first section has read the cause.
    """
    log = tmp_path / "emrgd.log"
    log.write_text("previous\n", encoding="utf-8")
    mark = dm._log_mark(log)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("this attempt: config.toml is not valid TOML\n")
    err = tmp_path / "emrgd-start.err"
    err.write_bytes(b"child: ImportError: no module named 'x'\n")

    class Dead:
        returncode = 1

    detail = dm._startup_failure_detail(log, mark, Dead(), err)
    assert "ImportError" in detail and "config.toml" in detail
    assert detail.index("ImportError") < detail.index("config.toml"), (
        "the child's own last words come first"
    )


def test_an_uncaptured_child_is_named_uncaptured_not_silent(tmp_path):
    """A channel nobody read is not a silent channel.

    Silence is a *measurement*, so it needs a channel to have been read. The
    reader is given no path here — the parameter's default, and what the spawn
    hands over when it could not open the file, so the child's stderr went to
    DEVNULL — and the earlier shape answered "the child wrote nothing to its own
    stderr" anyway: a claim about a channel the report never opened. The log half
    is still named as silent, because that channel *was* read.
    """
    log = tmp_path / "emrgd.log"
    log.write_text("previous\n", encoding="utf-8")

    class Dead:
        returncode = 9

    detail = dm._startup_failure_detail(log, dm._log_mark(log), Dead())
    assert "wrote nothing to emrgd.log" in detail
    assert "was not captured" in detail, "the channel that was not read is named as such"
    assert "wrote nothing to its own stderr" not in detail, (
        "nothing was read from that channel, so its silence cannot be claimed"
    )
    assert "exit=9" in detail


def test_an_uncaptured_channel_is_named_when_a_log_tail_is_reported(tmp_path):
    """The same fact, in the branch that prints the tail: an absent section is read.

    With a log tail to show, the report has no sentence left to say "the child
    wrote nothing to its own stderr" — so the *absence* of that section is what a
    host would read as silence. The unread channel is named in the tail branch
    instead, which is why the note is not dead code.
    """
    log = tmp_path / "emrgd.log"
    log.write_text("previous\n", encoding="utf-8")
    mark = dm._log_mark(log)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("this attempt: RuntimeError: bad config\n")

    class Dead:
        returncode = 1

    detail = dm._startup_failure_detail(log, mark, Dead(), None)
    assert "RuntimeError: bad config" in detail, "the tail is still what this attempt wrote"
    assert "未捕获" in detail, "the unread channel is named next to the tail"
    assert "wrote nothing to its own stderr" not in detail


def test_a_captured_child_that_wrote_nothing_is_still_named_silent(tmp_path):
    """The other half of the same distinction: the claim survives where it is true.

    The file exists, this attempt truncated it, and it came back empty — that is a
    measurement of the channel, and the report makes the claim it supports.
    """
    log = tmp_path / "emrgd.log"
    log.write_text("previous\n", encoding="utf-8")
    err = tmp_path / "emrgd-start.err"
    handle = dm._truncate_start_stderr(err)
    assert handle is not None
    handle.close()

    class Dead:
        returncode = 9

    detail = dm._startup_failure_detail(log, dm._log_mark(log), Dead(), err)
    assert "wrote nothing to its own stderr" in detail
    assert "未捕获" not in detail
    assert "exit=9" in detail


def test_the_uncaptured_channel_cannot_quote_an_earlier_attempt(tmp_path):
    """Both faces of one bug, at the call site's own reduction.

    ``emrgd-start.err`` readable but not writable (mode 444, or one left behind by
    an earlier ``sudo emrg …``) is the shape where the spawn falls back to DEVNULL
    while the *reader* would still succeed: the report quoted an older attempt's
    bytes as this failure's cause, and the sentence that replaced the quote then
    published the silence claim about the channel it had never opened. What the
    call site hands over is ``stderr_path if stderr_handle is not None else None``,
    and that reduction is applied here to a file that really is unwritable.
    """
    log = tmp_path / "emrgd.log"
    log.write_text("previous\n", encoding="utf-8")
    err = tmp_path / "emrgd-start.err"
    err.write_bytes(b"ImportError: STALE-FROM-AN-EARLIER-ATTEMPT\n")
    err.chmod(0o444)
    try:
        handle = dm._truncate_start_stderr(err)
    finally:
        err.chmod(0o644)
    if handle is not None:
        handle.close()
        pytest.skip("this filesystem lets the owner write a 444 file — unmeasurable here")

    class Dead:
        returncode = 1

    detail = dm._startup_failure_detail(
        log, dm._log_mark(log), Dead(), err if handle is not None else None
    )
    assert "STALE-FROM-AN-EARLIER-ATTEMPT" not in detail, (
        "an earlier attempt's bytes are not this attempt's cause"
    )
    assert "wrote nothing to its own stderr" not in detail, (
        "the child's stderr went to DEVNULL: its silence is unknown, not measured"
    )
    assert "was not captured" in detail


def test_the_stderr_line_cap_keeps_the_end_of_a_traceback(tmp_path):
    """A traceback's cause is its *end*, and 15 lines is not enough for that.

    The log tail's cap is 15 because a log line is self-contained; a traceback is
    not — its useful half is the exception line at the bottom. Measured here with
    a body longer than either cap.
    """
    err = tmp_path / "emrgd-start.err"
    err.write_bytes(
        ("Traceback (most recent call last):\n"
         + "".join(f'  File "f{i}.py", line {i}, in <module>\n' for i in range(60))
         + "ImportError: the cause\n").encode()
    )
    got = dm._read_start_stderr(err)
    assert "ImportError: the cause" in got, "the last line is the one that names the cause"
    assert len(got.splitlines()) == 40, "the cap is 40 lines, and it is applied"
    assert dm._read_start_stderr(tmp_path / "nope.err") == "", "unreadable is not an exception"
    assert dm._read_start_stderr(None) == "", "no path is not an exception either"


def test_start_daemon_captures_the_child_stderr_instead_of_discarding_it():
    """The wiring itself, asserted on the source: the diagnostic above is dead code
    unless the spawn passes the handle through.

    A text assertion rather than a call, because calling `start_daemon()` spawns —
    and, worse, calls `cleanup_server()` first, which is a stop path. Line endings
    are normalised so a CRLF checkout cannot satisfy the assertion with a newline.

    Four fragments, each a different way for the channel to go dead: the handle is
    opened from the path the diagnostic reads, the child's stderr is that handle,
    and what the report is handed is that handle's *outcome* — `… if … is not None
    else None`, the reduction that keeps an uncaptured channel from being quoted
    and from being called silent. Dosing the open line leaves the other three
    present with the feature dead: measured on the mutant, 1 failed (the reviewer
    of this head found the same gap).
    """
    src = Path(dm.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "stderr=stderr_handle if stderr_handle is not None else subprocess.DEVNULL" in src
    assert "stderr_path = _start_stderr_path()" in src
    assert "stderr_handle = _truncate_start_stderr(stderr_path)" in src
    assert "stderr_path=stderr_path if stderr_handle is not None else None" in src
