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
from emrg.sandbox.providers import (
    PLATFORM_CHAINS,
    linux,
    select_runner,
    unconfined_mode,
)
from emrg.sandbox.providers.darwin import SEATBELT_EXEC, seatbelt_profile_args
from emrg.sandbox.providers.linux import bwrap_profile_args
from emrg.sandbox.roots import canonical_path, writable_roots
from emrg.tools import bash_tool

#: An absolute path on **every** platform.  ``"/tmp"`` is not one: the policy
#: layer asserts absoluteness, and ``os.path.isabs("/tmp")`` is False on Windows
#: (measured by this file's own first Windows CI run), so a test that spells it
#: would pass on macOS and fail on a Windows runner.  ``os.sep`` is the root this
#: host actually has.
ABSOLUTE_ROOT = os.path.abspath(os.sep)

#: How the Windows rung's runner is invoked.  Spelled out here rather than read
#: back from the provider: a test that asks the code under test what it does can
#: only ever agree with it.
WINDOWS_ACL_INVOCATION = [sys.executable, "-m", "emrg.sandbox.win32.runner"]


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


def test_a_host_with_no_usable_temp_area_grants_what_it_can_compute(monkeypatch, tmp_path):
    """The derivation is total: an unreportable temp source is absent, not fatal (#1561).

    ``tempfile.gettempdir()`` is a *probe* — CPython creates a file to find a writable
    candidate and raises ``FileNotFoundError`` when none of them is. Before this,
    the raise escaped ``writable_roots`` (``emrg/sandbox/roots.py``) and reached the
    caller as a bare ``FileNotFoundError``: not the ``SandboxUnavailableError``
    ``confine`` documents, and not anything the tools' ``except OSError`` around the
    *spawn* can see, because ``confine`` is called outside it. The blueprint
    (``roots.ts:52-55``) calls Node's ``os.tmpdir()``, which is total, so the port has
    to be total as well — and the only non-inventing way is to grant the sources that
    resolve.

    Measured (2026-09-24, this host): a host really can be in this state — inside a
    ``read-only`` seatbelt child a bare ``gettempdir()`` raises — which is why this is
    a guard rather than a curiosity.
    """
    policy = SandboxPolicy(mode="workspace-write", workspace_root=str(tmp_path))

    def _no_temp_area():  # pragma: no cover - the raise IS the subject
        raise FileNotFoundError(2, "No usable temporary directory found in [...]")

    monkeypatch.setattr(tempfile, "gettempdir", _no_temp_area)

    roots = writable_roots(policy)
    assert canonical_path(str(tmp_path)) in roots, (
        "a host with no temp area must still be granted its workspace root — the grant "
        "is narrower, never absent"
    )
    assert len(roots) == len(set(roots)), roots
    # No spelling is conjured to replace the probe: the process cwd is Node's own
    # last-resort fallback and is deliberately *not* ported, because it would grant a
    # root the caller never named (``canonical_path``'s stated rule).
    assert canonical_path(os.getcwd()) not in roots, roots

    # The seam the issue reproduced on: ``confine`` must not raise for a cause that is
    # not "no backend can enforce the mode". The darwin provider is pure argv building
    # (no host probe), so this asserts the same thing on every platform.
    confined = confine(["echo", "ok"], policy, platform_name="darwin")
    assert canonical_path(str(tmp_path)) in " ".join(confined.argv), (
        "the workspace-root grant must survive into the profile the seam builds"
    )


def test_the_modes_that_never_probe_are_unaffected_by_a_missing_temp_area(monkeypatch):
    """The negative control: the probe is only reached under ``workspace-write``.

    ``read-only`` and ``danger-full-access`` return before it, so injecting the failure
    globally must leave them returning ``[]`` rather than raising — otherwise the guard
    above would be measuring the injection rather than the fix.
    """

    def _no_temp_area():  # pragma: no cover - the raise IS the subject
        raise FileNotFoundError(2, "No usable temporary directory found in [...]")

    monkeypatch.setattr(tempfile, "gettempdir", _no_temp_area)
    assert writable_roots(SandboxPolicy(mode="read-only", workspace_root=ABSOLUTE_ROOT)) == []
    assert writable_roots(SandboxPolicy(mode=DANGER_FULL_ACCESS, workspace_root=ABSOLUTE_ROOT)) == []


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


