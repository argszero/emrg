"""The in-process file policy has exactly one home (P7, issues #1675 / #1553).

The two tool families answer "may this write happen?" through two mechanisms: the
process-boundary tool through the kernel fence (a Seatbelt profile / bwrap bind
set built from ``emrg/sandbox/roots.py``), and the ``write``/``edit`` tools
through a predicate that runs inside the daemon.

Until the host ruling of **2026-09-28T21:50** that predicate was the file tools'
*own* answer, and it disagreed with the kernel fence in both directions at
``read-only`` (bash could write nowhere while the file tools could write
anywhere outside the workspace) and by one root at ``workspace-write``. The
ruling — one policy, both families — is implemented by asking
:func:`emrg.sandbox.fence.file_refusal`, whose only input is
:func:`emrg.sandbox.roots.writable_roots`: the same list the profile is built
from. So the home is ``emrg/sandbox/fence.py``, and the two gates that used to
live in ``emrg/tools/file_policy.py`` (and before that in the legacy file #1675
deletes) are gone rather than moved a third time.

A move is easy to undo by accident: a later change can re-add a copy in the
legacy file, make one of the tools answer for itself, or give the fence a second
input. These tests pin the home, the binding and the single input, and the last
ones inject an *independent answer* to show the tools really read the fence —
i.e. that a divergence would be visible here rather than silent.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import tempfile
from pathlib import Path

import pytest

from emrg.sandbox import fence
from emrg.sandbox.fence import file_refusal
from emrg.sandbox.policy import DANGER_FULL_ACCESS, resolve_policy
from emrg.tools import edit_tool, file_policy, write_tool

#: An absolute path on **every** platform, and outside every temp root it grants.
#: ``"/tmp"`` is neither: ``resolve_policy`` refuses a workspace root that is not
#: absolute in the execution world (`policy.py`), ``os.path.isabs("/tmp")`` is
#: False on Windows, and pytest's temp base *is* ``/tmp/...`` on a Linux runner,
#: i.e. a root ``workspace-write`` grants. Three tests in this file spelled
#: ``/tmp/...`` and the Windows leg reported all three (CI run 36432808100:
#: ``ValueError: … workspace_root must be an absolute execution-world path``) —
#: the same constant ``tests/test_bash_v2_policy.py`` uses, for the same reason.
ABSOLUTE_ELSEWHERE = os.path.join(os.path.abspath(os.sep), "emrg-not-granted")

#: The helpers the *mechanics* keep in ``emrg/tools/file_policy.py``: where a
#: relative spelling resolves, what "inside" means, and the host's protected
#: files. The policy itself is no longer one of them.
MECHANICS = ("resolve_file_target", "is_within", "is_absolute_path", "protected_paths")


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("name", (*MECHANICS,))
def test_the_home_defines_the_helper(name):
    assert callable(getattr(file_policy, name))


@pytest.mark.parametrize(
    "name", ("resolve_file_target", "is_within", "is_absolute_path")
)
def test_the_legacy_scan_is_gone_rather_than_re_exported(name):
    """The file #1675 deletes is deleted, and its scan did not move to the fence.

    This used to assert *about* the legacy module — that it neither defined nor
    re-exported these names. Deleting the module makes that assertion
    unstateable, and the property it protected is the one worth keeping: the
    legacy scanner (``_check_sandbox`` and its static word lists) must not come
    back, which is the negative guard the v2 design asks for (§7, row 20). A
    re-export or a re-creation is the shape a later change reaches for, so both
    are refused here — the module by the filesystem and the finder, the
    mechanics by the module that is supposed to be their only home.

    The two names the fence legitimately carries are excluded, each for its own
    reason: ``file_refusal`` is the fence's own entry point and the subject of
    this whole file, and ``protected_paths`` is *imported* from the home rather
    than copied (asserted below), which is the one-spelling rule rather than a
    violation of it. Importing a mechanic is likewise legitimate and is asserted
    to be the home's own object, so a copy cannot pass as an import.
    """
    module_file = Path(file_policy.__file__).with_name("bash_tool.py")
    assert not module_file.exists(), f"the legacy scan came back: {module_file}"
    assert importlib.util.find_spec("emrg.tools.bash_tool") is None
    for module in (edit_tool, write_tool):
        if hasattr(module, name):
            assert getattr(module, name) is getattr(file_policy, name), (
                f"{module.__name__} has its own {name}, not the home's"
            )


@pytest.mark.parametrize("name", ("check_read_only_file_write", "check_workspace_write",
                                  "_check_sandbox", "_extract_write_targets"))
def test_the_legacy_predicates_are_gone_everywhere(name):
    """The names the deletion removed must not reappear under any module."""
    for module in (file_policy, fence, edit_tool, write_tool):
        assert not hasattr(module, name), f"{module.__name__} re-binds {name}"


def test_the_fence_imports_the_home_s_protected_paths():
    """The fence names the host's protected files through the home, not around it."""
    assert fence.protected_paths is file_policy.protected_paths


