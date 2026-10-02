"""One frame's failure is one row: the TUI's generic error row goes last.

The defect this pins (measured 2026-10-02): the TUI's frame dispatch carried a
generic reporter — `if "error" in data:` → `chat.add("system", f"Error: {err}")` —
that sat **above** the typed branches, and every one of those branches renders its
own failure (`Clear failed: …`, `Model switch failed: …`, `Install failed for
`x`: …`) and then `continue`s. So a failure answered by a typed reply printed
**two** rows: the branch's, which says what failed, and the generic one, which
says only the daemon's sentence. Nineteen typed replies reachable from the TUI
were doubled that way.

What the fix had to preserve is the reason the generic row exists at all: the
frames **no branch claims** — a turn's failure broadcast (`{request_id, error}`,
the only report of a turn that died) and the daemon's untyped direct replies
(`{"error": "unknown message type"}`, `{"error": "compact requires session_id and
cwd"}`). Dropping the row would have silenced exactly those.

So the invariant is a placement plus a habit, and both are read off the source:

1. every typed branch that renders the frame's error ends in `continue` — that is
   what keeps the frame from falling through into the generic row below it;
2. the generic row is **after** every such branch.

Why the source and not a frame: the dispatch lives inside `read_server()`'s read
loop, which needs a live socket and a terminal, so the branch bodies are not
reachable from a test. Same split, and the same reason, as
`test_app_replay_rows.py` and `test_app_resume_cwd.py`: what is testable is the
shape, not the keystroke.

No daemon is started, stopped or restarted by this file — it reads a source file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from emrg.client.app import the_failure_a_frame_reports

APP = Path(__file__).resolve().parent.parent / "emrg" / "client" / "app.py"

# The dispatch is the `try:` the read loop's own JSON guard closes: it is the one
# region of the file where `data` is a frame off the wire.
_DISPATCH_END = "except json.JSONDecodeError: pass"
_TRY = "try:"
_BRANCH = re.compile(r'^(\s+)if data\.get\("type"\) == "([a-z_]+)":')
# What a branch says when it renders the frame's failure itself.
_RENDERS_ERROR = ('data.get("error"', 'data["error"]')
# The generic reporter, found by the line it logs with rather than by position.
_GENERIC_LOG = 'logger.error("server error: %s", err)'


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _dispatch_region() -> tuple[list[str], int]:
    """The lines of the frame dispatch, and the 0-based index its `try` sits at."""
    lines = APP.read_text(encoding="utf-8").splitlines()
    end = next(
        i for i, ln in enumerate(lines) if ln.strip() == _DISPATCH_END
    )
    start = next(
        i for i in range(end, -1, -1) if lines[i].strip() == _TRY
    )
    return lines[start : end + 1], start


def _typed_branches() -> list[tuple[str, int, int, list[str]]]:
    """Each `data.get("type") == "x"` branch: (type, start, end, body).

    `end` is exclusive. A branch's body runs to the next *code* line at or above
    the `if`'s own indent; comments and blank lines may sit either side of that
    boundary, so only code lines close a branch.
    """
    lines, base = _dispatch_region()
    found: list[tuple[str, int, int]] = []
    for i, ln in enumerate(lines):
        m = _BRANCH.match(ln)
        if not m:
            continue
        indent = len(m.group(1))
        end = len(lines)
        for j in range(i + 1, len(lines)):
            if _indent(lines[j]) <= indent and lines[j].strip() and not lines[j].lstrip().startswith("#"):
                end = j
                break
        found.append((m.group(2), i, end))
    return [(t, base + s, base + e, lines[s:e]) for t, s, e in found]


def test_the_dispatch_has_typed_branches_to_check():
    """A guard over an empty set would pass for the wrong reason."""
    assert len(_typed_branches()) > 15


def test_every_branch_that_renders_the_frames_error_continues():
    """A branch that renders the failure and does not `continue` falls through.

    It would then print its own row *and* the generic one — the doubled row this
    file exists for — so this is the half of the rule that the placement cannot
    enforce by itself.
    """
    offenders = []
    for name, _start, _end, body in _typed_branches():
        if not any(marker in ln for ln in body for marker in _RENDERS_ERROR):
            continue
        last = next(ln.strip() for ln in reversed(body) if ln.strip() and not ln.lstrip().startswith("#"))
        if last != "continue":
            offenders.append(f"{name} ends with {last!r}, not 'continue'")
    assert not offenders, (
        "these branches render their frame's error and then let it travel on to the "
        "generic row below them, which prints the same failure a second time:\n  "
        + "\n  ".join(offenders)
    )


def test_the_generic_row_is_the_last_reader_of_a_frame():
    """The generic row must sit after every branch that renders an error itself.

    Above them, it fires for their frames too — which is the defect, and why this
    assertion is about position rather than about a helper's name.
    """
    lines, base = _dispatch_region()
    generic = [
        i for i, ln in enumerate(lines)
        if _GENERIC_LOG in ln
    ]
    assert len(generic) == 1, f"expected exactly one generic error row, found {len(generic)}"
    generic_at = base + generic[0]

    after = [
        (name, start)
        for name, start, _end, body in _typed_branches()
        if any(marker in ln for ln in body for marker in _RENDERS_ERROR) and start > generic_at
    ]
    assert not after, (
        "the generic error row is not the last reader of a frame: these branches "
        "render their own failure *below* it, so their frames are reported twice:\n  "
        + "\n  ".join(f"{name} (line {start})" for name, start in after)
    )


# ── the value half: an error is a value, not a key ──────────────────────────
#
# Every shape below is a frame this project really sends. `tool_end`'s is the one
# that names the rule: `ToolResult.error` is a `bool`, so `"error" in frame` is
# true for every successful tool call.


def test_a_successful_tool_call_reports_no_failure():
    """`tool_end` carries `error: False` on success — the key is not the failure."""
    assert the_failure_a_frame_reports(
        {"type": "tool_end", "request_id": "r1", "tool_name": "bash", "error": False}
    ) == ""


@pytest.mark.parametrize(
    "frame",
    [
        {},                              # no key at all
        {"error": None},                 # the github replies' "no error" value
        {"error": ""},                   # a clean `clear_result`
        {"error": 0},
    ],
)
def test_every_falsy_error_is_no_failure(frame):
    assert the_failure_a_frame_reports(frame) == ""


@pytest.mark.parametrize(
    "frame,expected",
    [
        # a turn's failure broadcast — untyped, so this row is its only report
        ({"request_id": "r1", "error": "Turn ended without reporting: Boom"},
         "Turn ended without reporting: Boom"),
        # the daemon's untyped direct replies
        ({"error": "unknown message type"}, "unknown message type"),
        ({"error": "compact requires session_id and cwd"}, "compact requires session_id and cwd"),
    ],
)
def test_a_reported_failure_keeps_its_sentence(frame, expected):
    assert the_failure_a_frame_reports(frame) == expected


def test_a_non_string_error_is_reported_rather_than_dropped():
    """A truthy value that is not text is still a failure, and still readable."""
    assert the_failure_a_frame_reports({"error": {"code": 500}}) == "{'code': 500}"
