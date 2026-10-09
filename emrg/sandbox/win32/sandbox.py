"""One write-restricted sandbox instance: token + write-SID grants + spawn.

Ported from the blueprint's ``index.ts`` ``AclSandbox`` (``sandbox-windows-acl``,
dsh 0.1.6-alpha.2).  The mechanism, in the blueprint's own words: a
``WRITE_RESTRICTED`` token whose restricting SIDs include distinct workspace
and temp write SIDs that this sandbox adds to their owning directories' DACLs —
the intersection check then allows writes exactly where either capability has a
Write ACE, and nowhere else *those SIDs are concerned*.  The check also inherits
the ambient write ACEs of the other restricting SIDs (the keep-alive group:
logon SID + Everyone), which is precisely why this backend advertises
``partial`` enforcement and not ``full``.

Known boundaries, inherent to restricted tokens rather than to this port:
writes are restricted but **reads, network and process visibility are not**;
console isolation is unavailable (``CREATE_NO_WINDOW``/``CREATE_NEW_CONSOLE``
children die with ``STATUS_DLL_INIT_FAILED`` under the restriction); the private
temp directory and every writable directory must be owned by the caller; and
NTFS hard links can alias a granted file to a path outside the granted tree.
"""

from __future__ import annotations

import ctypes
import os

from emrg.sandbox.win32 import abi
from emrg.sandbox.win32.acl import grant_write, revoke_write
from emrg.sandbox.win32.ffi import (
    Win32Bindings,
    alloc_ptr_slot,
    decode_ptr,
    is_null_ptr,
    local_free,
    throw_last_error,
    win32,
)
from emrg.sandbox.win32.sid import assert_private_temp_disjoint
from emrg.sandbox.win32.spawn import RestrictedChild, spawn_restricted
from emrg.sandbox.win32.token import (
    create_restricted_token,
    find_logon_sid,
    make_well_known_sid,
    open_current_process_token,
    set_token_default_dacl_grant,
)


def _parse_sid(api: Win32Bindings, sid: str) -> int:
    """Parse one SID string into a SID pointer.

    :param api: the binding table.
    :param sid: the SDDL string form.
    :returns: the parsed SID pointer.
    :raises Win32Error: when the string is not a SID.
    """
    sid_slot = alloc_ptr_slot()
    if int(api.advapi32.ConvertStringSidToSidW(sid, ctypes.byref(sid_slot))) == 0:
        throw_last_error(api, "ConvertStringSidToSidW", sid)
    parsed = decode_ptr(sid_slot)
    if is_null_ptr(parsed):
        throw_last_error(api, "ConvertStringSidToSidW", f"null SID for {sid}")
    return parsed


def _free_sid_best_effort(
    api: Win32Bindings, sid_ptr: int | None, label: str, failures: list[BaseException]
) -> None:
    """Free one optional SID, recording a failure instead of raising it.

    :param api: the binding table.
    :param sid_ptr: the SID to free, or ``None``.
    :param label: the caller's name for error details.
    :param failures: the list a failure is appended to.
    """
    if sid_ptr is None:
        return
    try:
        freed = local_free(api, sid_ptr)
        if not is_null_ptr(freed):
            throw_last_error(api, "LocalFree", label)
    except BaseException as exc:  # noqa: BLE001 - collected, not swallowed
        failures.append(exc)


