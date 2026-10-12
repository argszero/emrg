"""The client's log sink keeps the connection lifecycle and drops the frame traffic (#2157).

`websockets` logs every frame it sends and receives at `DEBUG`, with the frame's payload
inside the record. The daemon's sink pins that logger to `INFO` on purpose — the comment in
`emrg/server/__main__.py` says why (rant `2026-08-06T10:21:26`: frame-level records were ~90 %
of `emrgd.log` and pure noise, while `INFO` keeps `connection open` / `connection closed`).
The client's sink, which states its own root level and installs its own handler on
``<cwd>/.emrg/emrg-client.log``, suppressed only `markdown_it`.

Measured 2026-10-12 on this host's own `~/.emrg/emrg-client.log` (mode `0644`, 6363 lines):
**3226** `websockets.client` records, of which 3129 are received `TEXT` frames whose payload
begins `{` — half the file. Those frames are the conversation: the daemon streams assistant
text as `TEXT` frames and the client sends the host's message out as one, so a credential
typed into the TUI lands there (a `sk-…` key is ~51 characters, inside the library's
truncation). The daemon's `~/.emrg/emrgd.log` holds `websockets.server` records at `INFO`
only, 160 of them, which is the rule holding on the other sink.

The arms below read the **emitted file**, not the source line: a level that is computed,
corrected and never reached by a record is not the property this pins, and a source-shape
assertion would pass on a sink whose handler went away.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg import __main__ as emrg_main  # noqa: E402

SINK = Path(".emrg") / "emrg-client.log"

#: A frame payload as the library would render it, carrying a marker this test can search for.
FRAME_RECORD = '> TEXT \'{"type":"ask","text":"MARKER-THE-HOSTS-MESSAGE"}\''


@contextlib.contextmanager
def _root_handlers_cleared():
    """Let `logging.basicConfig` install a handler while this block runs.

    `basicConfig` is a no-op when the root logger already has handlers, and pytest's logging
    plugin puts one there for every test — so without this the sink under test would install
    nothing and report green while having written no file at all.
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


def test_the_frame_logger_is_pinned_the_way_the_daemons_sink_pins_it(tmp_path, monkeypatch):
    """`INFO` is the level, and the child loggers the library actually uses inherit it."""
    monkeypatch.chdir(tmp_path)
    with _websockets_level_restored() as logger:
        with _root_handlers_cleared():
            emrg_main._configure_client_logging()

        assert logger.level == logging.INFO, (
            f"the client's sink left `websockets` at {logging.getLevelName(logger.level)} — the "
            "daemon's sink pins it to INFO for the same library and the same reason"
        )
        # The library logs frames from a child logger, so the property is the child's
        # effective level — not the parent's attribute.
        frames = logging.getLogger("websockets.client")
        assert not frames.isEnabledFor(logging.DEBUG), (
            "a frame record would still be emitted — this is the leak, not a style question"
        )
        assert frames.isEnabledFor(logging.INFO), (
            "the connection lifecycle was dropped too: `connection open` is an INFO record"
        )


def test_a_frame_record_does_not_reach_the_client_log_but_the_lifecycle_one_does(
    tmp_path, monkeypatch
):
    """The end of the chain: what the client's file actually holds after the sink ran."""
    sink = tmp_path / SINK
    monkeypatch.chdir(tmp_path)

    with _websockets_level_restored():
        with _root_handlers_cleared():
            emrg_main._configure_client_logging()
            frames = logging.getLogger("websockets.client")
            frames.debug(FRAME_RECORD)
            frames.info("connection open")

    assert sink.exists(), "the sink wrote no file at all — the arm below would pass vacuously"
    written = sink.read_text(encoding="utf-8")
    assert "connection open" in written, (
        f"the INFO lifecycle record did not reach the log: {written!r}"
    )
    assert "MARKER-THE-HOSTS-MESSAGE" not in written, (
        "a frame payload was written to the client's log — measured 3129 such lines in the "
        f"host's own log on 2026-10-12: {written!r}"
    )
