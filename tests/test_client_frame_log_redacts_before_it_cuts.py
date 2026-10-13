"""The client's bad-JSON-frame warning applies the credential rule, then takes its window.

`DaemonConnection.recv` reports a frame it could not parse with
``logger.warning("bad JSON frame ignored: %.120r", text[:120])`` — the window at the call
site, so the rule the client's log sink applies (`emrg/__main__.py::_RedactClientLogRecords`,
#2159) only ever sees the cut text. Every pattern in `emrg/server/redaction.py` has a minimum
length (`sk-` 16 alphanumerics, `ghp_` 20, a base64 run 40), so a credential the cut lands
inside stops matching and its surviving prefix is written into
``<cwd>/.emrg/emrg-client.log`` in the clear.

A sink cannot rescue the order: the slice is taken *before* the record is built, so the
filter receives text with nothing left to match — which is why the fix is at the site and
why the assertion below is about the string the record carries rather than about the file.

The two arms are the two directions of the one order, and each is red for a *different*
mistake: the first for cutting before the rule, the second for dropping the window.

No client is started here: `DaemonConnection` wraps whatever ws-like object it is handed,
so the frame arrives through the real `recv` with a stand-in socket.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.client.daemon_manager import DaemonConnection  # noqa: E402

LOGGER = "emrg.client.daemon_manager"

#: A real shape for this rule: `sk-` plus 32 alphanumerics, the minimum-length family.
KEY = "sk-A1b2C3d4E5F6G7H8J9K0L1M2N3P4Q5R6"

#: The key begins at offset 110, so a 120-character window keeps exactly 10 characters of it
#: — below every pattern's minimum length, which is the case `text[:120]` before the rule
#: cannot see.
CROSSING = "z" * 110 + KEY + '", "rest": "of a frame that never parses'


class _FakeWS:
    """The one method `recv` uses: `recv()` returning the frame under test."""

    def __init__(self, frame: str):
        self._frame = frame

    async def recv(self):
        return self._frame

    async def close(self):
        pass


def _recv(frame: str):
    return asyncio.run(DaemonConnection(_FakeWS(frame)).recv(timeout=1))


def _frame_records(caplog):
    return [r for r in caplog.records if r.name == LOGGER]


def test_a_credential_crossing_the_window_leaves_no_part_of_itself_behind(caplog):
    """The defect and its fix: cut-then-redact is what leaked, and the order under test kills it."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    assert _recv(CROSSING) is None, "a bad frame must still be reported as no frame"

    records = _frame_records(caplog)
    assert records, "the bad frame was not reported at all — the arm would pass vacuously"
    message = records[0].getMessage()
    assert "bad JSON frame ignored" in message, f"not the frame warning: {message!r}"
    assert "sk-" not in message, (
        f"the cut landed inside the key and the rule could not see what was left: {message!r}"
    )
    assert "***" in message, (
        f"nothing was masked, so the frame's own text is the text that was logged: {message!r}"
    )
    # The benign half survives: this masks the credential, it does not blank the frame.
    assert "zzzz" in message, f"the frame's own text is gone: {message!r}"


def test_the_window_is_still_a_window(caplog):
    """The other direction: the rule is cheap to run now, and the cut must not be dropped."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    long_benign = "not json at all — " + "b" * 2000

    assert _recv(long_benign) is None

    records = _frame_records(caplog)
    assert records, "the bad frame was not reported at all"
    message = records[0].getMessage()
    assert "not json at all" in message, f"the frame's head is missing: {message!r}"
    # `%.120r` bounds the value, and the prefix bounds the record: a site that logged the
    # whole frame would write the conversation into the client's log once per bad frame.
    assert len(message) <= 160, f"the window is gone — the whole frame was logged: {len(message)}"