class AclSandbox:
    """A token, its grants, and the confined children spawned under it.

    :param writable_dirs: the directories the confined child may write into
        (they must exist and be owned by the caller).
    :param temp_dir: the existing private temp directory whose ACE is
        revocable, or ``None`` for "no temp write capability".
    :param mode: the file-effect mode; selects the restricting-SID list and
        must match the grant shape (``read-only`` pairs with zero grants).
    :param write_sid: the workspace write SID; required under
        ``workspace-write``, rejected under ``read-only``.
    :param temp_write_sid: the private temp write SID; required whenever
        ``workspace-write`` grants a temp directory, and never equal to
        ``write_sid`` (otherwise a sibling session could use the shared
        workspace capability inside this session's temp tree).
    :param extra_grants: the host-named extra writable roots, each paired with
        the capability SID derived from **its own** path (rant
        ``2026-10-09T09:43:39``, item 7).  One SID per root rather than one for
        all of them, for the same reason the workspace has its own: the SID is a
        pure function of the path, and the token is what grants it — so dropping
        a root from the next call's argv stops that write immediately, with no
        DACL teardown on a hot path.  A path that is a **file** is accepted here
        as well as a directory: Windows ACLs are per-object, and the single-file
        case is the one half of item 7 the Windows leg has not yet measured
        (the seam's ``GrantStore`` word is the standing-ACE cache for both).
    :param manage_dacls: whether this instance owns its DACL grants.  ``False``
        means the caller (the seam's grant store) already materialized them, so
        ``init``/``dispose`` neither grant nor revoke.
    """

    def __init__(
        self,
        *,
        writable_dirs: list[str],
        temp_dir: str | None,
        mode: str,
        write_sid: str | None = None,
        temp_write_sid: str | None = None,
        extra_grants: "list[tuple[str, str]] | tuple[tuple[str, str], ...]" = (),
        manage_dacls: bool = True,
    ) -> None:
        self.mode = mode
        self.manage_dacls = manage_dacls
        resolved: list[str] = []
        for directory in writable_dirs:
            absolute = os.path.abspath(directory)
            if not os.path.isdir(absolute):
                raise ValueError(
                    f"AclSandbox writable dir does not exist or is not a directory: {absolute}"
                )
            resolved.append(absolute)
        self.writable_dirs = resolved
        self.temp_dir_option = temp_dir
        self.write_sid = write_sid
        self.temp_write_sid = temp_write_sid

        # An extra root is granted per object, so its existence — not its
        # directory-ness — is the precondition (item 7 asks for files too).
        resolved_extras: list[tuple[str, str]] = []
        for path, sid in extra_grants:
            absolute = os.path.abspath(path)
            if not os.path.exists(absolute):
                raise ValueError(f"AclSandbox extra root does not exist: {absolute}")
            resolved_extras.append((absolute, sid))
        self.extra_grants = resolved_extras

        if mode == "workspace-write" and write_sid is None:
            raise ValueError(
                "AclSandbox workspace-write requires a write SID — derive it from the workspace "
                "via workspace_write_sid()"
            )
        if mode == "read-only" and (
            write_sid is not None or temp_write_sid is not None or resolved_extras
        ):
            raise ValueError("AclSandbox read-only does not accept write SIDs")
        if write_sid is not None and temp_write_sid == write_sid:
            raise ValueError("AclSandbox workspace and temp write SIDs must be distinct")
        # One identity per root is what makes a root grantable *and* droppable on
        # its own: two roots sharing a SID would share a fate, and a SID equal to
        # the workspace's or the temp's would silently widen that grant instead of
        # adding one.
        named = {sid for sid in (write_sid, temp_write_sid) if sid is not None}
        for path, sid in resolved_extras:
            if sid in named:
                raise ValueError(
                    f"AclSandbox extra root SID must be distinct from every other write SID: "
                    f"{sid} ({path})"
                )
            named.add(sid)
        if temp_dir is None and temp_write_sid is not None:
            raise ValueError("AclSandbox temp write SID requires a temp directory")
        if mode == "workspace-write" and temp_dir is not None and temp_write_sid is None:
            raise ValueError(
                "AclSandbox workspace-write with temp requires a temp write SID — derive it via "
                "temp_write_sid()"
            )

        self._api: Win32Bindings | None = None
        self._token: int | None = None
        self._write_sid_ptr: int | None = None
        self._temp_write_sid_ptr: int | None = None
        self._temp_dir: str | None = temp_dir if mode == "workspace-write" else None
        #: Buffers this process owns, held for as long as the sandbox lives.
        #:
        #: The logon SID and the Everyone SID are ``ctypes`` buffers, so their
        #: memory belongs to the interpreter and the *owner* is what has to be
        #: kept: an address alone would be used by ``CreateRestrictedToken`` after
        #: the collector had already handed those bytes to the next allocation.
        #: They are released by dropping the reference — never by ``LocalFree``,
        #: which is for the SIDs the OS allocated (``ConvertStringSidToSidW``).
        self._owned_sids: list[ctypes.Array] = []
        self._granted: list[tuple[str, int]] = []
        #: The host-named roots' parsed SIDs, each beside the path it was derived
        #: from.  They are OS allocations like the workspace's and the temp's — the
        #: token points at them for the child's whole life — so they are freed on
        #: the same two exits and by the same helper.
        self._extra_sid_ptrs: list[tuple[str, int]] = []

    @property
    def temp_dir(self) -> str | None:
        """The resolved private temp directory (``None`` when temp writes are disabled).

        :returns: the directory, or ``None``.
        """
        return self._temp_dir

    def init(self, api: Win32Bindings | None = None) -> None:
        """Create the restricted token and apply the capability-SID grants.

        Fail-closed: any failure revokes the revocable (temp) grants and throws.
        Standing workspace ACEs are **not** revoked on the failure path — they
        are the intended end state (the reuse cache), not an error artifact.

        :param api: a binding table to use (tests); defaults to the host's.
        :raises Win32Error: when a Win32 call failed.
        :raises RuntimeError: when a call failed and cleanup failed too.
        """
        if self._api is not None:
            raise RuntimeError("AclSandbox is already initialized")
        bindings = api if api is not None else win32()
        current_token = open_current_process_token(bindings)
        token_open = True
        restricted_token: int | None = None
        try:
            self._write_sid_ptr = (
                None if self.write_sid is None else _parse_sid(bindings, self.write_sid)
            )
            self._temp_write_sid_ptr = (
                None if self.temp_write_sid is None else _parse_sid(bindings, self.temp_write_sid)
            )
            for path, sid in self.extra_grants:
                self._extra_sid_ptrs.append((path, _parse_sid(bindings, sid)))
            if self._temp_dir is not None:
                if not os.path.isdir(self._temp_dir):
                    raise ValueError(
                        f"AclSandbox temp dir does not exist or is not a directory: {self._temp_dir}"
                    )
                # The extra roots belong in this check, not only the workspace: a
                # root that *contains* the private temp would hand the temp's
                # revocable capability the whole tree it sits in, so the temp would
                # stop being private — the same reason the workspace is checked.
                assert_private_temp_disjoint(
                    [*self.writable_dirs, *(path for path, _ in self.extra_grants)],
                    self._temp_dir,
                )

            if self.manage_dacls and self._write_sid_ptr is not None:
                for path in self.writable_dirs:
                    grant_write(bindings, path, self._write_sid_ptr)
                # Standing, exactly like the workspace ACE, and for the same
                # reason: the SID is a pure function of the canonical path, so the
                # ACE is a cache rather than session state — and the *policy* is
                # what grants it per call (`--extra-root`), so a host who removes
                # the root stops passing it and the very next call's token no
                # longer carries the SID.  Revoking instead would put a whole-tree
                # propagation on a hot path and buy nothing.
                for path, sid_ptr in self._extra_sid_ptrs:
                    grant_write(bindings, path, sid_ptr)
                if self._temp_dir is not None and self._temp_write_sid_ptr is not None:
                    # Recorded before the grant: grant_write can throw after a
                    # successful apply, and the fail-closed path must still
                    # revoke that path.
                    self._granted.append((self._temp_dir, self._temp_write_sid_ptr))
                    grant_write(bindings, self._temp_dir, self._temp_write_sid_ptr)

            logon_sid = find_logon_sid(bindings, current_token)
            self._owned_sids.append(logon_sid.buffer)
            world_sid = make_well_known_sid(bindings, abi.WIN_WORLD_SID)
            self._owned_sids.append(world_sid.buffer)
            write_sids = [
                sid
                for sid in (
                    self._write_sid_ptr,
                    self._temp_write_sid_ptr,
                    *(sid_ptr for _, sid_ptr in self._extra_sid_ptrs),
                )
                if sid is not None
            ]
            restricted_token = create_restricted_token(
                bindings, current_token, logon_sid.address, write_sids, world_sid.address, self.mode
            )
            self._token = restricted_token
            # The restricted token's default DACL still names only the user's
            # ambient SIDs — none of the restricting ones — and every NEW object
            # the confined process creates takes its DACL from that default.  A
            # SID that fails either half of the write-restricted check therefore
            # denies the object to its own creator, which is what broke pipes and
            # every captured-stdio grandchild spawn at ``workspace-write``.
            #
            # The logon SID is the narrowest argument that passes both halves: the
            # token holds it as a normal group *and* as a restrictor, by
            # construction, and an object granted to it stays inside this logon
            # session instead of being handed to EVERYONE.  A capability SID looks
            # narrower and is the wrong choice — it is in the restricting list
            # alone, so the enabled-group pass never runs in its favour.
            #
            # ``token.set_token_default_dacl_grant``'s docstring carries the
            # measured rule and the six rows behind it (issue #1560); the two SIDs
            # that also passed are why this argument is the *narrowest* one and
            # not simply a working one.
            # This is a deliberate divergence from the blueprint, recorded as one
            # rather than landed as a silent improvement (issue #1615).  The
            # blueprint's call is byte-identical in shape and still names the
            # capability SID at its newest revision — ``src/index.ts:296``
            # (``ddefc45fbc``, the pin) and ``src/index.ts:303`` (``477b4f4205``,
            # ``dsh-v0.1.7-rc.2``), with ``setTokenDefaultDaclGrant``
            # (``src/token.ts:112``) merging that one ACE at both — so the two-pass
            # failure is inherited rather than a port slip, and the table that
            # varies only this argument is what justifies leaving the blueprint
            # here.
            set_token_default_dacl_grant(bindings, restricted_token, logon_sid.address)
            if int(bindings.kernel32.CloseHandle(ctypes.c_void_p(current_token))) == 0:
                throw_last_error(bindings, "CloseHandle", "current process token")
            token_open = False
            self._api = bindings
        except BaseException as error:  # noqa: BLE001 - re-raised below, after cleanup
            failures: list[BaseException] = []
            if token_open:
                try:
                    if int(bindings.kernel32.CloseHandle(ctypes.c_void_p(current_token))) == 0:
                        throw_last_error(bindings, "CloseHandle", "current process token after init failure")
                except BaseException as exc:  # noqa: BLE001 - collected
                    failures.append(exc)
            if restricted_token is not None:
                try:
                    if int(bindings.kernel32.CloseHandle(ctypes.c_void_p(restricted_token))) == 0:
                        throw_last_error(bindings, "CloseHandle", "restricted token after init failure")
                except BaseException as exc:  # noqa: BLE001 - collected
                    failures.append(exc)
            for path, sid_ptr in self._granted:
                try:
                    revoke_write(bindings, path, sid_ptr)
                except BaseException as exc:  # noqa: BLE001 - collected
                    failures.append(exc)
            _free_sid_best_effort(bindings, self._write_sid_ptr, "workspace write SID", failures)
            _free_sid_best_effort(bindings, self._temp_write_sid_ptr, "temp write SID", failures)
            for path, sid_ptr in self._extra_sid_ptrs:
                _free_sid_best_effort(bindings, sid_ptr, f"extra root write SID ({path})", failures)
            self._owned_sids = []
            self._token = None
            self._write_sid_ptr = None
            self._temp_write_sid_ptr = None
            self._extra_sid_ptrs = []
            self._granted = []
            if failures:
                aggregate = RuntimeError(
                    f"AclSandbox init failed and {len(failures)} cleanup operation(s) also failed"
                )
                aggregate.failures = failures  # type: ignore[attr-defined]
                raise aggregate from error
            raise

    def spawn(self, argv: list[str], *, cwd: str | None = None) -> RestrictedChild:
        """Spawn one child under the restricted token, inheriting this stdio.

        :param argv: the program plus its arguments.
        :param cwd: the child's working directory; ``None`` keeps this process's.
        :returns: the running child.
        :raises RuntimeError: when the sandbox is not initialized.
        :raises Win32Error: when the spawn failed.
        """
        if self._api is None or self._token is None:
            raise RuntimeError("AclSandbox is not initialized")
        return spawn_restricted(self._api, self._token, argv, cwd=cwd)

    def dispose(self) -> None:
        """Revoke the temp grants, keep the standing ones, free every allocation.

        :raises RuntimeError: when one or more cleanup operations failed, with
            every failure attached.
        """
        api = self._api
        if api is None:
            return
        failures: list[BaseException] = []
        for path, sid_ptr in self._granted:
            try:
                revoke_write(api, path, sid_ptr)
            except BaseException as exc:  # noqa: BLE001 - collected
                failures.append(exc)
        self._granted = []
        self._owned_sids = []
        _free_sid_best_effort(api, self._write_sid_ptr, "workspace write SID", failures)
        _free_sid_best_effort(api, self._temp_write_sid_ptr, "temp write SID", failures)
        for path, sid_ptr in self._extra_sid_ptrs:
            _free_sid_best_effort(api, sid_ptr, f"extra root write SID ({path})", failures)
        self._write_sid_ptr = None
        self._temp_write_sid_ptr = None
        self._extra_sid_ptrs = []
        if self._token is not None:
            try:
                if int(api.kernel32.CloseHandle(ctypes.c_void_p(self._token))) == 0:
                    throw_last_error(api, "CloseHandle", "restricted token")
            except BaseException as exc:  # noqa: BLE001 - collected
                failures.append(exc)
            self._token = None
        self._api = None
        if failures:
            error = RuntimeError(
                f"AclSandbox dispose completed with {len(failures)} cleanup failure(s)"
            )
            error.failures = failures  # type: ignore[attr-defined]
            raise error
