"""A count parameter has a domain, and a value outside it is refused (issue #1935).

The JSON Schema of every tool in `emrg/tools/` declares ``{"type": "integer"}`` and
stops there, so a parameter's lower bound existed only in the code consuming the
number — and each consumer read an out-of-domain value as a **different number**
rather than as a caller error. Measured 2026-10-08 (`cyc20261008-175325`):

* ``read`` with ``line_limit=-3``: a 10-line file came back as lines 1-7 (a slice
  from the *end*) and the continuation hint named ``start_line=-2``;
* ``read`` with ``line_limit=0``: falsy, so an ``or`` chain read it as *absent* and
  returned the default 1000 lines;
* ``read`` with ``start_line_byte_offset=-5``: clamped to 0, which returns the line
  whole — byte-identical to asking for no offset at all;
* ``grep`` with ``context_before=-2``: the block printed its ``file:3:`` header and
  no line at all, and the same value collapsed the stop budget so the summary blamed
  ``max_results=200``, a cause that was not the cause;
* ``bash``/``pwsh`` with ``timeout=0``: ``echo HELLO`` came back as
  ``(no output) / [timed out after 0ms] / [killed by signal: 9]`` — the command was
  killed with the value the caller sent, and the reading called it a timeout;
* ``edit`` with ``replace_all="false"``: the string is truthy, so **both**
  occurrences of a two-occurrence ``old_string`` were replaced and the call answered
  ``Made 2 replacements`` — a write in a place the caller had excluded.

This file pins the rule at its one implementation (`count_argument`,
`boolean_argument`) and at each carrier the tools expose, plus the schema sentence
that tells a caller the domain *before* it errs. The per-tool behaviour is pinned
beside each tool (`tests/test_read_tool.py`, `tests/test_grep_tool.py`,
`tests/test_edit_tool.py`); this is the shared rule.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from emrg.server import scheduler
from emrg.tools import bash_tool_v2, edit_tool, grep_tool, pwsh_tool_v2, read_tool
from emrg.tools.base import boolean_argument, count_argument

#: (label, kwargs, expected value) for the helper's three answers.
ACCEPTED = [
    ("absent, with a default", dict(minimum=1, default=7), 7),
    ("absent, no default", dict(minimum=0, default=None), None),
    ("zero where the domain admits it", dict(minimum=0, default=5), 0),
    ("an integer", dict(minimum=1, default=5), 3),
    ("a numeric string", dict(minimum=1, default=5), "3"),
    ("a padded numeric string", dict(minimum=1, default=5), " 3 "),
    ("an integral float", dict(minimum=1, default=5), 3.0),
    ("the minimum itself", dict(minimum=2, default=5), 2),
]

REFUSED = [
    ("below the minimum", dict(minimum=1, default=5), 0),
    ("negative", dict(minimum=0, default=5), -3),
    ("fractional", dict(minimum=1, default=5), 2.5),
    ("not a number", dict(minimum=1, default=5), "abc"),
    ("a boolean", dict(minimum=1, default=5), True),
    ("NaN", dict(minimum=1, default=5), float("nan")),
    ("infinity", dict(minimum=1, default=5), float("inf")),
    ("a list", dict(minimum=1, default=5), [1]),
]


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("label,kwargs,value", ACCEPTED, ids=[c[0] for c in ACCEPTED])
def test_count_argument_reads_the_values_its_domain_admits(label, kwargs, value):
    got, refusal = count_argument({"n": value}, "n", **kwargs)
    assert refusal is None, f"{label}: refused a value its domain admits: {refusal!r}"
    assert got == kwargs["default"] if value is None else got == int(value)


@pytest.mark.parametrize("label,kwargs,value", REFUSED, ids=[c[0] for c in REFUSED])
def test_count_argument_refuses_a_value_outside_the_domain(label, kwargs, value):
    got, refusal = count_argument({"n": value}, "n", **kwargs)
    assert got is None, f"{label}: a value outside the domain was answered with {got!r}"
    assert refusal, f"{label}: refused silently"
    assert "n" in refusal and repr(value) in refusal, (
        f"the refusal must name the parameter and the value it saw: {refusal!r}"
    )


def test_count_argument_names_the_spelling_the_caller_used():
    """An alias in the refusal, so a caller can find the parameter it sent.

    `read` accepts `offset` for `start_line`; a refusal that named the other one
    would send a reader to a key that is not in their call.
    """
    _, refusal = count_argument({"offset": 0}, "start_line", "offset", minimum=1)
    assert refusal is not None
    assert "offset" in refusal and "start_line" not in refusal


def test_count_argument_reads_presence_not_truthiness():
    """`0` is a value, not an absence — the half that made `line_limit=0` a default.

    An `or` chain cannot tell "the caller sent 0" from "the caller sent nothing",
    and that is exactly how the default window was returned for `line_limit=0`.
    """
    got, refusal = count_argument({"n": 0, "m": 4}, "n", "m", minimum=0)
    assert (got, refusal) == (0, None), "0 fell through to the alias"


def test_the_shell_tools_read_one_field_the_same_way():
    """`bash` and `pwsh` carry the same coercion; a divergence is the defect.

    They are separate modules with separate copies (the file's own convention), so
    the agreement is asserted rather than assumed.
    """
    for value in (None, 5, 0.5, "2", 0, -5, "abc", True, float("nan"), float("inf")):
        bash_answer = bash_tool_v2._as_timeout(value)
        pwsh_answer = pwsh_tool_v2._as_timeout(value)
        assert (bash_answer[1] is None) == (pwsh_answer[1] is None), (
            f"timeout={value!r}: bash {bash_answer!r} vs pwsh {pwsh_answer!r}"
        )
        assert bash_answer[0] == pwsh_answer[0]


def test_the_shell_default_is_the_one_the_watchdog_bounds_by():
    """One datum, two readers, and they must agree on what 'absent' means.

    `scheduler._tool_silence_seconds` bounds a silent call by
    `_TOOL_SILENCE_DEFAULT_SECONDS`, whose comment says it is `_as_timeout`'s own
    default "so the watchdog and the tool agree". This is that agreement, measured.
    """
    assert bash_tool_v2._as_timeout(None)[0] == scheduler._TOOL_SILENCE_DEFAULT_SECONDS
    assert pwsh_tool_v2._as_timeout(None)[0] == scheduler._TOOL_SILENCE_DEFAULT_SECONDS


@pytest.mark.parametrize("value", [0, -5, "abc", True, float("nan")])
def test_a_shell_call_with_an_out_of_domain_timeout_never_runs(tmp_path, value):
    """The measurement, driven end to end: no command runs, and no timeout is claimed.

    Before the fix `timeout=0` answered ``(no output) / [timed out after 0ms] /
    [killed by signal: 9]`` — a killed command reported as a timeout, which is what
    a reader would act on. Both tools are driven because the refusal must precede
    the spawn in each.
    """
    for tool, name in ((bash_tool_v2.BashToolV2(), "bash"), (pwsh_tool_v2.PwshToolV2(), "pwsh")):
        result = _run(tool.execute({
            "command": "echo HELLO", "workdir": str(tmp_path), "timeout": value,
            "intent": "print a word",
        }))
        assert result.error, f"{name} accepted timeout={value!r}"
        assert "timeout" in result.content and repr(value) in result.content
        assert "HELLO" not in result.content, f"{name} ran the command anyway"
        assert "timed out after" not in result.content, (
            f"{name} reported a timeout for a command it refused to run"
        )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash on the Windows runner is WSL's, not the shell this control runs under in "
    "CI: `bash.exe` there is the WSL launcher, which with no installed distribution "
    "prints 'Windows Subsystem for Linux has no installed distributions.' and never runs "
    "the command (measured 2026-10-08, run 37761817997 on PR #1936). The control asserts "
    "a command really ran, which no shell on that runner can establish",
)
def test_a_positive_shell_timeout_still_runs(tmp_path):
    """The control: an in-domain timeout still executes, so the refusal is not a wall.

    The refusal half above stays armed on Windows (both tools refuse before the spawn);
    only this half needs a shell that runs, and the Windows runner has none.
    """
    result = _run(bash_tool_v2.BashToolV2().execute({
        "command": "echo HELLO", "workdir": str(tmp_path), "timeout": 5,
        "intent": "print a word",
    }))
    assert not result.error
    assert "HELLO" in result.content


#: (tool, parameter, phrases any correct description carries) — the schema is the
#: only place a caller is told the domain *before* the call, so a description that
#: states no bound is the declaration half of the same defect.
#:
#: `read.start_line_byte_offset` was the last missing row, and its absence was **stated**
#: rather than silent: its description was being rewritten by the then-open PR #1929, so
#: editing the same lines here would have conflicted. #1929 merged (`2893bfad`) with a
#: description that named no bound, and the row was added on top of that rewrite — the
#: three carriers of `read` now declare their domains in the same table.
DOMAINS = [
    (read_tool.ReadTool(), "start_line", ("1 or more", "at least 1", ">= 1")),
    (read_tool.ReadTool(), "line_limit", ("at least 1", "1 or more", ">= 1")),
    (read_tool.ReadTool(), "start_line_byte_offset", ("0 or more", "at least 0", ">= 0")),
    (grep_tool.GrepTool(), "context_before", ("0 or more", "at least 0", ">= 0")),
    (grep_tool.GrepTool(), "context_after", ("0 or more", "at least 0", ">= 0")),
    (grep_tool.GrepTool(), "max_results", ("at least 1", "1 or more", ">= 1")),
    (edit_tool.EditTool(), "replace_all", ("true or false",)),
    (bash_tool_v2.BashToolV2(), "timeout", ("positive",)),
    (pwsh_tool_v2.PwshToolV2(), "timeout", ("positive",)),
]


@pytest.mark.parametrize(
    "tool,parameter,phrases",
    DOMAINS,
    ids=[f"{t.definition().name}.{p}" for t, p, _ in DOMAINS],
)
def test_the_schema_states_the_domain(tool, parameter, phrases):
    description = tool.definition().parameters["properties"][parameter]["description"]
    lowered = description.lower()
    assert any(phrase in lowered for phrase in phrases), (
        f"{parameter}'s description states no domain: {description!r}"
    )
    assert "refus" in lowered, (
        f"{parameter}'s description does not say an out-of-domain value is refused: "
        f"{description!r}"
    )


# ── the boolean half: the carrier where getting it wrong writes ───────────────


def test_boolean_argument_reads_the_spellings_that_name_a_value():
    assert boolean_argument({"b": True}, "b") == (True, None)
    assert boolean_argument({"b": False}, "b") == (False, None)
    assert boolean_argument({}, "b", default=True) == (True, None)
    for spelling, expected in (("false", False), ("TRUE", True), (" true ", True)):
        assert boolean_argument({"b": spelling}, "b") == (expected, None), spelling


@pytest.mark.parametrize("value", [0, 1, "yes", "", "0", 2.0, []])
def test_boolean_argument_refuses_everything_else(value):
    got, refusal = boolean_argument({"b": value}, "b")
    assert got is None, f"{value!r} was coerced to {got!r}"
    assert refusal and "b" in refusal and repr(value) in refusal


def test_edit_does_not_read_replace_all_by_truthiness(tmp_path):
    """`replace_all="false"` used to replace **both** occurrences — a write the caller excluded.

    Measured before the fix: the string is truthy, so `content.replace(old, new)`
    ran without the count and the call answered ``Made 2 replacements``.
    """
    f = tmp_path / "two.txt"
    f.write_text("dup\ndup\n")
    result = _run(edit_tool.EditTool().execute({
        "file_path": str(f), "old_string": "dup", "new_string": "X",
        "replace_all": "false", "intent": "replace one",
    }))
    assert result.error, "a two-occurrence edit without replace_all must be refused"
    assert f.read_text() == "dup\ndup\n", "the file was written anyway"
    assert "2 replacements" not in result.content


@pytest.mark.parametrize("value", [0, 1, "yes", ""])
def test_edit_refuses_a_replace_all_that_is_not_a_boolean(tmp_path, value):
    f = tmp_path / "two.txt"
    f.write_text("dup\ndup\n")
    result = _run(edit_tool.EditTool().execute({
        "file_path": str(f), "old_string": "dup", "new_string": "X",
        "replace_all": value, "intent": "replace all",
    }))
    assert result.error
    assert "replace_all" in result.content and repr(value) in result.content
    assert f.read_text() == "dup\ndup\n", "a refused call still wrote"


def test_edit_still_replaces_all_when_asked(tmp_path):
    """The control: the flag still does its job, in both spellings of "true"."""
    for value in (True, "true"):
        f = tmp_path / f"two_{value}.txt"
        f.write_text("dup\ndup\n")
        result = _run(edit_tool.EditTool().execute({
            "file_path": str(f), "old_string": "dup", "new_string": "X",
            "replace_all": value, "intent": "replace all",
        }))
        assert not result.error, result.content
        assert f.read_text() == "X\nX\n"
