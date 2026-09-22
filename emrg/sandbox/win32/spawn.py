"""Spawn a child under a restricted token, with kill-on-close Job semantics.

The blueprint reaches this through ``@deepseek-ai/dsh-win32-process``
(``process.ts``, 641 lines) because Node's child_process cannot carry a token.
Here the interpreter's own ``subprocess`` cannot either, but :mod:`ctypes` can
call the API the blueprint calls, so this module is the port of the *inherited
stdio* variant only: the confined child takes the runner's stdio verbatim
(``GetStdHandle`` handed to ``STARTUPINFOW``), which is exactly what the runner
needs — bytes flow straight through and the seam above sees one process.

The kill-on-close Job is kept: without it a killed runner leaves the confined
child running, so EMRG's timeout path would report a stopped command that is
still executing.  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` makes the child die
with its last job handle, which is the runner's.
"""

from __future__ import annotations

import ctypes
import subprocess

from emrg.sandbox.win32 import abi
from emrg.sandbox.win32.ffi import (
    JobExtendedLimitInformation,
    ProcessInformation,
    StartupInfoW,
    Win32Bindings,
    is_null_ptr,
    throw_last_error,
    throw_win32,
)

#: The runner's own failure exit, mirrored from the blueprint's
#: ``WINDOWS_ACL_RUNNER_FAILURE_EXIT``.
RUNNER_FAILURE_EXIT = 127


class RestrictedChild:
    """One confined child: its pid, and the exit code once it settles."""

    __slots__ = ("_api", "_process", "_job", "_thread", "pid")

    def __init__(self, api: Win32Bindings, process: int, job: int, thread: int, pid: int) -> None:
        self._api = api
        self._process = process
        self._job = job
        self._thread = thread
        self.pid = pid

    def close_thread(self) -> None:
        """Close the primary-thread handle after the resume.

        :raises Win32Error: when ``CloseHandle`` reported a failure.
        """
        if self._thread:
            closed = int(self._api.kernel32.CloseHandle(ctypes.c_void_p(self._thread)))
            self._thread = 0
            if closed == 0:
                throw_last_error(self._api, "CloseHandle", "child primary thread handle")

    def wait(self) -> int:
        """Wait for the child and return its exit code, full 32-bit width.

        :returns: the child's exit code.
        :raises Win32Error: when waiting or reading the code failed.
        """
        waited = int(self._api.kernel32.WaitForSingleObject(ctypes.c_void_p(self._process), abi.INFINITE))
        if waited != 0:  # WAIT_OBJECT_0
            throw_win32(self._api, "WaitForSingleObject", self._api.get_last_error(), f"child {self.pid}")
        code = ctypes.c_uint32(0)
        if int(self._api.kernel32.GetExitCodeProcess(ctypes.c_void_p(self._process), ctypes.byref(code))) == 0:
            throw_last_error(self._api, "GetExitCodeProcess", f"child {self.pid}")
        return int(code.value)

    def close(self) -> None:
        """Close the process and job handles (best-effort, never raised).

        Closing the job is what kills a child still running, so this is called
        on the failure path as well.
        """
        for handle in (self._process, self._thread, self._job):
            if handle:
                self._api.kernel32.CloseHandle(ctypes.c_void_p(handle))
        self._process = 0
        self._thread = 0
        self._job = 0


def quote_command_line(argv: list[str]) -> str:
    """Build the ``lpCommandLine`` string for one argv.

    ``list2cmdline`` is the interpreter's implementation of the same quoting
    rules ``CommandLineToArgvW`` parses — the rules the blueprint's spawn
    follows, so a command containing quotes and backslashes survives the trip.

    :param argv: the program plus its arguments.
    :returns: the command line.
    """
    return subprocess.list2cmdline(argv)


