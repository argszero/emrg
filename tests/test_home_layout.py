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


# ── the register travels with the trees it names ──────────────────────
#
# A register stores the verbatim directory a session runs in, so moving a tree without its
# row leaves an agent pointing at a directory that no longer exists: the row is well-formed,
# the tree is intact, and only a reader that stats the path finds the disagreement. These
# tests hold the rewrite that closes it — and the boundaries that keep it from reaching rows
# it has no business touching.


def _register(root: Path, rows: list[dict]) -> Path:
    """A register at ``root``'s top level, the shape a pre-migration host has."""
    import yaml

    path = root / home_layout.AGENTS_FILENAME
    path.write_text(yaml.safe_dump(rows), encoding="utf-8")
    return path


def _rows(path: Path) -> dict[str, str]:
    import yaml

    return {row["name"]: row["path"] for row in yaml.safe_load(path.read_text(encoding="utf-8"))}


def test_an_agent_under_a_moved_tree_still_names_a_directory_that_exists(tmp_path):
    """The defect: after the move the row pointed at the pre-split path, which is gone."""
    _tree(tmp_path, "evolution/", "config.toml")
    agent = tmp_path / "evolution"
    _register(tmp_path, [{"path": str(agent), "name": "emrg", "life": 145}])

    home_layout.migrate_into_global(tmp_path)

    rows = _rows(tmp_path / "global" / home_layout.AGENTS_FILENAME)
    assert rows["emrg"] == str(tmp_path / "global" / "evolution")
    assert Path(rows["emrg"]).is_dir(), "the row must name a directory that is really there"


def test_a_row_for_the_root_itself_is_left_alone(tmp_path):
    """The home root is a registered agent and it does not move, so neither does its row."""
    _tree(tmp_path, "evolution/")
    _register(tmp_path, [
        {"path": str(tmp_path), "name": ".emrg", "life": 120},
        {"path": str(tmp_path / "evolution"), "name": "emrg", "life": 145},
    ])

    home_layout.migrate_into_global(tmp_path)

    rows = _rows(tmp_path / "global" / home_layout.AGENTS_FILENAME)
    assert rows[".emrg"] == str(tmp_path), "the root stayed, so its row must too"
    assert rows["emrg"] == str(tmp_path / "global" / "evolution")


def test_a_row_outside_the_root_is_left_alone(tmp_path, tmp_path_factory):
    """A register holds directories anywhere; a rewrite bounded to the move must miss them."""
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    _tree(tmp_path, "evolution/")
    _register(tmp_path, [{"path": str(elsewhere), "name": "other", "life": 5}])

    home_layout.migrate_into_global(tmp_path)

    rows = _rows(tmp_path / "global" / home_layout.AGENTS_FILENAME)
    assert rows["other"] == str(elsewhere)


def test_a_row_for_a_look_alike_name_is_not_rewritten(tmp_path):
    """``evolution-old`` is not inside ``evolution``; a prefix test that says it is is wrong."""
    _tree(tmp_path, "evolution/")
    _register(tmp_path, [
        {"path": str(tmp_path / "evolution-old"), "name": "older", "life": 9},
    ])

    home_layout.migrate_into_global(tmp_path)

    rows = _rows(tmp_path / "global" / home_layout.AGENTS_FILENAME)
    assert rows["older"] == str(tmp_path / "evolution-old")


def test_a_tree_set_aside_has_its_row_follow_it_too(tmp_path):
    """An entry whose name ``global/`` already holds lands beside it, and the row follows."""
    _tree(tmp_path, "evolution/")
    (tmp_path / "global" / "evolution").mkdir(parents=True)
    _register(tmp_path, [{"path": str(tmp_path / "evolution"), "name": "emrg", "life": 1}])

    home_layout.migrate_into_global(tmp_path)

    rows = _rows(tmp_path / "global" / home_layout.AGENTS_FILENAME)
    assert rows["emrg"] == str(tmp_path / "global" / f"evolution{home_layout.SET_ASIDE_SUFFIX}")
    assert Path(rows["emrg"]).is_dir()


def test_an_unreadable_register_is_left_exactly_as_it_was(tmp_path):
    """It could not be read, so its rows are unknown; rewriting it would delete them all."""
    _tree(tmp_path, "evolution/")
    path = tmp_path / home_layout.AGENTS_FILENAME
    path.write_bytes(b"\xff\xfe not yaml at all\n")

    moved = home_layout.migrate_into_global(tmp_path)

    assert sorted(moved) == ["agents.yml", "evolution"], "the move itself still happened"
    assert (tmp_path / "global" / home_layout.AGENTS_FILENAME).read_bytes() == (
        b"\xff\xfe not yaml at all\n"
    ), "a register this call could not read must come back byte for byte"


