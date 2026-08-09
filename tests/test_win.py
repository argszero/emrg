"""Tests for the Windows windowless-subprocess infrastructure.

Rant 2026-08-09T13:16:36 (v0.2.15 Windows regression, emergency): the daemon
spawned dozens of console windows on Windows because no subprocess call site
suppressed them (CREATE_NO_WINDOW). :func:`emrg._win.win32_no_window_kwargs`
is the single source of truth for the window-suppression kwargs — every
subprocess.Popen / asyncio.create_subprocess_* call site splats it.

These tests pin the API contract:
- POSIX (os.name != "nt"): empty dict (no-op, nothing changes)
- Windows: {"creationflags": subprocess.CREATE_NO_WINDOW}

The Windows branch is exercised by swapping the module's platform flag
(``_IS_WINDOWS`` is read at call time, so a direct attribute patch suffices —
``importlib.reload`` would re-run the top-level ``os.name == "nt"`` guard and
reset the patch).
"""

import subprocess
import sys

import pytest


@pytest.fixture
def win():
    import emrg._win as win

    orig = win._IS_WINDOWS
    yield win
    win._IS_WINDOWS = orig


def test_win32_no_window_kwargs_posix_noop(win):
    """On non-Windows the kwargs dict must be empty (zero behavior change)."""
    win._IS_WINDOWS = False
    assert win.win32_no_window_kwargs() == {}


def test_win32_no_window_kwargs_windows_create_no_window(win):
    """On Windows the kwargs must carry CREATE_NO_WINDOW."""
    win._IS_WINDOWS = True
    kwargs = win.win32_no_window_kwargs()
    assert kwargs == {"creationflags": 0x08000000}  # CREATE_NO_WINDOW (Win32 constant)
    assert kwargs["creationflags"] == 0x08000000


def test_win32_no_window_kwargs_splats_into_subprocess_run(win):
    """The dict must be splat-compatible with subprocess.run (no unknown keys)."""
    win._IS_WINDOWS = False
    # POSIX: plain run still works with the splat
    result = subprocess.run(
        [sys.executable, "-c", "print('ok')"],
        capture_output=True, text=True,
        **win.win32_no_window_kwargs(),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"
