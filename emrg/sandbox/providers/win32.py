"""The Windows rung: the ACL restricted-token backend.

Mirrors the ``windows-acl`` row of the blueprint's provider
(``packages/sandbox/sandbox-local/src/index.ts``: the chain table ``:159-166``,
the static enforcement table ``:177-187``, the denial dialect ``:205-213``, the
runner-failure rules ``:231-240``, and ``materializeAclGrant`` ``:395-439``),
over :mod:`emrg.sandbox.win32`, which is the port of
``packages/sandbox/sandbox-windows-acl/`` itself.

**Enforcement is ``partial``, and the reason is the backend's own**: a
``WRITE_RESTRICTED`` token needs Everyone in both restricting lists so
processes can initialize, so an external object that grants Everyone write
access stays writable, and an NTFS hard link can alias a granted workspace file
to a path outside it.  The backend governs the remaining ACL-addressable
surface; it must not advertise the absolute promise (blueprint ``:181-186``).

Two grants, two lifetimes (the blueprint's own split):

* the **workspace** write SID is derived from the canonical workspace path, so
  its ACE is *standing* — it is the cross-session reuse cache and is never
  revoked, because revoking would force the next provision to re-propagate the
  whole tree;
* each live session × workspace pair gets a random private temp directory with
  its own SID, so sibling sessions sharing a workspace cannot enter one
  another's temp trees.  That ACE is *revocable*, and
  :func:`release_session` is where it goes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass

from emrg.sandbox.contract import Runner, RunnerFailureRule
from emrg.sandbox.policy import SandboxPolicy
from emrg.sandbox.win32.ffi import Win32Bindings, win32
from emrg.sandbox.win32.grants import AclWriteGrant
from emrg.sandbox.win32.sid import (
    assert_temp_root_outside_workspace,
    temp_write_sid,
    workspace_write_sid,
)

#: The runner's documented failure exit (its own contract, distinct from
#: Landlock's 125): every runner-side failure exits with it.
RUNNER_FAILURE_EXIT = 127

#: What this backend can honestly claim: see the module docstring.
ENFORCEMENT = "partial"

#: The dialect a refused write produces under it — PowerShell/.NET's
#: ``Access to the path '…' is denied.``, cmd's ``Access is denied.``, and the
#: ``EACCES``/``EPERM`` renderings.  A dialect, not a cross-backend union.
DENIAL_SIGNATURES: tuple[str, ...] = (
    "access is denied",
    "access to the path",
    "permission denied",
    "operation not permitted",
)

#: ``windows-acl-run: <detail>`` on stderr, exit-gated on 127 so a confined
#: command that merely *prints* the signature — or a runner cleanup failure
#: reported beside a non-zero child exit — is never misread as "the command did
#: not run".
RUNNER_FAILURE_RULES: tuple[RunnerFailureRule, ...] = (
    RunnerFailureRule(
        fatal_signatures=("windows-acl-run: ",),
        allowed_exit_codes=(RUNNER_FAILURE_EXIT,),
    ),
)

#: How the private temp directories are named under the host temp root.
TEMP_PREFIX = "emrg-"


def runner_invocation() -> list[str]:
    """The runner's argv prefix.

    A Python entry rather than a built native executable: the blueprint's
    contract is explicit that the argv shape is what matters ("a native-exe
    replacement would keep the same contract"), and the seam sees one process
    either way.

    ``-P`` is load-bearing rather than tidiness: ``-m`` puts the process's
    **working directory** at the head of ``sys.path``, ahead of ``PYTHONPATH``,
    and the seam spawns this runner with ``cwd`` set to the session's workdir
    (:mod:`emrg.tools.pwsh_tool_v2`, :mod:`emrg.tools.bash_tool_v2`).  A workdir
    that is itself an EMRG checkout — the ordinary state of this project's own
    sessions — therefore shadows the installed package, and the runner dies at
    import before it has spawned anything: measured on Windows Server 2022
    under both confined tiers,

        Error while finding module specification for 'emrg.sandbox.win32.runner'
        (ModuleNotFoundError: No module named 'emrg.sandbox'), exit 1

    The confined run then reports a failure that belongs to the boundary.  The
    one-variable control is the same spawn with this flag, which exits 0.

    ``-I`` is the tempting neighbour and is wrong: it implies ``-E``, so
    ``PYTHONPATH`` is ignored too and ``emrg`` becomes unresolvable — the runner
    cannot start at all.  Keeping the workdir out of ``sys.path`` moves the
    runner's own import root onto the environment, which is why the seam hands
    it one (:func:`emrg.tools.shell_env.runner_import_env`).

    :returns: the program plus the module that implements the runner.
    """
    return [sys.executable, "-P", "-m", "emrg.sandbox.win32.runner"]


@dataclass(frozen=True)
class TempCapability:
    """One session × workspace pair's revocable temp capability."""

    directory: str
    write_sid: str
    grant: AclWriteGrant


class GrantStore:
    """Server-lifetime write grants: standing per workspace, revocable per pair.

    :param api: a binding table to use (tests); defaults to the host's on first
        use, so importing this module stays platform-neutral.
    """

    def __init__(self, api: Win32Bindings | None = None) -> None:
        self._api = api
        self._workspaces: dict[str, AclWriteGrant] = {}
        self._temps: dict[str, TempCapability] = {}

    def _bindings(self) -> Win32Bindings:
        """The binding table, resolved on first use.

        :returns: the binding table.
        """
        if self._api is None:
            self._api = win32()
        return self._api

    def materialize(
        self, session_id: str, workspace_root: str, *, temp_root: str | None = None
    ) -> TempCapability:
        """Materialize one policy's ACEs, once per pair per server lifetime.

        Fail-closed: a half-materialized temp grant is revoked and its directory
        removed before the error propagates.  A standing workspace ACE that
        survived a post-apply throw is *not* revoked — it is the intended end
        state, not an error artifact.

        :param session_id: the calling session's identity.
        :param workspace_root: the canonical workspace root.
        :param temp_root: the parent for the private temp directory; defaults
            to the host temp root.
        :returns: the pair's private temp directory and write capability.
        :raises ValueError: when the temp root is inside the workspace.
        """
        api = self._bindings()
        root = temp_root or tempfile.gettempdir()
        assert_temp_root_outside_workspace(workspace_root, root)
        write_sid = workspace_write_sid(workspace_root)
        if workspace_root not in self._workspaces:
            grant = AclWriteGrant.create(write_sid, api)
            try:
                grant.add(workspace_root, standing=True)
            except BaseException as error:  # noqa: BLE001 - cleanup, then re-raise
                try:
                    grant.dispose()
                except BaseException as cleanup_error:  # noqa: BLE001 - reported
                    raise RuntimeError(
                        "windows-acl workspace grant failed and its cleanup also failed"
                    ) from cleanup_error
                raise error
            self._workspaces[workspace_root] = grant
        key = json.dumps([str(session_id), workspace_root])
        existing = self._temps.get(key)
        if existing is not None:
            return existing

        directory = tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=root)
        sid = temp_write_sid(directory)
        grant = AclWriteGrant.create(sid, api)
        try:
            grant.add(directory)
        except BaseException as error:  # noqa: BLE001 - cleanup, then re-raise
            failures: list[BaseException] = []
            try:
                grant.dispose()
            except BaseException as cleanup_error:  # noqa: BLE001 - collected
                failures.append(cleanup_error)
            try:
                shutil.rmtree(directory, ignore_errors=False)
            except BaseException as cleanup_error:  # noqa: BLE001 - collected
                failures.append(cleanup_error)
            if failures:
                aggregate = RuntimeError(
                    "windows-acl temp grant materialization failed and its cleanup also failed"
                )
                aggregate.failures = failures  # type: ignore[attr-defined]
                raise aggregate from error
            raise
        capability = TempCapability(directory=directory, write_sid=sid, grant=grant)
        self._temps[key] = capability
        return capability

    def release_session(self, session_id: str) -> None:
        """Revoke and remove every temp capability one session holds.

        The standing workspace ACEs are left in place, by design.

        :param session_id: the session whose temp grants end here.
        """
        prefix = json.dumps([str(session_id), ""])[:-2]  # ``["<id>", ` prefix
        for key in [k for k in self._temps if k.startswith(prefix)]:
            capability = self._temps.pop(key)
            try:
                capability.grant.dispose()
            finally:
                shutil.rmtree(capability.directory, ignore_errors=True)

    def clear(self) -> None:
        """Revoke every revocable grant and drop the store (workspace ACEs stand).

        :raises RuntimeError: when one or more revocations failed.
        """
        failures: list[BaseException] = []
        for capability in self._temps.values():
            try:
                capability.grant.dispose()
            except BaseException as exc:  # noqa: BLE001 - collected
                failures.append(exc)
            shutil.rmtree(capability.directory, ignore_errors=True)
        self._temps.clear()
        self._workspaces.clear()
        if failures:
            error = RuntimeError(f"windows-acl grant disposal reported {len(failures)} failure(s)")
            error.failures = failures  # type: ignore[attr-defined]
            raise error


