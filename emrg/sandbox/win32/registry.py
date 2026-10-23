"""DACL editing for a registry key: grant and revoke a capability SID on a key.

The registry counterpart of :mod:`emrg.sandbox.win32.acl`, and deliberately a
second module rather than a second mode of that one: the two edit one kind of
thing — a security descriptor's DACL — through APIs that have almost nothing in
common. A path is addressed by name, and its DACL is read and written by
``GetNamedSecurityInfoW`` / ``SetNamedSecurityInfoW``, which hand back a
self-relative descriptor and take one back. A key is addressed by an opened
handle, and its descriptor is read and written by ``RegGetKeySecurity`` /
``RegSetKeySecurity`` — so the merged ACL has to be assembled into a self-relative
descriptor here, through ``InitializeSecurityDescriptor`` →
``SetSecurityDescriptorDacl`` → ``MakeSelfRelativeSD``. One module with two
bodies would be one function with two of everything; the parts that genuinely
are shared — the ``EXPLICIT_ACCESS_W`` packing, the ACE merge, the exact-ACE
check, the per-object lock — are imported from ``acl.py``, not copied.

Three contracts, the first two carried over from that module:

* the ACL pointer returned by ``SetEntriesInAclW`` is its **own** allocation and
  must be freed, while a descriptor's DACL points *into* the descriptor and must
  not be;
* the exact-ACE check keeps provisioning idempotent: without it every provision
  re-merges an identical ACE, which re-propagates it across the key's subkeys;
* **the merge preserves what is already there.** The DACL a key carries is where
  the user's own access comes from. The ACE added here is an addition, never a
  replacement — a rebuild built from the capability SID alone would lock the host
  out of their own key. So the current DACL is read, merged into, and written
  back; and a key whose DACL is **absent** (a NULL DACL, which grants everyone
  full control) is refused rather than replaced, because narrowing that is not
  this module's decision to make.

The key's spelling is parsed once (:func:`split_key`) and its capability SID is
derived from that same spelling (:func:`~emrg.sandbox.win32.sid.registry_write_sid`)
by both halves of the grant — the ACE applied here and the SID the runner puts in
the restricted token — so the two cannot disagree about which key was granted.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from emrg.sandbox.win32 import abi
from emrg.sandbox.win32.acl import (
    build_explicit_access,
    has_exact_grant,
    with_path_lock,
)
from emrg.sandbox.win32.ffi import (
    Win32Bindings,
    alloc_ptr_slot,
    decode_ptr,
    is_null_ptr,
    local_free,
    throw_last_error,
    throw_win32,
)

#: The predefined roots a grant may name, by their accepted spellings.
#:
#: Only the two hives a per-session grant can mean anything under: a grant names
#: what one confined process may write, which is the current user's own state
#: (``HKCU``) or a machine-wide key that is already writable (``HKLM``). ``HKU``,
#: ``HKCR`` and ``HKCC`` are views onto the same two, and accepting a third
#: spelling of one place would let two grants of one key derive two SIDs.
KEY_ROOTS: dict[str, int] = {
    "HKCU": abi.HKEY_CURRENT_USER,
    "HKEY_CURRENT_USER": abi.HKEY_CURRENT_USER,
    "HKLM": abi.HKEY_LOCAL_MACHINE,
    "HKEY_LOCAL_MACHINE": abi.HKEY_LOCAL_MACHINE,
}


class RegistryKeyError(ValueError):
    """A grant's target is not a registry key this module can address."""


class KeyDaclRefused(RuntimeError):
    """A key's DACL is not one this module will edit."""


class KeyDacl:
    """A key's current DACL pointer and the buffer that owns it.

    ``RegGetKeySecurity`` writes the descriptor into a buffer the caller supplies,
    and the DACL is a pointer *into* that buffer — so the two travel together or
    the pointer is meaningless. It is meaningless in a way that does not raise:
    the pointer's next consumers are :func:`~emrg.sandbox.win32.acl.has_exact_grant`,
    which walks it as an ACL header, and ``SetEntriesInAclW``, which copies every
    ACE it is told is there. On a collected buffer those read an allocator's
    bookkeeping as ``AclSize``/``AceCount``, so the walk runs off the end of a
    freed block and the copy can overrun the new ACL: the heap is corrupted and
    some later call is the one that dies.

    This is :class:`~emrg.sandbox.win32.acl.CurrentDacl` in the registry's idiom,
    and the two files carry one each because nothing else keeps a descriptor
    alive: the answer holds the buffer, so a caller that holds the answer holds
    the memory its pointer addresses.
    """

    __slots__ = ("_buffer", "pointer")

    def __init__(self, pointer: int, buffer: ctypes.Array) -> None:
        self._buffer = buffer
        self.pointer = pointer

    @property
    def buffer(self) -> ctypes.Array:
        """The descriptor buffer :attr:`pointer` addresses.

        Exposed so a test can assert the pointer lies inside it, and for nothing
        else: the buffer is owned here and is not the caller's to free or resize.

        :returns: the buffer this answer keeps alive.
        """
        return self._buffer


