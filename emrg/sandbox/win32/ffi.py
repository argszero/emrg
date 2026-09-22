"""The Win32 binding table the ACL/token backend calls through.

Ported from the blueprint's ``ffi.ts`` (``sandbox-windows-acl``) layered over
``win32-process/src/ffi.ts`` (dsh 0.1.6-alpha.2), with the blueprint's
``koffi`` marshalling replaced by :mod:`ctypes` — the one structural
difference in this port, and the reason the binding table is a plain object a
test can substitute: every function above this layer takes ``api`` as its
first parameter, exactly as the blueprint's do, so the ACL logic can be
exercised without a Windows host.

Two consequences of that substitution are deliberate and documented where
they occur:

* ``ctypes`` can pass the ``GetCurrentProcess()`` pseudo-handle straight to
  ``OpenProcessToken``, so the blueprint's ``OpenProcess``/``CloseHandle``
  round trip (which exists because ``koffi`` cannot address the pseudo-handle)
  is not reproduced;
* structures are declared as :class:`ctypes.Structure` subtypes, so the
  offsets the blueprint hard-codes (``EXPLICIT_ACCESS_W`` 48 bytes,
  ``SID_AND_ATTRIBUTES`` 16-byte stride, ``TOKEN_GROUPS`` offset 8) are computed
  by the C ABI rules for this interpreter instead of being asserted by hand.
  The port therefore ASSUMES A 64-BIT INTERPRETER, the same assumption the
  blueprint's hard-coded offsets encode.

A third is not deliberate but has to be obeyed: ``ctypes`` memory belongs to a
Python object, where ``koffi``'s belongs to the caller.  Every pointer this
module hands upward therefore travels with its owner (:class:`NativeBuffer`),
because a bare address is one the interpreter may reuse — the Windows CI
mechanism test failed exactly that way, with every confined child dying
``STATUS_DLL_INIT_FAILED`` (``0xC0000142``) once the restricting SIDs had been
reused by the ``SID_AND_ATTRIBUTES`` array built from them.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

from emrg.sandbox.win32 import abi

#: ``FormatMessageW`` flags: read the message from the system table, and do not
#: process ``%n`` insertions (the placeholder text is fine as it stands).
_FORMAT_MESSAGE_FROM_SYSTEM = 0x00001000
_FORMAT_MESSAGE_IGNORE_INSERTS = 0x00000200

#: ``LocalAlloc`` flag: fixed memory.
_LPTR = 0x0000


class Win32Error(OSError):
    """One failed Win32 call, with the API name and the exact code.

    The blueprint shapes this error once (``win32-process/src/errors.ts``) and
    every call site reports through it: an ignored return value is how the
    original proof-of-concept silently ran children *unrestricted*, so no call
    in this package checks a return value without reporting the failure.
    """

    def __init__(self, api_name: str, code: int, detail: str, message: str) -> None:
        super().__init__(f"{api_name} failed (Win32 {code}) for {detail}: {message}")
        self.api_name = api_name
        self.code = code
        self.detail = detail


class SIDAndAttributes(ctypes.Structure):
    """``SID_AND_ATTRIBUTES`` — one token group entry."""

    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TrusteeW(ctypes.Structure):
    """``TRUSTEE_W`` — the trustee inside one ``EXPLICIT_ACCESS_W``."""

    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", ctypes.c_void_p),
    ]


class ExplicitAccessW(ctypes.Structure):
    """``EXPLICIT_ACCESS_W`` — one ACE to merge into a DACL."""

    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", TrusteeW),
    ]


class ACLStructure(ctypes.Structure):
    """``ACL`` — the header, so the ACE walk starts at the right offset."""

    _fields_ = [
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", wintypes.WORD),
        ("AceCount", wintypes.WORD),
        ("Sbz2", wintypes.WORD),
    ]


class StartupInfoW(ctypes.Structure):
    """``STARTUPINFOW``."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class ProcessInformation(ctypes.Structure):
    """``PROCESS_INFORMATION``."""

    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class JobBasicLimitInformation(ctypes.Structure):
    """``JOBOBJECT_BASIC_LIMIT_INFORMATION``."""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IoCounters(ctypes.Structure):
    """``IO_COUNTERS``."""

    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JobExtendedLimitInformation(ctypes.Structure):
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION``."""

    _fields_ = [
        ("BasicLimitInformation", JobBasicLimitInformation),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class Overlapped(ctypes.Structure):
    """``OVERLAPPED`` — left zeroed for a synchronous offset-0 lock."""

    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class Win32Bindings:
    """The API surface this package calls, with every prototype pinned.

    Prototypes are declared rather than left to ``ctypes``'s guesses: an
    undeclared return value is an ``int``, which truncates a 64-bit handle or
    pointer on this platform — the same class of silent failure the blueprint's
    ``Pointer`` types exist to prevent.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":  # pragma: no cover - platform guard
            raise RuntimeError("the Windows ACL backend can only be loaded on Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        self._declare()

    def _declare(self) -> None:
        k32, a32 = self.kernel32, self.advapi32
        c = ctypes

        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetCurrentProcess.argtypes = []

        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        k32.LocalAlloc.restype = c.c_void_p
        k32.LocalAlloc.argtypes = [wintypes.UINT, c.c_size_t]

        k32.LocalFree.restype = c.c_void_p
        k32.LocalFree.argtypes = [c.c_void_p]

        k32.GetLastError.restype = wintypes.DWORD
        k32.GetLastError.argtypes = []

        k32.FormatMessageW.restype = wintypes.DWORD
        k32.FormatMessageW.argtypes = [
            wintypes.DWORD, c.c_void_p, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPWSTR, wintypes.DWORD, c.c_void_p,
        ]

        k32.GetTempPathW.restype = wintypes.DWORD
        k32.GetTempPathW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]

        k32.SetEnvironmentVariableW.restype = wintypes.BOOL
        k32.SetEnvironmentVariableW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]

        k32.SetConsoleCtrlHandler.restype = wintypes.BOOL
        k32.SetConsoleCtrlHandler.argtypes = [c.c_void_p, wintypes.BOOL]

        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, c.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]

        k32.LockFileEx.restype = wintypes.BOOL
        k32.LockFileEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, c.POINTER(Overlapped),
        ]

        k32.UnlockFileEx.restype = wintypes.BOOL
        k32.UnlockFileEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            c.POINTER(Overlapped),
        ]

        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetStdHandle.argtypes = [wintypes.DWORD]

        k32.CreateProcessAsUserW.restype = wintypes.BOOL
        k32.CreateProcessAsUserW.argtypes = [
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, c.c_void_p,
            c.c_void_p, wintypes.BOOL, wintypes.DWORD, c.c_void_p,
            wintypes.LPCWSTR, c.POINTER(StartupInfoW), c.POINTER(ProcessInformation),
        ]

        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, c.POINTER(wintypes.DWORD)]

        k32.ResumeThread.restype = wintypes.DWORD
        k32.ResumeThread.argtypes = [wintypes.HANDLE]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [c.c_void_p, wintypes.LPCWSTR]

        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, c.c_int, c.c_void_p, wintypes.DWORD,
        ]

        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

        a32.OpenProcessToken.restype = wintypes.BOOL
        a32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, c.POINTER(wintypes.HANDLE)]

        a32.GetTokenInformation.restype = wintypes.BOOL
        a32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, c.c_int, c.c_void_p, wintypes.DWORD, c.POINTER(wintypes.DWORD),
        ]

        a32.SetTokenInformation.restype = wintypes.BOOL
        a32.SetTokenInformation.argtypes = [wintypes.HANDLE, c.c_int, c.c_void_p, wintypes.DWORD]

        a32.CreateRestrictedToken.restype = wintypes.BOOL
        a32.CreateRestrictedToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, c.c_void_p,
            wintypes.DWORD, c.c_void_p, wintypes.DWORD, c.c_void_p,
            c.POINTER(wintypes.HANDLE),
        ]

        a32.ConvertStringSidToSidW.restype = wintypes.BOOL
        a32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, c.POINTER(c.c_void_p)]

        a32.CreateWellKnownSid.restype = wintypes.BOOL
        a32.CreateWellKnownSid.argtypes = [
            c.c_int, c.c_void_p, c.c_void_p, c.POINTER(wintypes.DWORD),
        ]

        a32.IsValidSid.restype = wintypes.BOOL
        a32.IsValidSid.argtypes = [c.c_void_p]

        a32.GetLengthSid.restype = wintypes.DWORD
        a32.GetLengthSid.argtypes = [c.c_void_p]

        a32.CopySid.restype = wintypes.BOOL
        a32.CopySid.argtypes = [wintypes.DWORD, c.c_void_p, c.c_void_p]

        a32.EqualSid.restype = wintypes.BOOL
        a32.EqualSid.argtypes = [c.c_void_p, c.c_void_p]

        a32.SetEntriesInAclW.restype = wintypes.DWORD
        a32.SetEntriesInAclW.argtypes = [
            wintypes.ULONG, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p),
        ]

        a32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        a32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR, c.c_int, wintypes.DWORD, c.c_void_p, c.c_void_p,
            c.c_void_p, c.c_void_p,
        ]

        a32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        a32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR, c.c_int, wintypes.DWORD,
            c.POINTER(c.c_void_p), c.POINTER(c.c_void_p), c.POINTER(c.c_void_p),
            c.POINTER(c.c_void_p), c.POINTER(c.c_void_p),
        ]

    # -- the primitive helpers every call site shares ------------------------

    def get_last_error(self) -> int:
        """The error code of the most recent failed call on this thread.

        :returns: the Win32 error code.
        """
        return int(self.kernel32.GetLastError())

    def message_for(self, code: int) -> str:
        """Format one Win32 code with the system's own text.

        :param code: the Win32 error code.
        :returns: the system message, trimmed, or a placeholder when the system
            has no text for it.
        """
        buffer = ctypes.create_unicode_buffer(512)
        written = self.kernel32.FormatMessageW(
            _FORMAT_MESSAGE_FROM_SYSTEM | _FORMAT_MESSAGE_IGNORE_INSERTS,
            None, code, 0, buffer, len(buffer), None,
        )
        text = buffer.value.strip() if written else ""
        return text or f"unknown Win32 error {code}"


