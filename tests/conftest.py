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


@pytest.fixture(autouse=True)
def _guard_upgrade_hermeticity(monkeypatch, tmp_path):
    """⛔ Red line (host 2026-08-21T10:35:57): tests must NEVER trigger the
    real auto-upgrade chain — real GitHub releases request, real
    ~/.emrg/install/version.txt read/write, real emrg-upgrade session write.

    Empirical evidence: a long-running pytest session (PID 72994, 21h) really
    executed the upgrade tick every 5 minutes — real releases API requests,
    real install/version.txt reads, real emrg-upgrade session writes with the
    downgrade prompt (delay=1440, target=v0.2.57) — continuing across daemon
    restarts and even after `emrg stop` stopped all real processes (writes at
    10:23:12 / 10:28:15 / 10:33:17 after the 10:22:56 stop).

    This autouse fixture blocks every side-effect endpoint of the chain:
      1. httpx.AsyncClient in emrg.server.upgrade → AssertionError on
         instantiation (module-local: only the upgrade module's reference is
         replaced, the global httpx module is untouched). Tests that
         legitimately exercise tick() stub it per-test (e.g. test_upgrade.py's
         fake client) by patching after this fixture.
      2. upgrade.VERSION_FILE → per-test tmp path (the real
         ~/.emrg/install/version.txt must never be read or written).
      3. EmrgServer._get_or_create_session for SESSION_ID ("emrg-upgrade")
         → AssertionError (no real emrg-upgrade session may be created or
         written). Tests that exercise the session runner isolate the factory
         (monkeypatch.setattr(server, "_get_or_create_session", fake)) after
         this fixture, overriding it as usual.
    """
    import emrg.server.daemon as daemon_mod
    import emrg.server.upgrade as up_mod

    # 1. Network — any real GitHub releases request is a loud failure.
    class _BlockedHttpx:
        class AsyncClient:
            def __init__(self, *args, **kwargs):
                raise AssertionError(
                    "test triggered a REAL GitHub releases request through the "
                    "auto-upgrade chain — ⛔ red-line violation (host "
                    "2026-08-21T10:35:57); stub emrg.server.upgrade.httpx."
                    "AsyncClient in your test"
                )

    monkeypatch.setattr(up_mod, "httpx", _BlockedHttpx)

    # 2. Version file — never the real ~/.emrg/install/version.txt.
    monkeypatch.setattr(up_mod, "VERSION_FILE", tmp_path / "upgrade-version.txt")

    # 3. Upgrade session — creating/writing the real emrg-upgrade session is a
    #    loud failure; tests that exercise the runner stub the factory after.
    _orig_get_or_create = daemon_mod.EmrgServer._get_or_create_session

    def _guarded_get_or_create(self, session_id, cwd):
        if session_id == up_mod.SESSION_ID:
            raise AssertionError(
                "test attempted to create the REAL emrg-upgrade session — ⛔ "
                "red-line violation (host 2026-08-21T10:35:57); isolate the "
                "session factory (monkeypatch.setattr(server, "
                "'_get_or_create_session', lambda sid, cwd: <fake>))"
            )
        return _orig_get_or_create(self, session_id, cwd)

    monkeypatch.setattr(
        daemon_mod.EmrgServer, "_get_or_create_session", _guarded_get_or_create
    )
