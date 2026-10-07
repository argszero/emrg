"""The tool-card header renders a call whatever shape its fields arrived in.

Measured 2026-10-04 on `2fda2d15`: `_format_args` read its fields as if the model had
sent the types the tool schema declares. It had not — `_parse_arguments` guarantees the
*container* is a dict and nothing guarantees a field — so six shapes raised out of it:

    {"file_path": "a.txt", "content": null}   TypeError: object of type 'NoneType' has no len()
    {"file_path": "a.txt", "content": 123}    TypeError: object of type 'int' has no len()
    {"file_path": {"a": 1}, "content": "hi"}  TypeError: argument should be a str or an os.PathLike ...
    {"file_path": 123, "content": "hi"}       TypeError: argument should be a str or an os.PathLike ...
    {"file_path": "a.txt", "start_line": "abc", "line_limit": 3}   ValueError: invalid literal for int()
    {"command": 123}                          AttributeError: 'int' object has no attribute 'split'

`_format_args` is not on a happy path. It runs while building the cards for a resumed
session (`_cards_from_tool_calls`) and on every `tool_start` whose `intent` is empty, so
one such call in a session's history made that session un-resumable — and the contract
the raise broke is the one `_cards_from_tool_calls` states in its own docstring: a call
that cannot be read still gets a card, because dropping it puts a resumed session back
on the flat-line path. So the header is exactly where a malformed call must stay
*visible*; killing the renderer is the one outcome it must not have.

The fix is one rule with two spellings — `_display_text` for a field that is rendered,
`_display_int` for one that is used as a number — and the arms below run in both
directions: every listed shape renders, and every listed shape is one the *unguarded*
read cannot survive.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "emrg" / "client" / "app.py"

sys.path.insert(0, str(REPO))

from emrg.client.app import (  # noqa: E402
    _cards_from_tool_calls,
    _edit_shows_a_diff,
    _format_args,
    _write_call_summary,
)

#: Every function that reads a tool call's fields to render them. The AST arm below checks
#: all of them, so a reader added without the guard fails here rather than in a session.
#: `_edit_shows_a_diff` and `_write_call_summary` are the `tool_end` half — before
#: 2026-10-07 they were inline in the event loop and read raw, which is exactly the shape
#: a rule that names one function cannot see.
_SHAPE_GUARDED_READERS = ("_format_args", "_edit_shows_a_diff", "_write_call_summary")


#: `(what the model sent, is the shape one the old read died on)` — the first element is
#: the field the header reads, the second the shape. Kept as data so the "renders" leg and
#: the "used to raise" leg are driven by the *same* list rather than by two copies that
#: can drift apart.
_CRASHING_SHAPES: list[tuple[str, dict, str]] = [
    ("write with a null content", {"file_path": "/a/b.txt", "content": None, "intent": "x"}, "write"),
    ("write with a numeric content", {"file_path": "/a/b.txt", "content": 123, "intent": "x"}, "write"),
    ("write with a mapping file_path", {"file_path": {"a": 1}, "content": "hi", "intent": "x"}, "write"),
    ("write with a numeric file_path", {"file_path": 123, "content": "hi", "intent": "x"}, "write"),
    (
        "read with a non-numeric start_line and a line_limit",
        {"file_path": "/a/b.txt", "start_line": "abc", "line_limit": 3, "intent": "x"},
        "read",
    ),
    ("bash with a numeric command", {"command": 123, "intent": "x"}, "bash"),
    (
        "bash with a long command and a mapping workdir",
        {"command": "x" * 80, "workdir": {f"k{i}": i for i in range(25)}, "intent": "x"},
        "bash",
    ),
]


@pytest.mark.parametrize(
    "label,args,tool_name",
    _CRASHING_SHAPES,
    ids=[shape[0] for shape in _CRASHING_SHAPES],
)
def test_the_header_renders_a_call_whatever_shape_its_fields_arrived_in(
    label: str, args: dict, tool_name: str
) -> None:
    """The measured defect: a `str` comes back, and the call stays visible.

    Asserted as "does not raise" *and* "is a string", because a header that answered
    `None` would satisfy a bare no-raise check while rendering nothing at all — which is
    the same loss of the call by a quieter route.
    """
    rendered = _format_args(dict(args), tool_name)
    assert isinstance(rendered, str), (
        f"{label}: the header answered {rendered!r}, not a string — the call is rendered "
        "as nothing, which loses it just as the raise did"
    )


def test_a_resumed_session_keeps_the_card_for_a_call_it_cannot_read() -> None:
    """The entry point that makes this fatal, and the contract it broke.

    `_cards_from_tool_calls` builds one card per call in a stored assistant record. Its
    docstring names the failure it exists to prevent — "dropping it would put a resumed
    session back on the flat-line path" — so the arm runs on the *real* builder with a
    record shaped the way the daemon stores one (`function.arguments` is the provider's
    JSON string), rather than on `_format_args` directly.
    """
    for label, args, tool_name in _CRASHING_SHAPES:
        record = {
            "id": "call-1",
            "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(args)},
        }
        cards = _cards_from_tool_calls([record])
        assert len(cards) == 1, f"{label}: the call was dropped instead of carded"
        assert isinstance(cards[0].command, str)
        assert cards[0].name == tool_name


@pytest.mark.parametrize(
    "label,args,tool_name",
    _CRASHING_SHAPES,
    ids=[shape[0] for shape in _CRASHING_SHAPES],
)
def test_each_shape_is_one_the_unguarded_read_cannot_survive(
    label: str, args: dict, tool_name: str
) -> None:
    """The other direction, so the list above is not a list of invented inputs.

    Each shape is pushed through the *unguarded* expression the fix replaced, taken from
    the tree rather than retyped — so an arm that stops describing the real defect (the
    field renamed, the expression rewritten) fails here instead of passing quietly. A
    shape that no longer raises would mean the list is testing nothing.
    """
    old = _unguarded_reads()
    # The names the pre-fix expressions used, and nothing else: an empty `__builtins__`
    # with `len`/`int`/`str` added is what keeps this a measurement of the shape rather
    # than of a namespace that happened to be missing something.
    namespace = {"args": args, "PurePath": Path, "__builtins__": {"len": len, "int": int, "str": str}}
    raised = False
    for field, expression in old.items():
        if field not in args:
            continue
        try:
            eval(expression, namespace)  # noqa: S307
        except (TypeError, ValueError, AttributeError, KeyError):  # noqa: F841
            raised = True
    assert raised, (
        f"{label}: pushing this shape through the pre-fix read did not raise, so this "
        f"entry proves nothing about the defect. Read expressions: {old}"
    )


def _unguarded_reads() -> dict[str, str]:
    """`field -> the expression the pre-fix source used to read it`, spelled here.

    Deliberately a copy of the *old* form: it is the "before" the arms above are about,
    and the tree no longer contains it (that is the point of the change). The guard that
    keeps the tree from growing a *new* unguarded read is the AST arm below.
    """
    return {
        "content": "len(args.get('content', ''))",
        "file_path": "PurePath(args.get('file_path', '')).name",
        "start_line": "int(args.get('start_line') or args.get('line_limit'))",
        "command": "args.get('command', '').split('\\n')",
        # The truncation branch, not the plain read: a mapping reaches the slice only once
        # the prefix is too long to leave room for the command (`remaining < 8`), which is
        # why the arm above pairs a 25-key workdir with an 80-character command.
        "workdir": (
            "('…' + args.get('workdir')[-19:]) if len(args.get('workdir')) > 20 "
            "else args.get('workdir')"
        ),
    }


def test_no_field_of_the_header_is_read_without_a_shape_guard() -> None:
    """The rule, mechanised: a future field read cannot skip the guard silently.

    Every `args.get(...)` / `args[...]` inside each function below must be the argument of
    `_display_text` or `_display_int` (reached through the `or` chain the range uses),
    except the whole-dict `json.dumps(args)` fallback. This is the arm that would have
    caught the original six: they were `len(args.get('content', ''))` and friends, whose
    parent is not one of the two helpers.

    Three readers, not one. `_format_args` was the first, and it was fixed alone —
    measured 2026-10-07 (`cyc20261007-215754`), the `tool_end` summary still read
    `len(args.get("content", ""))` and `PurePath(args.get("file_path", "?"))` raw, so the
    *finished* call of a session rendered through an unguarded path while its *header* was
    guarded. One home, every reader — which is what this list is for.
    """
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    unchecked: list[str] = []
    for name in _SHAPE_GUARDED_READERS:
        target = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == name
            ),
            None,
        )
        if target is None:
            unchecked.append(f"{name} (not in app.py — this guard names what it reads)")
            continue

        parents: dict[int, ast.AST] = {}
        for parent in ast.walk(target):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent

        reads: list[ast.AST] = []
        for node in ast.walk(target):
            if isinstance(node, ast.Subscript) and _is_args(node.value):
                reads.append(node)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and _is_args(node.func.value)
            ):
                reads.append(node)
        if not reads:
            unchecked.append(f"{name} (no field read found — the guard would pass vacuously)")
            continue

        for read in reads:
            if _guarded_by_helper(read, parents):
                continue
            # The whole-dict fallback renders every field, which is what a field the header
            # cannot read is supposed to do.
            parent = parents.get(id(read))
            if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute):
                if parent.func.attr == "dumps":
                    continue
            unchecked.append(f"{name} line {read.lineno}")
    assert not unchecked, (
        "a display reader reads a field without a shape guard, which is the defect this "
        "module exists for: a field of the wrong shape raises out of the renderer and "
        f"takes the session with it. Unguarded read(s): {', '.join(unchecked)}"
    )


def _is_args(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "args"


def _guarded_by_helper(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Is this read the argument of `_display_text` / `_display_int`?

    Walks up through the `or` chain the line range uses — `args.get("start_line") or
    args.get("offset")` is one `_display_int(...)` call, and neither half is the call's
    direct argument.
    """
    current = node
    while id(current) in parents:
        parent = parents[id(current)]
        if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.Or):
            current = parent
            continue
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name):
            return parent.func.id in {"_display_text", "_display_int"}
        return False
    return False


