"""Unit tests for emrg.server.scheduler — task loading, migration, and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path

import yaml

from emrg.protocol import InstanceIdentity
from emrg.server.scheduler import (
    TaskHandler,
    TaskScheduler,
    _resolve_project_path,
)


# ── _resolve_project_path ─────────────────────────────────────────


def test_task_handler_logger_adapter_carries_task_column(tmp_path):
    """Rant 2026-08-19T10:18:44: TaskHandler logs carry a dedicated `task`
    extra (LoggerAdapter) so the daemon's Formatter can render a [task]
    column — scheduler lines are otherwise indistinguishable in emrgd.log."""
    from emrg.server import scheduler as mod
    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        handler = TaskHandler(
            name="emrg-task",
            config={"project": "emrg"},
            interval=60,
            identity=InstanceIdentity(),
        )
    finally:
        mod.config_dir = orig_config

    # The per-task logger is a LoggerAdapter that injects the task name.
    assert isinstance(handler._logger, logging.LoggerAdapter)
    assert handler._logger.extra == {"task": "emrg-task"}

    # A record emitted through it carries the `task` attribute (Formatter
    # renders it as the [task] column; missing → "-").
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    cap = _Capture()
    logger = logging.getLogger("emrg.server.scheduler")
    logger.addHandler(cap)
    try:
        logger.setLevel(logging.DEBUG)
        handler._logger.info("TaskHandler[%s] tick", handler.name)
    finally:
        logger.removeHandler(cap)
    assert records, "expected at least one captured log record"
    assert getattr(records[0], "task", None) == "emrg-task"


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
    """No tasks.yml → self-heal creates emrg-task and starts it (rant 20:42 方案 C).

    Previously returned an empty list; now a packaged install without
    tasks.yml gets the emrg self-evolution task bootstrapped automatically.
    """
    from emrg.server import scheduler as mod

    async def _run():
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        return sched.load_and_start(), sched

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        (coros, sched) = asyncio.run(_run())
    finally:
        mod.config_dir = orig_config

    assert len(coros) == 1
    assert sched._handlers[0].name == "emrg-task"
    assert sched._handlers[0].interval == 60
    sched.stop_all()
    for c in coros:
        c.cancel()


def test_load_and_start_enabled_task(tmp_path):
    """Starts a coroutine for each enabled task."""
    from emrg.server import scheduler as mod
    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([
        {"name": "emrg", "type": "evolution", "config": {"project": "emrg"}, "interval": 99, "enabled": True},
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

    assert len(coros) == 1  # emrg-task is the self-evolution task — no duplicate
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
        {"name": "enabled", "type": "evolution", "config": {"project": "emrg"}, "enabled": True},
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
    """Custom/unknown types start as TaskHandler with the fallback template (P2).

    rant 2026-08-12T18:23:15: types not in TASK_TEMPLATES are treated as
    user-defined task types — resolved via ~/.emrg/task-templates/<type>.md,
    falling back to evolution_prompt.md with a warning. Self-heal still adds
    emrg-task.
    """
    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([
        {"name": "bad", "type": "nonexistent_handler", "config": {}, "enabled": True},
    ]))

    async def _run():
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tasks_yml
        return sched.load_and_start(), sched

    from emrg.server import scheduler as mod
    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        (coros, sched) = asyncio.run(_run())
    finally:
        mod.config_dir = orig_config

    by_name = {h.name: h for h in sched._handlers}
    assert set(by_name) == {"bad", "emrg-task"}  # custom type + self-healed
    assert by_name["bad"]._template_path.name == "evolution_prompt.md"  # fallback
    sched.stop_all()
    for c in coros:
        c.cancel()


# ── Template task types (paper / open-source / promote) ────────────


def test_task_templates_cover_all_handlers():
    """Every HANDLERS type has a TASK_TEMPLATES mapping and the file exists.

    Regression guard: template path bugs (promote #304, #306) crashed the
    scheduler at runtime. This test fails fast if a handler type loses its
    template mapping or a template file is renamed/missing.
    """
    from emrg.server import scheduler as mod

    for task_type in TaskScheduler.HANDLERS:
        assert task_type in mod.TASK_TEMPLATES, (
            f"missing TASK_TEMPLATES mapping for {task_type!r}"
        )
        template_path = (
            Path(__file__).resolve().parent.parent
            / "emrg" / "server" / mod.TASK_TEMPLATES[task_type]
        )
        assert template_path.exists(), (
            f"template file missing for {task_type!r}: {template_path.name}"
        )


def test_load_and_start_promote_task(tmp_path):
    """Promote tasks start an TaskHandler with the promote template."""
    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([
        {"name": "olr-promote", "type": "promote",
         "config": {"project": "openlocalrouter"},
         "interval": 3600, "enabled": True},
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

    # promote task + self-healed emrg-task
    assert len(coros) == 2
    by_name = {h.name: h for h in sched._handlers}
    assert by_name["olr-promote"]._template_path.name == "promote_prompt.md"
    assert by_name["emrg-task"]._template_path.name == "evolution_prompt.md"
    sched.stop_all()
    for c in coros:
        c.cancel()


def test_resolve_task_template_custom_user_file(tmp_path):
    """Custom type with a user template resolves to ~/.emrg/task-templates/<type>.md."""
    from emrg.server import scheduler as mod
    (tmp_path / "task-templates").mkdir()
    user_tpl = tmp_path / "task-templates" / "report.md"
    user_tpl.write_text("# Custom {{ instance_id }}\n", encoding="utf-8")

    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        assert mod._resolve_task_template("report") == user_tpl
    finally:
        mod.config_dir = orig_config


def test_resolve_task_template_custom_missing_falls_back(tmp_path):
    """Custom type without a user template falls back to evolution_prompt.md."""
    from emrg.server import scheduler as mod
    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        resolved = mod._resolve_task_template("no-such-type")
        assert resolved.name == "evolution_prompt.md"
    finally:
        mod.config_dir = orig_config


def test_resolve_task_template_builtin_unchanged(tmp_path):
    """Built-in types keep resolving to emrg/server/<builtin>.md regardless of user dir."""
    from emrg.server import scheduler as mod
    (tmp_path / "task-templates").mkdir()
    (tmp_path / "task-templates" / "evolution.md").write_text(
        "# evil override\n", encoding="utf-8"
    )
    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        resolved = mod._resolve_task_template("evolution")
        assert resolved.name == "evolution_prompt.md"
        assert "task-templates" not in str(resolved)
    finally:
        mod.config_dir = orig_config


# ── TaskHandler core ─────────────────────────────────────────


def test_evolution_handler_project_path_fallback():
    """Without config.project or config.path, name is the fallback path."""
    handler = TaskHandler(
        name="emrg",
        config={},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler.project_path == "emrg"


def test_evolution_handler_project_path_from_config():
    """config.path is used when config.project is empty."""
    handler = TaskHandler(
        name="emrg",
        config={"path": "/custom/path"},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler.project_path == "/custom/path"


def test_evolution_handler_stop():
    """stop() sets _running to False."""
    handler = TaskHandler(
        name="test", config={}, interval=60,
        identity=InstanceIdentity(),
    )
    handler._running = True
    handler.stop()
    assert handler._running is False


def test_evolution_handler_status_last_run_fields():
    """status() exposes last-run + saturation (rant 2026-08-18T10:45:52)."""
    from emrg.protocol import EvolutionLog
    handler = TaskHandler(
        name="test", config={}, interval=60,
        identity=InstanceIdentity(),
    )
    # no evolutions yet → null last-run fields, saturation defaults
    st = handler.status()
    assert st["name"] == "test"
    assert st["last_run_at"] is None
    # rant 2026-08-20T10:58:55: last_cycle_summary deleted; saturation is
    # {heartbeat_interval, heartbeat_active} only
    assert st["saturation"]["heartbeat_active"] is False
    assert "heartbeat_interval" in st["saturation"]
    # rant 2026-08-18T21:32:32: recent_runs present, empty before any run
    assert st["recent_runs"] == []
    # after one evolution → last-run populated from the latest log
    handler.evolutions.append(EvolutionLog(
        timestamp="2026-08-18T10:00:00",
        trigger="evolution-test-ts",
        impact=["tools-executed=24", "cycle-complete"],
        operations=["llm-reflection", "tool-execution"],
    ))
    handler._slowdown_active = True
    st = handler.status()
    assert st["last_run_at"] == "2026-08-18T10:00:00"
    assert st["saturation"]["heartbeat_active"] is True
    assert len(st["recent_runs"]) == 1
    r0 = st["recent_runs"][0]
    assert r0["timestamp"] == "2026-08-18T10:00:00"
    assert r0["work"] == ""
    assert r0["impact"] == ["tools-executed=24", "cycle-complete"]
    assert r0["recommend_slowdown"] is False
    assert r0["slowdown_reason"] == ""
    assert r0["tool_count"] == 0
    # agent work summary preferred over machine impact tags
    handler.evolutions.append(EvolutionLog(
        timestamp="2026-08-18T11:00:00",
        trigger="evolution-test-ts",
        impact=["tools-executed=5", "cycle-complete"],
        operations=[],
        work="修了 stop_all 双实例根因，提交 PR #854",
        recommend_slowdown=False,
        slowdown_reason="meaningful work done",
        tool_count=5,
    ))
    st = handler.status()
    assert len(st["recent_runs"]) == 2, "recent_runs holds last 5 runs"
    assert st["recent_runs"][1]["work"] == "修了 stop_all 双实例根因，提交 PR #854"
    assert st["recent_runs"][1]["slowdown_reason"] == "meaningful work done"
    assert st["recent_runs"][1]["tool_count"] == 5


def test_evolution_handler_recent_runs_capped_at_five():
    """recent_runs keeps only the last 5 evolutions (rant 2026-08-18T21:32:32)."""
    from emrg.protocol import EvolutionLog
    handler = TaskHandler(
        name="test", config={}, interval=60,
        identity=InstanceIdentity(),
    )
    for i in range(7):
        handler.evolutions.append(EvolutionLog(
            timestamp=f"2026-08-18T10:0{i}:00",
            trigger=f"t{i}",
            impact=[f"cycle-{i}-complete"],
        ))
    st = handler.status()
    runs = st["recent_runs"]
    assert len(runs) == 5
    assert runs[0]["timestamp"] == "2026-08-18T10:02:00"
    assert runs[-1]["timestamp"] == "2026-08-18T10:06:00"


# ── Task-run persistence (rant 2026-08-19T20:50:36, host Plan B) ─────
# Execution records append to ~/.emrg/logs/task-runs/<task>.jsonl so the GUI
# recent-runs survive daemon restarts; restored on handler init.


def test_task_handler_task_runs_persist_across_restart(tmp_path):
    """Appended run records are restored by a fresh handler (daemon restart).

    Plan B (rant 2026-08-19T20:50:36): cycle records persist to
    <config>/logs/task-runs/<task>.jsonl; a new TaskHandler over the same
    config_dir loads them back into self.evolutions.
    """
    from emrg.protocol import EvolutionLog
    from emrg.server import scheduler as mod
    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        h1 = TaskHandler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        h1.evolutions.append(EvolutionLog(
            timestamp="2026-08-19T20:10:00",
            trigger="evolution-emrg-task-ts",
            work="fixed vibe-check 400, submitted PR #874",
            recommend_slowdown=False,
            slowdown_reason="",
            tool_count=7,
        ))
        h1._append_task_run(h1.evolutions[-1])
        # second record
        h1.evolutions.append(EvolutionLog(
            timestamp="2026-08-19T20:20:00",
            trigger="evolution-emrg-task-ts2",
            work="",
            recommend_slowdown=True,
            slowdown_reason="长期无产出",
            tool_count=0,
        ))
        h1._append_task_run(h1.evolutions[-1])

        # "daemon restart": a brand-new handler over the same config_dir
        h2 = TaskHandler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        assert len(h2.evolutions) == 2
        first = h2.evolutions[0]
        assert first.timestamp == "2026-08-19T20:10:00"
        assert first.work == "fixed vibe-check 400, submitted PR #874"
        assert first.recommend_slowdown is False
        assert first.slowdown_reason == ""
        assert first.tool_count == 7
        second = h2.evolutions[1]
        assert second.work == ""
        assert second.recommend_slowdown is True
        assert second.slowdown_reason == "长期无产出"
        # GUI secondary list shows the restored records
        runs = h2.status()["recent_runs"]
        assert [r["timestamp"] for r in runs] == [
            "2026-08-19T20:10:00", "2026-08-19T20:20:00",
        ]
        assert runs[1]["slowdown_reason"] == "长期无产出"
        assert runs[1]["recommend_slowdown"] is True
        # JSONL file exists under logs/task-runs/<task>.jsonl
        f = tmp_path / "logs" / "task-runs" / "emrg-task.jsonl"
        assert f.exists()
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
    finally:
        mod.config_dir = orig


def test_task_handler_task_runs_capped_at_fifty(tmp_path):
    """JSONL keeps only the most recent 50 records (bounded append)."""
    from emrg.protocol import EvolutionLog
    from emrg.server import scheduler as mod
    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        h1 = TaskHandler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        for i in range(60):
            h1.evolutions.append(EvolutionLog(
                timestamp=f"2026-08-19T20:{i % 60:02d}:00",
                work=f"run-{i}",
                tool_count=i,
            ))
            h1._append_task_run(h1.evolutions[-1])

        f = tmp_path / "logs" / "task-runs" / "emrg-task.jsonl"
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 50, "file trimmed to the last 50 records"
        # fresh handler restores the most recent 50
        h2 = TaskHandler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        assert len(h2.evolutions) == 50
        assert h2.evolutions[-1].work == "run-59"
        assert h2.evolutions[0].work == "run-10"
    finally:
        mod.config_dir = orig


def test_task_handler_task_runs_corrupt_file_ignored(tmp_path):
    """Corrupt/unreadable JSONL is ignored — handler starts empty, no crash."""
    from emrg.server import scheduler as mod
    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        runs_dir = tmp_path / "logs" / "task-runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "emrg-task.jsonl").write_text(
            "not-json-at-all\n{broken json\n{\"timestamp\": \"ok\", \"work\": \"kept\"}\n",
            encoding="utf-8",
        )
        handler = TaskHandler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        # corrupt lines skipped; valid line kept
        assert len(handler.evolutions) == 1
        assert handler.evolutions[0].work == "kept"
    finally:
        mod.config_dir = orig


def test_task_handler_task_runs_write_failure_tolerated(tmp_path):
    """A failed append only logs a warning — never breaks the running cycle."""
    from emrg.protocol import EvolutionLog
    from emrg.server import scheduler as mod
    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        handler = TaskHandler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        # sabotage: make the target file path a directory → append raises
        runs_dir = tmp_path / "logs" / "task-runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "emrg-task.jsonl").mkdir()  # dir where the file should be
        log = EvolutionLog(timestamp="2026-08-19T20:30:00", work="x")
        handler.evolutions.append(log)
        # must not raise; cycle continues with the in-memory record
        handler._append_task_run(log)
        assert len(handler.evolutions) == 1
        assert handler.evolutions[0].work == "x"
    finally:
        mod.config_dir = orig


def test_evolution_handler_default_owner():
    """When no git remote is detectable, falls back to EMRG defaults."""
    handler = TaskHandler(
        name="unknown-project",
        config={"path": "/nonexistent/path"},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler._owner == "argszero"
    assert handler._repo == "emrg"
    assert handler._repo_url == "https://github.com/argszero/emrg.git"
    # No repo configured (non-emrg project, no remote, no config) → no self-heal
    assert handler._repo_configured is False


def test_task_handler_emrg_configured_by_default():
    """The emrg evolution task is always repo-configured (defaults to argszero/emrg)."""
    handler = TaskHandler(
        name="emrg-task",
        config={"project": "emrg"},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler._repo_configured is True
    assert handler._repo == "emrg"


def test_task_handler_repo_configured_from_config():
    """config owner/repo overrides the default and enables self-heal (rant 18:14:46 P1)."""
    handler = TaskHandler(
        name="paper-x",
        config={"project": "some-proj", "owner": "acme", "repo": "paper-x"},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler._repo_configured is True
    assert handler._owner == "acme"
    assert handler._repo == "paper-x"
    assert handler._repo_url == "https://github.com/acme/paper-x.git"


def test_task_handler_no_repo_skips_self_heal():
    """Non-emrg task without any repo config → no repo override (defaults)."""
    handler = TaskHandler(
        name="docs-task",
        config={"path": "/tmp/plain-folder"},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler._repo_configured is False
    assert handler._source_dir == "/tmp/plain-folder"


def test_task_scheduler_total_evolutions():
    """total_evolutions sums per-handler evolution log counts."""
    from emrg.protocol import EvolutionLog

    sched = TaskScheduler(InstanceIdentity())
    h1 = TaskHandler(name="a", config={}, interval=60, identity=InstanceIdentity())
    h2 = TaskHandler(name="b", config={}, interval=60, identity=InstanceIdentity())
    sched._handlers = [h1, h2]

    assert sched.total_evolutions() == 0
    h1.evolutions.append(EvolutionLog(timestamp="t1"))
    h1.evolutions.append(EvolutionLog(timestamp="t2"))
    assert sched.total_evolutions() == 2
    h2.evolutions.append(EvolutionLog(timestamp="t3"))
    assert sched.total_evolutions() == 3


def test_paper_template_renders_with_context():
    """paper_prompt.md renders without Jinja2 errors (seq/uptime placeholders)."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "paper_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    out = template.render(
        instance_id="test", host_name="host", uptime="0h 0m",
        source_dir="/tmp/paper", session_id="s1", timestamp="20260806",
        task={}, project={}, evolution_count=0,
    )
    assert "paper_state.md" in out, "状态文件指引应渲染"
    assert "latexmk" in out, "LaTeX 检查指引应渲染"
    assert "literature" in out, "文献去重指引应渲染"


def test_open_source_template_renders_with_context():
    """open_source_prompt.md renders without Jinja2 errors (rant-scan section)."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "open_source_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    out = template.render(
        instance_id="test", host_name="host", uptime="0h 0m",
        repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/os", source_dir="/tmp/os", session_id="s1",
        evolution_cwd="/tmp/evo", timestamp="20260813",
        task={"role": "committer", "project": "aitokenpool"},
        project={}, evolution_count=0, git_path="git", gh_path="gh",
    )
    assert "0.5 Rant scan" in out, "rant-scan 0.5 节应渲染"
    assert "rants.jsonl" in out, "rant 扫描命令应渲染"
    assert "config.project" in out, "project 匹配过滤应渲染"
    # Rant 2026-08-17T14:17:03: rant project matching is the SINGLE value
    # config.project — owner/repo dual-compat removed (host decision).
    assert "aitokenpool" in out, "task.project 值应渲染进 0.5 节"
    assert "does **NOT** match" in out, "owner/repo 形式明确不匹配"
    # the unmatched-rant hint may still mention the owner/repo form for
    # detecting rants the host should fix — that is a hint, not a match rule
    assert "Unmatched-rant hint" in out, "未匹配疑似 rant 提示应渲染"
    assert "B.1b Rant-driven mode" in out, "rant 驱动模式应渲染"
    assert "ROLE LOCK" in out, "既有 ROLE LOCK 应保留"
    assert "json.dumps(..., ensure_ascii=False)" in out, "rant 状态写入要求应渲染"


def test_open_source_template_allow_self_merge_conditional():
    """open_source_prompt.md renders allow_self_merge conditional (rant 2026-08-14T12:47:25)."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "open_source_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))

    base = dict(
        instance_id="test", host_name="host", uptime="0h 0m",
        repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/os", source_dir="/tmp/os", session_id="s1",
        evolution_cwd="/tmp/evo", timestamp="20260814",
        project={}, evolution_count=0, git_path="git", gh_path="gh",
    )

    # default (allow_self_merge absent) → rule stands, no override
    out_default = template.render(task={"role": "committer", "project": "aitokenpool"}, **base)
    assert "Do not merge your own PRs (wait for other Committers to review)" in out_default
    assert "allow_self_merge: true" not in out_default

    # explicit true → override text appears + opt-in note in role section
    out_true = template.render(
        task={"role": "committer", "project": "aitokenpool", "allow_self_merge": True}, **base
    )
    assert "allow_self_merge" in out_true, "allow_self_merge 说明应渲染"
    assert "may review and merge their own PRs" in out_true, "self-merge 允许说明应渲染"
    assert "**overridden**: this task configures" in out_true, "Forbidden 条件化覆盖应渲染"

    # explicit false → same as default
    out_false = template.render(
        task={"role": "committer", "project": "aitokenpool", "allow_self_merge": False}, **base
    )
    assert "Do not merge your own PRs (wait for other Committers to review)" in out_false
    assert "**overridden**" not in out_false


