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

A named platform must be that platform, on every host
-----------------------------------------------------
The behaviour tests name the platform they are about, so they must be able to
name its *paths* too, and that is what `PurePosixPath` / `PureWindowsPath` are
for. This file learned it the hard way: its first version passed `platform="linux"`
with a `tmp_path` home and let `tool_dirs` build the absolute prefixes with
`Path`, so on `test-windows` the expected spellings were POSIX and the actual
ones were `\\opt\\homebrew\\bin` — a path that exists on neither platform, and
which the injected `isdir` therefore never matched. Three tests passed on macOS
and failed on Windows for that reason alone. `tool_path.path_flavour` is the
fix, and `test_a_named_platform_spells_its_own_paths` is the guard that keeps it.

⚠️ Nothing here starts, stops or restarts a daemon (MANIFESTO 第四条附则二), and
nothing opens `~/.emrg/config.toml` or `~/.emrg/logs/emrgd.log`: the two entry tests
stub the logging setup, the config load and the server itself, so no process is
spawned and no host file is written. The directories that must exist on disk are
made under `tmp_path` and `Path.home` is pointed at them for the duration.

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
import os
import types
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from emrg import tool_path

#: A home in each platform's own flavour. The behaviour tests use these rather
#: than a real directory because `isdir` is injected: what is under test is the
#: augmentation's arithmetic, and it must not depend on what this host has
#: installed (`/opt/homebrew/bin` is here, absent on a Linux box).
POSIX_HOME = PurePosixPath("/home/host")
WINDOWS_HOME = PureWindowsPath(r"C:\Users\host")


def _isdir_except(missing: set[str]):
    """An ``isdir`` that says every directory exists except the named ones."""

    def exists(path) -> bool:
        return str(path) not in missing

    return exists


def _isdir_only(existing: str, *more: str):
    """An ``isdir`` that says **only** the named directories exist.

    The list is no longer three entries (issue #1783 added the version-manager
    shim directories), and on a machine like this one several of the absolute
    prefixes really do exist — so an ``isdir`` that defaults to "everything
    exists" makes an exact assertion about what was *added* unreadable, and one
    that defaults to "nothing exists" makes it pass for the wrong reason. This
    helper makes the fixture's world explicit.
    """
    present = {existing, *more}

    def exists(path) -> bool:
        return str(path) in present

    return exists


def _home_with(tmp_path: Path, *names: str) -> Path:
    home = tmp_path / "home"
    for name in names:
        (home / name).mkdir(parents=True, exist_ok=True)
    return home


def test_a_tool_dir_is_appended_after_the_inherited_path():
    """The host's own PATH keeps priority; ours is added behind it."""
    env = {"PATH": "/usr/bin:/bin"}

    # Only `~/.local/bin` exists: the absolute prefixes are the machine's fact,
    # and naming them missing keeps this assertion about the augmentation itself.
    added = tool_path.augment_path(
        env, platform="linux", home=POSIX_HOME,
        isdir=_isdir_only("/home/host/.local/bin"),
    )

    assert [str(d) for d in added] == ["/home/host/.local/bin"]
    assert env["PATH"] == "/usr/bin:/bin:/home/host/.local/bin"


def test_a_directory_that_does_not_exist_is_not_added():
    """A healthy environment is left byte-identical — no empty entries, no invention."""
    env = {"PATH": "/usr/bin:/bin"}
    every_prefix = {str(d) for d in tool_path.tool_dirs("linux", POSIX_HOME)}

    added = tool_path.augment_path(
        env, platform="linux", home=POSIX_HOME, isdir=_isdir_except(every_prefix)
    )

    assert added == []
    assert env["PATH"] == "/usr/bin:/bin"


def test_a_directory_already_on_the_path_is_not_added_again():
    """A trailing separator is the same directory, not a second one."""
    env = {"PATH": "/home/host/.local/bin/:/usr/bin"}

    added = tool_path.augment_path(
        env, platform="linux", home=POSIX_HOME,
        isdir=_isdir_only("/home/host/.local/bin"),
    )

    assert added == []
    assert env["PATH"].count("/home/host/.local/bin") == 1


