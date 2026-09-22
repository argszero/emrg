"""Restricted-token construction for the Windows ACL backend.

Ported from the blueprint's ``token.ts`` (``sandbox-windows-acl``, dsh
0.1.6-alpha.2) — including its two empirically-found keep-alive rules, which
are the difference between a sandbox and a broken one:

* the logon session SID and Everyone stay in the restricting list under **both**
  modes: without them early DLL initialization dies with ``0xC0000142`` and CNG
  (the ``\\Device\\CNG`` write trustee) fails, which makes PowerShell crash with
  ``0xE0434352``;
* the write SIDs join **only** under ``workspace-write``, so a standing grant
  ACE from an earlier ``workspace-write`` period stays inert under ``read-only``
  (the pass-2 check grants only what the restricting list carries) while the
  unrevoked ACE keeps the re-upgrade free.

Fails closed: every failure throws before a token exists, and the caller's next
step is a refusal, never an unrestricted spawn.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from emrg.sandbox.win32 import abi
from emrg.sandbox.win32.acl import build_explicit_access
from emrg.sandbox.win32.ffi import (
    SIDAndAttributes,
    Win32Bindings,
    alloc_bytes,
    alloc_ptr_slot,
    alloc_uint32,
    decode_ptr,
    decode_uint32,
    is_null_ptr,
    local_free,
    throw_last_error,
    throw_win32,
)


class TokenGroups(ctypes.Structure):
    """``TOKEN_GROUPS`` — the header whose ``Groups`` offset the walk uses."""

    _fields_ = [("GroupCount", wintypes.DWORD), ("Groups", SIDAndAttributes * 1)]


class TokenDefaultDacl(ctypes.Structure):
    """``TOKEN_DEFAULT_DACL`` — a single ACL pointer."""

    _fields_ = [("DefaultDacl", ctypes.c_void_p)]


def open_current_process_token(api: Win32Bindings) -> int:
    """Open this process's token with the rights ``CreateRestrictedToken`` needs.

    The blueprint reaches the token through a real ``OpenProcess`` handle
    because ``koffi`` cannot address the ``GetCurrentProcess()`` pseudo-handle;
    ``ctypes`` can, so that round trip (and its two extra failure paths) is not
    reproduced here.  Everything else — the access mask, the null-handle check,
    the reported failure — matches.

    :param api: the binding table.
    :returns: the opened token handle.
    :raises Win32Error: when the token could not be opened.
    """
    process_handle = api.kernel32.GetCurrentProcess()
    token_slot = alloc_ptr_slot()
    opened = int(api.advapi32.OpenProcessToken(
        process_handle,
        abi.TOKEN_QUERY | abi.TOKEN_DUPLICATE | abi.TOKEN_ADJUST_DEFAULT | abi.TOKEN_ASSIGN_PRIMARY,
        ctypes.byref(token_slot),
    ))
    if opened == 0:
        throw_last_error(api, "OpenProcessToken", "the current process token")
    token = decode_ptr(token_slot)
    if is_null_ptr(token):
        throw_win32(api, "OpenProcessToken", api.get_last_error(), "null token handle")
    return token


def find_logon_sid(api: Win32Bindings, token: int) -> int:
    """Find and copy the token's logon session SID (``S-1-5-5-x-y``).

    The restricted token needs it for ``WinSta0``/desktop and other per-logon
    objects.

    :param api: the binding table.
    :param token: the token whose groups are scanned.
    :returns: a copied logon SID.
    :raises Win32Error: when the token information could not be read.
    :raises RuntimeError: when the token carries no logon SID.
    """
    needed_slot = alloc_uint32()
    api.advapi32.GetTokenInformation(
        ctypes.c_void_p(token), abi.TOKEN_GROUPS, None, 0, ctypes.byref(needed_slot)
    )  # expected to fail with ERROR_INSUFFICIENT_BUFFER
    needed = decode_uint32(needed_slot)
    if needed == 0:
        throw_last_error(api, "GetTokenInformation", "TokenGroups size query")
    groups_offset = TokenGroups.Groups.offset
    if needed < groups_offset:
        throw_win32(
            api, "GetTokenInformation", api.get_last_error(), f"implausible TokenGroups size {needed}"
        )
    buffer = alloc_bytes(needed)
    read = int(api.advapi32.GetTokenInformation(
        ctypes.c_void_p(token),
        abi.TOKEN_GROUPS,
        ctypes.byref(buffer),
        needed,
        ctypes.byref(needed_slot),
    ))
    if read == 0:
        throw_last_error(api, "GetTokenInformation", "TokenGroups")
    group_count = int(ctypes.c_uint32.from_address(ctypes.addressof(buffer)).value)
    stride = ctypes.sizeof(SIDAndAttributes)
    for index in range(group_count):
        entry_address = ctypes.addressof(buffer) + groups_offset + index * stride
        sid_ptr = decode_ptr(ctypes.c_void_p.from_address(entry_address))
        attributes = int(
            ctypes.c_uint32.from_address(entry_address + SIDAndAttributes.Attributes.offset).value
        )
        if is_null_ptr(sid_ptr) or attributes & abi.SE_GROUP_LOGON_ID != abi.SE_GROUP_LOGON_ID:
            continue
        sid_length = int(api.advapi32.GetLengthSid(ctypes.c_void_p(sid_ptr)))
        if sid_length == 0:
            throw_last_error(api, "GetLengthSid", f"logon SID group {index}")
        copy = alloc_bytes(sid_length)
        copied = int(api.advapi32.CopySid(
            sid_length, ctypes.byref(copy), ctypes.c_void_p(sid_ptr)
        ))
        if copied == 0:
            throw_last_error(api, "CopySid", f"logon SID group {index}")
        return ctypes.addressof(copy)
    raise RuntimeError(
        f"CreateRestrictedToken prerequisite failed: no logon SID found among {group_count} token groups"
    )


def make_well_known_sid(api: Win32Bindings, sid_type: int) -> int:
    """Create one well-known SID and assert its validity.

    :param api: the binding table.
    :param sid_type: the ``WELL_KNOWN_SID_TYPE`` value to create.
    :returns: the created SID pointer.
    :raises Win32Error: when the SID could not be created.
    """
    sid = alloc_bytes(abi.SECURITY_MAX_SID_SIZE)
    size_slot = wintypes.DWORD(abi.SECURITY_MAX_SID_SIZE)
    created = int(api.advapi32.CreateWellKnownSid(
        sid_type, None, ctypes.byref(sid), ctypes.byref(size_slot)
    ))
    if created == 0:
        throw_last_error(api, "CreateWellKnownSid", f"type {sid_type}")
    if int(api.advapi32.IsValidSid(ctypes.byref(sid))) == 0:
        throw_last_error(api, "IsValidSid", f"CreateWellKnownSid type {sid_type}")
    return ctypes.addressof(sid)


def set_token_default_dacl_grant(api: Win32Bindings, token: int, sid_ptr: int) -> None:
    """Merge one full-access allow ACE for ``sid_ptr`` into the token's DEFAULT DACL.

    The default DACL is the one every *new* object the token holder creates
    takes.  A restricted token inherits the user's default DACL verbatim, which
    names no restricting SID — so a new anonymous pipe (a child's stdio) fails
    the write pass-2 check at creation, and every piped-stdio grandchild spawn
    breaks.  The merged ACE names a **restricting** SID, so each new object's own
    DACL passes pass-2 while object *creation* stays gated by the parent
    container's DACL (files outside the granted trees remain uncreatable).

    :param api: the binding table.
    :param token: the restricted token to adjust (needs ``TOKEN_ADJUST_DEFAULT``).
    :param sid_ptr: the restricting SID whose full-access ACE joins the default DACL.
    :raises Win32Error: when any call failed.
    """
    needed_slot = alloc_uint32()
    api.advapi32.GetTokenInformation(
        ctypes.c_void_p(token), abi.TOKEN_DEFAULT_DACL, None, 0, ctypes.byref(needed_slot)
    )  # expected to fail with ERROR_INSUFFICIENT_BUFFER
    needed = decode_uint32(needed_slot)
    if needed == 0:
        throw_last_error(api, "GetTokenInformation", "TokenDefaultDacl size query")
    buffer = alloc_bytes(needed)
    read = int(api.advapi32.GetTokenInformation(
        ctypes.c_void_p(token),
        abi.TOKEN_DEFAULT_DACL,
        ctypes.byref(buffer),
        needed,
        ctypes.byref(needed_slot),
    ))
    if read == 0:
        throw_last_error(api, "GetTokenInformation", "TokenDefaultDacl")
    current_dacl = decode_ptr(ctypes.c_void_p.from_address(ctypes.addressof(buffer)))
    if is_null_ptr(current_dacl):
        raise RuntimeError("set_token_default_dacl_grant: the token carries no default DACL to extend")

    new_dacl_slot = alloc_ptr_slot()
    entry = build_explicit_access(sid_ptr, abi.GRANT_ACCESS, abi.FILE_ALL_ACCESS)
    result = int(api.advapi32.SetEntriesInAclW(
        1,
        ctypes.byref(entry),
        ctypes.c_void_p(current_dacl),
        ctypes.byref(new_dacl_slot),
    ))
    if result != abi.ERROR_SUCCESS:
        throw_win32(api, "SetEntriesInAclW", result, "default DACL merge")
    new_dacl = decode_ptr(new_dacl_slot)
    if is_null_ptr(new_dacl):
        throw_win32(api, "SetEntriesInAclW", result, "null merged default DACL")
    info = TokenDefaultDacl()
    info.DefaultDacl = ctypes.c_void_p(new_dacl)
    applied = int(api.advapi32.SetTokenInformation(
        ctypes.c_void_p(token),
        abi.TOKEN_DEFAULT_DACL,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ))
    if applied == 0:
        code = api.get_last_error()
        local_free(api, new_dacl)
        throw_win32(api, "SetTokenInformation", code, "TokenDefaultDacl")
    local_free(api, new_dacl)


def create_restricted_token(
    api: Win32Bindings,
    current_token: int,
    logon_sid: int,
    write_sids: list[int],
    world_sid: int,
    mode: str,
) -> int:
    """Create the ``WRITE_RESTRICTED`` token for one mode.

    ``read-only`` restricts to ``[logon SID, EVERYONE]``; ``workspace-write``
    restricts to ``[logon SID, EVERYONE, *write SIDs]``.  ``Authenticated
    Users`` is in neither list (the WMI namespace security check fails, so CIM
    is unavailable in every confined mode, and the ``C:\\``-root tree-creation
    escape closes in both), and ``INTERACTIVE``/``LOCAL`` are in neither (the
    host's Public tree grants write to ``INTERACTIVE``, so removing it closes
    that escape).  Console isolation is unavailable for the same reason the
    blueprint documents: ``CREATE_NO_WINDOW``/``CREATE_NEW_CONSOLE`` children
    die with ``STATUS_DLL_INIT_FAILED`` under the restriction.

    :param api: the binding table.
    :param current_token: the process token to restrict.
    :param logon_sid: the copied logon session SID.
    :param write_sids: the write SIDs forming the workspace and optional temp
        allowlists (``workspace-write`` only; empty under ``read-only``).
    :param world_sid: the Everyone SID.
    :param mode: selects the restricting list.
    :returns: the restricted token handle.
    :raises RuntimeError: when ``workspace-write`` names no write SID.
    :raises Win32Error: when the call failed.
    """
    if mode == "read-only":
        sids = [logon_sid, world_sid]
    else:
        if not write_sids:
            raise RuntimeError(
                "create_restricted_token: workspace-write restricting list requires at least one write SID"
            )
        sids = [logon_sid, world_sid, *write_sids]
    restricting = (SIDAndAttributes * len(sids))()
    for index, sid_ptr in enumerate(sids):
        restricting[index].Sid = ctypes.c_void_p(sid_ptr)
        restricting[index].Attributes = 0
    token_slot = alloc_ptr_slot()
    created = int(api.advapi32.CreateRestrictedToken(
        ctypes.c_void_p(current_token),
        abi.DISABLE_MAX_PRIVILEGE | abi.LUA_TOKEN | abi.WRITE_RESTRICTED,
        0,
        None,
        0,
        None,
        len(sids),
        ctypes.byref(restricting),
        ctypes.byref(token_slot),
    ))
    if created == 0:
        throw_last_error(api, "CreateRestrictedToken", f"restricting SIDs: {len(sids)}")
    token = decode_ptr(token_slot)
    if is_null_ptr(token):
        throw_win32(api, "CreateRestrictedToken", api.get_last_error(), "null token handle")
    return token