def test_canonical_path_returns_the_spelling_when_resolution_fails(monkeypatch):
    """The documented fallback, tested where it is the *only* thing that happens.

    ``realpath`` is tolerant by construction, so the refusal is rare and
    platform-shaped — which is why this drives it directly instead of hunting for
    a spelling that makes the host give up.  (A spelling that makes *POSIX* raise
    is a NUL byte, but Windows merely prefixes the cwd for it, so a test built on
    one would measure `ntpath.abspath` rather than this function's contract.)
    """
    spelling = "unresolvable\\path"

    def _refuse(path):  # pragma: no cover - the raise IS the subject
        raise OSError("cannot resolve")

    monkeypatch.setattr(os.path, "realpath", _refuse)
    assert canonical_path(spelling) == spelling


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
    """A request for confinement that cannot be honoured must not run unconfined.

    The platform is a synthetic one, deliberately: as the chain table fills up, a
    real platform's name stops meaning "no rung" and this test would silently
    start measuring something else (``win32`` was that name until P4).
    """
    with pytest.raises(SandboxUnavailableError) as excinfo:
        confine(["bash", "-c", "echo hi"], SandboxPolicy(mode="workspace-write", workspace_root=ABSOLUTE_ROOT),
                platform_name="freebsd")
    assert excinfo.value.code == SANDBOX_UNAVAILABLE
    assert excinfo.value.mode == "workspace-write"
    assert "workspace-write" in str(excinfo.value)


def test_confine_selects_the_platform_it_is_told_and_not_the_host():
    """Injectable selection is what makes a chain testable anywhere.

    The name used is the table's entry that is *not* this host's: naming the
    host's own platform would pass even if the parameter were ignored, which is
    the very property under test.
    """
    other = "darwin" if host_platform() == "win32" else "win32"
    confined = confine(["bash", "-c", "echo hi"], SandboxPolicy(mode="read-only", workspace_root=ABSOLUTE_ROOT),
                       platform_name=other)
    expected = [SEATBELT_EXEC] if other == "darwin" else WINDOWS_ACL_INVOCATION
    assert confined.argv[: len(expected)] == expected


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


def test_linux_selects_its_sole_rung_without_a_probe():
    """P3: the linux chain is ``bwrap`` alone, and the deviation D4 is gone.

    The blueprint's linux chain is ``['bwrap', 'landlock']`` and probes exist
    only to arbitrate between candidates; the landlock launcher is still a
    missing artifact (blueprint §1.5 B1), so this chain has one rung and is
    selected the way darwin's is.  Two halves, because either can go wrong
    alone: the chain must carry the rung, and the rung must be *usable* as a
    runner — a chain that named a backend which cannot confine anything would
    read as coverage while failing closed on every real command.

    D4 ("run unconfined and report ``danger-full-access`` on linux") is deleted
    by its own exit condition, and this asserts its absence: a linux host
    without ``bwrap`` must fail closed rather than run bare.
    """
    assert [runner.name for runner in PLATFORM_CHAINS["linux"]] == ["bwrap"]
    assert select_runner("read-only", platform_name="linux") is linux.BWRAP
    assert unconfined_mode("workspace-write", platform_name="linux") is None
    assert unconfined_mode("read-only", platform_name="linux") is None
    # The blueprint's own short-circuit still stands on every platform.
    assert unconfined_mode(DANGER_FULL_ACCESS, platform_name="linux") == DANGER_FULL_ACCESS


def test_linux_reaches_the_seam_instead_of_being_answered_first():
    """The half D4 used to answer: this platform is now confined like any other.

    Under D4 the consumer returned before ``confine()`` was called, so the seam
    never saw a linux policy.  Now it does, and the wrapping is the same shape
    darwin gets: the runner, its profile, the trailing ``--`` and the caller's
    exact argv.  The *fail-closed* half of "a host without bwrap" cannot be
    asserted here — ``confine`` builds an argv and spawns nothing — so it lives
    one layer out, in ``test_bash_v2_boundary.py``, where a real spawn is refused.
    """
    confined = confine(
        ["bash", "-c", "true"],
        SandboxPolicy(mode="read-only", workspace_root=ABSOLUTE_ROOT),
        platform_name="linux",
    )
    assert confined.argv[0] == linux.BWRAP_BIN
    assert "bwrap" in confined.argv[0]
    assert confined.argv[-4:] == ["--", "bash", "-c", "true"]
    assert confined.enforcement == "full"
    assert confined.denial_signatures == linux.DENIAL_SIGNATURES


