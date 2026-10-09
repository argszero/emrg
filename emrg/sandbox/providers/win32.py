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

The **host-named extra roots** are the first kind, not a third: one capability
SID derived from each root's own canonical path, one standing ACE per path,
cached beside the workspace's.  That is what makes them withdrawable without any
teardown — the ACE stands, and the *policy* is what grants the root per call
(``--extra-root``), so a call that no longer names it builds a token whose
restricting list no longer carries that SID and the write is refused again
(rant ``2026-10-09T09:43:39`` §7).
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
from emrg.sandbox.roots import canonical_path
from emrg.sandbox.win32.ffi import Win32Bindings, win32
from emrg.sandbox.win32.grants import AclWriteGrant
from emrg.sandbox.win32.sid import (
    assert_temp_root_outside_workspace,
    root_write_sid,
    temp_write_sid,
    workspace_write_sid,
)

#: The runner's documented failure exit (its own contract, distinct from
#: Landlock's 125): every runner-side failure exits with it.
RUNNER_FAILURE_EXIT = 127

#: The statuses the Windows loader itself reports when an image cannot be loaded
#: or initialized, as the unsigned 32-bit values a parent observes.  Nothing here
#: can be a shell's own result: these are written by ``Ldr`` before any user code
#: runs, which is why they carry the one thing a signature cannot — a *per-call*
#: answer to "did the child start?".
#:
#: ``STATUS_DLL_INIT_FAILED`` (``0xC0000142``) is the measured one, four times on
#: this host and twice in one session: at 15:33 a confined run answered five
#: consecutive commands with exit ``3221225794`` and **empty stdout and stderr**,
#: and the tool reported each as the command's own failure (``denied: false``),
#: because a wrapper-level death carries no signature to match.  The agent's own
#: reading of that turns is the cost: the environment was dead and every command
#: looked like a bad command.
#:
#: The family is named rather than the one code, because these are one event —
#: the image never initialized — and each is produced by the same loader pass:
#: ``INVALID_IMAGE_FORMAT`` (``0xC000007B``), ``DLL_NOT_FOUND`` (``0xC0000135``),
#: ``ENTRYPOINT_NOT_FOUND`` (``0xC0000139``).  A code with a *different* meaning is
#: deliberately absent: ``STATUS_ACCESS_VIOLATION`` (``0xC0000005``) is a crash a
#: command can really cause, and reading it as "nothing ran" would be the false
#: positive this field is shaped to avoid.
LOADER_EXIT_CODES: tuple[int, ...] = (
    0xC000007B,  # STATUS_INVALID_IMAGE_FORMAT
    0xC0000135,  # STATUS_DLL_NOT_FOUND
    0xC0000139,  # STATUS_ENTRYPOINT_NOT_FOUND
    0xC0000142,  # STATUS_DLL_INIT_FAILED
)

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

#: The line this backend's runner writes the instant it is about to mirror the
#: command, spelled here as the literal ``runner.py`` writes
#: (:data:`~emrg.sandbox.win32.runner.START_ANNOUNCEMENT`).  The agreement is
#: mechanised by a test rather than by an import, exactly as the fatal prefix's
#: is: the runner is the one part of this backend that must import as little as
#: possible, because its own import failing is one of the events the line exists
#: to detect.
#:
#: This is the answer to a question the exit status cannot give on this platform
#: — "did the child ever start?" — and it is the reason the rule carries a
#: ``start_line`` at all: the rungs whose runner is a third-party binary
#: (``bwrap``, ``sandbox-exec``) can be asked nothing of the kind and leave the
#: field ``None``.
START_ANNOUNCEMENT = "emrg-sandbox-runner: started"

