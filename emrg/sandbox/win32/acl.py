"""DACL editing: grant and revoke a capability SID on a directory.

Ported from the blueprint's ``acl.ts`` (``sandbox-windows-acl``, dsh
0.1.6-alpha.2): the same ``SetEntriesInAclW`` + ``SetNamedSecurityInfoW``
read-merge-write, the same per-path exclusive ``LockFileEx`` serialization, and
the same failure handling the original proof-of-concept lacked (every call is
checked; every failure names the API, the exact code, the system text and the
path).

Two contracts carried verbatim because both were learned the hard way:

* the ACL pointer **sits inside** the security-descriptor allocation, so only
  the descriptor may be freed and only after the merge consumed the ACL —
  freeing the ACL pointer itself corrupts the heap;
* the exact-ACE check is what keeps provisioning O(1) per workspace: without
  it, ``SetNamedSecurityInfoW`` re-propagates an identical inheritable ACE
  across the whole tree on every provision (minutes on a large workspace).
"""

from __future__ import annotations

import ctypes
import hashlib
import os

from emrg.sandbox.win32 import abi
from emrg.sandbox.win32.ffi import (
    ACLStructure,
    ExplicitAccessW,
    Overlapped,
    Win32Bindings,
    alloc_ptr_slot,
    decode_ptr,
    get_temp_path,
    is_invalid_handle,
    is_null_ptr,
    local_free,
    read_sid_bytes,
    throw_last_error,
    throw_win32,
)


def build_explicit_access(sid_ptr: int, mode: int, permissions: int) -> ExplicitAccessW:
    """Pack one ``EXPLICIT_ACCESS_W``.

    ``permissions`` is the access mask; ``REVOKE_ACCESS`` passes 0, which
    removes every ACE naming that trustee.

    :param sid_ptr: the trustee SID the entry names.
    :param mode: the access mode (``GRANT_ACCESS`` or ``REVOKE_ACCESS``).
    :param permissions: the access mask to grant.
    :returns: the packed entry.
    """
    entry = ExplicitAccessW()
    entry.grfAccessPermissions = permissions
    entry.grfAccessMode = mode
    entry.grfInheritance = abi.SUB_CONTAINERS_AND_OBJECTS_INHERIT
    entry.Trustee.pMultipleTrustee = None
    entry.Trustee.MultipleTrusteeOperation = abi.NO_MULTIPLE_TRUSTEE
    entry.Trustee.TrusteeForm = abi.TRUSTEE_IS_SID
    entry.Trustee.TrusteeType = abi.TRUSTEE_IS_UNKNOWN
    entry.Trustee.ptstrName = ctypes.c_void_p(sid_ptr)
    return entry


def lock_file_path(api: Win32Bindings, path: str) -> str:
    """The lock file one protected directory's DACL edits serialize on.

    ``<GetTempPathW()>\\emrg-acl-locks\\<first 16 hex of sha256(lowercased
    path)>.lock``.  The lowercasing maps Windows's case-insensitive path
    spellings onto one lock, and the lock root derives from ``GetTempPathW``
    rather than from the runner's argv or any environment variable a confined
    caller could move.

    :param api: the binding table.
    :param path: the protected directory (absolute).
    :returns: the lock file path for that directory.
    """
    digest = hashlib.sha256(path.lower().encode("utf-8")).hexdigest()[:16]
    return os.path.join(get_temp_path(api), "emrg-acl-locks", f"{digest}.lock")


