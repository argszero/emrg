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

import re
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

    `killpg` is refused alongside `kill`. `__getattr__` would otherwise delegate it
    to the real `os` — measured on the revision before this one,
    `guard.killpg(4242, 15)` reached the stand-in `os` — which made "signal the
    daemon's process group" the one signal route out of this wrapper. Nothing in
    the restart path signals a group today, so this is scope rather than a live
    hole; it is here because the act the red line forbids does not get narrower
    when the target is a group, and because the blast radius is *larger*: the
    daemon shares its group with whatever else the host started.

    ``holder`` is the module the shim is installed on. It is named in the refusal
    because two modules carry a kill that reaches the daemon and the message is
    the only thing that says which one fired — `emrg.client.daemon_manager`'s
    restart path and `emrg.__main__`'s own `emrg server stop` fallback. One class
    for both keeps one rule: a second wrapper would be a second place to forget
    something, and the two routes are the same act.
    """

    def __init__(self, real, is_probe=_kill_is_a_liveness_probe,
                 holder="emrg.client.daemon_manager"):
        self._real = real
        self._is_probe = is_probe
        self._holder = holder

    def __getattr__(self, name):
        return getattr(self._real, name)

    def kill(self, pid, sig):
        if self._is_probe(sig):
            return self._real.kill(pid, sig)
        raise AssertionError(
            f"{_RED_LINE}: {self._holder} tried to signal pid {pid} "
            f"with signal {sig!r}. A test must never stop or restart the live "
            f"daemon — it is EMRG's life core. Isolate the restart path you are "
            f"testing instead, as tests/test_daemon_manager.py's restart tests do "
            f"(`@patch('emrg.client.daemon_manager.os.kill')`)."
        )

    def killpg(self, pgid, sig):
        raise AssertionError(
            f"{_RED_LINE}: {self._holder} tried to signal the process "
            f"group {pgid} with signal {sig!r}. A test must never stop or restart "
            f"the live daemon — it is EMRG's life core. Isolate the restart path "
            f"you are testing instead, as tests/test_daemon_manager.py's restart "
            f"tests do (`@patch('emrg.client.daemon_manager.os.kill')`)."
        )


_EMRG_PROGRAMS = ("emrg", "emrgd")
_SHELLS = ("sh", "bash", "dash", "zsh", "ksh")
_SIGNALLERS = ("pkill", "killall", "kill")
_STOP_VERBS = ("stop", "restart")

#: Punctuation a shell may leave glued to a token: `(emrg`, `emrg)`, `'emrg'`,
#: `` `emrg` ``, `$(which` — see `_normalise_token`.
_SHELL_PUNCTUATION = "(){}[]$`'\"<>"


def _normalise_token(token: str) -> str:
    """The token as the *program* would receive it — glued shell punctuation stripped.

    A shell line arrives here already split on whitespace by `Popen`, and shell
    punctuation is not whitespace, so it stays glued to its neighbour:
    `sh -c "(emrg server restart)"` tokenises to `["(emrg", "server",
    "restart)"]` and `sh -c "$(which emrg) server stop"` to `["$(which",
    "emrg)", "server", "stop"]`. Read literally, neither carries an `emrg`
    basename or a bare `stop`/`restart` verb, so the first revision of this guard
    allowed **all** of them (measured, this PR's veto, cycle `cyc20260918-043412`): seven spellings of the
    one act the red line forbids, including the substitution form a test reaches
    for when it wants the installed script's path — and that form reaches the
    *live* daemon.

    None of them needs a shell parser. The punctuation is a fixed set, it is
    stripped from both ends, and what is left is what the program is handed; where
    a spelling is still ambiguous after this, the caller keeps its bias toward
    refusing (`_spawns_a_daemon_stop_or_restart`). Stripping is deliberately blind
    to *purpose*: `$(which emrg)` is not evaluated here, because evaluating it
    would mean running a command to answer a question about an argv.
    """
    return token.strip(_SHELL_PUNCTUATION)


def _emrg_entry_index(tokens) -> int:
    """Where in this argv the emrg program itself would be run, or -1.

    The program is not necessarily `tokens[0]`: a wrapper hands it over (`env emrg
    …`, `nohup emrg …`, `timeout 5 emrg …`, `uv run emrg …` — `uv run` is how this
    repo runs its own tools), and an interpreter names it with `-m emrg` /
    `-m emrg.server`. Reading only `tokens[0]` missed every one of those: measured
    on the revision before this one, `uv run emrg server stop`, `env emrg stop`,
    `nohup emrg stop`, `timeout 5 emrg server restart` and `nice -n 5 pkill -f
    emrg.server` all passed a guard whose own docstring claims it refuses "the emrg
    entry points … carrying a `stop`/`restart` verb".

    An interpreter's `-c` string is *not* read here: that would mean parsing Python,
    and a test hiding the act inside a language string is evading the guard rather
    than reaching the daemon by an ordinary route.

    Matching goes through `_normalise_token`, so a program the shell glued to its
    own punctuation (`$(which emrg)`, `(emrg`, `'emrg'`) is the same program here.

    The scan accepts **any** position whose basename is `emrg`/`emrgd`, not only
    command position, and that is a deliberate asymmetry rather than an oversight:
    the wider rule costs a false refusal whenever an `emrg`-named path is used as
    data (`git -C <this repo> log --grep restart` is refused — measured by this
    PR's veto, with this repo's own path), while narrowing it to `tokens[0]` plus
    the wrappers would allow an `emrg` program handed over by anything not on that
    list (`xargs -I{} emrg {} stop`). Of the two, only the second is the incident:
    a false refusal fails a test at its own assertion, naming the red line, and a
    false allowance SIGTERMs the live daemon mid-suite (#1337 item 2, one cycle
    lost to read-only). The verb test that follows is what keeps the common
    data-verb shapes allowed — `git commit -m stop`, `git log --grep emrg` — and
    they are pinned in `tests/test_hermeticity_guard.py`.
    """
    for i, token in enumerate(tokens):
        if Path(_normalise_token(token)).name in _EMRG_PROGRAMS:
            return i
        if token == "-m" and tokens[i + 1 : i + 2] in (["emrg"], ["emrg.server"]):
            return i + 1
    return -1


def _spawns_a_daemon_stop_or_restart(args) -> bool:
    """Would this `Popen` argv stop or restart the emrg daemon?

    Keyed on the **act**, not on the mention: the emrg program carrying a
    `stop`/`restart` verb, or a process-signalling command whose text names emrg.
    The verb must come *after* the program, so a verb passed as data (`git commit
    -m stop`) or a name in a pattern (`git log --grep emrg`) stays allowed — the
    suite spawns git, node, pytest and the read-only `emrg` verbs, and a guard that
    refused those would break them and teach the next reader to distrust it.

    Two spellings the first revision read as somebody else's argv, both of them
    this repo's own idiom, and both closed by looking past `tokens[0]`:

    * a **shell** runs the string it was handed, so `sh -c "emrg server stop"` is
      the same act as `emrg server stop` — the line inside is classified;
    * a **wrapper** hands the program over — `env emrg stop`, `nohup emrg stop`,
      `timeout 5 emrg server restart`, `uv run emrg server stop`, `nice -n 5 pkill
      -f emrg.server`.

    The bias is deliberate: a false refusal is loud, immediate and cheap (the test
    fails where it stands, with a message naming the red line), while a false
    allowance is the incident — a SIGTERM to the live daemon, which cost a cycle
    read-only on 2026-09-17 and is unrecoverable mid-run. Where the two spellings
    of the act are distinguishable only by shell parsing this cannot do, the guard
    refuses rather than guesses.

    **Every token match goes through `_normalise_token`** (this PR's veto, cycle
    `cyc20260918-043412`), which closed seven measured spellings that the
    token-literal reading allowed — all of them the same act, and one of them
    (`$(which emrg) server stop`) the form a test reaches for when it wants the
    installed script's path, i.e. a route to the *live* daemon:

    * `sh -c "(emrg server restart)"` — the program glued to a grouping paren;
    * `sh -c "$(which emrg) server stop"` — a command substitution's result;
    * ``sh -c "`emrg server stop`"`` — the older substitution spelling;
    * `sh -c "'emrg' server stop"` — a quoted program name;
    * `sh -c 'env -S "emrg server stop"'` — `env -S` handing over one string;
    * `[..., "emrg", "server", "(stop)"]` — the *verb* glued to punctuation, no
      shell involved at all.

    The rules they all sit on top of are unchanged, and the shapes that must stay
    allowed are pinned beside these in `tests/test_hermeticity_guard.py`: the verb
    must come *after* the program, so `git commit -m stop` and `emrg --help | grep
    stop` remain allowed. One qualification the veto measured, kept rather than
    papered over: the program scan accepts any position whose basename is
    `emrg`/`emrgd`, so an `emrg`-named path used as *data* makes the verb decisive
    — `git -C <this repo> log --grep restart` is refused. That direction is loud
    and cheap and its repair is a narrowing whose own hole (an `emrg` program
    handed over by a wrapper not on any list) is the incident; see
    `_emrg_entry_index`, and `test_an_emrg_named_path_costs_a_false_refusal` for
    the shape pinned as a known cost.
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

    if Path(_normalise_token(tokens[0])).name in _SHELLS:
        # `sh -c <line>` / `bash -lc <line>`: what runs is the line, so decide on
        # what the line would run — one *simple command* at a time, because a line
        # is a pipeline of them and only one of them may be the act. Splitting on
        # the separators is what keeps `sh -c "emrg --help | grep stop"` allowed
        # (the verb is a pattern there) while `sh -c "emrg server stop && echo ok"`
        # is refused. A newline separates two commands exactly as `;` does, so it
        # is a separator too. The split drops quotes and leaves the punctuation at
        # the segment edges, which is why both the token match below and the verb
        # test go through `_normalise_token`.
        return any(
            _spawns_a_daemon_stop_or_restart(segment.split())
            for segment in re.split(r"[;&|\n]+", " ".join(tokens[1:]))
        )

    for i, token in enumerate(tokens):
        if (
            Path(_normalise_token(token)).name in _SIGNALLERS
            and "emrg" in " ".join(tokens[i + 1 :]).lower()
        ):
            return True

    entry = _emrg_entry_index(tokens)
    if entry < 0:
        return False
    return any(_normalise_token(token) in _STOP_VERBS for token in tokens[entry + 1 :])


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
    the other three — the client-side restart's `os.kill`, the `emrg server stop`
    CLI's own SIGTERM fallback, and a child process spawned from a test (issue
    #1337, item 2).

    **Two modules, not one.** `emrg/__main__.py::_stop_daemon` SIGTERMs the pid it
    read from a `ping` frame, and it does *not* go through `emrg._stop_all`'s five
    stop functions — so `_guard_stop_all_hermeticity` above never sees it, and a
    test calling it in-process would signal the daemon the evolution is running
    on. Measured shape, not a hypothesis: both files that describe the stop path
    (`tests/test_cli_failure_reporting.py`, `tests/test_stop_all.py`) say in prose
    that they must never run it, and prose is not a guard. One shim class covers
    both so there is one rule to read and one place to change.

    Tests that really do exercise the restart path keep working: their own
    `@patch('emrg.client.daemon_manager.os.kill')` layers over this fixture and
    replaces the refusal with their mock, which is the visible, deliberate act
    the red line asks for.
    """
    import subprocess

    import emrg.__main__ as cli_mod
    import emrg.client.daemon_manager as daemon_manager

    for module, holder in (
        (daemon_manager, "emrg.client.daemon_manager"),
        (cli_mod, "emrg.__main__"),
    ):
        monkeypatch.setattr(module, "os", _NoSignalOs(module.os, holder=holder))

    real_popen = subprocess.Popen

    class _GuardedPopen(real_popen):
        """`Popen` with the refusal in front of it — and still a *type*.

        A plain function patched over `subprocess.Popen` answers
        `isinstance(x, subprocess.Popen)` with `TypeError: isinstance() arg 2 must
        be a type, a tuple of types, or a union`. The patch is autouse for the whole
        suite, so a plain function would make that question unanswerable everywhere
        — and the error it raises names `isinstance`, not the red line, so the next
        reader debugs the wrong thing. No test asks it today (grepped the tree);
        subclassing removes the class of failure instead of pinning it, since
        `isinstance`, `issubclass` and the inherited `__init__` all stay honest
        while the refusal is identical.
        """

        def __init__(self, args, *rest, **kwargs):
            if _spawns_a_daemon_stop_or_restart(args):
                raise AssertionError(
                    f"{_RED_LINE}: a test tried to spawn {args!r}, which stops or "
                    f"restarts the emrg daemon. A child process can kill the live "
                    f"daemon just as a direct call can; test the CLI's behaviour by "
                    f"calling it in-process with its stop path isolated."
                )
            super().__init__(args, *rest, **kwargs)

    _GuardedPopen.__name__ = "Popen"
    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)


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