#: ``windows-acl-run: <detail>`` on stderr, exit-gated on 127 so a confined
#: command that merely *prints* the signature — or a runner cleanup failure
#: reported beside a non-zero child exit — is never misread as "the command did
#: not run".
#:
#: The second field is the reading that has no line to match, and it is the
#: measured one on this backend (:data:`LOADER_EXIT_CODES`).  Both sides of the
#: seam are covered by it without either side having to agree on a sentence: the
#: exit status is the loader's own, so whichever process failed to initialize —
#: this runner's interpreter or the child it mirrors — nothing ran.
#:
#: The third is ``start_line``, the runner's own side of the same question and
#: the half no status can express (see :data:`START_ANNOUNCEMENT` and
#: :class:`~emrg.sandbox.contract.RunnerFailureRule`).
RUNNER_FAILURE_RULES: tuple[RunnerFailureRule, ...] = (
    RunnerFailureRule(
        fatal_signatures=("windows-acl-run: ",),
        allowed_exit_codes=(RUNNER_FAILURE_EXIT,),
        never_started_exit_codes=LOADER_EXIT_CODES,
        start_line=START_ANNOUNCEMENT,
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
    """Server-lifetime write grants: standing per path, revocable per pair.

    :param api: a binding table to use (tests); defaults to the host's on first
        use, so importing this module stays platform-neutral.
    """

    def __init__(self, api: Win32Bindings | None = None) -> None:
        self._api = api
        #: Standing grants, keyed by the canonical path they name — the workspace
        #: root and every host-named extra root live here together, because they
        #: are one kind of thing: a capability SID derived from a path, with an ACE
        #: that is never revoked.  What makes a root *effective* is the policy, not
        #: this cache (see :func:`runner_argv`).
        self._standing: dict[str, AclWriteGrant] = {}
        self._temps: dict[str, TempCapability] = {}

    def _bindings(self) -> Win32Bindings:
        """The binding table, resolved on first use.

        :returns: the binding table.
        """
        if self._api is None:
            self._api = win32()
        return self._api

    def _materialize_standing(self, path: str) -> None:
        """Materialize one path's standing capability ACE, once per server lifetime.

        Fail-closed: a grant that could not be applied is disposed before the
        error propagates — and a standing ACE that survived a post-apply throw is
        *not* revoked, because it is the intended end state rather than an error
        artifact.

        :param path: the canonical root path, a directory or a single file.
        :raises Win32Error: when the grant failed.
        """
        if path in self._standing:
            return
        api = self._bindings()
        grant = AclWriteGrant.create(workspace_write_sid(path), api)
        try:
            grant.add(path, standing=True)
        except BaseException as error:  # noqa: BLE001 - cleanup, then re-raise
            try:
                grant.dispose()
            except BaseException as cleanup_error:  # noqa: BLE001 - reported
                raise RuntimeError(
                    "windows-acl standing grant failed and its cleanup also failed"
                ) from cleanup_error
            raise error
        self._standing[path] = grant

    def materialize(
        self,
        session_id: str,
        workspace_root: str,
        *,
        temp_root: str | None = None,
        extra_roots: tuple[str, ...] | list[str] = (),
    ) -> TempCapability:
        """Materialize one policy's ACEs, once per pair per server lifetime.

        Fail-closed: a half-materialized temp grant is revoked and its directory
        removed before the error propagates.  A standing workspace ACE that
        survived a post-apply throw is *not* revoked — it is the intended end
        state, not an error artifact.

        The host-named extra roots are materialized the same way and with the same
        lifetime as the workspace's, one ACE per path (rant
        ``2026-10-09T09:43:39`` §7).  That is what keeps them *withdrawable*: the
        ACE stands, but only the call that still names the root passes
        ``--extra-root``, so the restricted token of the next call no longer
        carries that root's SID and the write is refused again — immediately, with
        no whole-tree revocation on the write path.

        :param session_id: the calling session's identity.
        :param workspace_root: the canonical workspace root.
        :param temp_root: the parent for the private temp directory; defaults
            to the host temp root.
        :param extra_roots: the policy's host-named roots, canonicalized here so a
            spelling the host typed and the path the ACE names are one value.
        :returns: the pair's private temp directory and write capability.
        :raises ValueError: when the temp root is inside the workspace.
        """
        api = self._bindings()
        root = temp_root or tempfile.gettempdir()
        assert_temp_root_outside_workspace(workspace_root, root)
        # Before the temp capability's cache is consulted, so a session that adds a
        # root mid-life gets its ACE on the call that first names it.
        for extra in extra_roots:
            self._materialize_standing(canonical_path(extra))
        self._materialize_standing(workspace_root)
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

        The standing ACEs — the workspace's and every host-named root's — are left
        in place, by design: they are the capability cache, and what withdraws a
        root is the policy no longer naming it.

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
        """Revoke every revocable grant and drop the store (standing ACEs stand).

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
        self._standing.clear()
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

    The host-named extra roots ride along as repeated ``--extra-root`` flags on
    **every** ``workspace-write`` invocation (rant ``2026-10-09T09:43:39`` §7),
    and never under ``read-only``, whose whole definition is that it grants no
    root.  Under a session the ACEs are materialized here first, standing like
    the workspace's; without one the runner owns its DACLs and grants them
    itself.  Either way the flag carries only a path — the SID is derived on the
    consuming side from that same path, so a granted ACE and an allowed token
    cannot name different roots.

    The roots are canonicalized and de-duplicated in order, because the SID is a
    function of the path: two spellings of one root would otherwise materialize
    one ACE and push two ``--extra-root`` flags, and ``AclSandbox`` would refuse
    the duplicate identity it saw.

    :param policy: the resolved per-call policy.
    :returns: the runner argv, before the seam's ``--`` and the caller's argv.
    """
    workspace = policy.workspace_root
    if policy.mode == "read-only":
        return [
            *runner_invocation(),
            "--workspace", workspace,
            "--temp", tempfile.gettempdir(),
            "--mode", policy.mode,
        ]
    extra_roots = _extra_root_spellings(policy)
    extra_flags: list[str] = []
    for root in extra_roots:
        extra_flags += ["--extra-root", root]
    if policy.session_id is None:
        return [
            *runner_invocation(),
            "--workspace", workspace,
            "--temp", tempfile.gettempdir(),
            "--mode", policy.mode,
            *extra_flags,
        ]
    capability = STORE.materialize(policy.session_id, workspace, extra_roots=extra_roots)
    return [
        *runner_invocation(),
        "--workspace", workspace,
        "--temp", capability.directory,
        "--mode", policy.mode,
        "--write-sid", workspace_write_sid(workspace),
        "--temp-write-sid", capability.write_sid,
        *extra_flags,
    ]


def _extra_root_spellings(policy: SandboxPolicy) -> tuple[str, ...]:
    """The policy's host-named roots, canonical, de-duplicated, order preserved.

    :param policy: the resolved per-call policy.
    :returns: the canonical spellings.
    """
    out: list[str] = []
    seen: set[str] = set()
    for spelling in policy.extra_roots:
        canonical = canonical_path(spelling)
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return tuple(out)


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
