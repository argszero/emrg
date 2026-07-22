"""Unit tests for emrg.server.scheduler — task loading, migration, and lifecycle."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import yaml

from emrg.protocol import InstanceIdentity
from emrg.server.scheduler import (
    EvolutionHandler,
    TaskScheduler,
    _resolve_project_path,
)


# ── _resolve_project_path ─────────────────────────────────────────


def test_resolve_project_path_found(tmp_path):
    """Returns path when project name exists in projects.yml."""
    projects_yml = tmp_path / "projects.yml"
    projects_yml.write_text(
        yaml.safe_dump([
            {"name": "emrg", "path": "/home/emrg/src"},
            {"name": "other", "path": "/tmp/other"},
        ])
    )
    # Temporarily replace config_dir
    from emrg.server import scheduler as mod
    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        assert _resolve_project_path("emrg") == "/home/emrg/src"
        assert _resolve_project_path("other") == "/tmp/other"
        assert _resolve_project_path("nonexistent") is None
    finally:
        mod.config_dir = orig


def test_resolve_project_path_no_file(tmp_path):
    """Returns None when projects.yml doesn't exist."""
    from emrg.server import scheduler as mod
    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        assert _resolve_project_path("anything") is None
    finally:
        mod.config_dir = orig


def test_resolve_project_path_invalid_yaml(tmp_path):
    """Returns None for invalid (non-list) YAML."""
    projects_yml = tmp_path / "projects.yml"
    projects_yml.write_text("key: value\n")
    from emrg.server import scheduler as mod
    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        assert _resolve_project_path("anything") is None
    finally:
        mod.config_dir = orig


# ── TaskScheduler._save_tasks ─────────────────────────────────────


def test_save_tasks_atomic_write(tmp_path):
    """_save_tasks writes YAML atomically via tempfile + rename."""
    from emrg.server import scheduler as mod
    tasks_yml = tmp_path / "tasks.yml"
    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tasks_yml

    sched._save_tasks([
        {"name": "test1", "type": "evolution", "config": {"project": "emrg"}},
    ])

    assert tasks_yml.exists()
    data = yaml.safe_load(tasks_yml.read_text())
    assert len(data) == 1
    assert data[0]["name"] == "test1"


def test_save_tasks_overwrite(tmp_path):
    """_save_tasks replaces existing content, not appends."""
    from emrg.server import scheduler as mod
    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([{"name": "old"}]))

    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tasks_yml
    sched._save_tasks([{"name": "new"}])

    data = yaml.safe_load(tasks_yml.read_text())
    assert len(data) == 1
    assert data[0]["name"] == "new"


def test_save_tasks_creates_parent_dir(tmp_path):
    """_save_tasks creates parent directory if missing."""
    tasks_yml = tmp_path / "deep" / "nested" / "tasks.yml"
    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tasks_yml
    sched._save_tasks([{"name": "deep"}])

    assert tasks_yml.exists()
    data = yaml.safe_load(tasks_yml.read_text())
    assert data[0]["name"] == "deep"


# ── TaskScheduler.create_task ─────────────────────────────────────


def test_create_task_new(tmp_path):
    """create_task appends a new entry when name doesn't exist."""
    tasks_yml = tmp_path / "tasks.yml"
    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tasks_yml
    sched._save_tasks([])  # seed empty file

    sched.create_task(
        name="new-project",
        task_type="evolution",
        config={"project": "new-project"},
        interval=900,
    )

    data = yaml.safe_load(tasks_yml.read_text())
    assert len(data) == 1
    assert data[0]["name"] == "new-project"
    assert data[0]["type"] == "evolution"
    assert data[0]["config"] == {"project": "new-project"}
    assert data[0]["interval"] == 900
    assert data[0]["enabled"] is True


def test_create_task_update_existing(tmp_path):
    """create_task updates an existing entry when name matches."""
    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([
        {"name": "existing", "type": "evolution", "config": {}, "interval": 1800, "enabled": False}
    ]))

    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tasks_yml
    sched.create_task(
        name="existing",
        task_type="evolution",
        config={"project": "updated"},
        interval=600,
    )

    data = yaml.safe_load(tasks_yml.read_text())
    assert len(data) == 1
    assert data[0]["name"] == "existing"
    assert data[0]["config"] == {"project": "updated"}
    assert data[0]["interval"] == 600
    assert data[0]["enabled"] is True


def test_create_task_does_not_affect_other_tasks(tmp_path):
    """create_task only touches the matching entry, others untouched."""
    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([
        {"name": "task-a", "type": "evolution", "interval": 300},
        {"name": "task-b", "type": "evolution", "interval": 600},
    ]))

    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tasks_yml
    sched.create_task("task-a", "evolution", {"project": "a"}, 120)

    data = yaml.safe_load(tasks_yml.read_text())
    assert len(data) == 2
    assert data[0]["interval"] == 120  # updated
    assert data[1]["interval"] == 600  # unchanged


