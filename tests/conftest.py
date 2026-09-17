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

import os
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


class _SignalTripwire:
    """An ``os`` stand-in whose ``kill`` refuses a real signal.

    Everything else delegates to the real ``os`` module, so the module holding
    it keeps working normally. Signal 0 is *not* a kill — it delivers nothing
    and only asks whether the pid exists — so it passes through (on Windows 0
    is ``signal.CTRL_C_EVENT`` instead: a console event, which is exactly why
    no production path probes with it there — ``emrg._stop_all.pid_alive``,
    issue #1349).

    It replaces a module's ``os`` *name*, not ``os.kill`` itself: a global
    patch would also disarm ``subprocess.Popen.terminate()``, breaking every
    test that reaps a process it owns.
    """

    def __init__(self, holder: str) -> None:
        self._holder = holder

    def __getattr__(self, name):
        return getattr(os, name)

    def kill(self, pid, sig, *args, **kwargs):
        if sig == 0:
            return os.kill(pid, sig, *args, **kwargs)
        raise AssertionError(
            f"a test sent signal {sig!r} to pid {pid} through {self._holder} — "
            f"⛔ red-line violation (MANIFESTO 第四条附则二 / host 2026-08-18T22:58): "
            f"the daemon is EMRG's life core. Isolate the kill in the test that "
            f"needs it, the way tests/test_daemon_manager.py's restart tests do: "
            f'monkeypatch.setattr({self._holder}.os, "kill", lambda pid, sig: None)'
        )


@pytest.fixture(autouse=True)
def _guard_live_daemon_signals(monkeypatch):
    """⛔ No test may signal a pid through either client-side route that kills
    the live daemon (issue #1337 part 2, measured 2026-09-17).

    The red line -- tests must never stop/restart the daemon (host
    2026-08-18T22:58) -- was enforced in-process only for ``emrg._stop_all``'s
    five ``stop_*()`` functions (``_guard_stop_all_hermeticity`` above). Two
    client-side kill routes were outside its reach, and both were held by
    *prose* alone: ``daemon_manager.check_and_restart_if_stale()`` SIGTERMs a
    live daemon whose source/config looks newer (``emrg/client/daemon_manager.py``
    ``os.kill(server_pid, signal.SIGTERM)``), and the ``emrg stop`` CLI's
    SIGTERM fallback (``emrg/__main__.py::_stop_daemon``). Two test files
    document that they must never be executed ("Neither test runs the stop
    path ... exercising it would kill the daemon hosting the evolution") — and
    nothing made that true; a new test could call either one and end the
    daemon mid-suite. Issue #1337 records the consequence of exactly that: a
    suite run SIGTERMed a live daemon, the scheduler restarted this cycle
    against a still-dirty tree, and the dirty-tree guard pinned it read-only,
    losing the cycle's work.

    Now the two routes raise instead of killing. Tests that legitimately drive
    the restart logic patch the module's ``os.kill`` themselves *after* this
    fixture, which overrides it as usual (that is the escape hatch, and it is
    what tests/test_daemon_manager.py already does).

    Not covered, and not coverable from inside the process: a child process
    that runs the stopper (``python -m emrg stop``). Measured on 2026-09-18: no
    test spawns one, and the child's stop log is already pinned away from host
    state by ``_guard_stop_log_is_not_host_state`` below.
    """
    import emrg.__main__ as cli_mod
    import emrg.client.daemon_manager as dm_mod

    for module, name in ((dm_mod, "emrg.client.daemon_manager"), (cli_mod, "emrg")):
        monkeypatch.setattr(module, "os", _SignalTripwire(name))


@pytest.fixture(scope="session")
def _stop_log_scratch(tmp_path_factory):
    """One scratch directory for the whole session — the whole suite shares it,
    so this does not create a temp dir per test."""
    return tmp_path_factory.mktemp("stop-logs")


@pytest.fixture(autouse=True)
def _guard_stop_log_is_not_host_state(monkeypatch, _stop_log_scratch):
    """⛔ A suite run must not write into the host's ``~/.emrg/logs`` (issue
    #1337, measured 2026-09-17 on this workspace).

    The stop log is host state. ``tests/test_stop_all.py``'s
    ``test_stop_all_retries_lock_kill`` and
    ``test_stop_all_process_residual_still_aborts`` call ``stop_all()``
    in-process — legitimate, all five killers isolated — and ``stop_all()``
    opens its forensic log on the way through, so each run created a real
    ``stop_all-YYYYMMDD-HHMMSS.log`` in the host's logs directory: **1877**
    were counted, one per run, each carrying the Windows-shaped fixture fake
    (``C:/locked.pyd``, ``daemon (pid 1234)``) on a macOS host.

    Containing it here rather than in each test: the stopper resolves the
    directory through ``EMRG_STOP_LOG_DIR`` (``emrg/_stop_all.py::
    _stop_log_dir``), so one pin covers every route — in-process calls from any
    test file, and a child process that inherits the environment (``python -m
    emrg stop``). Tests that assert the *default* resolution clear the variable
    themselves (``monkeypatch.delenv``), which patches after this fixture and
    overrides it as usual.
    """
    monkeypatch.setenv("EMRG_STOP_LOG_DIR", str(_stop_log_scratch))


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
