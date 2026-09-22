"""The bash-tool-v2 policy layer: vocabulary, roots, and the confinement seam.

Rant ``2026-09-21T18:50:03`` ("bash tool v2"), design
``/Users/argszero/.emrg/designs/bash-tool-v2-design.md`` §5.2/§5.3.  These tests
need no sandbox backend and no spawn: they pin the words, the derivations and the
fail-closed contract that the boundary tests in ``test_bash_v2_boundary.py``
then exercise for real.
"""

import os
import re
import sys
import tempfile

import pytest

from emrg.sandbox.contract import (
    SANDBOX_UNAVAILABLE,
    SandboxUnavailableError,
    confine,
    host_platform,
    sandbox_denial_marker,
    sandbox_unavailable_message,
)
from emrg.sandbox.policy import (
    DANGER_FULL_ACCESS,
    DEFAULT_MODE,
    SANDBOX_MODES,
    SandboxPolicy,
    resolve_policy,
)
from emrg.sandbox.providers import PLATFORM_CHAINS, select_runner, unconfined_mode
from emrg.sandbox.providers.darwin import SEATBELT_EXEC, seatbelt_profile_args
from emrg.sandbox.roots import canonical_path, writable_roots
from emrg.tools import bash_tool

#: An absolute path on **every** platform.  ``"/tmp"`` is not one: the policy
#: layer asserts absoluteness, and ``os.path.isabs("/tmp")`` is False on Windows
#: (measured by this file's own first Windows CI run), so a test that spells it
#: would pass on macOS and fail on a Windows runner.  ``os.sep`` is the root this
#: host actually has.
ABSOLUTE_ROOT = os.path.abspath(os.sep)


def _grants(profile_args: list[str]) -> list[str]:
    """The path grants one Seatbelt profile makes, read back out of the profile.

    A round trip rather than a substring match, and the difference matters: a
    profile is a *parsed* language, so the honest assertion is "this path is
    granted, once the quoting is undone".  A substring test has to re-spell the
    escaping, and would therefore also pass an arm that stopped escaping a path
    containing a quote (measured on the Windows runner, where a ``\\`` path
    turned the re-spelled expectation into a false red).
    """
    profile = " ".join(profile_args)
    raw_grants = re.findall(r'\((?:subpath|literal) ("(?:[^"\\]|\\.)*")\)', profile)
    return [raw[1:-1].replace('\\"', '"').replace("\\\\", "\\") for raw in raw_grants]


# ── vocabulary ────────────────────────────────────────────────────────────


def test_mode_vocabulary_is_pinned_to_the_frozen_tool():
    """The two tools must speak one mode list while they run in parallel.

    The parallel period's whole hazard is drift: one word list, two
    implementations, and a model-visible contract that quietly means different
    things depending on which executor the switch built.  The old definition
    disappears with its file at P7; until then this pins them equal.
    """
    assert SANDBOX_MODES == bash_tool.SANDBOX_MODES


def test_bare_call_keeps_the_mode_a_host_session_already_had():
    """A call carrying no tier stays unconfined.

    The blueprint defaults a deployment to ``read-only``; EMRG's tier comes from
    the *task* configuration, and a call that carries none is a host session —
    which the old tool ran unconfined.  Mapping that silence to ``read-only``
    would confiscate a session nobody asked to confine (design §1.3).
    """
    assert DEFAULT_MODE == DANGER_FULL_ACCESS
    assert resolve_policy(workspace_root=ABSOLUTE_ROOT).mode == DANGER_FULL_ACCESS


def test_denial_marker_vocabulary_is_one_line_for_both_enforcing_families():
    """The model must recognize a denial identically at either layer."""
    assert sandbox_denial_marker("read-only") == "[sandbox: file access denied under read-only mode]"


def test_host_platform_speaks_the_chain_table_vocabulary():
    """No translation layer is introduced to be wrong later.

    ``sys.platform`` already says ``darwin``/``linux``/``win32`` — the exact keys
    the chain table uses — so a mapping table would be a second place to be
    wrong.  ``windows-2025`` CI is what makes this worth pinning: a translation
    written for ``"win32"`` that the host actually reports as ``"windows"`` would
    fail closed on every Windows run.
    """
    assert host_platform() == sys.platform