class _PathLock:
    """The per-path exclusive lock as a context manager (see :func:`with_path_lock`)."""

    def __init__(self, api: Win32Bindings, path: str) -> None:
        self._api = api
        self._path = path
        self._handle: int | None = None
        self._overlapped = Overlapped()

    def __enter__(self) -> None:
        lock_path = lock_file_path(self._api, self._path)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        handle = self._api.kernel32.CreateFileW(
            lock_path,
            abi.GENERIC_READ | abi.GENERIC_WRITE,
            abi.FILE_SHARE_READ | abi.FILE_SHARE_WRITE,
            None,
            abi.OPEN_ALWAYS,
            0,
            None,
        )
        handle_value = int(handle or 0)
        if is_invalid_handle(handle_value):
            throw_last_error(self._api, "CreateFileW", lock_path)
        self._handle = handle_value
        locked = int(self._api.kernel32.LockFileEx(
            ctypes.c_void_p(handle_value), abi.LOCKFILE_EXCLUSIVE_LOCK, 0, 1, 0,
            ctypes.byref(self._overlapped),
        ))
        if locked == 0:
            code = self._api.get_last_error()
            self._api.kernel32.CloseHandle(ctypes.c_void_p(handle_value))
            self._handle = None
            throw_win32(self._api, "LockFileEx", code, lock_path)

    def __exit__(self, exc_type, exc, tb) -> bool:
        handle = self._handle
        if handle is None:
            return False
        if exc is not None:
            # Best-effort release on the action-failure path: a cleanup failure
            # must not mask the action's own error.
            self._api.kernel32.UnlockFileEx(
                ctypes.c_void_p(handle), 0, 1, 0, ctypes.byref(self._overlapped)
            )
            self._api.kernel32.CloseHandle(ctypes.c_void_p(handle))
            self._handle = None
            return False
        unlocked = int(self._api.kernel32.UnlockFileEx(
            ctypes.c_void_p(handle), 0, 1, 0, ctypes.byref(self._overlapped)
        ))
        if unlocked == 0:
            code = self._api.get_last_error()
            self._api.kernel32.CloseHandle(ctypes.c_void_p(handle))
            self._handle = None
            throw_win32(self._api, "UnlockFileEx", code, self._path)
        closed = int(self._api.kernel32.CloseHandle(ctypes.c_void_p(handle)))
        self._handle = None
        if closed == 0:
            throw_last_error(self._api, "CloseHandle", f"lock file {self._path}")
        return False


def with_path_lock(api: Win32Bindings, path: str) -> _PathLock:
    """Serialize one path's get-merge-set sequence against other sandboxes.

    A grant is a read-merge-write against the directory's **current** DACL, so
    two sandbox instances provisioning the same workspace concurrently would
    otherwise clobber each other's ACEs.

    :param api: the binding table.
    :param path: the protected directory (absolute).
    :returns: the context manager holding the lock.
    """
    return _PathLock(api, path)


class CurrentDacl:
    """A directory's current explicit DACL and the descriptor that owns it.

    The ACL pointer is inside the descriptor allocation: free the descriptor
    (which frees the ACL with it) and never the ACL pointer.
    """

    __slots__ = ("api", "old_acl", "descriptor")

    def __init__(self, api: Win32Bindings, old_acl: int | None, descriptor: int | None) -> None:
        self.api = api
        self.old_acl = old_acl
        self.descriptor = descriptor

    def release(self, label: str) -> None:
        """Free the owning descriptor, reporting a failure.

        :param label: the caller's name for error details.
        :raises Win32Error: when ``LocalFree`` reported a failure.
        """
        if self.descriptor is None:
            return
        freed = local_free(self.api, self.descriptor)
        self.descriptor = None
        if not is_null_ptr(freed):
            throw_last_error(self.api, "LocalFree", f"{label} descriptor")


def read_current_dacl(api: Win32Bindings, path: str) -> CurrentDacl:
    """Read the directory's current explicit DACL.

    :param api: the binding table.
    :param path: the directory whose DACL is read.
    :returns: the current DACL and its owning descriptor.
    :raises Win32Error: when the read failed.
    """
    owner = alloc_ptr_slot()
    group = alloc_ptr_slot()
    dacl = alloc_ptr_slot()
    sacl = alloc_ptr_slot()
    descriptor = alloc_ptr_slot()
    result = int(api.advapi32.GetNamedSecurityInfoW(
        path,
        abi.SE_FILE_OBJECT,
        abi.DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        ctypes.byref(group),
        ctypes.byref(dacl),
        ctypes.byref(sacl),
        ctypes.byref(descriptor),
    ))
    if result != abi.ERROR_SUCCESS:
        throw_win32(api, "GetNamedSecurityInfoW", result, path)
    old_acl = decode_ptr(dacl) or None
    return CurrentDacl(api, old_acl, decode_ptr(descriptor) or None)


