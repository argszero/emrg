"""A relative write target that climbs out of the workspace (issue #1353).

The rule this file pins: "relative, therefore inside the workspace" is an
assumption about *where* the write lands, and it is only sound while the
resolved target is still under the directory the command runs in. A literal
`..` breaks it without moving anything, and before this fix the guard had every
fact it needed and answered ALLOW:

    guard ALLOW   echo x > ../escaped.txt
    rc=0          the file really appears outside the workspace

Two spellings, one hole. `$T/../../escaped.txt` arrives through the resolution
added for #1316 (a variable the command itself assigns), and lands in the same
"relative, therefore inside" branch — so a rule keyed on the command's *text*
(`".." in cmd`) would miss it, while a rule keyed on the resolved target covers
both.

**Why the rows live under this repo rather than under `tmp_path`.** The
workspace-write allowance deliberately covers the OS temp root
(`_temp_write_roots()`), and `tmp_path` is inside it — so a climb out of
`<tmp>/ws` lands in an allowed zone and *every* row would be ALLOW for a reason
that has nothing to do with the rule under test. It is the same measurement trap
`tests/test_sandbox_omitted_workdir.py` documents for its own rows. The scratch
below is therefore created inside `tests/`, one level deeper than it looks: the
workspace is `<scratch>/a/b/ws`, so a one- and a two-level climb both land
inside `<scratch>`, which this file removes afterwards.

The `/bin/sh` half is skipped on Windows (no such binary); the classification
half runs everywhere, which is what makes the refusal verifiable on both CI
legs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from emrg.tools.bash_tool import _check_sandbox

MODE = "workspace-write"

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

#: ``(command, the file the shell opens, how many levels above the workspace)``.
#: Each row was run for real before it was pinned — see
#: `TestTheEscapeIsReal`, which does exactly that on every run.
_ESCAPE_ROWS = (
    ("echo x > ../escaped.txt", os.path.join("a", "b", "escaped.txt")),
    ("T=. && echo hi > $T/../../escaped.txt", os.path.join("a", "escaped.txt")),
    ("echo x > ../../escaped2.txt", os.path.join("a", "escaped2.txt")),
)

#: Rows that leave the workspace directory and return to it — allowed, and the
#: reason the rule is a containment test on the resolved target rather than a
#: search for `..`.
_CLIMB_AND_RETURN_ROWS = (
    "echo x > ../ws/out.txt",             # one level up, straight back in
    "echo x > ../../b/ws/out.txt",        # two levels up, then back down
)

#: Commands whose relative target resolves back inside the workspace. The fix may
#: only add refusals, so these are the over-block controls — the second climbs
#: out of the workspace directory and returns to it, and must stay allowed.
_ALLOWED_ROWS = (
    "echo x > out.txt",
    "echo x > sub/out.txt",
    "echo x > ./sub/../out.txt",
    "echo x > ../ws/out.txt",
    "echo x > /dev/null",
    "cd sub && echo x > out.txt",
)


def _reason(cmd: str, workdir: str | None) -> str | None:
    _allowed, reason, _enforcement = _check_sandbox(cmd, MODE, workdir)
    return reason


def _allowed(cmd: str, workdir: str | None) -> bool:
    return _check_sandbox(cmd, MODE, workdir)[0]


@pytest.fixture
def scratch():
    """`<scratch>/a/b/ws` plus its ancestors, all removed afterwards.

    Three levels of headroom so the two-level climb in `_ESCAPE_ROWS` stays
    inside the directory this fixture deletes: a row that escaped into the
    checkout's own `tests/` would leave a file behind on a green run.
    """
    root = tempfile.mkdtemp(dir=_TESTS_DIR, prefix="emrg-relative-escape-")
    try:
        ws = os.path.join(root, "a", "b", "ws")
        os.makedirs(os.path.join(ws, "sub"))
        with open(os.path.join(ws, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("payload\n")
        yield root, ws
    finally:
        shutil.rmtree(root, ignore_errors=True)


class TestTheEscapeIsReal:
    """The row is refused *and* the shell really leaves the workspace.

    Without the second half this file could be pinning a refusal of a command
    that writes nothing — the defect `tests/test_bash_tool_sandbox.py` names for
    its own rows. The command is run in a scratch directory the fixture owns, by
    `/bin/sh`, and the created file must be outside `ws` and inside the scratch.
    """

    @pytest.mark.parametrize("row", _ESCAPE_ROWS)
    def test_the_shell_really_writes_outside_and_the_guard_refuses(self, scratch, row):
        if sys.platform == "win32":
            pytest.skip("POSIX shell ground truth: /bin/sh does not exist")
        root, ws = scratch
        cmd, created = row
        proc = subprocess.run(
            ["/bin/sh", "-c", cmd], cwd=ws, capture_output=True, text=True
        )
        landed = os.path.join(root, created)
        assert proc.returncode == 0, f"/bin/sh could not run {cmd!r}: {proc.stderr}"
        assert os.path.exists(landed), (
            f"{cmd!r} created nothing at {landed!r} — the row no longer measures "
            "an escape, so the refusal below would be about nothing"
        )
        assert not os.path.exists(os.path.join(ws, created)), (
            f"{cmd!r} landed inside the workspace after all — re-measure the row"
        )
        assert _allowed(cmd, ws) is False, f"{cmd!r} was ALLOWED at workspace-write"

    @pytest.mark.parametrize("row", _ESCAPE_ROWS)
    def test_the_refusal_names_the_rule_and_the_directory(self, scratch, row):
        """Why it is refused, not merely that it is: the reason is this rule's
        own message, and it names the base the target was resolved against.

        Compared by **component**, never by a whole spelling: the message
        carries `!r` paths, and Windows rewrites the separators and the drive —
        the mistake that cost two CI rounds in
        `tests/test_sandbox_omitted_workdir.py`."""
        _root, ws = scratch
        cmd, created = row
        reason = _reason(cmd, ws)
        assert reason is not None
        assert "relative target" in reason, reason
        assert "issue #1353" in reason, reason
        assert os.path.basename(created) in reason, reason


class TestTheClimbBackInsideIsStillAllowed:
    """Over-block controls: every row here was ALLOWED before the fix."""

    @pytest.mark.parametrize("cmd", _ALLOWED_ROWS)
    def test_a_target_that_resolves_inside_is_allowed(self, scratch, cmd):
        _root, ws = scratch
        assert _allowed(cmd, ws) is True, _reason(cmd, ws)

    @pytest.mark.parametrize("cmd", _CLIMB_AND_RETURN_ROWS)
    def test_a_climb_that_returns_to_the_workspace_is_allowed(self, scratch, cmd):
        """A spelling-based rule (`".." in cmd`) would refuse these rows; the
        resolved-target form allows them, which is the over-block it avoids."""
        _root, ws = scratch
        assert _allowed(cmd, ws) is True, _reason(cmd, ws)


class TestBothReadingsRefuseIt:
    """The property `tests/test_sandbox_omitted_workdir.py` established for the
    move-out check (issue #1359) has to survive this one: an omitted workdir
    must never permit what a declared one refuses.

    With no declared workspace the base is this process's cwd — the directory
    `execute()` hands the child as `cwd=None` — so both readings resolve the same
    target the same way, and both refuse a climb out of the base.
    """

    @pytest.mark.parametrize("row", _ESCAPE_ROWS)
    def test_omitting_the_workdir_does_not_permit_the_escape(self, scratch, row):
        _root, ws = scratch
        cmd, _created = row
        assert _allowed(cmd, ws) is False, "control: the declared reading refuses"
        assert _allowed(cmd, None) is False, (
            "an omitted workdir let a relative climb through (issue #1359's "
            "property, regressed by the #1353 fix)"
        )

    @pytest.mark.parametrize("cmd", _ALLOWED_ROWS)
    def test_the_ordinary_relative_rows_are_unchanged_without_a_workdir(self, scratch, cmd):
        """Negative control in the other direction: the omitted reading must keep
        allowing every relative target that stays under the directory the child
        runs in — this file's fixture makes that directory `ws`."""
        _root, ws = scratch
        assert _allowed(cmd, ws) is True
        cwd = os.getcwd()
        try:
            os.chdir(ws)
            assert _allowed(cmd, None) is True, _reason(cmd, None)
        finally:
            os.chdir(cwd)
