"""The client's log sink masks credentials in everything it writes (issue #2156).

The client prints the host's own text on purpose: the submitted message (``SUBMIT``), the
input buffer (``ENTER``), the head of the stream coming back (``DONE: stream_buffer``) and the
chat rows on every delta (``ROWS after delta``, run once per frame). `emrgd.log`'s sibling issue
(#2150/#2151) closed the daemon's half of this surface; the client's own log is the other half,
in the same directory, read the same way — and the rule's origin (rant ``2026-08-06T10:21:26``)
is a credential **typed into the TUI**, which is exactly what these records hold.

Measured 2026-10-12 on the host's own ``~/.emrg/emrg-client.log`` (mode 0644, 6363 lines), by
count only: 4 ``ENTER`` records, 4 ``SUBMIT``, 5 ``DONE``, **3101** ``ROWS after delta`` and 4
``ROWS after asst`` — the last-typed message and the head of the answer, 3118 records in all.

The fix is at the **sink**, not at those five sites: the rule's single home is
`emrg/server/redaction.py` (#2151) and a site added tomorrow is covered by construction. So
these arms drive the real `_configure_client_logging`, emit through the real root logger and
read the **file** — a source-shape assertion would pass on a sink whose filter was never
installed, which is the defect itself.

The controls matter here as much as the arms: a sink that redacted *everything*, or that
installed a handler which wrote nothing, would pass every credential arm and be useless. So one
arm asserts the record is still written and one asserts a benign message is unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg import __main__ as emrg_main  # noqa: E402

SINK = Path(".emrg") / "emrg-client.log"

#: A shape the rule knows: an inline `sk-` key, spelled the way the rule's origin spells it.
SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
BENIGN = "a plain sentence with no credential in it at all"


@contextlib.contextmanager
def _root_handlers_cleared():
    """Let `logging.basicConfig` install a handler while this block runs.

    `basicConfig` is a no-op when the root logger already has handlers, and pytest's logging
    plugin puts one there for every test — so without this the sink under test would install
    nothing and every credential arm below would pass *vacuously*, against an empty file.
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


def _sink_lines(tmp_path) -> list[str]:
    """Run the real sink in `tmp_path`, emit the client's own record shapes, return the lines."""
    monkeypatch_cwd = tmp_path
    with _root_handlers_cleared():
        emrg_main._configure_client_logging()
        log = logging.getLogger("emrg.client.app")
        # The five shapes `emrg/client/app.py` writes, with a credential where the host's own
        # text would be: `%r` and `%s` on one argument, a bare `%s`, `%d` plus a joined string,
        # and a plain message.
        log.debug("ENTER: text=%r busy=%s len=%d", f"export OPENAI_API_KEY={SECRET}", False, 61)
        log.debug("SUBMIT: text=%r", f"export OPENAI_API_KEY={SECRET}")
        log.debug("DONE: stream_buffer=%r", f"the answer mentions {SECRET} once")
        log.debug("ROWS after delta: %d [%s]", 3, f"user=export OPENAI_API_KEY={SECRET}, assistant=ok")
        log.debug("ROWS after asst: %d [%s]", 3, f"assistant=ok")
        log.debug("plain: %s", BENIGN)
    return (monkeypatch_cwd / SINK).read_text(encoding="utf-8").splitlines()


def test_no_client_record_carries_the_credential_into_the_file(tmp_path, monkeypatch):
    """Every shape the client writes is masked, and the key never reaches the file."""
    monkeypatch.chdir(tmp_path)
    lines = _sink_lines(tmp_path)
    written = "\n".join(lines)

    assert SECRET not in written, (
        "a credential reached the client's log file — this is the leak the issue measured "
        f"on the host's own log: {written!r}"
    )
    for marker in ("ENTER: text=", "SUBMIT: text=", "DONE: stream_buffer=", "ROWS after delta"):
        assert marker in written, (
            f"the {marker!r} record was not written at all, so its arm above proves nothing: "
            f"{written!r}"
        )


def test_the_records_are_still_written_and_a_benign_message_is_untouched(tmp_path, monkeypatch):
    """The control: masking must not become silencing, nor over-redaction.

    A sink that dropped its records, or that masked the whole message rather than the
    credential inside it, would pass the arm above while destroying the log's usefulness —
    which is the whole reason these records exist.
    """
    monkeypatch.chdir(tmp_path)
    written = "\n".join(_sink_lines(tmp_path))

    assert f"plain: {BENIGN}" in written, (
        f"a message with no credential in it was altered or dropped: {written!r}"
    )
    assert "export OPENAI_" in written, (
        "the surrounding text was not kept beside the mask — the record must still say what "
        f"happened, only not the credential: {written!r}"
    )
    assert "***" in written, (
        f"nothing was masked, so the arm above passed for the wrong reason: {written!r}"
    )
    assert "ROWS after delta" in written and "assistant=ok" in written, (
        f"the per-delta rows record lost its useful part: {written!r}"
    )
