"""The in-process file policy has exactly one home (P7, issue #1675).

The two tool families answer "may this write happen?" through two mechanisms: the
process-boundary tool through the kernel fence, the ``write``/``edit`` tools
through in-process predicates. The predicates used to live in
``emrg/tools/bash_tool.py`` — the legacy shell tool #1675 deletes — and the point
of moving them to ``emrg/sandbox/file_policy.py`` is that the home is **not** the
file being deleted.

A move is easy to undo by accident: a later change can re-add a copy in the
legacy file (the divergence comes back, now with two spellings), or make one of
the tools answer for itself. These tests pin the home and the binding, and the
last one injects an *independent answer* to show the file tools really do read
the policy — i.e. that a divergence would be visible here rather than silent.

What is deliberately **not** asserted: which semantics the file tools *should*
have at ``read-only``. The two families disagree there today, measured in
``check_read_only_file_write``'s docstring, and choosing a side is the open
decision #1553 carries — the tests pin the fact, not a preference.
"""

from __future__ import annotations

import asyncio
import inspect
import tempfile
from pathlib import Path

import pytest

from emrg.sandbox import file_policy
from emrg.tools import bash_tool, edit_tool, write_tool

PREDICATES = ("check_read_only_file_write", "check_workspace_write", "resolve_file_target")


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("name", PREDICATES)
def test_the_home_defines_the_predicate(name):
    assert callable(getattr(file_policy, name))


@pytest.mark.parametrize("name", PREDICATES)
def test_the_legacy_file_does_not_define_or_re_export_it(name):
    """No definition site and no re-export in the file #1675 deletes.

    Both halves matter: a **definition** here would be a second implementation,
    and a re-export would let an importer keep naming the legacy module and go
    unnoticed when that module is deleted.
    """
    source = inspect.getsource(bash_tool)
    assert f"def {name}(" not in source, f"bash_tool.py grew its own {name}"
    assert not hasattr(bash_tool, name), f"bash_tool.py re-exports {name}"


@pytest.mark.parametrize("tool", [write_tool, edit_tool])
@pytest.mark.parametrize("name", PREDICATES)
def test_the_file_tools_are_bound_to_the_home(tool, name):
    """Bound to the policy's own function objects — not to a local copy."""
    assert getattr(tool, name) is getattr(file_policy, name)


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
    assert bash_tool._WINDOWS_SHELL is file_policy.WINDOWS_SHELL


def test_an_independent_answer_reaches_the_tool(monkeypatch):
    """The write tool's verdict follows the policy — proven by inverting it.

    This is the injection point acceptance 3 of #1675 asks about: if a future
    change makes the write tool answer for itself, a policy that says "blocked"
    stops being visible, and this asserts the opposite (blocked answers for a
    target the policy would allow).
    """
    monkeypatch.setattr(
        write_tool, "check_workspace_write", lambda target, workspace: "blocked by the arm"
    )
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "f.txt"
        result = _run(
            write_tool.WriteTool().execute(
                {
                    "file_path": str(target),
                    "content": "x",
                    "workspace": tmp,
                    # The gate is tier-gated (it is asked only when the daemon
                    # injected a tier), so the arm has to arrive at that tier.
                    "sandbox": "workspace-write",
                }
            )
        )
    assert result.error
    assert "blocked by the arm" in result.content
    assert not target.exists(), "the tool refused after writing"


def test_the_divergence_the_move_preserved_is_still_measured():
    """The two families disagree at ``read-only``, and the move kept it.

    Behaviour was moved verbatim on purpose: choosing a side is #1553's decision,
    not this step's. Stated as an assertion so that a *silent* change of the
    file-tool semantics here fails loudly, and whoever changes it has to say so.
    """
    from emrg.sandbox.roots import writable_roots
    from emrg.sandbox.policy import resolve_policy

    outside = "/etc/emrg-file-policy-probe"  # no workspace declared: no boundary
    assert file_policy.check_read_only_file_write(outside, None) is None
    assert file_policy.check_workspace_write(outside, "/tmp/ws") is not None
    assert writable_roots(resolve_policy(mode="read-only", workspace_root="/tmp/ws")) == []