def test_open_source_template_never_stashes_host_work():
    """Rant 2026-08-20T11:58:27 (host data-loss report): a dirty working tree
    must run the cycle read-only — the prompt must never instruct stashing /
    resetting the host's uncommitted work in the live source directory."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "open_source_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    out = template.render(
        instance_id="test", host_name="host", uptime="0h 0m",
        repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/os", source_dir="/tmp/os", session_id="s1",
        evolution_cwd="/tmp/evo", timestamp="20260820",
        task={"role": "committer", "project": "aitokenpool"},
        project={}, evolution_count=0, git_path="git", gh_path="gh",
    )
    # the old data-losing instruction is gone
    assert "→ `git stash`" not in out, "no more 'git stash' action instruction"
    assert "→ `git pull --rebase`" not in out or "dirty tree" in out, \
        "pull --rebase must be gated on a clean tree"
    # the new safety rules render
    assert "Never touch the host's uncommitted work" in out
    assert "read-only" in out, "dirty tree → read-only cycle"
    assert "git rebase --abort" in out, "pull-conflict handling must abort, not stash"
    assert "Never stash host work" in out
    # the prohibition explicitly forbids the destructive forms
    for banned in ("git checkout .", "git restore .", "git reset --hard", "git clean"):
        assert f"`{banned}`" in out, f"禁止项应列出 {banned}"


def test_open_source_template_full_code_study_b2b():
    """open_source_prompt.md B.2b requires full-code study before contributing
    (rant 2026-08-14T15:53:39)."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "open_source_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    out = template.render(
        instance_id="test", host_name="host", uptime="0h 0m",
        repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/os", source_dir="/tmp/os", session_id="s1",
        evolution_cwd="/tmp/evo", timestamp="20260814",
        task={"role": "committer", "project": "aitokenpool"},
        project={}, evolution_count=0, git_path="git", gh_path="gh",
    )
    # 1) the new section exists (positive discrimination: absent section → red)
    assert "B.2b Read the full codebase" in out, "B.2b 全代码研读节应渲染"
    # 2) must read the complete codebase, not just the target file
    assert "(not just the target files)" in out, "读完整代码要求应渲染"
    # 3) must always re-read the latest code before each contribution
    assert "re-read the latest code before every contribution" in out, "每次读取最新代码要求应渲染"
    # 4) understand design intent from the repository author's perspective
    assert "Understand the design intent from the repository author's perspective" in out, "作者视角设计意图要求应渲染"
    # 5) only after understanding the design may one contribute — align with it
    assert "Only when you understand the author's design intent should you consider how to contribute" in out, "理解设计意图后才可贡献应渲染"