# ── control legs: the fix must not change what the header shows ──────────────────────


def test_a_readable_write_still_reports_its_size() -> None:
    """The happy path this header exists for, so the guards did not flatten it."""
    assert _format_args({"file_path": "/a/b.txt", "content": "hello"}, "write") == "/a/b.txt (5B)"
    assert _format_args({"file_path": "/a/b.txt", "content": "x" * 2048}, "write") == "/a/b.txt (2KB)"
    assert _format_args({"file_path": "/a/b.txt", "content": ""}, "write") == "/a/b.txt"


def test_a_readable_read_still_reports_its_range() -> None:
    assert (
        _format_args({"file_path": "/a/b.txt", "start_line": 5, "line_limit": 3}, "read")
        == "/a/b.txt [L5:L8]"
    )
    assert _format_args({"file_path": "/a/b.txt", "start_line": 5}, "read") == "/a/b.txt [from L5]"
    # The numeric string a provider may send is still a line number.
    assert (
        _format_args({"file_path": "/a/b.txt", "start_line": "5", "line_limit": 3}, "read")
        == "/a/b.txt [L5:L8]"
    )


def test_a_readable_shell_call_still_shows_its_first_line_and_workdir() -> None:
    assert _format_args({"command": "ls -la"}, "bash") == "ls -la"
    assert _format_args({"command": "\n\n  echo hi\n"}, "bash") == "echo hi"
    assert _format_args({"command": "ls", "workdir": "/tmp"}, "bash") == "[/tmp] ls"


