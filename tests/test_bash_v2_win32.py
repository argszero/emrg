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
from emrg.sandbox.win32 import ffi
from emrg.sandbox.win32.ffi import SIDAndAttributes
from emrg.sandbox.win32.runner import (
    RUNNER_FAILURE_EXIT,
    RUNNER_SIGNATURE,
    RunnerFailure,
    _build_sandbox,
    fail,
    git_safety_env,
    main,
    parse_args,
    run,
)
from emrg.sandbox.win32.sandbox import AclSandbox
from emrg.sandbox.win32.token import TokenGroups, find_logon_sid, make_well_known_sid
from emrg.sandbox.win32.sid import (
    assert_private_temp_disjoint,
    assert_temp_root_outside_workspace,
    contains_directory,
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
    """Both the apply and the cleanup failing is a different error, and says so."""
    built, _ = store
    workspace, temps = _workspace_and_temps(tmp_path)
    CONTROLLER.update(fail_workspace_add=True, fail_dispose=True)
    with pytest.raises(RuntimeError, match="workspace grant failed and its cleanup also failed"):
        built.materialize("s1", str(workspace), temp_root=str(temps))


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
    :mod:`emrg.sandbox.providers.win32` (its exit gate, by contrast, *imports*
    ``RUNNER_FAILURE_EXIT`` and so cannot drift). Rename one side alone and the
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

    with pytest.raises(RunnerFailure, match="no such directory"):
        fail("no such directory: C:\\nope")
    printed = capsys.readouterr().err

    # What the runner wrote, through the rule the seam consults: one line, the
    # same one `main` exits 127 behind (the test above pins that half).
    assert classify_runner_failure(RUNNER_FAILURE_EXIT, printed, provider.RUNNER_FAILURE_RULES) == (
        printed.strip()
    )
    # The two spellings of the prefix, named so a rename fails as this agreement
    # rather than as an incidental unmatched line.
    assert provider.RUNNER_FAILURE_RULES[0].fatal_signatures == (f"{RUNNER_SIGNATURE}: ",)
    # The gate, in the direction that matters: the same line beside a non-zero
    # exit the *command* produced is the command's failure, not the runner's.
    assert classify_runner_failure(1, printed, provider.RUNNER_FAILURE_RULES) is None


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
    argv: list[str], workspace, temp_root, mode: str, cwd: str | None = None
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
    assert completed.stderr.strip() == "err-marker", f"stderr did not reach the seam ({_evidence(completed)})"


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
    assert granted_to == [api.restricting_sids[1]], "the default DACL takes the Everyone SID"
    assert api.restricted_flags & 0x8, "WRITE_RESTRICTED is what makes the list mean anything"

    sandbox.dispose()
    assert api.kernel32.freed == [], "ctypes memory is released by dropping it, never by LocalFree"
    assert len(api.kernel32.closed) == 2, "both the current and the restricted token are closed"


