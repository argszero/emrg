"""The guard with no declared workdir (issue #1359).

`_check_sandbox(cmd, mode)` takes `workdir` as an optional third argument, and
the two readings used to disagree in *both* directions:

  - **fail-open** — the move-out check was skipped outright (`moved_out = None`
    whenever `workdir_real` was None), so `cd <outside> && cat > f` was BLOCKED
    with a workspace declared and ALLOWED without one. That is the dangerous
    direction: the guard let through exactly the write it exists to place.
  - **stricter** — `allowed_srcs` omitted the workspace, so an absolute target
    under the declared workspace was blocked when workdir was omitted.

The fix answers the move-out question with the cwd the child will actually have
(the process's own, since `execute()` passes `cwd=workdir` and a None cwd means
"inherit"), and keeps `allowed_srcs` keyed on a **declared** workspace. The
property that makes that safe is one sentence: **an omitted workdir never permits
anything a declared one refuses.** Not "the two readings agree" — the omitted one
is deliberately the stricter of the pair on absolute targets, and now equal on
relative ones.

Production always passes a workdir (the daemon injects the session cwd), so these
are the readings a *caller that omits the argument* gets — how a future tool, or
a probe, reaches the guard.
"""
from __future__ import annotations

import os

import pytest

from emrg.tools.bash_tool import _check_sandbox

MODE = "workspace-write"

# A directory the guard can place inside neither the workspace nor this process's
# cwd, and that the temp-root allowance does not already cover (a destination
# under the OS temp root deliberately does not count as leaving the workspace, so
# a tmp_path would make these rows unmeasurable).
_OUTSIDE = "/opt/emrg-nowhere/elsewhere"


def _verdict(cmd: str, workdir: str | None) -> bool:
    allowed, _reason, _enforcement = _check_sandbox(cmd, MODE, workdir)
    return allowed


def _reason(cmd: str, workdir: str | None) -> str | None:
    _allowed, reason, _enforcement = _check_sandbox(cmd, MODE, workdir)
    return reason


@pytest.fixture
def workspace(tmp_path):
    """A workspace the test builds itself — never a host path."""
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    return str(ws)


class TestTheFailOpenIsClosed:
    """The direction that mattered: the omitted reading must not permit a write
    the declared reading refuses."""

    def test_moving_out_then_writing_relative_is_refused_without_a_workdir(
        self, workspace, monkeypatch
    ):
        monkeypatch.chdir(workspace)
        cmd = f"cd {_OUTSIDE} && cat > f"
        assert _verdict(cmd, workspace) is False, "control: the declared reading refuses"
        assert _verdict(cmd, None) is False, (
            "an omitted workdir must not let a move-out write through (issue #1359)"
        )

    def test_the_refusal_is_the_move_check_and_not_a_coincidence(
        self, workspace, monkeypatch
    ):
        """Why it is refused, not merely that it is: the reason names the
        directory the command moved to, which is the check the omitted reading
        used to skip — a generic block would be some other rule firing."""
        monkeypatch.chdir(workspace)
        reason = _reason(f"cd {_OUTSIDE} && cat > f", None)
        # The directory as the guard resolved it, not as the command spelled it:
        # on Windows the guard reports `realpath("/opt/...")` as `C:\opt\...`, so
        # a substring test against the literal spelling measures the host's path
        # algebra instead of the rule under test (measured red on windows-2025).
        assert reason is not None and os.path.realpath(_OUTSIDE) in reason

    def test_a_variable_move_out_is_also_refused_without_a_workdir(
        self, workspace, monkeypatch
    ):
        """The same rule through the other spelling of the same escape."""
        monkeypatch.chdir(workspace)
        cmd = f'D={_OUTSIDE} && cd "$D" && cat > f'
        assert _verdict(cmd, workspace) is False, "control: the declared reading refuses"
        assert _verdict(cmd, None) is False


class TestTheOmittedReadingIsNotWeaker:
    """The property, over rows in both directions. A row where the omitted
    reading allows while the declared one refuses is the regression."""

    ROWS = (
        "cat > f",                             # plain relative, no move
        "cat > sub/f",                         # relative into a subdirectory
        "cd sub && cat > f",                   # a move that stays inside
        "cat > /dev/null",                     # never a target
        f"cd {_OUTSIDE} && cat > f",           # move out, then relative
        f"cd {_OUTSIDE}; echo x > out.txt",    # the #1244 spelling
        'D={d} && cd "$D" && cat > f',         # move out through a variable
    )

    @pytest.mark.parametrize("row", ROWS)
    def test_omitting_a_workdir_never_permits_more(self, workspace, monkeypatch, row):
        monkeypatch.chdir(workspace)
        cmd = row.format(d=_OUTSIDE)
        declared = _verdict(cmd, workspace)
        omitted = _verdict(cmd, None)
        assert not (omitted and not declared), (
            f"an omitted workdir allowed what a declared one refuses: {cmd!r}"
        )


class TestTheOrdinaryCasesAreUnchanged:
    """Negative controls: the fix may only add refusals. If these go red the fix
    bought the fail-open's closure with an over-block."""

    def test_a_relative_write_with_no_move_is_allowed_in_both_readings(
        self, workspace, monkeypatch
    ):
        monkeypatch.chdir(workspace)
        assert _verdict("cat > f", workspace) is True
        assert _verdict("cat > f", None) is True

    def test_a_move_that_stays_inside_is_allowed_in_both_readings(
        self, workspace, monkeypatch
    ):
        monkeypatch.chdir(workspace)
        assert _verdict("cd sub && cat > f", workspace) is True
        assert _verdict("cd sub && cat > f", None) is True


# One asymmetry is deliberately NOT pinned here, and the reason is worth keeping
# rather than rediscovering: with no declared workspace there is no workspace to
# allow, so an *absolute* target under one stays refused by the omitted reading
# while the declared reading allows it. Asserting that as a test was tried and
# dropped — for any directory a test can safely create (`tmp_path`, the checkout)
# the answer is decided by the temp-root allowance or this repo's own trusted
# zone, not by the rule under test, so the row would pass or fail by *where the
# tree happens to sit*, which is a defect in a test rather than evidence about
# the guard. It is recorded in `_check_sandbox`'s own comment instead, together
# with the instruction that matters: do not "fix" it into the fail-open's mirror
# image by widening `allowed_srcs` to an undeclared cwd.