@pytest.mark.parametrize(
    "name", ("check_read_only_file_write", "check_workspace_write")
)
def test_the_file_tools_own_gates_are_gone(name):
    """The divergence is deleted, not parked somewhere new.

    Each of these answered for the file tools alone. Leaving either defined —
    even unread — is the shape a later change re-imports, and the whole point of
    the ruling is that there is no second answer to keep in step.
    """
    assert not hasattr(file_policy, name), f"a second answer came back: {name}"
    assert not hasattr(write_tool, name)
    assert not hasattr(edit_tool, name)


@pytest.mark.parametrize("tool", [write_tool, edit_tool])
def test_the_file_tools_are_bound_to_the_fence(tool):
    """Bound to the fence's own function object — not to a local copy."""
    assert tool.file_refusal is fence.file_refusal


def test_the_fence_has_one_input_the_shared_derivation(monkeypatch):
    """Whatever the tier says, the verdict follows ``writable_roots`` and nothing else.

    The injection is the proof: a scratch directory that is *not* the policy's
    workspace root becomes writable when the derivation grants it, and the
    policy's own workspace root stops being writable when the derivation does
    not — so a second input (a workspace test, a protected list, a mode branch of
    its own) would show up here as an answer the derivation did not produce.
    """
    with tempfile.TemporaryDirectory() as granted_dir:
        granted = os.path.realpath(granted_dir)
        monkeypatch.setattr(fence, "writable_roots", lambda policy: [granted])
        not_granted = ABSOLUTE_ELSEWHERE
        policy = resolve_policy(mode="workspace-write", workspace_root=not_granted)
        assert file_refusal(os.path.join(granted, "f.txt"), policy) is None
        refusal = file_refusal(os.path.join(not_granted, "f.txt"), policy)
        assert refusal is not None, "the fence honoured the workspace root on its own"


@pytest.mark.parametrize("tool", [write_tool.WriteTool, edit_tool.EditTool])
def test_an_independent_answer_reaches_the_tool(tool):
    """Each tool's verdict follows the fence — proven by inverting it.

    This is the injection point acceptance 3 of #1675 asks about: if a future
    change makes a tool answer for itself, a fence that says "blocked" stops
    being visible, and this asserts the opposite (a blocked answer for a target
    the fence would allow).
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "f.txt"
        target.write_text("keep\n", encoding="utf-8")
        original = getattr(write_tool, "file_refusal")
        setattr(write_tool if tool is write_tool.WriteTool else edit_tool,
                "file_refusal", lambda path, policy: "blocked by the arm")
        try:
            result = _run(
                tool().execute(
                    {
                        "file_path": str(target),
                        "content": "x",
                        "old_string": "keep",
                        "new_string": "gone",
                        "workspace": tmp,
                        "sandbox": "workspace-write",
                    }
                )
            )
        finally:
            setattr(write_tool if tool is write_tool.WriteTool else edit_tool,
                    "file_refusal", original)
        assert result.error
        assert "blocked by the arm" in result.content
        assert target.read_text() == "keep\n", "the tool acted after refusing"


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_the_two_tools_state_the_same_answer(mode, tmp_path):
    """One tier, two tools, one verdict — measured in both directions.

    Byte equality on the refusal is the strongest form of "they share a policy"
    a test can state, and it is the property the divergence violated: the two
    tools agreed with each other but not with the kernel fence. The second half
    is the control — a target the tier *does* grant is refused by neither tool —
    so this cannot pass by both tools refusing everything.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("keep\n", encoding="utf-8")
    # Outside every root any tier grants: not the workspace, not the OS temp
    # areas (so not `tmp_path`, which lives under one of them). Never written —
    # a fence that stopped refusing would reach a directory this test cannot
    # create, which is why the row is safe even with the fence under test.
    outside = str(Path(os.sep) / "emrg-1553-shared-answer" / "f.txt")
    args = {"sandbox": mode, "workspace": str(workspace)}

    write_refusal = _run(
        write_tool.WriteTool().execute({"file_path": outside, "content": "x", **args})
    )
    edit_refusal = _run(
        edit_tool.EditTool().execute(
            {"file_path": outside, "old_string": "keep", "new_string": "gone", **args}
        )
    )
    assert write_refusal.error and edit_refusal.error
    assert write_refusal.content == edit_refusal.content

    write_granted = _run(
        write_tool.WriteTool().execute({"file_path": str(inside), "content": "x\n", **args})
    )
    edit_granted = _run(
        edit_tool.EditTool().execute(
            {"file_path": str(inside), "old_string": "keep", "new_string": "kept", **args}
        )
    )
    # `read-only` grants nothing, so even the file inside the workspace is
    # refused — the tier that used to mean "blocked inside, allowed outside" now
    # means "blocked", for both tools. `workspace-write` grants the workspace.
    # Read as "did the fence refuse this?" rather than "did the call succeed?":
    # a granted target can still fail later for its own reason (an `old_string`
    # the write half just overwrote), and that failure is not this test's subject.
    refused = mode == "read-only"
    for result in (write_granted, edit_granted):
        assert ("sandbox" in result.content) is refused, result.content