def test_promote_template_learn_latest_state_04():
    """promote_prompt.md §0.4 requires learning the project's latest state
    before promoting (rant 2026-08-14T22:13:57, mirrors open-source B.2b #790)."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "promote_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    out = template.render(
        instance_id="test", host_name="host", uptime="0h 0m",
        repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/pm", source_dir="/tmp/pm", session_id="s1",
        evolution_cwd="/tmp/evo", timestamp="20260814",
        task={"project": "aitokenpool"},
        project={"path": "/tmp/proj", "name": "aitokenpool", "description": "d"},
        evolution_count=0, git_path="git", gh_path="gh",
    )
    # 1) the new section exists (positive discrimination: absent section → red)
    assert "0.4 Learn the project's latest state" in out, "0.4 节应渲染"
    # 2) MUST every round
    assert "re-learn the project's latest state before every promotion round" in out, "每轮 MUST 前提应渲染"
    # 3) latest commit inspection command with project.path
    assert "git fetch -q origin" in out, "git fetch 最新代码应渲染"
    assert "git log --oneline -10 origin/HEAD" in out, "最近 commit 检视应渲染"
    # 4) claims must come from just-verified state, not stale memory
    assert "no fabrication, no relying on stale version knowledge" in out, "不得沿用旧认知应渲染"
    # 5) Step 2 feature descriptions must come from §0.4 verification
    assert "MUST come from the project's latest state verified in §0.4" in out, "Step 2 功能描述来源应渲染"
    # 6) state file records knowledge freshness
    assert "last learned" in out, "状态文件 last learned 字段应渲染"
    # 7) reflection Q3 records what was learned (commit range / modules)
    assert "commit range / modules read via §0.4" in out, "反思 Q3 学习记录应渲染"


def test_promote_template_homework_first_dehardening():
    """promote_prompt.md §2 homework-before-participating + red line 4
    disclosure-default-OFF (rant 2026-08-15T08:40:25, 2× HN [flagged] 反例)."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "promote_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    out = template.render(
        instance_id="test", host_name="host", uptime="0h 0m",
        repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/pm", source_dir="/tmp/pm", session_id="s1",
        evolution_cwd="/tmp/evo", timestamp="20260815",
        task={"project": "aitokenpool"},
        project={"path": "/tmp/proj", "name": "aitokenpool", "description": "d"},
        evolution_count=0, git_path="git", gh_path="gh",
    )
    # A. homework-before-participating section exists (positive discrimination)
    assert "Do your homework before participating (MUST — host mandate)" in out, "§2 功课先行节应渲染"
    assert "Read the full discussion" in out, "读完整讨论要求应渲染"
    assert "write a test script / run a local verification before replying" in out, "本地验证要求应渲染"
    assert "skip that discussion" in out, "做不好功课宁可跳过应渲染"
    # B. red line 4 disclosure default OFF
    assert "disclosure default OFF" in out, "红线 4 披露默认关闭应渲染"
    assert "NO disclosure, NO project mention" in out, "普通参与不披露应渲染"
    assert "one sentence at the END" in out, "披露一句话后置应渲染"
    assert "fixed-formula disclosure as the first sentence" in out, "禁止固定句式开头披露应渲染"
    # B. mention density ≥70/≤30 + de-template + flagged cool-down
    assert "≥70%" in out, "纯价值回复 ≥70% 应渲染"
    assert "≤30%" in out, "提及项目 ≤30% 应渲染"
    assert "cool-down period" in out, "被 flag 降温期应渲染"
    # C. state file supplementary fields
    assert "homework record" in out, "状态文件功课记录字段应渲染"
    assert "flagged/negative" in out, "状态文件 flagged/negative 字段应渲染"
    assert "mention stats" in out, "状态文件提及统计字段应渲染"


