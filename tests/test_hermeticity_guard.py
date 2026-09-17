"""Hermeticity guard tests (2026-08-13 ~/.emrg/projects.yml leak).

Verifies the autouse conftest fixture is wired (both server modules
carry the guarded wrapper) and that the discriminator is reliable in
BOTH states (#455 lesson):
- tmp projects.yml writes pass through unchanged (negative state);
- the real ~/.emrg/projects.yml / tasks.yml paths raise AssertionError
  (positive state) — the raise happens before any write, so this test
  is safe.
"""
from __future__ import annotations

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


# ── no suite run may stop or restart the daemon (issue #1337, item 2) ────────
#
# The two routes `_guard_stop_all_hermeticity` cannot see: the client-side
# restart's `os.kill`, and a child process spawned from a test. Each guard is
# pinned in BOTH directions — the #455 lesson, and the acceptance #1337 asks
# for: "a check that refuses everything would be as wrong as one that refuses
# nothing."
#
# Every positive control below calls the guarded path and is safe by
# construction: the refusal happens *before* any signal is sent or any process
# is spawned, so the forbidden act never occurs. The pid 4242 is never signalled
# and `emrg server stop` is never executed.


def test_guard_refuses_signalling_the_daemon():
    """Positive: the client-side restart's kill is refused, term and kill.

    The wrapper's identity is asserted first, in the same spirit as
    `test_guard_installed_in_both_modules` above: the behavioural controls below
    would also pass if someone patched the kill inside the test body, whereas a
    refusal that comes from the *installed* guard is what a real regression
    would meet.
    """
    import signal

    import emrg.client.daemon_manager as daemon_manager

    assert type(daemon_manager.os).__name__ == "_NoSignalOs"
    # SIGKILL is Unix-only; SIGTERM is the signal the restart path actually uses.
    signals = [signal.SIGTERM] + (
        [signal.SIGKILL] if hasattr(signal, "SIGKILL") else []
    )
    for sig in signals:
        with pytest.raises(AssertionError, match="red-line violation"):
            daemon_manager.os.kill(4242, sig)


def test_guard_still_allows_a_liveness_probe(daemon_kill_is_a_probe):
    """Negative: a genuine probe is not a signal — `kill(pid, 0)` must reach `os`.

    This is the arm that keeps the guard from being "refuse every os.kill":
    `daemon_manager` uses the probe to wait for the old daemon to die
    (rant 2026-08-18T12:49:09 ②), so refusing it would turn a benign check into
    an error.

    POSIX only, and that is a property of the *value* rather than a shortcut. On
    Windows `0` is `signal.CTRL_C_EVENT`, so the call this test exists to allow is
    the call the guard must refuse there — and it is not a call this file may make
    to find that out. Measured on the windows-2025 leg of run 35252423114: this
    test passed at 17:33:41.5 and the *next* test that spawned a child died at
    17:33:44.4 with `KeyboardInterrupt` at `threading.py:359` (`waiter.acquire()`
    inside `Condition.wait`, i.e. `Thread.start()` waiting on `_started`) — the
    Ctrl+C this line sent to its own console group, noticed at the next blocking
    call. So the Windows half is pinned where it can be, without sending it:
    `test_kill_zero_is_a_probe_on_posix_and_a_ctrl_c_on_windows` for the decision
    and `test_the_refusal_asks_that_decision_instead_of_assuming_it` for the wiring.
    """
    if not daemon_kill_is_a_probe(0):
        pytest.skip("kill(pid, 0) is CTRL_C_EVENT here — see the two tests named above")

    import os as real_os

    import emrg.client.daemon_manager as daemon_manager

    # Our own pid is alive, so the real probe returns normally.
    assert daemon_manager.os.kill(real_os.getpid(), 0) is None


