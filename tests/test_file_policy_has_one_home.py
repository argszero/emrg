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
import inspect
import os
import tempfile
from pathlib import Path

import pytest

from emrg.sandbox import fence
from emrg.sandbox.fence import file_refusal
from emrg.sandbox.policy import DANGER_FULL_ACCESS, resolve_policy
from emrg.tools import bash_tool, edit_tool, file_policy, write_tool

#: The helpers the *mechanics* keep in ``emrg/tools/file_policy.py``: where a
#: relative spelling resolves, what "inside" means, and the host's protected
#: files. The policy itself is no longer one of them.
MECHANICS = ("resolve_file_target", "is_within", "is_absolute_path", "protected_paths")


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("name", (*MECHANICS,))
def test_the_home_defines_the_helper(name):
    assert callable(getattr(file_policy, name))


@pytest.mark.parametrize("name", (*MECHANICS, "file_refusal"))
def test_the_legacy_file_does_not_define_or_re_export_it(name):
    """No definition site and no re-export in the file #1675 deletes.

    Both halves matter: a **definition** here would be a second implementation,
    and a re-export would let an importer keep naming the legacy module and go
    unnoticed when that module is deleted.
    """
    source = inspect.getsource(bash_tool)
    assert f"def {name}(" not in source, f"bash_tool.py grew its own {name}"
    assert not hasattr(bash_tool, name), f"bash_tool.py re-exports {name}"


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
        policy = resolve_policy(mode="workspace-write", workspace_root="/tmp/emrg-not-granted")
        assert file_refusal(os.path.join(granted, "f.txt"), policy) is None
        refusal = file_refusal("/tmp/emrg-not-granted/f.txt", policy)
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
        policy = resolve_policy(mode=mode, workspace_root="/tmp/emrg-invariant-ws")
        for target in (
            "/tmp/emrg-invariant-ws/f.txt",
            "/tmp/emrg-invariant-outside.txt",
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
    policy = resolve_policy(mode=None, workspace_root="/tmp/emrg-silence")
    assert policy.mode == DANGER_FULL_ACCESS
    assert file_refusal("/etc/hosts", policy) is None


def test_the_scanners_helpers_are_the_home_s_own_objects():
    """The legacy scan and the file tools cannot drift: one implementation.

    The scanner keeps private aliases (it had the public names) so its ~30 call
    sites did not have to move in this step; an alias rebound to a *copy* would
    be exactly the drift the move removes.
    """
    assert bash_tool._is_within is file_policy.is_within
    assert bash_tool._is_absolute_path is file_policy.is_absolute_path
    assert bash_tool._protected_paths is file_policy.protected_paths
    assert bash_tool._trusted_write_zones is file_policy.trusted_write_zones
    assert bash_tool._temp_write_roots is file_policy.temp_write_roots
    assert bash_tool._PROTECTED_FILES is file_policy.PROTECTED_FILES
    # The platform axis is read from the home, never re-bound here: a copy is a
    # second value with its own lifetime, and it cannot see a seam that moves the
    # home (the failure `tests/test_windows_path_tokens.py` records).
    assert not hasattr(bash_tool, "_WINDOWS_SHELL")
