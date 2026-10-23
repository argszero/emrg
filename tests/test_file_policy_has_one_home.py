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
MECHANICS = ("resolve_file_target", "is_within", "is_absolute_path", "protected_paths",
             "same_file", "protected_hit")


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


def test_the_fence_asks_the_home_which_files_are_protected():
    """The fence names the host's protected files through the home, not around it.

    The name this pins moved when the comparison became an identity one (#2551):
    the fence used to import the *list* and compare canonical strings, and it now
    imports the lookup the home states the rule in. The requirement is unchanged
    — one home, and the fence bound to the home's own object rather than a copy.
    """
    assert fence.protected_hit is file_policy.protected_hit


def _fence_with_protected(monkeypatch, protected: Path, workspace: Path):
    """A ``workspace-write`` policy whose protected list is one temporary file.

    ``protected_paths()`` is derived from the *real* home, and no test here may
    touch it; the seam is the home's own function, so what a test arms is a list
    of temporary paths and the derivation keeps its own guard elsewhere. The
    workspace is the same temporary directory on purpose: the file tools' fence
    is deliberately stricter than containment, and a target the workspace grants
    is the only place that difference is visible.
    """
    monkeypatch.setattr(
        file_policy, "protected_paths", lambda: [os.path.realpath(protected)]
    )
    return resolve_policy(mode="workspace-write", workspace_root=str(workspace))


def test_a_second_name_for_a_protected_file_is_refused(tmp_path, monkeypatch):
    """One file, one verdict — by identity, not by the spelling that reached it.

    A hard link asks this question with case folding taken out: two names, one
    inode, and ``realpath`` returns each name as written, so the string
    comparison this replaces answered "inside the workspace and not on the list"
    and allowed the write. No volume can help here — a hard link is one file on
    every filesystem this repo runs on — so this is the arm that holds on a
    case-sensitive runner too (#2551).
    """
    victim = tmp_path / "config.toml"
    victim.write_text("host state", encoding="utf-8")
    alias = tmp_path / "not-the-config"
    os.link(victim, alias)
    policy = _fence_with_protected(monkeypatch, victim, tmp_path)

    assert file_refusal(str(victim), policy) is not None
    refusal = file_refusal(str(alias), policy)

    assert refusal is not None and "protected daemon file" in refusal
    # A judgment names the file it judged: the alias and the protected file
    # differ in spelling, and a refusal printing only the spelling would send its
    # reader looking for a second file that is not there.
    #
    # The clause is read, not grepped for the bare path. The message renders the
    # resolved identity with `!r`, so on Windows it carries escaped backslashes
    # and `realpath` is *not* a substring of it: the bare-path form of this
    # assertion passed here and failed `test-windows` (measured on #2552's run
    # 39962749524, `1 failed, 5346 passed`). Comparing the clause with the same
    # rendering asks the question the sentence asks — is the file it names the
    # one that was reached — on every platform.
    resolved = refusal.split("(resolves to ", 1)[1].rstrip(")")
    assert resolved == repr(os.path.realpath(victim)), refusal

    # The same construction on a file the list does not name. Identity is what
    # the answer is keyed to, so a second name for an ordinary file stays
    # writable: the arm is "this IS a protected file", never "this file has two
    # names" — and that direction is measurable on every volume, unlike the
    # case-folding one below.
    mine = tmp_path / "notes.md"
    mine.write_text("mine", encoding="utf-8")
    mine_alias = tmp_path / "notes-alias.md"
    os.link(mine, mine_alias)
    assert file_refusal(str(mine_alias), policy) is None


def test_a_case_variant_spelling_of_a_protected_file_is_refused(tmp_path, monkeypatch):
    """The same question on the volume the defect was reported against.

    The volume is asked, not assumed: where it keeps the two spellings apart they
    are two files and the old answer was already right, so the case is skipped
    with that reason. On a volume that folds them — APFS's default, and NTFS —
    ``CONFIG.TOML`` is ``config.toml``, and it was *allowed* before this fix
    while its own spelling was refused (#2551, requirement 1).
    """
    victim = tmp_path / "config.toml"
    victim.write_text("host state", encoding="utf-8")
    variant = tmp_path / "CONFIG.TOML"
    if not file_policy.same_file(str(variant), str(victim)):
        pytest.skip("this volume keeps CONFIG.TOML and config.toml apart as two files")
    policy = _fence_with_protected(monkeypatch, victim, tmp_path)

    refusal = file_refusal(str(variant), policy)

    assert refusal is not None and "protected daemon file" in refusal


def test_a_distinct_file_whose_name_differs_only_in_case_stays_writable(
    tmp_path, monkeypatch
):
    """The other direction, and why the mechanism is identity rather than lowering.

    On a volume that tells the two names apart, ``notes.md`` beside ``NOTES.md``
    is a second file the session owns, and refusing it would be the false
    positive that makes a case-insensitive comparison unusable on Linux:
    ``normcase`` is the identity function on POSIX, so a blanket ``.lower()`` is
    the shape this test refuses (#2551, requirement 2). Skipped where the volume
    folds the names, because then there is no second file to ask about.
    """
    victim = tmp_path / "config.toml"
    victim.write_text("host state", encoding="utf-8")
    sibling = tmp_path / "NOTES.md"
    sibling.write_text("mine", encoding="utf-8")
    shout = tmp_path / "notes.md"
    if file_policy.same_file(str(shout), str(sibling)):
        pytest.skip("this volume folds case, so notes.md and NOTES.md are one file")
    policy = _fence_with_protected(monkeypatch, victim, tmp_path)

    assert file_refusal(str(shout), policy) is None


def test_a_target_that_is_not_there_still_gets_a_verdict(tmp_path, monkeypatch):
    """An identity test cannot compare what is absent, and that must not read as a pass.

    ``same_file`` answers False for a path with no inode, which leaves the target
    to the containment rule it was always judged by: inside the workspace it is
    writable, outside every granted root it is refused, and neither answer raises
    (#2551, requirement 3).
    """
    victim = tmp_path / "config.toml"
    victim.write_text("host state", encoding="utf-8")
    policy = _fence_with_protected(monkeypatch, victim, tmp_path)

    assert file_refusal(str(tmp_path / "not-there-yet.txt"), policy) is None
    outside = os.path.join(ABSOLUTE_ELSEWHERE, "not-there-either.txt")
    refusal = file_refusal(outside, policy)
    assert refusal is not None and "outside every root" in refusal


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