def merge_and_apply(
    api: Win32Bindings,
    path: str,
    entry: ExplicitAccessW,
    current: CurrentDacl,
    label: str,
) -> None:
    """Merge one entry into the current DACL and apply the result.

    The shared tail of :func:`grant_write` and :func:`revoke_write`: merge
    (``old_acl`` NULL means "no explicit DACL yet", and ``SetEntriesInAclW``
    builds one from scratch), free the descriptor **before** applying, apply,
    then free the merged ACL — checking every call.

    :param api: the binding table.
    :param path: the directory the edit applies to.
    :param entry: the ``EXPLICIT_ACCESS_W`` to merge.
    :param current: the current DACL from :func:`read_current_dacl`.
    :param label: the caller's name for error details.
    :raises Win32Error: when any call failed.
    """
    new_acl_slot = alloc_ptr_slot()
    merge_result = int(api.advapi32.SetEntriesInAclW(
        1,
        ctypes.byref(entry),
        ctypes.c_void_p(current.old_acl or 0),
        ctypes.byref(new_acl_slot),
    ))
    if merge_result != abi.ERROR_SUCCESS:
        if current.descriptor is not None:
            local_free(api, current.descriptor)
            current.descriptor = None
        throw_win32(api, "SetEntriesInAclW", merge_result, f"{label}({path})")
    new_acl = decode_ptr(new_acl_slot)
    if is_null_ptr(new_acl):
        if current.descriptor is not None:
            local_free(api, current.descriptor)
            current.descriptor = None
        throw_win32(api, "SetEntriesInAclW", api.get_last_error(), f"{label}({path}): null new ACL")

    # The descriptor block (old_acl included) is dead after the merge.
    freed_descriptor = None
    if current.descriptor is not None:
        freed_descriptor = local_free(api, current.descriptor)
        current.descriptor = None
    apply_result = int(api.advapi32.SetNamedSecurityInfoW(
        path,
        abi.SE_FILE_OBJECT,
        abi.DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.c_void_p(new_acl),
        None,
    ))
    freed_new = local_free(api, new_acl)
    if apply_result != abi.ERROR_SUCCESS:
        throw_win32(api, "SetNamedSecurityInfoW", apply_result, f"{label}({path})")
    if freed_descriptor is not None and not is_null_ptr(freed_descriptor):
        throw_last_error(api, "LocalFree", f"{label}({path}) descriptor")
    if not is_null_ptr(freed_new):
        throw_last_error(api, "LocalFree", f"{label}({path}) new ACL")