def test_a_long_path_is_still_compacted() -> None:
    deep = "/" + "/".join(["averylongdirectoryname"] * 4) + "/b.txt"
    rendered = _format_args({"file_path": deep, "content": "hi"}, "write")
    assert rendered.startswith("…/b.txt"), rendered


def test_a_field_the_header_cannot_read_falls_back_to_the_raw_arguments() -> None:
    """A shape that is not a path is shown as the JSON it arrived as, not hidden.

    This is the pre-existing behaviour for an absent `file_path` (`if fp:`), kept for the
    shapes the guard now folds into it: the reader still sees what the model sent.
    """
    rendered = _format_args({"file_path": {"a": 1}, "content": "hi"}, "write")
    assert json.loads(rendered) == {"file_path": {"a": 1}, "content": "hi"}


# ── the `tool_end` half: a finished call renders through the same rule ────────────────


def test_a_finished_write_renders_whatever_shape_its_fields_arrived_in() -> None:
    """The `✓ Wrote N bytes to …` line, for every shape the raw read died on.

    Measured 2026-10-07 (`cyc20261007-215754`) on master `7b3ed020`, replaying the
    pre-fix expressions verbatim: `len(args.get("content", ""))` raised `TypeError` on
    `content: 123` and on `content: null`, and answered **2** — printed as a byte count —
    on `content: ["a","b"]`; `PurePath(args.get("file_path", "?")).name` raised on a
    numeric or mapping `file_path`. This is the path that renders every *finished* write
    call, so it is not only the resumed session at stake: the live frame reached it too.
    """
    assert _write_call_summary({"file_path": "/a/b.txt", "content": 123}) == "✓ Wrote 0 bytes to /a/b.txt"
    assert _write_call_summary({"file_path": "/a/b.txt", "content": None}) == "✓ Wrote 0 bytes to /a/b.txt"
    assert _write_call_summary({"file_path": "/a/b.txt", "content": ["a", "b"]}) == "✓ Wrote 0 bytes to /a/b.txt"
    assert _write_call_summary({"file_path": 42, "content": "hi"}) == "✓ Wrote 2 bytes to ?"
    assert _write_call_summary({"file_path": {"a": 1}, "content": "hi"}) == "✓ Wrote 2 bytes to ?"
    assert _write_call_summary({"content": "hi"}) == "✓ Wrote 2 bytes to ?"