# ── TaskScheduler._migrate_from_projects ──────────────────────────


def test_migrate_no_projects_file(tmp_path):
    """No-op when projects.yml doesn't exist."""
    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tmp_path / "tasks.yml"
    sched._migrate_from_projects()
    # Should not have created tasks.yml
    assert not sched._tasks_file.exists()


def test_migrate_auto_evolve_entries_real(tmp_path):
    """Migrates auto_evolve=True entries to tasks.yml."""
    from emrg.server import scheduler as mod
    projects_yml = tmp_path / "projects.yml"
    tasks_yml = tmp_path / "tasks.yml"

    projects_yml.write_text(yaml.safe_dump([
        {"name": "manual", "path": "/tmp/m", "auto_evolve": False},
        {"name": "auto1", "path": "/tmp/a1", "auto_evolve": True, "interval": 600},
        {"name": "auto2", "path": "/tmp/a2", "auto_evolve": True},
    ]))

    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tasks_yml

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        sched._migrate_from_projects()
    finally:
        mod.config_dir = orig_config

    assert tasks_yml.exists()
    data = yaml.safe_load(tasks_yml.read_text())
    assert len(data) == 2  # only auto_evolve=True entries
    names = [e["name"] for e in data]
    assert "auto1" in names
    assert "auto2" in names
    assert "manual" not in names

    auto1 = next(e for e in data if e["name"] == "auto1")
    assert auto1["interval"] == 600
    assert auto1["type"] == "evolution"

    auto2 = next(e for e in data if e["name"] == "auto2")
    assert auto2["interval"] == 1800  # default


# ── TaskScheduler.load_and_start ──────────────────────────────────


def test_load_and_start_no_file(tmp_path):
    """Returns empty coro list when tasks.yml doesn't exist."""
    from emrg.server import scheduler as mod
    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tmp_path / "nonexistent" / "tasks.yml"

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        coros = sched.load_and_start()
    finally:
        mod.config_dir = orig_config

    assert coros == []


def test_load_and_start_enabled_task(tmp_path):
    """Starts a coroutine for each enabled task."""
    from emrg.server import scheduler as mod
    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([
        {"name": "emrg", "type": "evolution", "config": {"path": "/tmp"}, "interval": 99, "enabled": True},
    ]))

    async def _run():
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tasks_yml
        return sched.load_and_start(), sched

    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        (coros, sched) = asyncio.run(_run())
    finally:
        mod.config_dir = orig_config

    assert len(coros) == 1
    assert len(sched._handlers) == 1
    assert sched._handlers[0].name == "emrg"
    assert sched._handlers[0].interval == 99
    # Clean up: stop handler + cancel coros
    sched.stop_all()
    for c in coros:
        c.cancel()


def test_load_and_start_skips_disabled(tmp_path):
    """Disabled tasks are not started."""
    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([
        {"name": "enabled", "type": "evolution", "config": {"path": "/tmp"}, "enabled": True},
        {"name": "disabled", "type": "evolution", "config": {"path": "/tmp"}, "enabled": False},
    ]))

    async def _run():
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tasks_yml
        return sched.load_and_start(), sched

    from emrg.server import scheduler as mod
    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        (coros, sched) = asyncio.run(_run())
    finally:
        mod.config_dir = orig_config

    assert len(coros) == 1
    assert sched._handlers[0].name == "enabled"
    sched.stop_all()
    for c in coros:
        c.cancel()


def test_load_and_start_unknown_type(tmp_path):
    """Tasks with unknown handler type are skipped gracefully."""
    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([
        {"name": "bad", "type": "nonexistent_handler", "config": {}, "enabled": True},
    ]))

    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tasks_yml

    from emrg.server import scheduler as mod
    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        coros = sched.load_and_start()
    finally:
        mod.config_dir = orig_config

    assert coros == []


# ── EvolutionHandler core ─────────────────────────────────────────


def test_evolution_handler_project_path_fallback():
    """Without config.project or config.path, name is the fallback path."""
    handler = EvolutionHandler(
        name="emrg",
        config={},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler.project_path == "emrg"


def test_evolution_handler_project_path_from_config():
    """config.path is used when config.project is empty."""
    handler = EvolutionHandler(
        name="emrg",
        config={"path": "/custom/path"},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler.project_path == "/custom/path"


def test_evolution_handler_stop():
    """stop() sets _running to False."""
    handler = EvolutionHandler(
        name="test", config={}, interval=60,
        identity=InstanceIdentity(),
    )
    handler._running = True
    handler.stop()
    assert handler._running is False


def test_evolution_handler_default_owner():
    """When no git remote is detectable, falls back to EMRG defaults."""
    handler = EvolutionHandler(
        name="unknown-project",
        config={"path": "/nonexistent/path"},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler._owner == "argszero"
    assert handler._repo == "emrg"
    assert handler._repo_url == "https://github.com/argszero/emrg.git"
