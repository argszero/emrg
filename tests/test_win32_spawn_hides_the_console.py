"""The confined child is created with its window hidden.

Rant 2026-09-28T14:48:39 (project ``emrg``): on Windows, a scheduled task's cycle
flashed a console window once per confined command. The tool layer is not the
cause — every process it starts carries ``CREATE_NO_WINDOW`` (``emrg/_win.py``) —
the sandbox chain is: the runner is started without a console, and
:func:`~emrg.sandbox.win32.spawn.spawn_restricted` then asked Windows for a child
with ``STARTF_USESTDHANDLES`` alone, so Windows allocated the child a console and
showed it. ``STARTUPINFO.wShowWindow`` was never assigned.

These tests stub the Win32 calls, because neither this host nor CI has Windows:
what they pin is the struct this module hands to ``CreateProcessAsUserW``, which
is the whole of the defect and the whole of the fix.
"""

from __future__ import annotations

import ctypes

from emrg.sandbox.win32 import abi
from emrg.sandbox.win32.ffi import ProcessInformation, StartupInfoW
from emrg.sandbox.win32.spawn import quote_command_line, spawn_restricted

#: ``CreateProcess``'s own console-suppression flag, spelled here rather than
#: imported because this module must *not* use it: a restricted token's
#: ``CREATE_NO_WINDOW`` child dies with ``STATUS_DLL_INIT_FAILED`` (the boundary
#: notes in ``emrg/sandbox/win32/token.py`` and ``.../sandbox.py``), which is why
#: the blueprint reaches for ``wShowWindow`` instead. Importing the constant would
#: suggest it is available to the spawn site.
CREATE_NO_WINDOW = 0x08000000

#: A token handle stand-in; the fake never dereferences it.
TOKEN = 0x7A11


class _FakeKernel32:
    """The kernel32 entry points one spawn reaches, and what they captured."""

    def __init__(self) -> None:
        self.startup: StartupInfoW | None = None
        self.command_line: str | None = None
        self.create_flags: int | None = None

    def GetStdHandle(self, which: int) -> int:
        return 0x1000 + abs(int(which))

    def CreateProcessAsUserW(self, *args: object) -> int:
        # (token, app, command_line, pa, ta, inherit, flags, env, cwd, startup, info)
        self.command_line = ctypes.wstring_at(args[2])  # type: ignore[arg-type]
        self.create_flags = int(args[6])  # type: ignore[arg-type]
        self.startup = ctypes.cast(args[9], ctypes.POINTER(StartupInfoW)).contents  # type: ignore[arg-type]
        info = ctypes.cast(args[10], ctypes.POINTER(ProcessInformation))  # type: ignore[arg-type]
        info.contents.hProcess = 0x2000
        info.contents.hThread = 0x2001
        info.contents.dwProcessId = 4242
        return 1

    def CreateJobObjectW(self, *args: object) -> int:
        return 0x3000

    def SetInformationJobObject(self, *args: object) -> int:
        return 1

    def AssignProcessToJobObject(self, *args: object) -> int:
        return 1

    def ResumeThread(self, *args: object) -> int:
        return 1

    def CloseHandle(self, *args: object) -> int:
        return 1


class _FakeApi:
    """A binding table with the two attributes the spawn site touches."""

    def __init__(self) -> None:
        self.kernel32 = _FakeKernel32()

    def get_last_error(self) -> int:
        return 0


def _spawn(argv: list[str]) -> _FakeKernel32:
    """Run one spawn against the fake and hand back what it captured."""
    api = _FakeApi()
    child = spawn_restricted(api, TOKEN, argv)
    assert child.pid == 4242, "the fake did not reach the running child"
    assert api.kernel32.startup is not None, "CreateProcessAsUserW was never called"
    return api.kernel32


def test_the_confined_child_asks_for_a_hidden_window():
    """``dwFlags`` names ``wShowWindow`` and it says hide.

    Both halves are the fix: the flag without the field leaves Windows reading a
    ``wShowWindow`` of zero by accident, and the field without the flag is never
    read at all.
    """
    kernel32 = _spawn(["cmd.exe", "/c", "echo hi"])
    startup = kernel32.startup
    assert startup is not None
    assert startup.dwFlags & abi.STARTF_USESHOWWINDOW, (
        "the child was created without STARTF_USESHOWWINDOW, so Windows ignores "
        "wShowWindow and shows the console it allocates for a console-less parent"
    )
    assert startup.wShowWindow == abi.SW_HIDE


def test_the_std_handles_are_still_the_reason_the_struct_is_read():
    """The flag that was already there survived the edit.

    The confined child inherits this process's stdio through ``STARTUPINFOW``;
    dropping ``STARTF_USESTDHANDLES`` while adding the hiding flag would trade a
    flashing window for a runner with no output to read.
    """
    kernel32 = _spawn(["cmd.exe", "/c", "echo hi"])
    startup = kernel32.startup
    assert startup is not None
    assert startup.dwFlags & abi.STARTF_USESTDHANDLES
    assert startup.hStdInput and startup.hStdOutput and startup.hStdError


def test_the_childs_command_line_is_the_callers_argv_verbatim():
    """The window is hidden at the creation point, not by an argv flag.

    The blueprint removed an argv-level workaround in favour of this one, and an
    argv flag would only cover the shells that understand it. Pinning the command
    line to ``quote_command_line(argv)`` is what makes "no smuggled flag" a
    reading rather than a promise.
    """
    argv = ["powershell.exe", "-NoProfile", "-Command", "Get-ChildItem"]
    kernel32 = _spawn(argv)
    assert kernel32.command_line == quote_command_line(argv)
    assert "-WindowStyle" not in (kernel32.command_line or "")
    assert "Hidden" not in (kernel32.command_line or "")


def test_the_creation_flags_do_not_carry_create_no_window():
    """The alternative that looks right and kills the child is not used.

    ``CREATE_NO_WINDOW`` is the obvious spelling of "no console", and it is the
    one thing this path must not do: under the restricted token the child exits
    immediately with ``STATUS_DLL_INIT_FAILED``. Asserting its absence keeps the
    two mechanisms from being "simplified" into each other later.
    """
    kernel32 = _spawn(["cmd.exe", "/c", "echo hi"])
    assert kernel32.create_flags is not None
    assert not kernel32.create_flags & CREATE_NO_WINDOW
    assert kernel32.create_flags & abi.CREATE_SUSPENDED, (
        "the child must be created suspended so the Job assignment cannot be outrun"
    )
