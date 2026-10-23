"""ACL/token-specific Win32 constants, name for name from the blueprint.

Ported from ``packages/sandbox/sandbox-windows-acl/src/win32-abi.ts`` in
deepseek-harness (blueprint commit ``ddefc45fbc``, dsh 0.1.6-alpha.2).  The
values are the Win32 SDK's own; the *names* are kept so a reader can diff this
file against the blueprint line by line (design §1.1 verbatim-copy list).
"""

from __future__ import annotations

import ctypes

#: OpenProcess access required to query the current process token.
PROCESS_QUERY_INFORMATION = 0x0400
#: Token right required by CreateProcessAsUserW.
TOKEN_ASSIGN_PRIMARY = 0x0001
#: Token right required by DuplicateTokenEx.
TOKEN_DUPLICATE = 0x0002
#: Token right required to read token information.
TOKEN_QUERY = 0x0008
#: Token right required to replace the token default DACL.
TOKEN_ADJUST_DEFAULT = 0x0080
#: Group attribute identifying the token logon SID.
SE_GROUP_LOGON_ID = 0xC0000000
#: Standard-rights portion excluded from the write capability grant.
STANDARD_RIGHTS_WRITE = 0x00020000
#: Generic file write access bits.
FILE_GENERIC_WRITE = 0x00120116
#: Delete or rename an object.
DELETE = 0x00010000
#: Delete or rename a directory child.
FILE_DELETE_CHILD = 0x0040
#: Capability-SID access mask granting write, delete and child deletion.
#:
#: WRITE_DAC and WRITE_OWNER stay excluded so a confined child cannot rewrite
#: DACLs or take ownership to escape the allowlist.
GRANT_MASK = (FILE_GENERIC_WRITE | DELETE | FILE_DELETE_CHILD) & ~STANDARD_RIGHTS_WRITE
#: Full access used in the restricted token default DACL.
FILE_ALL_ACCESS = 0x1F01FF
#: CreateRestrictedToken flag that disables maximum privileges.
DISABLE_MAX_PRIVILEGE = 0x1
#: CreateRestrictedToken limited-user flag.
LUA_TOKEN = 0x4
#: Restrict write access to the listed restricting SIDs.
WRITE_RESTRICTED = 0x8
#: WELL_KNOWN_SID_TYPE value for Everyone.
WIN_WORLD_SID = 1
#: TOKEN_INFORMATION_CLASS value for token groups.
TOKEN_GROUPS = 2
#: TOKEN_INFORMATION_CLASS value for the token default DACL.
TOKEN_DEFAULT_DACL = 6
#: SECURITY_INFORMATION flag selecting the DACL.
DACL_SECURITY_INFORMATION = 0x00000004
#: SE_OBJECT_TYPE value for filesystem objects.
SE_FILE_OBJECT = 1
#: TRUSTEE_TYPE value used when trustee classification is unknown.
TRUSTEE_IS_UNKNOWN = 0
#: TRUSTEE_FORM value indicating a SID pointer.
TRUSTEE_IS_SID = 0
#: Trustee record has no chained trustee.
NO_MULTIPLE_TRUSTEE = 0
#: EXPLICIT_ACCESS mode that grants access.
GRANT_ACCESS = 1
#: EXPLICIT_ACCESS mode that revokes access.
REVOKE_ACCESS = 4
#: ACE inheritance flags for child containers and objects.
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
#: Legacy Win32 maximum path character count used by GetTempPathW.
MAX_PATH = 260
#: Successful Win32 status code.
ERROR_SUCCESS = 0
#: Win32 error reported when an immediate byte-range lock cannot be obtained.
ERROR_LOCK_VIOLATION = 33
#: Win32 error reported when a buffer was too small to hold the answer.
ERROR_INSUFFICIENT_BUFFER = 122
#: Generic read access bit.
GENERIC_READ = 0x80000000
#: Generic write access bit.
GENERIC_WRITE = 0x40000000
#: CreateFile share-read flag.
FILE_SHARE_READ = 0x00000001
#: CreateFile share-write flag.
FILE_SHARE_WRITE = 0x00000002
#: CreateFile share-delete flag.
FILE_SHARE_DELETE = 0x00000004
#: CreateFile disposition that opens or creates the file.
OPEN_ALWAYS = 4
#: LockFileEx exclusive-lock flag.
LOCKFILE_EXCLUSIVE_LOCK = 0x2
#: LockFileEx immediate-failure flag.
LOCKFILE_FAIL_IMMEDIATELY = 0x1
#: ACE type for an allowed-access entry.
ACCESS_ALLOWED_ACE_TYPE = 0
#: Maximum SID sub-authority count.
SID_MAX_SUB_AUTHORITIES = 15
#: ACE flag marking inherited entries.
INHERITED_ACE = 0x10
class Sid(ctypes.Structure):
    """``SID`` — the security identifier, sized by its own declaration.

    Declared here, beside the constant that describes it, so that constant can be
    *measured*: a two-byte header, a six-byte identifier authority and the
    sub-authority array.  The struct holds no pointer, so its layout is the same
    on a 32-bit and a 64-bit interpreter — which is exactly why a literal ``68``
    reads as correct and stays correct by accident, with nothing saying so.

    The offsets the ACL walk reads are taken from here rather than written down:
    :attr:`SubAuthority` starts the variable-length tail an inline ACE SID is
    read from, and :attr:`SubAuthorityCount` is the byte that sizes it.
    """

    _fields_ = [
        ("Revision", ctypes.c_ubyte),
        ("SubAuthorityCount", ctypes.c_ubyte),
        ("IdentifierAuthority", ctypes.c_ubyte * 6),
        ("SubAuthority", ctypes.c_uint32 * SID_MAX_SUB_AUTHORITIES),
    ]