def test_guard_refuses_spawning_the_stop_or_restart_cli(tmp_path):
    """Positive: a child that would stop/restart the daemon is refused.

    The guard raises before anything is spawned, so the refusal itself is
    observable without running a CLI. Both routes are covered because an instance
    need not use the installed script: `python -m emrg server restart` is the same
    act, and `pkill -f emrg.server` is the one a shell reaches for.

    Every argv names a program in a directory this test created. The guard reads
    the *shape* of the argv — the `emrg`/`emrgd` basename carrying a `stop` or
    `restart` verb, or a signalling command whose text names emrg — and a stub's
    basename is the only part of that shape which is load-bearing here. Pointed at
    the real PATH instead, this control would be the one place where a regression
    to "refuse nothing" *is* the incident it forbids (`killall emrgd` on a live
    host), i.e. a test whose safety depends on the code it is testing. With stubs,
    a regression still fails the test — the spawn raises nothing — and the host is
    untouched.

    The wrapper and shell rows are here because the first revision of this guard
    read only `tokens[0]` and let all of them through (measured): a shell runs the
    line it is handed, and `env`/`nohup`/`timeout`/`nice` hand the program over, so
    the *same* act arrives as somebody else's argv. Every program in those rows is
    a stub as well, the wrapper included — so the "a regression must still be
    harmless" property holds for them too.

    The last block is the veto of cycle `cyc20260918-043412`, which measured seven
    spellings that glued shell punctuation to the program or the verb and got
    through the token-literal reading: `(emrg`, `$(which emrg)`, `` `emrg ``, a
    quoted `'emrg'`, `env -S "…"`, and `(stop)` as an argv element with no shell
    involved. `$(which emrg) server stop` is the one that must never come back — it
    is how a test asks for the installed script's path, and it reaches the **live**
    daemon. Those rows carry a `PATH` pinned to the stubs, because the substitution
    is resolved by the shell *if* the guard ever lets it through: with the stub
    `which` on `PATH` printing nothing, a regression expands to an empty command
    instead of the host's real `emrg`.
    """
    import subprocess

    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name in (
        "emrg",
        "emrgd",
        "pkill",
        "killall",
        "kill",
        "python",
        "sh",
        "bash",
        "env",
        "nohup",
        "timeout",
        "nice",
        "which",
    ):
        script = stubs / name
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)

    for argv in (
        [str(stubs / "emrg"), "server", "stop"],
        [str(stubs / "emrg"), "server", "restart"],
        [str(stubs / "python"), "-m", "emrg", "server", "stop"],
        [str(stubs / "python"), "-m", "emrg", "server", "restart"],
        [str(stubs / "pkill"), "-f", "emrg.server"],
        [str(stubs / "killall"), "emrgd"],
        f"{stubs / 'pkill'} -f 'python -m emrg'",
        # a shell runs the line it is handed: the same act, one level in
        [str(stubs / "sh"), "-c", f"{stubs / 'emrg'} server stop"],
        [str(stubs / "sh"), "-c", f"{stubs / 'emrg'} server stop && echo done"],
        [str(stubs / "bash"), "-lc", f"{stubs / 'python'} -m emrg server restart"],
        # a wrapper hands the program over: the emrg program is not tokens[0]
        [str(stubs / "env"), str(stubs / "emrg"), "stop"],
        [str(stubs / "nohup"), str(stubs / "emrg"), "server", "restart"],
        [str(stubs / "timeout"), "5", str(stubs / "emrg"), "server", "stop"],
        [str(stubs / "nice"), "-n", "5", str(stubs / "pkill"), "-f", "emrg.server"],
    ):
        with pytest.raises(AssertionError, match="red-line violation"):
            subprocess.Popen(argv)

    # shell punctuation glued to the program or the verb (veto cyc20260918-043412).
    # `emrg` is a stub here *and* on `PATH`, so the shell cannot reach the host's
    # real CLI even if the refusal this block asserts were removed.
    pinned = {"PATH": str(stubs)}
    for argv in (
        [str(stubs / "sh"), "-c", "(emrg server restart)"],
        [str(stubs / "sh"), "-c", "$(which emrg) server stop"],
        [str(stubs / "sh"), "-c", "`emrg server stop`"],
        [str(stubs / "sh"), "-c", "'emrg' server stop"],
        [str(stubs / "sh"), "-c", 'env -S "emrg server stop"'],
        [str(stubs / "sh"), "-c", "emrg server (stop)"],
        [str(stubs / "sh"), "-c", "(pkill -f emrg.server)"],
        # a newline separates two commands exactly as `;` does
        [str(stubs / "sh"), "-c", "emrg --help\nemrg server stop"],
        # no shell at all: the verb itself is glued to punctuation
        [str(stubs / "emrg"), "server", "(stop)"],
        # characters a shell DROPS rather than a token edge: an escape and quote
        # concatenation. Both were measured to reach the live daemon — a stub
        # `emrg` on PATH ran, with argv `server stop`, for each — and both were
        # allowed by the edge-strip rule, because `str.strip` never saw them
        # (cycle cyc20260918-071815, reviewing this PR's own veto fix).
        [str(stubs / "sh"), "-c", "\\emrg server stop"],
        [str(stubs / "sh"), "-c", "'e''mrg' server stop"],
        [str(stubs / "sh"), "-c", "e\\mrg server stop"],
        [str(stubs / "sh"), "-c", "emrg sto\\p"],
        [str(stubs / "sh"), "-c", "e''mrg server stop"],
        # the accepted over-refusal: the shell reads this as ONE command name with
        # spaces (`emrg server stop: command not found`, measured), but the split
        # here is whitespace-only by design, so it arrives as the act's own token
        # stream and is refused. Cheap and loud; no test writes it.
        [str(stubs / "sh"), "-c", "emrg\\ server\\ stop"],
    ):
        with pytest.raises(AssertionError, match="red-line violation"):
            subprocess.Popen(argv, env=pinned)