def test_promote_template_registration_blog_sections():
    """promote_prompt.md §2.x host-authorized account registration + §2.y
    blog publishing (rants 2026-08-15T09:04:28 / 09:06:12)."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "promote_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    out = template.render(
        instance_id="test", host_name="host", uptime="0h 0m",
        repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/pm", source_dir="/tmp/pm", session_id="s1",
        evolution_cwd="/tmp/evo", timestamp="20260815",
        task={"project": "aitokenpool"},
        project={"path": "/tmp/proj", "name": "aitokenpool", "description": "d"},
        evolution_count=0, git_path="git", gh_path="gh",
    )
    # A. Account Registration (host-authorized) section with 3 preconditions
    assert "Account Registration (host-authorized)" in out, "账号注册授权节应渲染"
    assert "Never register a duplicate" in out, "禁止重复注册应渲染"
    assert "blocked (registration needs human)" in out, "需人工验证→blocked 应渲染"
    # A. blanket Forbidden ban removed (negative discrimination)
    assert "No auto-creating/managing social accounts" not in out, "Forbidden 不应再有 blanket 禁止注册"
    # A. §0.3 registration-aware flow + channel accounts state field
    assert "channel accounts" in out, "状态文件 channel accounts 字段应渲染"
    assert "REUSE it" in out, "已有账号复用应渲染"
    # B. Blog Publishing section
    assert "Blog Publishing (deep content output)" in out, "Blog Publishing 节应渲染"
    assert "blogger.com / Dev.to / Medium" in out, "博客渠道应渲染"
    assert "≤1 post/week" in out, "发布节奏 ≤1 篇/周 应渲染"
    # B. blog state fields + §0.4 linkage
    assert "blog posts" in out, "状态文件 blog posts 字段应渲染"
    assert "blog drafts" in out, "状态文件 blog drafts 字段应渲染"
    assert "deep-content topic candidate" in out, "§0.4 新 release → blog drafts 联动应渲染"
    # B. red line 7 account asset maintenance
    assert "Registered accounts are long-term assets" in out, "红线 7 账号长期资产应渲染"


# ── Evolution workspace self-heal (rant 2026-08-06T20:42:05, 方案 C) ──────


def _make_handler(tmp_path, name="emrg-task", project="emrg", path=None):
    """Build an TaskHandler pointed at a tmp config dir."""
    from emrg.server import scheduler as mod
    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    handler = TaskHandler(
        name=name,
        config={"project": project} if project else {},
        interval=60,
        identity=InstanceIdentity(),
    )
    mod.config_dir = orig_config
    if path is not None:
        handler._source_dir = str(path)
        handler.project_path = str(path)
    return handler



def test_ensure_self_evolution_task_adds_when_missing(tmp_path):
    """tasks.yml without an emrg evolution task gets emrg-task appended."""
    from emrg.server import scheduler as mod
    from emrg.server.scheduler import TaskScheduler

    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([
        {"name": "other", "type": "evolution", "config": {"project": "other"}, "enabled": True},
    ]))

    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tasks_yml

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        sched._ensure_self_evolution_task()
        sched._ensure_self_evolution_task()  # idempotent
    finally:
        mod.config_dir = orig_config

    data = yaml.safe_load(tasks_yml.read_text(encoding="utf-8"))
    names = [e["name"] for e in data]
    assert "emrg-task" in names
    assert "other" in names
    emrg = next(e for e in data if e["name"] == "emrg-task")
    assert emrg["type"] == "evolution"
    assert emrg["config"] == {"project": "emrg"}
    assert emrg["interval"] == 60
    assert emrg["enabled"] is True
    assert len(names) == 2  # no duplicate from second call


def test_ensure_self_evolution_task_idempotent_when_present(tmp_path):
    """Existing emrg evolution task is left untouched (no duplicate)."""
    from emrg.server import scheduler as mod
    from emrg.server.scheduler import TaskScheduler

    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([
        {"name": "emrg-task", "type": "evolution",
         "config": {"project": "emrg"}, "interval": 60, "enabled": True,
         "last_run": None},
    ]))

    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tasks_yml

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        sched._ensure_self_evolution_task()
    finally:
        mod.config_dir = orig_config

    data = yaml.safe_load(tasks_yml.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["name"] == "emrg-task"


def test_ensure_self_evolution_task_adds_project_entry_when_missing(tmp_path):
    """Missing projects.yml emrg entry gets added (fixed path, no network)."""
    from emrg.server import scheduler as mod
    from emrg.server.scheduler import EVOLUTION_CWD, TaskScheduler

    sched = TaskScheduler(InstanceIdentity())

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        sched._ensure_self_evolution_task()
        sched._ensure_self_evolution_task()  # idempotent
    finally:
        mod.config_dir = orig_config

    projects_yml = tmp_path / "projects.yml"
    assert projects_yml.exists()
    data = yaml.safe_load(projects_yml.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    emrg = next(e for e in data if e.get("name") == "emrg")
    assert emrg["path"] == str(EVOLUTION_CWD / "emrg")
    assert len([e for e in data if e.get("name") == "emrg"]) == 1  # no dup


def test_ensure_self_evolution_task_preserves_existing_project_entry(tmp_path):
    """Existing emrg project entry (dev-machine path) is preserved as-is."""
    from emrg.server import scheduler as mod
    from emrg.server.scheduler import TaskScheduler

    # A real existing checkout dir (dev machine) — preserved, never repaired.
    dev_path = tmp_path / "dev" / "emrg"
    dev_path.mkdir(parents=True)
    projects_yml = tmp_path / "projects.yml"
    projects_yml.write_text(yaml.safe_dump([
        {"name": "emrg", "path": str(dev_path),
         "last_active": "2026-01-01T00:00:00"},
    ]))

    sched = TaskScheduler(InstanceIdentity())

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        sched._ensure_self_evolution_task()
    finally:
        mod.config_dir = orig_config

    data = yaml.safe_load(projects_yml.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["name"] == "emrg"
    assert data[0]["path"] == str(dev_path)  # untouched


def test_ensure_self_evolution_task_repairs_stale_project_entry(tmp_path):
    """A dead emrg path (deleted pytest-temp dir) is repaired to the canonical
    workspace (2026-08-12 incident: a test run leaked a pytest temp path into
    the real ~/.emrg/projects.yml; the dir is gone after the suite, leaving a
    dangling entry that list_projects/GUI pickers would show forever)."""
    from emrg.server import scheduler as mod
    from emrg.server.scheduler import EVOLUTION_CWD, TaskScheduler

    stale = tmp_path / "gone" / "emrg"  # never created → dead path
    projects_yml = tmp_path / "projects.yml"
    projects_yml.write_text(yaml.safe_dump([
        {"name": "emrg", "path": str(stale),
         "last_active": "2026-08-12T18:44:50"},
        {"name": "other", "path": str(tmp_path / "other")},
    ]))

    sched = TaskScheduler(InstanceIdentity())

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        sched._ensure_self_evolution_task()
    finally:
        mod.config_dir = orig_config

    data = yaml.safe_load(projects_yml.read_text(encoding="utf-8"))
    by_name = {e["name"]: e for e in data}
    assert by_name["emrg"]["path"] == str(EVOLUTION_CWD / "emrg")  # repaired
    assert by_name["other"]["path"] == str(tmp_path / "other")  # untouched
    assert len(data) == 2


def test_ensure_self_evolution_task_other_entries_preserved(tmp_path):
    """Non-emrg project entries survive the self-heal."""
    from emrg.server import scheduler as mod
    from emrg.server.scheduler import TaskScheduler

    projects_yml = tmp_path / "projects.yml"
    projects_yml.write_text(yaml.safe_dump([
        {"name": "paper", "path": "/some/paper"},
    ]))

    sched = TaskScheduler(InstanceIdentity())

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        sched._ensure_self_evolution_task()
    finally:
        mod.config_dir = orig_config

    data = yaml.safe_load(projects_yml.read_text(encoding="utf-8"))
    names = [e.get("name") for e in data]
    assert "paper" in names
    assert "emrg" in names
    assert len(names) == 2

# ── TaskHandler cycle truncation detection ──────────────────
# mem-repo lesson (tool-call truncation must be flagged, not silently
# treated as a successful/empty cycle — #523 applied it to the chat UI;
# this covers EMRG's own evolution task loop).

def _make_cycle_handler(tmp_path, frames):
    """Build a fully-scripted handler for _run_evolution_cycle tests."""
    import json as _json

    from websockets.exceptions import ConnectionClosed as _Closed

    from emrg.server import scheduler as mod

    class _FakeWS:
        def __init__(self, frm):
            self._frames = list(frm)
            self.sent = []

        async def send(self, msg):
            self.sent.append(msg)

        async def recv(self):
            if self._frames:
                return _json.dumps(self._frames.pop(0), ensure_ascii=False)
            raise _Closed()

        async def close(self):
            pass

    async def _fake_connect():
        return _FakeWS(frames)

    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    handler._build_evolution_prompt = lambda: "test prompt"
    mod.connect_to_server = _fake_connect

    class _CapturedLog(dict):
        """Lazily resolves `captured["log"]` to handler.evolutions[-1].

        The cycle now keeps logs in the in-memory list only (rant
        2026-08-19T14:18:40 — _write_evolution_log deleted); `"log" not in
        captured` stays True while no cycle completed.
        """

        def __contains__(self, key):
            if key == "log":
                return bool(handler.evolutions)
            return super().__contains__(key)

        def __getitem__(self, key):
            if key == "log":
                return handler.evolutions[-1]
            return super().__getitem__(key)

    return handler, _CapturedLog()


def test_evolution_cycle_truncated_not_empty_not_complete(tmp_path):
    """Truncated done frame → flagged truncated, NOT a complete cycle, slowdown
    state untouched (no vibe signal from a truncated round)."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"tool_name": "bash"},
        {"request_id": "r1", "content": "Exceeded maximum tool call rounds (270).",
         "done": True, "delta": False, "session_id": "s"},
    ])
    asyncio.run(handler._run_evolution_cycle())
    assert handler._slowdown_active is False, \
        "truncated cycle must not touch the slowdown state (no vibe signal)"
    impact = captured["log"].impact
    assert any("truncated" in i for i in impact), impact
    assert "truncated=max-tool-rounds" in impact, impact
    assert not any(i.endswith("-complete") for i in impact), impact


