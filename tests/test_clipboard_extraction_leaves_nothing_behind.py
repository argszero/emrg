"""A clipboard image that cannot be extracted leaves nothing behind — and says so.

Both halves of this file come from one report and one measurement.

**The litter.** Every branch of `_extract_clipboard_image` opens its target *before*
the data arrives, so an attempt that fails still leaves a file. Measured 2026-10-08
(cycle `cyc20261008-150345`) with the AppleScript path's own two steps — `open for
access (POSIX file …) with write permission`, then a `write` that fails — against a
scratch path: the file exists afterwards and holds **0 bytes**. In the session's
`images/` directory that is a `_clipboard_tmp_N.png` nobody can see or explain: no
placeholder is inserted, so the reader cannot tell it apart from a paste that
carried nothing.

**The silence.** The paste path called `_extract_clipboard_image` and simply fell
through when it answered `False` — no message, no log line. `/image` had said
`无法从剪贴板提取图片。` all along. So the same failure was visible on one path and
invisible on the other, and the invisible one is the path the original report came
from ("GUI 输入框里 Cmd+V，现在没有任何效果", rant 2026-09-30T09:35:04).

What is asserted here, and its limit
------------------------------------
The two decisions are asserted directly — the verdict/cleanup helper is driven
against a scratch file it did not create, and the report helper is driven with a
recording stand-in for the chat — so neither needs a clipboard, a terminal or a
platform.

The *wiring* is asserted as source shape, and that limit is named rather than
hidden: the three platform branches and the two call sites live inside
`handle_key`, which needs a terminal and a socket, so nothing here proves a real
paste reaches them. What it pins is that every branch asks the same verdict
function and that both paths report a failure — the shapes whose absence was the
defect. A green run of this file is a statement about how those lines are written.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from emrg.client import app as client_app

APP_SOURCE = (Path(client_app.__file__)).read_text(encoding="utf-8")

#: The one wording the TUI uses for "an image was advertised and none could be taken".
#: The report helper owns it; a second copy anywhere is how the two paths drifted apart.
REFUSAL_TEXT = "无法从剪贴板提取图片。"


# ── the verdict and the cleanup ───────────────────────────────────────────────


def test_an_empty_target_is_removed_and_reads_as_a_failure(tmp_path) -> None:
    """The measured shape: the attempt created the file and wrote nothing into it.

    A 0-byte file is not an image. Reading it as a failure is not enough on its own —
    it has to be removed, or the failed paste leaves litter in the session directory
    that outlives the turn.
    """
    target = tmp_path / "_clipboard_tmp_1.png"
    target.write_bytes(b"")

    assert client_app._clipboard_extraction_verdict(str(target)) is False
    assert not target.exists(), (
        "an extraction that produced no bytes left its target behind — the 0-byte "
        "`_clipboard_tmp_N.png` litter the AppleScript path creates before it can fail"
    )


def test_an_image_is_kept(tmp_path) -> None:
    """The control: a verdict that removed a real image would be the worse defect."""
    target = tmp_path / "_clipboard_tmp_2.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"not-really-a-png-but-not-empty"
    target.write_bytes(payload)

    assert client_app._clipboard_extraction_verdict(str(target)) is True
    assert target.read_bytes() == payload


def test_a_target_that_was_never_created_is_a_failure(tmp_path) -> None:
    """The other failure shape: the branch never got as far as creating the file."""
    missing = tmp_path / "never-written.png"
    assert client_app._clipboard_extraction_verdict(str(missing)) is False
    assert not missing.exists()


# ── the report ────────────────────────────────────────────────────────────────


class _RecordingChat:
    """The `chat` the TUI hands in, reduced to what this module uses."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def add(self, kind: str, text: str) -> None:
        self.lines.append((kind, text))


def test_the_report_is_visible_to_the_reader_and_in_the_log(tmp_path, caplog) -> None:
    """Both halves of "visible": the chat line a reader sees, and the log line a host reads.

    The log half is not decoration — the whole report is that the reader could not
    tell a dropped image from an empty paste, and `emrg-client.log` is where the
    host-side trace of that lives.
    """
    chat = _RecordingChat()
    with caplog.at_level(logging.WARNING, logger=client_app.logger.name):
        client_app._report_clipboard_extraction_failure(chat, str(tmp_path / "x.png"))

    assert chat.lines == [("system", REFUSAL_TEXT)], (
        f"the reader was not told the clipboard image could not be taken: {chat.lines!r}"
    )
    assert any(
        rec.levelno == logging.WARNING and "x.png" in rec.getMessage()
        for rec in caplog.records
    ), f"no warning naming the target reached the log: {[r.getMessage() for r in caplog.records]!r}"


# ── the wiring (source shape, limit named in the module docstring) ────────────


def _branch_bodies(source: str) -> dict[str, str]:
    """The text of the three platform branches of `_extract_clipboard_image`."""
    start = source.index("def _extract_clipboard_image(")
    end = source.index("\nclass _StderrContainment", start)
    body = source[start:end]
    out = {}
    for name in ("Darwin", "Linux", "Windows"):
        marker = f'if system == "{name}":' if name == "Darwin" else f'elif system == "{name}":'
        assert marker in body, f"the {name} branch is gone from _extract_clipboard_image"
        rest = body[body.index(marker) + len(marker):]
        for nxt in ('if system == "', 'elif system == "', "except Exception"):
            if nxt in rest:
                rest = rest[: rest.index(nxt)]
        out[name] = rest
    return out


@pytest.mark.parametrize("system", ["Darwin", "Linux", "Windows"])
def test_every_platform_branch_asks_the_verdict(system: str) -> None:
    """Each branch decides through the one function that also removes the litter.

    A branch that goes back to testing the file for itself — `path.exists() and
    path.stat().st_size > 0` — reintroduces the 0-byte file on that platform only,
    which is the kind of defect a single-platform host never sees.
    """
    branch = _branch_bodies(APP_SOURCE)[system]
    assert "_clipboard_extraction_verdict(target_path)" in branch, (
        f"the {system} branch decides by itself instead of through the verdict helper, "
        f"so a failed extraction there leaves its target behind: {branch!r}"
    )


def test_both_clipboard_paths_report_a_failure() -> None:
    """A paste and a `/image` token must report the same failure the same way.

    The defect was one path reporting and the other returning in silence, so the
    count is the assertion: exactly two call sites, one per path.
    """
    calls = [
        m.start()
        # `(?<!def )` keeps the helper's own `def` line out of the count: the question
        # is how many *call sites* exist, and the definition is not one.
        for m in re.finditer(r"(?<!def )_report_clipboard_extraction_failure\(chat, ", APP_SOURCE)
    ]
    assert len(calls) == 2, (
        f"expected one failure report per clipboard path (a paste and a `/image` token), "
        f"found {len(calls)}"
    )


def test_the_refusal_has_one_wording() -> None:
    """One copy of the text, in the helper — a second is how the paths drifted."""
    assert APP_SOURCE.count(REFUSAL_TEXT) == 1, (
        "the refusal text appears more than once, so the two paths can diverge again"
    )