def test_an_emrg_named_path_costs_a_false_refusal(tmp_path, daemon_spawn_refusal):
    """The mirror direction: an `emrg` basename in *any* position can make a verb decisive.

    Measured by this PR's veto (`cyc20260918-043412`) with this repo's own path:
    the program scan accepts any token whose basename is `emrg`/`emrgd`, so
    `git -C <this repo> log --grep restart` is refused — a false refusal for an
    argv that runs nothing but git.

    Pinned rather than repaired, and the reason is the guard's own bias: the repair
    is to require *command position*, and the hole that opens is an `emrg` program
    handed over by anything not on the wrapper list (`xargs -I{} emrg {} stop`).
    A false refusal fails at its own assertion with the red line in the message; a
    false allowance SIGTERMs the live daemon mid-suite. So the asymmetry is kept,
    and what this pins is its exact scope: the *verb* is what decides, so the same
    path with a read-only verb stays allowed.
    """
    emrg_named = tmp_path / "emrg"
    emrg_named.mkdir()

    assert not daemon_spawn_refusal(["git", "-C", str(emrg_named), "log"])
    assert daemon_spawn_refusal(
        ["git", "-C", str(emrg_named), "log", "--grep", "restart"]
    ), "the known cost narrowed: re-check the docstring in conftest before relaxing it"


def test_guard_refuses_the_cli_stop_fallback_signal():
    """Positive: `emrg.__main__`'s own SIGTERM route is guarded too.

    `emrg/__main__.py::_stop_daemon` signals the pid it read from a `ping` frame
    and does **not** pass through `emrg._stop_all`'s five stop functions, so
    `_guard_stop_all_hermeticity` never saw it: a test calling that function
    in-process would SIGTERM the daemon this evolution is running on, and the two
    files that describe the stop path said so only in prose (issue #1337, item 2's
    three named routes — in-process, subprocess, client-side restart — are only
    covered when this one is).

    The refusal comes before any signal, so pid 4242 is never signalled, and the
    shim's identity is asserted first so the refusal is the *installed* guard's
    rather than one a test body arranged.
    """
    import signal

    import emrg.__main__ as cli_mod

    assert type(cli_mod.os).__name__ == "_NoSignalOs"
    with pytest.raises(AssertionError, match="red-line violation"):
        cli_mod.os.kill(4242, signal.SIGTERM)
    # and it names the module it fired in — two shims, one rule, so the message is
    # the only thing that says which route was reached
    with pytest.raises(AssertionError, match=r"emrg\.__main__ tried to signal"):
        cli_mod.os.kill(4242, signal.SIGTERM)


def test_the_cli_shim_still_answers_everything_else():
    """Negative: only `kill`/`killpg` are replaced — the CLI runs on the rest of `os`."""
    import os

    import emrg.__main__ as cli_mod

    assert cli_mod.os.getpid() == os.getpid()
    assert cli_mod.os.sep == os.sep
    assert cli_mod.os.environ is os.environ


