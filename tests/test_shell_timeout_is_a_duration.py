"""A shell timeout is a *positive duration* — measured on both shell tools at once.

The class this file exists for
-----------------------------
`timeout` is an argument the model sends, read by a function (`_as_timeout`) that has one
job: turn whatever arrives into a bound. Measured 2026-10-02 on this host, before this
file, it returned the value unchecked, so a non-positive bound became an **instant kill**
reported as a duration:

    echo hi, timeout=0   ->  (no output) [timed out after 0ms] [killed by signal: 9]
    echo hi, timeout=-1  ->  (no output) [timed out after -1000ms] [killed by signal: 9]

The second is the load-bearing one: `-1000ms` is not a wait. A negative duration is a
claim about the world that cannot be true, printed in the marker the model reads and
reacts to — the same shape as the `restored byte-for-byte: True` over a changed file that
`scripts/run-mutation-arm.py` carried until 2026-10-02: **a number the reader cannot
falsify is not a report.** Nothing failed loudly in either case; the run was killed and
the number was nonsense.

Why the two tools are tested in one file
----------------------------------------
`emrg/tools/pwsh_tool_v2.py` is a sibling of `emrg/tools/bash_tool_v2.py`, not a
subclass: it carries its own `ShellRunResult`, `run_command` and `render_result`, and its
own **copy** of `_as_timeout` (byte-identical when this file was written). Two homes for
one rule is how the rule drifts, so the domain is pinned on both and their agreement is
pinned in its own right — a fix applied to one copy only would be caught here.

What is deliberately *not* pinned
---------------------------------
`0` means "no limit" in this repository's own `scripts/run-mutation-arm.py`. That is that
tool's documented spelling for a *bounded-by-the-caller* run; an unbounded bash run is the
hazard the 30-second default exists to prevent, so the rule here is the positive one and
`0` is not given the arm runner's meaning. The assertion below pins that choice rather
than leaving it to whichever reading a later change prefers.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

import emrg.tools.bash_tool_v2 as bash_v2
import emrg.tools.pwsh_tool_v2 as pwsh_v2

#: Every module that reads a shell `timeout` for itself. Importing both here is what makes
#: "the rule has one meaning" checkable instead of assumed.
TIMEOUT_READERS = [bash_v2, pwsh_v2]

DEFAULT = 30.0


def _ids(mods):
    return [m.__name__.rsplit(".", 1)[-1] for m in mods]


@pytest.mark.parametrize("mod", TIMEOUT_READERS, ids=_ids(TIMEOUT_READERS))
@pytest.mark.parametrize(
    "value,expected",
    [
        # the documented default
        (None, DEFAULT),
        (30, 30.0),
        (1.5, 1.5),
        ("2.5", 2.5),
        (45, 45.0),
        # unusable: the documented default (this is what the rule already did for "abc")
        ("abc", DEFAULT),
        (True, DEFAULT),          # a bool is not a duration
        (False, DEFAULT),
        # non-positive: an instant kill is not a bound, and a negative duration is not
        # a duration at all — the measured defect
        (0, DEFAULT),
        (0.0, DEFAULT),
        ("0", DEFAULT),
        (-1, DEFAULT),
        (-0.5, DEFAULT),
        ("-3", DEFAULT),
        # values that would make `asyncio.wait` wait forever or not at all
        (float("inf"), DEFAULT),
        (float("-inf"), DEFAULT),
        (float("nan"), DEFAULT),
        ("nan", DEFAULT),
        ("inf", DEFAULT),
        (10**9, 10**9),           # large but finite is the caller's business
    ],
)
def test_the_domain_of_a_timeout(mod, value, expected) -> None:
    assert mod._as_timeout(value) == expected


@pytest.mark.parametrize("mod", TIMEOUT_READERS, ids=_ids(TIMEOUT_READERS))
def test_a_timeout_is_always_a_positive_duration(mod) -> None:
    """The invariant behind the marker: no input may yield `<= 0` (or a NaN/inf).

    Stated separately from the table above because this is the property a *reader* of the
    report depends on — `[timed out after {timeout_ms}ms]` is composed straight from it,
    so a value that is not a positive duration becomes prose that cannot be true.
    """
    probes = [
        None, 0, -1, -0.0, 1, "1", "abc", "", " ", [], {}, True, False,
        float("nan"), float("inf"), float("-inf"), 10**12, -(10**12),
    ]
    for value in probes:
        seconds = mod._as_timeout(value)
        assert seconds > 0 and seconds == seconds and seconds != float("inf"), (
            f"{mod.__name__} turned {value!r} into {seconds!r}, which reaches the "
            "timeout marker as a duration that cannot exist"
        )


def test_the_two_copies_of_the_rule_agree_on_every_probe() -> None:
    """A rule with two homes is a rule free to drift apart.

    The bash and pwsh tools keep their own copy (they are siblings, not a base class), and
    a fix applied to one of them is exactly the change that leaves the other behind. This
    is the assertion that makes that visible instead of silent.
    """
    probes = [
        None, True, False, 0, 0.0, 1, 1.5, -1, -0.5, 30, 45, 10**6,
        "0", "1", "2.5", "-3", "abc", "", " ", "nan", "inf",
        float("nan"), float("inf"), float("-inf"),
    ]
    for value in probes:
        assert bash_v2._as_timeout(value) == pwsh_v2._as_timeout(value), (
            f"the two copies disagree about {value!r}: "
            f"{bash_v2._as_timeout(value)!r} vs {pwsh_v2._as_timeout(value)!r}"
        )


#: v2's inner shell is ``bash`` by contract, so the end-to-end half needs one. The same
#: guard, and the same reason, as ``tests/test_tool_cancel_kills_the_group.py``.
needs_a_shell = pytest.mark.skipif(
    sys.platform == "win32",
    reason="v2's inner shell is bash; these spawn it end to end",
)


def _run(args: dict, tmp_path) -> str:
    """One real run of the bash tool, unconfined and confined to the test's own tree.

    The policy is named explicitly for the reason the sibling test names it: a tier that
    can confine needs a backend that may be absent in CI, and this file is about the
    *timeout* argument — whether the shell ran under a sandbox is not its subject.
    ``workspace``/``workdir`` come from ``tmp_path`` so nothing here reaches the checkout.
    """
    tool = bash_v2.BashToolV2()
    result = asyncio.run(
        tool.execute(
            {
                **args,
                "intent": "test the timeout domain",
                "sandbox": "danger-full-access",
                "workspace": str(tmp_path),
                "workdir": str(tmp_path),
            }
        )
    )
    return result.content if hasattr(result, "content") else str(result)


@needs_a_shell
class TestTheArgumentAtTheToolBoundary:
    """End to end, because the defect was only visible in the rendered marker."""

    @pytest.mark.parametrize("value", [0, -1, "0", "-1"])
    def test_a_non_positive_timeout_does_not_kill_the_command(self, value, tmp_path) -> None:
        """The measured defect: `echo hi` with `timeout=0` came back killed and empty."""
        out = _run({"command": "echo the command really ran", "timeout": value}, tmp_path)
        assert "the command really ran" in out, (
            f"timeout={value!r} killed the command instead of using the default:\n{out}"
        )
        assert "timed out after -" not in out, out
        assert "killed by signal" not in out, out

    def test_a_positive_timeout_is_still_honoured(self, tmp_path) -> None:
        """The other direction: the fix must not turn every bound into 30 seconds.

        A `sleep 2` under `timeout=1` must still be killed — otherwise the "always
        positive" rule would have been bought by making the argument decorative.
        """
        out = _run({"command": "sleep 2; echo never printed", "timeout": 1}, tmp_path)
        assert "[timed out after 1000ms]" in out, out
        assert "never printed" not in out, out

    def test_the_timeout_marker_never_carries_a_negative_duration(self, tmp_path) -> None:
        """The prose the model reads, composed from the value this file is about.

        Measured before the fix: `[timed out after -1000ms]`. The assertion is on the
        marker rather than on the return value because the marker is what a reader acts
        on — the same reason `run-mutation-arm.py`'s `restored byte-for-byte` sentence had
        to stop saying a thing its check could not see.
        """
        out = _run({"command": "echo hi", "timeout": -5}, tmp_path)
        assert "hi" in out, out
        assert "[timed out after -" not in out, out
        assert "[timed out after" not in out, out