def test_only_the_prefixes_that_exist_are_offered():
    """The absolute prefixes are candidates, not assertions about the machine."""
    offered = {str(d) for d in tool_path.tool_dirs("linux", POSIX_HOME)}
    assert offered == {
        "/home/host/.local/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/home/host/.asdf/shims",
        "/home/host/.mise/shims",
        "/home/host/.local/share/mise/shims",
        "/home/host/.pyenv/shims",
        "/home/host/.volta/bin",
        "/home/host/.bun/bin",
        "/home/host/.cargo/bin",
        "/home/host/Library/pnpm",
        "/home/host/.local/share/pnpm",
    }

    env = {"PATH": "/usr/bin"}
    added = tool_path.augment_path(
        env, platform="linux", home=POSIX_HOME,
        isdir=_isdir_only("/home/host/.local/bin", "/usr/local/bin"),
    )

    assert [str(d) for d in added] == ["/home/host/.local/bin", "/usr/local/bin"]


def test_windows_gets_the_windows_list_and_a_semicolon():
    """`emrgd.cmd` needs no second copy of this logic — the entry normalizes it."""
    assert [str(d) for d in tool_path.tool_dirs("win32", WINDOWS_HOME)] == [
        r"C:\Users\host\.local\bin"
    ]

    inherited = r"C:\Windows\system32"
    env = {"PATH": inherited}
    added = tool_path.augment_path(env, platform="win32", home=WINDOWS_HOME, isdir=lambda _p: True)

    assert [str(d) for d in added] == [r"C:\Users\host\.local\bin"]
    # A Windows PATH is joined with ";", even when this host is POSIX.
    assert env["PATH"] == r"C:\Windows\system32;C:\Users\host\.local\bin"

    # And the same directory in another spelling is the same directory: an
    # installer that writes `C:\Users\Host\` where the registry holds
    # `c:\users\host` must not produce a second entry.
    env = {"PATH": r"c:\users\host\.local\bin;C:\Windows\system32"}
    assert tool_path.augment_path(
        env, platform="win32", home=WINDOWS_HOME, isdir=lambda _p: True
    ) == []


def test_a_named_platform_spells_its_own_paths():
    """The guard for the defect this file shipped with: flavour follows `platform`.

    Measured on CI 2026-09-28 — the three behaviour tests above passed on macOS
    and failed on `test-windows`, because `tool_dirs(platform="linux")` built its
    absolute prefixes with the *running* interpreter's `Path` and answered
    `\\opt\\homebrew\\bin`. Both spellings below are asserted on every host, which
    is the only way a cross-platform claim can be checked from one machine.
    """
    linux = [str(d) for d in tool_path.tool_dirs("linux", POSIX_HOME)]
    assert linux == [
        "/home/host/.local/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/home/host/.asdf/shims",
        "/home/host/.mise/shims",
        "/home/host/.local/share/mise/shims",
        "/home/host/.pyenv/shims",
        "/home/host/.volta/bin",
        "/home/host/.bun/bin",
        "/home/host/.cargo/bin",
        "/home/host/Library/pnpm",
        "/home/host/.local/share/pnpm",
    ]

    windows = [str(d) for d in tool_path.tool_dirs("win32", WINDOWS_HOME)]
    assert windows == [r"C:\Users\host\.local\bin"]

    # The separators follow the same argument, so the two halves cannot disagree.
    assert tool_path.path_separator("linux") == ":"
    assert tool_path.path_separator("win32") == ";"
    assert tool_path.path_flavour("linux") is PurePosixPath
    assert tool_path.path_flavour("win32") is PureWindowsPath


def test_a_version_manager_shim_dir_is_a_candidate():
    """The list is not installer-only (issue #1783).

    Measured 2026-09-30: a daemon started from the Dock got launchd's minimal
    `PATH` plus exactly the three installer directories, while `~/.asdf/shims`
    on the same host holds 76 executables — `node`, `npm`, `python3`, `cargo`,
    `java` among them. A terminal start saw all of them and a Dock start saw
    none, which is the difference this module exists to remove.

    Each name is asserted rather than the list's length, because the failure
    that mattered was an *absent* entry and a count cannot see which one.
    """
    offered = {str(d) for d in tool_path.tool_dirs("linux", POSIX_HOME)}
    for name in (
        ".asdf/shims",
        ".mise/shims",
        ".local/share/mise/shims",
        ".pyenv/shims",
        ".volta/bin",
        ".bun/bin",
        ".cargo/bin",
        "Library/pnpm",
        ".local/share/pnpm",
    ):
        assert f"/home/host/{name}" in offered, f"{name} is not a candidate directory"


