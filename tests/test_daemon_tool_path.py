"""The daemon's tool directories must not depend on what launched it.

Why this file exists
--------------------
Rant 2026-09-27T19:45:54 (project `emrg`), defect B — measured on this host.
After the host restarted the daemon from the GUI, the new daemon's PATH was
`/Users/argszero/.emrg/install/bin:/usr/bin:/bin:/usr/sbin:/sbin`: no
`~/.local/bin` (uv), no `/opt/homebrew/bin` (gh), because a Dock-started GUI
hands its child launchd's minimal environment and `bin/emrgd` only *prepends*
its own `bin/`. Every scheduled task's tools then failed with `command not
found`, each agent rediscovering the directories on its own, and nothing in
`emrgd.log` said what the PATH had been. A terminal-started daemon and a
GUI-started one therefore behaved differently — that difference is the defect.

The fix is `emrg/tool_path.py` called from the two entries every launch path
ends in (`emrg/server/__main__.py` for `emrgd`, both GUI and terminal;
`emrg/__main__.py::_run_daemon` for `emrg server`), which is why the checks
below split in two: the *behaviour* of the augmentation and its log line, and
the fact that each entry still calls it. The second half is the one a refactor
drops in silence — the augmentation would keep passing its own tests while no
daemon ever ran it.

⚠️ Nothing here starts, stops or restarts a daemon (MANIFESTO 第四条附则二), and
nothing opens `~/.emrg/config.toml` or `~/.emrg/emrgd.log`: the two entry tests
stub the logging setup, the config load and the server itself, so no process is
spawned and no host file is written. The directories used are `tmp_path`'s.

Named limit
-----------
This pins that the PATH is normalized and recorded, not that a given tool is
then found: whether `gh` or `uv` resolves is the child's answer, and no test
here can speak for it. What the last test does show is the one link that would
make the rest moot — that the directory added at startup is in the environment a
tool child actually inherits (`emrg/server/git_utils.py::no_prompt_env`, the env
`bash_tool_v2` spawns with).
"""

from __future__ import annotations

import logging
import types
from pathlib import Path

import pytest

from emrg import tool_path


def _isdir_except(missing: set[str]):
    """An ``isdir`` that says every directory exists except the named ones.

    Which absolute prefixes exist is the *machine's* fact (`/opt/homebrew/bin`
    is here, absent on a Linux box); naming them explicitly keeps these tests
    from answering a question about this host.
    """

    def exists(path) -> bool:
        return str(path) not in missing

    return exists


def _home_with(tmp_path: Path, *names: str) -> Path:
    home = tmp_path / "home"
    for name in names:
        (home / name).mkdir(parents=True, exist_ok=True)
    return home


def test_a_tool_dir_is_appended_after_the_inherited_path(tmp_path):
    """The host's own PATH keeps priority; ours is added behind it."""
    home = _home_with(tmp_path, ".local/bin")
    env = {"PATH": "/usr/bin:/bin"}

    # Only the home directory exists: the absolute prefixes are the machine's fact,
    # and naming them missing keeps this assertion about the augmentation itself.
    added = tool_path.augment_path(
        env, platform="linux", home=home,
        isdir=_isdir_except({"/opt/homebrew/bin", "/usr/local/bin"}),
    )

    assert [str(d) for d in added] == [str(home / ".local/bin")]
    assert env["PATH"] == f"/usr/bin:/bin:{home / '.local/bin'}"


def test_a_directory_that_does_not_exist_is_not_added(tmp_path):
    """A healthy environment is left byte-identical — no empty entries, no invention."""
    home = _home_with(tmp_path)  # no .local/bin
    env = {"PATH": "/usr/bin:/bin"}
    every_prefix = {str(d) for d in tool_path.tool_dirs("linux", home)}

    added = tool_path.augment_path(env, platform="linux", home=home, isdir=_isdir_except(every_prefix))

    assert added == []
    assert env["PATH"] == "/usr/bin:/bin"


def test_a_directory_already_on_the_path_is_not_added_again(tmp_path):
    """A trailing separator is the same directory, not a second one."""
    home = _home_with(tmp_path, ".local/bin")
    already = f"{home / '.local/bin'}/"
    env = {"PATH": f"{already}:/usr/bin"}

    added = tool_path.augment_path(
        env, platform="linux", home=home,
        isdir=_isdir_except({"/opt/homebrew/bin", "/usr/local/bin"}),
    )

    assert str(home / ".local/bin") not in [str(d) for d in added]
    assert env["PATH"].count(str(home / ".local/bin")) == 1


