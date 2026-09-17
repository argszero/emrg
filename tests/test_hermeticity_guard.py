"""Hermeticity guard tests (2026-08-13 ~/.emrg/projects.yml leak).

Verifies the autouse conftest fixture is wired (both server modules
carry the guarded wrapper) and that the discriminator is reliable in
BOTH states (#455 lesson):
- tmp projects.yml writes pass through unchanged (negative state);
- the real ~/.emrg/projects.yml / tasks.yml paths raise AssertionError
  (positive state) — the raise happens before any write, so this test
  is safe.

Second family (issue #1337 part 2): the live-daemon signal guard. Same
both-states discipline — each route's test drives the real, un-isolated
kill path and asserts the guard fires, one control proves the guard fires
*before* the signal reaches the process, another proves the liveness probe
and an isolating test still work.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest


def test_guard_installed_in_both_modules():
    """Both scheduler and daemon carry the guarded atomic_write_yaml."""
    import emrg.server.daemon as daemon_mod
    import emrg.server.scheduler as sched_mod

    wrapped = sched_mod.atomic_write_yaml
    assert wrapped is daemon_mod.atomic_write_yaml
    assert wrapped.__name__ == "guarded"


def test_guard_allows_tmp_projects_yml(tmp_path):
    """Writes to a tmp projects.yml pass through to the original writer."""
    import emrg.server.scheduler as sched_mod

    target = tmp_path / "projects.yml"
    sched_mod.atomic_write_yaml(
        [{"name": "x", "path": "p", "last_active": "t"}],
        target,
        prefix=".projects_",
    )
    assert target.exists()
    data = target.read_text(encoding="utf-8")
    assert "name: x" in data


def test_guard_rejects_real_projects_yml():
    """Writing the real ~/.emrg/projects.yml is a hard error."""
    import emrg.server.scheduler as sched_mod

    real = (Path.home() / ".emrg" / "projects.yml").resolve()
    with pytest.raises(AssertionError, match="hermetic"):
        sched_mod.atomic_write_yaml([], real, prefix=".projects_")


def test_guard_rejects_real_tasks_yml():
    """Writing the real ~/.emrg/tasks.yml is a hard error (same class)."""
    import emrg.server.scheduler as sched_mod

    real = (Path.home() / ".emrg" / "tasks.yml").resolve()
    with pytest.raises(AssertionError, match="hermetic"):
        sched_mod.atomic_write_yaml([], real, prefix=".tasks_")


# ── live-daemon signal guard (issue #1337 part 2) ─────────────
#
# The red line is "a test never stops or restarts the daemon". In-process
# `_stop_all.stop_*()` calls are covered by conftest::_guard_stop_all_hermeticity;
# these two client-side routes were covered by prose only, and the prose lived in
# the very test files that had to obey it.
#
# Every test below is a POSITIVE control — it drives the real, un-isolated route
# and asserts the guard fires. That is safe precisely because the guard fires
# first; test_the_tripwire_refuses_before_the_real_kill proves that claim on a
# real child process rather than assuming it.

_PONG = json.dumps({
    "type": "pong",
    "identity": {"instance_id": "test-instance", "host_name": "test-host"},
    "started_at": "2026-08-03T00:00:00",
    "pid": 9999,
})


class _FakeWS:
    """Just enough websockets for the ping round-trip."""

    def __init__(self, frames):
        self._frames = list(frames)

    async def send(self, data):
        pass

    async def recv(self):
        return self._frames.pop(0)

    async def close(self):
        pass


def test_both_kill_routes_carry_the_tripwire():
    """The guard is installed on both modules — and is a shim, not a stub."""
    import emrg.__main__ as cli_mod
    import emrg.client.daemon_manager as dm_mod

    assert type(dm_mod.os).__name__ == "_SignalTripwire"
    assert type(cli_mod.os).__name__ == "_SignalTripwire"
    # everything else still reaches the real os module
    assert dm_mod.os.getpid() == os.getpid()
    assert cli_mod.os.path.join("a", "b") == os.path.join("a", "b")


def test_the_client_restart_route_cannot_signal_a_live_daemon(monkeypatch, tmp_path):
    """daemon_manager.check_and_restart_if_stale(): source looks newer than the
    running daemon → the real code reaches os.kill(pid, SIGTERM). Refused.

    Source mtime is the whole trigger: the config.toml branch was removed by
    #1355 ("a config.toml edit never restarts the daemon again"), so a
    config-mtime patch here would name an attribute that no longer exists."""
    import emrg.client.daemon_manager as dm_mod

    token_file = tmp_path / "emrgd.token"
    token_file.write_text("token\n")
    monkeypatch.setattr(dm_mod, "_get_server_source_mtime", lambda: 1e12)
    monkeypatch.setattr(dm_mod, "is_running", lambda: True)
    monkeypatch.setattr(dm_mod, "cleanup_server", lambda: None)
    monkeypatch.setattr(dm_mod, "get_server_path", lambda: str(token_file))

    async def _connect():
        return _FakeWS([_PONG])

    monkeypatch.setattr(dm_mod, "connect_to_server", _connect)

    with pytest.raises(AssertionError, match="red-line"):
        asyncio.run(dm_mod.check_and_restart_if_stale())


def test_the_cli_stop_route_cannot_signal_a_live_daemon(monkeypatch):
    """`emrg stop`'s SIGTERM fallback (emrg/__main__.py::_stop_daemon): the
    graceful shutdown is refused, so the code reaches the pid and the kill."""
    import emrg.__main__ as cli_mod

    async def _shutdown():
        return False

    async def _connect():
        return _FakeWS([_PONG])

    monkeypatch.setattr(cli_mod, "_send_shutdown", _shutdown)
    monkeypatch.setattr(cli_mod, "connect_to_server", _connect)

    with pytest.raises(AssertionError, match="red-line"):
        cli_mod._stop_daemon()


def test_the_tripwire_refuses_before_the_real_kill():
    """The evidence, not the assumption: a REAL child process named in a refused
    kill is still alive afterwards."""
    import emrg.client.daemon_manager as dm_mod

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(AssertionError, match="red-line"):
            dm_mod.os.kill(child.pid, signal.SIGTERM)
        assert child.poll() is None, "the guard raised but the child was signalled"
    finally:
        child.terminate()  # real os.kill — only the module's name is shimmed
        child.wait(timeout=10)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="signal 0 is signal.CTRL_C_EVENT on Windows — a console event to the "
           "whole process group, not a liveness probe (issue #1349)",
)
def test_the_liveness_probe_still_reaches_the_real_os():
    """Negative control: the guard refuses signals, not the probe. If it refused
    this too, it would pass by breaking the restart path it protects."""
    import emrg.client.daemon_manager as dm_mod

    assert dm_mod.os.kill(os.getpid(), 0) is None


def test_a_test_that_isolates_the_kill_still_runs(monkeypatch, tmp_path):
    """Negative control, the other direction: the documented escape hatch — a
    test that patches the module's own os.kill — still drives the full restart
    path (this is the shape tests/test_daemon_manager.py uses).

    Also the merge pin: this file was written against a tree that still had
    the config-mtime branch, and CI tests the PR *merged with master*, where
    #1355 removed `_get_config_mtime`. Nothing here names an attribute that
    master does not have."""
    import emrg.client.daemon_manager as dm_mod

    token_file = tmp_path / "emrgd.token"
    token_file.write_text("token\n")
    monkeypatch.setattr(dm_mod, "_get_server_source_mtime", lambda: 1e12)
    monkeypatch.setattr(dm_mod, "is_running", lambda: True)
    monkeypatch.setattr(dm_mod, "cleanup_server", lambda: None)
    monkeypatch.setattr(dm_mod, "get_server_path", lambda: str(token_file))

    async def _connect():
        return _FakeWS([_PONG])

    monkeypatch.setattr(dm_mod, "connect_to_server", _connect)

    calls = []

    def _fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError(pid)  # old daemon already gone

    monkeypatch.setattr(dm_mod.os, "kill", _fake_kill)

    asyncio.run(dm_mod.check_and_restart_if_stale())

    assert (9999, signal.SIGTERM) in calls
