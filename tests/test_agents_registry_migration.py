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

from emrg.config import agents_file, migrate_legacy_registry, register_for_reading

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
        server_main,
        "migrate_task_config_keys",
        lambda *a, **k: calls.append("migrate-tasks") or 0,
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

    assert calls == ["migrate", "migrate-tasks", "serve"], calls


def test_the_old_name_is_spelled_only_where_the_move_is():
    """One producer: the register's name lives in one module.

    A reading of the source, not of behaviour, because the question is exactly
    "how many places spell this name" — and a second spelling is what a rename
    leaves behind. `emrg/config.py` is the move itself; every other file asks.

    One carrier is exempt, and it is enumerated rather than matched: the rotation
    script runs stdlib-only, so it cannot ask `emrg.config`, and it must keep
    answering while a checkout is ahead of the daemon that performs the move
    (`projects.yml` travels in a release, and installing one is what restarts that
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
            if "projects.yml" in text and "config_dir()" in text:
                # A path built from the config directory again, rather than asked for.
                for line in text.splitlines():
                    if "projects.yml" in line and "config_dir()" in line:
                        offenders.append(f"{relative}: {line.strip()}")
    assert not offenders, "the register's path is built outside its one producer:\n" + "\n".join(
        offenders
    )


# ── The register read path (#2545) ────────────────────────────────────────────
#
# The move above is the *writer's* business. These pin the reader's: while the
# file is still at the old name — because the move cannot happen on this host,
# or because a checkout runs ahead of the daemon that performs it — every reader
# must read the register that is actually there, and must not answer "empty".


def test_a_register_at_the_old_name_is_read_by_every_reader(tmp_path, monkeypatch):
    """The measured defect, one reader per assertion.

    With only the pre-rename file present, each of these answered the empty
    answer — `[]`, `None`, `{}` — while the register held two entries: silent,
    because "nothing there" and "it is at the other name" read alike. The
    candidate list is the one with a consequence: `submit_rant`'s warning about a
    mis-spelled agent is gated on that list being non-empty, so it never fired.
    """
    from emrg.server.scheduler import _load_agent_config, _resolve_agent_path
    from emrg.tools.file_policy import protected_paths
    from emrg.tools.submit_rant_tool import SubmitRantTool

    legacy = _legacy(tmp_path)
    # Each module holds its own bound copy of `config_dir` (`from emrg.config
    # import config_dir`), so patching the definition does not rebind them — a
    # seam that patches only the library leaves the caller reading the host's
    # real register, which is host state in a test.
    monkeypatch.setattr("emrg.config.config_dir", lambda: tmp_path)
    monkeypatch.setattr("emrg.server.scheduler.config_dir", lambda: tmp_path)

    assert register_for_reading(tmp_path) == legacy

    assert sorted(SubmitRantTool()._registered_agent_names()) == ["aitokenpool", "emrg"]

    assert _resolve_agent_path("emrg") == "/home/h/evolution/emrg"

    entry = _load_agent_config("emrg", "/home/h/evolution/emrg")
    assert entry.get("name") == "emrg", entry
    assert entry.get("path") == "/home/h/evolution/emrg", entry

    assert str(legacy.resolve()) in protected_paths(), (
        "the register a sandboxed write must not touch is the one that is live"
    )


def test_the_new_name_governs_when_both_are_present(tmp_path):
    """A legacy file an older daemon re-created cannot shadow the register."""
    _legacy(tmp_path)
    current = tmp_path / "agents.yml"
    current.write_text(yaml.safe_dump([{"name": "only-new"}]), encoding="utf-8")

    assert register_for_reading(tmp_path) == current


def test_the_read_path_never_moves_the_register(tmp_path):
    """Asking a reader where the register is must not perform the migration."""
    legacy = _legacy(tmp_path)

    assert register_for_reading(tmp_path) == legacy

    assert legacy.exists(), "reading the register moved it"
    assert not (tmp_path / "agents.yml").exists()


def test_the_read_fallback_cannot_shadow_the_migration_target(tmp_path):
    """The reason the fallback is its own function and not part of `agents_file`.

    `migrate_legacy_registry` takes `agents_file()` as the move *target*. A
    fallback inside that function would hand it the legacy path as the target,
    `target.exists()` would be true, and the move would answer "already done" on
    every host for ever. Read the fallback first, then migrate, and the move
    still happens.
    """
    legacy = _legacy(tmp_path)

    assert register_for_reading(tmp_path) == legacy
    assert migrate_legacy_registry(tmp_path) is True, "the fallback made the move a no-op"

    assert not legacy.exists()
    assert agents_file(tmp_path).exists()
    assert register_for_reading(tmp_path) == agents_file(tmp_path)
