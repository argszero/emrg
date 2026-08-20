"""Shared pytest fixtures — hermeticity guards.

2026-08-13 incident: the full test suite intermittently wrote pytest
temp paths into the real ~/.emrg/projects.yml. The mechanism is a
feedback loop — once a stale entry lands in the file (seeded by the
workspace self-heal family), subsequent tests re-read it and re-write
it, advancing the pytest-N counter on every full run. The daemon's
per-cycle repair (#734) eventually cleans it, but until then the host's
GUI project picker shows a dead path.

CI finding (PR #738): test_ws_e2e._boot_server only patched
daemon/connect config_dir, but EmrgServer.serve() builds a real
TaskScheduler whose load_and_start() → _ensure_self_evolution_task()
writes config_dir()/projects.yml AND tasks.yml via scheduler.py's own
(unpatched) config_dir → on a fresh runner this hits the real
~/.emrg/ files.

This autouse fixture makes any write to the REAL ~/.emrg/projects.yml
or ~/.emrg/tasks.yml a hard test failure: the offending test is named
immediately instead of the pollution being discovered later (precedent:
#583 assertPortFileInTmp sandbox guard for the emrgd.token file).
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REAL_CONFIG_FILES = (
    (Path.home() / ".emrg" / "projects.yml").resolve(),
    (Path.home() / ".emrg" / "tasks.yml").resolve(),
)


@pytest.fixture(autouse=True)
def _guard_real_config_files(monkeypatch):
    """Fail any test that writes the real ~/.emrg/projects.yml / tasks.yml."""
    import emrg.server.daemon as daemon_mod
    import emrg.server.scheduler as sched_mod

    orig = sched_mod.atomic_write_yaml
    assert orig is daemon_mod.atomic_write_yaml, "both modules must share atomic_write_yaml"

    def guarded(data, path, **kwargs):
        if Path(path).resolve() in _REAL_CONFIG_FILES:
            raise AssertionError(
                "test attempted to write a real ~/.emrg config file; "
                f"keep tests hermetic (target={path!r})"
            )
        return orig(data, path, **kwargs)

    monkeypatch.setattr(sched_mod, "atomic_write_yaml", guarded)
    monkeypatch.setattr(daemon_mod, "atomic_write_yaml", guarded)


@pytest.fixture(autouse=True)
def _redirect_sessions_index(monkeypatch, tmp_path):
    """Redirect the global session index to a per-test tmp file.

    Rant 2026-08-13T16:42:22 added a global ~/.emrg/sessions_index.json that
    Session._save_meta_with_title / Session.delete write to on every session
    create/append/delete. Without redirection, the whole session test suite
    would pollute the host's real index with pytest temp paths (same class as
    the projects.yml leak guarded above).
    """
    import emrg.sessions_index as sidx

    monkeypatch.setattr(
        sidx, "sessions_index_path",
        lambda: tmp_path / "sessions_index.json",
    )


@pytest.fixture(autouse=True)
def _guard_stop_daemon_hermeticity(monkeypatch):
    """⛔ Red line (host 2026-08-18T22:58): tests must NEVER trigger a real
    stop_daemon() — it sends a shutdown over the websocket and kills the live
    emrgd (2026-08-20 16:18/16:25 real incidents; the daemon is EMRG's life
    core; rant 2026-08-20T16:32:30). Any test that calls stop_all()/
    stop_daemon() without isolating stop_daemon now fails loudly with
    AssertionError instead of killing the daemon. Tests that DO isolate it
    (monkeypatch.setattr(_stop_all, "stop_daemon", lambda: None)) patch after
    this fixture and override it as usual.
    """
    import emrg._stop_all as stop_mod

    def _no_real_stop_daemon(*args, **kwargs):
        raise AssertionError(
            "test triggered a REAL stop_daemon() — ⛔ red-line violation "
            "(host 2026-08-18T22:58); tests must isolate it via "
            "monkeypatch.setattr(emrg._stop_all, 'stop_daemon', lambda: None)"
        )

    monkeypatch.setattr(stop_mod, "stop_daemon", _no_real_stop_daemon)
