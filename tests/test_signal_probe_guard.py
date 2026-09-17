"""Controls for the suite-wide real-probe refusal (issue #1351).

Every test here is safe on a Windows host: the refusal is decided before the real
call, and each guard under test wraps a **recorder** rather than the host's
``os.kill``. So the failure a broken guard would produce here is a missing
refusal, never a delivered Ctrl+C — a control whose safety depended on the code
it tests could not double as a mutation arm.
"""
from __future__ import annotations

import os
import signal
import sys

import pytest

from emrg import _stop_all
from tests.signal_probe_guard import REFUSAL, RefusingProbe, refuses_a_real_probe


class _Recorder:
    """A ``kill`` that signals nothing and remembers it was asked."""

    def __init__(self, answer=None):
        self.calls: list = []
        self._answer = answer

    def __call__(self, pid, sig, *args, **kwargs):
        self.calls.append((pid, sig))
        return self._answer


#: Signals that exist on **both** legs. `SIGKILL` does not exist on Windows, and
#: naming it in a decorator is a *collection* error there — measured on this
#: file's first CI round (run 35287611871): `AttributeError: module 'signal' has
#: no attribute 'SIGKILL'`, `collected 3064 items / 1 error`, the whole windows
#: leg dead before a single test ran. Built from what the platform has, so the
#: list can only grow where the name is real.
_REAL_SIGNALS = [signal.SIGTERM, signal.SIGINT]
if hasattr(signal, "SIGKILL"):
    _REAL_SIGNALS.append(signal.SIGKILL)


class TestTheDecision:
    """`refuses_a_real_probe` — both states, on any runner."""

    def test_windows_with_signal_zero_is_the_refused_pair(self):
        assert refuses_a_real_probe(1234, 0, "win32") is True

    def test_posix_with_signal_zero_is_not(self):
        """The pair the whole mechanism exists for: on POSIX signal 0 *is* a probe."""
        assert refuses_a_real_probe(1234, 0, "linux") is False

    def test_an_injected_reading_wins_over_the_host(self):
        """Injecting the reading is what makes the Windows answer pinnable here."""
        assert refuses_a_real_probe(1234, 0, "win32") is True
        assert refuses_a_real_probe(1234, 0, "linux") is False

    def test_the_host_reading_is_the_default(self):
        """No injected reading → `sys.platform`, the spelling `pid_alive` uses.

        Compared against an independently computed host reading, so this pins the
        default without patching `sys.platform` under a live pytest.
        """
        assert refuses_a_real_probe(1234, 0) is sys.platform.startswith("win")

    @pytest.mark.parametrize("sig", _REAL_SIGNALS)
    def test_a_real_signal_is_never_refused_not_even_on_windows(self, sig):
        """The guard is about the probe; stopping a process is the caller's job."""
        assert refuses_a_real_probe(1234, sig, "win32") is False

    def test_ctrl_c_event_is_the_zero_signal_where_it_exists(self):
        """The fact the whole guard rests on, pinned where the name is real.

        On Windows `signal.CTRL_C_EVENT` is 0 — which is why `sig == 0` is the
        refused pair there and why this row cannot exist on POSIX, where the name
        does not. The value is asserted rather than assumed: if CPython ever moved
        it, the refusal would need moving with it, and a bare `0` in `pid_alive`
        would stop being the probe it is.
        """
        if not hasattr(signal, "CTRL_C_EVENT"):
            pytest.skip("CTRL_C_EVENT is Windows-only — asserted on the windows leg")
        assert signal.CTRL_C_EVENT == 0
        assert refuses_a_real_probe(1234, signal.CTRL_C_EVENT, "win32") is True
        assert refuses_a_real_probe(1234, signal.CTRL_C_EVENT, "linux") is False


class TestTheWrapper:
    """`RefusingProbe` — refuses before delegating, otherwise transparent."""

    def test_it_refuses_before_the_real_call(self):
        real = _Recorder()
        with pytest.raises(AssertionError, match="issue #1351"):
            RefusingProbe(real, platform="win32")(1234, 0)
        assert real.calls == []

    def test_the_refusal_names_the_rule_and_the_remedy(self):
        with pytest.raises(AssertionError) as caught:
            RefusingProbe(_Recorder(), platform="win32")(1234, 0)
        assert str(caught.value) == REFUSAL
        assert "CTRL_C_EVENT" in REFUSAL and "pid_alive" in REFUSAL

    def test_a_probe_passes_through_where_a_probe_is_safe(self):
        real = _Recorder(answer=True)
        assert RefusingProbe(real, platform="linux")(1234, 0) is True
        assert real.calls == [(1234, 0)]

    def test_a_real_signal_passes_through_even_on_windows(self):
        real = _Recorder(answer=None)
        assert RefusingProbe(real, platform="win32")(1234, signal.SIGTERM) is None
        assert real.calls == [(1234, signal.SIGTERM)]

    def test_the_return_value_is_the_real_one(self):
        """Transparency: whatever the real call answers is what the caller sees."""
        real = _Recorder(answer=False)
        assert RefusingProbe(real, platform="linux")(4321, 0) is False

    def test_it_carries_the_host_reading_by_default(self):
        assert RefusingProbe(_Recorder()).host_platform == sys.platform

    def test_an_injected_decision_is_what_decides(self):
        """The seam the fixture relies on, so this class is pinnable without a host."""
        asked: list = []

        def _decide(pid, sig, platform):
            asked.append((pid, sig, platform))
            return True

        with pytest.raises(AssertionError):
            RefusingProbe(_Recorder(), platform="linux", decide=_decide)(7, 0)
        assert asked == [(7, 0, "linux")]


class TestTheGuardIsOnEveryTest:
    """The wiring — the fixture installed it, and a default-argument probe reaches it."""

    def test_os_kill_is_the_guard_here(self):
        assert isinstance(os.kill, RefusingProbe)
        assert os.kill.host_platform == sys.platform

    def test_a_default_argument_probe_reaches_the_guard(self, monkeypatch):
        """`pid_alive` resolves `(kill or os.kill)` at call time (`_stop_all.py:253`).

        So the guard is on the path of a probe made through a *default argument*,
        not only of a test's own `os.kill`. The recorder is the guard's `real`:
        under a guard that lost its decision this test fails for the missing
        refusal, and still signals nothing.
        """
        real = _Recorder()
        monkeypatch.setattr(os, "kill", RefusingProbe(real, platform="win32"))
        with pytest.raises(AssertionError, match="issue #1351"):
            _stop_all.pid_alive(os.getpid(), platform="linux")
        assert real.calls == []

    def test_the_same_call_passes_through_where_signal_zero_is_a_probe(self, monkeypatch):
        """The other state: a host where signal 0 is a probe must be unaffected.

        This is also what proves the probe is reached through `os.kill` rather
        than through a captured reference.
        """
        real = _Recorder()
        monkeypatch.setattr(os, "kill", RefusingProbe(real, platform="linux"))
        assert _stop_all.pid_alive(os.getpid(), platform="linux") is True
        assert real.calls == [(os.getpid(), 0)]
