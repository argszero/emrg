"""The Windows rung's own logic: capability SIDs, grant lifetimes, argv, parsing.

Design ``/Users/argszero/.emrg/designs/bash-tool-v2-design.md`` §5.7; the port of
``packages/sandbox/sandbox-windows-acl`` (deepseek-harness 0.1.6-alpha.2).

**Why most of this file runs on macOS.** The Windows *mechanism* — creating the
restricted token and applying the DACL — needs the Win32 API, so its end-to-end
measurement is the Windows-only test at the bottom. Everything above that line is
the layer P4 adds to *this* project and it is ordinary Python, but it is not
decoration: the capability-SID derivation decides who can write where, the two
grant lifetimes decide what outlives a session, and the policy→argv rendering
decides what the runner is even told. A defect in any of them ships silently on
every platform, because no platform here can exercise the mechanism for real.
The DACL application itself is the blueprint's own code path, ported rather than
re-invented.
"""

import ctypes
import gc
import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

from emrg.sandbox.policy import SandboxPolicy
from emrg.sandbox.providers import win32 as provider
from emrg.sandbox.roots import canonical_path
from emrg.sandbox.win32 import ffi
from emrg.sandbox.win32.ffi import SIDAndAttributes
from emrg.sandbox.win32.runner import (
    RUNNER_FAILURE_EXIT,
    RUNNER_SIGNATURE,
    START_ANNOUNCEMENT,
    RunnerFailure,
    _build_sandbox,
    announce,
    fail,
    git_safety_env,
    main,
    note_cleanup_failure,
    parse_args,
    run,
)
from emrg.sandbox.win32.sandbox import AclSandbox
from emrg.sandbox.win32.token import TokenGroups, find_logon_sid, make_well_known_sid
from emrg.sandbox.win32.sid import (
    assert_private_temp_disjoint,
    assert_temp_root_outside_workspace,
    contains_directory,
    root_write_sid,
    temp_write_sid,
    workspace_write_sid,
)
from emrg.tools.shell_env import runner_import_env

#: A capability SID's string shape: ``S-1-4-<sub>-<sub>`` and the temp form's
#: domain-separating ``-1``.
WORKSPACE_SID_SHAPE = re.compile(r"^S-1-4-(\d+)-(\d+)$")
TEMP_SID_SHAPE = re.compile(r"^S-1-4-(\d+)-(\d+)-1$")

#: The sub-authority ceiling the derivation reduces into (30-bit).
SUB_AUTHORITY_CEILING = 2**30 - 1


# ── capability SIDs ───────────────────────────────────────────────────────


def test_the_workspace_sid_is_deterministic_so_its_ace_is_a_reuse_cache():
    """One workspace, one identity, for every session and every process.

    This is what makes the standing ACE O(1) after the first provision: a
    derivation that moved between runs would re-propagate the whole tree on every
    provision, and the grant's exact-ACE skip would never fire.
    """
    assert workspace_write_sid("/work/tree") == workspace_write_sid("/work/tree")
    assert workspace_write_sid("/work/tree") != workspace_write_sid("/work/other")


def test_the_two_capabilities_differ_for_the_same_directory():
    """A temp SID can never collide with a workspace SID of the same path.

    Its third sub-authority is fixed at ``-1`` and its seed is domain-separated,
    so the temp capability cannot be satisfied by the workspace's ACE (which would
    make the private temp tree shared with every sibling session).
    """
    path = "/work/tree"
    assert workspace_write_sid(path) != temp_write_sid(path)
    assert WORKSPACE_SID_SHAPE.match(workspace_write_sid(path))
    assert TEMP_SID_SHAPE.match(temp_write_sid(path))


def test_every_derived_sub_authority_is_inside_the_30_bit_range():
    """The ceiling is not cosmetic: the token and ACE layers carry 30-bit values."""
    for path in ("/a", "/b", "/work/tree", "C:\\work"):
        for sid in (workspace_write_sid(path), temp_write_sid(path)):
            for sub in re.findall(r"\d+", sid.split("S-1-4-", 1)[1]):
                assert 1 <= int(sub) <= SUB_AUTHORITY_CEILING, sid


def test_contains_directory_is_the_boundary_both_assertions_are_built_on(tmp_path):
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    (tmp_path / "sibling").mkdir()
    assert contains_directory(str(root), str(root)) is True
    assert contains_directory(str(root), str(child)) is True
    assert contains_directory(str(root), str(tmp_path / "sibling")) is False
    # A prefix that is not a path component is not containment.
    (tmp_path / "rootish").mkdir()
    assert contains_directory(str(root), str(tmp_path / "rootish")) is False


def test_a_temp_root_inside_the_workspace_is_refused(tmp_path):
    """Every child below such a parent would inherit the *standing* workspace ACE.

    So the refusal is what keeps the private temp private, and it is checked
    before any directory is created rather than after.
    """
    workspace = tmp_path / "ws"
    (workspace / "inner").mkdir(parents=True)
    with pytest.raises(ValueError, match="outside the workspace"):
        assert_temp_root_outside_workspace(str(workspace), str(workspace / "inner"))
    with pytest.raises(ValueError, match="outside the workspace"):
        assert_temp_root_outside_workspace(str(workspace), str(workspace))
    assert_temp_root_outside_workspace(str(workspace), str(tmp_path))  # no raise


def test_a_private_temp_overlapping_a_writable_dir_is_refused_in_both_directions(tmp_path):
    """Either inheritance direction merges two capabilities into one identity."""
    writable = tmp_path / "ws"
    (writable / "temp").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    with pytest.raises(ValueError, match="disjoint"):
        assert_private_temp_disjoint([str(writable)], str(writable / "temp"))
    with pytest.raises(ValueError, match="disjoint"):
        assert_private_temp_disjoint([str(writable / "temp")], str(writable))
    assert_private_temp_disjoint([str(tmp_path / "outside")], str(writable / "temp"))


# ── the two grant lifetimes ───────────────────────────────────────────────


class _FakeGrant:
    """Records what a real ``AclWriteGrant`` would apply and revoke."""

    def __init__(self, write_sid: str) -> None:
        self.write_sid = write_sid
        self.added: list[tuple[str, bool]] = []
        self.disposed = False

    @classmethod
    def create(cls, write_sid: str, api: object | None = None) -> "_FakeGrant":
        return cls(write_sid)

    def add(self, path: str, *, standing: bool = False) -> None:
        failing = CONTROLLER["fail_workspace_add"] if standing else CONTROLLER["fail_temp_add"]
        if failing:
            raise RuntimeError("SetNamedSecurityInfoW failed (fake)")
        self.added.append((path, standing))

    def dispose(self) -> None:
        if CONTROLLER["fail_dispose"]:
            raise RuntimeError("revoke failed (fake)")
        self.disposed = True


#: One mutable switch the tests flip instead of building a fake per test.  The two
#: apply failures are separate because the two branches clean up differently.
CONTROLLER: dict[str, bool] = {
    "fail_temp_add": False,
    "fail_workspace_add": False,
    "fail_dispose": False,
}


def _clear_controller() -> None:
    CONTROLLER.update(fail_temp_add=False, fail_workspace_add=False, fail_dispose=False)


@pytest.fixture
def store(monkeypatch):
    """A grant store with the Win32-facing grant replaced, plus its instances.

    The binding table is a bare object because the fake never calls it: what is
    under test is the store's own decisions — which paths get which kind of ACE,
    what is reused, and what a release actually revokes.
    """
    made: list[_FakeGrant] = []
    _clear_controller()

    def _make(write_sid, api=None):
        grant = _FakeGrant(write_sid)
        made.append(grant)
        return grant

    monkeypatch.setattr(provider, "AclWriteGrant", type("F", (), {"create": staticmethod(_make)}))
    built = provider.GrantStore(api=object())
    yield built, made
    _clear_controller()
    try:
        built.clear()
    except BaseException:  # pragma: no cover - only when a test armed fail_dispose
        pass


def test_the_workspace_ace_is_standing_and_materialized_once_per_workspace(store, tmp_path):
    """The cross-session reuse cache: one standing ACE, one private temp per pair."""
    built, made = store
    workspace, temps = _workspace_and_temps(tmp_path)
    first = built.materialize("s1", str(workspace), temp_root=str(temps))
    standing = [grant for grant in made if grant.added == [(str(workspace), True)]]
    assert len(standing) == 1, "the workspace ACE is standing, and there is exactly one"

    assert first.directory.startswith(str(temps))
    assert os.path.basename(first.directory).startswith(provider.TEMP_PREFIX)
    assert os.path.isdir(first.directory)
    temp_grant = next(grant for grant in made if grant.write_sid == first.write_sid)
    assert temp_grant.added == [(first.directory, False)], "the temp ACE is revocable, never standing"

    # Same pair: the same capability, not a second one.
    assert built.materialize("s1", str(workspace), temp_root=str(temps)) is first

    # A second session sharing the workspace: a new private temp, no new ACE.
    second = built.materialize("s2", str(workspace), temp_root=str(temps))
    assert second.directory != first.directory
    assert len([grant for grant in made if grant.added == [(str(workspace), True)]]) == 1


def test_release_revokes_one_sessions_temp_and_leaves_the_standing_ace(store, tmp_path):
    """The lifetime split, measured on the two paths it separates."""
    built, made = store
    workspace, temps = _workspace_and_temps(tmp_path)
    first = built.materialize("s1", str(workspace), temp_root=str(temps))
    second = built.materialize("s2", str(workspace), temp_root=str(temps))

    built.release_session("s1")

    first_grant = next(grant for grant in made if grant.write_sid == first.write_sid)
    second_grant = next(grant for grant in made if grant.write_sid == second.write_sid)
    assert first_grant.disposed is True
    assert not os.path.exists(first.directory), "a revoked temp capability must not leave its directory"
    assert second_grant.disposed is False, "another session's capability is not this release's business"
    assert os.path.isdir(second.directory)
    assert built.materialize("s2", str(workspace), temp_root=str(temps)) is second
    assert all(
        not grant.disposed for grant in made if grant.added == [(str(workspace), True)]
    ), "revoking a standing workspace ACE would force the next provision to re-propagate the tree"


def test_release_does_not_reach_a_session_whose_id_merely_starts_the_same(store, tmp_path):
    """``s1`` must not release ``s10``: the key is encoded, not concatenated.

    Measured because the key is built by string surgery on ``json.dumps``, and the
    obvious concatenation would make one session's release revoke another's temp
    capability — a capability the other session is still running under.
    """
    built, made = store
    workspace, temps = _workspace_and_temps(tmp_path)
    ten = built.materialize("s10", str(workspace), temp_root=str(temps))
    one = built.materialize("s1", str(workspace), temp_root=str(temps))
    assert json.loads(json.dumps(["s1", str(workspace)]))[0] == "s1"  # the key's own vocabulary

    built.release_session("s1")

    assert next(g for g in made if g.write_sid == one.write_sid).disposed is True
    assert next(g for g in made if g.write_sid == ten.write_sid).disposed is False
    assert os.path.isdir(ten.directory)


def test_a_temp_grant_that_could_not_be_applied_is_revoked_and_removed(store, tmp_path):
    """Fail closed: a half-materialized capability must not look like a whole one."""
    built, made = store
    workspace, temps = _workspace_and_temps(tmp_path)
    CONTROLLER["fail_temp_add"] = True
    with pytest.raises(RuntimeError, match="SetNamedSecurityInfoW failed"):
        built.materialize("s1", str(workspace), temp_root=str(temps))
    assert list(temps.iterdir()) == [], "the directory of a failed grant must not survive it"
    workspace_grant = next(grant for grant in made if grant.added == [(str(workspace), True)])
    assert workspace_grant.disposed is False, "the standing ACE is the intended end state, not an artifact"


def test_a_cleanup_failure_during_a_failed_materialization_is_reported_not_swallowed(store, tmp_path):
    """The aggregate counts both failures, because one of them is a security fact."""
    built, _ = store
    workspace, temps = _workspace_and_temps(tmp_path)
    CONTROLLER.update(fail_temp_add=True, fail_dispose=True)
    with pytest.raises(RuntimeError, match="its cleanup also failed") as excinfo:
        built.materialize("s1", str(workspace), temp_root=str(temps))
    assert [type(failure).__name__ for failure in excinfo.value.failures] == ["RuntimeError"]
    assert list(temps.iterdir()) == [], "the directory is removed even when the revocation failed"


