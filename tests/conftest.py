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


# ── the daemon is EMRG's life core: no suite run may signal it ────────────────
#
# Issue #1337, item 2. `_guard_stop_all_hermeticity` above covers
# `emrg._stop_all`'s five stop functions — the *in-process* route. Two other
# routes reach a live daemon without passing through them, and neither was
# covered by anything:
#
#   * the client-side restart, `emrg.client.daemon_manager.
#     check_and_restart_if_stale()` — it SIGTERMs the pid the pong frame named
#     whenever the source looks newer than the daemon's start time;
#   * a child process — `python -m emrg server stop`, `emrg server restart`,
#     `pkill -f emrg.server`.
#
# The shape this exists for is measured, not hypothetical: on 2026-09-17 a
# full-suite run SIGTERMed the live daemon mid-run (`~/.emrg/emrgd-exit.log`:
# `reason sigterm exit_code 143`). It respawned, the scheduler re-sent the cycle
# task while the tree still held uncommitted work, and the dirty-tree guard
# pinned that cycle to read-only — the cost of one unguarded route.
#
# Both halves refuse *before* anything is signalled or spawned, and that is what
# makes the positive controls in tests/test_hermeticity_guard.py safe to write:
# they call these paths and never cause the event they forbid.

_RED_LINE = "⛔ red-line violation (host 2026-08-18T22:58, issue #1337)"


def _kill_is_a_liveness_probe(sig, platform: str = "") -> bool:
    """Is `os.kill(pid, sig)` on this platform a pure existence check?

    POSIX `kill(pid, 0)` is: it delivers nothing and reports ESRCH / EPERM.
    **Windows has no such call.** `signal.CTRL_C_EVENT` is **0**, and CPython's
    `os_kill_impl` routes `CTRL_C_EVENT` / `CTRL_BREAK_EVENT` to
    `GenerateConsoleCtrlEvent(pid, sig)` before it ever reaches
    `TerminateProcess` — so there `kill(pid, 0)` is a **Ctrl+C to that process
    group**, delivered to every process sharing the console, pytest included.

    This is why the guard below cannot delegate `sig == 0` unconditionally: on
    Windows that single value *is* a signal, and signalling the daemon is the one
    act the red line forbids. `emrg/client/daemon_manager.py`'s restart path
    skips its probe on win32 for the same reason, so refusing it there costs the
    suite nothing.

    The platform is a parameter rather than a `sys.platform` read so the decision
    can be pinned on any runner — a guard whose behaviour on Windows is only
    testable on Windows is a guard whose Windows behaviour is only discovered on
    Windows.
    """
    if not platform:
        import sys

        platform = sys.platform
    return sig == 0 and not platform.startswith("win")


class _NoSignalOs:
    """`os` as `emrg/client/daemon_manager.py` sees it: identical, minus signals.

    Scoped to that module's own namespace rather than patching `os.kill` itself,
    because `daemon_manager` is the only place in the client that signals a
    process and the only process it signals is the daemon. A global patch would
    also intercept `Popen.kill()` on a child a test spawned deliberately — a
    different act, and one the suite needs.

    `kill(pid, 0)` is delegated only where it is genuinely a probe — see
    `_kill_is_a_liveness_probe`: on POSIX it is the liveness check this module
    uses to wait for the old daemon to die (rant 2026-08-18T12:49:09 ②) and
    refusing it would replace a benign check with an error; on Windows the same
    value is `signal.CTRL_C_EVENT`, i.e. a delivered signal, so it is refused
    like any other.

    `is_probe` is injected so a test can drive both decisions on either platform
    without ever delegating to a real `os.kill`.
    """

    def __init__(self, real, is_probe=_kill_is_a_liveness_probe):
        self._real = real
        self._is_probe = is_probe

    def __getattr__(self, name):
        return getattr(self._real, name)

    def kill(self, pid, sig):
        if self._is_probe(sig):
            return self._real.kill(pid, sig)
        raise AssertionError(
            f"{_RED_LINE}: emrg.client.daemon_manager tried to signal pid {pid} "
            f"with signal {sig!r}. A test must never stop or restart the live "
            f"daemon — it is EMRG's life core. Isolate the restart path you are "
            f"testing instead, as tests/test_daemon_manager.py's restart tests do "
            f"(`@patch('emrg.client.daemon_manager.os.kill')`)."
        )


