"""The client's DONE log line redacts the whole buffer, then windows it (issue #2166).

`emrg/client/app.py` writes the head of the streamed response to `.emrg/emrg-client.log` on
every completed turn. It used to take the window first — `stream_buffer[:80]` — and hand the
already-cut string to the log record, where the credential rule cannot see it: the rule's
patterns have minimum lengths (`sk-` needs 16 alphanumerics, `ghp_` 20, a base64 run 40), so a
credential the cut lands inside stops matching and its surviving prefix is written in the
clear. Measured 2026-10-13 on master `579a24ca`: a key starting at offset 70 of the buffer,
windowed at 80, left `sk-A1b2C3d` — 9 characters of a live key — in the line.

The handler's own filter does not cover this record either (see the sibling work on #2159): a
slice is taken *before* the record exists, so the filter receives `record.args` already cut
and has nothing left to match. The order is therefore the call site's, and that is what these
arms read.

The behavioural arms drive the named helper the site calls; the third arm reads the call site
itself, because a helper nothing routes through is green and useless.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.client.app import _redacted_window  # noqa: E402

APP = Path(__file__).resolve().parent.parent / "emrg" / "client" / "app.py"

#: A live-looking credential: `sk-` plus 34 alphanumerics, well past the rule's 16 minimum.
SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

WIDTH = 80


def test_a_credential_the_window_lands_inside_does_not_survive():
    """The defect's own offset: the key starts at 70, the window ends at 80."""
    buffer = "x" * 70 + SECRET

    windowed = _redacted_window(buffer, WIDTH)

    assert "sk-A1b2C3d" not in windowed, (
        f"the window cut the key and the rule could no longer see it: {windowed!r}"
    )
    assert SECRET not in windowed
    assert "***" in windowed, f"nothing was masked at all: {windowed!r}"
    assert len(windowed) <= WIDTH


def test_a_credential_entirely_inside_the_window_is_masked_too():
    """The easy case still has to hold — the fix is not a reordering that loses it."""
    buffer = "the answer mentions " + SECRET + " once"

    windowed = _redacted_window(buffer, WIDTH)

    assert SECRET not in windowed
    assert "***" in windowed


def test_a_benign_buffer_is_windowed_unchanged():
    """The control: a helper that emptied or rewrote every line would pass the arms above."""
    buffer = "a" * 200

    assert _redacted_window(buffer, WIDTH) == "a" * WIDTH


def test_a_short_benign_buffer_is_returned_whole():
    """`limit` is "at most", so a value under the window is not padded or dropped."""
    assert _redacted_window("done", WIDTH) == "done"


def test_the_done_call_site_routes_the_whole_buffer_through_the_helper():
    """A helper nothing calls guards nothing: assert the call, at the site it guards."""
    site = [
        line
        for line in APP.read_text(encoding="utf-8").splitlines()
        if '"DONE: stream_buffer=%r"' in line
    ]

    assert len(site) == 1, f"expected exactly one DONE log site, found {len(site)}"
    assert "_redacted_window(stream_buffer, 80)" in site[0], (
        f"the DONE record no longer hands the buffer to the helper: {site[0]!r}"
    )
    assert "stream_buffer[:" not in site[0], (
        f"the window is taken before the rule again: {site[0]!r}"
    )