def test_the_fence_is_never_looser_than_the_derivation():
    """Every allowed target is inside a granted root — the invariant, measured.

    The one exception the fence carries on purpose is the host's protected
    daemon state (``file_policy.protected_paths``), which it refuses even when a
    granted root would contain it. That is the *safe* direction — the fence can
    refuse a write bash would make, never permit one bash could not — and it is
    asserted here so the exception cannot quietly become the other kind.
    """
    for mode in ("read-only", "workspace-write"):
        workspace = ABSOLUTE_ELSEWHERE
        outside = f"{ABSOLUTE_ELSEWHERE}-outside"
        policy = resolve_policy(mode=mode, workspace_root=workspace)
        for target in (
            os.path.join(workspace, "f.txt"),
            os.path.join(outside, "f.txt"),
            os.path.expanduser("~/.emrg/config.toml"),
            "/etc/hosts",
        ):
            verdict = file_refusal(target, policy)
            if verdict is None:
                from emrg.sandbox.roots import canonical_path, writable_roots

                roots = writable_roots(policy)
                assert any(
                    canonical_path(target) == root
                    or canonical_path(target).startswith(root.rstrip(os.sep) + os.sep)
                    for root in roots
                ), f"{target} was allowed but no root grants it"


def test_the_silence_keeps_its_meaning():
    """A call carrying no tier is unconfined, for the file tools as for the shell.

    ``DEFAULT_MODE`` is ``danger-full-access`` and a host session is the caller
    that arrives with silence; mapping it to ``read-only`` would confiscate a
    session nobody asked to confine (``emrg/sandbox/policy.py``).
    """
    policy = resolve_policy(mode=None, workspace_root=ABSOLUTE_ELSEWHERE)
    assert policy.mode == DANGER_FULL_ACCESS
    assert file_refusal("/etc/hosts", policy) is None


def test_the_mechanics_have_no_second_spelling():
    """The legacy scan's private aliases die with it; the home keeps the only copies.

    ``emrg/tools/bash_tool.py`` held private aliases of these helpers
    (``_is_within``, ``_protected_paths``, …) so its ~30 call sites could stay
    put through the move. Those call sites are gone, so an alias anywhere is now
    a second name for the same value — exactly the drift the single home exists
    to remove. The platform axis is the sharpest case: it was re-bound in the
    scanner as a module constant, which is a second value with its own lifetime
    and cannot see a seam that moves the home (the failure
    ``tests/test_windows_path_tokens.py`` recorded before its own deletion).
    """
    assert file_policy.WINDOWS_SHELL is (os.name == "nt")
    for module in (file_policy, fence, write_tool, edit_tool):
        for alias in ("_is_within", "_is_absolute_path", "_protected_paths",
                      "_trusted_write_zones", "_temp_write_roots", "_WINDOWS_SHELL"):
            assert not hasattr(module, alias), f"{module.__name__} re-binds {alias}"