def _spawns_a_daemon_stop_or_restart(args) -> bool:
    """Would this `Popen` argv stop or restart the emrg daemon?

    Deliberately narrow, because the suite spawns git, node, pytest and the emrg
    CLI itself for read-only verbs: it refuses the emrg entry points (the
    installed `emrg`/`emrgd` script, or an interpreter on `-m emrg`) carrying a
    `stop`/`restart` verb, and a process-signalling command that names emrg.
    Everything else passes through untouched.
    """
    if isinstance(args, bytes):
        args = args.decode("utf-8", "replace")
    if isinstance(args, str):
        tokens = args.split()
    else:
        try:
            tokens = [str(a) for a in args]
        except TypeError:
            return False  # not an argv at all — let Popen raise its own error
    if not tokens:
        return False
    head = Path(tokens[0]).name
    runs_emrg = head in ("emrg", "emrgd")
    if "-m" in tokens:
        i = tokens.index("-m")
        if tokens[i + 1 : i + 2] == ["emrg"]:
            runs_emrg = True
    if runs_emrg:
        return any(token in ("stop", "restart") for token in tokens[1:])
    if head in ("pkill", "killall", "kill"):
        return "emrg" in " ".join(tokens).lower()
    return False


@pytest.fixture
def daemon_spawn_refusal():
    """The argv predicate the guard keys on, for a test that must classify a shape
    without spawning it.

    The shapes worth pinning as *allowed* include the live `emrg` CLI, and a test
    that proves allowance by running it is asserting two things at once: that the
    guard permitted the spawn, and that the CLI behaves on this platform. The
    second is `tests/test_cli_output_encoding.py`'s job, and it already runs this
    CLI here (`--help` under ascii and cp1252). Exposing the predicate lets this
    file pin the first, over more shapes than one invocation could cover.
    """
    return _spawns_a_daemon_stop_or_restart


@pytest.fixture
def daemon_kill_is_a_probe():
    """The guard's "is this call a probe?" decision, for the platform table.

    Exposed for the reason `daemon_spawn_refusal` is: Windows is the platform
    where the decision flips, and a test that could only observe it by running on
    Windows would leave the flip unmeasured on every other runner.
    """
    return _kill_is_a_liveness_probe


@pytest.fixture
def daemon_kill_refusal():
    """The guard's `os` substitute, constructible with a supplied decision.

    A test that wants to see the refusal for a Windows-shaped `kill(pid, 0)`
    cannot do it by calling the installed guard: on POSIX that reaches a real
    `os.kill`, and on Windows the whole point is that it must not reach one.
    Building the guard over a stand-in `os` lets both branches be pinned while
    nothing is signalled on any platform.
    """
    return _NoSignalOs


@pytest.fixture(autouse=True)
def _guard_no_live_daemon_is_signalled(monkeypatch):
    """⛔ No suite run may stop or restart a live daemon, by any route.

    The in-process route is covered by `_guard_stop_all_hermeticity`; this covers
    the other two — the client-side restart's `os.kill`, and a child process
    spawned from a test (issue #1337, item 2).

    Tests that really do exercise the restart path keep working: their own
    `@patch('emrg.client.daemon_manager.os.kill')` layers over this fixture and
    replaces the refusal with their mock, which is the visible, deliberate act
    the red line asks for.
    """
    import subprocess

    import emrg.client.daemon_manager as daemon_manager

    monkeypatch.setattr(daemon_manager, "os", _NoSignalOs(daemon_manager.os))

    real_popen = subprocess.Popen

    def _guarded_popen(args, *rest, **kwargs):
        if _spawns_a_daemon_stop_or_restart(args):
            raise AssertionError(
                f"{_RED_LINE}: a test tried to spawn {args!r}, which stops or "
                f"restarts the emrg daemon. A child process can kill the live "
                f"daemon just as a direct call can; test the CLI's behaviour by "
                f"calling it in-process with its stop path isolated."
            )
        return real_popen(args, *rest, **kwargs)

    _guarded_popen.__name__ = "Popen"
    monkeypatch.setattr(subprocess, "Popen", _guarded_popen)


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