def test_a_workspace_ace_that_could_not_be_applied_is_cleaned_up_and_not_recorded(store, tmp_path):
    """A workspace grant that failed is never cached as if it had succeeded."""
    built, made = store
    workspace, temps = _workspace_and_temps(tmp_path)
    CONTROLLER["fail_workspace_add"] = True
    with pytest.raises(RuntimeError, match="SetNamedSecurityInfoW failed"):
        built.materialize("s1", str(workspace), temp_root=str(temps))
    failed = made[0]
    assert failed.added == [] and failed.disposed is True, "the failed grant is revoked before the raise"

    CONTROLLER["fail_workspace_add"] = False
    capability = built.materialize("s1", str(workspace), temp_root=str(temps))
    assert os.path.isdir(capability.directory)
    assert len([grant for grant in made if grant.added == [(str(workspace), True)]]) == 1, (
        "the retry applies the standing ACE rather than trusting a cache entry from the failed attempt"
    )


def test_a_workspace_cleanup_failure_that_itself_fails_is_raised_as_such(store, tmp_path):
    """Both the apply and the cleanup failing is a different error, and says so.

    The wording is the *standing* grant's rather than the workspace's: one helper
    now materializes the workspace's ACE and every host-named root's, and a
    message naming the workspace would be read as a root it never mentioned.
    ``test_an_extra_root_ace_that_could_not_be_applied_is_cleaned_up_and_not_cached``
    is the other half — the same helper, reached through the other caller.
    """
    built, _ = store
    workspace, temps = _workspace_and_temps(tmp_path)
    CONTROLLER.update(fail_workspace_add=True, fail_dispose=True)
    with pytest.raises(RuntimeError, match="standing grant failed and its cleanup also failed"):
        built.materialize("s1", str(workspace), temp_root=str(temps))


def test_a_host_named_root_gets_its_own_standing_ace_once(store, tmp_path):
    """One capability SID and one standing ACE per host-named root (rant §7).

    Both spellings item 7 asks for are covered, because the two go down the same
    path and differ only in what the host pointed at: a directory, granted as a
    tree by the inheritable ACE, and a **single file**, which Windows grants as
    the object itself.  The measurement that matters is the *identity*: the SID is
    a pure function of the path, so a root that got the workspace's derivation (or
    the temp's) would hand this session a capability nobody asked for, and the
    assertion below names which derivation each path must carry.
    """
    built, made = store
    workspace, temps = _workspace_and_temps(tmp_path)
    directory = tmp_path / "outside"
    directory.mkdir()
    single = tmp_path / "outside.txt"
    single.write_text("host\n", encoding="utf-8")
    roots = (str(directory), str(single))

    built.materialize("s1", str(workspace), temp_root=str(temps), extra_roots=roots)

    standing = {grant.added[0][0]: grant for grant in made if grant.added and grant.added[0][1]}
    assert set(standing) == {str(workspace), canonical_path(str(directory)), canonical_path(str(single))}
    assert standing[canonical_path(str(directory))].write_sid == root_write_sid(canonical_path(str(directory)))
    assert standing[canonical_path(str(single))].write_sid == root_write_sid(canonical_path(str(single)))
    assert standing[str(workspace)].write_sid == workspace_write_sid(str(workspace)), (
        "the workspace keeps its own derivation — the extra roots add identities, they do not replace one"
    )
    assert all(grant.disposed is False for grant in standing.values()), (
        "a standing ACE is the reuse cache: disposing it would force the next provision to rebuild the tree"
    )

    # A second session, and a second call in the first: the ACEs are reused, not
    # re-applied — the whole point of a path-keyed cache.
    before = len(made)
    built.materialize("s1", str(workspace), temp_root=str(temps), extra_roots=roots)
    built.materialize("s2", str(workspace), temp_root=str(temps), extra_roots=roots)
    assert [grant for grant in made[before:] if grant.added and grant.added[0][1]] == [], (
        "a standing ACE is materialized once per path per server lifetime"
    )

    # And a call that names no root does not conjure one: the roots are the
    # policy's, not the store's memory of a previous call.
    plain = built.materialize("s3", str(workspace), temp_root=str(temps))
    plain_grants = [grant for grant in made[before:] if grant.added and grant.added[0][1]]
    assert plain_grants == [], f"an unrooted call granted something ({plain_grants})"
    assert plain.directory.startswith(str(temps))


def test_an_extra_root_ace_that_could_not_be_applied_is_cleaned_up_and_not_cached(store, tmp_path):
    """Fail closed at the *root*: a session never runs under a capability it was half given.

    The order is deliberate and this test is what pins it — the roots are
    materialized before the workspace, so a root that cannot be granted stops the
    call before a temp directory exists to leak, and no cache entry is written for
    the failed path (a cached failure would be indistinguishable from a grant).
    """
    built, made = store
    workspace, temps = _workspace_and_temps(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = canonical_path(str(outside))
    CONTROLLER["fail_workspace_add"] = True
    with pytest.raises(RuntimeError, match="SetNamedSecurityInfoW failed"):
        built.materialize("s1", str(workspace), temp_root=str(temps), extra_roots=(str(outside),))
    failed = made[0]
    assert failed.write_sid == root_write_sid(root)
    assert failed.added == [] and failed.disposed is True, "the failed grant is revoked before the raise"
    assert list(temps.iterdir()) == [], "no private temp is created for a policy that could not be granted"

    CONTROLLER["fail_workspace_add"] = False
    capability = built.materialize("s1", str(workspace), temp_root=str(temps), extra_roots=(str(outside),))
    assert os.path.isdir(capability.directory)
    attempts = [grant.added for grant in made if grant.write_sid == root_write_sid(root)]
    assert attempts == [[], [(root, True)]], (
        f"the retry applies the standing ACE rather than trusting a cache entry from the failed attempt ({attempts})"
    )


def test_clear_reports_every_revocation_it_could_not_complete(store, tmp_path):
    """One failure must not hide the next: the count is what a caller acts on."""
    built, made = store
    workspace, temps = _workspace_and_temps(tmp_path)
    first = built.materialize("s1", str(workspace), temp_root=str(temps))
    second = built.materialize("s2", str(workspace), temp_root=str(temps))
    CONTROLLER["fail_dispose"] = True
    with pytest.raises(RuntimeError, match="2 failure") as excinfo:
        built.clear()
    assert [type(failure).__name__ for failure in excinfo.value.failures] == ["RuntimeError", "RuntimeError"]
    for capability in (first, second):
        assert not os.path.exists(capability.directory)
    assert made, "the fake's instances stay inspectable for the assertions above"


def test_a_temp_root_inside_the_workspace_stops_the_materialization(store, tmp_path):
    """The precondition is checked before a directory or an ACE exists."""
    built, made = store
    workspace = tmp_path / "ws"
    inside = workspace / "temp"
    inside.mkdir(parents=True)
    with pytest.raises(ValueError, match="outside the workspace"):
        built.materialize("s1", str(workspace), temp_root=str(inside))
    assert list(inside.iterdir()) == []
    assert [grant for grant in made if not grant.disposed and grant.added] == []


def _workspace_and_temps(tmp_path) -> tuple[object, object]:
    """A workspace and a host temp root, both outside each other."""
    workspace = tmp_path / "ws"
    temps = tmp_path / "temps"
    workspace.mkdir()
    temps.mkdir()
    return workspace, temps


# ── policy → runner argv ──────────────────────────────────────────────────


def test_the_runner_is_an_interpreter_entry_carrying_the_roots_and_the_mode():
    """A Python module entry, because argv shape is the contract the seam sees."""
    policy = SandboxPolicy(mode="read-only", workspace_root=os.path.abspath(os.sep))
    assert provider.runner_argv(policy) == [
        sys.executable,
        "-P",
        "-m",
        "emrg.sandbox.win32.runner",
        "--workspace",
        os.path.abspath(os.sep),
        "--temp",
        tempfile.gettempdir(),
        "--mode",
        "read-only",
    ]


def test_the_runner_argv_keeps_the_workdir_out_of_sys_path():
    """``-P``, and not the tempting neighbour that would break resolution instead.

    The seam spawns this runner with ``cwd`` set to the session's workdir, and
    ``-m`` resolves the module *through* ``sys.path``, whose head is that
    workdir.  A workdir that is itself an EMRG checkout — this project's own
    flagship session — then answers the import, and because a checkout need not
    carry ``emrg/sandbox`` the runner dies before it has spawned anything; the
    confined run reports it as the caller's command failing.  ``-P`` removes
    exactly that entry.  ``-I`` looks like the same thing and is not: it implies
    ``-E``, which ignores ``PYTHONPATH`` too, so ``emrg`` becomes unresolvable and
    the runner cannot start at all — which is why the seam also hands the runner
    its import root (``shell_env.runner_import_env``).
    """
    argv = provider.runner_invocation()
    assert argv[1] == "-P", argv
    assert "-I" not in argv and "-E" not in argv, argv
    assert argv[-2:] == ["-m", "emrg.sandbox.win32.runner"], argv


def test_a_session_run_hands_the_runner_both_capabilities_and_its_private_temp(store, monkeypatch, tmp_path):
    """The runner grants nothing itself when the seam already materialized the ACEs."""
    built, _ = store
    monkeypatch.setattr(provider, "STORE", built)
    workspace, _temps = _workspace_and_temps(tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "temps"))
    policy = SandboxPolicy(mode="workspace-write", workspace_root=str(workspace), session_id="s1")

    argv = provider.runner_argv(policy)

    capability = built.materialize("s1", str(workspace), temp_root=str(tmp_path / "temps"))
    assert argv[argv.index("--temp") + 1] == capability.directory
    assert argv[argv.index("--write-sid") + 1] == workspace_write_sid(str(workspace))
    assert argv[argv.index("--temp-write-sid") + 1] == temp_write_sid(capability.directory)
    assert argv[argv.index("--mode") + 1] == "workspace-write"


def test_an_agentless_run_gets_no_sid_flags_so_the_runner_owns_its_temp(store, monkeypatch, tmp_path):
    """No session, no standing key: the runner creates one private temp per spawn."""
    built, made = store
    monkeypatch.setattr(provider, "STORE", built)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    argv = provider.runner_argv(SandboxPolicy(mode="workspace-write", workspace_root=str(workspace)))
    assert "--write-sid" not in argv and "--temp-write-sid" not in argv
    assert made == [], "an agentless call materializes no grant for a session to release"


def test_the_runner_argv_carries_one_extra_root_flag_per_host_named_root(store, monkeypatch, tmp_path):
    """The host's roots reach the runner as paths, canonical and without duplicates.

    The flag carries no SID on purpose: the runner derives each root's capability
    SID from the path it is handed, so the ACE the seam materialized and the
    restricting list the token is built with cannot name two different roots.  A
    duplicated spelling would be the same root twice — one ACE, two identities in
    the token — which ``AclSandbox`` refuses; the de-duplication here is what
    keeps the refusal for a caller that *meant* one root twice.
    """
    built, made = store
    monkeypatch.setattr(provider, "STORE", built)
    workspace, temps = _workspace_and_temps(tmp_path)
    directory = tmp_path / "outside"
    directory.mkdir()
    single = tmp_path / "outside.txt"
    single.write_text("x", encoding="utf-8")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temps))
    policy = SandboxPolicy(
        mode="workspace-write",
        workspace_root=str(workspace),
        session_id="s1",
        extra_roots=(str(directory), str(single), str(directory) + os.sep + ".", str(directory)),
    )

    argv = provider.runner_argv(policy)

    flags = [argv[i + 1] for i, token in enumerate(argv) if token == "--extra-root"]
    assert flags == [
        canonical_path(str(directory)),
        canonical_path(str(single)),
    ], f"one canonical flag per distinct root, in the host's order ({flags})"
    assert argv[-1] == canonical_path(str(single)), "the flags sit after the SIDs, before the seam's --"
    assert argv[argv.index("--write-sid") + 1] == workspace_write_sid(str(workspace))
    # And the seam really materialized them, because the runner grants nothing
    # itself on this path (``manage_dacls=False``).
    standing = {grant.added[0][0] for grant in made if grant.added and grant.added[0][1]}
    assert standing == {str(workspace), canonical_path(str(directory)), canonical_path(str(single))}


