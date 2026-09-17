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


def test_guard_refuses_spawning_the_stop_or_restart_cli():
    """Positive: a child that would stop/restart the daemon is refused.

    Never actually spawned — the guard raises first. Both shapes are covered
    because an instance does not have to use the installed script: `python -m
    emrg server restart` is the same act, and `pkill -f emrg.server` is the one
    a shell reaches for.
    """
    import subprocess
    import sys

    for argv in (
        ["emrg", "server", "stop"],
        ["emrg", "server", "restart"],
        [sys.executable, "-m", "emrg", "server", "stop"],
        [sys.executable, "-m", "emrg", "server", "restart"],
        ["pkill", "-f", "emrg.server"],
        ["killall", "emrgd"],
        "pkill -f 'python -m emrg'",
    ):
        with pytest.raises(AssertionError, match="red-line violation"):
            subprocess.Popen(argv)


def test_guard_allows_read_only_spawns():
    """Negative: the refusal keys on the verb, not on "it mentions emrg".

    The suite spawns the emrg CLI for read-only verbs, and a guard that refused
    every emrg invocation would break those tests and teach the next reader to
    distrust it. `--help` is the discriminating twin of `server stop` above.
    """
    import subprocess
    import sys

    helped = subprocess.run(
        [sys.executable, "-m", "emrg", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert helped.returncode == 0, helped.stderr
    # The property, not argparse's exact prog wording: the CLI ran and printed its
    # usage. Spelling the program name would test a derivation that is not ours.
    assert "usage:" in helped.stdout

    benign = subprocess.run(
        [sys.executable, "-c", "print('ok')"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert benign.stdout.strip() == "ok"