def test_linux_profile_is_the_blueprint_mount_table(tmp_path):
    """The mount profile, argument for argument, from ``profiles.ts::bwrapProfileArgs``.

    Pinned as a *sequence* rather than a set: the order carries meaning here —
    ``--tmpfs /tmp`` before ``--bind <workspace>`` is what keeps a workspace
    living under ``/tmp`` writable (measured in the container, 2026-09-22: the
    mount added last wins).
    """
    workspace = str(tmp_path)
    read_only = bwrap_profile_args(SandboxPolicy(mode="read-only", workspace_root=workspace))
    assert read_only == [
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--unshare-pid",
        "--proc", "/proc",
        "--die-with-parent",
    ], "read-only is the bare profile: no writable mount at all"

    writable = bwrap_profile_args(SandboxPolicy(mode="workspace-write", workspace_root=workspace))
    assert writable == read_only + ["--tmpfs", "/tmp", "--bind", workspace, workspace]

    # Not the Seatbelt grant list: a mount namespace can give the sandbox its
    # own /tmp, and the blueprint does.  A drift to "grant the host's /tmp"
    # would be invisible in the profile above without this assertion.
    assert "--tmpfs" in writable and writable.count("/tmp") == 1


def test_the_linux_tables_are_the_blueprints_rows():
    """Enforcement, denial dialect and runner-failure rule for this rung.

    Each row is the one ``sandbox-local/src/index.ts`` carries for ``bwrap``:
    ``full`` (the profile is the mount table, so the claim is a profile fact),
    the EROFS string the kernel raises against the read-only mount, and a
    signature-only failure rule — ``bwrap`` exits 1 on its own fatal paths, but
    so does any command that fails, so an exit status here would misread a
    confined command's own failure as "the command never ran".
    """
    assert linux.ENFORCEMENT == "full"
    assert linux.DENIAL_SIGNATURES == ("read-only file system",)
    assert len(linux.RUNNER_FAILURE_RULES) == 1
    rule = linux.RUNNER_FAILURE_RULES[0]
    assert rule.fatal_signatures == ("bwrap: ",)
    assert rule.allowed_exit_codes is None, "an exit status is not evidence here (see docstring)"
    assert linux.BWRAP.runner_argv is linux.runner_argv


def test_the_linux_runner_argv_prepends_the_program(tmp_path):
    policy = SandboxPolicy(mode="workspace-write", workspace_root=str(tmp_path))
    argv = linux.runner_argv(policy)
    assert argv[0] == linux.BWRAP_BIN
    assert argv[1:] == bwrap_profile_args(policy)


# ── win32 rung ────────────────────────────────────────────────────────────


def test_the_windows_chain_is_the_acl_restricted_token_backend():
    """One candidate, selected without a probe — and it is the ACL backend.

    The name matters, not the count: Windows has exactly one way to confine a
    process from Python here (an ACL-restricted token), and a second candidate
    would have to be arbitrated by a probe that does not exist yet.
    """
    assert len(PLATFORM_CHAINS["win32"]) == 1
    assert select_runner("read-only", platform_name="win32").name == "windows-acl"


def test_the_windows_rung_claims_partial_enforcement_because_that_is_what_it_has():
    """``WRITE_RESTRICTED`` cannot be the absolute promise, so it must not be spelled as one.

    Everyone has to sit in both restricting lists for the restricted child to
    initialize at all, so an object granting Everyone write access stays writable,
    and an NTFS hard link can alias a granted file to a path outside the
    workspace.  The blueprint's own row is ``partial``
    (``sandbox-local/src/index.ts:177-187``); a ``full`` here would be a claim the
    result surface then repeats to the model.
    """
    window = select_runner("read-only", platform_name="win32")
    assert window.enforcement == "partial"
    assert window.enforcement != select_runner("read-only", platform_name="darwin").enforcement


def test_a_confined_windows_run_still_carries_the_exact_argv_behind_the_separator():
    """The seam's one job, through the other rung: no second parse, no re-quoting.

    ``read-only`` is the mode this test can use on any host: it renders no
    capability SIDs, so no Win32 call is made and no grant is materialized.
    """
    command = "printf '%s\\n' \"a b\" 'c\"d'"
    confined = confine(
        ["bash", "-c", command],
        SandboxPolicy(mode="read-only", workspace_root=ABSOLUTE_ROOT),
        platform_name="win32",
    )
    assert confined.argv[-4:] == ["--", "bash", "-c", command]
    assert "--write-sid" not in confined.argv and "--temp-write-sid" not in confined.argv
    assert confined.enforcement == "partial"


def test_the_windows_refusal_names_the_runner_the_operator_can_fix():
    """The fail-closed message is the same one, and it names this platform's rung."""
    assert "restricted-token runner" in sandbox_unavailable_message("workspace-write")


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