def win32() -> Win32Bindings:
    """The process-wide binding table, built lazily on first use.

    Lazy because the table can only be built on Windows while every module that
    takes it as a parameter must import anywhere (the same reason the blueprint
    loads ``koffi`` through ``createLazyRequire``).

    :returns: the shared binding table.
    :raises RuntimeError: when this host is not Windows.
    """
    global _BINDINGS
    if _BINDINGS is None:
        _BINDINGS = Win32Bindings()
    return _BINDINGS


#: The process-wide binding table (see :func:`win32`).
_BINDINGS: Win32Bindings | None = None


def throw_last_error(api: Win32Bindings, api_name: str, detail: str) -> None:
    """Raise for a call that failed without returning a status code.

    :param api: the binding table (for the code and the message).
    :param api_name: the API whose call failed.
    :param detail: what the call was doing.
    :raises Win32Error: always.
    """
    code = api.get_last_error()
    raise Win32Error(api_name, code, detail, api.message_for(code))


def throw_win32(api: Win32Bindings, api_name: str, code: int, detail: str) -> None:
    """Raise for a call that returned its failure as a status code.

    :param api: the binding table (for the message).
    :param api_name: the API whose call failed.
    :param code: the Win32 error code the call returned.
    :param detail: what the call was doing.
    :raises Win32Error: always.
    """
    raise Win32Error(api_name, code, detail, api.message_for(code))