def test_guard_allows_read_only_spawns(daemon_spawn_refusal):
    """Negative: the refusal keys on the verb, not on "it mentions emrg".

    The suite spawns git, node, pytest and the `emrg` CLI itself for read-only
    verbs, and a guard that refused every emrg invocation would break those tests
    and teach the next reader to distrust it. Those shapes have to stay allowed,
    and this test pins that on the argv predicate — which is the guard's own
    question, and the one thing only this file can ask. Read together with
    `test_guard_refuses_spawning_the_stop_or_restart_cli` above, which drives the
    installed `Popen` wrapper and sees it raise, the refusal is pinned in both
    directions: a guard that refused everything fails here, and a guard that
    refused nothing fails there.

    Nothing is spawned here, deliberately. Two earlier revisions of this test did
    spawn, and the windows-2025 leg reddened on the later one twice: first a
    `UnicodeDecodeError` raised in the parent while decoding the child, then —
    once the child's bytes were captured instead of decoded — a
    `KeyboardInterrupt` reported in the parent while its main thread was blocked
    in a `Condition.wait`. The second is the reason this test spawns nothing at
    all: the spawn is not this guard's subject, `tests/test_cli_output_encoding.py`
    already runs this CLI on every platform and pins its output in both codecs,
    and the suite's other spawns already pin that an allowed argv reaches `Popen`.
    What this guard owns is which argv it refuses.
    """
    import sys

    allowed = [
        # the CLI forms — the discriminating twins of `server stop` / `restart`
        [sys.executable, "-m", "emrg", "--help"],
        [sys.executable, "-m", "emrg", "server", "status"],
        [sys.executable, "-m", "emrg", "rant", "hello"],
        ["emrg", "--version"],
        ["emrg", "update"],
        # the same wrappers and shells carrying a read-only verb: the refusal keys
        # on the verb, so closing the wrapper spellings must not close these
        ["sh", "-c", "emrg server status"],
        ["bash", "-lc", "python3 -m emrg --help"],
        # a read-only emrg command whose *pipeline* carries the verb as data
        ["sh", "-c", "emrg --help | grep stop"],
        ["env", "emrg", "--version"],
        ["timeout", "5", "emrg", "server", "status"],
        # emrg mentioned without being the program being run
        ["git", "log", "--grep", "emrg"],
        ["nice", "-n", "5", "git", "log", "--grep", "emrg"],
        # the verb present as *data* rather than as the verb being asked for
        ["git", "commit", "-m", "stop"],
        # everything else the suite spawns
        ["git", "status"],
        ["node", "--version"],
        ["uv", "run", "python", "-m", "pytest", "tests/", "-v"],
        [sys.executable, "-c", "print('ok')"],
    ]
    for argv in allowed:
        assert not daemon_spawn_refusal(argv), argv


def test_guard_refuses_signalling_the_daemon_group():
    """Positive: `killpg` is refused too — the same act, wider blast radius.

    `__getattr__` delegates every attribute of the `os` substitute to the real
    module, so before this arm `killpg` was the one signal route *out* of the
    guard: measured, `guard.killpg(4242, 15)` reached the stand-in `os` and
    returned normally. Nothing in the restart path signals a group today, which is
    why this is a scope arm rather than a live hole — but a group signal is the
    same act with a wider target, and the wrapper's own docstring would otherwise
    be the only place saying so.

    The group `4242` is never signalled: the refusal precedes the delegation, and
    the assertion is on the refusal. This is also the arm that keeps the `os`
    substitute from being "refuse `kill`": the passthrough test below pins what
    must still delegate.
    """
    import signal

    import emrg.client.daemon_manager as daemon_manager

    assert type(daemon_manager.os).__name__ == "_NoSignalOs"
    with pytest.raises(AssertionError, match="red-line violation"):
        daemon_manager.os.killpg(4242, signal.SIGTERM)