# ── SandboxPolicy ─────────────────────────────────────────────────────────


def test_policy_rejects_an_unknown_mode():
    with pytest.raises(ValueError, match="unknown mode"):
        SandboxPolicy(mode="mostly-safe", workspace_root=ABSOLUTE_ROOT)


def test_policy_rejects_a_relative_workspace_root():
    """Absolute-path assertion at the policy layer, canonicalization at the provider."""
    with pytest.raises(ValueError, match="absolute"):
        SandboxPolicy(mode="workspace-write", workspace_root="relative/dir")


def test_policy_keeps_the_workspace_root_under_modes_that_do_not_use_it():
    """The caller can resolve one policy before choosing the enforcement path."""
    policy = SandboxPolicy(mode="read-only", workspace_root=ABSOLUTE_ROOT)
    assert policy.workspace_root == ABSOLUTE_ROOT
    assert policy.session_id is None


# ── roots ─────────────────────────────────────────────────────────────────


def test_read_only_grants_no_writable_root():
    assert writable_roots(SandboxPolicy(mode="read-only", workspace_root=ABSOLUTE_ROOT)) == []
    assert writable_roots(SandboxPolicy(mode=DANGER_FULL_ACCESS, workspace_root=ABSOLUTE_ROOT)) == []


def test_workspace_write_grants_the_workspace_and_the_temp_areas(tmp_path):
    roots = writable_roots(SandboxPolicy(mode="workspace-write", workspace_root=str(tmp_path)))
    assert canonical_path(str(tmp_path)) in roots
    assert canonical_path(tempfile.gettempdir()) in roots
    assert len(roots) == len(set(roots)), "roots must be deduplicated"


def test_canonical_path_resolves_symlinks(tmp_path):
    """Granting a root *as spelled* matches nothing — measured (design §3.5)."""
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without the symlink privilege
        pytest.skip(f"this host cannot create a directory symlink ({exc}), so the property is unmeasurable")
    assert canonical_path(str(link)) == os.path.realpath(str(target))


def test_canonical_path_never_invents_a_path_for_a_missing_root():
    """A missing root matches nothing until it exists — never invent a fallback.

    ``realpath`` canonicalizes the prefix that exists and leaves the missing leaf
    as spelled (measured: ``/var/...`` becomes ``/private/var/...`` while the leaf
    survives).  Resolution failing outright returns the spelling unchanged; either
    way nothing is conjured, because a conjured path would grant something the
    caller never named.
    """
    missing = os.path.join(tempfile.gettempdir(), "emrg-v2-does-not-exist-9f2c")
    canonical = canonical_path(missing)
    assert os.path.basename(canonical) == "emrg-v2-does-not-exist-9f2c"
    assert canonical == os.path.join(canonical_path(tempfile.gettempdir()), "emrg-v2-does-not-exist-9f2c")
    assert canonical_path("\0not a path") == "\0not a path"


def test_the_deleted_trusted_write_zone_is_not_carried_over_under_another_name():
    """Host decision D5: ``~/.emrg/evolution/.emrg/`` must not reappear as a grant.

    The former ``_trusted_write_zones()`` was deleted, not renamed, so the check
    that it stays deleted belongs to a test rather than to a spare field.
    """
    policy = SandboxPolicy(mode="workspace-write", workspace_root=str(tempfile.gettempdir()))
    roots = writable_roots(policy)
    assert not any(".emrg" in root for root in roots)


# ── seam: confine() ───────────────────────────────────────────────────────


def test_confine_wraps_the_exact_argv_and_never_re_parses_the_command():
    """The command survives as ONE argv element: no second parse, no re-quoting.

    This is the property the whole v2 exists for (blueprint §3.2, 11/11): a
    shell string that is re-parsed is a shell string that can be re-interpreted.
    """
    command = "printf '%s\\n' \"a b\" 'c\"d' e\\ f"
    confined = confine(
        ["bash", "-c", command],
        SandboxPolicy(mode="workspace-write", workspace_root=ABSOLUTE_ROOT),
        platform_name="darwin",
    )
    assert confined.argv[-3:] == ["bash", "-c", command]
    assert confined.argv[-4] == "--", "the caller argv must follow the runner's own separator"


