"""The daemon's log sink states the frame-logger rule, and this file reads it back (#2160).

`emrg/server/__main__.py::_configure_logging` raises the root logger to `DEBUG` and then pins the
frame logger down to `INFO`. That call is this whole surface's **origin**: the measurement written
beside it — frame-level send/receive records were ~90 % of `emrgd.log` and pure noise, while `INFO`
keeps `connection open` / `connection closed` (rant `2026-08-06T10:21:26`) — is why the rule exists
at all, and why every other log sink is being taught to mirror it.

Measured 2026-10-13 on master `daab42c6`: setting that pin to `DEBUG` **survives the entire suite**
(`4908 passed, 16 skipped`) — no test in the tree reads the level. So the daemon's own carrier of
the rule was the one with no arm.

Both arms drive the **real** `_configure_logging()` under a stubbed `HOME` and read the file it
actually writes. A source-shape assertion would pass on a sink whose handler went away, and a level
that is set and then never reached by a record is not the property this pins.

The client's sink carries the same rule under its own arm (`tests/test_client_log_frame_noise.py`).
The two files deliberately do **not** read each other: each drives its own sink and reads its own
file, so neither verdict depends on the other's spelling. What no arm measures is that the two
carriers *agree* — stated here as the boundary rather than implied.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.server import __main__ as server_main  # noqa: E402

#: Where `_configure_logging` puts the daemon's file, relative to the stubbed home.
SINK = Path(".emrg") / "logs" / "emrgd.log"

#: A frame payload as the library would render it, carrying a marker this test can search for.
FRAME_RECORD = '> TEXT \'{"type":"ask","text":"MARKER-THE-HOSTS-MESSAGE"}\''


def _stub_home(monkeypatch, home: Path) -> None:
    """Point every home lookup at `home`, on both platforms.

    `Path.home()` reads `os.path.expanduser("~")` per call, so setting the variables is enough —
    and both spellings are set because Windows' `ntpath` reads `USERPROFILE` while POSIX reads
    `HOME`, so a test that set one would silently write into the host's real directory on the
    other.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@contextlib.contextmanager
def _root_handlers_cleared():
    """Let `logging.basicConfig` install a handler while this block runs.

    `basicConfig` is a no-op when the root logger already has handlers, and pytest's logging
    plugin puts one there for every test — so without this the sink under test would install
    nothing and report green while having written no file at all. What it installs is closed on
    the way out, so the block also bounds the sink's global effect on the rest of the session.
    """
    saved_handlers = logging.root.handlers[:]
    saved_level = logging.root.level
    logging.root.handlers = []
    try:
        yield
    finally:
        for handler in logging.root.handlers:
            with contextlib.suppress(Exception):
                handler.close()
        logging.root.handlers = saved_handlers
        logging.root.level = saved_level


@contextlib.contextmanager
def _websockets_level_restored():
    """The sink sets a **global** level; a test that sets one has to put it back."""
    logger = logging.getLogger("websockets")
    saved = logger.level
    try:
        yield logger
    finally:
        logger.setLevel(saved)


def test_the_daemon_sink_pins_the_frame_logger_to_info(tmp_path, monkeypatch):
    """`INFO` is the level, and the child logger the library really uses inherits it."""
    _stub_home(monkeypatch, tmp_path)
    with _websockets_level_restored() as logger:
        with _root_handlers_cleared():
            server_main._configure_logging()

        assert logger.level == logging.INFO, (
            f"the daemon's sink left `websockets` at {logging.getLevelName(logger.level)} — the "
            "rule it states in its own comment is that frame records are dropped and the "
            "connection lifecycle kept"
        )
        # The library logs frames from a child logger, so the property is the child's
        # *effective* level — not the parent's attribute.
        frames = logging.getLogger("websockets.client")
        assert not frames.isEnabledFor(logging.DEBUG), (
            "a frame record would reach `emrgd.log` — the ~90 % of that file the rule exists to "
            "keep out, with the conversation's payload inside each line"
        )
        assert frames.isEnabledFor(logging.INFO), (
            "the connection lifecycle was dropped too: `connection open` is an INFO record"
        )


def test_a_frame_record_does_not_reach_the_daemon_log_but_the_lifecycle_one_does(
    tmp_path, monkeypatch
):
    """The end of the chain: what the daemon's file actually holds after the sink ran."""
    sink = tmp_path / SINK
    _stub_home(monkeypatch, tmp_path)

    with _websockets_level_restored():
        with _root_handlers_cleared():
            server_main._configure_logging()
            frames = logging.getLogger("websockets.client")
            frames.debug(FRAME_RECORD)
            frames.info("connection open")

    assert sink.exists(), "the sink wrote no file at all — the arm below would pass vacuously"
    written = sink.read_text(encoding="utf-8")
    assert "connection open" in written, (
        f"the INFO lifecycle record did not reach the daemon log: {written!r}"
    )
    assert "MARKER-THE-HOSTS-MESSAGE" not in written, (
        "a frame payload was written to the daemon log — the defect the sink's own comment "
        f"measures at ~90 % of that file: {written!r}"
    )