def test_a_readable_write_still_reports_its_own_path_and_size() -> None:
    """The control: the guard must not have replaced the reading with a placeholder."""
    assert _write_call_summary({"file_path": "/a/b.txt", "content": "hello"}) == "✓ Wrote 5 bytes to /a/b.txt"
    deep = "/" + "/".join(["averylongdirectoryname"] * 4) + "/b.txt"
    assert _write_call_summary({"file_path": deep, "content": "hi"}) == "✓ Wrote 2 bytes to …/b.txt"


def test_an_edit_hands_the_diff_only_shapes_it_can_render() -> None:
    """`None` for a pair that is not renderable, so the deferred crash cannot happen.

    `Diff(old=123)` does **not** raise where the call is built — `Diff.render` raises
    `AttributeError: 'int' object has no attribute 'splitlines'` one widget later, with
    the frame already consumed. So the guard has to be here, at the read, and this arm
    drives the render path the TUI drives, not merely the constructor.
    """
    from emrg.client.python_tui.widgets.base import RenderContext
    from emrg.client.python_tui.widgets.diff import Diff

    ctx = RenderContext(width=80)
    for args in (
        {"old_string": 123, "new_string": "b"},
        {"old_string": None, "new_string": None},
        {"old_string": {"a": 1}, "new_string": "b"},
        {"old_string": 123, "new_string": 456},
        {},
    ):
        pair = _edit_shows_a_diff(args)
        if pair is None:
            continue
        list(Diff(old=pair[0], new=pair[1]).render(ctx))  # must not raise

    # The control: a renderable pair is still handed on, and still renders.
    pair = _edit_shows_a_diff({"old_string": "a", "new_string": "b"})
    assert pair == ("a", "b")
    list(Diff(old=pair[0], new=pair[1]).render(ctx))
    assert _edit_shows_a_diff({"old_string": "a"}) == ("a", "")
    assert _edit_shows_a_diff({}) is None