def test_confine_fails_closed_when_this_platform_has_no_backend():
    """A request for confinement that cannot be honoured must not run unconfined."""
    with pytest.raises(SandboxUnavailableError) as excinfo:
        confine(["bash", "-c", "echo hi"], SandboxPolicy(mode="workspace-write", workspace_root=ABSOLUTE_ROOT),
                platform_name="win32")
    assert excinfo.value.code == SANDBOX_UNAVAILABLE
    assert excinfo.value.mode == "workspace-write"
    assert "workspace-write" in str(excinfo.value)


def test_confine_selects_the_platform_it_is_told_and_not_the_host():
    """Injectable selection is what makes a chain testable anywhere."""
    assert "win32" not in PLATFORM_CHAINS
    confined = confine(["bash", "-c", "echo hi"], SandboxPolicy(mode="read-only", workspace_root=ABSOLUTE_ROOT),
                       platform_name="darwin")
    assert confined.argv[0] == SEATBELT_EXEC


def test_the_refusal_names_what_the_operator_can_do_about_it():
    message = sandbox_unavailable_message("read-only")
    assert 'sandbox mode "read-only"' in message
    assert "refusing to run the command unconfined" in message
    assert "danger-full-access" in message


def test_the_refusal_carries_the_runners_own_fatal_line_when_one_was_captured():
    message = sandbox_unavailable_message("read-only", "sandbox-exec: bad profile")
    assert message.endswith("Runner failure: sandbox-exec: bad profile")


def test_a_sole_candidate_is_selected_without_a_probe():
    """Probing arbitrates; it does not re-validate a choice that has no alternative."""
    assert len(PLATFORM_CHAINS["darwin"]) == 1
    assert select_runner("read-only", platform_name="darwin").name == "seatbelt"


def test_select_runner_refuses_an_unknown_platform():
    with pytest.raises(SandboxUnavailableError):
        select_runner("read-only", platform_name="plan9")


def test_danger_full_access_is_short_circuited_before_any_provider():
    """No provider is consulted, so the result carries no enforcement claim."""
    assert unconfined_mode(DANGER_FULL_ACCESS, platform_name="darwin") == DANGER_FULL_ACCESS
    assert unconfined_mode("read-only", platform_name="darwin") is None


def test_linux_reports_no_boundary_rather_than_one_it_cannot_honour():
    """Host-authorised deviation D4 (2026-09-21 18:29): the Linux chain has no artifact yet.

    The honest report is that Linux has no boundary at all — a falsified
    ``enforcement`` field is worse than an absent one.  Exit condition: P3 lands
    and this test is deleted with the arm it pins.
    """
    assert unconfined_mode("workspace-write", platform_name="linux") == DANGER_FULL_ACCESS
    assert "linux" not in PLATFORM_CHAINS


# ── darwin profile ────────────────────────────────────────────────────────


def test_seatbelt_grants_only_the_derived_roots(tmp_path):
    policy = SandboxPolicy(mode="workspace-write", workspace_root=str(tmp_path))
    profile = " ".join(seatbelt_profile_args(policy))
    assert "(deny file-write*)" in profile
    grants = _grants(seatbelt_profile_args(policy))
    assert canonical_path(str(tmp_path)) in grants
    assert "/dev/null" in grants


def test_seatbelt_denies_every_write_under_read_only(tmp_path):
    """read-only has no grant line at all — only /dev/null is writable."""
    profile = " ".join(seatbelt_profile_args(SandboxPolicy(mode="read-only", workspace_root=str(tmp_path))))
    allows = [form for form in profile.split("(allow file-write*")[1:]]
    assert len(allows) == 1, f"read-only must grant nothing but /dev/null, got {allows}"
    assert "/dev/null" in allows[0]
    assert str(tmp_path) not in profile


def test_seatbelt_quotes_a_path_containing_a_quote(tmp_path):
    """A profile is a parsed language: an unescaped quote in a path breaks it.

    Read back through :func:`_grants`, so the assertion is "the path survives the
    round trip" rather than "the escaping was spelled the way this test expects".
    """
    hostile = str(tmp_path / 'we"ird')
    args = seatbelt_profile_args(SandboxPolicy(mode="workspace-write", workspace_root=hostile))
    assert hostile in _grants(args)
