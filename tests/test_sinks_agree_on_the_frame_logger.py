"""The two log sinks that state the frame-logger rule must **agree** about it (#2170).

`websockets` logs every frame at `DEBUG` with the payload inside the record, so both sinks pin
that logger: `emrg/server/__main__.py::_configure_logging` (the rule's origin) and
`emrg/__main__.py::_configure_client_logging` (taught to mirror it by #2157/#2158). Each side
now has its own arm — `tests/test_client_log_frame_noise.py` on master, and the daemon's
`tests/test_server_log_frame_noise.py` being added by #2163 — and neither reads the other,
deliberately: each drives its own sink and reads its own file.

That is exactly the shape this file exists for. Two carriers of one rule, with an arm on each,
prove nothing about the **pair**: each arm is written against the carrier its author was looking
at, so a level that drifts *within* the acceptable set on one side reddens nothing on the other,
and the two can drift apart for a whole release without a single red row. This is the class the
project has hit four times in two days (config file mode, argv redaction, the redaction rule, and
this level), and the cure is a test that reads both carriers and **compares** them.

Why the reading is taken the way it is
--------------------------------------
* The sinks set a **global** level on `logging.getLogger("websockets")`, so the second call
  would overwrite the first's effect and "they agree" would be true by construction. Each sink
  is therefore driven, its reading **recorded**, and the global state restored before the next
  one runs — the comparison is between two recorded readings, never between two live states.
* The reading is the **effective** level of `websockets.client`, the child logger the library
  actually logs frames from, because that is the level a record is tested against — the parent's
  attribute alone is not the property (`websockets` is where the sinks set it; the library logs
  through the child).
* The pair's reading is also asserted against the rule itself, so the two cannot drift
  **together** into a state that is consistent and wrong. That half deliberately duplicates the
  per-carrier files: they hold each side's correctness, this one holds the agreement, and a
  change that moves the shared level has to move all three.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg import __main__ as emrg_main  # noqa: E402
from emrg.server import __main__ as server_main  # noqa: E402

#: The child logger `websockets` logs frames from — the level a frame record is tested against.
FRAME_LOGGER = "websockets.client"

#: Loggers the two sinks set or suppress, recorded and put back so this file leaves no trace
#: of itself in the session's logging state (`websockets` is the rule; the rest are noise
#: suppression the sinks also perform).
TOUCHED = ("websockets", FRAME_LOGGER, "httpcore", "httpx", "markdown_it")


@contextlib.contextmanager
def _reading_after(drive):
    """Drive one sink, yield the effective frame-logger level, then restore everything.

    `basicConfig` is a no-op when the root logger already has handlers — pytest's logging
    plugin puts one there for every test — so the root is cleared while the sink runs, and its
    handlers are closed on the way out. Every logger the sinks touch is recorded and restored:
    a global that a test sets and does not put back is how one file reddens another.
    """
    saved_levels = {name: logging.getLogger(name).level for name in TOUCHED}
    saved_handlers = logging.root.handlers[:]
    saved_root_level = logging.root.level
    logging.root.handlers = []
    try:
        drive()
        # Read **before** anything is restored: this is the state the sink left behind. Two
        # numbers, because they answer two questions: the parent's own attribute says whether
        # the carrier *states* a level at all (`NOTSET` = it states nothing), and the child's
        # effective level is what a frame record is tested against.
        stated = logging.getLogger(FRAME_LOGGER).parent.level
        yield stated, logging.getLogger(FRAME_LOGGER).getEffectiveLevel()
    finally:
        for handler in logging.root.handlers:
            with contextlib.suppress(Exception):
                handler.close()
        logging.root.handlers = saved_handlers
        logging.root.level = saved_root_level
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)


def _readings(tmp_path, monkeypatch) -> dict[str, tuple[int, int]]:
    """What each sink states, and where it leaves the frame logger, measured one at a time.

    The client's sink derives its path from the **cwd**, so this runs inside `tmp_path`; the
    daemon's derives its own from the home directory, so `HOME` (and Windows' `USERPROFILE`)
    are pointed at the same temporary root. Neither write is asserted here — the per-carrier
    files read the files they emit; this file is about the level the two agree on.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    readings = {}
    with _reading_after(server_main._configure_logging) as daemon:
        readings["daemon"] = daemon
    with _reading_after(emrg_main._configure_client_logging) as client:
        readings["client"] = client
    return readings


def test_the_two_sinks_hold_the_frame_logger_at_the_same_level(tmp_path, monkeypatch):
    """The pair, compared — the property neither per-carrier arm can see."""
    readings = _readings(tmp_path, monkeypatch)

    assert set(readings) == {"daemon", "client"}, (
        f"a carrier was not measured at all, so 'they agree' would prove nothing: {readings}"
    )
    # Vacuity guard, and the reason the reading carries two numbers: a sink that stopped
    # *stating* a level leaves the logger inherited, and two inherited loggers agree about
    # nothing. `NOTSET` is that shape — the parent states nothing.
    for carrier, (stated, _effective) in readings.items():
        assert stated != logging.NOTSET, (
            f"the {carrier} sink states no frame-logger level at all — it leaves the logger "
            "inherited, and two carriers that state nothing trivially agree"
        )

    assert readings["daemon"][1] == readings["client"][1], (
        "the two sinks disagree about the frame-logger level: "
        f"daemon={logging.getLevelName(readings['daemon'][1])}, "
        f"client={logging.getLevelName(readings['client'][1])}. The rule is one rule about one "
        "library; the sink whose name is on this row is the one that drifted."
    )


def test_the_agreed_level_is_the_rule_both_comments_state(tmp_path, monkeypatch):
    """The other direction: the pair must not agree on something wrong.

    `INFO` is the trade the rule's origin makes — frame records are dropped, `connection open`
    / `connection closed` survive — so a reading that a record is tested against has to be
    **above** `DEBUG` (frames out) and **at or below** `INFO` (lifecycle in). Asserted from the
    recorded numbers, because `_readings` puts the global state back on its way out and a
    `isEnabledFor` asked afterwards would be asking about the restored logger, not the sinks'.
    """
    readings = _readings(tmp_path, monkeypatch)
    names = ", ".join(f"{k}={logging.getLevelName(v[1])}" for k, v in sorted(readings.items()))

    for carrier, (_stated, level) in readings.items():
        assert level > logging.DEBUG, (
            f"the {carrier} sink holds the frame logger at {logging.getLevelName(level)} "
            f"({names}), so frames reach its file — the ~90 % of those logs the rule exists to "
            "keep out, with the conversation inside each line"
        )
        assert level <= logging.INFO, (
            f"the {carrier} sink holds the frame logger above INFO ({names}), which drops the "
            "connection lifecycle as well: `connection open` is an INFO record, and that is "
            "the information the rule keeps"
        )
