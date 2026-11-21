"""The home-level ``.emrg`` root: one directory for EMRG's data, one for an agent's state.

The root held two things under one name — EMRG's own data (config, register, install tree,
logs) and the state of a session started with the home directory as its working directory,
because that state lives in ``<cwd>/.emrg``. The split is a directory, not a judgement: what
the ``<cwd>/.emrg`` rule produces stays where that rule put it, and everything else moves
into ``global/``.

What these tests hold is the migration's contract, because it is the one call here that
moves a host's files: it is repeatable, it never overwrites, it leaves nothing at the top
level, and a half-migration is undone rather than kept. Every test hands it a directory of
its own — the migration is the subject, so the real home must never be one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from emrg import home_layout


def _tree(root: Path, *names: str) -> None:
    """Create ``names`` at ``root``'s top level, each holding one file of its own."""
    for name in names:
        entry = root / name
        if name.endswith("/"):
            entry.mkdir(parents=True, exist_ok=True)
            (entry / f"{entry.name}.txt").write_text(name, encoding="utf-8")
        else:
            entry.write_text(name, encoding="utf-8")


def test_the_two_roots_hang_off_the_home_directory():
    """Pure predicates: ``global/`` is the one directory EMRG owns under the root."""
    assert home_layout.home_root().name == ".emrg"
    assert home_layout.global_dir() == home_layout.home_root() / "global"


def test_an_agent_state_entry_keeps_its_place(tmp_path):
    """``sessions/``, ``memory/`` and the client log are the ``<cwd>/.emrg`` rule's own."""
    _tree(tmp_path, "sessions/", "memory/", "emrg-client.log", "config.toml")

    pending = home_layout.pending_moves(tmp_path)

    assert [source.name for source, _target in pending] == ["config.toml"]


def test_an_entry_nobody_recognises_moves_too(tmp_path):
    """There is no allow-list to fall off: an unknown entry is EMRG's and it moves."""
    _tree(tmp_path, "tmp_usage.json", "main.js", "git-backup/")

    moved = home_layout.migrate_into_global(tmp_path)

    assert sorted(moved) == ["git-backup", "main.js", "tmp_usage.json"]
    assert sorted(p.name for p in (tmp_path / "global").iterdir()) == [
        "git-backup", "main.js", "tmp_usage.json",
    ]


def test_the_top_level_keeps_only_global_and_the_agent_state(tmp_path):
    """The state the split exists to end: loose global entries sitting at the top level."""
    _tree(tmp_path, "sessions/", "memory/", "emrg-client.log",
          "config.toml", "agents.yml", "tasks.yml", "install/", "logs/", "rants.jsonl")

    home_layout.migrate_into_global(tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        ["global", "sessions", "memory", "emrg-client.log"]
    )
    assert (tmp_path / "global" / "config.toml").read_text(encoding="utf-8") == "config.toml"


def test_a_name_global_already_holds_is_set_aside_inside_global(tmp_path):
    """The upgrade case: the new installer wrote ``global/install`` while the old tree is
    still at ``install``. Nothing is deleted, nothing is overwritten, and the old tree does
    not stay at the top level."""
    (tmp_path / "global" / "install").mkdir(parents=True)
    (tmp_path / "global" / "install" / "new.txt").write_text("new", encoding="utf-8")
    (tmp_path / "install").mkdir()
    (tmp_path / "install" / "old.txt").write_text("old", encoding="utf-8")

    pending = home_layout.pending_moves(tmp_path)
    assert [(s.name, t.name) for s, t in pending] == [("install", "install.pre-global")]

    home_layout.migrate_into_global(tmp_path)

    assert (tmp_path / "global" / "install" / "new.txt").read_text(encoding="utf-8") == "new"
    assert (tmp_path / "global" / "install.pre-global" / "old.txt").read_text(
        encoding="utf-8") == "old"
    assert not (tmp_path / "install").exists()


def test_a_second_set_aside_entry_does_not_reuse_the_first_name(tmp_path):
    """Repeatability across upgrades: the set-aside name is taken, so the next one moves
    one suffix further instead of replacing the tree already there."""
    (tmp_path / "global" / "install.pre-global").mkdir(parents=True)
    (tmp_path / "global" / "install").mkdir()
    (tmp_path / "install").mkdir()

    pending = home_layout.pending_moves(tmp_path)

    assert [target.name for _source, target in pending] == ["install.pre-global-2"]


def test_the_migration_is_repeatable(tmp_path):
    """A second call finds nothing left to move, which is what makes it safe to run at
    every start."""
    _tree(tmp_path, "config.toml", "install/")

    first = home_layout.migrate_into_global(tmp_path)
    second = home_layout.migrate_into_global(tmp_path)

    assert sorted(first) == ["config.toml", "install"]
    assert second == []
    assert home_layout.pending_moves(tmp_path) == []