def has_exact_grant(api: Win32Bindings, old_acl: int, sid_ptr: int) -> bool:
    """Whether the DACL already carries the exact write grant this module adds.

    Every field is read through bounded offset reads (Allow ACE, ``OI|CI``
    inheritance, :data:`~emrg.sandbox.win32.abi.GRANT_MASK`, the capability
    SID) — no pointer arithmetic, no ``memcpy``.  The ACE's SID is **inline**
    (embedded after the 4-byte mask), so there is no pointer to follow: it is
    read byte-wise against the ACE's own size.  A malformed header reads as "no
    exact grant", which sends the caller down the merge path — the path that
    owns the robust failure handling.

    :param api: the binding table.
    :param old_acl: the current explicit DACL pointer.
    :param sid_ptr: the capability SID to match.
    :returns: whether the exact grant ACE is already present.
    """
    header = ACLStructure.from_address(old_acl)
    acl_size = int(header.AclSize)
    ace_count = int(header.AceCount)
    if acl_size < 8 or acl_size > 1_048_576:
        return False  # implausible: fall back to the merge path
    target = read_sid_bytes(api, sid_ptr)
    offset = 8  # the first ACE follows the 8-byte ACL header
    for _ in range(ace_count):
        ace_type = ctypes.c_ubyte.from_address(old_acl + offset).value
        ace_flags = ctypes.c_ubyte.from_address(old_acl + offset + 1).value
        ace_size = int(ctypes.c_uint16.from_address(old_acl + offset + 2).value)
        if ace_size < 8 or offset + ace_size > acl_size:
            return False  # implausible: fall back to the merge path
        mask = int(ctypes.c_uint32.from_address(old_acl + offset + 4).value)
        exact = (
            ace_type == abi.ACCESS_ALLOWED_ACE_TYPE
            and ace_flags == abi.SUB_CONTAINERS_AND_OBJECTS_INHERIT
            and mask == abi.GRANT_MASK
        )
        if exact:
            inline = _inline_sid_bytes(old_acl + offset + 8, ace_size - 8)
            if inline is not None and inline == target:
                return True
        offset += ace_size
    return False


def _inline_sid_bytes(address: int, available: int) -> bytes | None:
    """Read the inline SID of an ``ACCESS_ALLOWED_ACE`` within its own bounds.

    :param address: the SID's address inside the ACE.
    :param available: how many bytes of the ACE remain at that address.
    :returns: the SID's bytes, or ``None`` when the SID does not fit.
    """
    if available < 8:
        return None
    sub_authority_count = int(ctypes.c_ubyte.from_address(address + 1).value)
    if sub_authority_count > abi.SID_MAX_SUB_AUTHORITIES:
        return None
    length = 8 + 4 * sub_authority_count
    if length > available:
        return None
    return ctypes.string_at(address, length)


def grant_write(api: Win32Bindings, path: str, sid_ptr: int) -> None:
    """Grant :data:`~emrg.sandbox.win32.abi.GRANT_MASK` to a capability SID.

    Idempotent: when the directory's current DACL already carries the exact ACE
    (a grant surviving from a previous server lifetime), the apply is
    **skipped** — it would otherwise re-propagate an identical inheritable ACE
    across the whole tree.  Otherwise read-merge-write, so pre-existing
    explicit ACEs survive.  Runs under the per-path lock.  The directory must
    be owned by the caller (owner-implicit ``WRITE_DAC``), the same
    precondition as the blueprint.

    :param api: the binding table.
    :param path: the directory whose DACL gains the grant.
    :param sid_ptr: the capability SID the ACE names.
    :raises Win32Error: when any call failed.
    """
    with with_path_lock(api, path):
        current = read_current_dacl(api, path)
        if current.old_acl is not None and has_exact_grant(api, current.old_acl, sid_ptr):
            current.release(f"grantWrite({path})")
            return
        entry = build_explicit_access(sid_ptr, abi.GRANT_ACCESS, abi.GRANT_MASK)
        merge_and_apply(api, path, entry, current, "grantWrite")


def revoke_write(api: Win32Bindings, path: str, sid_ptr: int) -> bool:
    """Remove every ACE naming a capability SID from a directory's DACL.

    Other entries are preserved (the merge is a ``REVOKE_ACCESS`` of one
    trustee).

    :param api: the binding table.
    :param path: the directory whose DACL loses the capability SID's ACEs.
    :param sid_ptr: the capability SID whose ACEs are removed.
    :returns: whether an ACE removal was attempted (``False`` when the
        directory carries no explicit DACL at all).
    :raises Win32Error: when any call failed.
    """
    with with_path_lock(api, path):
        current = read_current_dacl(api, path)
        if current.old_acl is None:
            current.release(f"revokeWrite({path})")
            return False
        entry = build_explicit_access(sid_ptr, abi.REVOKE_ACCESS, 0)
        merge_and_apply(api, path, entry, current, "revokeWrite")
        return True
