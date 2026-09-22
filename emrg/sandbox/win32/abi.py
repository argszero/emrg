"""ACL/token-specific Win32 constants, name for name from the blueprint.

Ported from ``packages/sandbox/sandbox-windows-acl/src/win32-abi.ts`` in
deepseek-harness (blueprint commit ``ddefc45fbc``, dsh 0.1.6-alpha.2).  The
values are the Win32 SDK's own; the *names* are kept so a reader can diff this
file against the blueprint line by line (design §1.1 verbatim-copy list).
"""

from __future__ import annotations

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
#: Maximum SID allocation size in bytes.
SECURITY_MAX_SID_SIZE = 68

# --- process creation (Win32 process owner, win32-process/src/abi.ts) -------

#: STARTUPINFO.dwFlags: the std handles in the struct are meaningful.
STARTF_USESTDHANDLES = 0x00000100
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