def test_a_read_only_policy_never_hands_the_runner_an_extra_root(store, monkeypatch, tmp_path):
    """The tier's whole definition is that it grants no root — even one the session names.

    A stored root is legitimate at ``read-only`` (``roots.judge_root_addition``
    says so, and it takes effect when the tier flips), so the flag has to be
    dropped **here**, where the tier is known, rather than trusted not to arrive.
    """
    built, made = store
    monkeypatch.setattr(provider, "STORE", built)
    workspace, temps = _workspace_and_temps(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temps))

    argv = provider.runner_argv(
        SandboxPolicy(
            mode="read-only",
            workspace_root=str(workspace),
            session_id="s1",
            extra_roots=(str(outside),),
        )
    )

    assert "--extra-root" not in argv, argv
    assert "--write-sid" not in argv and "--temp-write-sid" not in argv, argv
    assert made == [], "read-only materializes nothing, not even for a root it was told about"


def test_an_agentless_run_still_carries_its_extra_roots(store, monkeypatch, tmp_path):
    """No session means the runner owns its DACLs — including the roots it was given.

    The two branches differ in who applies the ACE, never in what the policy
    granted: dropping the roots here would make a root writable for ``bash`` and
    not for the ``pwsh`` seam's own spawn, which is the asymmetry the single
    ``policy.extra_roots`` field exists to prevent.
    """
    built, made = store
    monkeypatch.setattr(provider, "STORE", built)
    workspace, temps = _workspace_and_temps(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temps))

    argv = provider.runner_argv(
        SandboxPolicy(mode="workspace-write", workspace_root=str(workspace), extra_roots=(str(outside),))
    )

    assert [argv[i + 1] for i, token in enumerate(argv) if token == "--extra-root"] == [
        canonical_path(str(outside))
    ]
    assert "--write-sid" not in argv, "an agentless run still gets no SID flags"
    assert made == [], "an agentless call materializes no grant for a session to release"


def test_temp_root_is_usable_answers_the_precondition_the_runner_asserts(monkeypatch, tmp_path):
    """The precondition is askable before a spawn, not only at it."""
    workspace = tmp_path / "ws"
    (workspace / "inner").mkdir(parents=True)
    assert provider.temp_root_is_usable(str(workspace)) is True
    assert provider.temp_root_is_usable(str(tmp_path)) is True
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(workspace / "inner"))
    assert provider.temp_root_is_usable(str(workspace)) is False


# ── the runner's own argument contract ────────────────────────────────────


def test_the_runner_parses_the_seam_argv_and_keeps_the_command_verbatim():
    parsed = parse_args(
        ["--temp", "/t", "--mode", "workspace-write", "--workspace", "/w", "--", "python", "-c", "a b c"]
    )
    assert (parsed.workspace, parsed.temp, parsed.mode) == ("/w", "/t", "workspace-write")
    assert parsed.command == "python"
    assert parsed.args == ["-c", "a b c"], "the command is never re-split into a shell string"
    assert parsed.write_sid is None and parsed.temp_write_sid is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([], "missing --workspace"),
        (["--workspace", "/w"], "missing --temp"),
        (["--workspace", "/w", "--temp", "/t"], "unknown mode: None"),
        (["--workspace", "/w", "--temp", "/t", "--mode", "danger-full-access"], "unknown mode"),
        (["--workspace"], "missing value after --workspace"),
        (["--nope", "x"], "unknown argument: --nope"),
        (["--workspace", "/w", "--temp", "/t", "--mode", "read-only"], "missing command after --"),
        (["--workspace", "/w", "--temp", "/t", "--mode", "read-only", "--"], "missing command after --"),
    ],
)
def test_a_malformed_argv_is_a_runner_failure(raw, expected):
    """Every malformed shape fails loudly rather than defaulting to something safe-looking."""
    with pytest.raises(RunnerFailure, match=re.escape(expected)):
        parse_args(raw)


def test_the_modes_contradicting_their_grants_are_refused(tmp_path):
    """The runner refuses a caller's grant vocabulary it cannot make sense of."""
    workspace, temps = _workspace_and_temps(tmp_path)
    sid = workspace_write_sid(str(workspace))
    temp_sid = temp_write_sid(str(temps))

    def build(*extra: str):
        return _build_sandbox(
            parse_args(
                ["--workspace", str(workspace), "--temp", str(temps), *extra, "--", "python"]
            )
        )

    with pytest.raises(RunnerFailure, match="read-only does not accept"):
        build("--mode", "read-only", "--write-sid", sid)
    with pytest.raises(RunnerFailure, match="together"):
        build("--mode", "workspace-write", "--write-sid", sid)
    with pytest.raises(RunnerFailure, match="together"):
        build("--mode", "workspace-write", "--temp-write-sid", temp_sid)
    with pytest.raises(RunnerFailure, match="does not match --workspace"):
        build("--mode", "workspace-write", "--write-sid", workspace_write_sid("/other"), "--temp-write-sid", temp_sid)
    with pytest.raises(RunnerFailure, match="does not match --temp"):
        build("--mode", "workspace-write", "--write-sid", sid, "--temp-write-sid", temp_write_sid("/other"))
    # The matching pair is accepted, and the runner then manages no DACL of its own.
    sandbox, owned = build("--mode", "workspace-write", "--write-sid", sid, "--temp-write-sid", temp_sid)
    assert (owned, sandbox.manage_dacls) == (None, False)
    assert sandbox.temp_dir == str(temps)


def test_the_runner_parses_the_extra_roots_in_order_and_derives_their_sids(tmp_path):
    """The runner reads paths, not SIDs — and closes the loop by deriving them.

    Two things are measured here.  ``--extra-root`` is **repeatable** and
    order-preserving, because the roots are a list rather than a set: the seam
    renders the policy in the host's order and this side must not shuffle it.  And
    each root's capability SID is derived from the path *in this process*, so the
    ACE (`grant_write` on the seam's side) and the restricting list (this token)
    are two readings of one string rather than two files agreeing about a string —
    the same shape as ``--write-sid``'s check, which fails loudly when the two
    disagree.
    """
    workspace, temps = _workspace_and_temps(tmp_path)
    directory = tmp_path / "outside"
    directory.mkdir()
    single = tmp_path / "outside.txt"
    single.write_text("x", encoding="utf-8")

    parsed = parse_args(
        [
            "--workspace", str(workspace),
            "--temp", str(temps),
            "--mode", "workspace-write",
            "--extra-root", str(directory),
            "--extra-root", str(single),
            "--", "python",
        ]
    )
    assert parsed.extra_roots == [str(directory), str(single)]

    sandbox, owned = _build_sandbox(parsed)
    assert owned is not None, "the runner owns its private temp when the seam hands it no SIDs"
    assert sandbox.extra_grants == [
        (str(directory), root_write_sid(str(directory))),
        (str(single), root_write_sid(str(single))),
    ], "a single file is a root like a directory — the SID is a function of the path, not of what it is"
    assert sandbox.manage_dacls is True, "with no SIDs from the seam, the runner applies these ACEs itself"


def test_an_extra_root_the_runner_cannot_find_is_a_runner_failure(tmp_path):
    """A root that does not exist must fail at the runner, never mid-child."""
    workspace, temps = _workspace_and_temps(tmp_path)
    with pytest.raises(RunnerFailure, match="--extra-root does not exist"):
        _build_sandbox(
            parse_args(
                [
                    "--workspace", str(workspace),
                    "--temp", str(temps),
                    "--mode", "workspace-write",
                    "--extra-root", str(tmp_path / "not-there"),
                    "--", "python",
                ]
            )
        )


