"""Tests for the daemon log [session] context column (rant 2026-08-24T10:48:50).

The production daemon (``python -m emrg.server``) renders emrgd.log lines as
``时间 [LEVEL] [task] [session] logger: message``. The [session] label comes
from the ``emrg.server.logcontext`` ContextVar, set around each message's
tool loop (daemon task-received branch). These tests cover the context
plumbing and the formatter rendering; importing ``emrg.server.__main__`` is
side-effect free (the entry point is guarded by ``if __name__ == "__main__"``
and the file/stream handlers are only created in ``main()``).
"""

from __future__ import annotations

import logging

from emrg.server import logcontext
from emrg.server.logcontext import session_label, session_label_for


def test_session_label_defaults_empty():
    """Outside any task context the label is empty (formatter renders '-')."""
    assert session_label.get() == ""


def test_session_label_push_and_reset():
    """push_session_label sets the current task's label; reset restores it."""
    token = logcontext.push_session_label("s_240824_1234:my-title")
    try:
        assert session_label.get() == "s_240824_1234:my-title"
    finally:
        session_label.reset(token)
    assert session_label.get() == ""


def test_session_label_for_id_and_name():
    """session_label_for: named session → id:name; unnamed → id only."""
    assert session_label_for("s_1", "journal draft") == "s_1:journal draft"
    assert session_label_for("s_1", "s_1") == "s_1"
    assert session_label_for("s_1", "") == "s_1"


def _render(label: str) -> tuple[str, logging.LogRecord]:
    """Render one log line through the real daemon formatter with the given
    session label set on the current task context."""
    from emrg.server.__main__ import _TaskColumnFormatter

    fmt = _TaskColumnFormatter(
        "%(levelname)s [%(task)s] [%(session)s] %(message)s"
    )
    record = logging.LogRecord(
        name="emrg.server.daemon",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    token = session_label.set(label)
    try:
        return fmt.format(record), record
    finally:
        session_label.reset(token)


def test_formatter_renders_session_column():
    """[session] column renders the ContextVar label; task stays independent;
    missing context falls back to '-'."""
    out, record = _render("s_1:my title")
    assert out == "INFO [-] [s_1:my title] hello"
    assert record.session == "s_1:my title"
    assert record.task == "-"

    out, record = _render("")
    assert out == "INFO [-] [-] hello"
    assert record.session == "-"
