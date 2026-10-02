"""The bounded-stream contract: a cut stream fits its budget and says what it lost.

``bash_tool_v2`` and ``pwsh_tool_v2`` are peers (design §14.5 item 1), so each
carries its own copy of the framing helpers rather than importing the other.  The
copy is deliberate; the drift it invites is not, and ``_truncate_stdout`` had
drifted with **no test on either side**:

* the bash copy reserved 200 characters for the notice and reported
  ``len(stdout) - remaining`` as the number omitted, so its notice named a number
  200 smaller than the characters it had actually dropped;
* the pwsh copy kept ``head + tail == remaining`` and added the notice on top, so
  the text it returned exceeded the budget it was handed (the notice is not
  exempt from the budget it announces).

Both rules are enforced from both sides here, and the twins are fed one matrix
rather than trusted to stay faithful — the reading that would have caught this is
the same one that keeps them in step.
"""

from __future__ import annotations

import re

import pytest

from emrg.tools import bash_tool_v2 as bash
from emrg.tools import pwsh_tool_v2 as pwsh

#: Both copies of the function under test.
PEERS = (("bash", bash), ("pwsh", pwsh))

_OMITTED_HEAD_TAIL = re.compile(r"\.\.\. \[(\d+) chars omitted\] \.\.\.")
_OMITTED_HEAD_ONLY = re.compile(r"\.\.\. \[stdout truncated: (\d+) chars omitted, head kept\]")
_TRUNCATED_TOTAL = re.compile(r"\.\.\. \[stdout truncated, (\d+) chars total\]")


def _stdout_of(n: int) -> str:
    """A long, fully distinguishable stdout of exactly ``n`` characters."""
    unit = "0123456789abcdefghij\n"
    return (unit * (n // len(unit) + 1))[:n]


def _split_notice(output: str) -> tuple[int, str, str]:
    """Split an output into (claimed omitted, head kept, tail kept).

    The framing is exactly the two newlines on each side of the notice, so the
    head and tail recovered here are the characters the reader actually got —
    stripping *all* newlines would eat the content's own, which is how a helper
    can quietly make a correct number look wrong.
    """
    m = _OMITTED_HEAD_TAIL.search(output) or _OMITTED_HEAD_ONLY.search(output)
    assert m, f"no omission notice in {output[-120:]!r}"
    prefix = output[: m.start()]
    assert prefix.endswith("\n\n"), "the framing newlines before the notice"
    head = prefix[:-2]
    rest = output[m.end() :]
    if rest:
        assert rest.startswith("\n\n"), "the framing newlines after the notice"
        tail = rest[2:]
    else:
        tail = ""
    return int(m.group(1)), head, tail


def _kept_head_and_tail(output: str, stdout: str) -> tuple[str, str]:
    """Recover the head and tail an output kept, proving they are really those.

    The proof is the digits themselves: the head must be a prefix of the original
    and the tail a suffix of it, so the number in the middle is the only thing
    that can be wrong about what the cut removed.
    """
    _, head, tail = _split_notice(output)
    assert stdout.startswith(head), "the head kept must be the original's head"
    assert stdout.endswith(tail), "the tail kept must be the original's tail"
    return head, tail


# ── rule 1: what comes back fits the budget it was given ─────────────────


@pytest.mark.parametrize("name,mod", PEERS)
@pytest.mark.parametrize("remaining", [250, 600, 1200, 5000, 40_000])
def test_a_bounded_stream_fits_the_budget_it_was_given(name, mod, remaining):
    """The budget is what the module promised the context; a notice is not exempt."""
    stdout = _stdout_of(remaining * 7)
    output = mod._truncate_stdout(stdout, remaining)
    assert len(output) <= remaining, (
        f"{name}: returned {len(output)} chars for a {remaining}-char budget"
    )


# ── rule 2: the notice names the characters the cut really dropped ────────


@pytest.mark.parametrize("name,mod", PEERS)
@pytest.mark.parametrize("remaining", [1200, 5000, 40_000])
def test_the_notice_names_the_characters_it_dropped(name, mod, remaining):
    """The number is checkable against the original, not a statement about the budget."""
    stdout = _stdout_of(remaining * 7)
    output = mod._truncate_stdout(stdout, remaining)

    m = _OMITTED_HEAD_TAIL.search(output)
    assert m, "the head+tail path is the one these budgets reach"
    claimed = int(m.group(1))

    head, tail = _kept_head_and_tail(output, stdout)
    dropped = len(stdout) - len(head) - len(tail)
    assert claimed == dropped, f"{name}: notice says {claimed}, cut dropped {dropped}"


@pytest.mark.parametrize("name,mod", PEERS)
def test_the_head_only_notice_names_what_it_dropped_too(name, mod):
    """The narrow-budget path has its own wording and the same obligation."""
    remaining = 600  # budget - reserve is below _MIN_BOTH_ENDS
    stdout = _stdout_of(remaining * 7)
    output = mod._truncate_stdout(stdout, remaining)

    m = _OMITTED_HEAD_ONLY.search(output)
    assert m, f"{name} did not take the head-only path: {output[-90:]!r}"

    claimed, kept, tail = _split_notice(output)
    assert tail == "", "the head-only path returns no tail"
    assert stdout.startswith(kept)
    assert claimed == len(stdout) - len(kept)


@pytest.mark.parametrize("name,mod", PEERS)
def test_no_room_for_content_names_the_total(name, mod):
    """When the reserve cannot fit, the loss is named and nothing is pretended."""
    stdout = _stdout_of(4000)
    output = mod._truncate_stdout(stdout, mod._NOTICE_RESERVE)
    m = _TRUNCATED_TOTAL.search(output)
    assert m, output
    assert int(m.group(1)) == len(stdout)


# ── the boundary, from both sides ────────────────────────────────────────


@pytest.mark.parametrize("name,mod", PEERS)
def test_a_stream_that_fits_is_returned_unchanged(name, mod):
    """Exactly the budget is inside it: no notice, no cut, nothing added."""
    for n in (1, 999, 1000):
        stdout = _stdout_of(n)
        assert mod._truncate_stdout(stdout, n) == stdout
        assert mod._truncate_stdout(stdout, n + 1) == stdout


@pytest.mark.parametrize("name,mod", PEERS)
def test_one_character_over_the_budget_is_cut(name, mod):
    """The other side of that boundary must not be silent."""
    stdout = _stdout_of(1001)
    output = mod._truncate_stdout(stdout, 1000)
    assert output != stdout
    assert "truncated" in output


# ── the twins are one policy, measured rather than compared by name ──────


def test_the_twins_agree_on_the_budget_constants():
    """The constants behind the policy, pinned the way the stderr pair is."""
    assert pwsh._NOTICE_RESERVE == bash._NOTICE_RESERVE
    assert pwsh._MIN_BOTH_ENDS == bash._MIN_BOTH_ENDS


@pytest.mark.parametrize("remaining", [600, 1200, 5000, 40_000])
def test_the_twins_cut_the_same_stdout_the_same_way(remaining):
    """One text, one budget, two dialects: the model must not see the difference.

    This is the leg whose absence let the two copies drift: the pair of tests
    beside it pinned constants and ``_truncate_stderr``, so ``_truncate_stdout``
    was the one function in the pair no assertion reached.
    """
    stdout = _stdout_of(remaining * 7)
    assert bash._truncate_stdout(stdout, remaining) == pwsh._truncate_stdout(
        stdout, remaining
    )
