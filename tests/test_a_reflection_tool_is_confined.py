"""A tool the reflection loop runs is confined to the session it reflects on.

Measured 2026-10-03 (`cyc20261003-090949`)
------------------------------------------
`_maybe_reflect_memory` runs a mini tool loop after an exchange, so the agent can
write what it learned. Its tools were dispatched with

    result = await tool.execute(args)

— the call the model sent, and nothing else. Every other dispatch site in the
daemon injects the two values the model must not choose (`_inject_tool_arguments`:
the session's cwd, and the tier the call runs under), and this one did not. Two
consequences, both measured here with the **real** `write` tool:

| the call | before | after |
|---|---|---|
| `write(file_path="note.md")` | created **`<daemon process cwd>/note.md`** — on this host, the repository root, beside the source | created `<session cwd>/note.md` |
| `write(file_path=<outside the session workspace>)` | **allowed** (`error=False`) | **refused** — `workspace-write sandbox: blocked file write` |

The resolved policy said why in one line: no tier means `policy.DEFAULT_MODE`,
which is `danger-full-access`, with `workspace_root` = `os.getcwd()` — the
daemon's own working directory, not the session's. So background reflection wrote
unconfined, wherever the daemon happened to be started.

Why the session's tier is the right value rather than a new one
--------------------------------------------------------------
Reflection writes the memory of the session it belongs to, and both of that
session's memory directories live under `session.cwd`: its own
`<session dir>/memory/` (the session dir is `<cwd>/.emrg/sessions/<id>`) and the
project's `<cwd>/.emrg/memory/`. `resolve_client_tier(session)` is the tier the
session itself runs at (its own when set, else the ruling's default
`workspace-write`), so it is exactly the tier that lets reflection do its job in
the default case and confines it in every case. It is also the same rule the main
loop already states: the tier a call runs under is the caller's, not the agent's.

Two legs, both directions
-------------------------
Each behaviour is pinned on a session the test builds under `tmp_path`, and the
tier is checked in both of its directions (default → `workspace-write`; a
read-only session → `read-only`), because a rule that always injects one constant
would pass a one-sided test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from emrg.sandbox.policy import resolve_policy
from emrg.sandbox.roots import writable_roots
from emrg.session import Session


def _is_within(path: Path, root: str) -> bool:
    """Whether `path` sits under `root` (the fence's containment, plainly)."""
    try:
        path.resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _recorder_tool(captured: list[dict], name: str = "write"):
    """A stand-in tool that records the arguments it was handed."""

    class Recorder:
        async def execute(self, args):
            captured.append(dict(args))
            result = MagicMock()
            result.content = "ok"
            result.error = False
            return result

    return Recorder()


def _server_for(tool):
    """A daemon whose registry returns `tool` and whose LLM asks for one call."""
    from emrg.server.daemon import EmrgServer

    server = EmrgServer.__new__(EmrgServer)
    server.tools = MagicMock()
    server.tools.get.return_value = tool
    server.skills = []
    server._max_tool_rounds = 10
    server.llm = AsyncMock()
    server.llm.chat.return_value = {
        "content": "writing a memory",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "write",
                    "arguments": '{"file_path": "note.md", "content": "x"}',
                },
            }
        ],
    }
    return server


def _reflect_once(server, session, cwd: Path) -> list[dict]:
    """Run one reflection and return the arguments its tool received."""
    captured: list[dict] = []
    server.tools.get.return_value = _recorder_tool(captured)

    async def _run():
        server._maybe_reflect_memory(
            session,
            "a substantive question the host asked",
            "a substantive answer the assistant gave, long enough to reflect on",
        )
        # The loop is fire-and-forget; one tool call means one round in practice,
        # and the recorder stops the loop growing past the first round.
        for _ in range(40):
            await asyncio.sleep(0.02)
            if captured:
                break

    asyncio.run(_run())
    return captured


def _session(tmp_path: Path, sid: str = "s_reflect_confine") -> Session:
    cwd = tmp_path / "project"
    cwd.mkdir(parents=True, exist_ok=True)
    return Session.create_with_id(sid, cwd)


