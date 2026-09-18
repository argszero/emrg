"""The one place a real ``os.kill(pid, 0)`` is refused on Windows (issue #1351).

``signal.CTRL_C_EVENT`` is **0** on Windows, and CPython's ``os_kill_impl`` routes
that value to ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`` *before* the
``TerminateProcess`` fallback — so there the call is not a probe but **a Ctrl+C to
that pid's console process group**: every process sharing the console, the
runner's pytest included. Measured on PR #1350's first CI round (run
35258286311): ``test_a_live_pid_on_posix`` printed PASSED on windows-2025 and the
*next* test died with ``KeyboardInterrupt`` inside ``subprocess.py:1606``.

**Keyed on the act, not on a helper's name.** ``emrg._stop_all.pid_alive``
resolves ``(kill or os.kill)(pid, 0)`` at call time (``_stop_all.py:253``), so
replacing ``os.kill`` for the duration of a test reaches a probe made from *any*
module — a test's own call, a helper's, a default argument. The AST scan in
``tests/test_stop_all.py`` cannot: it reads its own source and matches
``pid_alive`` by name, so it guards that file and nothing else (issue #1351:
keep it, as the weaker and earlier of the two instruments — it pins that file's
inventory, this one enforces the rule).

The refusal is decided **before** the real call, so a refused probe signals
nothing and the controls in ``tests/test_signal_probe_guard.py`` stay safe to run
on a Windows host. What a ``platform=`` argument cannot do is change the host: it
selects a branch of the code under test, and the *host* reading is what decides
here (issue #1349's defect, one layer out).
"""
from __future__ import annotations

import sys

REFUSAL = (
    "refused a real os.kill(pid, 0) on Windows: signal.CTRL_C_EVENT is 0 there, "
    "so CPython routes it to GenerateConsoleCtrlEvent — a Ctrl+C to that pid's "
    "console process group, this runner's pytest included (issue #1351). Take "
    "the answer from emrg._stop_all.pid_alive(pid, platform=..., win_probe=..., "
    "kill=<recorder>) instead; a test that must reach the real call belongs "
    "behind tests/test_stop_all.py's _POSIX_ONLY, so Windows never runs it."
)


def refuses_a_real_probe(pid, sig, platform: str = "") -> bool:
    """Is ``os.kill(pid, sig)`` a delivered console event on this host?

    True for the one pair that is dangerous and only that pair: **Windows** and
    signal **0**. Any other signal on Windows, and every signal on POSIX, is the
    caller's business — this guard is about the probe, not about stopping a
    process.

    ``platform`` defaults to ``sys.platform`` with the same ``startswith('win')``
    spelling ``pid_alive`` uses. It is a parameter rather than a read so the
    Windows answer can be pinned on a POSIX runner — the device PR #1348 and
    PR #1350 both used, and the only way the unsafe branch gets exercised here.

    ``pid`` is part of the signature the fixture injects and is deliberately not
    consulted: the danger is in the *signal*, not in which process is named.
    """
    return (platform or sys.platform).startswith("win") and sig == 0


class RefusingProbe:
    """``os.kill`` minus the one call that is not a probe.

    Installed suite-wide by ``tests/conftest.py``'s autouse fixture, over the
    host's real ``os.kill``. The decision runs **before** the delegation, which is
    the property the issue asks for: a refusal cannot itself signal anything, so
    a control that provokes one is safe wherever the suite runs.
    """

    def __init__(self, real, *, platform: str = "", decide=refuses_a_real_probe):
        self._real = real
        self._decide = decide
        #: The host reading this guard carries (``sys.platform`` unless injected).
        self.host_platform = platform or sys.platform

    def __call__(self, pid, sig, *args, **kwargs):
        if self._decide(pid, sig, self.host_platform):
            raise AssertionError(REFUSAL)
        return self._real(pid, sig, *args, **kwargs)

    def __repr__(self) -> str:
        return f"<RefusingProbe platform={self.host_platform!r} of {self._real!r}>"