def test_a_second_pass_rewrites_nothing(tmp_path):
    """Repeatable: the rows already name their new homes, so nothing matches."""
    _tree(tmp_path, "evolution/")
    _register(tmp_path, [{"path": str(tmp_path / "evolution"), "name": "emrg", "life": 1}])
    home_layout.migrate_into_global(tmp_path)
    after_first = (tmp_path / "global" / home_layout.AGENTS_FILENAME).read_bytes()

    assert home_layout.migrate_into_global(tmp_path) == []
    assert (tmp_path / "global" / home_layout.AGENTS_FILENAME).read_bytes() == after_first


def test_the_register_is_found_at_the_top_level_when_it_did_not_move(tmp_path):
    """``register_after_migration`` reads where the file is, not where it should be."""
    inside = tmp_path / "global" / home_layout.AGENTS_FILENAME
    assert home_layout.register_after_migration(tmp_path) == tmp_path / home_layout.AGENTS_FILENAME
    inside.parent.mkdir(parents=True)
    inside.write_text("[]", encoding="utf-8")
    assert home_layout.register_after_migration(tmp_path) == inside


# --------------------------------------------------------------------------- #
# The split must not move the tree it is running from (issue #3143)
#
# Measured on a host that upgraded across the split: the first run of the new version
# renamed `install/` — which holds the executing source and the packaged interpreter —
# and then died inside the call that renamed it, with two `ModuleNotFoundError`s. The
# register rows that pass was meant to carry were never rewritten, the `PATH` anchor kept
# naming the moved directory, and the repair was done by hand.
#
# Every test hands the migration a root of its own; none of them touches the real home.
# --------------------------------------------------------------------------- #


def test_a_live_tree_is_never_moved_out_from_under_the_process(tmp_path, monkeypatch):
    """The acceptance that discriminates: a module **not imported before** the call.

    A test that re-imports an already-loaded module passes against the broken code,
    because the module object is in `sys.modules` and no path is consulted. The import
    below is new, so it has to find the source tree on disk — which is exactly what the
    rename took away.
    """
    import importlib
    import sys

    install = tmp_path / "install"
    source = install / "source"
    package = source / "probe_live_tree_3143"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "already_in.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "never_in.py").write_text("VALUE = 2\n", encoding="utf-8")
    _tree(tmp_path, "config.toml")

    monkeypatch.syspath_prepend(str(source))
    importlib.import_module("probe_live_tree_3143.already_in")
    # This process "runs from" the tree: the anchors are the seam that says so.
    monkeypatch.setattr(
        home_layout, "running_tree_anchors", lambda: [package / "__init__.py"]
    )
    try:
        moved = home_layout.migrate_into_global(tmp_path)

        assert "install" not in moved, (
            "the migration moved the tree this process executes from - every path the "
            "process holds now names a directory that is gone"
        )
        assert (tmp_path / "install").is_dir(), "the live tree must still be where it was"
        assert (tmp_path / "global" / "config.toml").exists(), (
            "the entries that are not the live tree must still have moved"
        )
        module = importlib.import_module("probe_live_tree_3143.never_in")
        assert module.VALUE == 2, "a module imported after the migration must still load"
    finally:
        for name in [n for n in sys.modules if n.startswith("probe_live_tree_3143")]:
            del sys.modules[name]


def test_a_second_pass_over_a_live_root_moves_nothing(tmp_path, monkeypatch):
    """Repeatable with the live tree present: the skip must not turn into a pending pair
    on the next start, which would rename the tree one run later instead of never."""
    install = tmp_path / "install"
    (install / "source").mkdir(parents=True)
    _tree(tmp_path, "config.toml")
    monkeypatch.setattr(home_layout, "running_tree_anchors", lambda: [install / "source"])

    first = home_layout.migrate_into_global(tmp_path)
    second = home_layout.migrate_into_global(tmp_path)

    assert first == ["config.toml"], first
    assert second == [], second
    assert install.is_dir()


def test_a_register_heals_on_a_start_that_moves_nothing(tmp_path):
    """The repair is not a stage of the move.

    The pass that should have carried the rows imports the register, so the migration that
    breaks the import graph is the one that skips the rewrite — a root already in that
    state has nothing left to move and would never be visited again.
    """
    (tmp_path / "global" / "evolution" / "emrg").mkdir(parents=True)
    _register(tmp_path / "global", [
        {"path": str(tmp_path / "evolution" / "emrg"), "name": "emrg", "life": 145},
    ])

    assert home_layout.pending_moves(tmp_path) == [], "nothing left to move is the premise"
    moved = home_layout.migrate_into_global(tmp_path)

    assert moved == []
    rows = _rows(tmp_path / "global" / home_layout.AGENTS_FILENAME)
    assert rows["emrg"] == str(tmp_path / "global" / "evolution" / "emrg"), (
        "a row naming a directory the split moved must be repaired by a start that moves "
        "nothing"
    )