def test_evolution_cycle_complete_agent_recommends_no_slowdown(tmp_path):
    """Clean completion + vibe work empty + recommend_slowdown=false → normal
    cadence maintained, work stays empty (rant 2026-08-20T10:58:55: the vibe
    check's recommend_slowdown is the ONLY slowdown switch)."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "r1", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
        {"type": "vibe_check_result", "ok": True,
         "result": {"work": "", "recommend_slowdown": False,
                    "slowdown_reason": "nothing to evolve"}},
    ])
    asyncio.run(handler._run_evolution_cycle())
    assert handler._slowdown_active is False
    log = captured["log"]
    impact = log.impact
    assert any(i.endswith("-complete") for i in impact), impact
    assert any(i.startswith("cycle-") for i in impact), \
        f"impact tag uses new cycle- prefix (rant 2026-08-12T18:03:26), got {impact}"
    assert "truncated=max-tool-rounds" not in impact, impact
    assert log.work == "", "empty work stays empty (no completion fallback)"
    assert log.recommend_slowdown is False


def test_evolution_cycle_agent_work_restores_normal_cadence(tmp_path):
    """Agent reports work + recommend_slowdown=false → a throttled handler is
    restored to normal cadence; the work is persisted (rant 2026-08-20T10:58:55
    — recommend=false is the restore signal, no counter/vote machinery)."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "r1", "content": "Analyzed the issue and wrote memory",
         "done": True, "delta": False, "session_id": "s"},
        {"type": "vibe_check_result", "ok": True,
         "result": {"work": "分析了 scheduler 空转判定 bug，写了 memory 记录",
                    "recommend_slowdown": False,
                    "slowdown_reason": ""}},
    ])
    handler._slowdown_active = True  # previously throttled
    asyncio.run(handler._run_evolution_cycle())
    assert handler._slowdown_active is False, \
        "recommend=false restores the normal cadence"
    assert "log" in captured
    log = captured["log"]
    assert log.work == "分析了 scheduler 空转判定 bug，写了 memory 记录"
    assert log.recommend_slowdown is False
    assert log.slowdown_reason == ""
    assert log.tool_count == 0


def test_evolution_cycle_log_work_no_completion_fallback(tmp_path):
    """Rant 2026-08-19T07:06:45 (host-finalized): work uses ONLY the vibe
    check "work" field — NO fallback to the completion first line. Empty stays
    empty (GUI renders "-"), never a machine/rough fallback."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "r1", "content": "Reviewed PR and posted LGTM",
         "done": True, "delta": False, "session_id": "s"},
        {"type": "vibe_check_result", "ok": True,
         "result": {"work": "", "recommend_slowdown": False,
                    "slowdown_reason": "reviewed"}},
    ])
    asyncio.run(handler._run_evolution_cycle())
    log = captured["log"]
    assert log.work == "", \
        "missing work → work stays empty (no completion fallback)"
    assert log.recommend_slowdown is False

    # vibe check entirely unavailable → work stays empty, flags False
    handler2, captured2 = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "r1", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
    ])
    asyncio.run(handler2._run_evolution_cycle())
    log2 = captured2["log"]
    assert log2.work == "", "vibe unavailable → work stays empty (no fallback)"
    assert log2.recommend_slowdown is False
    assert log2.tool_count == 0


def test_evolution_cycle_vibe_unavailable_state_unchanged(tmp_path):
    """Vibe check unavailable (timeout/failure) → slowdown state unchanged.

    Conservative: a failed question must not cause a wrong throttle NOR a
    wrong restore (rant 2026-08-20T10:58:55)."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "r1", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
        # no vibe_check_result frame → helper times out / connection closed
    ])
    handler._slowdown_active = True
    asyncio.run(handler._run_evolution_cycle())
    assert handler._slowdown_active is True, \
        "vibe check failure must not touch the slowdown state"
    assert "log" in captured, "main task still completed normally"
    assert captured["log"].work == ""
    assert captured["log"].recommend_slowdown is False


