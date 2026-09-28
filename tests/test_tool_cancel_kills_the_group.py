"""A cancelled tool call must not leave its command running.

Host P0 rant 2026-09-28T13:00:38.  The tool-loop *timeout* path already killed the
process group (host P0 rant 2026-09-27T19:41:13, PR #1662).  The *cancellation*
path did not: ``run_command`` awaited ``_collect`` with nothing around it, so a
cancel landed on an await inside the collector and left the child and its
descendants running in their own session — unreachable even by a daemon restart
(``daemon.py`` has no ``killpg``/``reap``/``terminate`` at all).

Measured on the installed 0.3.4 with the rant's probe: ``run_command("echo $$ >
pidf; sleep 300", timeout=600)`` cancelled after 2s left the shell alive (``Ss``,
a session leader of its own) through the rest of the probe, and only the probe's
own ``killpg`` removed it.  The three triggers are all ordinary: the host's ESC,
a turn replaced by a new message, and a connection closing.

The two tests below are deliberately different in kind.  The bash one runs a real
process and proves the group really dies — this is the one the mutation arm must
kill.  The pwsh one pins the wiring, because no ``pwsh`` exists on this host; its
twin's killer is the same literal function the bash test exercises end to end.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest

from emrg.sandbox.policy import SandboxPolicy
from emrg.tools import pwsh_tool_v2 as pwsh
from emrg.tools.bash_tool_v2 import BashToolV2

#: v2's inner shell is ``bash`` by contract, and the group semantics asserted
#: here are the POSIX ones (``preexec_fn=os.setsid``); Windows has no group of
#: our own, so the assertion would be about a different mechanism.
needs_a_shell = pytest.mark.skipif(
    sys.platform == "win32",
    reason="v2's inner shell is bash; the group semantics under test are POSIX's",
)


async def _gone(pid: int, tries: int = 60) -> bool:
    """Whether ``pid`` stopped existing, waiting for the signal to be delivered.

    ``os.kill(pid, 0)`` is the POSIX liveness probe and is only used from the
    POSIX-only test below: on Windows the same call is ``CTRL_C_EVENT``, which
    would signal a whole process group rather than ask a question.
    """
    for _ in range(tries):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


@needs_a_shell
def test_a_cancelled_bash_call_kills_the_shell_and_its_child(tmp_path):
    """The host's shape: ESC mid-run, and the command keeps running.

    The cancellation is applied to the *tool call* — ``BashToolV2().execute``,
    which is what the daemon's turn task awaits when it cancels — so what is
    pinned is the contract at that boundary: the caller receives the cancellation
    unchanged, and the process group is gone.

    Two pids are recorded by the command itself while it is alive: ``$$`` — the
    group leader, since ``preexec_fn=os.setsid`` made the child a session
    leader — and ``$!``, the background child that shares its group.  The second
    one is the point: the direct child is the only handle asyncio holds, so only
    a group kill can reach a descendant, and asserting on the leader alone would
    pass on a fix that reaped just the process asyncio knew about.
    """
    async def _test():
        pid_file = tmp_path / "cancel.pid"
        command = f"echo $$ > {pid_file}; sleep 300 & echo $! >> {pid_file}; sleep 300"
        task = asyncio.ensure_future(
            BashToolV2().execute(
                {
                    "command": command,
                    "intent": "probe",
                    "sandbox": "danger-full-access",
                    "workspace": str(tmp_path),
                    "workdir": str(tmp_path),
                    "timeout": 600,
                }
            )
        )
        for _ in range(60):
            if pid_file.exists() and len(pid_file.read_text().split()) == 2:
                break
            await asyncio.sleep(0.05)
        leader, child = (int(value) for value in pid_file.read_text().split())

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for name, pid in (("the shell", leader), ("its background child", child)):
            if not await _gone(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise AssertionError(
                    f"{name} (pid {pid}) outlived the cancelled call: the command "
                    "became an orphan in its own session, which is the shape the "
                    "host's P0 rant measured"
                )

    asyncio.run(_test())


class _HangingStream:
    """A stream that never delivers, so the collector stays inside its await."""

    async def read(self) -> bytes:
        await asyncio.sleep(300)
        return b""


class _HangingProcess:
    """Stand-in for a spawned pwsh that never finishes.

    The pid is one no group can have, so the real killer would be a harmless
    no-op if it ran — it does not, because this test records the call instead.
    """

    def __init__(self) -> None:
        self.pid = 2**22 - 1
        self.returncode = None
        self.stdout = _HangingStream()
        self.stderr = _HangingStream()

    async def wait(self) -> int:
        await asyncio.sleep(300)
        return 0


def test_a_cancelled_pwsh_call_reaches_the_group_killer(tmp_path, monkeypatch):
    """The twin's wiring: a cancellation must not skip the cleanup call.

    This is not the end-to-end shape the bash test is, and it does not claim to
    be: no ``pwsh`` exists on this host, so no real Windows-dialect process can
    be spawned here.  What it pins is the half that was missing from both files —
    ``run_command`` may not let a cancellation leave without signalling — and the
    killer it must reach is the same literal function the bash test exercises
    against a real group.
    """
    killed: list[int] = []
    process = _HangingProcess()

    async def _spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(pwsh, "resolve_pwsh_path", lambda *a, **k: "pwsh")
    monkeypatch.setattr(pwsh.asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(pwsh, "_kill_process_group", lambda proc: killed.append(proc.pid))

    async def _test():
        task = asyncio.ensure_future(
            pwsh.run_command(
                "Write-Output hi",
                policy=SandboxPolicy(mode="danger-full-access", workspace_root=str(tmp_path)),
                workdir=str(tmp_path),
                timeout=600.0,
                platform_name="linux",
            )
        )
        await asyncio.sleep(0.05)  # one turn of the loop: now inside the collector
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_test())
    assert killed == [process.pid], (
        "a cancelled pwsh call must signal the process group before the "
        f"cancellation leaves run_command; recorded kills: {killed}"
    )