def test_a_failed_move_puts_back_what_this_call_moved(tmp_path, monkeypatch):
    """All or nothing: a half-migrated root leaves some readers on the old path and some
    on the new, which is worse than an unmigrated one."""
    _tree(tmp_path, "a.yml", "b.yml", "c.yml")
    real_move = shutil.move

    def flaky(source, target):
        if Path(source).name == "c.yml":
            raise OSError("disk full")
        return real_move(source, target)

    monkeypatch.setattr(home_layout.shutil, "move", flaky)

    assert home_layout.migrate_into_global(tmp_path) == []
    for name in ("a.yml", "b.yml", "c.yml"):
        assert (tmp_path / name).read_text(encoding="utf-8") == name
    assert list((tmp_path / "global").iterdir()) == []


def test_the_migration_runs_for_the_daemon_and_not_for_stop_or_update(monkeypatch):
    """The mutation this holds: stop moving an old installation's entries and this reds.

    ``stop`` and ``update`` are the paths an installation is repaired or replaced through,
    so a move underneath them would be a second change while the first is in flight.
    """
    from emrg import __main__ as cli

    calls: list[str] = []

    def spy() -> list[str]:
        calls.append("moved")
        return []

    monkeypatch.setattr(home_layout, "migrate_into_global", spy)

    for command in (None, "server"):
        cli._migrate_home_root(command)
    assert calls == ["moved", "moved"]

    for command in ("stop", "update", "rant"):
        cli._migrate_home_root(command)
    assert calls == ["moved", "moved"], "a command that does not own the root must not move it"


@pytest.mark.parametrize("name", ["global", "sessions", "memory", "emrg-client.log"])
def test_a_name_the_root_owns_is_never_a_migration_subject(tmp_path, name):
    """``global/`` itself, and the three entries the ``<cwd>/.emrg`` rule produces."""
    if name == "global":
        (tmp_path / name).mkdir()
        (tmp_path / name / "config.toml").write_text("x", encoding="utf-8")
    else:
        _tree(tmp_path, name)

    assert home_layout.pending_moves(tmp_path) == []


def test_a_host_log_directory_lands_where_its_reader_looks(tmp_path):
    """The ordering premise, as a behaviour rather than a comment.

    `global/logs/` is the daemon's log directory, and the logging setup *creates* it. If
    that happens before the move, the host's pre-split `logs/` at the top level finds the
    name taken and is set aside as `logs.pre-global` — its reader then looks at an empty
    directory while the host's own logs sit one name over. So a run that migrates first is
    the only run in which `global/logs/` receives them.
    """
    _tree(tmp_path, "logs/")
    assert not (tmp_path / "global" / "logs").exists()

    home_layout.migrate_into_global(tmp_path)

    assert (tmp_path / "global" / "logs" / "logs.txt").is_file()
    assert not (tmp_path / "global" / "logs.pre-global").exists()


def test_the_daemon_entry_point_runs_the_migration_first():
    """Every launch path ends at `python -m emrg.server`, so the migration has to be there.

    Not a restatement of the CLI's call: the GUI spawns this entry point directly
    (`emrg/gui/daemon_client.js` — `["-m", "emrg.server"]`) and so does the TUI client
    (`emrg/client/daemon_manager.py`), with no `emrg` command in between. Without the call
    here, an upgraded host's daemon reads `global/config.toml`, finds the config still at
    its pre-split path, and `load_config()` raises — the daemon exits before binding, and
    every client reports a server that will not start. Measured: CI run 44860021369, the
    `test` leg's GUI integration suite, seven tests on `daemon token file timeout`.

    A source reading rather than a drive of `main()`, and the reason is a red line: the
    function below the call migrates the host's register, task table and rant ledger, then
    starts a real server. Driving it would touch a live host's files to prove an ordering.
    What is asserted is the call itself and that it precedes the logging setup that would
    otherwise create `global/logs/` first — the two facts that failed.
    """
    from emrg.server import __main__ as daemon_entry

    source = Path(daemon_entry.__file__).read_text(encoding="utf-8")
    main_body = source.split("def main() -> None:", 1)[1]

    # Statement lines, not any occurrence: the first draft of this guard compared raw
    # offsets and read the *comment* above the call — which names `_configure_logging()`
    # while explaining why it comes second — so it reported the call after the setup when
    # the code had it before. A subject that cannot tell a call from prose about a call
    # answers for neither.
    statements = [line.strip() for line in main_body.splitlines()
                  if not line.strip().startswith("#")]

    def first_statement(call: str) -> int:
        for position, line in enumerate(statements):
            if line == call:
                return position
        return -1

    migrate_at = first_statement("migrate_into_global()")
    logging_at = first_statement("_configure_logging()")

    assert migrate_at != -1, (
        "the daemon entry point does not call migrate_into_global() — every client spawns "
        "this module, so an upgraded host's daemon would find no config and exit"
    )
    assert logging_at != -1, "the logging setup is not called from main() any more"
    assert migrate_at < logging_at, (
        "the migration runs after the logging setup, which creates global/logs/ first — so "
        "a host's pre-split logs/ would be set aside as logs.pre-global instead of moved"
    )