def test_evolution_cycle_recommend_slowdown_throttles(tmp_path):
    """recommend_slowdown=true → _slowdown_active=True (heartbeat cadence);
    the next cycle's recommend=false restores normal cadence (rant
    2026-08-20T10:58:55 — the vibe flag is the single switch)."""
    handler, _ = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "r1", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
        {"type": "vibe_check_result", "ok": True,
         "result": {"work": "", "recommend_slowdown": True,
                    "slowdown_reason": "长期无产出"}},
    ])
    asyncio.run(handler._run_evolution_cycle())
    assert handler._slowdown_active is True, \
        "recommend=true must throttle the next run to heartbeat cadence"
    assert handler._saturation_heartbeat_active() is True
    assert handler._heartbeat_interval() == 480  # 60s task → 8 min

    # second cycle: agent says value again → restore
    handler2, captured2 = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "r1", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
        {"type": "vibe_check_result", "ok": True,
         "result": {"work": "merged PR #880", "recommend_slowdown": False,
                    "slowdown_reason": ""}},
    ])
    asyncio.run(handler2._run_evolution_cycle())
    assert handler2._slowdown_active is False
    assert captured2["log"].recommend_slowdown is False


def test_slowdown_state_persisted_across_restart(tmp_path):
    """_slowdown_active survives a daemon restart via the saturation file
    (~/.emrg/saturation/<task>.json, rant 2026-08-20T10:58:55)."""
    from emrg.server import scheduler as mod
    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        h1 = TaskHandler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        h1._slowdown_active = True
        h1._save_saturation_state()
        # "daemon restart": a fresh handler over the same config_dir
        h2 = TaskHandler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        assert h2._slowdown_active is True, \
            "throttled state restored from disk"
        # old-format file (no slowdown_active) reads as False — no migration
        (tmp_path / "saturation" / "other.json").write_text(
            '{"empty_cycles": 3, "slowdown_hits": 2}', encoding="utf-8")
        h4 = TaskHandler(name="other", config={}, interval=60, identity=InstanceIdentity())
        assert h4._slowdown_active is False, \
            "legacy saturation files simply read as not throttled"
    finally:
        mod.config_dir = orig


def test_evolution_cycle_aborted_error_not_counted(tmp_path):
    """Server error frame (e.g. 'session busy') → no evolution log, no count."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"error": "session busy"},
    ])
    asyncio.run(handler._run_evolution_cycle())
    assert "log" not in captured, "aborted cycle must not write an evolution log"
    assert handler.evolutions == [], "aborted cycle must not append to evolutions"
    assert handler._slowdown_active is False, \
        "aborted cycle must not touch the slowdown state (agent never ran)"


def test_evolution_cycle_aborted_leaves_slowdown_state(tmp_path):
    """Aborted cycle leaves a pre-existing throttle flag untouched (blocked ≠
    a vibe signal; rant 2026-08-20T10:58:55 conservative rule)."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"error": "session busy"},
    ])
    handler._slowdown_active = True
    asyncio.run(handler._run_evolution_cycle())
    assert "log" not in captured
    assert handler._slowdown_active is True, \
        "abort must not clear the throttle flag (no vibe signal received)"


# ── Connect-failure alerting (G129, rant 2026-08-09T08:03:46) ─────
# GUI tests once overwrote the real ~/.emrg/emrgd.token with fake values,
# so the evolution cycle failed to reach the daemon for 10 hours with only
# a WARNING log. Consecutive failures must escalate to ERROR + carry an
# actionable hint (check the token file), never silently swallow.

def test_evolution_cycle_connect_failure_escalates_to_error(tmp_path, caplog):
    """Repeated connect failures must escalate from warning to error alert."""
    import logging

    from emrg.server import scheduler as mod

    handler, captured = _make_cycle_handler(tmp_path, frames=[])
    async def _refuse():
        raise ConnectionRefusedError("no daemon")
    mod.connect_to_server = _refuse
    try:
        for i in range(handler._CONNECT_FAIL_ALERT):
            with caplog.at_level(logging.ERROR, logger="emrg.server.scheduler"):
                asyncio.run(handler._run_evolution_cycle())
                assert "log" not in captured, "connect failure must not write an evolution log"
                assert handler.evolutions == []
                assert handler._slowdown_active is False, "connect failure ≠ throttle signal"
        assert handler._connect_failures == handler._CONNECT_FAIL_ALERT
        # 第 3 次（达到阈值）必须出现 ERROR 告警，且提示检查 port 文件
        error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_msgs, "expected an ERROR alert after threshold"
        assert any("emrgd.token" in m for m in error_msgs), error_msgs
        assert any("consecutive" in m for m in error_msgs), error_msgs
    finally:
        mod.connect_to_server = _original_connect_to_server()


def test_evolution_cycle_connect_failure_resets_on_success(tmp_path, caplog):
    """A successful connection resets the consecutive-failure counter."""
    from emrg.server import scheduler as mod

    handler, captured = _make_cycle_handler(tmp_path, frames=[])
    async def _refuse():
        raise ConnectionRefusedError("no daemon")
    mod.connect_to_server = _refuse
    try:
        asyncio.run(handler._run_evolution_cycle())
        assert handler._connect_failures == 1
        # 成功连接 → 计数归零
        async def _fake_connect():
            return _FakeWsForCycle([{"request_id": "r1", "content": "Done", "done": True,
                                     "delta": False, "session_id": "s"}])
        mod.connect_to_server = _fake_connect
        asyncio.run(handler._run_evolution_cycle())
        assert handler._connect_failures == 0, "success must reset the failure counter"
    finally:
        mod.connect_to_server = _original_connect_to_server()


# ── Connect-failure exponential backoff (rant 2026-08-09T13:16:36 ③) ─
# Windows v0.2.15 regression: daemon down → every tick's connect failure
# returned immediately and the loop re-ran at full interval — with multiple
# handlers that produced a per-second retry/window storm. Backoff must be
# max(30s, interval * 2^n) capped at 10 minutes.

def test_connect_backoff_zero_failures_returns_interval():
    """No consecutive failures → normal interval (no backoff)."""
    from emrg.server.scheduler import TaskHandler

    handler = TaskHandler(
        name="emrg-task", config={"project": "emrg"}, interval=60,
        identity=InstanceIdentity(),
    )
    handler._connect_failures = 0
    assert handler._connect_backoff() == 60.0


def test_connect_backoff_grows_exponentially_capped(tmp_path):
    """Consecutive failures grow the wait, capped at 10 minutes."""
    from emrg.server.scheduler import TaskHandler

    handler = TaskHandler(
        name="emrg-task", config={"project": "emrg"}, interval=60,
        identity=InstanceIdentity(),
    )
    # interval=60s: 2^1=2 → 120s; 2^2=4 → 240s; 2^3=8 → 480s; 2^4=16 → 960s → capped 600s
    expectations = {1: 120.0, 2: 240.0, 3: 480.0, 4: 600.0, 5: 600.0, 10: 600.0}
    for failures, expected in expectations.items():
        handler._connect_failures = failures
        assert handler._connect_backoff() == expected, (
            f"failures={failures}: expected {expected}"
        )


def test_connect_backoff_floor_30s_for_small_interval(tmp_path):
    """Backoff never drops below 30s even for very fast intervals."""
    from emrg.server.scheduler import TaskHandler

    handler = TaskHandler(
        name="emrg-task", config={"project": "emrg"}, interval=10,
        identity=InstanceIdentity(),
    )
    handler._connect_failures = 2
    # max(30, 10 * 2^2) = max(30, 40) = 40
    assert handler._connect_backoff() == 40.0
    handler._connect_failures = 1
    # max(30, 10 * 2^1) = max(30, 20) = 30 → floor holds
    assert handler._connect_backoff() == 30.0


