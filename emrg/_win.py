"""Windows windowless subprocess infrastructure.

Rant 2026-08-09T13:16:36 (v0.2.15 Windows regression, emergency): the daemon
is a non-interactive background process — every subprocess.Popen /
asyncio.create_subprocess_* without CREATE_NO_WINDOW pops a console window
on Windows. GUI/scheduler retry loops turned that into a cmd-window storm
(host observed hundreds of popups, had to reboot). All Python subprocess
call sites must splat the kwargs from :func:`win32_no_window_kwargs`; the
GUI side uses Node's ``windowsHide: true`` (already present in main.js /
daemon_client.js).

The function is a no-op on POSIX (empty dict) so call sites stay portable.
"""

from __future__ import annotations

import os
import subprocess

_IS_WINDOWS = os.name == "nt"

# CREATE_NO_WINDOW (0x08000000) is Windows-only — subprocess exposes it only
# on win32 builds. getattr keeps the module importable and the function
# callable on POSIX (e.g. tests that force the Windows branch on a POSIX
# runner); the literal is the documented Win32 constant.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def win32_no_window_kwargs() -> dict:
    """Kwargs that suppress console windows for subprocess children.

    Returns ``{"creationflags": subprocess.CREATE_NO_WINDOW}`` on Windows
    and ``{}`` elsewhere — safe to ``**``-splat into ``subprocess.run`` /
    ``subprocess.Popen`` and ``asyncio.create_subprocess_*`` on every
    platform.
    """
    if _IS_WINDOWS:
        return {"creationflags": _CREATE_NO_WINDOW}
    return {}