#: The most bytes a ``SID`` may occupy, and the buffer size a SID is copied into.
#:
#: The SDK defines this as ``sizeof(SID)`` with the sub-authority array at its
#: maximum, so it is taken from the struct above and never written down.  A
#: literal here would be a number a reader cannot check: the declaration is what
#: says how large a SID is, and it costs nothing to ask it (issue #2517).
SECURITY_MAX_SID_SIZE = ctypes.sizeof(Sid)

# --- registry keys (issue #2492 Stage 2) -----------------------------------

#: Predefined ``HKEY_CURRENT_USER`` handle.
HKEY_CURRENT_USER = 0x80000001
#: Predefined ``HKEY_LOCAL_MACHINE`` handle.
HKEY_LOCAL_MACHINE = 0x80000002
#: Standard right: read an object's DACL.
READ_CONTROL = 0x00020000
#: Standard right: replace an object's DACL.
WRITE_DAC = 0x00040000
#: Key access needed to read a key's DACL and rewrite it — and nothing else.
#:
#: ``RegOpenKeyExW`` wants the rights to be asked for up front, so the applier
#: asks for exactly the two the edit needs: with these, a key whose DACL the
#: caller may not rewrite fails at the open, before any merge is attempted.
KEY_SECURITY_ACCESS = READ_CONTROL | WRITE_DAC
#: Key right: read a key's values.
KEY_QUERY_VALUE = 0x0001
#: Key right: write a key's values.
KEY_SET_VALUE = 0x0002
#: Key right: create a subkey.
KEY_CREATE_SUB_KEY = 0x0004
#: Key right: enumerate a key's subkeys.
KEY_ENUMERATE_SUB_KEYS = 0x0008
#: Key right: receive change notifications for a key.
KEY_NOTIFY = 0x0010
#: Capability-SID access mask granting read, value writes and subkey creation.
#:
#: The registry's counterpart of :data:`GRANT_MASK`, and it excludes the standard
#: rights for the same reason: ``WRITE_DAC`` and ``WRITE_OWNER`` would let a
#: confined child rewrite the key's DACL or take its ownership and walk out of the
#: allowlist it was handed. ``READ_CONTROL`` is excluded with them — the mask
#: grants what a tool does to a key's *contents*, never what it does to the key's
#: own security descriptor.
REGISTRY_GRANT_MASK = (
    KEY_QUERY_VALUE | KEY_SET_VALUE | KEY_CREATE_SUB_KEY | KEY_ENUMERATE_SUB_KEYS | KEY_NOTIFY
)
#: ``SECURITY_DESCRIPTOR_REVISION``: the only revision ``InitializeSecurityDescriptor`` accepts.
SECURITY_DESCRIPTOR_REVISION = 1
class SecurityDescriptor(ctypes.Structure):
    """``SECURITY_DESCRIPTOR`` — the absolute form ``InitializeSecurityDescriptor`` builds.

    Declared here, beside the constant it sizes, so that constant can be
    *measured*: a four-byte header (``Revision``, ``Sbz1``, ``Control``) followed
    by four pointers is 20 bytes on a 32-bit interpreter and 40 on a 64-bit one,
    and the DACL pointer — the last field the API writes — starts at offset 32
    there.  A buffer sized by the 32-bit answer is overrun by half its length on a
    64-bit host, and because the allocation is a raw byte buffer rather than a
    Python object's storage, nothing raises: the surplus write lands in the
    process heap (issue #2509).
    """

    _fields_ = [
        ("Revision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("Control", ctypes.c_ushort),
        ("Owner", ctypes.c_void_p),
        ("Group", ctypes.c_void_p),
        ("Sacl", ctypes.c_void_p),
        ("Dacl", ctypes.c_void_p),
    ]


#: The bytes an absolute ``SECURITY_DESCRIPTOR`` occupies.
#:
#: Named by the SDK for exactly this use — the buffer an absolute descriptor is
#: built in before ``MakeSelfRelativeSD`` measures the form the API wants — and
#: the SDK defines it as ``sizeof(SECURITY_DESCRIPTOR)``, so it is taken from the
#: struct above and never written down.  A literal ``20`` here is the 32-bit
#: answer: correct on one interpreter width and a 20-byte overrun on the other.
SECURITY_DESCRIPTOR_MIN_LENGTH = ctypes.sizeof(SecurityDescriptor)

# --- process creation (Win32 process owner, win32-process/src/abi.ts) -------

#: STARTUPINFO.dwFlags: the std handles in the struct are meaningful.
STARTF_USESTDHANDLES = 0x00000100
#: STARTUPINFO.dwFlags: ``wShowWindow`` is meaningful for the new process.
#:
#: The blueprint carries this since its fix for the flashing console (see
#: :data:`SW_HIDE`); this port was taken before it and had only the std-handle
#: flag, which is why every confined command flashed a window (rant
#: 2026-09-28T14:48:39).
STARTF_USESHOWWINDOW = 0x00000001
#: ``STARTUPINFO.wShowWindow``: create the child's window hidden.
#:
#: The only console suppression that works under a restricted token — the child
#: is created hidden at the creation point, so it covers every process this
#: module starts. ``CREATE_NO_WINDOW`` is *not* an option here: the restricted
#: token's own boundary notes (``win32/token.py``, ``win32/sandbox.py``) record
#: that its ``CREATE_NO_WINDOW`` child dies with ``STATUS_DLL_INIT_FAILED``.
SW_HIDE = 0
#: CreateProcess flag: the environment block is Unicode.
CREATE_UNICODE_ENVIRONMENT = 0x00000400
#: CreateProcess flag: start the primary thread suspended.
CREATE_SUSPENDED = 0x00000004
#: Job-object limit: every process in the job dies when the last job handle closes.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
#: JOBOBJECTINFOCLASS value for the extended limit information.
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
#: ``GetStdHandle`` argument for the standard input handle.
STD_INPUT_HANDLE = -10
#: ``GetStdHandle`` argument for the standard output handle.
STD_OUTPUT_HANDLE = -11
#: ``GetStdHandle`` argument for the standard error handle.
STD_ERROR_HANDLE = -12
#: ``WaitForSingleObject`` timeout meaning "no timeout".
INFINITE = 0xFFFFFFFF
#: ``CreateFileW`` disposition that fails when the file already exists.
CREATE_NEW = 1
#: ``CreateFileW`` flags: no buffering, no inheritance.
FILE_ATTRIBUTE_NORMAL = 0x00000080
