"""The client's row-log windows are applied **after** the credential rule, never before it (#2164).

`ROWS after delta` and `ROWS after asst` render every chat row into one debug line, so both window
each row's content — and both used to take the window at the call site
(`f'{r.role}={r.content[:30]}'`). The rule then ran on the already-cut string, and every pattern in
`emrg/server/redaction.py::INLINE_SECRET_PATTERNS` has a minimum length (`sk-` 16 alphanumerics,
`ghp_` 20, a base64 run 40): a credential the cut lands inside stops matching and its surviving
prefix is written in the clear. Measured 2026-10-13 on master `daab42c6` — a key starting 13
characters in, windowed at 30, left `sk-A1b2C3d4E5F6G7` (17 characters of a live key) in the line.

A sink-level filter cannot rescue this: the slice is taken before the record exists, so a filter that
redacts `record.msg` and its string arguments sees text with nothing left to match. That is why the
order has to hold at the call site, and why `_rows_for_log` states it once instead of twice.

The two arms below are the two directions of the one order, and each is red for a *different*
mistake: the first for cutting before the rule, the second for dropping the window because the rule
became cheap to apply.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emrg.client.app import _rows_for_log  # noqa: E402
from emrg.client.python_tui import ChatRow  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A real shape for this rule: `sk-` plus 32 alphanumerics, the minimum-length family.
KEY = "sk-A1b2C3d4E5F6G7H8J9K0L1M2N3P4Q5R6"

#: The key begins at offset 13, so a 30-character window cuts it in half — which is exactly the
#: case `content[:30]` before the rule cannot see.
CROSSING = "please check " + KEY


def test_a_credential_crossing_the_cut_leaves_no_part_of_itself_behind():
    """The first direction: cut-then-redact is the defect, and the order under test kills it."""
    line = _rows_for_log([ChatRow(role="user", content=CROSSING)], 30)

    assert KEY not in line, f"the whole key survived — the rule never ran: {line!r}"
    assert "sk-" not in line, (
        f"the cut landed inside the key and the rule could not see what was left: {line!r}"
    )
    assert "***" in line, f"nothing was masked at all, so the hidden text is unchanged: {line!r}"
    # The benign half is still the benign half: this masks the credential, it does not blank the row.
    assert "please check" in line, f"the line lost the text around the credential: {line!r}"


def test_the_window_still_applies_to_a_row_with_nothing_to_hide():
    """The second direction: redacting first must not turn the window into a whole-content dump.

    `ROWS after delta` runs once per streamed delta and renders the user's row *and* the
    assistant's, so a "fix" that drops the cut writes the conversation into the file, per delta,
    at the volume the window exists to bound.
    """
    line = _rows_for_log([ChatRow(role="assistant", content="a" * 200)], 10)

    assert line == "assistant=" + "a" * 10, f"the window was not applied at the width given: {line!r}"


def test_the_width_is_the_argument_and_a_non_row_is_not_rendered():
    """`width` is the parameter, and the join keeps the shape the call sites used."""
    rows = [ChatRow(role="user", content="b" * 50), "not a row", ChatRow(role="system", content="c" * 50)]

    assert _rows_for_log(rows, 4) == "user=bbbb, system=cccc", _rows_for_log(rows, 4)


def test_the_row_log_sites_go_through_the_helper():
    """The seam: a correct helper that a call site no longer calls passes every arm above.

    The three arms before this one pin the helper's *order*, and each would stay green if a site
    went back to building its own window. So this pins the call — the thing that actually broke on
    the day — and it is deliberately narrow: it reads only the statements that *are* the row log,
    so a display site elsewhere that wants a window of its own is not swept in.

    The baseline is asserted first: two statements being read as "all of them use the helper" is
    only true while there is at least one to read.
    """
    src = (REPO_ROOT / "emrg" / "client" / "app.py").read_text(encoding="utf-8")
    statements = re.findall(r'logger\.debug\(\s*"ROWS after.*?\)\)', src, re.S)

    assert statements, "no `ROWS after` statement was found — this arm has no subject"
    for stmt in statements:
        assert "_rows_for_log(" in stmt, (
            "a row-log site builds its own window again, so the rule sits on the wrong side of "
            f"the cut: {stmt!r}"
        )
