"""A reconnect drops the questions that were in flight — issue #2390.

`read_server`'s frame loop holds three one-shot markers, each naming a question
it has sent the daemon and not yet had answered: `_rewind_replay` (a rewind's
redraw), `_replay_pending` (a resume's replay) and `_resume_pending_sid` (a
session's location). Each is matched by session id alone, so a marker that
outlives its answer is not inert — it claims the next answer that arrives for
that session. A `/rewind` would then have the rows its picker draws painted as a
rewind redraw: no selector, and a note about a cut that never happened.

The markers live in a closure built inside `read_server`, so what is testable
here is the shape of `_reconnect`: each marker must be *reassigned* in it and
*declared `nonlocal`* there. The second half is the one that matters — without
the declaration an assignment creates a local of the nested function, so the
reset is a silent no-op that reads correctly in the source. A test that grepped
only for `_rewind_replay = None` would stay green on exactly that defect.

No daemon is started, stopped or restarted by this file: it reads the source.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_PY = Path(__file__).resolve().parent.parent / "emrg" / "client" / "app.py"

MARKERS = ("_rewind_replay", "_replay_pending", "_resume_pending_sid")


def _reconnect() -> ast.AsyncFunctionDef:
    """The `_reconnect` coroutine defined inside `read_server`."""
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_reconnect"
    ]
    assert len(found) == 1, f"expected one _reconnect in app.py, found {len(found)}"
    return found[0]


def _declared_nonlocal(fn: ast.AST) -> set[str]:
    return {
        name
        for stmt in ast.walk(fn)
        if isinstance(stmt, ast.Nonlocal)
        for name in stmt.names
    }


def _assigned(fn: ast.AST) -> set[str]:
    """Names this function itself assigns, whatever the value."""
    return {
        target.id
        for stmt in ast.walk(fn)
        if isinstance(stmt, ast.Assign)
        for target in stmt.targets
        if isinstance(target, ast.Name)
    }


def _assigned_none(fn: ast.AST) -> set[str]:
    """Names this function itself assigns the constant `None`."""
    return {
        target.id
        for stmt in ast.walk(fn)
        if isinstance(stmt, ast.Assign)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is None
        for target in stmt.targets
        if isinstance(target, ast.Name)
    }


def test_the_extraction_finds_the_reconnect_coroutine():
    """The control: if this fails, the two tests below are reading nothing.

    `busy` is the marker `_reconnect` has always cleared; if it is missing from
    the body, the source moved and every assertion here is vacuous.
    """
    fn = _reconnect()

    assert "busy" in _declared_nonlocal(fn), (
        "_reconnect must still declare the closure names it rebinds"
    )
    assert "busy" in _assigned(fn), (
        "the extraction is not reading _reconnect's body: busy = False is gone"
    )


def test_each_pending_answer_marker_is_declared_nonlocal_in_reconnect():
    """Without the declaration the reset below is a no-op, not a reset.

    `_rewind_replay = None` inside a nested function that does not declare it
    creates that function's own local and leaves `read_server`'s marker set —
    the source reads as a fix and behaves as the defect.
    """
    declared = _declared_nonlocal(_reconnect())

    missing = [name for name in MARKERS if name not in declared]
    assert not missing, (
        "a reconnect loses these questions' answers, so their markers must be "
        f"reset in the frame loop's scope; not declared nonlocal: {missing}"
    )


def test_each_pending_answer_marker_is_cleared_by_reconnect():
    """The half the closure declaration exists for: the reset itself."""
    cleared = _assigned_none(_reconnect())

    missing = [name for name in MARKERS if name not in cleared]
    assert not missing, (
        "a marker left set takes the next answer for its session, so a fresh "
        f"/rewind's picker never opens (issue #2390); not cleared: {missing}"
    )