def test_a_listed_tool_dir_is_appended_against_a_real_filesystem(tmp_path):
    """The mechanism, measured on the real `isdir` rather than an injected one.

    Two things this covers that no test above does: the directory really exists
    on disk (so the existence branch is exercised by the filesystem, not by a
    stub), and the appended entry is the one a minimal inherited `PATH` gains —
    the Dock-start shape `~/.emrg/emrgd.log` recorded, where the daemon reached
    only launchd's directories.

    **The subject is taken from `tool_dirs()` and the platform is the running
    one, and both are deliberate.** The list is per-platform (Windows offers
    `~/.local/bin` alone), and `home` is a real `tmp_path` in the host's flavour,
    so naming a platform here would join it with the wrong flavour's separator —
    green on macOS and red on `test-windows`, which is the defect this file's own
    docstring records and which this test shipped with once. What the *list*
    contains is asserted by name in `test_a_version_manager_shim_dir_is_a_candidate`;
    what this asserts is the arithmetic applied to whatever the list offers.
    """
    home = tmp_path / "home"
    offered = tool_path.tool_dirs(home=home)
    under_home = [d for d in offered if str(d).startswith(str(home))]
    assert under_home, "the platform's list must offer a directory under the home"
    target = under_home[0]
    Path(target).mkdir(parents=True)

    inherited = tool_path.path_separator().join(["/usr/bin", "/bin"])
    env = {"PATH": inherited}

    added = tool_path.augment_path(env, home=home)

    assert str(target) in [str(d) for d in added]
    assert env["PATH"].startswith(inherited), "the inherited PATH keeps priority"


def test_a_version_keyed_directory_is_documented_as_out_of_reach():
    """`nvm`'s layout cannot be a static entry — the gap is stated, not implied."""
    assert tool_path.VERSION_KEYED_TOOL_DIRS == ("~/.nvm/versions/node/<version>/bin",)
    offered = {str(d) for d in tool_path.tool_dirs("linux", POSIX_HOME)}
    assert not any("nvm" in path for path in offered)


def test_the_startup_helper_records_the_effective_path(monkeypatch, caplog, tmp_path):
    """One INFO line: the daemon's only statement of the PATH its tools will see."""
    home = _home_with(tmp_path, ".local/bin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    with caplog.at_level(logging.INFO, logger="emrg.tool_path"):
        added = tool_path.ensure_tool_dirs()

    # `.local/bin` is first in the list on every platform, so a directory the host
    # really has (a Homebrew prefix) may follow it but cannot precede it.
    assert str(added[0]) == str(home / ".local/bin")
    records = [r for r in caplog.records if r.name == "emrg.tool_path"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert "effective PATH" in message
    assert str(home / ".local/bin") in message


def test_the_startup_helper_records_the_path_when_nothing_was_added(monkeypatch, caplog, tmp_path):
    """'nothing added' is an answer too — that line is what makes a healthy start checkable."""
    # A home with every directory this platform offers, so the empty branch is
    # reachable on a machine that has none of them (a bare CI runner).
    home = _home_with(tmp_path, ".local/bin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    reachable = [str(d) for d in tool_path.tool_dirs() if os.path.isdir(str(d))]
    assert reachable, "the fixture home must offer at least one tool dir"
    monkeypatch.setenv("PATH", tool_path.path_separator().join(reachable))

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


def test_the_added_directory_is_in_the_environment_a_tool_child_inherits(monkeypatch, tmp_path):
    """The link that would make the rest moot: startup PATH → the tool child's env."""
    from emrg.server.git_utils import no_prompt_env

    home = _home_with(tmp_path, ".local/bin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    added = tool_path.ensure_tool_dirs()
    assert added, "the fixture home offers a tool dir, so nothing being added is the defect"

    assert str(added[0]) in no_prompt_env()["PATH"]
