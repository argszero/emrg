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
def _ensure_git_on_path(monkeypatch):
    """Make bare ``git`` subprocess calls work on hosts without PATH git.

    Three tests shell out to bare ``git`` (test_cmd_crlf.py ``git ls-files``,
    test_git_utils.py ``git rev-parse`` / ``git init``) while the product
    code resolves git through git_utils.resolve_git_gh() (install-info cache
    → bundled ~/.emrg/install → PATH). On packaged installs git is NOT on
    PATH, so those tests raise FileNotFoundError even though the daemon works
    (2026-08-24: 3/972 failures on a PATH-less host). When PATH has no git,
    prepend the directory of the same resolved git binary the product would
    use (same tier order, no cache write). No-op on dev/CI where git is on
    PATH.
    """
    import os
    import shutil

    if shutil.which("git"):
        return  # git already reachable (dev / CI) — nothing to do

    from emrg.server.git_utils import _cached_tool_path, _tool_in_install

    git = _cached_tool_path("git")
    if not (git and Path(git).exists()):
        git = _tool_in_install("git") or shutil.which("git")
    if not git:
        return  # no git anywhere — let the tests fail with their own error

    git_dir = str(Path(git).resolve().parent)
    monkeypatch.setenv("PATH", git_dir + os.pathsep + os.environ.get("PATH", ""))


@pytest.fixture(autouse=True)
def _guard_stop_all_hermeticity(monkeypatch, request):
    """⛔ Red line (host 2026-08-18T22:58, extended by rant 2026-08-25T10:42:47):
    tests must NEVER trigger a real stop step. stop_daemon() sends a shutdown
    over the websocket and kills the live emrgd (2026-08-20 16:18/16:25 real
    incidents; the daemon is EMRG's life core; rant 2026-08-20T16:32:30), and
    stop_gui()/stop_tui()/stop_bundled_git()/stop_lock_owners() kill the live
    GUI/TUI/processes on a Windows run (rant 2026-08-25T10:42:47:
    test_stop_all.py:770 only isolated stop_daemon, so the other four ran for
    real under the mocked Windows branch). Any test that calls stop_all()/
    stop_*() without isolating the killer now fails loudly with AssertionError
    instead of killing processes. Tests that DO isolate (monkeypatch.setattr(
    _stop_all, "stop_x", lambda: None)) patch after this fixture and override
    it as usual.

    Escape hatch: a test that calls a stop function directly while having
    already isolated its internals (e.g. stop_lock_owners with
    find_install_module_holders + _lock_owner_ps mocked, or a POSIX noop)
    opts out per function with @pytest.mark.allow_real_stop("stop_lock_owners").
    """
    import emrg._stop_all as stop_mod

    _STOP_FUNCS = (
        "stop_daemon", "stop_gui", "stop_tui", "stop_bundled_git",
        "stop_lock_owners",
    )

    allowed = set()
    marker = request.node.get_closest_marker("allow_real_stop")
    if marker is not None:
        allowed = set(marker.args)

    def _no_real_stop(name):
        def _raises(*args, **kwargs):
            raise AssertionError(
                f"test triggered a REAL {name}() — ⛔ red-line violation "
                f"(host 2026-08-18T22:58 / rant 2026-08-25T10:42:47); tests must "
                f"isolate it via monkeypatch.setattr(emrg._stop_all, {name!r}, "
                f"lambda: None)"
            )
        _raises.__name__ = f"_no_real_{name}"
        return _raises

    for name in _STOP_FUNCS:
        if name not in allowed:
            monkeypatch.setattr(stop_mod, name, _no_real_stop(name))


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
