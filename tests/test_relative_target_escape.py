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

from emrg.tools import bash_tool as bt
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


@pytest.fixture(autouse=True)
def _pinned_write_roots(monkeypatch):
    """Pin the guard's write roots, so this file's verdict is about the tree.

    The rule pinned here (issue #1353) is a containment question — does the
    resolved target stay under the directory the command runs in — but the
    allowance it is measured against is the union of that directory with
    `_trusted_write_zones()` and `_temp_write_roots()`. The second of those is the
    OS temp area, so when the checkout itself is materialised *under*
    `tempfile.gettempdir()` a climb out of `<scratch>/a/b/ws` lands inside a root
    the guard trusts, and the guard allows the write by its own rule — correctly,
    because the resolved file really is inside a zone it permits.

    That makes the file's verdict depend on where the tree was materialised, which
    is not a property of the tree. Measured before this pin: 23 passed in the
    repository and **9 failed** (3 rows × 3 classes) for the identical tree
    materialised under `tempfile.gettempdir()`. That is not hypothetical —
    `scripts/check-merge-plan-suite.py` builds the tree a merge would land under
    exactly that root, so every plan on this master read FAILED while the product
    was correct.

    The scratch living under `tests/` (rather than under `tmp_path`, see the
    module docstring) is what keeps the *default* materialisation out of the temp
    root; it does not help once the checkout is there. Pinning removes the ambient
    variable rather than the claim: what this file measures is the resolved-target
    containment rule, and the temp/trusted-root policy has its own tests. Both
    roots are pinned, not just the temp one, because the trusted zone is
    HOME-dependent the same way.
    """
    monkeypatch.setattr(bt, "_temp_write_roots", lambda: set(), raising=True)
    monkeypatch.setattr(bt, "_trusted_write_zones", lambda: set(), raising=True)


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
            ["/bin/sh", "-c", cmd],
            cwd=ws,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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


def test_the_write_roots_are_pinned_for_this_file():
    """A silent removal of the pin must fail here, not pass everywhere else."""
    assert bt._temp_write_roots() == set()
    assert bt._trusted_write_zones() == set()


def test_a_write_root_containing_the_tree_would_swallow_the_rows(monkeypatch):
    """The mechanism the pin defends against, driven with the root made explicit.

    Stated as a measurement rather than as a comment, so that a later change which
    makes the containment verdict independent of the write roots fails here
    instead of quietly retiring the pin.
    """
    ambient = tempfile.mkdtemp(dir=_TESTS_DIR, prefix="emrg-relative-escape-")
    try:
        ws = os.path.join(ambient, "a", "b", "ws")
        os.makedirs(ws)
        cmd, _created = _ESCAPE_ROWS[2]
        assert _allowed(cmd, ws) is False, "the pinned reading"
        # A root that contains the tree is what flips this verdict: the climb now
        # resolves inside a zone the guard trusts, which is what a checkout under
        # the OS temp root does to this file's rows for real.
        monkeypatch.setattr(
            bt, "_temp_write_roots", lambda: {os.path.realpath(_TESTS_DIR)}
        )
        assert _allowed(cmd, ws) is True, (
            "a write root containing the tree is what flips this verdict, so the "
            "pin is what keeps these rows about the rule rather than about the "
            "directory the tree happens to sit in"
        )
    finally:
        shutil.rmtree(ambient, ignore_errors=True)