def _create_kill_on_close_job(api: Win32Bindings) -> int:
    """Create a Job that kills every member when its last handle closes.

    :param api: the binding table.
    :returns: the job handle.
    :raises Win32Error: when the job could not be created or configured.
    """
    job = int(api.kernel32.CreateJobObjectW(None, None) or 0)
    if is_null_ptr(job):
        throw_last_error(api, "CreateJobObjectW", "kill-on-close job")
    limits = JobExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = abi.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = int(api.kernel32.SetInformationJobObject(
        ctypes.c_void_p(job),
        abi.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ))
    if configured == 0:
        code = api.get_last_error()
        api.kernel32.CloseHandle(ctypes.c_void_p(job))
        throw_win32(api, "SetInformationJobObject", code, "kill-on-close job")
    return job


def spawn_restricted(
    api: Win32Bindings,
    token: int,
    argv: list[str],
    *,
    cwd: str | None = None,
) -> RestrictedChild:
    """Spawn ``argv`` under ``token`` with this process's stdio inherited.

    The child is created suspended, assigned to the kill-on-close Job, and then
    resumed — the order the blueprint uses, so a child cannot outrun the job
    assignment.

    :param api: the binding table.
    :param token: the restricted primary token.
    :param argv: the program plus its arguments.
    :param cwd: the working directory; ``None`` keeps this process's.
    :returns: the running child.
    :raises Win32Error: when the spawn failed.  The child never runs
        unrestricted on this path: there is no fallback.
    """
    startup = StartupInfoW()
    startup.cb = ctypes.sizeof(StartupInfoW)
    startup.dwFlags = abi.STARTF_USESTDHANDLES
    startup.hStdInput = api.kernel32.GetStdHandle(abi.STD_INPUT_HANDLE)
    startup.hStdOutput = api.kernel32.GetStdHandle(abi.STD_OUTPUT_HANDLE)
    startup.hStdError = api.kernel32.GetStdHandle(abi.STD_ERROR_HANDLE)
    information = ProcessInformation()
    command_line = ctypes.create_unicode_buffer(quote_command_line(argv))
    created = int(api.kernel32.CreateProcessAsUserW(
        ctypes.c_void_p(token),
        None,
        command_line,
        None,
        None,
        True,
        abi.CREATE_UNICODE_ENVIRONMENT | abi.CREATE_SUSPENDED,
        None,
        cwd,
        ctypes.byref(startup),
        ctypes.byref(information),
    ))
    if created == 0:
        throw_last_error(api, "CreateProcessAsUserW", " ".join(argv))
    process = int(information.hProcess or 0)
    thread = int(information.hThread or 0)
    job = _create_kill_on_close_job(api)
    assigned = int(api.kernel32.AssignProcessToJobObject(
        ctypes.c_void_p(job), ctypes.c_void_p(process)
    ))
    if assigned == 0:
        code = api.get_last_error()
        api.kernel32.CloseHandle(ctypes.c_void_p(process))
        api.kernel32.CloseHandle(ctypes.c_void_p(thread))
        api.kernel32.CloseHandle(ctypes.c_void_p(job))
        throw_win32(api, "AssignProcessToJobObject", code, f"child of {argv[0]}")
    resumed = int(api.kernel32.ResumeThread(ctypes.c_void_p(thread)))
    if resumed == 0xFFFFFFFF:  # (DWORD)-1
        code = api.get_last_error()
        api.kernel32.CloseHandle(ctypes.c_void_p(process))
        api.kernel32.CloseHandle(ctypes.c_void_p(thread))
        api.kernel32.CloseHandle(ctypes.c_void_p(job))
        throw_win32(api, "ResumeThread", code, f"child of {argv[0]}")
    child = RestrictedChild(api, process, job, thread, int(information.dwProcessId))
    child.close_thread()
    return child


def exit_status_for_mirroring(code: int) -> int:
    """Normalize a child exit code for this interpreter's own exit.

    Windows mirrors the full 32-bit exit status, and the blueprint verified the
    round trip end to end (a child exiting with the NTSTATUS ``0xC0000005`` is
    read back as ``3221225477``).  ``sys.exit`` takes a signed ``int`` on
    Windows, so a code above ``0x7FFFFFFF`` is handed over as its two's
    complement — the same bits, which is all the parent observes.

    :param code: the child's exit code as a 32-bit unsigned value.
    :returns: the value to pass to ``sys.exit``.
    """
    if code > 0x7FFFFFFF:
        return code - 0x100000000
    return code