def test_read_only_refuses_an_extra_root_even_when_the_seam_hands_it_one(tmp_path):
    """A0 is "no root", so a caller that names one has contradicted itself.

    Refused rather than dropped quietly: the seam already refuses to pass one
    (``test_a_read_only_policy_never_hands_the_runner_an_extra_root``), and this
    side's refusal is what makes an argv disagreeing with the policy a loud
    failure instead of a run under a tier whose definition was just ignored.
    """
    workspace, temps = _workspace_and_temps(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(RunnerFailure, match="read-only does not accept --extra-root"):
        _build_sandbox(
            parse_args(
                [
                    "--workspace", str(workspace),
                    "--temp", str(temps),
                    "--mode", "read-only",
                    "--extra-root", str(outside),
                    "--", "python",
                ]
            )
        )


def test_an_agentless_workspace_write_run_owns_its_private_temp_and_dacls(tmp_path):
    """No SIDs from the seam means the runner creates, grants and removes its own."""
    workspace, temps = _workspace_and_temps(tmp_path)
    sandbox, owned = _build_sandbox(
        parse_args(["--workspace", str(workspace), "--temp", str(temps), "--mode", "workspace-write", "--", "python"])
    )
    assert owned is not None and os.path.isdir(owned)
    assert os.path.dirname(owned) == str(temps)
    assert os.path.basename(owned).startswith("emrg-")
    assert owned != str(temps), "the private temp is a fresh child, never the shared parent"
    assert sandbox.manage_dacls is True
    assert sandbox.temp_dir == owned
    assert sandbox.temp_write_sid == temp_write_sid(owned)


class _UntouchableApi:
    """A binding table that fails the test if anything reaches it."""

    def __getattr__(self, name):  # pragma: no cover - the raise IS the assertion
        raise AssertionError(f"the runner touched the Win32 API ({name}) before validating its args")


def test_missing_directories_are_checked_in_both_modes_before_the_api_is_used(tmp_path):
    """A provider bug that names a bogus root fails at the runner, never mid-child.

    Driven through ``run`` rather than ``_build_sandbox`` because that ordering is
    the property: validation must come first, so the refusal is a runner failure on
    every host instead of a Win32 error that only Windows can produce.
    """
    workspace, temps = _workspace_and_temps(tmp_path)
    missing = tmp_path / "not-there"
    api = _UntouchableApi()
    for mode in ("read-only", "workspace-write"):
        with pytest.raises(RunnerFailure, match="--workspace is not an existing directory"):
            run(
                parse_args(["--workspace", str(missing), "--temp", str(temps), "--mode", mode, "--", "python"]),
                api,
            )
        with pytest.raises(RunnerFailure, match="--temp is not an existing directory"):
            run(
                parse_args(["--workspace", str(workspace), "--temp", str(missing), "--mode", mode, "--", "python"]),
                api,
            )


def test_a_private_temp_inside_the_workspace_is_refused_by_the_runner_too(tmp_path):
    """The seam checks it and so does the runner: whoever forgets, the boundary holds."""
    workspace = tmp_path / "ws"
    inside = workspace / "temp"
    inside.mkdir(parents=True)
    with pytest.raises(ValueError, match="outside the workspace"):
        _build_sandbox(
            parse_args(
                ["--workspace", str(workspace), "--temp", str(inside), "--mode", "workspace-write", "--", "python"]
            )
        )


def test_main_reports_a_bad_argv_as_the_runners_own_failure_exit(capsys):
    """Exit 127 plus the signature, so the seam can tell "did not run" from "ran and failed"."""
    assert main(["--nope", "x"]) == 127
    captured = capsys.readouterr()
    assert captured.err.startswith(f"{RUNNER_SIGNATURE}: ")
    assert "unknown argument: --nope" in captured.err


def test_the_win32_rule_classifies_the_line_the_runner_really_prints(capsys):
    """The rule the seam matches, fed the line this runner really writes.

    The third rung of one family: ``linux`` got this pin in #1587 and ``darwin``
    in #1590, where the fatal line comes from a third-party binary and therefore
    had to be *recorded*. Here both halves live in this repository, which makes
    the agreement mechanisable instead of recorded — and it is the agreement,
    not the line, that is at stake: the prefix the runner writes is
    :data:`~emrg.sandbox.win32.runner.RUNNER_SIGNATURE`, while the rule spells it
    again as the literal ``"windows-acl-run: "`` in
    :mod:`emrg.sandbox.providers.win32`. The exit half is written twice the same
    way — that provider defines ``RUNNER_FAILURE_EXIT`` as its own ``127``, and
    nothing imports it across the seam — so both halves rest on the assertions
    below rather than on the compiler. Rename one side alone and the
    rule matches nothing: the seam then reads a runner failure as the command's
    own non-zero exit — the misreading this rung exists to prevent — and every
    test that used the constant on both sides stays green.

    The exit half is pinned here too, and here it is this rung's deliberate
    difference from its siblings: the darwin rule stays signature-only (#1590
    records why an exit gate is one macOS version's behaviour), while this one
    gates on the runner's own failure exit, so a confined command that merely
    *prints* the signature is never read as "the command did not run".
    """
    from emrg.tools.bash_tool_v2 import classify_runner_failure

    # A real run's stderr, in its order: the announcement, then (on this path)
    # the fatal line — `run` writes the first one on its way to the spawn, and
    # `fail` is only reached from there.
    announce()
    with pytest.raises(RunnerFailure, match="no such directory"):
        fail("no such directory: C:\\nope")
    printed = capsys.readouterr().err
    lines = printed.splitlines()
    assert lines[0] == START_ANNOUNCEMENT
    fatal_line = lines[-1]

    # What the runner wrote, through the rule the seam consults: the detail, not
    # the announcement — the line that is skipped is the one the rule declares as
    # its start line, and this is the assertion that fails if that stops working.
    assert fatal_line != START_ANNOUNCEMENT
    assert classify_runner_failure(RUNNER_FAILURE_EXIT, printed, provider.RUNNER_FAILURE_RULES) == (
        fatal_line
    )
    # The two spellings of the prefix, named so a rename fails as this agreement
    # rather than as an incidental unmatched line.
    assert provider.RUNNER_FAILURE_RULES[0].fatal_signatures == (f"{RUNNER_SIGNATURE}: ",)
    # The gate, in the direction that matters: the same line beside a non-zero
    # exit the *command* produced is the command's failure, not the runner's.
    assert classify_runner_failure(1, printed, provider.RUNNER_FAILURE_RULES) is None


def test_an_exit_the_loader_produced_is_read_as_nothing_ran_without_a_signature():
    """The one reading a signature cannot express: the child never started.

    `LOADER_EXIT_CODES` records the measurements — four on this host, two of them
    in a single session — and every one has the same shape: a nonzero exit with
    **empty stdout and empty stderr**.  There is nothing for the rule above to
    match, so the seam reported the environment's death as the command's own
    failure (`denied: false`), and the agent spent those turns retrying command
    *shapes* against a boundary that could not start a process at all.

    The evidence is the status itself, and that is why it needs its own field
    rather than a wider signature list: these codes come from the loader, before
    any user code runs, so no line can exist to match — and no text a command can
    print may stand in for it, which is the false positive the exit gate in the
    rule above was written to avoid.

    Both directions are asserted, because a reading that cannot say "no" is not a
    reading: the loader codes classify on the backend that names them, on **both**
    sides of the seam (this runner's interpreter and the child it mirrors) and in
    both spellings of the value, while the same codes on a backend that does not
    name them stay unclassified and a command's own statuses stay untouched.
    """
    from emrg.sandbox.contract import never_started_detail
    from emrg.sandbox.providers.darwin import RUNNER_FAILURE_RULES as DARWIN_RULES
    from emrg.sandbox.providers.linux import RUNNER_FAILURE_RULES as LINUX_RULES
    from emrg.tools.bash_tool_v2 import classify_runner_failure as bash_classify
    from emrg.tools.pwsh_tool_v2 import classify_runner_failure as pwsh_classify

    readers = (bash_classify, pwsh_classify)
    # The wiring, named: a rule that drops the field fails as this agreement.
    assert provider.RUNNER_FAILURE_RULES[0].never_started_exit_codes == provider.LOADER_EXIT_CODES
    # The measured instance is in the family, and the family is the loader's.
    assert 0xC0000142 in provider.LOADER_EXIT_CODES  # STATUS_DLL_INIT_FAILED
    assert 0xC0000005 not in provider.LOADER_EXIT_CODES  # a crash a command can cause

    for code in provider.LOADER_EXIT_CODES:
        for reader in readers:
            detail = reader(code, "", provider.RUNNER_FAILURE_RULES)
            assert detail == never_started_detail(code), hex(code)
            assert str(code) in detail and f"0x{code:08X}" in detail
        # The two's complement spelling `sys.exit` takes on Windows is the same
        # status to a parent, so it must be the same reading.
        signed = code - 0x100000000
        assert bash_classify(signed, "", provider.RUNNER_FAILURE_RULES) == never_started_detail(code)

    # It is evidence *this backend* names, not a global rule about big numbers.
    for rules in (DARWIN_RULES, LINUX_RULES):
        for code in provider.LOADER_EXIT_CODES:
            for reader in readers:
                assert reader(code, "", rules) is None

    # And it does not swallow what a command really reports: beside a run's own
    # announcement an ordinary nonzero exit is unchanged, with or without a line,
    # and exit 0 / signal death are never evidence.
    started = f"{START_ANNOUNCEMENT}\n"
    for code in (1, 2, 127, 3221225477):
        for reader in readers:
            assert reader(code, started, provider.RUNNER_FAILURE_RULES) is None
    for reader in readers:
        assert reader(0, started, provider.RUNNER_FAILURE_RULES) is None
        assert reader(None, started, provider.RUNNER_FAILURE_RULES) is None
    line = r"windows-acl-run: no such directory: C:\nope"
    assert bash_classify(RUNNER_FAILURE_EXIT, started + line, provider.RUNNER_FAILURE_RULES) == line
    assert pwsh_classify(1, started + line, provider.RUNNER_FAILURE_RULES) is None


def test_a_runner_that_never_announced_its_start_is_read_as_nothing_ran(capsys):
    """The runner's own half of one question: did the child ever start?

    The loader family above reads the *parent's* evidence, and it misses the
    shape measured on 2026-09-28: this module's own import failed
    (``ModuleNotFoundError: No module named 'emrg.sandbox'``), so the process
    exited 1 with empty stdout and the seam reported a dead environment as six
    consecutive failed commands.  The status is not the evidence there — 1 is a
    command's own as often as a runner's — and the rant behind issue #1560 asked
    for the runner's own channel instead: the wrapper knows whether the child
    ever started, so it should say so on its own line rather than leave the seam
    decoding a per-platform dialect.

    What it says: one line, written immediately before the spawn.  Its
    **absence** beside a nonzero exit is a runner that never got that far, which
    is reported through
    :func:`emrg.sandbox.contract.never_announced_detail` without decoding the
    status at all; the line itself is then dropped from what the model reads,
    because it rides on every confined run and would otherwise put a ``[stderr]``
    section on every successful command.
    """
    from emrg.sandbox.contract import (
        never_announced_detail,
        never_started_detail,
        without_start_announcements,
    )
    from emrg.sandbox.providers.darwin import RUNNER_FAILURE_RULES as DARWIN_RULES
    from emrg.sandbox.providers.linux import RUNNER_FAILURE_RULES as LINUX_RULES
    from emrg.tools.bash_tool_v2 import classify_runner_failure as bash_classify
    from emrg.tools.pwsh_tool_v2 import classify_runner_failure as pwsh_classify

    readers = (bash_classify, pwsh_classify)
    rule = provider.RUNNER_FAILURE_RULES[0]
    # The agreement between the two spellings, which nothing imports across the
    # seam, exactly as with the fatal prefix: the runner writes this line and the
    # rule declares it.
    assert rule.start_line == START_ANNOUNCEMENT == provider.START_ANNOUNCEMENT
    # And it is not the failure prefix: a line that matched one would be the first
    # match of every walk and would hide the detail a real failure printed.
    assert not any(sig in START_ANNOUNCEMENT.lower() for sig in rule.fatal_signatures)
    assert RUNNER_SIGNATURE not in START_ANNOUNCEMENT

    # What the runner writes, read off the runner itself.
    announce()
    assert capsys.readouterr().err == f"{START_ANNOUNCEMENT}\n"

    # The measured shape: nothing on either stream, a status a command could also
    # have chosen, and no announcement to be found.
    for code in (1, 2, 127, 3221225477):
        for reader in readers:
            detail = reader(code, "", provider.RUNNER_FAILURE_RULES)
            assert detail == never_announced_detail(code), hex(code)
            assert str(code) in detail and f"0x{code:08X}" in detail
        # Both spellings of the status are the same status to a parent.
        signed = code - 0x100000000
        assert bash_classify(signed, "", provider.RUNNER_FAILURE_RULES) == never_announced_detail(code)

    # An announced run is the other reading, whatever the status — and that is
    # what keeps a command's own failure from being reported as the environment's.
    for code in (1, 2, 127, 3221225477):
        for reader in readers:
            assert reader(code, f"{START_ANNOUNCEMENT}\n", provider.RUNNER_FAILURE_RULES) is None

    # The loader family outranks it: that evidence is exactly the case where the
    # runner wrote nothing at all.  The sibling rungs, whose runner is a
    # third-party binary, cannot announce and are untouched by the field.
    for code in provider.LOADER_EXIT_CODES:
        for reader in readers:
            assert reader(code, "", provider.RUNNER_FAILURE_RULES) == never_started_detail(code)
    for rules in (DARWIN_RULES, LINUX_RULES):
        assert rules[0].start_line is None
        for code in (1, 127):
            for reader in readers:
                assert reader(code, "", rules) is None

    # Dropped from the text the model reads — the announcement lines and nothing
    # else, in both line endings, and only where a rule declares one.
    strip = without_start_announcements
    assert strip(f"{START_ANNOUNCEMENT}\nerr-marker", provider.RUNNER_FAILURE_RULES) == "err-marker"
    assert strip(f"{START_ANNOUNCEMENT}\r\nout\n", provider.RUNNER_FAILURE_RULES) == "out\n"
    assert strip("a\nb\n", provider.RUNNER_FAILURE_RULES) == "a\nb\n"
    assert strip(f"{START_ANNOUNCEMENT}\n", LINUX_RULES) == f"{START_ANNOUNCEMENT}\n"


def test_a_cleanup_notice_beside_the_childs_own_exit_is_not_a_runner_failure(capsys):
    """The runner's other non-failure line, and why it cannot carry the prefix.

    ``run``'s ``finally`` reports a disposal failure *beside* the code the child
    exited with — the exit code wins, because the command really ran.  Spelled in
    the failure vocabulary (``windows-acl-run: cleanup: …``) it was classified as
    one whenever that code was also 127, which is not exotic: a shell's "command
    not found" is 127.  A command that ran and returned 127 was then reported as
    an unusable environment, with the run's own result discarded — the same
    misreading the start line above exists to prevent, in the other direction.
    """
    from emrg.tools.bash_tool_v2 import classify_runner_failure
    from emrg.tools.pwsh_tool_v2 import classify_runner_failure as pwsh_classify

    started = f"{START_ANNOUNCEMENT}\n"
    note_cleanup_failure(RuntimeError("dispose failed"))
    notice = capsys.readouterr().err
    assert notice == f"{RUNNER_SIGNATURE} cleanup: dispose failed\n"

    for reader in (classify_runner_failure, pwsh_classify):
        assert reader(RUNNER_FAILURE_EXIT, started + notice, provider.RUNNER_FAILURE_RULES) is None

    # The direction of the pin: in the failure's own spelling the very same notice
    # *is* classified, so this pair fails if the notice goes back to carrying it.
    misread = f"{RUNNER_SIGNATURE}: cleanup: dispose failed\n"
    assert (
        classify_runner_failure(RUNNER_FAILURE_EXIT, started + misread, provider.RUNNER_FAILURE_RULES)
        == misread.strip()
    )


def _stub_runner(script: str):
    """A ``Runner`` whose program is a two-line interpreter script, not a boundary.

    The rules are the real ones for this rung — it is the only backend that
    declares a ``start_line`` — while the *program* is stubbed, because what the
    tests below measure is the consumer's wiring rather than the ACL boundary
    (which the Windows-only tests in this file already measure for real).  The
    stub is what makes them run on every platform, and what lets them drive the
    path that no other test reaches: ``run_command``, where the readings and the
    strip that follows them live.

    :param script: the stub program's source.
    :returns: a runner that spawns it in place of a confinement tool.
    """
    from emrg.sandbox.contract import Runner

    return Runner(
        name="stub",
        enforcement="full",
        denial_signatures=provider.DENIAL_SIGNATURES,
        runner_failure_rules=provider.RUNNER_FAILURE_RULES,
        runner_argv=lambda policy: [sys.executable, "-c", script],
    )


def _stub_confined(monkeypatch, script: str):
    """Point the seam at a stubbed runner for one test.

    Patched on the providers module, not on ``emrg.sandbox.contract``: ``confine``
    imports ``select_runner`` from there at call time, which is exactly the seam
    the patch has to reach.

    :param monkeypatch: the pytest fixture.
    :param script: the stub program's source.
    """
    from emrg.sandbox import providers as providers_module

    monkeypatch.setattr(
        providers_module,
        "select_runner",
        lambda mode, *, platform_name=None: _stub_runner(script),
    )


def test_the_announcement_is_stripped_from_what_the_model_reads(monkeypatch, tmp_path):
    """The consumer's half of the start line, driven through the real seam.

    Two production lines implement this and nothing reached them before: the
    announcement is removed from the run's stderr in ``run_command``, after the
    readings that need it and before anything is rendered.  It has to be removed
    rather than tolerated — the runner writes it on **every** confined run, so
    leaving it would put a ``[stderr]`` section on every successful command the
    model reads, and an announcement is not the command's output.

    The stub reports the announcement on stderr and markers on both streams, so
    one row says all three facts at once: the child's output survives, the
    announcement does not, and the run is still read as confined (``enforcement``
    present) rather than as a runner failure.
    """
    import asyncio

    from emrg.sandbox.policy import SandboxPolicy
    from emrg.tools.bash_tool_v2 import render_result as bash_render
    from emrg.tools.bash_tool_v2 import run_command as bash_run_command
    from emrg.tools.pwsh_tool_v2 import render_result as pwsh_render
    from emrg.tools.pwsh_tool_v2 import run_command as pwsh_run_command

    _stub_confined(
        monkeypatch,
        "import sys;"
        f"sys.stderr.write({START_ANNOUNCEMENT!r} + '\\n');"
        "sys.stderr.flush();"
        "sys.stdout.write('out-marker');"
        "sys.stderr.write('err-marker')",
    )
    policy = SandboxPolicy(mode="read-only", workspace_root=str(tmp_path))

    for run_command, render in ((bash_run_command, bash_render), (pwsh_run_command, pwsh_render)):
        result = asyncio.run(
            run_command(
                "ignored (the stub never reads it)",
                policy=policy,
                workdir=str(tmp_path),
                timeout=30,
                platform_name="win32",
            )
        )
        text = render(result)
        assert "out-marker" in text, text
        assert "err-marker" in text, text
        assert START_ANNOUNCEMENT not in text, text
        assert result.stderr == "err-marker", repr(result.stderr)
        assert result.sandbox == {
            "mode": "read-only",
            "denied": False,
            "enforcement": "full",
        }, result.sandbox

    # The row above is only worth having if it can fail, so the strip is
    # neutralised and the same run repeated: the announcement reappears as
    # `[stderr]` output the model would read.  Without this, a future edit that
    # stopped calling the strip would leave the assertions above green while the
    # leak came back — the "reading that cannot say no" failure mode.
    import emrg.tools.bash_tool_v2 as bash_module

    monkeypatch.setattr(
        bash_module, "without_start_announcements", lambda stderr, rules: stderr
    )
    unstripped = asyncio.run(
        bash_module.run_command(
            "ignored",
            policy=policy,
            workdir=str(tmp_path),
            timeout=30,
            platform_name="win32",
        )
    )
    assert START_ANNOUNCEMENT in bash_render(unstripped)


def test_a_run_with_no_announcement_is_refused_rather_than_reported_as_the_commands(monkeypatch, tmp_path):
    """The reading's readable end: a dead environment is not a failed command.

    This is the shape the rant measured — the runner's own process dies with a
    status a command could also have chosen, and nothing on either stream — and
    what the model must see is a refusal naming the environment, not
    ``[exit code: 3]`` as though the command had run and returned 3.

    Both the seam (``run_command`` raises) and the tool (what the model reads)
    are asserted, because the second is the one the rant's three readers asked
    about and only it shows the sentence that reaches the transcript.
    """
    import asyncio

    from emrg.sandbox.contract import SANDBOX_UNAVAILABLE, SandboxUnavailableError, never_announced_detail
    from emrg.sandbox.policy import SandboxPolicy
    from emrg.tools.bash_tool_v2 import BashToolV2
    from emrg.tools.bash_tool_v2 import run_command as bash_run_command
    from emrg.tools.pwsh_tool_v2 import run_command as pwsh_run_command

    _stub_confined(monkeypatch, "import sys; sys.exit(3)")
    policy = SandboxPolicy(mode="read-only", workspace_root=str(tmp_path))

    for run_command in (bash_run_command, pwsh_run_command):
        with pytest.raises(SandboxUnavailableError) as raised:
            asyncio.run(
                run_command(
                    "ignored",
                    policy=policy,
                    workdir=str(tmp_path),
                    timeout=30,
                    platform_name="win32",
                )
            )
        assert raised.value.code == SANDBOX_UNAVAILABLE
        assert raised.value.detail == never_announced_detail(3)

    # And the tool's own surface: the refusal, with the environment named.
    tool_result = asyncio.run(
        BashToolV2().execute(
            {
                "command": "ignored",
                "intent": "probe",
                "sandbox": "read-only",
                "workspace": str(tmp_path),
                "workdir": str(tmp_path),
            }
        )
    )
    assert tool_result.error is True, tool_result
    assert 'sandbox mode "read-only" is requested' in tool_result.content, tool_result.content
    assert never_announced_detail(3) in tool_result.content, tool_result.content
    assert "[exit code: 3]" not in tool_result.content, tool_result.content


def test_a_matched_fatal_line_outranks_the_missing_announcement(monkeypatch, tmp_path):
    """The order the two readings take, and why the walk has to be first.

    The same fatal line is present in two of the three rows and only the
    announcement moves, so one row isolates the ordering claim:

    * announcement + fatal line -> the fatal line (the run spoke and failed);
    * fatal line alone -> **still** the fatal line, because ``fail()`` prints one
      for every refusal it raises before the spawn and none of those is preceded
      by an announcement.  Reading the silence first answered
      ``never_announced_detail`` here, which reported a deliberate exit as a
      death, asserted a cause the case does not have, and discarded the only line
      naming the argument or the directory (``SandboxUnavailableError`` renders
      without attaching stderr);
    * nothing at all -> the death reading, which is the case the reading exists
      for: no signature could exist to match, so silence is the whole evidence.
    """
    import asyncio

    from emrg.sandbox.contract import SandboxUnavailableError, never_announced_detail
    from emrg.sandbox.policy import SandboxPolicy
    from emrg.tools.bash_tool_v2 import run_command as bash_run_command
    from emrg.tools.pwsh_tool_v2 import run_command as pwsh_run_command

    fatal = r"windows-acl-run: no such directory: C:\nope"
    policy = SandboxPolicy(mode="read-only", workspace_root=str(tmp_path))

    for script, expected in (
        (
            "import sys;"
            f"sys.stderr.write({START_ANNOUNCEMENT!r} + '\\n');"
            f"sys.stderr.write({fatal!r} + '\\n');"
            "sys.exit(127)",
            fatal,
        ),
        (
            "import sys;"
            f"sys.stderr.write({fatal!r} + '\\n');"
            "sys.exit(127)",
            fatal,
        ),
        (
            "import sys; sys.exit(127)",
            never_announced_detail(127),
        ),
    ):
        _stub_confined(monkeypatch, script)
        for run_command in (bash_run_command, pwsh_run_command):
            with pytest.raises(SandboxUnavailableError) as raised:
                asyncio.run(
                    run_command(
                        "ignored",
                        policy=policy,
                        workdir=str(tmp_path),
                        timeout=30,
                        platform_name="win32",
                    )
                )
            assert raised.value.detail == expected


def test_the_walk_is_first_at_every_status_a_pre_spawn_refusal_can_exit_with():
    """The ordering claim, stated where it is cheap to state: a pure predicate.

    Every ``fail()`` in ``runner.py`` exits ``RUNNER_FAILURE_EXIT``, and every one
    of them prints a line the walk recognises; none is preceded by an
    announcement.  The row that makes the ordering load-bearing rather than
    cosmetic is the last one: a status the exit-code gate does not admit is a run
    the walk cannot answer, so the silence reading — deliberately ungated, since
    the status a loader dies with is not a dialect we decode — is what remains.
    """
    from emrg.sandbox.contract import never_announced_detail, never_started_detail
    from emrg.tools.bash_tool_v2 import classify_runner_failure as bash_classify
    from emrg.tools.pwsh_tool_v2 import classify_runner_failure as pwsh_classify

    rules = provider.RUNNER_FAILURE_RULES
    refusal = f"{RUNNER_SIGNATURE}: --workspace is not an existing directory: C:\\nope\n"

    for reader in (bash_classify, pwsh_classify):
        # The runner's own line, whatever the announcement says — its absence here
        # is the pre-spawn refusal's normal shape, not evidence of a death.
        assert reader(RUNNER_FAILURE_EXIT, refusal, rules) == refusal.strip()
        assert reader(RUNNER_FAILURE_EXIT, f"{START_ANNOUNCEMENT}\n{refusal}", rules) == refusal.strip()
        # Silence is still the death, and it is read without decoding the status:
        # the loader's own family keeps its precedence over both.
        assert reader(RUNNER_FAILURE_EXIT, "", rules) == never_announced_detail(RUNNER_FAILURE_EXIT)
        assert reader(1, "", rules) == never_announced_detail(1)
        assert reader(provider.LOADER_EXIT_CODES[0], "", rules) == never_started_detail(
            provider.LOADER_EXIT_CODES[0]
        )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows is where the backend is loadable")
def test_the_backend_fails_closed_where_the_api_does_not_exist(capsys):
    """A wrong-platform loader is a runner failure with the same contract.

    Not an exception surfacing as a Python traceback through the seam: on a host
    whose platform has no rung the chain table is never consulted — but the
    runner module must still refuse in the vocabulary the seam classifies.
    """
    assert ffi.win32.__module__.endswith("ffi")
    with pytest.raises(RuntimeError, match="only be loaded on Windows"):
        ffi.win32()


@pytest.mark.skipif(sys.platform == "win32", reason="off Windows is where the laziness is observable")
def test_importing_the_windows_rung_never_touches_the_win32_api():
    """The chain table imports on every platform, so the binding table is lazy.

    ``ctypes.WinDLL`` does not exist off Windows, so a module-level binding build
    would make ``import emrg.sandbox.providers`` fail on this host — and with it
    every confinement on every other platform.
    """
    import ctypes

    assert not hasattr(ctypes, "WinDLL"), "this assertion is about the platform it runs on"
    assert ffi._BINDINGS is None


# ── AclSandbox's own validation ───────────────────────────────────────────


def test_the_sandbox_refuses_a_grant_shape_that_contradicts_its_mode(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sid = workspace_write_sid(str(workspace))
    other = workspace_write_sid("/other")
    with pytest.raises(ValueError, match="workspace-write requires a write SID"):
        AclSandbox(writable_dirs=[str(workspace)], temp_dir=None, mode="workspace-write")
    with pytest.raises(ValueError, match="read-only does not accept write SIDs"):
        AclSandbox(writable_dirs=[], temp_dir=None, mode="read-only", write_sid=sid)
    with pytest.raises(ValueError, match="must be distinct"):
        AclSandbox(writable_dirs=[str(workspace)], temp_dir=str(tmp_path), mode="workspace-write",
                   write_sid=sid, temp_write_sid=sid)
    with pytest.raises(ValueError, match="requires a temp directory"):
        AclSandbox(writable_dirs=[str(workspace)], temp_dir=None, mode="workspace-write",
                   write_sid=sid, temp_write_sid=other)
    with pytest.raises(ValueError, match="does not exist"):
        AclSandbox(writable_dirs=[str(tmp_path / "gone")], temp_dir=None, mode="workspace-write", write_sid=sid)
    sandbox = AclSandbox(writable_dirs=[str(workspace)], temp_dir=None, mode="workspace-write", write_sid=sid)
    assert sandbox.temp_dir is None and sandbox.write_sid == sid
    read_only = AclSandbox(writable_dirs=[], temp_dir=str(tmp_path), mode="read-only")
    assert read_only.temp_dir is None, "read-only grants no temp write capability"


def test_the_sandbox_refuses_an_extra_root_it_cannot_grant_safely(tmp_path):
    """Three refusals, each one a way the extra-root mechanism could go quietly wrong.

    A missing path is refused because ``grant_write`` on nothing would throw later
    and *outside* the argv the host can read.  A root whose SID collides with the
    workspace's (or the temp's) is refused because the token's restricting list
    would then carry one identity for two paths — the grant would be wider than
    what was named, and nothing downstream can tell.  And ``read-only`` accepts no
    root at all: a shape that described one would mean the tier's whole definition
    had been contradicted one field away from where anyone looks.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    single = tmp_path / "outside.txt"
    single.write_text("x", encoding="utf-8")
    sid = workspace_write_sid(str(workspace))

    with pytest.raises(ValueError, match="extra root does not exist"):
        AclSandbox(writable_dirs=[str(workspace)], temp_dir=None, mode="workspace-write",
                   write_sid=sid, extra_grants=[(str(tmp_path / "gone"), root_write_sid("/gone"))])
    with pytest.raises(ValueError, match="must be distinct"):
        AclSandbox(writable_dirs=[str(workspace)], temp_dir=None, mode="workspace-write",
                   write_sid=sid, extra_grants=[(str(outside), sid)])
    with pytest.raises(ValueError, match="read-only does not accept write SIDs"):
        AclSandbox(writable_dirs=[], temp_dir=None, mode="read-only",
                   extra_grants=[(str(outside), root_write_sid(str(outside)))])

    # Both spellings item 7 names are accepted, and each keeps its own identity.
    sandbox = AclSandbox(
        writable_dirs=[str(workspace)],
        temp_dir=None,
        mode="workspace-write",
        write_sid=sid,
        extra_grants=[
            (str(outside), root_write_sid(str(outside))),
            (str(single), root_write_sid(str(single))),
        ],
    )
    assert sandbox.extra_grants == [
        (str(outside), root_write_sid(str(outside))),
        (str(single), root_write_sid(str(single))),
    ]


# ── the boundary itself (Windows only) ────────────────────────────────────


#: The real boundary needs the Win32 token/ACL APIs, so it is measured on the one
#: platform that has them: CI's ``windows-2025`` leg.
needs_windows = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the ACL restricted-token boundary exists only on Windows",
)


#: ``TOKEN_INFORMATION_CLASS`` value for ``TokenUser``.  The boundary's own ABI
#: table is the blueprint's list, which has no name for it; this read is the only
#: place the test harness needs the value.
_TOKEN_USER = 1


def _caller_ace(api, path, sid_ptr: int) -> None:
    """Apply the ambient ACE a deployer's own workspace already carries.

    Full control, not write alone, because the boundary checks a
    ``WRITE_RESTRICTED`` child twice and a directory has to answer for both: the
    capability ACE the sandbox added answers the restricting check, and this one
    answers the check against the SIDs the token itself carries.  Write alone was
    enough while the only subject was a file effect; it is not enough for a tool
    that has to *open* the directory it works in (git reads the working directory
    before it reads anything else), and the two arms would then be measuring the
    harness's DACL instead of the boundary.

    :param api: the binding table.
    :param path: the directory that gains the ambient ACE, as a path string (the
        API the sweep calls takes one).
    :param sid_ptr: the caller's own SID, alive for the length of this call.
    """
    from emrg.sandbox.win32.abi import FILE_ALL_ACCESS, GRANT_ACCESS
    from emrg.sandbox.win32.acl import (
        build_explicit_access,
        merge_and_apply,
        read_current_dacl,
        with_path_lock,
    )

    with with_path_lock(api, path):
        current = read_current_dacl(api, path)
        entry = build_explicit_access(sid_ptr, GRANT_ACCESS, FILE_ALL_ACCESS)
        merge_and_apply(api, path, entry, current, "grantCaller")


def _grant_caller(api, path) -> None:
    """Give the running user's own SID the control of one boundary directory it already has.

    Windows checks a ``WRITE_RESTRICTED`` child's access twice: once against the
    SIDs the token itself carries, and once against the restricting list.  A
    capability ACE can only answer the second, so a directory must already be
    reachable through the caller's own SID for the first — the blueprint's "the
    granted directories belong to the caller" precondition, and the ordinary
    state of every workspace a deployer hands the sandbox (on this host's own
    workspace: ``YD-RPA-NODE04\\Administrator:(I)(OI)(CI)(F)``).  Without it the
    restricted child is denied before the capability is ever consulted, which
    would make these tests measure the filesystem they run on rather than the
    boundary.

    ``tmp_path`` on CI is the one place the precondition does not hold, and that
    is a property of the harness rather than of the boundary: the ambient temp
    root above it grants the running user, while the pytest base directory is
    owned by ``BUILTIN\\Administrators`` and inherits only ``SYSTEM`` /
    ``Administrators`` / ``OWNER RIGHTS`` ACEs, which a filtered token reaches
    through no SID it holds.  Granting the caller's SID restores what the tests
    assume; it does not soften what they measure, because that SID appears in no
    restricting list, so a capability ACE remains the only ACE a ``read-only``
    child could use — and
    ``test_a_read_only_run_is_refused_inside_the_workspace_too`` is the proof
    that it still does not.

    :param api: the binding table.
    :param path: the directory that gains the ambient ACE.
    """
    from emrg.sandbox.win32.ffi import alloc_bytes, alloc_uint32, decode_ptr, decode_uint32, is_null_ptr
    from emrg.sandbox.win32.token import open_current_process_token

    token = open_current_process_token(api)
    try:
        needed_slot = alloc_uint32()
        api.advapi32.GetTokenInformation(
            ctypes.c_void_p(token), _TOKEN_USER, None, 0, ctypes.byref(needed_slot)
        )  # expected to fail with ERROR_INSUFFICIENT_BUFFER
        needed = decode_uint32(needed_slot)
        assert needed > 0, "the token's user SID could not be sized"
        # One buffer owns both the TOKEN_USER record and the SID it points at, so
        # it must outlive the grant — the same lifetime rule the sandbox itself
        # learned the hard way (``NativeBuffer`` in ``ffi``).
        block = alloc_bytes(needed)
        read = int(api.advapi32.GetTokenInformation(
            ctypes.c_void_p(token), _TOKEN_USER, ctypes.byref(block), needed, ctypes.byref(needed_slot)
        ))
        assert read != 0, "the token's user SID could not be read"
        sid = decode_ptr(ctypes.c_void_p.from_address(ctypes.addressof(block)))
        assert not is_null_ptr(sid), "the token carries no user SID"
        _caller_ace(api, str(path), sid)
    finally:
        api.kernel32.CloseHandle(ctypes.c_void_p(token))


#: The size of the ``TOKEN_USER`` record this fake hands back (one pointer) plus
#: its SID — the allocation class ``_grant_caller`` reads its SID out of.
_FAKE_TOKEN_USER_SIZE = ctypes.sizeof(ctypes.c_void_p) + 8


class _FakeUserSidApi:
    """``_grant_caller``'s token read, over a fake that owns the SID it points at.

    The fake keeps only the *address* of its SID block, exactly as the boundary
    sees it, so a helper that hands the grant a bare pointer cannot be rescued by
    the fake holding the memory alive.
    """

    def __init__(self, blob: bytes = b"USERSID!") -> None:
        self.blob = blob
        self.sid_address: int | None = None
        self.advapi32 = self
        self.kernel32 = _FakeKernel32()

    def OpenProcessToken(self, process, access, token_ref):
        token_ref._obj.value = 0x7000  # a non-null handle is all the read needs
        return 1

    def GetTokenInformation(self, token, info_class, buffer, size, needed_ref):
        """Answer the size probe and then the read, with one user SID."""
        assert info_class == 1, "``_grant_caller`` asks for the token's user SID"
        needed_ref._obj.value = _FAKE_TOKEN_USER_SIZE
        if not buffer:
            return 0  # expected to fail with ERROR_INSUFFICIENT_BUFFER
        block = buffer._obj
        ctypes.memset(ctypes.addressof(block), 0, _FAKE_TOKEN_USER_SIZE)
        self.sid_address = ctypes.addressof(block) + ctypes.sizeof(ctypes.c_void_p)
        ctypes.c_void_p.from_address(ctypes.addressof(block)).value = self.sid_address
        ctypes.memmove(self.sid_address, self.blob, len(self.blob))
        return 1


def test_grant_caller_grants_a_sid_that_its_own_buffer_still_owns(tmp_path, monkeypatch):
    """The harness's one SID must outlive the apply call, exactly like the sandbox's.

    ``_grant_caller`` reads a SID out of a ``TOKEN_USER`` block and passes the
    *pointer* on, so the block has to be alive for as long as the ACE that names
    it is built and applied.  A helper that sized the block, read the SID and let
    it die would hand the apply call an address the interpreter has already given
    to the next allocation — the defect the sandbox itself carried
    (``NativeBuffer``), reproduced in the test harness, where it would surface as
    a grant that fails for a reason that has nothing to do with the boundary.

    The seam is the harness's own apply step; what is asserted about it is the
    pointer it receives, not how the product applies an ACE, and the pointer has
    to survive the ambient churn that produced the original defect.

    The churn inside the granted call is that next allocation.  Nothing is
    asserted about the memory after the call returns: the SID is copied by the
    ACL merge while the apply runs, which is the whole window the buffer has to
    survive.
    """
    api = _FakeUserSidApi()
    granted: list[tuple[str, int, bytes]] = []

    def fake_caller_ace(bindings, path, sid_ptr):
        for _ in range(256):
            ctypes.create_string_buffer(_FAKE_TOKEN_USER_SIZE)  # this block's own class
            ctypes.create_string_buffer(68)  # SECURITY_MAX_SID_SIZE
        gc.collect()
        granted.append((path, sid_ptr, ctypes.string_at(sid_ptr, 8)))

    monkeypatch.setattr(sys.modules[__name__], "_caller_ace", fake_caller_ace)
    _grant_caller(api, tmp_path)

    assert api.sid_address is not None, "the fake never handed out a SID"
    assert granted == [(str(tmp_path), api.sid_address, api.blob)]
    assert api.kernel32.closed == [0x7000], "the token it opened is closed again"


def _confined(
    argv: list[str], workspace, temp_root, mode: str, cwd: str | None = None, extra_roots: tuple = ()
) -> subprocess.CompletedProcess:
    """Spawn the runner the way the seam does, and let the child report for itself.

    Both halves come from their production sources — the rung's own
    ``runner_invocation`` and the seam's ``runner_import_env`` — rather than from
    a copy spelled out here.  A copy is what this harness used to hold, and it is
    how the argv could carry a flag the boundary tests never exercised.

    ``cwd`` is the seam's third half and defaults to *inherited* only because most
    of these tests are about the boundary rather than about a directory: the seam
    itself always names the workdir (``pwsh_tool_v2``'s ``cwd=workdir``), the
    confined child inherits it (``spawn_restricted`` passes it through), and a test
    whose subject *is* a command's behaviour in a directory has to spawn from that
    directory or it measures the test runner's instead.

    ``extra_roots`` are the host-named roots, one ``--extra-root`` each.  On this
    path the runner owns its DACLs (no SID flags are passed), so it is the
    runner's own grant that makes a root writable — which is the measurement the
    host's ``/sandbox add`` rests on and the one no macOS host can take.
    """
    env = dict(os.environ)
    env.update(runner_import_env())
    return subprocess.run(
        [
            *provider.runner_invocation(),
            "--workspace",
            str(workspace),
            "--temp",
            str(temp_root),
            "--mode",
            mode,
            *[flag for root in extra_roots for flag in ("--extra-root", str(root))],
            "--",
            *argv,
        ],
        cwd=None if cwd is None else str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def _write_script(paths: list) -> str:
    return ";".join(f"open({str(path)!r}, 'w').write('x')" for path in paths)


def _evidence(completed: subprocess.CompletedProcess) -> str:
    """The facts a failure message needs when the child's streams may be dead.

    A confined child that dies before it can print (a DLL-init failure, a refused
    exec) reports nothing on either stream, so an assertion that quotes only
    ``stderr`` says "the workspace grant did not hold" for a child that never ran.
    The exit code survives that, and it is the first thing to read.
    """
    return (
        f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )


@needs_windows
def test_a_confined_child_runs_and_reports_its_own_exit_code(tmp_path):
    """The spawn contract before the boundary: a child that cannot start proves nothing.

    Exit codes travel without stdio, so this is the one fact the seam has even when
    the child's streams are dead — and the reason this test exists separately from
    the two below, whose assertions are about file effects.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    completed = _confined([sys.executable, "-c", "pass"], workspace, tmp_path, "read-only")
    assert completed.returncode == 0, (
        f"a confined child did not run to completion ({_evidence(completed)})"
    )


@needs_windows
def test_a_workdir_that_shadows_the_package_does_not_stop_the_runner(tmp_path):
    """The workdir cannot answer the import the boundary itself needs.

    Measured on Windows Server 2022: the same spawn with an argv that leaves the
    workdir on ``sys.path`` dies here with
    ``No module named 'emrg.sandbox'`` (exit 1), because the workdir holds an
    ``emrg`` package of its own that resolution finds first.  That is not a
    hypothetical checkout: it is the ordinary state of this project's own
    sessions, and because such a checkout need not carry ``emrg/sandbox`` at all,
    the run loses its boundary and reports the loss as the caller's command
    failing.  The second arm is that failure, asserted rather than described, so
    the flag stays load-bearing instead of decorative.
    """
    checkout = tmp_path / "checkout"
    (checkout / "emrg").mkdir(parents=True)
    (checkout / "emrg" / "__init__.py").write_text("", encoding="utf-8")

    completed = _confined([sys.executable, "-c", "pass"], checkout, tmp_path, "read-only")
    assert completed.returncode == 0, f"the workdir shadowed the package ({_evidence(completed)})"

    shadowed = subprocess.run(
        [
            sys.executable,
            "-m",
            "emrg.sandbox.win32.runner",
            "--workspace",
            str(checkout),
            "--temp",
            str(tmp_path),
            "--mode",
            "read-only",
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        cwd=checkout,
        env={**os.environ, **runner_import_env()},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    assert "No module named 'emrg" in shadowed.stderr, (
        f"the workdir did not shadow the package, so this arm proves nothing ({_evidence(shadowed)})"
    )


@needs_windows
def test_a_confined_childs_stdio_reaches_the_seam(tmp_path):
    """The runner's stdio is the child's stdio, byte for byte, on both streams.

    Without this the two boundary tests below can only report "no output", which is
    the same reading for a refused write and for a child that never started.

    One line is the runner's own — the start announcement — and it is pinned here
    where it belongs: **first**, ahead of everything the child wrote, because
    that is the position the seam's reading rests on.  The seam then removes it
    again (``without_start_announcements``), so what the model reads is the
    child's streams as they were.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    completed = _confined(
        [sys.executable, "-c", "import sys; print('out-marker'); sys.stderr.write('err-marker')"],
        workspace,
        tmp_path,
        "read-only",
    )
    assert completed.returncode == 0, _evidence(completed)
    assert completed.stdout.strip() == "out-marker", f"stdout did not reach the seam ({_evidence(completed)})"
    assert completed.stderr.splitlines()[0] == START_ANNOUNCEMENT, _evidence(completed)
    assert completed.stderr.splitlines()[1:] == ["err-marker"], _evidence(completed)


@needs_windows
def test_a_workspace_write_child_can_capture_a_grandchilds_output(tmp_path):
    """The tier a workload can actually run in: its child creates a pipe.

    Every real workload captures output — ``uv``, ``pytest`` and ``node`` all read
    a child's stdout through a pipe — and under ``workspace-write`` both of these
    calls were refused with ``[WinError 5]`` while the same child ran them fine
    under ``read-only``.  The cause was the one ACE the sandbox merges into the
    restricted token's *default* DACL, which every new object takes its own DACL
    from: it named a capability SID, and a capability SID exists only in the
    token's restricting list, so the first half of the two-pass check denied the
    object to its own creator.  ``read-only`` escaped by accident — its fallback
    names Everyone, which is in both sets.

    The child reports each step as a marker, so a failure names the step rather
    than the boundary: the pipe and the captured grandchild are separate facts,
    and the second cannot be reached without the first.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    api = ffi.win32()
    _grant_caller(api, workspace)
    _grant_caller(api, tmp_path)
    script = (
        "import os, subprocess, sys;"
        "os.pipe(); print('pipe-ok');"
        "done = subprocess.run([sys.executable, '-c', 'print(42)'], capture_output=True, text=True);"
        "print('capture-ok', done.stdout.strip())"
    )

    completed = _confined([sys.executable, "-c", script], workspace, tmp_path, "workspace-write")

    assert completed.returncode == 0, _evidence(completed)
    assert "pipe-ok" in completed.stdout, (
        f"a confined child could not create its own pipe ({_evidence(completed)})"
    )
    assert "capture-ok 42" in completed.stdout, (
        f"a confined child could not capture a grandchild's output ({_evidence(completed)})"
    )


@needs_windows
def test_a_workspace_write_run_inherits_the_capability_and_nothing_outside_it(tmp_path):
    """The capability, measured where it exists: the workspace ACE and nothing else.

    The outside directory is a sibling of the workspace under the pytest temp root,
    so both spellings are equally reachable by the interpreter and only the ACL
    separates them — a test that ran the child in a directory it could not enter
    would pass without the sandbox doing anything.
    """
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    inside_file = workspace / "inside.txt"
    escaped = outside / "escaped.txt"
    # The ambient ACE every ordinary workspace already has (``_grant_caller``).
    api = ffi.win32()
    _grant_caller(api, workspace)
    _grant_caller(api, tmp_path)

    inherited = _confined(
        [sys.executable, "-c", _write_script([inside_file])], workspace, tmp_path, "workspace-write"
    )
    assert inherited.returncode == 0, f"the workspace grant did not hold ({_evidence(inherited)})"
    assert inside_file.exists(), f"the workspace grant did not hold ({_evidence(inherited)})"

    refused = _confined(
        [sys.executable, "-c", _write_script([escaped])], workspace, tmp_path, "workspace-write"
    )
    assert not escaped.exists(), f"the sandbox did not hold outside the workspace ({_evidence(refused)})"
    assert refused.returncode != 0, f"the refusing child claimed success ({_evidence(refused)})"
    assert "denied" in refused.stderr.lower(), refused.stderr


@needs_windows
def test_a_host_named_root_outside_the_workspace_is_writable(tmp_path):
    """Item 7 on the platform whose grant model had to grow: a root, and its control.

    Both spellings the host may name are here — a **directory** and a **single
    file** — because Windows grants per object, and the single file is the arm
    that could plausibly be refused by the ACL layer even though the SID derives
    from the path either way.

    Each arm is paired with its control, and the control is one variable wide: the
    same file, the same child, the same ambient ACEs, and only ``--extra-root``
    differs.  The refusal is therefore the restricting check's — ``_grant_caller``
    has given the running user's own SID access to the root, so a run that failed
    for a *directory* reason would fail both arms rather than one.

    The refusals are read off the **host**, not off the exit code: a write that
    never happened exits non-zero for whatever reason, and an exit code says
    nothing about which byte landed.  So both halves are asserted — the child was
    told "denied", and the file on the host still holds exactly what it held.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    named_in_directory = outside / "written.txt"
    single = tmp_path / "single.txt"
    single.write_text("host\n", encoding="utf-8")
    api = ffi.win32()
    for path in (workspace, tmp_path, outside, single):
        _grant_caller(api, path)

    granted = _confined(
        [sys.executable, "-c", _write_script([named_in_directory, single])],
        workspace,
        tmp_path,
        "workspace-write",
        extra_roots=(outside, single),
    )
    assert granted.returncode == 0, f"the host-named roots were not granted ({_evidence(granted)})"
    assert named_in_directory.exists(), f"a host-named directory was not writable ({_evidence(granted)})"
    assert single.read_text(encoding="utf-8") == "x", (
        f"a host-named single file was not writable ({_evidence(granted)})"
    )

    # The controls: the identical child without the flags, one run per subject, so
    # neither can hide behind the other's failure.  The single file's arm also
    # measures the *withdrawal* semantics: the ACE the granted run applied to it
    # is standing and still on the file, and the write is refused anyway, because
    # what allows a write here is the token's restricting list and not the DACL.
    single.write_text("host\n", encoding="utf-8")
    denied_file = _confined(
        [sys.executable, "-c", _write_script([single])], workspace, tmp_path, "workspace-write"
    )
    assert single.read_text(encoding="utf-8") == "host\n", (
        f"an unnamed file outside the workspace was written ({_evidence(denied_file)})"
    )
    assert denied_file.returncode != 0 and "denied" in denied_file.stderr.lower(), _evidence(denied_file)

    denied_dir = _confined(
        [sys.executable, "-c", _write_script([outside / "refused.txt"])],
        workspace,
        tmp_path,
        "workspace-write",
    )
    assert not (outside / "refused.txt").exists(), (
        f"an unnamed directory outside the workspace was writable ({_evidence(denied_dir)})"
    )
    assert denied_dir.returncode != 0 and "denied" in denied_dir.stderr.lower(), _evidence(denied_dir)


@needs_windows
def test_a_read_only_run_is_refused_inside_the_workspace_too(tmp_path):
    """read-only grants no capability at all, not even where the command runs."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "forbidden.txt"
    # The ambient ACE is granted on purpose here: the refusal below must be the
    # restricting check's, not a directory the token could not reach anyway.
    _grant_caller(ffi.win32(), workspace)
    _grant_caller(ffi.win32(), tmp_path)

    completed = _confined([sys.executable, "-c", _write_script([target])], workspace, tmp_path, "read-only")

    assert not target.exists(), f"read-only wrote into the workspace ({_evidence(completed)})"
    assert completed.returncode != 0, f"the refusing child claimed success ({_evidence(completed)})"
    assert "denied" in completed.stderr.lower(), _evidence(completed)


# ── what git is told about the granted workspace ──────────────────────────
#
# The token's restriction is also a change of identity, and git is the tool that
# notices: it refuses a repository whose owner is not the caller, which is every
# repository this host's elevated daemon created.  Measured on Windows Server
# 2022, the refusal is the same at both confined tiers while the same command
# outside the boundary exits 0 — so the confined tier looks unusable and the way
# out an operator finds is `danger-full-access`.  The runner therefore announces
# the root the policy granted, which is the directory its ACE already names.
#
# The keys are spelled out here rather than read back from the module: they are
# git's vocabulary, and a test that asks the code under test how to spell them
# can only ever agree with it.

#: What a confined child can read back about git's own declaration.
_GIT_SAFETY_SCRIPT = (
    "import os;print(os.environ.get('GIT_CONFIG_COUNT'),"
    " os.environ.get('GIT_CONFIG_KEY_0'),"
    " repr(os.environ.get('GIT_CONFIG_VALUE_0')))"
)


def test_git_is_told_the_granted_workspace_is_safe():
    """One entry, naming the granted root — nothing wider is this run's to declare."""
    assert git_safety_env("C:\\work", {}) == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "C:\\work",
    }


def test_git_entries_are_appended_to_a_deployers_own_count():
    """A deployer's own ``GIT_CONFIG_*`` setup is extended, never overwritten."""
    assert git_safety_env("/work", {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.pager"}) == {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_2": "safe.directory",
        "GIT_CONFIG_VALUE_2": "/work",
    }


def test_an_entry_already_sitting_at_the_count_is_not_overwritten():
    """The first *free* index is used, so an environment that disagrees with its own
    count still keeps every entry it declared."""
    assert git_safety_env("/work", {"GIT_CONFIG_KEY_0": "user.name"}) == {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_1": "safe.directory",
        "GIT_CONFIG_VALUE_1": "/work",
    }


@pytest.mark.parametrize("count", ["many", "-1"])
def test_a_count_that_cannot_be_read_leaves_the_environment_alone(count):
    """An unreadable count is not guessed at: a mangled ``GIT_CONFIG_*`` environment
    is the deployer's to fix, and half-rewriting it would hide that."""
    assert git_safety_env("/work", {"GIT_CONFIG_COUNT": count}) == {}


@needs_windows
def test_a_confined_git_run_works_in_the_workspace_it_was_granted(tmp_path):
    """The measured failure, end to end: git works in the root the policy granted.

    The declaration is asserted at the child as well as by the pure tests above,
    because the two can come apart — the helper can be right while the runner
    never applies it — and the end-to-end arm alone would pass on a host whose
    workspace happens to be owned by the calling user, which is not this scenario.

    Both arms run **in the granted root**: the repository's owner is what git
    complains about, and a run from anywhere else would be asking git about a
    different repository — the test runner's own checkout, which is exactly the
    repository this harness used to interrogate by accident.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    api = ffi.win32()
    _grant_caller(api, workspace)
    _grant_caller(api, tmp_path)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)

    declared = _confined(
        [sys.executable, "-c", _GIT_SAFETY_SCRIPT], workspace, tmp_path, "read-only", cwd=workspace
    )
    assert declared.stdout.strip() == f"1 safe.directory {str(workspace)!r}", _evidence(declared)

    status = _confined(
        ["git", "status", "--porcelain"], workspace, tmp_path, "workspace-write", cwd=workspace
    )
    assert status.returncode == 0, f"git refused the granted workspace ({_evidence(status)})"


# ── the SIDs a restricted token is built from ─────────────────────────────
#
# These two are the ones the Windows CI mechanism test found: a port that returns
# ``addressof(buffer)`` and lets the buffer die hands ``CreateRestrictedToken`` a
# pointer into memory the interpreter has already given to the next allocation,
# and every child then dies with STATUS_DLL_INIT_FAILED (0xC0000142) — which is
# exactly what the blueprint documents for a restricting list that carries no
# usable keep-alive group.  No macOS test can run the mechanism, so what is pinned
# here is the property that failure came from.


class _FakeSidApi:
    """The two Advapi32 calls ``make_well_known_sid`` makes, and a SID blob."""

    def __init__(self, blob: bytes = b"SIDBLOB!") -> None:
        self.blob = blob
        self.advapi32 = self

    def CreateWellKnownSid(self, sid_type, domain, sid_ref, size_ref):
        sid_ref._obj[0 : len(self.blob)] = self.blob
        return 1

    def IsValidSid(self, sid_ref):
        sid_ref._obj[0 : len(self.blob)] = self.blob
        return 1


def test_a_well_known_sid_hands_back_the_buffer_that_owns_its_memory():
    """The address and its owner travel together; a bare address is the defect.

    ``ctypes`` memory belongs to the interpreter, so an address that nothing
    references is memory the collector may reuse — and the allocation that reuses
    it is the ``SID_AND_ATTRIBUTES`` array built a few lines later from these very
    addresses.
    """
    handle = make_well_known_sid(_FakeSidApi(), 1)
    assert ctypes.addressof(handle.buffer) == handle.address
    assert ctypes.string_at(handle.address, 8) == b"SIDBLOB!"


def test_the_sid_memory_survives_the_allocations_that_used_to_land_on_it():
    """Kept alive by the handle, not by luck: the same bytes after a churn of peers."""
    handle = make_well_known_sid(_FakeSidApi(), 1)
    before = ctypes.string_at(handle.address, 8)
    for _ in range(256):
        ctypes.create_string_buffer(16)  # the size class of a SID_AND_ATTRIBUTES array
        ctypes.create_string_buffer(68)  # SECURITY_MAX_SID_SIZE, the well-known SID's own
    gc.collect()
    assert ctypes.string_at(handle.address, 8) == before


class _FakeTokenApi:
    """``GetTokenInformation``/``GetLengthSid``/``CopySid`` over one fake logon group."""

    def __init__(self, blob: bytes = b"LOGONSID") -> None:
        self.blob = blob
        self.sid_block = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        self.advapi32 = self

    def GetTokenInformation(self, token, info_class, buffer, size, needed_ref):
        """Answer the size probe and then the read, with one logon-SID group."""
        groups_offset = TokenGroups.Groups.offset
        needed = groups_offset + ctypes.sizeof(SIDAndAttributes)
        needed_ref._obj.value = needed
        if not buffer:
            return 0  # the probe is *expected* to fail with ERROR_INSUFFICIENT_BUFFER
        target = buffer._obj
        ctypes.memset(ctypes.addressof(target), 0, needed)
        ctypes.c_uint32.from_address(ctypes.addressof(target)).value = 1
        entry = ctypes.addressof(target) + groups_offset
        ctypes.c_void_p.from_address(entry).value = ctypes.addressof(self.sid_block)
        ctypes.c_uint32.from_address(entry + SIDAndAttributes.Attributes.offset).value = 0xC0000000
        return 1

    def GetLengthSid(self, sid_ptr):
        return len(self.blob)

    def CopySid(self, length, destination_ref, sid_ptr):
        destination_ref._obj[0:length] = ctypes.string_at(sid_ptr, length)
        return 1


def test_a_logon_sid_hands_back_the_buffer_that_owns_its_memory():
    """The same defect, on the other SID — the one the blueprint calls the keep-alive group."""
    handle = find_logon_sid(_FakeTokenApi(), 0)
    assert ctypes.addressof(handle.buffer) == handle.address
    assert ctypes.string_at(handle.address, 8) == b"LOGONSID"


class _FakeAclApi(_FakeTokenApi, _FakeSidApi):
    """One read-only ``init`` end to end: the two SID fakes above plus kernel32.

    Both fakes install themselves as ``advapi32``, so their methods compose here —
    and both read the same ``blob``, which is what lets this test recognize the
    bytes the token was built from after the fact.
    """

    def __init__(self, blob: bytes = b"SIDBLOB!") -> None:
        _FakeTokenApi.__init__(self, blob)
        self.blob = blob
        self.restricting_sids: list[int] = []
        self.restricted_flags = 0
        self.kernel32 = _FakeKernel32()

    def OpenProcessToken(self, process, access, token_ref):
        token_ref._obj.value = 0x7000  # a non-null handle is all the port reads
        return 1

    def CreateRestrictedToken(
        self, token, flags, disabled_count, disabled, deleted_count, deleted, count, list_ref, out_ref
    ):
        """Record the restricting list, which is what the SID owners must outlive."""
        self.restricted_flags = flags
        entries = list_ref._obj
        self.restricting_sids = [int(entries[index].Sid or 0) for index in range(count)]
        out_ref._obj.value = 0xB0B
        return 1


class _FakeKernel32:
    """The three kernel32 calls ``init``/``dispose`` make, and what they were handed."""

    def __init__(self) -> None:
        self.closed: list[int] = []
        self.freed: list[int] = []

    def GetCurrentProcess(self):
        return 0x1

    def CloseHandle(self, handle):
        self.closed.append(int(getattr(handle, "value", handle) or 0))
        return 1

    def LocalFree(self, pointer):
        self.freed.append(int(getattr(pointer, "value", pointer) or 0))
        return 0


class _FakeSidParseApi(_FakeAclApi):
    """``_FakeAclApi`` plus the one call ``_parse_sid`` makes, handing out distinct addresses.

    Distinct on purpose: the assertion this fake exists for is *which* root each
    address in the token's restricting list belongs to, and three identical
    pointers would let a sandbox that parsed one SID and reused it for every root
    pass.
    """

    def __init__(self, blob: bytes = b"SIDBLOB!") -> None:
        super().__init__(blob)
        self.asked: list[str] = []
        self.next_address = ctypes.addressof(self.sid_block)

    def ConvertStringSidToSidW(self, sid, sid_slot):
        self.asked.append(sid)
        self.next_address += 0x100
        sid_slot._obj.value = self.next_address
        return 1


def test_init_grants_and_carries_each_host_named_root(tmp_path, monkeypatch):
    """The extra roots at the site that decides: the ACE applied and the token's list.

    Two halves of one fact, and the ACL stack fails **open** when they disagree —
    an ACE under a SID no token carries grants nothing, and a SID in the token
    with no ACE under it allows nothing, but a SID *shared* between two paths
    would widen one grant into another.  So both directions are named here: which
    path got which SID (`grant_write`), and which addresses the restricting list
    was built from (`restricting_sids`), in the policy's order.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    directory = tmp_path / "outside"
    directory.mkdir()
    single = tmp_path / "outside.txt"
    single.write_text("x", encoding="utf-8")
    api = _FakeSidParseApi()
    granted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "emrg.sandbox.win32.sandbox.grant_write",
        lambda bindings, path, sid_ptr: granted.append((path, sid_ptr)),
    )
    monkeypatch.setattr(
        "emrg.sandbox.win32.sandbox.set_token_default_dacl_grant",
        lambda bindings, token, sid_ptr: None,
    )
    workspace_sid = workspace_write_sid(str(workspace))
    sandbox = AclSandbox(
        writable_dirs=[str(workspace)],
        temp_dir=None,
        mode="workspace-write",
        write_sid=workspace_sid,
        extra_grants=[
            (str(directory), root_write_sid(str(directory))),
            (str(single), root_write_sid(str(single))),
        ],
    )
    sandbox.init(api=api)

    assert api.asked == [
        workspace_sid,
        root_write_sid(str(directory)),
        root_write_sid(str(single)),
    ], "every SID is parsed from the string the ACE was derived from, in the policy's order"
    assert [path for path, _ in granted] == [str(workspace), str(directory), str(single)], (
        "each root's own ACE is applied to that root"
    )
    extra_pointers = [sid_ptr for _, sid_ptr in sandbox._extra_sid_ptrs]
    assert extra_pointers == [granted[1][1], granted[2][1]], "the token points at the root's own parsed SID"
    owners = [ctypes.addressof(owner) for owner in sandbox._owned_sids]
    assert api.restricting_sids == [*owners, granted[0][1], *extra_pointers], (
        "the restricting list is the logon SID, Everyone, then the workspace and every "
        "host-named root — and nothing else, which is what keeps a root from widening "
        "a grant the policy never named"
    )

    sandbox.dispose()
    assert sorted(api.kernel32.freed) == sorted([granted[0][1], *extra_pointers]), (
        "every SID the token was pointed at is released exactly once"
    )


def test_init_owns_every_sid_the_restricted_token_is_pointed_at(tmp_path, monkeypatch):
    """The whole defect, at the site that had it: the sandbox keeps the buffers.

    The helper tests above pin one function's return shape.  This one pins the
    property the crash depends on — that after ``init`` every address in the
    token's restricting list is inside memory this sandbox still references, so
    ``CreateRestrictedToken`` cannot read a byte the interpreter has reused.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    api = _FakeAclApi()
    granted_to: list[int] = []
    monkeypatch.setattr(
        "emrg.sandbox.win32.sandbox.set_token_default_dacl_grant",
        lambda bindings, token, sid_ptr: granted_to.append(sid_ptr),
    )
    sandbox = AclSandbox(writable_dirs=[str(workspace)], temp_dir=None, mode="read-only")
    sandbox.init(api=api)

    owners = [*sandbox._owned_sids]
    assert all(isinstance(owner, ctypes.Array) for owner in owners), (
        "an address is not an owner: keeping the pointer is what let the memory be reused"
    )
    assert [ctypes.addressof(owner) for owner in owners] == api.restricting_sids, (
        "the restricting list must point inside buffers init kept"
    )
    assert [ctypes.string_at(address, 8) for address in owners] == [b"SIDBLOB!"] * len(owners)
    assert granted_to == [api.restricting_sids[0]], (
        "the default DACL takes the logon SID — the one SID the token holds both normally and as a restrictor"
    )
    assert api.restricted_flags & 0x8, "WRITE_RESTRICTED is what makes the list mean anything"

    sandbox.dispose()
    assert api.kernel32.freed == [], "ctypes memory is released by dropping it, never by LocalFree"
    assert len(api.kernel32.closed) == 2, "both the current and the restricted token are closed"