def test_only_the_prefixes_that_exist_are_offered(tmp_path):
    """The absolute prefixes are candidates, not assertions about the machine."""
    home = _home_with(tmp_path, ".local/bin")
    offered = {str(d) for d in tool_path.tool_dirs("linux", home)}
    assert offered == {str(home / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"}

    env = {"PATH": "/usr/bin"}
    added = tool_path.augment_path(
        env, platform="linux", home=home, isdir=_isdir_except({"/opt/homebrew/bin"})
    )

    assert [str(d) for d in added] == [str(home / ".local/bin"), "/usr/local/bin"]


def test_windows_gets_the_windows_list_and_a_semicolon(tmp_path):
    """`emrgd.cmd` needs no second copy of this logic — the entry normalizes it."""
    home = _home_with(tmp_path, ".local/bin")
    assert [str(d) for d in tool_path.tool_dirs("win32", home)] == [str(home / ".local/bin")]

    inherited = r"C:\Windows\system32"
    env = {"PATH": inherited}
    added = tool_path.augment_path(env, platform="win32", home=home, isdir=_isdir_except(set()))

    assert [str(d) for d in added] == [str(home / ".local/bin")]
    # A Windows PATH is joined with ";", even when this host is POSIX.
    assert env["PATH"] == ";".join([inherited, str(home / ".local/bin")])


def test_the_startup_helper_records_the_effective_path(monkeypatch, caplog, tmp_path):
    """One INFO line: the daemon's only statement of the PATH its tools will see."""
    home = _home_with(tmp_path, ".local/bin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    with caplog.at_level(logging.INFO, logger="emrg.tool_path"):
        added = tool_path.ensure_tool_dirs()

    # `.local/bin` is first in the POSIX list, so a directory the host really has
    # (a Homebrew prefix) may follow it but cannot precede it.
    assert str(added[0]) == str(home / ".local/bin")
    records = [r for r in caplog.records if r.name == "emrg.tool_path"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert "effective PATH" in message
    assert str(home / ".local/bin") in message


def test_the_startup_helper_records_the_path_when_nothing_was_added(monkeypatch, caplog):
    """'nothing added' is an answer too — that line is what makes a healthy start checkable."""
    reachable = [str(d) for d in tool_path.tool_dirs() if d.is_dir()]
    if not reachable:  # pragma: no cover - a machine with none of them
        pytest.skip("no standard tool dir exists here, so the empty branch is unreachable")
    sep = tool_path.path_separator()
    monkeypatch.setenv("PATH", sep.join(reachable))

    with caplog.at_level(logging.INFO, logger="emrg.tool_path"):
        added = tool_path.ensure_tool_dirs()

    assert added == []
    records = [r for r in caplog.records if r.name == "emrg.tool_path"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "effective PATH" in message and reachable[0] in message


def test_the_launcher_entry_normalizes_the_path(monkeypatch):
    """`emrgd` execs `python -m emrg.server` — the GUI and the terminal, one entry."""
    from emrg.server import __main__ as server_main

    calls: list[bool] = []
    monkeypatch.setattr(server_main, "ensure_tool_dirs", lambda: calls.append(True))
    # Stubbed so the test opens no host file: these are the real entry's side effects.
    monkeypatch.setattr(server_main, "_configure_logging", lambda: None)
    monkeypatch.setattr(server_main, "_redirect_std_streams", lambda: None)
    monkeypatch.setattr("emrg.config.load_config", lambda: types.SimpleNamespace(llm=object()))

    class _Exit:
        exit_code = 0

        def write_record(self) -> None:
            pass

    async def _fake_run_server(_llm):
        return _Exit()

    monkeypatch.setattr(server_main, "run_server", _fake_run_server)

    with pytest.raises(SystemExit) as exit_info:
        server_main.main()

    assert exit_info.value.code == 0
    assert calls == [True], "the daemon entry stopped normalizing the PATH"


def test_the_foreground_entry_normalizes_the_path(monkeypatch):
    """`emrg server` runs the daemon in-process and must reach the same directories."""
    from emrg import __main__ as cli_main

    calls: list[bool] = []
    monkeypatch.setattr("emrg.tool_path.ensure_tool_dirs", lambda: calls.append(True))
    monkeypatch.setattr("emrg.config.load_config", lambda: types.SimpleNamespace(llm=object()))
    monkeypatch.setattr("emrg.server.daemon.run_server", lambda _llm: None)
    monkeypatch.setattr(cli_main, "asyncio", types.SimpleNamespace(run=lambda _coro: None))

    cli_main._run_daemon()

    assert calls == [True], "the foreground daemon entry stopped normalizing the PATH"


def test_the_added_directory_is_in_the_environment_a_tool_child_inherits(monkeypatch):
    """The link that would make the rest moot: startup PATH → the tool child's env."""
    from emrg.server.git_utils import no_prompt_env

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    added = tool_path.ensure_tool_dirs()
    if not added:  # pragma: no cover - a machine with none of the directories
        pytest.skip("no standard tool dir exists here, so nothing is added to inherit")

    assert str(added[0]) in no_prompt_env()["PATH"]