def is_null_ptr(pointer: int | None) -> bool:
    """Whether a pointer value is NULL.

    :param pointer: the pointer as an integer, or ``None``.
    :returns: true for ``None`` and for 0.
    """
    return pointer is None or pointer == 0


def is_invalid_handle(handle: int | None) -> bool:
    """Whether ``CreateFileW`` produced ``INVALID_HANDLE_VALUE``.

    :param handle: the handle ``CreateFileW`` returned.
    :returns: true for null, zero, and the all-bits-one sentinel.
    """
    if is_null_ptr(handle):
        return True
    return handle in (-1, 0xFFFFFFFFFFFFFFFF)


def alloc_ptr_slot() -> ctypes.c_void_p:
    """Allocate one pointer-sized slot for an out-parameter.

    :returns: the slot, zeroed.
    """
    return ctypes.c_void_p(0)


def alloc_uint32(value: int = 0) -> wintypes.DWORD:
    """Allocate one 32-bit out-parameter slot.

    :param value: the initial value.
    :returns: the slot.
    """
    return wintypes.DWORD(value)


def alloc_bytes(size: int) -> ctypes.Array:
    """Allocate a zeroed byte buffer.

    The memory belongs to the interpreter, not to Win32: it must never be passed
    to ``LocalFree``, and a pointer to it is only valid while something still
    references the buffer (see :class:`NativeBuffer`).

    :param size: the buffer size in bytes.
    :returns: the buffer.
    """
    return (ctypes.c_ubyte * size)()