def split_key(key: str) -> tuple[int, str]:
    """Split a key's full spelling into its predefined root and its subkey path.

    Pure, and measured on every platform: the parsing is the half of this module
    that says which key a grant named, so it is the half a POSIX leg can hold to
    its rule.

    A **bare hive** is refused. ``HKCU`` alone names the whole user hive, and a
    grant that reaches every key the user owns is a widening nobody asked for —
    the same reason a path grant names the directory rather than the volume.

    :param key: the full spelling, e.g. ``HKCU\\Software\\LarkCli\\keychain``.
    :returns: the predefined ``HKEY`` value and the subkey path.
    :raises RegistryKeyError: when the spelling names no key this module can
        address, with the reason in the message.
    """
    if not isinstance(key, str):
        raise RegistryKeyError(f"a registry grant needs a key spelling, not {type(key).__name__}")
    spelling = key.strip()
    if not spelling:
        raise RegistryKeyError("a registry grant needs a key spelling, not an empty string")
    head, separator, tail = spelling.partition("\\")
    if not separator:
        raise RegistryKeyError(
            f"registry key {spelling!r} names a whole hive; a grant names the key it needs, "
            "e.g. HKCU\\Software\\Vendor\\Component"
        )
    root = KEY_ROOTS.get(head.upper())
    if root is None:
        raise RegistryKeyError(
            f"registry key {spelling!r} starts with {head!r}, which is not one of "
            f"{', '.join(sorted(KEY_ROOTS))}"
        )
    subkey = tail.strip("\\")
    if not subkey:
        raise RegistryKeyError(
            f"registry key {spelling!r} names a whole hive; a grant names the key it needs, "
            "e.g. HKCU\\Software\\Vendor\\Component"
        )
    return root, subkey


#: The one spelling each root is **stored** as, by the ``HKEY`` value it resolves to.
#:
#: :data:`KEY_ROOTS` accepts two spellings per hive because a host may type
#: either, and this is the second half of that decision: a capability SID is
#: derived from the spelling (:func:`~emrg.sandbox.win32.sid.registry_write_sid`),
#: so two spellings of one key would be two identities on one key — the same
#: failure the ``KEY_ROOTS`` note names for a *third* hive spelling, one level in.
CANONICAL_ROOT_NAMES: dict[int, str] = {
    abi.HKEY_CURRENT_USER: "HKCU",
    abi.HKEY_LOCAL_MACHINE: "HKLM",
}


def canonical_key_spelling(key: str) -> str:
    """The one spelling a key grant is stored as: the short root, then the subkey.

    The registry namespace is case-insensitive and
    :func:`~emrg.sandbox.win32.sid.registry_write_sid` lowercases what it derives,
    so the subkey keeps the case the host typed — it is display.  What has to
    converge is the **root**, where ``HKCU`` and ``HKEY_CURRENT_USER`` are two
    accepted spellings of one hive and would otherwise derive two SIDs for one
    key.

    :param key: the key's full spelling, in any accepted form.
    :returns: the canonical spelling, e.g. ``HKCU\\Software\\LarkCli``.
    :raises RegistryKeyError: when the spelling names no key (:func:`split_key`).
    """
    root, subkey = split_key(key)
    return f"{CANONICAL_ROOT_NAMES[root]}\\{subkey}"


def key_exists(key: str) -> bool:
    """Whether this host has the key.

    The registry counterpart of ``os.path.isdir`` in the manifest's totality rule:
    a default grant naming a key nobody created is **absent**, never an error, so
    a host that has never run the tool that owns the key is not a broken boundary.

    Answers ``False`` off Windows, where ``winreg`` does not exist — which is what
    lets the one derivation read every platform's manifest row on every platform.

    :param key: the key's full spelling.
    :returns: whether the key is present and readable.
    """
    try:
        root, subkey = split_key(key)
    except RegistryKeyError:
        return False
    try:
        import winreg  # noqa: PLC0415 - Windows-only stdlib, imported where it exists
    except ImportError:  # pragma: no cover - the POSIX legs
        return False
    hive = winreg.HKEY_CURRENT_USER if root == abi.HKEY_CURRENT_USER else winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ):
            return True
    except OSError:
        return False