def test_the_guarded_popen_is_still_a_type():
    """The suite-wide `Popen` patch must not make `isinstance` unanswerable.

    `isinstance(proc, subprocess.Popen)` is the question a reader asks about a
    child, and the revision before this one installed the refusal as a plain
    function over the attribute — which answers that question with `TypeError:
    isinstance() arg 2 must be a type, a tuple of types, or a union` (measured). No
    test asks it today, so nothing was red; the failure it would produce names
    `isinstance`, not the red line, and would send the next reader to the wrong
    place. A subclass refuses identically and keeps the question answerable, and
    the name is kept because that is what the attribute is called.

    The child spawned here is read-only — the guard allows it, which is the other
    half of this test: a guard that refused every spawn would fail on its own
    setup line rather than on an assertion.
    """
    import subprocess
    import sys

    assert isinstance(subprocess.Popen, type)
    assert subprocess.Popen.__name__ == "Popen"
    # Identity, the same way `test_guard_refuses_signalling_the_daemon` asserts
    # `type(daemon_manager.os).__name__ == "_NoSignalOs"`: the installed object must
    # be the guard's own subclass, not the real class left in place. A bare `type`
    # check would pass on an unpatched `subprocess.Popen`; under the previous
    # function patch this line raises `AttributeError` (a function has no `__mro__`)
    # — i.e. the arm discriminates in the direction the property is about.
    installed = subprocess.Popen
    assert installed is not installed.__mro__[1], (
        "the installed subprocess.Popen is the real class, not the guard's subclass"
    )
    assert installed.__mro__[1].__name__ == "Popen"

    proc = subprocess.Popen(
        [sys.executable, "-c", "print('guarded')"],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # The line that raised `TypeError` before this arm exists.
    assert isinstance(proc, subprocess.Popen)
    assert proc.communicate()[0].strip() == "guarded"


def test_kill_zero_is_a_probe_on_posix_and_a_ctrl_c_on_windows(daemon_kill_is_a_probe):
    """`kill(pid, 0)` means "does this pid exist" on POSIX and "interrupt" on Windows.

    The guard delegates `kill(pid, 0)` so the restart path's liveness wait keeps
    working, and the whole justification for that delegation is the POSIX reading.
    It is not the Windows one: `signal.CTRL_C_EVENT` is **0**, and CPython's
    `os_kill_impl` sends `CTRL_C_EVENT` through `GenerateConsoleCtrlEvent(pid,
    sig)` — a Ctrl+C to that process group. A guard that delegated that value on
    Windows would permit exactly the act it exists to refuse, and would do it by
    signalling the console the test process is sitting on.

    Pinned as a table rather than by calling `os.kill`: the Windows row cannot be
    exercised on this runner without sending the signal it describes.
    """
    assert daemon_kill_is_a_probe(0, "linux")
    assert daemon_kill_is_a_probe(0, "darwin")
    assert not daemon_kill_is_a_probe(0, "win32")
    # Cygwin's `os.kill` is the POSIX one and Cygwin Python has no
    # `signal.CTRL_C_EVENT`, so the boundary is the `win*` platform string and
    # not "anything that runs on Windows".
    assert daemon_kill_is_a_probe(0, "cygwin")
    # every real signal is a signal — zero is the only value in question
    assert not daemon_kill_is_a_probe(15, "linux")
    assert not daemon_kill_is_a_probe(9, "win32")


def test_the_refusal_asks_that_decision_instead_of_assuming_it(daemon_kill_refusal):
    """The wrapper routes `kill` through the probe decision, both ways.

    Driven over a stand-in `os`, so the delegated branch appends to a list
    instead of reaching a real `os.kill`: the Windows row's assertion is that
    nothing is delivered, which a test cannot make by delivering it.
    """
    delivered: list = []

    class _RecordingOs:
        def kill(self, pid, sig):
            delivered.append((pid, sig))

    delegating = daemon_kill_refusal(_RecordingOs(), is_probe=lambda sig: True)
    assert delegating.kill(4711, 0) is None
    assert delivered == [(4711, 0)]

    refusing = daemon_kill_refusal(_RecordingOs(), is_probe=lambda sig: False)
    with pytest.raises(AssertionError, match="red-line violation"):
        refusing.kill(4711, 0)
    assert delivered == [(4711, 0)], "the refused call reached an os.kill anyway"


def test_other_os_attributes_pass_through_unchanged():
    """The guard replaces `kill` and nothing else — `daemon_manager` uses more of `os`."""
    import os

    import emrg.client.daemon_manager as daemon_manager

    installed = daemon_manager.os  # the wrapper the autouse guard installed
    assert installed.getpid() == os.getpid()
    assert installed.path is os.path
    assert installed.sep == os.sep