class _FakeWsForCycle:
    """Minimal ws stand-in for the reset-on-success test."""
    def __init__(self, frames):
        import json as _json
        from websockets.exceptions import ConnectionClosed as _Closed
        self._frames = list(frames)
        self._json = _json
        self._Closed = _Closed
        self.sent = []
    async def send(self, msg):
        self.sent.append(msg)
    async def recv(self):
        if self._frames:
            return self._json.dumps(self._frames.pop(0), ensure_ascii=False)
        raise self._Closed()
    async def close(self):
        pass


def _original_connect_to_server():
    """Restore the real connect_to_server after a test replaced it."""
    import importlib
    from emrg.server import scheduler as mod
    return importlib.import_module("emrg.connect").connect_to_server


# ── Saturation heartbeat: slow down, never stop (rant 2026-08-09T09:35:55) ─
# The old complete halt (skipping scheduled runs) is replaced by
# low-frequency full cycles: throttled ticks still run, just at the heartbeat
# interval. The throttle flag is set solely by the vibe check's
# recommend_slowdown (rant 2026-08-20T10:58:55).

def test_heartbeat_interval_formula(tmp_path):
    """heartbeat = max(interval, min(interval*8, 8h)); long intervals unchanged."""
    from emrg.server import scheduler as mod
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    for interval, expected in [
        (1, 8),            # min*8 floor below the 8h cap
        (60, 480),         # emrg-task: 8 minutes
        (600, 4800),       # 10-min task: 80 minutes
        (3600, 28800),     # 1h task: 8h (cap)
        (14400, 28800),    # 4h task: min(115200, 28800) = 8h (cap)
        (28800, 28800),    # 8h task: unchanged (max keeps original)
        (86400, 86400),    # 24h task: unchanged (8x beyond cap → original)
    ]:
        handler.interval = interval
        assert handler._heartbeat_interval() == expected, (interval, expected)


def test_saturation_heartbeat_active_true_when_throttled(tmp_path):
    """Throttle flag on → heartbeat cadence (not skip), no network (rant
    2026-08-18T20:32:07 — upstream check removed; flag from vibe check, rant
    2026-08-20T10:58:55)."""
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    handler._slowdown_active = True
    assert handler._saturation_heartbeat_active() is True
    assert handler._heartbeat_interval() == 480  # 60s task → 8 min


def test_saturation_heartbeat_log_message_no_skip(tmp_path, caplog):
    """A throttled cycle logs 'heartbeat interval', never 'skipping scheduled run'."""
    import logging

    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "r1", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
        {"type": "vibe_check_result", "ok": True,
         "result": {"work": "", "recommend_slowdown": True,
                    "slowdown_reason": "长期无产出"}},
    ])
    with caplog.at_level(logging.INFO, logger="emrg.server.scheduler"):
        asyncio.run(handler._run_evolution_cycle())
    assert handler._slowdown_active is True
    msgs = " ".join(r.message for r in caplog.records)
    assert "skipping scheduled run" not in msgs, \
        "old complete-halt log must not appear (rant 09:35:55)"
    assert "heartbeat" in msgs, msgs
    assert "log" in captured, "throttled tick must still run a full cycle"


def test_saturation_heartbeat_makes_no_network_calls(tmp_path):
    """Saturation judgment never touches the network (rant 2026-08-18T20:32:07
    — the old _remote_advanced ls-remote blocked the event loop; the check is
    gone entirely, recovery happens via the next vibe check).
    scheduler no longer imports subprocess at all (rant 2026-08-19T14:20:52
    deleted the self-heal git machinery) — no subprocess can be called."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    handler._slowdown_active = True

    assert not hasattr(mod, "subprocess"), \
        "scheduler must not import subprocess anymore (self-heal deleted)"
    assert handler._saturation_heartbeat_active() is True


def test_saturation_heartbeat_false_when_normal(tmp_path):
    """No throttle flag → normal interval (remote state irrelevant)."""
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    handler._slowdown_active = False
    assert handler._saturation_heartbeat_active() is False


def test_throttled_tick_still_runs_full_cycle(tmp_path):
    """Throttled handler runs a full cycle (never skipped) at heartbeat; a
    recommend=false vibe result clears the throttle afterwards."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "r1", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
        {"type": "vibe_check_result", "ok": True,
         "result": {"work": "reviewed PR #879", "recommend_slowdown": False,
                    "slowdown_reason": ""}},
    ])
    handler._slowdown_active = True  # throttled
    asyncio.run(handler._run_evolution_cycle())
    assert "log" in captured, "throttled tick must still run a full cycle"
    assert handler._slowdown_active is False, \
        "recommend=false restores normal cadence (heartbeat continues until then)"


def test_list_tasks_logs_slow_handler(tmp_path, caplog):
    """list_tasks must be pure in-memory; a slow handler.status() (>200ms)
    surfaces as a WARNING with a per-handler breakdown so the culprit is
    identifiable without manual profiling (rant 2026-08-18T20:48:45)."""
    import logging
    import time as _time
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    orig_status = handler.status

    def slow_status():
        _time.sleep(0.3)
        return orig_status()

    handler.status = slow_status
    sched = mod.TaskScheduler(InstanceIdentity())
    sched._handlers = [handler]
    with caplog.at_level(logging.WARNING, logger="emrg.server.scheduler"):
        tasks = sched.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["name"] == handler.name
    msgs = " ".join(r.message for r in caplog.records)
    assert "list_tasks took" in msgs, msgs
    assert ">200ms" in msgs, msgs
    assert handler.name in msgs, "per-handler breakdown must name the slow handler"


# ── Task CRUD + hot reload + templates (rant 2026-08-12T18:23:15 P2) ──


def _p2_env(tmp_path):
    """Point config_dir at tmp_path with a registered project."""
    from emrg.server import scheduler as mod
    projects_yml = tmp_path / "projects.yml"
    projects_yml.write_text(yaml.safe_dump([
        {"name": "emrg", "path": str(tmp_path / "emrg")},
        {"name": "mem", "path": str(tmp_path / "mem")},
    ]))
    orig = mod.config_dir
    mod.config_dir = lambda: tmp_path
    return mod, orig


def test_task_create_validation(tmp_path):
    """Invalid name / unknown type / unregistered project / interval<60 rejected."""
    from emrg.server import scheduler as mod
    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        ok, err = sched.task_create("Bad Name", "evolution", "emrg", 60)
        assert not ok and "invalid task name" in err
        ok, err = sched.task_create("good", "no-such-type", "emrg", 60)
        assert not ok and "unknown task type" in err
        ok, err = sched.task_create("good", "evolution", "not-registered", 60)
        assert not ok and "not registered" in err
        ok, err = sched.task_create("good", "evolution", "emrg", 30)
        assert not ok and ">= 60" in err
        ok, err = sched.task_create("good", "evolution", "emrg", "abc")
        assert not ok and ">= 60" in err
    finally:
        mod.config_dir = orig


def test_task_create_and_duplicate(tmp_path):
    """Valid task create persists to tasks.yml; duplicate rejected."""
    from emrg.server import scheduler as mod
    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        tasks_file = tmp_path / "tasks.yml"
        sched._tasks_file = tasks_file
        ok, task = sched.task_create("daily", "evolution", "mem", 300, repo="acme/x")
        assert ok and task["name"] == "daily" and task["interval"] == 300
        assert task["config"] == {"project": "mem", "repo": "acme/x"}
        saved = yaml.safe_load(tasks_file.read_text(encoding="utf-8"))
        assert any(t["name"] == "daily" for t in saved)
        ok, err = sched.task_create("daily", "evolution", "emrg", 60)
        assert not ok and "already exists" in err
    finally:
        mod.config_dir = orig


