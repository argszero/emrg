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


def test_guard_still_allows_a_liveness_probe():
    """Negative: `kill(pid, 0)` is a probe, not a signal — it must reach `os`.

    This is the arm that keeps the guard from being "refuse every os.kill":
    `daemon_manager` uses the probe to wait for the old daemon to die
    (rant 2026-08-18T12:49:09 ②), so refusing it would turn a benign check into
    an error.
    """
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
    """
    import subprocess

    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name in ("emrg", "emrgd", "pkill", "killall", "kill", "python"):
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
    ):
        with pytest.raises(AssertionError, match="red-line violation"):
            subprocess.Popen(argv)


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
        # emrg mentioned without being the program being run
        ["git", "log", "--grep", "emrg"],
        # the verb present as *data* rather than as the verb being asked for
        ["git", "commit", "-m", "stop"],
        # everything else the suite spawns
        ["git", "status"],
        ["node", "--version"],
        [sys.executable, "-c", "print('ok')"],
    ]
    for argv in allowed:
        assert not daemon_spawn_refusal(argv), argv
