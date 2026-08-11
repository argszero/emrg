"""Unit tests for the TUI status line widget (rant 2026-08-11T20:02:43).

Layout after the reorg: left = session + model + elapsed + msg-count + dir
(bold magenta), center = server id + host only (dim), no right section.
"""

from __future__ import annotations

from emrg.client.python_tui.widgets.base import RenderContext
from emrg.client.python_tui.widgets.status_line import StatusLine


def _spans(status: StatusLine, width: int = 100) -> list[dict]:
    lines = status.render(RenderContext(width=width))
    assert len(lines) == 1
    return [{"text": s.text, "style": str(s.style)} for s in lines[0].spans]


def test_left_contains_session_model_elapsed_extra():
    """All core info lands in the left (bold magenta) section."""
    status = StatusLine(
        left="emrg-main (s_260727) [deepseek-v4-flash]",
        center="emrg-5fa @ host",
    )
    status.elapsed = "[1:23]"
    status.left_extra = "· 3 msgs · ~/proj"
    spans = _spans(status)

    left = spans[0]
    assert left["text"] == " emrg-main (s_260727) [deepseek-v4-flash] [1:23] · 3 msgs · ~/proj"
    assert "bold magenta" in left["style"]

    # Center is the server id + host only
    assert spans[1]["text"].strip() == "emrg-5fa @ host"
    assert "dim" in spans[1]["style"]


def test_no_right_section():
    """No right section is rendered after the reorg."""
    status = StatusLine(left="emrg (sid12345) [m]", center="ab12 @ h")
    spans = _spans(status)
    # Only two spans max: left + center
    assert len(spans) <= 2
    texts = "".join(s["text"] for s in spans)
    assert "tk" not in texts  # old token counter removed
    assert not any(s["text"].endswith("msgs") or "msgs" in s["text"].split(" ") and s is spans[-1] for s in spans)


def test_elapsed_updates_dirty_and_renders_left():
    """Setting elapsed marks dirty and appends to left, not center."""
    status = StatusLine(left="emrg (sid)", center="ab @ h")
    assert status.dirty is True
    status.render(RenderContext(width=80))
    assert status.dirty is False

    status.elapsed = "[0:45]"
    assert status.dirty is True
    spans = _spans(status)
    assert "[0:45]" in spans[0]["text"]
    assert "[0:45]" not in spans[1]["text"]  # not in center


def test_elapsed_and_extra_empty_when_idle():
    """Idle state: no elapsed, left shows session + hint only."""
    status = StatusLine(left="emrg (sid) [model]", center="ab @ h")
    status.left_extra = "Enter=send  Esc=quit  /help"
    spans = _spans(status)
    assert "[0" not in spans[0]["text"]
    assert "Enter=send" in spans[0]["text"]