def test_a_repair_never_moves_a_row_to_a_directory_that_is_not_there(tmp_path):
    """The property that makes the repair safe to run on every start.

    A row whose directory is gone and whose post-split twin is *also* absent must be left
    exactly as it was: rewriting it would replace one dead path with another and lose the
    only evidence that something moved. Rows outside this root and the root's own row are
    not this repair's subjects at all.
    """
    (tmp_path / "global" / "evolution").mkdir(parents=True)
    outside = tmp_path.parent / "somewhere-else-3143"
    vanished = tmp_path / "vanished" / "agent"
    _register(tmp_path / "global", [
        {"path": str(outside), "name": "elsewhere"},
        {"path": str(vanished), "name": "vanished"},
        {"path": str(tmp_path / "global" / "gone"), "name": "gone"},
        {"path": str(tmp_path), "name": ".emrg"},
    ])

    home_layout.reconcile_register_paths(tmp_path)

    rows = _rows(tmp_path / "global" / home_layout.AGENTS_FILENAME)
    assert rows["elsewhere"] == str(outside), "a row outside the root is not ours to move"
    assert not (tmp_path / "global" / "vanished" / "agent").exists()
    assert rows["vanished"] == str(vanished), (
        "the row's directory is gone and its post-split twin is gone too, so the row must "
        "stay exactly as it was - rewriting it would trade one dead path for another and "
        "erase the evidence that something moved"
    )
    assert rows["gone"] == str(tmp_path / "global" / "gone"), (
        "a row already under global/ whose directory is missing is not the split's doing"
    )
    assert rows[".emrg"] == str(tmp_path), "the root does not move, so neither does its row"


def test_the_path_anchor_follows_the_move_and_leaves_the_hosts_own_line_alone(tmp_path):
    """Only the installer's block is rewritten, and only once."""
    rc = tmp_path / ".zshrc"
    host_line = 'export PATH="$HOME/bin:$PATH"\n'
    rc.write_text(
        host_line
        + home_layout.PATH_ANCHOR_START + "\n"
        + 'export PATH="$HOME/.emrg/install/bin:$PATH"\n'
        + home_layout.PATH_ANCHOR_END + "\n",
        encoding="utf-8",
    )

    changed = home_layout.rewrite_path_anchors(tmp_path)

    text = rc.read_text(encoding="utf-8")
    assert changed == [".zshrc"], changed
    assert 'export PATH="$HOME/.emrg/global/install/bin:$PATH"' in text, (
        "an upgraded host kept an anchor naming the directory the migration moved, so its "
        "next shell answered 'emrg: command not found'"
    )
    assert host_line in text, "a host's own PATH line is not ours to edit"
    assert text.count(home_layout.PATH_ANCHOR_START) == 1
    assert home_layout.rewrite_path_anchors(tmp_path) == [], "a second run changes nothing"


def test_an_anchor_outside_the_block_is_not_rewritten(tmp_path):
    """The control for the test above: the same spelling outside the markers is the
    host's, and the boundary is the markers rather than the text."""
    rc = tmp_path / ".bashrc"
    stray = 'alias e="PATH=$HOME/.emrg/install/bin:$PATH"\n'
    rc.write_text(stray, encoding="utf-8")

    assert home_layout.rewrite_path_anchors(tmp_path) == []
    assert rc.read_text(encoding="utf-8") == stray


def test_a_stale_legacy_tree_is_filed_aside_when_the_live_one_is_already_split(
    tmp_path, monkeypatch
):
    """The measured upgrade case, with the process running from the new root: the old
    tree moves aside rather than replacing anything, and nothing is deleted."""
    _tree(tmp_path, "install/")
    live = tmp_path / "global" / "install"
    live.mkdir(parents=True)
    monkeypatch.setattr(home_layout, "running_tree_anchors", lambda: [live / "bin" / "emrg"])

    home_layout.migrate_into_global(tmp_path)

    aside = tmp_path / "global" / f"install{home_layout.SET_ASIDE_SUFFIX}"
    assert aside.is_dir(), "the stale legacy tree must be kept, under the set-aside suffix"
    assert live.is_dir(), "the live tree must not be replaced"
    assert not (tmp_path / "install").exists(), "and nothing may be left at the top level"