class TestTheReflectionLoopInjectsTheBoundary:
    def test_a_filesystem_tool_is_given_the_session_cwd_and_tier(self, tmp_path):
        """The defect: neither key was present, so the tool resolved its own."""
        session = _session(tmp_path)
        captured = _reflect_once(_server_for(None), session, session.cwd)
        assert captured, "the reflection loop ran no tool at all"
        args = captured[0]
        assert args["sandbox"] == "workspace-write", args
        assert args["workspace"] == str(session.cwd), args

    def test_a_shell_tool_also_gets_the_session_cwd_as_workdir(self, tmp_path):
        """`workdir` is the second half of the same rule, for the shell tools."""
        from emrg.server.daemon import EmrgServer

        session = _session(tmp_path, "s_reflect_shell")
        args: dict = {"command": "true"}
        EmrgServer._inject_tool_arguments("bash", args, session, "workspace-write")
        assert args["workdir"] == str(session.cwd)
        assert args["workspace"] == str(session.cwd)
        assert args["sandbox"] == "workspace-write"

    def test_a_read_only_session_injects_read_only(self, tmp_path):
        """The other direction of the same rule: the session's tier wins.

        Without this leg a fix that hard-coded `workspace-write` would pass, and
        a read-only session's reflection would quietly gain write access.
        """
        session = _session(tmp_path, "s_reflect_readonly")
        session.set_sandbox("read-only")
        captured = _reflect_once(_server_for(None), session, session.cwd)
        assert captured
        assert captured[0]["sandbox"] == "read-only", captured[0]

    def test_no_tier_means_no_boundary_keys(self, tmp_path):
        """The `None` path stays what it was: the tool's own default applies.

        The reflection loop is the caller that used to be here; the main loop and
        the scheduler always pass a tier (`req.sandbox`), so this leg pins the
        documented fallback rather than a live caller.
        """
        from emrg.server.daemon import EmrgServer

        session = _session(tmp_path, "s_reflect_notier")
        args = {"command": "true"}
        EmrgServer._inject_tool_arguments("bash", args, session, None)
        assert "sandbox" not in args and "workspace" not in args
        assert args["workdir"] == str(session.cwd), "the cwd is injected regardless"

    def test_the_memory_directories_are_inside_the_injected_root(self, tmp_path):
        """Why the session's own cwd is enough for reflection to do its job.

        If this ever stops holding, the injection above would confine reflection
        away from the memory it exists to write - the failure mode that made the
        fix look unsafe.
        """
        session = _session(tmp_path, "s_reflect_memdirs")
        cwd = str(session.cwd)
        assert str(session.memory_dir).startswith(cwd), session.memory_dir
        assert str(session.cwd / ".emrg" / "memory").startswith(cwd)


class TestTheConfinementIsReal:
    """The same call, judged by the real tool under both policies.

    Both legs below hand the **real** `write` tool a real path, so a run under a
    mutated policy really writes. Each one therefore removes its own target in a
    `finally`: measured 2026-10-03 (`cyc20261003-090949`), the first draft left
    `<repo>/note.md` and `<repo>/emrg-reflect-probe-should-not-exist.txt` behind
    when a mutation arm made the write land in the daemon's cwd — and the stray
    files then made the *next* arm's preflight fail, so the arm reported
    TARGET-BROKEN instead of attributing anything. A test that writes must also
    un-write.
    """

    def _write(self, args: dict):
        from emrg.tools.write_tool import WriteTool

        return asyncio.run(WriteTool().execute(dict(args)))

    def test_a_relative_write_lands_in_the_session_not_the_daemon(self, tmp_path):
        """Measured: this used to land in the daemon's process cwd — the repo root.

        The daemon-cwd candidate is cleaned up as well: under a mutated policy the
        write really goes there, and leaving it would be a write into the checkout.
        """
        session = _session(tmp_path, "s_reflect_relwrite")
        injected = {"file_path": "note.md", "content": "mem"}
        from emrg.server.daemon import EmrgServer

        daemon_cwd_copy = Path.cwd() / "note.md"
        try:
            EmrgServer._inject_tool_arguments(
                "write", injected, session, "workspace-write"
            )
            result = self._write(injected)
            assert result.error is False, result.content
            assert (session.cwd / "note.md").is_file()
        finally:
            if daemon_cwd_copy.exists():
                daemon_cwd_copy.unlink()

    def test_a_path_outside_the_session_workspace_is_refused(self, tmp_path):
        """Measured: with no tier this was allowed (`danger-full-access`).

        The target is the checkout, which is outside the session's workspace root
        *and* outside the platform temp areas — `workspace-write` grants both, so a
        `tmp_path` sibling would be allowed and would prove nothing. The assertion
        is that the fence refuses it, and the reason text is required to be the
        fence's own ('blocked file write'), so an OS permission error could not be
        mistaken for a confinement.
        """
        repo = Path(__file__).resolve().parents[1]
        granted = writable_roots(
            resolve_policy(mode="workspace-write", workspace_root=str(tmp_path))
        )
        if any(_is_within(repo, root) for root in granted):
            pytest.skip(
                f"this checkout ({repo}) sits inside a root workspace-write grants "
                f"({granted}), so it is not an 'outside' subject"
            )
        session = _session(tmp_path, "s_reflect_outside")
        target = repo / "emrg-reflect-probe-should-not-exist.txt"
        args = {"file_path": str(target), "content": "x"}
        from emrg.server.daemon import EmrgServer

        try:
            EmrgServer._inject_tool_arguments("write", args, session, "workspace-write")
            result = self._write(args)
            assert result.error is True, "a write outside the session workspace was allowed"
            assert "blocked file write" in result.content, result.content
            assert not target.exists()
        finally:
            if target.exists():
                target.unlink()

    def test_the_same_tier_allows_a_path_inside_the_workspace(self, tmp_path):
        """The control for the leg above: the boundary is what refuses it.

        Same tier, same tool, a target inside the workspace root: allowed. Without
        this leg a `workspace-write` that refused everything would pass.
        """
        session = _session(tmp_path, "s_reflect_inside")
        args = {"file_path": "inside.txt", "content": "x"}
        from emrg.server.daemon import EmrgServer

        EmrgServer._inject_tool_arguments("write", args, session, "workspace-write")
        result = self._write(args)
        assert result.error is False, result.content
        assert (session.cwd / "inside.txt").is_file()