def open_key(api: Win32Bindings, key: str) -> int:
    """Open a key for the two rights a DACL edit needs, and nothing else.

    Asking for exactly ``READ_CONTROL | WRITE_DAC`` means a key the caller may not
    re-DACL fails here, at the open, instead of half way through a merge.

    :param api: the binding table.
    :param key: the key's full spelling.
    :returns: the key handle.
    :raises RegistryKeyError: when the spelling is not one this module addresses.
    :raises Win32Error: when the open failed.
    """
    root, subkey = split_key(key)
    handle_slot = alloc_ptr_slot()
    status = int(api.advapi32.RegOpenKeyExW(
        ctypes.c_void_p(root),
        subkey,
        0,
        abi.KEY_SECURITY_ACCESS,
        ctypes.byref(handle_slot),
    ))
    if status != abi.ERROR_SUCCESS:
        throw_win32(api, "RegOpenKeyExW", status, key)
    handle = decode_ptr(handle_slot)
    if is_null_ptr(handle):
        throw_win32(api, "RegOpenKeyExW", api.get_last_error(), f"{key}: null key handle")
    return int(handle)


def close_key(api: Win32Bindings, handle: int, key: str) -> None:
    """Close one key handle, reporting a failure.

    :param api: the binding table.
    :param handle: the handle :func:`open_key` returned.
    :param key: the key's full spelling, for the error message.
    :raises Win32Error: when ``RegCloseKey`` reported a failure.
    """
    status = int(api.advapi32.RegCloseKey(ctypes.c_void_p(handle)))
    if status != abi.ERROR_SUCCESS:
        throw_win32(api, "RegCloseKey", status, key)


def _close_key_best_effort(api: Win32Bindings, handle: int) -> None:
    """Close a handle on a path that is already failing.

    A cleanup failure must not mask the action's own error — the rule
    ``acl.py``'s lock states — so this call's status is dropped and only the
    original exception travels.

    :param api: the binding table.
    :param handle: the handle to close.
    """
    try:
        api.advapi32.RegCloseKey(ctypes.c_void_p(handle))
    except BaseException:  # noqa: BLE001 - the caller is already raising
        pass


def read_current_key_dacl(api: Win32Bindings, key: str, handle: int) -> KeyDacl | None:
    """Read the key's current explicit DACL, with the buffer that owns it.

    The descriptor is read into a buffer this module owns, so nothing here is
    freed with ``LocalFree``: the DACL pointer refers *into* that buffer, and the
    two are therefore handed back together — as a :class:`KeyDacl`, never as a
    bare pointer, because a pointer alone outlives nothing and its consumers
    would read freed memory instead of raising.

    :param api: the binding table.
    :param key: the key's full spelling, for error messages.
    :param handle: the open key handle.
    :returns: the DACL pointer with the buffer that owns it, or ``None`` when the
        key carries no DACL.
    :raises Win32Error: when a call failed.
    :raises KeyDaclRefused: when the key carries a NULL DACL, which grants
        everyone full control; replacing it would narrow access the host has
        today, and that is not this module's decision.
    """
    size = wintypes.DWORD(0)
    probe = int(api.advapi32.RegGetKeySecurity(
        ctypes.c_void_p(handle), abi.DACL_SECURITY_INFORMATION, None, ctypes.byref(size),
    ))
    if probe != abi.ERROR_INSUFFICIENT_BUFFER:
        throw_win32(api, "RegGetKeySecurity", probe, f"{key} (size probe)")
    if size.value == 0:  # pragma: no cover - the API never answers zero here
        throw_win32(
            api, "RegGetKeySecurity", abi.ERROR_INSUFFICIENT_BUFFER, f"{key}: empty descriptor"
        )
    buffer = ctypes.create_string_buffer(size.value)
    read = int(api.advapi32.RegGetKeySecurity(
        ctypes.c_void_p(handle),
        abi.DACL_SECURITY_INFORMATION,
        ctypes.cast(buffer, ctypes.c_void_p),
        ctypes.byref(size),
    ))
    if read != abi.ERROR_SUCCESS:
        throw_win32(api, "RegGetKeySecurity", read, key)
    present = wintypes.BOOL()
    dacl_slot = alloc_ptr_slot()
    defaulted = wintypes.BOOL()
    if int(api.advapi32.GetSecurityDescriptorDacl(
        ctypes.cast(buffer, ctypes.c_void_p),
        ctypes.byref(present),
        ctypes.byref(dacl_slot),
        ctypes.byref(defaulted),
    )) == 0:
        throw_last_error(api, "GetSecurityDescriptorDacl", key)
    if not present.value:
        raise KeyDaclRefused(
            f"registry key {key} carries no DACL, which grants every principal full control; "
            "writing one here would narrow access this module did not grant and cannot restore"
        )
    dacl = decode_ptr(dacl_slot)
    if is_null_ptr(dacl):
        return None
    return KeyDacl(int(dacl), buffer)