#: The server-lifetime grant store the provider uses.  One per daemon process,
#: built on first use so importing this module never touches Win32.
STORE = GrantStore()


def runner_argv(policy: SandboxPolicy) -> list[str]:
    """The runner invocation for one policy.

    With a calling session under ``workspace-write`` the grants are
    materialized once per pair per server lifetime, and the runner receives
    ``--write-sid`` plus ``--temp-write-sid`` — it grants nothing itself.
    Without one (an agentless call), or under ``read-only``, the runner gets the
    ambient temp *root* and no SID flags: for ``workspace-write`` it then
    creates and removes a random private child directory for that one
    invocation, and under ``read-only`` it needs no temp capability at all.

    :param policy: the resolved per-call policy.
    :returns: the runner argv, before the seam's ``--`` and the caller's argv.
    """
    workspace = policy.workspace_root
    if policy.mode == "read-only" or policy.session_id is None:
        return [
            *runner_invocation(),
            "--workspace", workspace,
            "--temp", tempfile.gettempdir(),
            "--mode", policy.mode,
        ]
    capability = STORE.materialize(policy.session_id, workspace)
    return [
        *runner_invocation(),
        "--workspace", workspace,
        "--temp", capability.directory,
        "--mode", policy.mode,
        "--write-sid", workspace_write_sid(workspace),
        "--temp-write-sid", capability.write_sid,
    ]


def release_session(session_id: str) -> None:
    """End one session's revocable temp grants (called when a session is deleted).

    :param session_id: the session whose temp capabilities are revoked.
    """
    STORE.release_session(session_id)


#: The backend as the chain tables carry it.
WINDOWS_ACL = Runner(
    name="windows-acl",
    enforcement=ENFORCEMENT,
    denial_signatures=DENIAL_SIGNATURES,
    runner_failure_rules=RUNNER_FAILURE_RULES,
    runner_argv=runner_argv,
)


def temp_root_is_usable(workspace_root: str) -> bool:
    """Whether a private temp root can exist outside ``workspace_root``.

    Used by tests and by any caller that wants the precondition checked before a
    spawn rather than at it.

    :param workspace_root: the canonical workspace root.
    :returns: whether the host temp root is outside the workspace.
    """
    try:
        assert_temp_root_outside_workspace(workspace_root, tempfile.gettempdir())
    except ValueError:
        return False
    return os.path.isdir(tempfile.gettempdir())