def test_task_update_and_delete(tmp_path):
    """Update changes fields; delete removes the entry; not-found errors."""
    from emrg.server import scheduler as mod
    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        sched.task_create("daily", "evolution", "mem", 300)
        ok, task = sched.task_update("daily", interval=600, enabled=False, repo="acme/y")
        assert ok and task["interval"] == 600 and task["enabled"] is False
        assert task["config"]["repo"] == "acme/y"
        ok, err = sched.task_update("nope", interval=60)
        assert not ok and "not found" in err
        ok, err = sched.task_delete("daily")
        assert ok and err == ""
        ok, err = sched.task_delete("daily")
        assert not ok and "not found" in err
    finally:
        mod.config_dir = orig


def test_apply_tasks_hot_reload(tmp_path):
    """apply_tasks diffs handlers: add / remove / restart on change (no daemon restart)."""
    from emrg.server import scheduler as mod
    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        sched._save_tasks([
            {"name": "a", "type": "evolution", "config": {"project": "emrg"}, "interval": 300, "enabled": True},
            {"name": "b", "type": "evolution", "config": {"project": "mem"}, "interval": 300, "enabled": True},
        ])

        async def _load_and_diff(new_tasks):
            sched.load_and_start()
            assert {h.name for h in sched._handlers} == {"a", "b"}
            summary = await sched.apply_tasks(new_tasks)
            return summary

        summary = asyncio.run(_load_and_diff([
            {"name": "a", "type": "evolution", "config": {"project": "emrg"}, "interval": 300, "enabled": True},
            {"name": "c", "type": "evolution", "config": {"project": "mem"}, "interval": 900, "enabled": True},
        ]))
        assert summary["removed"] == ["b"]
        assert summary["added"] == ["c"]
        assert summary["updated"] == []
        names = {h.name for h in sched._handlers}
        assert names == {"a", "c"}
        c = next(h for h in sched._handlers if h.name == "c")
        assert c.interval == 900
        sched.stop_all()
    finally:
        mod.config_dir = orig


def test_apply_tasks_update_restart(tmp_path):
    """Changing a task's interval restarts (stops + starts) its handler."""
    from emrg.server import scheduler as mod
    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"

        async def _run():
            sched._save_tasks([
                {"name": "a", "type": "evolution", "config": {"project": "emrg"}, "interval": 300, "enabled": True},
            ])
            sched.load_and_start()
            h_old = sched._handlers[0]
            summary = await sched.apply_tasks([
                {"name": "a", "type": "evolution", "config": {"project": "emrg"}, "interval": 600, "enabled": True},
            ])
            return h_old, summary

        h_old, summary = asyncio.run(_run())
        assert summary["updated"] == ["a"]
        assert summary["added"] == [] and summary["removed"] == []
        assert len(sched._handlers) == 1
        assert sched._handlers[0] is not h_old  # restarted
        assert sched._handlers[0].interval == 600
        sched.stop_all()
    finally:
        mod.config_dir = orig


def test_apply_tasks_idempotent(tmp_path):
    """Applying the same tasks is a no-op (no add/remove/update)."""
    from emrg.server import scheduler as mod
    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        tasks = [
            {"name": "a", "type": "evolution", "config": {"project": "emrg"}, "interval": 300, "enabled": True},
        ]

        async def _run():
            sched._save_tasks(tasks)
            sched.load_and_start()
            return await sched.apply_tasks(tasks)

        summary = asyncio.run(_run())
        assert summary == {"added": [], "removed": [], "updated": []}
        assert len(sched._handlers) == 1
        sched.stop_all()
    finally:
        mod.config_dir = orig


def test_hot_reload_offloads_handler_construction_to_thread():
    """rant 2026-08-19T01:05:47 — apply_tasks (hot reload, on the event loop
    while serving websockets) must not run TaskHandler's sync git probe on
    the loop. Construction goes through _start_handler_async →
    asyncio.to_thread(_build_handler); the boot path keeps the sync
    _start_handler_for (no clients connected yet)."""
    import inspect

    from emrg.server import scheduler as mod

    sched_src = inspect.getsource(mod.TaskScheduler)
    # apply_tasks awaits the async start path
    assert "await self._start_handler_async(cfg)" in sched_src
    # async start path offloads the sync construction
    assert "asyncio.to_thread(self._build_handler, cfg)" in sched_src
    # boot path unchanged (sync, pre-serve)
    assert "handler = self._build_handler(cfg)" in sched_src
    assert "def _start_handler_for(self, cfg: dict) -> TaskHandler:" in sched_src


def test_daemon_projects_list_offloads_git_probe_to_thread():
    """rant 2026-08-19T01:05:47 — the daemon's projects_list handler probes
    each project's git remote with a sync subprocess; it must run in worker
    threads (asyncio.to_thread) so a slow git probe never freezes the loop."""
    from pathlib import Path as _Path

    src = _Path(__file__).resolve().parent.parent / "emrg" / "server" / "daemon.py"
    content = src.read_text(encoding="utf-8")
    assert "asyncio.to_thread(_detect_git_remote, p.get(\"path\", \"\"))" in content
    assert "repos = await asyncio.gather(*(" in content


def test_template_crud_and_guards(tmp_path):
    """Custom templates: create/list/update/delete; builtin read-only; delete-refused guard."""
    from emrg.server import scheduler as mod
    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        # builtin read-only
        ok, err = sched.template_create("evolution", "x")
        assert not ok and "read-only" in err
        ok, err = sched.template_update("evolution", "x")
        assert not ok and "read-only" in err
        ok, err = sched.template_delete("evolution")
        assert not ok and "read-only" in err
        # create
        ok, err = sched.template_create("report", "# Report {{ instance_id }}")
        assert ok and err == ""
        ok, err = sched.template_create("report", "dup")
        assert not ok and "already exists" in err
        ok, err = sched.template_create("Bad Name", "x")
        assert not ok and "invalid template name" in err
        ok, err = sched.template_create("empty", "   ")
        assert not ok and "must not be empty" in err
        # list
        templates = {t["name"]: t for t in sched.list_templates()}
        assert templates["evolution"]["builtin"] is True
        # rant 09:17:45：builtin 附带 prompt 正文（GUI 只读 Monaco 查看器）
        assert templates["evolution"]["prompt"] and "instance_id" in templates["evolution"]["prompt"]
        assert templates["report"]["builtin"] is False
        assert "instance_id" in templates["report"]["prompt"]
        # update
        ok, err = sched.template_update("report", "# New")
        assert ok
        assert mod._read_custom_template("report") == "# New"
        ok, err = sched.template_update("missing", "x")
        assert not ok and "not found" in err
        # delete referenced → refused (host decision)
        sched.task_create("uses-report", "report", "mem", 300)
        ok, err = sched.template_delete("report")
        assert not ok and "1 task(s) use it" in err
        # delete after removing reference → ok
        sched.task_delete("uses-report")
        ok, err = sched.template_delete("report")
        assert ok and err == ""
        assert mod._read_custom_template("report") is None
    finally:
        mod.config_dir = orig


def test_task_create_custom_type(tmp_path):
    """A custom template type can be used to create a runnable task."""
    from emrg.server import scheduler as mod
    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        sched.template_create("report", "# Report {{ instance_id }}")

        async def _run():
            ok, task = sched.task_create("daily-report", "report", "mem", 300)
            assert ok and task["type"] == "report"
            sched._save_tasks([task])
            sched.load_and_start()
            h = next(h for h in sched._handlers if h.name == "daily-report")
            assert h._template_path == tmp_path / "task-templates" / "report.md"
            sched.stop_all()

        asyncio.run(_run())
    finally:
        mod.config_dir = orig


def test_evolution_template_renders_dual_project_match():
    """evolution_prompt.md renders the dual-compatible rant project match
    (rant 2026-08-17T12:09:57): both config.project AND owner/repo forms
    must be accepted when scanning rants."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "evolution_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    out = template.render(
        instance_id="test", host_name="host", uptime="0h 0m",
        repo_url="https://github.com/argszero/emrg.git", owner="argszero",
        repo="emrg", local_source="/tmp/evo", source_dir="/tmp/evo",
        session_id="s1", evolution_cwd="/tmp/evo", timestamp="20260817",
        task={"role": "committer", "project": "emrg"},
        project={}, evolution_count=0, git_path="git", gh_path="gh",
    )
    assert "emrg" in out, "task.project 值应渲染"
    assert "argszero/emrg" in out, "owner/repo 形式应渲染"
    assert "ignore rants without a `project` field entirely" in out