def merge_entry(api: Win32Bindings, entry, old_acl: int | None, label: str) -> int:
    """Merge one ``EXPLICIT_ACCESS_W`` into a DACL and return the new ACL.

    The registry half of ``acl.py``'s ``merge_and_apply``: ``old_acl`` ``None``
    builds a DACL from scratch, and the result is the caller's to free.

    :param api: the binding table.
    :param entry: the packed entry to merge.
    :param old_acl: the current DACL pointer, or ``None``.
    :param label: the caller's name for error details.
    :returns: the merged ACL pointer.
    :raises Win32Error: when the merge failed.
    """
    new_acl_slot = alloc_ptr_slot()
    merged = int(api.advapi32.SetEntriesInAclW(
        1,
        ctypes.byref(entry),
        ctypes.c_void_p(old_acl or 0),
        ctypes.byref(new_acl_slot),
    ))
    if merged != abi.ERROR_SUCCESS:
        throw_win32(api, "SetEntriesInAclW", merged, label)
    new_acl = decode_ptr(new_acl_slot)
    if is_null_ptr(new_acl):
        throw_win32(api, "SetEntriesInAclW", api.get_last_error(), f"{label}: null new ACL")
    return int(new_acl)


def apply_dacl(api: Win32Bindings, key: str, handle: int, new_acl: int) -> None:
    """Install ``new_acl`` as the key's DACL.

    ``RegSetKeySecurity`` takes a **self-relative** descriptor, while an ACL is
    installed on an *absolute* one — so the merged ACL is put into a fresh
    absolute descriptor, and ``MakeSelfRelativeSD`` produces the form the API
    wants. Building a fresh descriptor rather than editing the one read earlier is
    what keeps this call honest: the only field carried across is the DACL this
    module just merged, and the previous descriptor's own memory is never mutated.

    :param api: the binding table.
    :param key: the key's full spelling, for error messages.
    :param handle: the open key handle.
    :param new_acl: the merged ACL pointer.
    :raises Win32Error: when any call failed.
    """
    descriptor = ctypes.create_string_buffer(abi.SECURITY_DESCRIPTOR_MIN_LENGTH)
    absolute = ctypes.cast(descriptor, ctypes.c_void_p)
    if int(api.advapi32.InitializeSecurityDescriptor(
        absolute, abi.SECURITY_DESCRIPTOR_REVISION
    )) == 0:
        throw_last_error(api, "InitializeSecurityDescriptor", key)
    if int(api.advapi32.SetSecurityDescriptorDacl(
        absolute, True, ctypes.c_void_p(new_acl), False
    )) == 0:
        throw_last_error(api, "SetSecurityDescriptorDacl", key)

    # The measuring call fails on purpose: a NULL buffer is answered with
    # ERROR_INSUFFICIENT_BUFFER and the size the relative form needs.
    size = wintypes.DWORD(0)
    if int(api.advapi32.MakeSelfRelativeSD(absolute, None, ctypes.byref(size))) == 0:
        code = api.get_last_error()
        if code != abi.ERROR_INSUFFICIENT_BUFFER:
            throw_win32(api, "MakeSelfRelativeSD", code, f"{key} (size probe)")
    if size.value == 0:  # pragma: no cover - the API never answers zero here
        throw_win32(
            api, "MakeSelfRelativeSD", abi.ERROR_INSUFFICIENT_BUFFER, f"{key}: empty measurement"
        )
    relative = ctypes.create_string_buffer(size.value)
    if int(api.advapi32.MakeSelfRelativeSD(
        absolute, ctypes.cast(relative, ctypes.c_void_p), ctypes.byref(size)
    )) == 0:
        throw_last_error(api, "MakeSelfRelativeSD", key)

    status = int(api.advapi32.RegSetKeySecurity(
        ctypes.c_void_p(handle), abi.DACL_SECURITY_INFORMATION, ctypes.cast(relative, ctypes.c_void_p)
    ))
    if status != abi.ERROR_SUCCESS:
        throw_win32(api, "RegSetKeySecurity", status, key)