@dataclass(frozen=True)
class NativeBuffer:
    """A pointer into memory this process owns, together with the owner.

    The blueprint's ``allocBytes`` is a *native* allocation (a koffi block), so
    its pointer is stable for as long as the caller holds it.  ``ctypes`` memory
    is a Python object's: a function that returns only ``addressof(buffer)``
    leaves nothing referencing the bytes, and the interpreter is free to hand
    that memory to the next allocation of a similar size — which for a SID is
    precisely what happens, because ``SID_AND_ATTRIBUTES`` entries are allocated
    between the call that makes a SID and the call that reads it.

    So the address and its owner travel together, and the owner is what a caller
    keeps.  Releasing it is dropping the reference, never ``LocalFree``: the two
    allocators are different, and freeing memory this process does not own is
    heap corruption rather than a leak.
    """

    address: int
    buffer: ctypes.Array


def decode_ptr(slot: ctypes.c_void_p) -> int:
    """Read the pointer an out-parameter slot received.

    :param slot: the slot passed to the call.
    :returns: the pointer as an integer (0 for NULL).
    """
    return int(slot.value or 0)


def decode_uint32(slot: wintypes.DWORD) -> int:
    """Read the 32-bit value an out-parameter slot received.

    :param slot: the slot passed to the call.
    :returns: the value.
    """
    return int(slot.value)


def local_alloc(api: Win32Bindings, size: int) -> int:
    """Allocate ``size`` bytes of local memory.

    :param api: the binding table.
    :param size: the byte count.
    :returns: the pointer.
    :raises Win32Error: when the allocation failed.
    """
    pointer = decode_ptr(ctypes.c_void_p(api.kernel32.LocalAlloc(_LPTR, size)))
    if is_null_ptr(pointer):
        throw_last_error(api, "LocalAlloc", f"{size} byte(s)")
    return pointer


def local_free(api: Win32Bindings, pointer: int) -> int:
    """Free a local allocation and return whatever ``LocalFree`` returned.

    :param api: the binding table.
    :param pointer: the allocation.
    :returns: ``LocalFree``'s return value (0 on success).
    """
    return decode_ptr(ctypes.c_void_p(api.kernel32.LocalFree(ctypes.c_void_p(pointer))))


def get_temp_path(api: Win32Bindings) -> str:
    """The host's temp directory, as ``GetTempPathW`` reports it.

    Deliberately *not* Python's ``tempfile.gettempdir()``: the blueprint keys
    the per-path lock files off this exact API, so a lock's home does not move
    with an environment variable a caller could set.

    :param api: the binding table.
    :returns: the temp path, with a trailing separator.
    :raises Win32Error: when the call failed.
    """
    buffer = ctypes.create_unicode_buffer(abi.MAX_PATH)
    written = int(api.kernel32.GetTempPathW(len(buffer), buffer))
    if written == 0:
        throw_last_error(api, "GetTempPathW", "temp path")
    return buffer.value


def set_environment_variable(api: Win32Bindings, name: str, value: str) -> None:
    """Set one variable in this process's environment.

    :param api: the binding table.
    :param name: the variable name.
    :param value: the new value.
    :raises Win32Error: when the call failed.
    """
    if int(api.kernel32.SetEnvironmentVariableW(name, value)) == 0:
        throw_last_error(api, "SetEnvironmentVariableW", name)


def sids_equal(api: Win32Bindings, first: int, second: int) -> bool:
    """Whether two SID pointers name the same SID.

    ``EqualSid`` is the kernel's own comparison — the blueprint reaches the
    same verdict through bounded offset reads because ``koffi`` cannot call it
    on an inline ACE SID; here the call is available and is used, with the
    bounded reader kept for the inline case only.

    :param api: the binding table.
    :param first: one SID pointer.
    :param second: the other SID pointer.
    :returns: true when both pointers name the same SID.
    """
    return int(api.advapi32.EqualSid(ctypes.c_void_p(first), ctypes.c_void_p(second))) != 0


def read_sid_bytes(api: Win32Bindings, sid_ptr: int) -> bytes:
    """Copy a SID's own bytes out of memory, using ``GetLengthSid`` to bound it.

    :param api: the binding table.
    :param sid_ptr: the SID pointer.
    :returns: the SID's bytes.
    :raises Win32Error: when the pointer is not a valid SID.
    """
    length = int(api.advapi32.GetLengthSid(ctypes.c_void_p(sid_ptr)))
    if length == 0:
        throw_last_error(api, "GetLengthSid", "SID")
    return ctypes.string_at(sid_ptr, length)
