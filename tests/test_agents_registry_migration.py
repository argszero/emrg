"""The agent register's rename: `projects.yml` → `agents.yml` (#2521).

Until an Agent became the record of the directory a session runs in, the
installation's register of those directories was `~/.emrg/projects.yml`. The
content does not change with the name — the same entries, the same fields, the
same `last_active` — so what these tests hold is the *transition*: one move,
whole, once, and never from a reader that only asked where the file lives.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
import yaml

from emrg.config import agents_file, migrate_legacy_registry

#: A register with what the daemon cares about: a `name`/`path` pair, and the
#: `last_active` stamp the pickers sort by. The second entry carries a custom
#: field, because entries are the host's to extend.
REGISTER = [
    {"name": "emrg", "path": "/home/h/evolution/emrg", "last_active": "2026-10-23T13:00:00"},
    {"name": "aitokenpool", "path": "/home/h/aitokenpool", "last_active": "2026-10-22T09:12:04",
     "notes": "host-added field"},
]


def _legacy(directory: Path) -> Path:
    """Write the pre-rename register. The one place a test spells the old name."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "projects.yml"
    path.write_text(yaml.safe_dump(REGISTER, allow_unicode=True), encoding="utf-8")
    return path


def test_the_move_carries_every_entry_and_its_last_active(tmp_path):
    legacy = _legacy(tmp_path)

    assert migrate_legacy_registry(tmp_path) is True

    moved = tmp_path / "agents.yml"
    assert moved.exists(), "the register did not arrive at its new name"
    assert not legacy.exists(), "the move copied instead of moving — two registers now"
    assert yaml.safe_load(moved.read_text(encoding="utf-8")) == REGISTER


def test_a_register_already_at_the_new_name_is_never_moved(tmp_path):
    current = tmp_path / "agents.yml"
    current.write_text(yaml.safe_dump([{"name": "mine", "path": "/one"}]), encoding="utf-8")
    # An older daemon re-created the legacy name; the new name governs and must
    # not be overwritten by it.
    _legacy(tmp_path)

    assert migrate_legacy_registry(tmp_path) is False

    assert yaml.safe_load(current.read_text(encoding="utf-8")) == [
        {"name": "mine", "path": "/one"}
    ]


def test_a_register_that_never_existed_is_not_created(tmp_path):
    assert migrate_legacy_registry(tmp_path) is False
    assert not (tmp_path / "agents.yml").exists(), (
        "a read of an installation with no register must not materialise an empty one"
    )


def test_a_move_that_cannot_happen_leaves_the_legacy_register_readable(tmp_path, monkeypatch):
    legacy = _legacy(tmp_path)

    def _refuse(self, target):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "rename", _refuse)

    assert migrate_legacy_registry(tmp_path) is False, "a failed move is not a move"

    assert legacy.exists(), "the register was lost on a failed move"
    assert yaml.safe_load(legacy.read_text(encoding="utf-8")) == REGISTER


def test_a_second_caller_finds_the_work_done(tmp_path):
    _legacy(tmp_path)

    assert migrate_legacy_registry(tmp_path) is True
    assert migrate_legacy_registry(tmp_path) is False

    assert yaml.safe_load((tmp_path / "agents.yml").read_text(encoding="utf-8")) == REGISTER


def test_asking_for_the_path_never_moves_the_register(tmp_path):
    """The producer answers a path and opens nothing.

    This is the separation that keeps a test run from renaming the host's
    register under a daemon that is still reading the old name: the suite
    constructs every reader of the register, so a move hidden in the path
    derivation would run during a suite.
    """
    legacy = _legacy(tmp_path)

    assert agents_file(tmp_path) == tmp_path / "agents.yml"

    assert legacy.exists(), "asking where the register lives moved it"
    assert not (tmp_path / "agents.yml").exists()


def test_the_daemon_entrypoint_migrates_before_it_serves(monkeypatch):
    """The one process that owns the register moves it, before the server exists.

    `main()` is called with every side effect stubbed — the shape
    `test_daemon_start_diagnostics` uses — so nothing starts, stops or restarts a
    daemon and no host file is opened. What is asserted is the order: the move
    runs, and it runs before the server object that asks for the path.
    """
    from emrg.server import __main__ as server_main

    calls: list[str] = []

    monkeypatch.setattr(server_main, "ensure_tool_dirs", lambda: [])
    monkeypatch.setattr(server_main, "_configure_logging", lambda: None)
    monkeypatch.setattr(server_main, "_redirect_std_streams", lambda: None)
    monkeypatch.setattr(
        server_main,
        "migrate_legacy_registry",
        lambda *a, **k: calls.append("migrate") or True,
    )
    monkeypatch.setattr(
        "emrg.config.load_config",
        lambda: types.SimpleNamespace(llm=None),
    )

    class _Exit:
        exit_code = 0

        def write_record(self) -> None:
            pass

    async def _run_server(llm):
        calls.append("serve")
        return _Exit()

    monkeypatch.setattr(server_main, "run_server", _run_server)

    with pytest.raises(SystemExit):
        server_main.main()

    assert calls == ["migrate", "serve"], calls


def test_the_old_name_is_spelled_only_where_the_move_is():
    """One producer: the register's name lives in one module.

    A reading of the source, not of behaviour, because the question is exactly
    "how many places spell this name" — and a second spelling is what a rename
    leaves behind. `emrg/config.py` is the move itself; every other file asks.

    One carrier is exempt, and it is enumerated rather than matched: the rotation
    script runs stdlib-only, so it cannot ask `emrg.config`, and it must keep
    answering while a checkout is ahead of the daemon that performs the move
    (`agents.yml` travels in a release, and installing one is what restarts that
    daemon). Its exemption is checked to be live below — an exemption that no
    longer names anything fails, so this list cannot rot into a permission.
    """
    root = Path(__file__).resolve().parent.parent
    exemptions = ("scripts/check-memory-rotation.py",)
    for name in exemptions:
        text = (root / name).read_text(encoding="utf-8", errors="replace")
        assert "projects.yml" in text, (
            f"{name} is exempted from the one-name rule but no longer names the old "
            f"name — drop it from `exemptions` rather than leaving a permission"
        )
    offenders: list[str] = []
    for area in ("emrg", "scripts"):
        for path in (root / area).rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            if path == root / "emrg" / "config.py" or relative in exemptions:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "projects.yml" in text:
                offenders.append(f"{relative}: old name")
            if "agents.yml" in text and "config_dir()" in text:
                # A path built from the config directory again, rather than asked for.
                for line in text.splitlines():
                    if "agents.yml" in line and "config_dir()" in line:
                        offenders.append(f"{relative}: {line.strip()}")
    assert not offenders, "the register's path is built outside its one producer:\n" + "\n".join(
        offenders
    )