def _free_new_acl(api: Win32Bindings, new_acl: int, label: str) -> None:
    """Free the ACL ``SetEntriesInAclW`` allocated.

    :param api: the binding table.
    :param new_acl: the ACL pointer.
    :param label: the caller's name for error details.
    :raises Win32Error: when ``LocalFree`` reported a failure.
    """
    freed = local_free(api, new_acl)
    if not is_null_ptr(freed):
        throw_last_error(api, "LocalFree", f"{label} new ACL")


def grant_key_write(api: Win32Bindings, key: str, sid_ptr: int) -> None:
    """Grant :data:`~emrg.sandbox.win32.abi.REGISTRY_GRANT_MASK` to a capability SID on a key.

    Idempotent: a key whose DACL already carries the exact ACE (one surviving from
    an earlier provision) skips the write entirely — otherwise every provision
    re-propagates an identical inheritable ACE across the key's subkeys.
    Otherwise read-merge-write, so the key's pre-existing ACEs survive. Runs under
    the per-object lock, keyed by the key's own spelling.

    :param api: the binding table.
    :param key: the key whose DACL gains the grant.
    :param sid_ptr: the capability SID the ACE names.
    :raises RegistryKeyError: when the spelling is not one this module addresses.
    :raises KeyDaclRefused: when the key carries a NULL DACL.
    :raises Win32Error: when any call failed.
    """
    with with_path_lock(api, key):
        handle = open_key(api, key)
        try:
            # ``dacl`` is bound, not just read: its buffer owns the pointer that
            # ``has_exact_grant`` walks and ``merge_entry`` copies ACEs out of,
            # so letting it go out of scope here would free the memory under both.
            dacl = read_current_key_dacl(api, key, handle)
            old_acl = dacl.pointer if dacl is not None else None
            if old_acl is not None and has_exact_grant(
                api, old_acl, sid_ptr, mask=abi.REGISTRY_GRANT_MASK
            ):
                close_key(api, handle, key)
                return
            entry = build_explicit_access(sid_ptr, abi.GRANT_ACCESS, abi.REGISTRY_GRANT_MASK)
            new_acl = merge_entry(api, entry, old_acl, f"grantKeyWrite({key})")
        except BaseException:
            _close_key_best_effort(api, handle)
            raise
        try:
            apply_dacl(api, key, handle, new_acl)
        finally:
            _free_new_acl(api, new_acl, f"grantKeyWrite({key})")
        close_key(api, handle, key)


def revoke_key_write(api: Win32Bindings, key: str, sid_ptr: int) -> bool:
    """Remove every ACE naming a capability SID from a key's DACL.

    Other entries are preserved: the merge is a ``REVOKE_ACCESS`` of one trustee.

    :param api: the binding table.
    :param key: the key whose DACL loses the capability SID's ACEs.
    :param sid_ptr: the capability SID whose ACEs are removed.
    :returns: whether a removal was attempted (``False`` when the key carries no
        explicit DACL — the NULL-DACL case is refused, not treated as empty).
    :raises RegistryKeyError: when the spelling is not one this module addresses.
    :raises KeyDaclRefused: when the key carries a NULL DACL.
    :raises Win32Error: when any call failed.
    """
    with with_path_lock(api, key):
        handle = open_key(api, key)
        try:
            dacl = read_current_key_dacl(api, key, handle)
            if dacl is None:
                close_key(api, handle, key)
                return False
            entry = build_explicit_access(sid_ptr, abi.REVOKE_ACCESS, 0)
            new_acl = merge_entry(api, entry, dacl.pointer, f"revokeKeyWrite({key})")
        except BaseException:
            _close_key_best_effort(api, handle)
            raise
        try:
            apply_dacl(api, key, handle, new_acl)
        finally:
            _free_new_acl(api, new_acl, f"revokeKeyWrite({key})")
        close_key(api, handle, key)
        return True
