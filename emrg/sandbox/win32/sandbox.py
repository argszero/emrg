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

        if mode == "workspace-write" and write_sid is None:
            raise ValueError(
                "AclSandbox workspace-write requires a write SID — derive it from the workspace "
                "via workspace_write_sid()"
            )
        if mode == "read-only" and (write_sid is not None or temp_write_sid is not None):
            raise ValueError("AclSandbox read-only does not accept write SIDs")
        if write_sid is not None and temp_write_sid == write_sid:
            raise ValueError("AclSandbox workspace and temp write SIDs must be distinct")
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
            if self._temp_dir is not None:
                if not os.path.isdir(self._temp_dir):
                    raise ValueError(
                        f"AclSandbox temp dir does not exist or is not a directory: {self._temp_dir}"
                    )
                assert_private_temp_disjoint(self.writable_dirs, self._temp_dir)

            if self.manage_dacls and self._write_sid_ptr is not None:
                for path in self.writable_dirs:
                    grant_write(bindings, path, self._write_sid_ptr)
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
                sid for sid in (self._write_sid_ptr, self._temp_write_sid_ptr) if sid is not None
            ]
            restricted_token = create_restricted_token(
                bindings, current_token, logon_sid.address, write_sids, world_sid.address, self.mode
            )
            self._token = restricted_token
            # The restricted token's default DACL still names only the user's
            # ambient SIDs — none of the restricting ones.  Every NEW object the
            # confined process creates takes its DACL from that default, so the
            # write check would deny pipe creation and break every piped-stdio
            # grandchild spawn.  Choosing the temp SID first keeps default-DACL
            # objects in one session's temp tree from acquiring the shared
            # workspace capability.
            #
            # Two lists, not one, and the fallback is why the tiers differ: a
            # restricting-only SID is refused by the enabled-group pass, so the
            # temp write SID (this branch) fails where EVERYONE (the read-only
            # fallback, ``world_sid``) passes.  The measured rule and the six rows
            # behind it are in ``token.set_token_default_dacl_grant``'s docstring
            # — read it before changing this argument (issue #1560).
            set_token_default_dacl_grant(
                bindings,
                restricted_token,
                self._temp_write_sid_ptr or self._write_sid_ptr or world_sid.address,
            )
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
            self._owned_sids = []
            self._token = None
            self._write_sid_ptr = None
            self._temp_write_sid_ptr = None
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
        self._write_sid_ptr = None
        self._temp_write_sid_ptr = None
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
